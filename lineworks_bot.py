"""
LINE Works Bot API クライアント
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

うみやまAI (海山社長 AI 分身) を LINE Works Bot API 経由でデプロイするための
最小限のクライアント。

認証: Service Account (RFC 7523 JWT Bearer Grant)
  - RS256 署名の JWT を作成
  - https://auth.worksmobile.com/oauth2/v2.0/token で access_token 取得
  - access_token を 24h キャッシュ

主要機能:
  - verify_signature(body, header_sig): webhook 受信時の HMAC 検証
  - send_text(user_id, text): 指定ユーザに 1:1 テキスト送信
  - parse_webhook(payload): webhook payload → dict

Env vars (.env):
  LW_CLIENT_ID
  LW_CLIENT_SECRET
  LW_SERVICE_ACCOUNT       (例: xxxxxx.serviceaccount@owndays)
  LW_PRIVATE_KEY_PATH      (RSA 秘密鍵 .pem ファイルパス)
     または LW_PRIVATE_KEY (PEM 文字列を直接)
  LW_BOT_ID                (Bot ID = API endpoint 用、URL の /bots/{LW_BOT_ID}/...)
  LW_BOT_USER_ID           (Bot User ID = mention 判定用、<m userId="..."> の値。
                            ★2026-05-24 Tier 0 追加。bot を group に追加した時の
                            joined event source.userId を観測 or 管理画面で確認して .env に設定。
                            空のままなら plain text "@<name>" のみで mention 検出。)
  LW_BOT_SECRET            (webhook 署名検証用)
  LW_BOT_MENTION_NAMES     (★2026-05-24 Tier 0、plain text mention の name list、
                            カンマ区切り、default: うみやまAI / うみやま / umiyamaAI 等)
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import hmac
import hashlib
import base64
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ─── @mention 検出 (★2026-05-24 Tier 0: LINE WORKS group 対応) ──
# group 内では bot は mention された時だけ反応する (= silent listen default)
#
# # LINE WORKS の mention format (= fact-checker verify 済 2026-05-24)
#
# 送信側 (公式 docs 確定):
#   <m userId="{userId}">表示名</m>     # XML-like inline tag
#   userId="all"                         # 全員 mention (= @All)
#   userId は UUID or email を受付
#
# 受信側 (公式 docs 未明記、対称性から蓋然性高い):
#   content.text に同じ <m userId="..."> tag が inline で来る想定
#   ただし**実機 1 回 payload log で確定すべき** (= 初回 group webhook で
#   _log_raw_payload_once 経由で原文 dump、後続で is_mentioned() を fine-tune)
#
# # 検出方法 (= 2 段階)
#
# 1. <m userId="LW_BOT_USER_ID"> tag (= 確証性高い、ただし LW_BOT_USER_ID env 設定が前提)
# 2. plain text "@<bot_name>" (= fallback、ユーザが mention 機能使わず素打ちした時 +
#    LW_BOT_USER_ID 未設定時の最低限保証)
_DEFAULT_MENTION_NAMES = ["うみやまAI", "うみやま", "umiyamaAI", "umiyama_ai", "umiyama-ai"]


def _get_mention_names() -> list[str]:
    """env override 可能な mention 名リスト取得"""
    raw = os.getenv("LW_BOT_MENTION_NAMES", "")
    if raw.strip():
        return [n.strip() for n in raw.split(",") if n.strip()]
    return _DEFAULT_MENTION_NAMES


def _get_bot_user_id() -> Optional[str]:
    """Bot User ID (= mention 判定用、<m userId="..."> の値). 未設定なら None."""
    v = os.getenv("LW_BOT_USER_ID", "").strip()
    return v or None


# <m userId="..."> tag 検出用 regex (= 公式 mention format)
# - userId は UUID or email、quote は " / ' 両対応 (= robust)
# - 内部 text は任意 (= 表示名)、</m> で閉じる
# - <m id="..."> や <m> bare 形式は LINE WORKS docs に無いので対象外
_M_TAG_REGEX = re.compile(
    r'<m\s+userId\s*=\s*["\']([^"\']+)["\']\s*>.*?</m>',
    re.IGNORECASE | re.DOTALL,
)


def is_mentioned(text: str) -> bool:
    """text 内に bot mention が含まれるか判定 (group silent listen 用).

    True 条件 (= いずれか 1 つ満たせば mention 成立):
      (A) <m userId="LW_BOT_USER_ID">...</m> tag を含む (= 確証 path、env 必須)
      (B) <m userId="all">...</m> (= 全員 mention、bot も含まれる)
      (C) "@<bot_name>" (= plain text mention、env LW_BOT_MENTION_NAMES or default、
           case-insensitive)

    False 条件:
      - mention 無し (= group 内の silent listen 対象)
      - 他 user 向け mention のみ (= <m userId="他人"> )

    Note:
      - LW_BOT_USER_ID 未設定時は (A)(B) は機能せず (C) のみで判定 (= 保守的)
      - (B) "all" は全員宛なので bot も含まれる扱い (= 海山判断で false に変えても可)
      - LINE WORKS 受信側 payload 仕様は未確認、初回 group webhook 後に fine-tune 余地あり
    """
    if not text:
        return False

    # (A) (B) <m userId="..."> tag 検出
    bot_user_id = _get_bot_user_id()
    for m in _M_TAG_REGEX.finditer(text):
        mentioned_user = m.group(1).strip()
        if mentioned_user.lower() == "all":
            return True  # @All は bot も含まれる
        if bot_user_id and mentioned_user.lower() == bot_user_id.lower():
            return True

    # (C) plain text fallback
    names = _get_mention_names()
    for name in names:
        # 日本語含むので \b は不可、name の直後が文字終端 or 非英数 (= 句読点 / 空白 / etc) を許容
        pattern = re.compile(
            re.escape("@" + name) + r"(?![A-Za-z0-9_\-])",
            re.IGNORECASE,
        )
        if pattern.search(text):
            return True

    return False


def strip_mention_tags(text: str) -> str:
    """text から <m userId="..."> tag を除去して plain text に正規化 (= LLM 渡し時用).

    LINE WORKS の mention tag を含んだまま LLM に渡すと、LLM が tag 自体を
    response に echo してしまう risk があるため、bot 内部で処理する前に除去推奨。
    """
    if not text:
        return text
    # tag を内部 text に置換 (= 「@表示名」相当)
    def _replace(m):
        # tag 内側の表示名を取得 (= <m userId="...">表示名</m> の表示名部)
        inner_match = re.match(
            r'<m\s+userId\s*=\s*["\'][^"\']+["\']\s*>(.*?)</m>',
            m.group(0),
            re.IGNORECASE | re.DOTALL,
        )
        if inner_match:
            return f"@{inner_match.group(1)}"
        return m.group(0)
    return _M_TAG_REGEX.sub(_replace, text)


# ─── 設定 ─────────────────────────────────────────
LW_CLIENT_ID = os.getenv("LW_CLIENT_ID", "")
LW_CLIENT_SECRET = os.getenv("LW_CLIENT_SECRET", "")
LW_SERVICE_ACCOUNT = os.getenv("LW_SERVICE_ACCOUNT", "")
LW_PRIVATE_KEY_PATH = os.getenv("LW_PRIVATE_KEY_PATH", "")
LW_PRIVATE_KEY = os.getenv("LW_PRIVATE_KEY", "")
LW_BOT_ID = os.getenv("LW_BOT_ID", "")
LW_BOT_SECRET = os.getenv("LW_BOT_SECRET", "")

TOKEN_URL = "https://auth.worksmobile.com/oauth2/v2.0/token"
API_BASE = "https://www.worksapis.com/v1.0"

# JWT 有効期限 (seconds). LINE Works の仕様では最大 3600 推奨
JWT_LIFETIME = 3600
# access_token のキャッシュ余裕 (expiry より早く再取得)
TOKEN_REFRESH_MARGIN = 60


def _get_private_key() -> str:
    """秘密鍵を文字列として取得 (PATH 指定を優先)"""
    if LW_PRIVATE_KEY_PATH:
        if os.path.exists(LW_PRIVATE_KEY_PATH):
            with open(LW_PRIVATE_KEY_PATH, "r") as f:
                return f.read()
        # ★2026-07-02 監査 P1 (consultant-push-dead): .env の PATH は container 基準 (/app/...) の
        # ため host 実行 (consultant dispatch 等) では不在 → 空鍵で JWT 生成し InvalidKeyError に
        # なっていた。/app/ を repo root (= 本ファイルの dir) に読み替える host-safe fallback。
        # container では第一分岐が常に成立するため挙動不変。
        if LW_PRIVATE_KEY_PATH.startswith("/app/"):
            host_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     LW_PRIVATE_KEY_PATH[len("/app/"):])
            if os.path.exists(host_path):
                with open(host_path, "r") as f:
                    return f.read()
    return LW_PRIVATE_KEY


def is_configured() -> bool:
    """必要な env が揃っているか"""
    return all([
        LW_CLIENT_ID,
        LW_CLIENT_SECRET,
        LW_SERVICE_ACCOUNT,
        _get_private_key(),
        LW_BOT_ID,
    ])


# ─── Webhook 署名検証 ────────────────────────────────
def verify_signature(body: bytes, signature_header: str) -> bool:
    """LINE Works の X-WORKS-Signature を検証

    署名: base64(HMAC-SHA256(body, bot_secret))
    """
    if not LW_BOT_SECRET:
        logger.warning("LW_BOT_SECRET 未設定 - 署名検証をスキップ")
        return False
    if not signature_header:
        return False

    expected = base64.b64encode(
        hmac.new(
            LW_BOT_SECRET.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected, signature_header)


# ─── OAuth2 アクセストークン ────────────────────────
class _TokenCache:
    value: Optional[str] = None
    expires_at: float = 0.0


_cache = _TokenCache()


async def _build_jwt() -> str:
    """RS256 JWT を生成"""
    import jwt  # lazy import
    now = int(time.time())
    payload = {
        "iss": LW_CLIENT_ID,
        "sub": LW_SERVICE_ACCOUNT,
        "iat": now,
        "exp": now + JWT_LIFETIME,
    }
    key = _get_private_key()
    return jwt.encode(payload, key, algorithm="RS256")


async def get_access_token(http: httpx.AsyncClient, force_refresh: bool = False) -> str:
    """access_token を取得 (キャッシュあり)"""
    now = time.time()
    if (
        not force_refresh
        and _cache.value
        and _cache.expires_at - TOKEN_REFRESH_MARGIN > now
    ):
        return _cache.value

    jwt_token = await _build_jwt()
    data = {
        "assertion": jwt_token,
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": LW_CLIENT_ID,
        "client_secret": LW_CLIENT_SECRET,
        "scope": "bot",
    }
    resp = await http.post(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    resp.raise_for_status()
    payload = resp.json()

    _cache.value = payload["access_token"]
    _cache.expires_at = now + int(payload.get("expires_in", 86400))
    logger.info(f"LINE Works access_token 取得 (expires in {payload.get('expires_in')}s)")
    return _cache.value


# ─── メッセージ送信 ────────────────────────────────
async def _post_message(http: httpx.AsyncClient, user_id: str, body: dict, max_retries: int = 1) -> dict:
    """共通 POST 処理 (token refresh リトライ含む)"""
    if not LW_BOT_ID:
        raise RuntimeError("LW_BOT_ID 未設定")
    url = f"{API_BASE}/bots/{LW_BOT_ID}/users/{user_id}/messages"
    last_resp = None
    for attempt in range(max_retries + 1):
        token = await get_access_token(http, force_refresh=(attempt > 0))
        resp = await http.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
        last_resp = resp
        if resp.status_code == 401 and attempt < max_retries:
            logger.warning("LINE Works 401 - token を refresh してリトライ")
            continue
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    # 到達しないはず
    last_resp.raise_for_status()
    return {}


def _truncate_for_lw(text: str, max_chars: int = 1900) -> str:
    """★fix 2026-05-25 MUST-FIX M-8: LW message body size 上限超過で 400 を防ぐ。
    LW API は概ね 2000 字程度が上限 (model 確定値は非公開、安全側で 1900 で運用)。
    超過時は末尾に "(続く...)" を付けて user に「途中で切れた」と明示。

    ★2026-05-26 海山指示: 「時折、うみやまAI の回答が途中で途切れることがある」
    対策。callers の多くは `_split_for_lw` + 連続 send で chunked 配信するように
    切替済 (= 1900 字超でも全文届く)。本関数は API 直叩き / chunked 不可な経路の
    最終 safety net として残置、+ truncate note 込みで 1900 上限を超えないように
    max_chars 値で予算管理する。
    """
    if not text:
        return text
    if len(text) <= max_chars:
        return text
    # truncate note の余白を確保 (= 旧実装は 1900 + note ≒ 1930 で LW 上限 2000 ギリギリ、
    # LW が note 文字の途中で勝手に切る事故もあり得た)
    note = "\n\n…(長いので途中で切れた、続きが必要なら聞いて)"
    keep = max_chars - len(note)
    return text[:keep] + note


def _split_for_lw(text: str, max_chars: int = 1900) -> list[str]:
    """★2026-05-26 海山指示: 「うみやまAI の回答が途中で途切れる」 対策。
    text を max_chars 以内の chunk list に分割し、複数 LW message として連続送信可能化。
    分割の優先順位 (= 自然な文章境界 を優先):
      1. paragraph 境界 (= "\\n\\n")
      2. 行境界 (= "\\n")
      3. 文境界 (= "。", "！", "？", "."等)
      4. 強制 hard cut (= 最終手段)

    各 chunk は max_chars - 20 (= "(N/M)" prefix 余白) 以下に収める。
    """
    if not text:
        return []
    # 余白 20 chars 確保 (= "(99/99) " prefix + 改行 用)
    effective_max = max_chars - 20
    if len(text) <= effective_max:
        return [text]

    chunks: list[str] = []
    remaining = text

    def _find_split_pos(s: str, max_p: int) -> int:
        """s[:max_p] に収まる最大の分割位置を返す。境界優先順位適用。"""
        if len(s) <= max_p:
            return len(s)
        # 1) paragraph 境界 (= "\n\n") を後ろから探す
        for sep in ("\n\n",):
            pos = s.rfind(sep, 0, max_p)
            if pos > 0 and pos > max_p * 0.5:  # 半分以下で割れたら使わない (= 細切れ防止)
                return pos + len(sep)
        # 2) 行境界 (= "\n")
        pos = s.rfind("\n", 0, max_p)
        if pos > 0 and pos > max_p * 0.5:
            return pos + 1
        # 3) 文境界 (= 句点 + 改行 もしくは句点)
        for sep in ("。\n", "。", "！", "？", "!", "?", ". ", "、"):
            pos = s.rfind(sep, 0, max_p)
            if pos > 0 and pos > max_p * 0.5:
                return pos + len(sep)
        # 4) 強制 hard cut
        return max_p

    while remaining:
        if len(remaining) <= effective_max:
            chunks.append(remaining)
            break
        cut = _find_split_pos(remaining, effective_max)
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    return chunks


async def send_text(
    http: httpx.AsyncClient,
    user_id: str,
    text: str,
    max_retries: int = 1,
    quick_reply: Optional[list] = None,  # 互換引数 (無視)
) -> dict:
    """指定 user に 1:1 プレーンテキストメッセージを送信。

    ★2026-05-26 海山指示 「うみやまAI の回答が途中で途切れる」 対策:
    1900 字超は **複数 message に自動分割** して順次送信 (= chunked send)。
    各 chunk に `(N/M)` prefix を付けて user 側で順序確認可能化。
    return value は最後の chunk の send 結果 (= 後方互換)。
    """
    if not text:
        return {}
    chunks = _split_for_lw(text, max_chars=1900)
    if len(chunks) <= 1:
        # 単発送信 (= 旧挙動)、念のため truncate safety net 通す
        return await _post_message(
            http, user_id, {"content": {"type": "text", "text": _truncate_for_lw(text)}},
            max_retries=max_retries,
        )

    # 複数 chunk: prefix + 順次送信 + 遅延
    last_result: dict = {}
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        labeled = f"({i}/{total})\n{chunk}"
        # ★安全側: labeled が万一 1900 超だったら最終 truncate
        labeled = _truncate_for_lw(labeled, max_chars=1900)
        try:
            last_result = await _post_message(
                http, user_id, {"content": {"type": "text", "text": labeled}},
                max_retries=max_retries,
            )
        except Exception as e:
            logger.warning(f"chunked send 失敗 chunk={i}/{total}: {e}")
            # 残り chunk は諦めずに試す (= API 一時的 hiccup で全 chunk 落ちるのを防ぐ)
        if i < total:
            await asyncio.sleep(0.3)  # rate limit + LW 側 order 保持
    return last_result


async def send_image(
    http: httpx.AsyncClient,
    user_id: str,
    original_url: str,
    preview_url: Optional[str] = None,
    max_retries: int = 1,
) -> dict:
    """指定 user に 1:1 画像メッセージを送信 (LINE Works Bot API image content、URL 方式)。

    original/preview とも HTTPS で **LW 側から到達可能な** URL が必要。chart route は token gate のため
    URL に ?token= を含めること。失敗は呼び出し側で握る想定 (= 画像不可なら text の URL を fallback に)。
    ★2026-06-20 海山指示「chart を URL でなく画像で」。
    """
    if not original_url:
        return {}
    body = {"content": {"type": "image",
                        "originalContentUrl": original_url,
                        "previewImageUrl": preview_url or original_url}}
    return await _post_message(http, user_id, body, max_retries=max_retries)


async def send_button_template(
    http: httpx.AsyncClient,
    user_id: str,
    content_text: str,
    buttons: list,
    max_retries: int = 1,
) -> dict:
    """ボタン付きメッセージ (button_template) を送信

    buttons: [{"label": "表示", "data": "postback data 文字列"}, ...]
    contentText は 1000 char 制限がある。長い応答は send_text() を先に送り、
    その後この関数で短い prompt + ボタンを follow-up する運用を想定。

    ★2026-05-26 schema 確定 (= LINE Works 公式 bot-actionobject matrix 準拠 v3):
    button_template の actions では `type:"postback"` は **使えない** (= matrix で
    postback は カルーセル / クイックリプライ / リッチメニュー のみ)。
    button_template には `type:"message"` + `postback` field (nested) を指定。
    tap 時は message event として bot が受信、`content.text` = label,
    `content.postback` = postback 値 が同梱で届く。

    後方互換: `postback` / `data` field 名どちらでも accept。
    label は 20 chars 制約 (= 公式 docs)、絵文字込みで運用。
    """
    content_text = (content_text or "")[:1000]
    body = {
        "content": {
            "type": "button_template",
            "contentText": content_text,
            "actions": [
                {
                    "type": "message",
                    "label": b["label"][:20],  # 20 chars 制約 (= 公式仕様)
                    # `postback` 優先、新 caller 用に `data` も accept (= 後方互換)
                    "postback": b.get("postback") or b.get("data", ""),
                }
                for b in buttons
            ],
        }
    }
    try:
        return await _post_message(http, user_id, body, max_retries=max_retries)
    except httpx.HTTPStatusError as e:
        # button_template 非対応 → 末尾に text でフォールバック
        logger.warning(
            f"button_template 失敗 - text fallback: {e.response.status_code} {e.response.text[:150]}"
        )
        fallback = content_text + "\n\n" + "\n".join(
            f"→ {b['label']} → 「{b.get('postback') or b.get('data', '')}」と返信"
            for b in buttons
        )
        return await _post_message(
            http, user_id, {"content": {"type": "text", "text": fallback}},
            max_retries=max_retries,
        )


async def _send_channel_single(
    http: httpx.AsyncClient,
    channel_id: str,
    text: str,
    max_retries: int = 1,
) -> dict:
    """channel に 1 message だけ送信する low-level helper (= chunked 化前の send_channel_text 本体)."""
    if not LW_BOT_ID:
        raise RuntimeError("LW_BOT_ID 未設定")
    url = f"{API_BASE}/bots/{LW_BOT_ID}/channels/{channel_id}/messages"
    body = {"content": {"type": "text", "text": text}}

    for attempt in range(max_retries + 1):
        token = await get_access_token(http, force_refresh=(attempt > 0))
        resp = await http.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
        if resp.status_code == 401 and attempt < max_retries:
            continue
        resp.raise_for_status()
        return resp.json() if resp.content else {}
    return {}


async def send_channel_text(
    http: httpx.AsyncClient,
    channel_id: str,
    text: str,
    max_retries: int = 1,
) -> dict:
    """ルーム (channel) にテキストメッセージを送信。

    ★2026-05-26 海山指示: 長文応答が途中で途切れる対策、1900 字超は **chunked send**。
    """
    if not text:
        return {}
    chunks = _split_for_lw(text, max_chars=1900)
    if len(chunks) <= 1:
        return await _send_channel_single(
            http, channel_id, _truncate_for_lw(text), max_retries=max_retries
        )
    last_result: dict = {}
    total = len(chunks)
    for i, chunk in enumerate(chunks, 1):
        labeled = _truncate_for_lw(f"({i}/{total})\n{chunk}", max_chars=1900)
        try:
            last_result = await _send_channel_single(
                http, channel_id, labeled, max_retries=max_retries
            )
        except Exception as e:
            logger.warning(f"channel chunked send 失敗 chunk={i}/{total}: {e}")
        if i < total:
            await asyncio.sleep(0.3)
    return last_result


# ─── 添付ファイルダウンロード ───────────────────────
async def download_attachment(
    http: httpx.AsyncClient,
    file_id: str,
    max_retries: int = 1,
    timeout: float = 120.0,
) -> Optional[bytes]:
    """LINE Works の attachment を取得しバイナリで返す

    エンドポイント:
      GET https://www.worksapis.com/v1.0/bots/{botId}/attachments/{fileId}
      → 302 Found で `jp{N}-apis-storage.worksmobile.com` の実体ストレージへ
      → 実体ストレージも Bearer token 必須

    httpx の follow_redirects=True は **cross-host redirect で Authorization を strip する**
    (httpx 0.28+ で確認)。LINE Works の場合は worksapis.com → worksmobile.com で
    cross-host になるため、自動 follow すると storage 側で 401。
    そこで手動で 302 を捕捉し、Location URL に Bearer を再付与して GET する。

    100MB クラスのファイルも扱うため timeout は長め (default 120s)。
    """
    if not LW_BOT_ID:
        raise RuntimeError("LW_BOT_ID 未設定")
    url = f"{API_BASE}/bots/{LW_BOT_ID}/attachments/{file_id}"
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            token = await get_access_token(http, force_refresh=(attempt > 0))
            auth_headers = {"Authorization": f"Bearer {token}"}

            # Step 1: redirect を取得 (follow_redirects=False)
            resp = await http.get(
                url,
                headers=auth_headers,
                follow_redirects=False,
                timeout=timeout,
            )

            # 401 → token refresh してリトライ
            if resp.status_code == 401 and attempt < max_retries:
                logger.warning("LINE Works download 401 (initial) - token refresh してリトライ")
                continue

            # Step 2: 302 ならストレージ URL に手動で Bearer 付け直して GET
            if resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = resp.headers.get("Location") or resp.headers.get("location")
                if not redirect_url:
                    raise RuntimeError(f"redirect status {resp.status_code} だが Location header 無し")
                logger.debug(
                    f"LINE Works attachment redirect: {resp.status_code} → "
                    f"{redirect_url.split('?')[0][:120]}..."
                )
                # 手動 follow: storage CDN は最大 5 hop まで
                for hop in range(5):
                    resp = await http.get(
                        redirect_url,
                        headers=auth_headers,
                        follow_redirects=False,
                        timeout=timeout,
                    )
                    if resp.status_code in (301, 302, 303, 307, 308):
                        next_url = resp.headers.get("Location") or resp.headers.get("location")
                        if not next_url:
                            break
                        redirect_url = next_url
                        continue
                    break
                else:
                    raise RuntimeError("redirect hop limit exceeded (>5)")

            if resp.status_code == 401 and attempt < max_retries:
                logger.warning("LINE Works download 401 (storage) - token refresh してリトライ")
                continue

            resp.raise_for_status()
            data = resp.content
            logger.info(
                f"LINE Works attachment 取得: {len(data)} bytes "
                f"(file_id={file_id[:30]}...)"
            )
            return data
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                logger.warning(f"LINE Works download 失敗 (attempt {attempt+1}): {e}")
                continue
            break
    logger.warning(f"LINE Works attachment ダウンロード最終失敗: {last_err}")
    return None


# ─── Webhook Payload Parser ────────────────────────
def parse_webhook(payload: dict) -> Optional[dict]:
    """LINE Works webhook payload を内部用 dict に正規化

    Returns None if not a supported event type.

    期待する payload (text):
      {
        "type": "message",
        "source": {"userId": "...", "channelId": "...", ...},
        "createdTime": ...,
        "content": {"type": "text", "text": "..."}
      }

    file / image:
      "content": {
        "type": "file" | "image",
        "fileId": "...",
        "fileName": "...",   # file のみ確実、image はないこともある
        "fileSize": 12345,
      }

    postback (★2026-05-26 海山指示 Drive 検索 button 用に追加):
      {
        "type": "postback",
        "source": {"userId": "...", ...},
        "createdTime": ...,
        "data": "DRIVE_SEARCH:<query>"  ← or "postback": {"data": "..."}
      }
    """
    ev_type = payload.get("type")
    source = payload.get("source", {}) or {}

    # ★ postback event (= button tap で送信される独立 channel)
    if ev_type == "postback":
        # LINE Works payload: 仕様上 data が top-level または postback.data に来る
        # 両方 accept で robust に
        data = (
            payload.get("data")
            or (payload.get("postback") or {}).get("data")
            or ""
        )
        return {
            "user_id": source.get("userId", ""),
            "channel_id": source.get("channelId"),
            "created_time": payload.get("createdTime"),
            "type": "postback",
            "data": data,
        }

    if ev_type != "message":
        return None

    content = payload.get("content", {}) or {}
    ctype = content.get("type", "")

    base = {
        "user_id": source.get("userId", ""),
        "channel_id": source.get("channelId"),
        "created_time": payload.get("createdTime"),
    }

    if ctype == "text":
        # ★2026-05-26 v4: button_template (type:message) tap 時、message event の
        # content.postback に postback 値が同梱される (= 公式 bot-callback-message)。
        # 既存の text path を保ちつつ、postback field を parsed に渡す (= main.py 側
        # でいち早く postback prefix を判定して route 切替できるように).
        return {
            **base,
            "type": "text",
            "text": content.get("text", ""),
            "postback": content.get("postback", ""),
        }

    if ctype in ("file", "image"):
        # file_id は実装によって fileId / contentId のどちらか
        file_id = (
            content.get("fileId")
            or content.get("contentId")
            or content.get("attachmentId")
            or ""
        )
        if not file_id:
            logger.warning(f"LINE Works {ctype} event に fileId 無し: {content}")
            return None
        return {
            **base,
            "type": ctype,
            "file_id": file_id,
            "file_name": content.get("fileName", ""),
            "file_size": int(content.get("fileSize", 0) or 0),
        }

    # ★2026-05-27 海山指示: video / audio を 受信認識 (= 内容処理は未対応だが drop しない、
    # bot から「現状未対応、画像なら可能」 と返信できるようにする).
    if ctype in ("video", "audio"):
        return {
            **base,
            "type": ctype,
            "file_id": (
                content.get("fileId")
                or content.get("contentId")
                or content.get("attachmentId")
                or ""
            ),
            "file_name": content.get("fileName", ""),
            "file_size": int(content.get("fileSize", 0) or 0),
        }

    # sticker / location 等は依然 未対応
    return None
