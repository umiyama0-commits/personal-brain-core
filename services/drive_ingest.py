"""
services/drive_ingest.py — うみやまAI が共有された Drive URL をその場で取得して応答に活用
                          (★2026-05-23 海山指示)

設計:
  社員が LINE Works で Google Slides / Docs / Sheets / Drive の URL を投げる
  → うみやまAI は今まで「401 で開けない」と返してた
  → 本 helper で URL 検出 + on-demand fetch + text 化 → system prompt に prepend
  → bot が中身を踏まえて応答できる

なぜ on-demand か (= 事前 sync 拡大ではなく):
- 即時性: 共有された瞬間に取れる、24h cron 待ちにならない
- PII リスク回避: 明示的に共有された資料のみ取り込む、全社 Drive を漁らない
- Drive 容量問題回避: wiki 永続化しない (= churn ゼロ)

失敗時の挙動:
- 401 / 404 / permission: silent skip → bot は「開けない」と正直に返す (= 現挙動維持)
- credentials 未設定: silent skip
- これにより「資料の中身を返せない場合」の応答は今と変わらない

Drive ファイル種別:
- Google Docs / Slides / Sheets: drive.files().export() で text 化
- PDF / DOCX / XLSX / PPTX: drive.files().get_media() で binary 取得 → content_extractor.extract で text 化
"""
from __future__ import annotations

