"""Google Drive 同期スクリプト

OWNDAYS の Google Drive 内のフォルダを Personal Brain に取り込む。

使い方:
    # 1) フォルダ ID を発見 (キーワード検索)
    python3 gdrive_sync.py --discover "monday dash"
    python3 gdrive_sync.py --discover "WBR"

    # 2) 1 フォルダを取り込み (再帰、サブフォルダ含む)
    python3 gdrive_sync.py --folder <FOLDER_ID> --label monday-dash --visibility public

    # 3) 設定ファイル (.gdrive_sources.json) の全フォルダ取り込み (cron 用)
    python3 gdrive_sync.py --all

設定ファイル形式 (data/brain/.gdrive_sources.json):
[
  {"folder_id": "XXX", "label": "monday-dash", "visibility": "public", "recursive": true},
  {"folder_id": "YYY", "label": "wbr",         "visibility": "public", "recursive": true},
  ...
]

増分処理:
- data/brain/.gdrive_state.json に fileId → modifiedTime を記録
- 次回スキャンでは modifiedTime が変わったファイルのみ取り込み

取り込み対象 / 非対象:
- Google Docs / Sheets / Slides → text or CSV export → 取り込み
- PDF / DOCX / XLSX / PPTX → そのままダウンロード → 取り込み
- 画像 / 動画 / バイナリ → スキップ (バイナリ禁止)
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("gdrive_sync")

BASE_DIR = Path(__file__).parent
DATA_BRAIN = BASE_DIR / "data" / "brain"
IMPORT_DIR = DATA_BRAIN / "import"
WIKI_IMPORTED_DIR = DATA_BRAIN / "wiki" / "imported_drive"  # ★retrieval 用 wiki (Drive 専用)
# ★2026-06-28 personal ドメイン: 非OWNDAYS の個人 PJ/投資 (Example Garden 等)。OWNDAYS 出力からは
#   全経路で除外される wiki/personal/ 配下に直接書く (imported_drive/ には置かない=DA cross-check の
#   「imported_drive/personal/ は wiki/ 直下だが personal/ 配下でないので path filter を逃れて leak」回避)。
PERSONAL_WIKI_DIR = DATA_BRAIN / "wiki" / "personal"
STATE_FILE = DATA_BRAIN / ".gdrive_state.json"
SOURCES_FILE = DATA_BRAIN / ".gdrive_sources.json"
TOKEN_FILE = DATA_BRAIN / ".google_token.json"

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Google ネイティブファイル → export MIME マップ
GDOC_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
# そのままダウンロード対象 (file watcher が後で extract する)
BINARY_DOWNLOAD = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/markdown": ".md",
}


# ─── 認証 ───────────────────────────────────
# ★2026-07-13 cross-check DA D5 (REAL 高): discover 12 並列化で get_credentials が同時
# 多発するようになり、旧「非アトミック write_text + 無 lock」だと同時 refresh の torn write
# で token JSON が壊れ Drive 全断 (bot 検索 + 全 cron) のリスク。lock で intra-process の
# 多重 refresh を直列化し、os.replace (POSIX atomic) で cross-process の torn read も根絶。
_TOKEN_LOCK = threading.Lock()


def _atomic_write_token(text: str) -> None:
    import tempfile
    d = str(TOKEN_FILE.parent)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, str(TOKEN_FILE))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def get_credentials() -> Credentials:
    if not TOKEN_FILE.exists():
        raise SystemExit(
            f"トークンが見つかりません: {TOKEN_FILE}\n"
            f"先に google_sync.py を 1 度実行して OAuth を済ませてください"
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds.expired and creds.refresh_token:
        with _TOKEN_LOCK:
            # double-check: lock 待ちの間に別 thread が refresh 済みなら再読みで済ませる
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            if creds.expired and creds.refresh_token:
                logger.info("トークンをリフレッシュ中...")
                creds.refresh(Request())
                _atomic_write_token(creds.to_json())
    return creds


# ─── State 管理 ─────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── Discover (キーワードで Drive 全体検索) ───────────

# ★2026-05-26 海山指示 Phase 1: bot 検索 default の mime filter (= PDF 含む)
# 画像 / 動画 / フォルダ / 圧縮 等は bot 検索 default で除外、ノイズ削減。
# ★2026-06-07 海山指示「クリエイトリンク包括出店PJ (.pptx) が出ない」修正:
#   Google ネイティブ + PDF だけだと Office 形式 (.pptx/.xlsx/.docx) が検索から漏れる
#   (= bot は既に BINARY_DOWNLOAD でこれらを取込・extract 済なのに検索不可は不整合)。
#   PowerPoint / Excel / Word を default 検索対象に追加。画像・動画・圧縮は引き続き除外。
BOT_SEARCH_DEFAULT_MIMES = (
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/pdf",
    # Office 形式 (= 社内資料は Google ネイティブでなく Office 形式が多い)
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",          # .xlsx
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",    # .docx
)


def discover(
    query: str,
    mime: str | None = None,
    limit: int = 30,
    mode: str = "name",
    apply_default_exclude: bool = True,
    since_days: int | None = None,
    mime_filter: list[str] | None = None,
    content_check: bool = True,
) -> list[dict]:
    """Drive 全体でキーワード検索 (= file content + metadata 含む).

    ★2026-05-26 海山指示「Drive 内検索を bot 経由で」: fullText mode 追加.
    ★2026-05-26 Phase 1 (海山指示): since_days + mime_filter で noise 削減.

    Args:
        query: 検索キーワード
        mime: 'folder' なら folder のみ、その他 None で全 type (= legacy)
        limit: 最大件数 (default 30)
        mode: 'name' = filename match (旧 default、folder 検索向き)
              'fulltext' = name OR 中身に query 含む (★bot 検索 向き)
        apply_default_exclude: 結果に DEFAULT_EXCLUDE_PATTERN match (= 人事評価 / 給与 等) を fail-safe 除外
        since_days: 指定すると modifiedTime > today-N 日 で絞り込み (= 過去 N 日以内のみ)
        mime_filter: 指定 list の mime のみ (= PDF / sheets / docs / slides 限定等)
        content_check: fullText mode の本文 2 次判定を行うか (★2026-07-13 latency fix)。
            False = 名前/フォルダ除外のみで返す (= 呼び手が候補を絞ってから
            content_safe_filter で並列 2 次判定する分業。**表示/外部送信前に必ず
            content_safe_filter を通すのが呼び手の責務** = §1.9 保証は移動しただけで不変)
    """
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    # 中で ' を含むときに query が壊れるので escape。
    # ★security: backslash を先に escape しないと、末尾 `\` の query で `\'` の `\` が
    #   割れて Drive `q` 構文が破損する (= injection/parse error)。順序固定 (backslash → quote)。
    safe_query = query.replace("\\", "\\\\").replace("'", "\\'")

    # mode 別 query 組み立て
    if mode == "fulltext":
        # name OR fullText の OR query。Drive API は q 内で OR 使用可能。
        q_parts = [
            f"(name contains '{safe_query}' or fullText contains '{safe_query}')",
            "trashed=false",
        ]
    else:
        # 旧 default: name only
        q_parts = [f"name contains '{safe_query}'", "trashed=false"]

    if mime == "folder":
        q_parts.append("mimeType='application/vnd.google-apps.folder'")
    elif mime_filter:
        # bot 検索の noise 削減: 指定 mime のみ
        mime_clause = " or ".join(f"mimeType='{m}'" for m in mime_filter)
        q_parts.append(f"({mime_clause})")
    if since_days:
        # modifiedTime > today-N 日 (= RFC3339 UTC)
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        cutoff = (_dt.now(_tz.utc) - _td(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q_parts.append(f"modifiedTime > '{cutoff}'")
    q = " and ".join(q_parts)
    try:
        results = drive.files().list(
            q=q,
            pageSize=limit,
            fields="files(id, name, mimeType, modifiedTime, parents, webViewLink, owners(displayName,emailAddress))",
            corpora="allDrives",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            orderBy="modifiedTime desc",
        ).execute()
    except HttpError as e:
        logger.error(f"Drive search failed (mode={mode}): {e}")
        return []
    files = results.get("files", [])

    # ★fail-safe filter: 人事評価 / 給与 / 機密 等は default で除外 (DEFAULT_EXCLUDE_PATTERN)
    if apply_default_exclude:
        import re as _re
        rx_exc = _re.compile(DEFAULT_EXCLUDE_PATTERN, _re.IGNORECASE)
        # (1) 名前 + 親フォルダ + SALARY_PUBLIC override を集約した is_confidential_file で 1 次 filter。
        #     旧実装は rx_exc.search(name) のみで親フォルダ check / override が抜けていた。
        #     drive_service を渡すことで「給与」folder 配下 (file 名に marker 無) も name 経路で落とせる。
        parent_cache: dict = {}
        before = len(files)
        kept = []
        for f in files:
            is_conf, reason = is_confidential_file(
                f, drive_service=drive, parent_name_cache=parent_cache
            )
            if is_conf:
                logger.info(f"  default exclude (name/folder): {reason}")
                continue
            kept.append(f)
        files = kept
        if before != len(files):
            logger.info(f"  default exclude filter: dropped {before - len(files)} files")

        # (2) ★security fix (content-only marker 漏れ): fullText 検索は **本文** にも
        #     ヒットするため、file 名に marker が無い 給与/相談/評価 file が 1 次 filter を
        #     すり抜けて 存在・名前・owner が漏れる。fullText mode に限り、生き残った file の
        #     本文先頭を取得して DEFAULT_EXCLUDE_PATTERN で 2 次判定し落とす。
        #     name mode (= legacy/folder 検索) は本文ヒットが無いので追加 fetch 不要 → skip。
        #     SALARY_PUBLIC override は _content_is_confidential 内で維持 (集計給与は通す)。
        if mode == "fulltext" and files and content_check:
            before2 = len(files)
            kept2 = []
            for f in files:
                c_conf, c_reason = _content_verdict_cached(drive, f)
                if c_conf:
                    logger.info(f"  default exclude (content): {c_reason}")
                    continue
                kept2.append(f)
            files = kept2
            if before2 != len(files):
                logger.info(
                    f"  content exclude filter: dropped {before2 - len(files)} files "
                    f"(fullText content-only markers)"
                )
    return files


# ─── content 除外 verdict の cache + 並列版 (★2026-07-13 latency fix) ─────
# 海山「(Drive 検索が) 時間がかかり過ぎている」: 実測 8-9 分の支配項は content 2 次判定
# (1 file 5-15 秒 × 数十件 × 検索語ごとに同一 file を再チェック × 全直列)。
# verdict は (id, modifiedTime) key でプロセス内 cache = file が更新されたら自動再判定。
_CONTENT_VERDICT_CACHE: dict = {}
_CONTENT_VERDICT_CACHE_MAX = 4000


def _content_verdict_cached(drive, f: dict) -> tuple:
    key = (f.get("id", ""), f.get("modifiedTime", ""))
    hit = _CONTENT_VERDICT_CACHE.get(key)
    if hit is not None:
        return hit
    v = _content_is_confidential(drive, f)
    # ★unverifiable (本文取得不可 = 一過性 network/429 の可能性) は cache しない。
    # cache すると一度の transient 失敗で file がプロセス寿命の間ずっと不可視化する
    # (cross-check Reviewer/FC 指摘)。除外自体は fail-closed で今回も効く (返り値は True)。
    if "unverifiable" not in v[1]:
        if len(_CONTENT_VERDICT_CACHE) >= _CONTENT_VERDICT_CACHE_MAX:
            _CONTENT_VERDICT_CACHE.clear()  # 粗い eviction で十分 (再判定されるだけ)
        _CONTENT_VERDICT_CACHE[key] = v
    return v


def content_safe_filter(files: list[dict], max_workers: int = 6) -> list[dict]:
    """§1.9 content 2 次判定の**並列**版。入力順を保持して安全な file のみ返す。

    discover(content_check=False) と対の関数: 呼び手 (search_drive_semantic 等) が
    スコアで候補を絞った後、**表示/外部送信前に必ずこれを通す** (fail-closed は
    _content_is_confidential 側で維持 = 本文取得不可も除外)。
    googleapiclient は thread-unsafe のため thread ごとに service を build する。"""
    if not files:
        return []
    import threading
    from concurrent.futures import ThreadPoolExecutor

    creds = get_credentials()
    tls = threading.local()

    def _check(f: dict) -> tuple:
        key = (f.get("id", ""), f.get("modifiedTime", ""))
        hit = _CONTENT_VERDICT_CACHE.get(key)
        if hit is not None:
            return f, hit[0], hit[1]
        drive = getattr(tls, "drive", None)
        if drive is None:
            drive = build("drive", "v3", credentials=creds, cache_discovery=False)
            tls.drive = drive
        conf, reason = _content_is_confidential(drive, f)
        if "unverifiable" not in reason:  # transient 失敗は cache しない (恒久不可視化防止)
            if len(_CONTENT_VERDICT_CACHE) >= _CONTENT_VERDICT_CACHE_MAX:
                _CONTENT_VERDICT_CACHE.clear()
            _CONTENT_VERDICT_CACHE[key] = (conf, reason)
        return f, conf, reason

    kept = []
    dropped = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for f, conf, reason in ex.map(_check, files):  # map = 入力順保持 (スコア順を壊さない)
            if conf:
                dropped += 1
                logger.info(f"  default exclude (content, parallel): {reason}")
            else:
                kept.append(f)
    if dropped:
        logger.info(f"  content exclude filter (parallel): dropped {dropped} files")
    return kept


# ─── Shared Drive 全体を flat 列挙 (★2026-07-11) ────────────────
def _list_shared_drive_files(drive, drive_id: str) -> list[dict]:
    """共有ドライブ (driveId) 配下の非フォルダ全ファイルを flat 取得。
    corpora="drive"+driveId は drive 全体を横断するため再帰不要 (サブフォルダ配下も 1 回で取れる)。"""
    out: list[dict] = []
    page_token = None
    while True:
        try:
            results = drive.files().list(
                corpora="drive",
                driveId=drive_id,
                q="trashed=false and mimeType != 'application/vnd.google-apps.folder'",
                pageSize=200,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            logger.warning(f"  shared-drive list fail (driveId={drive_id}): {e}")
            break
        out.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    logger.info(f"  shared-drive {drive_id}: {len(out)} files (flat)")
    return out


# ─── フォルダ内列挙 (再帰可) ────────────────
def list_folder_files(
    drive, folder_id: str, recursive: bool = True, depth: int = 0, max_depth: int = 5
) -> list[dict]:
    if depth > max_depth:
        logger.warning(f"  max_depth {max_depth} 到達、それ以上潜らない")
        return []
    # ★2026-07-11 Shared Drive root 修正: 共有ドライブ ID (先頭 "0A") を通常フォルダと同じ
    #   corpora="allDrives" + "'{id}' in parents" で引くと **サブフォルダ配下のファイルが取れず
    #   silent に欠落** (実測: 社内規程ドライブで 5 件 vs 正しくは 84 件、= 規程 PDF が全滅で
    #   制度 FAQ が空だった真因)。共有ドライブは corpora="drive"+driveId で drive 全体を flat 取得。
    if folder_id.startswith("0A"):
        return _list_shared_drive_files(drive, folder_id)
    out: list[dict] = []
    page_token = None
    while True:
        try:
            results = drive.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                pageSize=200,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink)",
                corpora="allDrives",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageToken=page_token,
            ).execute()
        except HttpError as e:
            logger.warning(f"  list_folder_files fail: {e}")
            break
        for f in results.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                if recursive:
                    out.extend(
                        list_folder_files(drive, f["id"], True, depth + 1, max_depth)
                    )
            else:
                out.append(f)
        page_token = results.get("nextPageToken")
        if not page_token:
            break
    return out


# ─── ファイル取得 ───────────────────────────
def _safe_filename(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    return name[:120]


def _write_wiki_imported(label: str, name: str, mime: str, file_id: str, web_link: str, modified: str, visibility: str, text: str | None = None, binary_note: str | None = None, domain: str = "", max_body_chars: int = 12000) -> None:
    """取り込んだ Drive ファイルを retrieval 用 markdown として書く。LLM compile せず直接置く。

    domain=="personal" → wiki/personal/<label>/ (非OWNDAYS、**常に private**)。
    それ以外 → wiki/imported_drive/<label>/ (OWNDAYS、従来どおり)。
    max_body_chars: 本文の truncate 上限 (★2026-07-03 DA-3: 規程原文は全文が要るため呼び出し側で拡大可)。
    """
    if domain == "personal":
        label_dir = PERSONAL_WIKI_DIR / label
        visibility = "private"   # ★personal は config 誤設定でも昇格させない (fail-safe)
    else:
        label_dir = WIKI_IMPORTED_DIR / label
    label_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(name)
    md_path = label_dir / f"{safe}.md"
    fm = (
        f"---\n"
        f"updated: {datetime.now().date().isoformat()}\n"
        f"source: gdrive\n"
        f"source_label: {label}\n"
        f"source_link: {web_link}\n"
        f"source_id: {file_id}\n"
        f"source_modified: {modified}\n"
        f"source_mime: {mime}\n"
        f"clone_visibility: {visibility}\n"
        f"tags: [Google Drive, {label}]\n"
        f"---\n"
        f"# {name}\n\n"
    )
    body = ""
    if text is not None:
        # 大きい時は truncate (vector search は chunks に分割するから上限は緩め)
        body = text[:max_body_chars]
        if len(text) > max_body_chars:
            body += f"\n\n...(truncated; original {len(text)} chars)"
    elif binary_note:
        body = binary_note
    md_path.write_text(fm + body, encoding="utf-8")


def _extract_personal_binary_text(
    data: bytes, ext: str, max_chars: int = 5000, max_pages: int = 20
) -> str | None:
    """personal binary (PDF/DOCX/XLSX 等) の本文を inline 抽出する。

    ★2026-06-28: personal は IMPORT_DIR (OWNDAYS file-watcher → LLM compile → OWNDAYS wiki) を
    経由させたくない (= leak)。content_extractor.extract_file_text を temp file 経由でその場で呼び、
    本文だけ取り出して wiki/personal/ に書く。失敗時 None (= pointer stub に fallback)。

    max_chars/max_pages は content_extractor の既定 (5000/20) を継承 (= personal の従来挙動)。
    ★2026-07-08: deterministic_text (規程/本部会議資料 等の全文取込) の呼び出し側は
    write 側 max_body_chars (60000) に見合う値を明示指定する (= 既定 5000/20 での silent
    truncation を回避、§DA-3 の「全文を決定論で書く」設計を実効化)。
    """
    import asyncio as _aio
    import tempfile
    from pathlib import Path as _P
    try:
        from content_extractor import extract_file_text
    except Exception:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tf:
            tf.write(data)
            tf.flush()
            return _aio.run(
                extract_file_text(_P(tf.name), max_chars=max_chars, max_pages=max_pages)
            )
    except Exception as e:
        logger.warning(f"personal binary 抽出失敗 ({ext}): {type(e).__name__}: {e}")
        return None


def fetch_and_save(
    drive, file: dict, label: str, visibility: str, domain: str = "",
    deterministic_text: bool = False,
) -> tuple[bool, str]:
    """Drive ファイルを取り込む。

    domain=="personal" → wiki/personal/ に直接置き IMPORT_DIR を経由させない
    (= OWNDAYS watcher/compile に渡さない leak 防止、binary は inline 抽出)。
    deterministic_text=True (★2026-07-03 DA-3、規程原文用) → OWNDAYS domain のまま
    IMPORT_DIR を経由させず inline 抽出 → wiki/imported_drive/ に**全文を決定論で**書く
    (= §1.7 の精神: 法務文書を LLM compile の paraphrase に通さない + visibility は config 値のまま)。
    それ以外 → 従来どおり import/ + wiki/imported_drive/ に書く。返り値: (saved, reason)
    """
    mime = file["mimeType"]
    name = _safe_filename(file["name"])
    file_id = file["id"]
    web_link = file.get("webViewLink", "")
    modified = file.get("modifiedTime", "")

    # Google ネイティブ (Docs/Sheets/Slides) → export
    if mime in GDOC_EXPORT:
        export_mime = GDOC_EXPORT[mime]
        ext = ".csv" if export_mime == "text/csv" else ".txt"
        try:
            data = drive.files().export(fileId=file_id, mimeType=export_mime).execute()
            text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        except HttpError as e:
            return False, f"export error: {e}"
        # frontmatter 添付してから保存
        body = (
            f"# {file['name']}\n\n"
            f"source: Google Drive\n"
            f"source_label: {label}\n"
            f"source_link: {web_link}\n"
            f"source_id: {file_id}\n"
            f"modified: {modified}\n"
            f"clone_visibility: {visibility}\n\n"
            f"---\n\n"
            f"{text}"
        )
        # ★2026-06-28 personal: IMPORT_DIR を経由させず wiki/personal/ に text stub を直接書く
        if domain == "personal":
            _write_wiki_imported(label, name, mime, file_id, web_link, modified, visibility,
                                 text=text, domain="personal")
            return True, f"saved personal (text): {name}"
        # ★2026-07-03 DA-3: 決定論 entry は IMPORT_DIR (LLM compile) 非経由で全文を wiki へ
        if deterministic_text:
            _write_wiki_imported(label, name, mime, file_id, web_link, modified, visibility,
                                 text=text, max_body_chars=60000)
            return True, f"saved deterministic (text): {name}"
        out_path = IMPORT_DIR / f"gdrive_{label}_{name}{ext}"
        out_path.write_text(body, encoding="utf-8")
        # ★retrieval 用 wiki にも書く
        _write_wiki_imported(label, name, mime, file_id, web_link, modified, visibility, text=text)
        return True, f"saved (text): {out_path.name}"

    # PDF / DOCX 等 → そのままダウンロード
    if mime in BINARY_DOWNLOAD:
        ext = BINARY_DOWNLOAD[mime]
        try:
            # ★2026-07-03 cross-check fact-checker: shared drive の file は
            # supportsAllDrives=True が無いと download が失敗し得る (metadata 系は指定済だった)
            request = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buf.seek(0)
            data = buf.read()
        except HttpError as e:
            return False, f"download error: {e}"
        # ★2026-06-28 personal: IMPORT_DIR を経由させず inline 抽出 → wiki/personal/ に text stub
        #   (OWNDAYS file-watcher の LLM compile に渡さない = leak 防止)。
        if domain == "personal":
            ptext = _extract_personal_binary_text(data, ext)
            _write_wiki_imported(
                label, name, mime, file_id, web_link, modified, visibility,
                text=ptext or None,
                binary_note=(None if ptext else f"バイナリ ({ext})。本文抽出できず。原本: {web_link}"),
                domain="personal",
            )
            return True, f"saved personal (binary {ext}{'+text' if ptext else ''}): {name}"
        # ★2026-07-03 DA-3: 決定論 entry (規程原文) は inline 抽出 → 全文を wiki へ (compile 非経由)
        # ★2026-07-08: write 側 max_body_chars=60000 に見合う抽出上限を明示 (既定 5000/20 だと
        #   多ページの財務/本部会議 PDF が silent truncation されるバグを修正、adversarial 検証で検出)。
        if deterministic_text:
            dtext = _extract_personal_binary_text(data, ext, max_chars=60000, max_pages=120)
            _write_wiki_imported(
                label, name, mime, file_id, web_link, modified, visibility,
                text=dtext or None,
                binary_note=(None if dtext else f"バイナリ ({ext})。本文抽出できず。原本: {web_link}"),
                max_body_chars=60000,
            )
            return True, f"saved deterministic (binary {ext}{'+text' if dtext else ''}): {name}"
        out_path = IMPORT_DIR / f"gdrive_{label}_{name}{ext}"
        out_path.write_bytes(data)
        # メタ情報を別ファイルに置く (file watcher がメインの ext を拾う、メタは別管理用)
        meta_path = IMPORT_DIR / f"gdrive_{label}_{name}{ext}.meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "source": "gdrive",
                    "source_label": label,
                    "source_link": web_link,
                    "source_id": file_id,
                    "modified": modified,
                    "clone_visibility": visibility,
                    "name": file["name"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        # ★retrieval 用 wiki にも参照行だけ書く (バイナリ本体は wiki に入れない)
        _write_wiki_imported(
            label, name, mime, file_id, web_link, modified, visibility,
            binary_note=(
                f"バイナリファイル ({ext})。\n"
                f"本体は file watcher が `{out_path.name}` から抽出してraw/notesに置く。\n"
                f"中身検索: 取り込み後 vector search で raw 経由でも見える、\n"
                f"または Google Drive 側で原本を参照: {web_link}"
            ),
        )
        return True, f"saved (binary {ext}): {out_path.name}"

    # その他 (画像 / 動画 / unknown) → スキップ
    return False, f"skip (mime={mime})"


# ─── Sync 1 フォルダ (★selective フィルタ付き、wiki 散らかし防止) ────
# 個人情報・機密ファイルを誤って取り込まないための **デフォルト exclude pattern**
# ★2026-05-07: 退職・休職・離職・採用候補・履歴書 も追加 (個人名 PII リスク)
# ★2026-05-26 海山指示: 個人情報 + 採用関連を expand、3 bypass 経路にも適用、フォルダ名 check も追加。
# ★2026-05-27 海山指示: (k) 相談 / 面談 / 個別 communication 系 追加 (= 「相談対応ログ」 等 PII 高 risk).
# カテゴリ:
#  (a) 人事評価 / 給与 / 考課 系
#  (b) 退職 / 休職 等 ステータス変化
#  (c) 採用 / 選考 / 面接
#  (d) 個人情報 / PII (マイナンバー / 住民票 等)
#  (e) 健康 / メンタル (健康診断 / 病歴 等)
#  (f) 懲戒 / 処分
#  (g) 給与詳細 (賃金台帳 / 源泉徴収 / 退職金 等)
#  (h) 機密 / 社外秘
#  (i) credentials / secret
#  (k) 相談 / 面談 / 個別 communication (★2026-05-27 海山指示)
# ★2026-08-09: **ドライブ単位**の取り込み禁止 (ファイル名 filter の手前に置く最初の門)。
# 背景: DEFAULT_EXCLUDE_PATTERN はファイル名しか見ないため、「共有ドライブごと入れてはいけない」
# という単位の意思を表現できなかった。実際 umiyama-ai は共有ドライブ「人事評価シート」に
# アクセスでき、名前に評価 marker を持たないファイル (例: 店舗別の集計シート) は素通りする。
# 議事録の一括取込を始めるにあたり、**ドライブ名/ID で先に丸ごと落とす**層を置く。
# ここに足したものは folder_id 指定でも recursive 走査でも入らない (fail-closed)。
DENY_DRIVE_NAMES: frozenset = frozenset({
    "人事評価シート",          # §1.9 (a) 人事評価
    "情報セキュリティ委員会",   # インシデント記録 (未公表の脆弱性/事故)
    "リスク管理委員会",         # 同上 + §1.9 (k) 相談/通報系
})


def is_denied_drive(drive, drive_or_folder_id: str, _cache: dict = {}) -> bool:
    """共有ドライブ単位の取り込み禁止判定。判定不能なら **禁止側に倒す** (fail-closed)。"""
    if not drive_or_folder_id:
        return False
    if drive_or_folder_id in _cache:
        return _cache[drive_or_folder_id]
    try:
        if drive_or_folder_id.startswith("0A"):      # 共有ドライブ ID そのもの
            name = drive.drives().get(driveId=drive_or_folder_id,
                                      fields="name").execute().get("name", "")
        else:                                        # フォルダ → 所属ドライブを引く
            meta = drive.files().get(fileId=drive_or_folder_id, fields="driveId",
                                     supportsAllDrives=True).execute()
            did = meta.get("driveId")
            if not did:
                _cache[drive_or_folder_id] = False   # マイドライブ配下 = 対象外
                return False
            name = drive.drives().get(driveId=did, fields="name").execute().get("name", "")
    except Exception as e:
        logger.warning(f"drive denylist 判定不能 ({drive_or_folder_id}): {e} → 禁止側に倒す")
        return True
    denied = name in DENY_DRIVE_NAMES
    if denied:
        logger.warning(f"★取り込み禁止ドライブ: {name} ({drive_or_folder_id})")
    _cache[drive_or_folder_id] = denied
    return denied


DEFAULT_EXCLUDE_PATTERN = (
    # (a) 人事評価 / 給与 / 考課 系
    r"(人事評価|個人評価|給与|考課|処遇|評価シート|ヒアリングシート|目標設定シート|"
    # (b) 退職 / 休職 / 離職 / 休業
    r"退職|休職|離職|休業|休暇申請|"
    # (c) 採用 / 選考 / 面接 / 履歴書 (★expand)
    r"履歴書|職務経歴|採用候補|採用面接|採用試験|採用通知|採用合否|"
    r"内定者|内定通知|応募者|応募書類|"
    r"選考|面接記録|面接評価|スカウト|ヘッドハント|オファーレター|"
    # (d) 個人情報 / PII (★expand)
    r"個人情報|マイナンバー|住民票|在留カード|戸籍|"
    # (e) 健康 / メンタル (★expand)
    r"健康診断|診断書|病歴|通院|メンタルヘルス|休職診断|"
    # (f) 懲戒 / 処分 (★expand)
    r"懲戒|処分通知|始末書|警告書|"
    # (g) 給与詳細 (★expand)
    r"賃金台帳|源泉徴収|退職金|福利厚生申請|年末調整|"
    # (h) 機密 / 社外秘
    r"機密|社外秘|極秘|"
    # (k) 相談 / 面談 / 個別 communication (★2026-05-27 海山指示: 「相談対応ログ」 PII 高 risk 検知)
    # = 社員相談 / ハラスメント / メンタル相談 / 1on1 / 個別面談 等の personal communication record
    r"相談対応|相談記録|相談ログ|相談履歴|相談窓口|"
    r"ハラスメント相談|メンタル相談|キャリア相談|個別相談|"
    r"面談記録|面談ログ|面談履歴|個別面談|1on1|1 ?on ?1|"
    r"通報|内部通報|"
    # (i) 英語 keyword (= 海外資料用)
    r"confidential|personnel|salary|compensation|payroll|equity|stock\s*option|"
    r"resume|cv\b|background\s*check|"
    r"performance\s*review|disciplinary|termination|"
    r"recruitment|interview\s*notes|offer\s*letter|"
    r"medical\s*record|health\s*check|"
    r"counseling|consultation\s*log|grievance|harassment\s*report|whistleblow|"
    # (j) credentials / secret (= bot が読むと事故るので block)
    r"パスワード一覧|password\s*list|秘密鍵|private\s*key|api\s*key|credential)"
)

# Drive API の `q` field に注入する高頻出 keyword (= server-side 除外で帯域節約)。
# Post-hoc の Python regex (= DEFAULT_EXCLUDE_PATTERN) が full coverage、こちらは速度補助。
# Drive API は `not name contains 'X'` operator をサポートするが、12 個並べると
# 「Bad Request 400」 を返すため **top 5 keyword に削減** (= 経験的に 5 以下なら安定)。
# 残りの 20+ keyword は post-hoc Python regex で完全 cover、機能的に等価。
# ★2026-05-26 海山指示: 「給与」 単独 server-side 除外を外す (= 集計/公開 keyword を post-hoc で
#   override する設計に変更、SALARY_PUBLIC_PATTERN で「リーグ別店長給与」 等を 通過させる)。
DEFAULT_EXCLUDE_QUERY_KEYWORDS = (
    "人事評価", "考課", "機密", "社外秘",
)


# ─── 集計/公開 給与情報 override (★2026-05-26 海山指示) ──────────
# 「個人と紐付かない、公開されてる」 給与情報 (= 給与レンジ、給与体系、リーグ別店長給与、
#  SV/AM 給与テーブル 等) は DEFAULT_EXCLUDE_PATTERN の「給与」hit を override で通す.
# ただし、個別性 marker (= 個人別 / 氏名 / 名簿 等) が file 名にあれば override を 拒否.
#
# Examples:
#   「給与レンジ.xlsx」 → DEFAULT hit + SALARY_PUBLIC hit + 個別 marker 無 → **通す**
#   「店長給与 リーグ別.xlsx」 → 同上 → **通す**
#   「SV給与テーブル.xlsx」 / 「AM給与表 2026.xlsx」 → 同上 → **通す**
#   「給与一覧 個人別.xlsx」 → DEFAULT hit + 個別 marker hit → **block**
#   「給与一覧 全社員.xlsx」 → DEFAULT hit + SALARY_PUBLIC 無 → **block** (= safe side)
#   「人事評価 2026.xlsx」 → (a) hit / SALARY_PUBLIC 外 → **block** (= 評価は override 対象外)
#   「健康診断 結果.xlsx」 → (e) hit / SALARY_PUBLIC 外 → **block**
SALARY_PUBLIC_PATTERN = (
    # 集計レンジ系
    r"(給与レンジ|給与水準|給与体系|給与表|給与テーブル|給与制度|"
    r"報酬体系|報酬制度|報酬テーブル|報酬レンジ|"
    # 役職別テーブル (= 個人特定不可な集計)
    r"店長給与|店長報酬|SV給与|SV報酬|AM給与|AM報酬|"
    r"職位別|役職別|"
    # 英語 keyword
    r"salary\s*range|salary\s*table|salary\s*band|"
    r"compensation\s*band|compensation\s*table|"
    r"pay\s*band|pay\s*scale|pay\s*grade|grade\s*table)"
)

# 個別性 marker (= これが name / folder に含まれてれば SALARY_PUBLIC override を 拒否)
# = 集計値 keyword あっても 個人特定可能性ある file は block を維持
# ★ note: 「個別」 は word boundary なしで catch (= `\b` は日本語境界に効かない)。
# 「個別評価」「個別面談」 等で hit、誤発火 risk は給与系 file 名では稀.
PERSONAL_MARKER_PATTERN = (
    r"(個人別|個別|社員別|氏名|姓名|名簿|"
    r"per\s*employee|by\s*name|individual|name\s*list)"
)


def _check_salary_public_override(text: str) -> bool:
    """text (file 名 / folder 名) が SALARY_PUBLIC marker hit + 個別 marker 無 で override 可か判定.

    ★2026-05-26 海山指示: 「個人と紐付かない、公開されてる」 給与情報は機密 exclude を override.
    Returns True なら exclude を pass (= 通す)、False なら exclude 維持 (= block).
    """
    import re as _re
    if not text:
        return False
    rx_pub = _re.compile(SALARY_PUBLIC_PATTERN, _re.IGNORECASE)
    if not rx_pub.search(text):
        return False  # 集計 marker 無 → override 不可
    rx_personal = _re.compile(PERSONAL_MARKER_PATTERN, _re.IGNORECASE)
    if rx_personal.search(text):
        return False  # 個別 marker hit → override 拒否 (= safe side)
    return True


# ─── 規程原文 override (★2026-07-03 海山指示 P2a = 6/15 決定の公開規程取込) ──────────
# DEFAULT_EXCLUDE_PATTERN は **record** (評価記録/相談ログ/個人データ) を止めるのが目的だが、
# 話題語での name match のため **rulebook** (規程原文) も誤 block していた:
#   「社員給与規程」(給与 hit) / 「育児・介護休業規程」(休業 hit) / 「懲戒手続に関する細則」(懲戒 hit)
#   「個人情報保護規程」(個人情報 hit) / 「内部通報規程」(通報 hit) — 全て 6/15 決定の社内公開規程。
# override 条件 (両方必須):
#   (1) 名前に規程文書 marker (規程/規則/細則/規定/内規) がある
#   (2) record/secret marker が一切無い (記録/ログ/個人別/結果/機密 等 → あれば block 維持)
# 例: 「懲戒処分記録_規程違反者一覧.xlsx」 = (1) hit でも 記録 hit → block 維持 (fail-safe)。
REGULATION_DOC_PATTERN = r"(規程|規則|細則|規定|内規)"
REGULATION_OVERRIDE_DENY = (
    r"(個人別|個別|社員別|氏名|姓名|名簿|記録|ログ|履歴|台帳|結果|評価|考課|"
    # ★cross-check reviewer C-1 + DA-2: 「人の集合/個別事案」文書 marker。
    # 「給与一覧(規程改定版)」「懲戒処分一覧_規程対応」「退職金規程_計算例(田中)」等が
    # 規程 mention だけで素通りする穴を塞ぐ。「規程一覧」(目次) も deny されるが fail-safe 側で許容。
    # 「相談対応」は §1.9(k) 海山指示 (5/27) の高 risk カテゴリのため規程名でも反転させない
    # (= 「相談対応規程」は block 維持。内部通報規程は 通報 が deny 外なので通る)。
    r"一覧|リスト|シート|予定表|明細|処分|対象者|支給額|計算例|新旧対照|相談対応|"
    r"申請書|通知書|診断書|履歴書|始末書|警告書|"
    r"機密|社外秘|極秘|パスワード|password|秘密鍵|private\s*key|api\s*key|credential|"
    r"per\s*employee|by\s*name|individual|name\s*list|list|sheet)"
)


def _check_regulation_doc_override(text: str) -> bool:
    """text (file 名) が規程原文 (rulebook) として exclude を override 可か判定.

    Returns True = 通す / False = exclude 維持。folder 名には適用しない
    (「規程」folder 下の record file を folder 名 override で通してしまう事故防止、
    file 名自身が両条件を満たす時のみ)。
    """
    import re as _re
    if not text:
        return False
    if not _re.search(REGULATION_DOC_PATTERN, text, _re.IGNORECASE):
        return False  # 規程文書 marker 無 → override 不可
    if _re.search(REGULATION_OVERRIDE_DENY, text, _re.IGNORECASE):
        return False  # record/secret marker hit → 拒否 (= safe side)
    return True


def is_confidential_file(file_dict: dict, drive_service=None, parent_name_cache: dict | None = None) -> tuple[bool, str]:
    """ファイルが機密情報かを判定 (= 名前 + 親フォルダ名 両方 check)。

    Args:
        file_dict: Drive API から返った file dict (= id / name / parents 等)
        drive_service: 親フォルダ名取得用の Drive service。None なら名前のみ check (= フォルダ skip)
        parent_name_cache: parent_id → folder_name の lookup cache (= 同 batch 内の重複 API call 削減)

    Returns:
        (is_confidential, reason). reason は何が hit したか (例: "name match: 人事評価2026.xlsx")
    """
    import re as _re
    rx = _re.compile(DEFAULT_EXCLUDE_PATTERN, _re.IGNORECASE)
    # (1) ファイル名 check
    name = file_dict.get("name", "")
    m = rx.search(name)
    if m:
        # ★2026-05-26 海山指示: SALARY_PUBLIC override (= 集計/公開 給与は通す)
        if _check_salary_public_override(name):
            return False, ""
        # ★2026-07-03 海山指示 P2a: 規程原文 (rulebook) override
        if _check_regulation_doc_override(name):
            return False, ""
        return True, f"name match '{m.group(0)}': {name}"
    # (2) 親フォルダ名 check (= drive_service 必須)
    if drive_service is None:
        return False, ""
    parents = file_dict.get("parents", [])
    if not parents:
        return False, ""
    if parent_name_cache is None:
        parent_name_cache = {}
    for pid in parents[:1]:  # 直接の親のみ (= 多段は対象外、性能 trade-off)
        if pid in parent_name_cache:
            folder_name = parent_name_cache[pid]
        else:
            try:
                meta = drive_service.files().get(
                    fileId=pid, fields="name", supportsAllDrives=True,
                ).execute()
                folder_name = meta.get("name", "")
                parent_name_cache[pid] = folder_name
            except Exception:
                folder_name = ""
                parent_name_cache[pid] = ""
        if folder_name:
            m = rx.search(folder_name)
            if m:
                # ★2026-05-26 海山指示: folder 名 も SALARY_PUBLIC override 対象
                # = 「給与体系」 folder 下の file は file 名次第で通す (= ただし file 名側で
                #    個別 marker hit すれば結局 (1) で block されるので safe)
                if _check_salary_public_override(folder_name) and _check_salary_public_override(name) is not False:
                    # folder 集計 marker + file 名 個別 marker 無 → 通す
                    # ※ _check_salary_public_override(name) は file 名に SALARY_PUBLIC が無くても True にならないので
                    #    file 名は中立 (= DEFAULT 全部 hit せず通過したもの) なら folder override で通す
                    # 厳密化: file 名側で個別 marker hit してたら 拒否
                    rx_personal = _re.compile(PERSONAL_MARKER_PATTERN, _re.IGNORECASE)
                    if not rx_personal.search(name):
                        return False, ""
                return True, f"parent folder match '{m.group(0)}': folder={folder_name}, file={name}"
    return False, ""


def build_drive_exclude_clause() -> str:
    """Drive API `q` 用の `not name contains '...'` 連結句を返す。
    主要 12 keyword で server-side 除外、残りは post-hoc Python regex で full filter。
    """
    return " and ".join(
        f"not name contains '{kw}'" for kw in DEFAULT_EXCLUDE_QUERY_KEYWORDS
    )


# ─── fullText hit の本文 2 次判定 (★security fix: content-only marker 漏れ対策) ──
# 背景: bot 経路 (search_drive_semantic → discover(mode="fulltext")) は Drive の
#   `fullText contains` で **ファイル中身** にもヒットする。一方 apply_default_exclude の
#   除外は file 名のみ (= rx_exc.search(name))。給与/相談/評価 file が名前に marker 無く
#   本文だけに該当すると、存在・名前・owner・link が全社員に漏れる (CLAUDE.md 1.9 の公開境界違反)。
# 対策: fullText hit で名前 filter を生き残った file の本文先頭を取得し、
#   DEFAULT_EXCLUDE_PATTERN で 2 次判定して落とす。SALARY_PUBLIC override は維持
#   (= 集計給与レンジ等は本文に「給与」があっても通す)。
# fail-closed (★2026-06-29 海山指示・codex HIGH [2]): 本文取得に失敗 (空 snippet) したら
#   「機密か検証できない」= §1.9 を優先し **保守的に除外** (漏らさない側に倒す)。旧実装は no-op で
#   通しており、本文だけに PII marker を持つ binary (名前は中立) を漏らす穴だった。代償は fullText
#   検索で本文を読めない file が結果から減ること (= 安全側、許容)。SALARY_PUBLIC override は維持。
#   残課題: PDF/Office は冒頭 raw 素読みのため本文 marker を取り逃す場合あり (深い抽出は未実施)。
CONTENT_SNIPPET_BYTES = 4000  # 本文先頭 N bytes (= marker は冒頭/見出しに出やすい、帯域節約)


def _fetch_content_snippet(drive, file_dict: dict) -> str:
    """file の本文先頭を軽量取得 (= export/get_media)。取得不可なら空文字。

    Google ネイティブは text export、Office/PDF/text は get_media を試みる。
    バイナリ (PDF/Office) は raw bytes をベストエフォートで decode (= marker は
    plain text 断片で出ることが多い、抽出器までは回さず冒頭の素読みで足切り)。
    """
    mime = file_dict.get("mimeType", "")
    fid = file_dict.get("id", "")
    if not fid:
        return ""
    try:
        if mime in GDOC_EXPORT:
            data = drive.files().export(
                fileId=fid, mimeType=GDOC_EXPORT[mime]
            ).execute(num_retries=3)  # ★D3: 一過性 429/5xx を backoff retry (偽「該当無し」抑制)
            text = data.decode("utf-8", "ignore") if isinstance(data, bytes) else str(data)
            return text[:CONTENT_SNIPPET_BYTES * 3]  # text は素直なので少し広めに
        # それ以外 (PDF/Office/text/csv 等) は冒頭 chunk だけ get_media
        # (★2026-07-03: shared drive 対応で supportsAllDrives 付与)
        request = drive.files().get_media(fileId=fid, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request, chunksize=CONTENT_SNIPPET_BYTES)
        # 最初の 1 chunk (= CONTENT_SNIPPET_BYTES) だけ取れれば足切り判定に十分
        try:
            downloader.next_chunk(num_retries=3)  # ★D3: transient error は backoff retry
        except Exception:
            pass
        raw = buf.getvalue()[:CONTENT_SNIPPET_BYTES]
        # PDF/Office は zip/binary だが、marker (給与/相談 等) が UTF-8 断片で出れば拾える
        return raw.decode("utf-8", "ignore")
    except Exception as e:
        logger.debug(f"  content snippet fetch failed for {file_dict.get('name','?')}: {e}")
        return ""


def _content_is_confidential(drive, file_dict: dict) -> tuple[bool, str]:
    """file 本文先頭が DEFAULT_EXCLUDE_PATTERN に該当するか (= content-only marker 検知).

    SALARY_PUBLIC override 維持: 本文が SALARY_PUBLIC marker hit かつ 個別 marker 無なら通す
    (= 集計給与レンジ等)。file 名は呼び元で既に name-based filter 通過済を前提。
    Returns: (is_confidential, reason). 取得失敗 (空 snippet) は §1.9 優先で (True, ...) = fail-closed 除外。
    """
    import re as _re
    snippet = _fetch_content_snippet(drive, file_dict)
    if not snippet:
        # ★fail-closed (codex HIGH [2] 2026-06-29): 本文を検証できない = 機密の可能性を排除できない。
        # §1.9 を優先し保守的に除外 (旧 no-op は本文 marker のみの binary を漏らす穴だった)。
        return True, "本文取得不可 → §1.9 で保守的に除外 (content unverifiable)"
    rx = _re.compile(DEFAULT_EXCLUDE_PATTERN, _re.IGNORECASE)
    m = rx.search(snippet)
    if not m:
        return False, ""
    # 本文に exclude marker hit。集計給与の override 判定 (= 本文 + file 名 両方で個別性 check)。
    name = file_dict.get("name", "")
    if _check_salary_public_override(snippet) and not _re.compile(
        PERSONAL_MARKER_PATTERN, _re.IGNORECASE
    ).search(name):
        return False, ""  # 集計給与 (本文) + file 名に個別 marker 無 → 通す
    return True, f"content match '{m.group(0)}': {name}"


def sync_folder(
    folder_id: str,
    label: str,
    visibility: str = "public",
    recursive: bool = False,
    max_age_days: int | None = 90,
    pattern: str | None = None,
    exclude_pattern: str | None = None,
    max_files: int | None = 50,
    force: bool = False,
    domain: str = "",
    allow_regulation_override: bool = False,
    deterministic_text: bool = False,
) -> dict:
    """1 フォルダをスキャンして selective に取り込み

    domain=="personal" → wiki/personal/<label>/ に直接 (非OWNDAYS、常に private、IMPORT_DIR 非経由)。

    フィルタ (wiki が散らからないように):
        recursive: サブフォルダ降りるか (default False、トップレベルのみ)
        max_age_days: 修正日が N 日以内のファイルのみ (default 90)
        pattern: ファイル名 regex マッチのみ (default None = 全部)
        exclude_pattern: ファイル名 regex に **マッチしたものを除外** (PII 等保護)
                        指定が無くても DEFAULT_EXCLUDE_PATTERN が暗黙適用
        max_files: 最大 N 件まで (default 50)
    """
    import re as _re

    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    state = load_state()
    folder_state = state.setdefault(folder_id, {})
    files_state = folder_state.setdefault("files", {})

    logger.info(f"=== Sync folder: label={label} folder_id={folder_id} ===")
    # ★2026-08-09: ファイル名 filter の **手前** に置く最初の門。
    #   ここで落ちたものは 1 件も列挙しない (= 名前を見る前に丸ごと止める)。
    if is_denied_drive(drive, folder_id):
        logger.warning(f"  取り込み禁止ドライブのため skip: label={label}")
        return {"label": label, "skipped": "denied_drive", "imported": 0, "files": []}
    logger.info(
        f"  filters: recursive={recursive} max_age={max_age_days}d "
        f"pattern={pattern!r} exclude={exclude_pattern!r} max_files={max_files}"
    )
    files = list_folder_files(drive, folder_id, recursive=recursive)
    logger.info(f"  found {len(files)} candidate files")

    # ★フィルタ適用 (新しい順)
    files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)

    if max_age_days is not None:
        from datetime import timedelta as _td
        cutoff = (datetime.now().astimezone() - _td(days=max_age_days)).isoformat()
        files = [f for f in files if f.get("modifiedTime", "") >= cutoff]
        logger.info(f"  after max_age filter: {len(files)} files")

    if pattern:
        rx = _re.compile(pattern, _re.IGNORECASE)
        files = [f for f in files if rx.search(f.get("name", ""))]
        logger.info(f"  after pattern filter: {len(files)} files")

    # ★ exclude pattern (個別指定 + デフォルト保護パターン両方適用)
    # ★2026-07-03 P2a: sync 経路にも override (SALARY_PUBLIC / REGULATION_DOC) を適用。
    #   従来は raw regex のみで、search 経路 (is_confidential_file) にしか override が
    #   効いていなかった。entry 個別の exclude_pattern は folder 運用者の明示意思なので
    #   override 対象外 (= DEFAULT 由来の hit のみ override で救済)。
    rx_def = _re.compile(DEFAULT_EXCLUDE_PATTERN, _re.IGNORECASE)
    rx_entry = _re.compile(exclude_pattern, _re.IGNORECASE) if exclude_pattern else None

    def _blocked(nm: str) -> bool:
        if rx_entry and rx_entry.search(nm):
            return True  # entry 明示 exclude は override 不可
        if not rx_def.search(nm):
            return False
        if _check_salary_public_override(nm):
            return False
        # ★DA-2: 規程 override は entry opt-in (regulations label) 限定 = 他 folder への scope creep 防止
        if allow_regulation_override and _check_regulation_doc_override(nm):
            return False
        return True

    before = len(files)
    _excluded_names = [f.get("name", "") for f in files if _blocked(f.get("name", ""))]
    files = [f for f in files if not _blocked(f.get("name", ""))]
    if _excluded_names:
        # ★DA-2: 何が落ちたか監査可能に (silent 除外を可視化、§1.18)
        logger.warning(
            f"  ★ exclude filter で {len(_excluded_names)} ファイル除外 (PII/機密、override 適用後): "
            + " / ".join(n[:40] for n in _excluded_names[:10])
            + (" …" if len(_excluded_names) > 10 else "")
        )

    if max_files is not None:
        files = files[:max_files]
        logger.info(f"  capped to max_files={max_files}")

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    counters = {"saved": 0, "skip_unchanged": 0, "skip_unsupported": 0, "error": 0}

    for f in files:
        fid = f["id"]
        modified = f.get("modifiedTime", "")
        prev = files_state.get(fid)
        if not force and prev and prev.get("modified") == modified:
            counters["skip_unchanged"] += 1
            continue
        ok, reason = fetch_and_save(drive, f, label, visibility, domain=domain,
                                    deterministic_text=deterministic_text)
        if ok:
            counters["saved"] += 1
            files_state[fid] = {
                "modified": modified,
                "name": f["name"],
                "synced_at": datetime.now().isoformat(timespec="seconds"),
            }
            logger.info(f"  ✓ {reason}")
        else:
            if reason.startswith("skip"):
                counters["skip_unsupported"] += 1
            else:
                counters["error"] += 1
                logger.warning(f"  ✗ {f['name']}: {reason}")

    folder_state["last_sync"] = datetime.now().isoformat(timespec="seconds")
    folder_state["label"] = label
    folder_state["visibility"] = visibility
    save_state(state)

    logger.info(
        f"  done: saved={counters['saved']} unchanged={counters['skip_unchanged']} "
        f"unsupported={counters['skip_unsupported']} error={counters['error']}"
    )
    return counters


# ─── 個別ファイル取り込み (URL or ID で 1 ファイル) ──
def sync_file(file_url_or_id: str, label: str, visibility: str = "public") -> bool:
    """単一ファイルだけ取り込む (海山が「これ重要」と指定したファイル用)"""
    # URL 形式から ID 抽出
    fid = file_url_or_id
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", file_url_or_id)
    if m:
        fid = m.group(1)
    elif "id=" in file_url_or_id:
        m2 = re.search(r"id=([a-zA-Z0-9_-]+)", file_url_or_id)
        if m2:
            fid = m2.group(1)

    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    try:
        f = drive.files().get(
            fileId=fid,
            fields="id,name,mimeType,modifiedTime,webViewLink",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        logger.error(f"file fetch failed: {e}")
        return False

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    ok, reason = fetch_and_save(drive, f, label, visibility)
    if ok:
        # state にも記録
        state = load_state()
        manual_state = state.setdefault("__manual__", {})
        manual_state[fid] = {
            "modified": f.get("modifiedTime", ""),
            "name": f.get("name", ""),
            "label": label,
            "synced_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(state)
        logger.info(f"✓ manual ingest: {reason}")
        return True
    else:
        logger.warning(f"✗ {f.get('name')}: {reason}")
        return False


# ─── 全フォルダ Sync (config 経由) ────────────
def sync_all(force: bool = False) -> None:
    if not SOURCES_FILE.exists():
        raise SystemExit(
            f"設定ファイルが無い: {SOURCES_FILE}\n"
            f"形式: [{{folder_id, label, visibility, recursive, max_age_days, max_files, pattern}}, ...]"
        )
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    for src in sources:
        sync_folder(
            folder_id=src["folder_id"],
            label=src["label"],
            visibility=src.get("visibility", "public"),
            recursive=src.get("recursive", False),
            max_age_days=src.get("max_age_days", 90),
            pattern=src.get("pattern"),
            exclude_pattern=src.get("exclude_pattern"),
            max_files=src.get("max_files", 30),
            force=force,
            domain=src.get("domain", ""),
            # ★2026-07-03 P2a/DA: 規程 entry 限定の opt-in flag 2 種
            allow_regulation_override=src.get("allow_regulation_override", False),
            deterministic_text=src.get("deterministic_text", False),
        )
    # config に "files" (個別ファイル list) があれば取り込み
    files_section = []
    try:
        config_root = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        # 配列じゃなく {folders: [...], files: [...]} 形式もサポート
        if isinstance(config_root, dict):
            files_section = config_root.get("files", [])
    except Exception:
        pass
    for fitem in files_section:
        sync_file(
            file_url_or_id=fitem.get("file_id") or fitem.get("url"),
            label=fitem.get("label", "misc"),
            visibility=fitem.get("visibility", "public"),
        )


# ─── CLI ────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Google Drive sync to Personal Brain")
    ap.add_argument("--discover", help="キーワードで Drive を検索 (folder + file)")
    ap.add_argument("--folder-only", action="store_true", help="--discover 時に folder のみ")
    ap.add_argument("--fulltext", action="store_true",
                    help="★2026-05-26: --discover 時に fullText 検索も含める (= 中身も対象、bot 検索向き)")
    ap.add_argument("--folder", help="取り込むフォルダ ID")
    ap.add_argument("--file", help="取り込む単一ファイル (URL or ID)")
    ap.add_argument("--label", default="misc", help="保存ファイル名 prefix (例: monday-dash)")
    ap.add_argument(
        "--visibility",
        default="public",
        choices=["public", "private"],
        help="clone_visibility デフォルト",
    )
    ap.add_argument("--recursive", action="store_true", default=False, help="サブフォルダも降りる (デフォルト OFF)")
    ap.add_argument("--max-age-days", type=int, default=90, help="直近 N 日以内のファイルのみ (default: 90)")
    ap.add_argument("--pattern", default=None, help="ファイル名 regex マッチのみ (例: 'Monday Dash')")
    ap.add_argument(
        "--exclude-pattern",
        default=None,
        help="ファイル名 regex マッチを除外 (DEFAULT で人事評価/給与/機密系は常に除外)",
    )
    ap.add_argument("--max-files", type=int, default=50, help="1 フォルダあたり最大 N 件 (default: 50)")
    ap.add_argument("--all", action="store_true", help="設定ファイルの全フォルダを sync")
    ap.add_argument("--force", action="store_true", help="modifiedTime 無視で全件取り込み")
    args = ap.parse_args()

    if args.discover:
        mode = "fulltext" if args.fulltext else "name"
        results = discover(
            args.discover,
            mime="folder" if args.folder_only else None,
            mode=mode,
        )
        if not results:
            print("(該当なし)")
            return
        print(f"=== Drive 検索結果: '{args.discover}' (mode={mode}, {len(results)} 件) ===\n")
        for f in results:
            mime_tag = (
                "📁 folder"
                if f["mimeType"] == "application/vnd.google-apps.folder"
                else f["mimeType"].split("/")[-1].split(".")[-1][:12]
            )
            owner = (f.get("owners") or [{}])[0].get("displayName", "?")
            mod = f.get("modifiedTime", "")[:10]
            link = f.get("webViewLink", "")
            print(f"[{mime_tag}] {f['name']}")
            print(f"  id:    {f['id']}")
            print(f"  owner: {owner} / mod: {mod}")
            print(f"  url:   {link}")
            print()
        return

    if args.file:
        sync_file(args.file, label=args.label, visibility=args.visibility)
        return

    if args.folder:
        sync_folder(
            folder_id=args.folder,
            label=args.label,
            visibility=args.visibility,
            recursive=args.recursive,
            max_age_days=args.max_age_days,
            pattern=args.pattern,
            exclude_pattern=args.exclude_pattern,
            max_files=args.max_files,
            force=args.force,
        )
        return

    if args.all:
        sync_all(force=args.force)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