import io
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Drive URL pattern (= https://docs.google.com/(presentation|document|spreadsheets)/d/<ID>/...
#                  or https://drive.google.com/file/d/<ID>/...)
_DRIVE_URL_RE = re.compile(
    r"https?://(?:docs|drive)\.google\.com/"
    r"(?:presentation|document|spreadsheets|file)/d/"
    r"([a-zA-Z0-9_-]{20,})"
    r"(?:/[^\s]*)?",
    re.IGNORECASE,
)

# 1 回 fetch あたりの本文上限 (= system prompt に prepend するため、5KB 目安)
MAX_TEXT_CHARS = int(os.getenv("DRIVE_INGEST_MAX_CHARS", "5000"))

# うみやまAI が見られない資料を社員から共有してもらう時の宛先 (= 海山が手動で設定する env)
# 未設定なら「管理者に確認を」と曖昧に返す。
# 想定: bot service account を作るか、海山の OWNDAYS Drive アドレスを設定する。
BOT_SHARE_ADDRESS = os.getenv("BOT_GDRIVE_SHARE_ADDRESS", "")

# Office 系 binary を text 化する extractor (lazy import で重い依存を回避)
_extractor = None


def _log_drive_failure(error_class: str, error_msg: str) -> None:
    """★2026-05-24 Tier 1: bot_uptime_monitor が component_streak で拾えるよう
    drive_ingest の bot 失敗 (= credentials / 5xx / unknown) を turn_failed event 化.

    permission_denied / not_found は user 起因なので log しない (= 誤検知防止)."""
    try:
        from scripts.bot_events import log_bot_event  # type: ignore
        log_bot_event(
            "drive_ingest", "turn_failed",
            error_class=error_class,
            error_msg=error_msg,
        )
    except Exception:
        pass


def extract_drive_urls(text: str) -> list[str]:
    """テキスト中の Google Drive URL を抽出。重複除去 + 順序維持。"""
    if not text:
        return []
    found = []
    seen = set()
    for m in _DRIVE_URL_RE.finditer(text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            found.append(url)
    return found


def _extract_id(url: str) -> str:
    """URL から file ID を抽出。"""
    m = _DRIVE_URL_RE.search(url)
    if m:
        return m.group(1)
    # fallback: id= query param
    m2 = re.search(r"id=([a-zA-Z0-9_-]+)", url)
    return m2.group(1) if m2 else ""


def _truncate(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... (truncated, 元 {len(text)} 字)"


def fetch_text(url: str, max_chars: int = MAX_TEXT_CHARS) -> dict:
    """Drive URL から text を取得。

    Returns:
        {
          "ok": bool,
          "title": str,
          "mime": str,
          "text": str,             # 取得成功時、抜粋 (max_chars に truncate 済)
          "error": str,            # 失敗時の理由 (人間向け文言)
          "error_code": str,       # "permission_denied" | "not_found" | "credentials_missing"
                                   # | "invalid_url" | "export_failed" | "download_failed"
                                   # | "extract_failed" | "unsupported_mime" | "other"
          "url": str,              # 元 URL
        }
    """
    result = {
        "ok": False, "url": url, "title": "", "mime": "", "text": "",
        "error": "", "error_code": "other",
    }
    fid = _extract_id(url)
    if not fid:
        result["error"] = "invalid url (no file id)"
        result["error_code"] = "invalid_url"
        return result

    # gdrive_sync の関数を再利用 (= 認証 + drive client 構築)
    try:
        import gdrive_sync  # type: ignore
        creds = gdrive_sync.get_credentials()
    except Exception as e:
        result["error"] = f"credentials unavailable: {e}"
        result["error_code"] = "credentials_missing"
        # ★Tier 1: bot_uptime_monitor で credential 障害を component_streak 検知可能化
        _log_drive_failure("CredentialsError", f"credentials unavailable: {str(e)[:120]}")
        return result

    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
    except Exception as e:
        result["error"] = f"google-api-client not installed: {e}"
        result["error_code"] = "credentials_missing"
        return result

    try:
        drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        f = drive.files().get(
            fileId=fid,
            fields="id,name,mimeType,modifiedTime",
            supportsAllDrives=True,
        ).execute()
    except HttpError as e:
        # 401/403 = permission denied / 404 = not found を切り分け
        # ★2026-05-25 fix: OAuth context での 404 は実質「対象 account に共有されてない」
        # = permission_denied と同じ user-facing message を出すべき。
        # (Drive API は 「存在を漏らさない」設計で、未共有 file も 404 返す)
        status = getattr(getattr(e, "resp", None), "status", 0) or 0
        if status in (401, 403, 404):
            result["error_code"] = "permission_denied"
            if status == 404:
                result["error"] = "file not found or not shared (= 対象 account 未共有 / URL 失効)"
            else:
                result["error"] = "permission denied (viewer 権限が無い)"
            # user 起因 (= 共有設定の問題)、bot 失敗じゃない → log_bot_event しない
        else:
            result["error_code"] = "other"
            result["error"] = f"HTTP {status}: {type(e).__name__}"
            # 5xx 等の API 障害は bot_uptime_monitor 検知対象
            _log_drive_failure("HttpError", f"HTTP {status}: {type(e).__name__}")
        logger.info(f"drive fetch failed: {result['error_code']} status={status}")
        return result
    except Exception as e:
        logger.info(f"drive fetch failed (unknown): {e}")
        result["error"] = f"fetch failed: {type(e).__name__}"
        result["error_code"] = "other"
        # network / auth refresh / unknown は bot 失敗扱い
        _log_drive_failure(type(e).__name__, str(e)[:200])
        return result

    result["title"] = f.get("name", "")
    result["mime"] = f.get("mimeType", "")
    mime = result["mime"]

    # Google ネイティブ → export で text 化
    GDOC_EXPORT = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    if mime in GDOC_EXPORT:
        try:
            data = drive.files().export(fileId=fid, mimeType=GDOC_EXPORT[mime]).execute()
            text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
            result["text"] = _truncate(text, max_chars)
            result["ok"] = True
            return result
        except Exception as e:
            result["error"] = f"export failed: {type(e).__name__}"
            return result

    # PDF / Office 系 → binary download + content_extractor で text 化
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
    if mime in BINARY_DOWNLOAD:
        try:
            request = drive.files().get_media(fileId=fid)
            buf = io.BytesIO()
            downloader = MediaIoBaseDownload(buf, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            buf.seek(0)
            data = buf.read()
        except Exception as e:
            result["error"] = f"download failed: {type(e).__name__}"
            return result

        ext = BINARY_DOWNLOAD[mime]
        # plain text 系はそのまま decode
        if ext in (".txt", ".csv", ".md"):
            try:
                result["text"] = _truncate(data.decode("utf-8"), max_chars)
                result["ok"] = True
                return result
            except Exception as e:
                result["error"] = f"decode failed: {e}"
                return result

        # PDF / Office → content_extractor 経由
        try:
            global _extractor
            if _extractor is None:
                import content_extractor  # lazy import (= pypdf / openpyxl 等の起動コスト回避)
                _extractor = content_extractor
            text = _extractor.extract(data, ext.lstrip("."))
            if text:
                result["text"] = _truncate(text, max_chars)
                result["ok"] = True
                return result
            result["error"] = "extract returned empty"
            return result
        except Exception as e:
            logger.warning(f"content_extractor failed for {ext}: {e}")
            result["error"] = f"extract failed: {type(e).__name__}"
            return result

    # その他 (画像 / 動画 / unknown) → skip
    result["error"] = f"unsupported mime: {mime}"
    return result


def _share_request_block(url: str) -> str:
    """権限不足時の共有依頼ブロックを生成 (= LLM が user に伝える材料).

    ★2026-05-24 海山指示: 「閲覧権限がなく見れない場合は
    『bot-account@example.co.jp に閲覧権限を付与して』と返答する」 に文言厳密合わせ。
    BOT_GDRIVE_SHARE_ADDRESS env で配信先メールアドレス指定。
    """
    if BOT_SHARE_ADDRESS:
        return (
            f"【閲覧権限が無い資料】{url}\n"
            f"うみやまAI には viewer 権限が無いため中身を見られない。\n"
            f"→ user に **「{BOT_SHARE_ADDRESS} に閲覧権限を付与して」** と短く依頼すること。\n"
            f"   (= Google Drive / Docs / Sheets / Slides の「共有」から {BOT_SHARE_ADDRESS} を\n"
            f"    『閲覧者』として追加してもらえば見れるようになる)\n"
            f"---"
        )
    return (
        f"【閲覧権限が無い資料】{url}\n"
        f"うみやまAI には viewer 権限が無いため中身を見られない。\n"
        f"→ user に「公開範囲を見直してもらうか、PDF で投げて」と短く依頼すること。\n"
        f"(★ env BOT_GDRIVE_SHARE_ADDRESS が未設定。海山が設定すれば bot 用共有アドレスを案内できる)\n"
        f"---"
    )


def build_context_block(user_text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """user_text 中の Drive URL を解決して system prompt 用 context block を返す。

    Returns:
        空文字 (URL 無しの場合) または
        「【共有された資料】<title>\n<本文抜粋>\n---」型の文字列
        権限不足時は「【閲覧権限が無い資料】<url>\n共有依頼テンプレ\n---」を返す
        (= bot が「ここに viewer 追加して」と user に返せるよう情報を渡す)
    """
    urls = extract_drive_urls(user_text)
    if not urls:
        return ""

    blocks = []
    for url in urls[:3]:  # 1 ターンで最大 3 件まで (= cost / latency 抑制)
        r = fetch_text(url, max_chars=max_chars)
        if r["ok"]:
            mime_short = (r["mime"] or "").split(".")[-1].split("-")[-1] or "?"
            blocks.append(
                f"【共有された資料】{r['title']} ({mime_short})\n"
                f"{r['text']}\n"
                f"---"
            )
        elif r.get("error_code") == "permission_denied":
            # 権限不足 → bot に共有依頼テンプレを渡す (★2026-05-23 海山指示)
            blocks.append(_share_request_block(url))
        else:
            # それ以外の失敗 (= credentials 不足 / not_found / 一時障害) は silent skip
            logger.info(f"drive_ingest skip {url}: {r.get('error_code')}/{r['error']}")
            continue

    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n\n"
