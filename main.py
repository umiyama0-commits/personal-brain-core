"""
LINE AI Agent — FastAPI Webhook Server
LINEからのメッセージを受信し、LLMエージェントで処理して返信する
"""

import os
import re
import json
import hashlib
import hmac
import base64
import logging
import asyncio
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Depends, BackgroundTasks, Query, Header
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, FileResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import httpx
import redis.asyncio as redis

from brain_wiki import BrainWiki
from brain_index import BrainIndex
from privacy_gate import PrivacyGate, gate3_scrub_pii
from brain_commands import handle_brain_commands, handle_alignment_answer
import lineworks_bot
import clone_history
import clone_feedback
import clone_learning
from chat_import import process_chat_export
from self_improve import run_self_improve, load_system_prompt_patches
from improvement_trigger import detect_and_improve
from notebooklm_extractor import find_notebooklm_urls, extract_notebooklm, get_notebook_id

# ─── 設定 ───
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
BRAIN_EXTENSION_KEY = os.getenv("BRAIN_EXTENSION_KEY", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import time as _time_mod
_start_time = _time_mod.time()

# ─── アプリ初期化 ───
IMPORT_DIR = Path(os.getenv("BRAIN_IMPORT_DIR", "/app/data/brain/import"))

# うみやまAI (LINE Works bot) で受信した添付ファイルの監査用保存先
CLONE_ATTACHMENTS_DIR = Path(
    os.getenv("CLONE_ATTACHMENTS_DIR", "/app/data/brain/clone_attachments")
)
# うみやまAI 添付の最大サイズ (env で上書き可、default 100MB)
CLONE_ATTACHMENT_MAX_BYTES = int(
    os.getenv("CLONE_ATTACHMENT_MAX_BYTES", str(100 * 1024 * 1024))
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ★2026-05-21: OpenTelemetry tracing 初期化 (opentelemetry 未インストールなら no-op)
    try:
        from scripts.tracing import init_tracing
        init_tracing("personal-brain")
    except Exception as e:
        logger.warning(f"tracing init failed (continuing without): {e}")

    # ★2026-05-24 deploy 信頼性監視 (海山指示): app 起動時刻を記録、container uptime 算出用。
    # /api/admin/deploy-status で「container 古い = auto_deploy stale 疑い」判定に使う。
    app.state.startup_at = datetime.now(JST).isoformat()

    app.state.http = httpx.AsyncClient(timeout=60.0)
    app.state.redis = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_timeout=3,
        socket_connect_timeout=3,
        health_check_interval=30,
    )
    app.state.brain = BrainWiki(app.state.http, LITELLM_URL, LITELLM_KEY)
    app.state.privacy = PrivacyGate(app.state.http, LITELLM_URL, LITELLM_KEY)

    # ベクトル索引の初期化 & BrainWikiに注入
    app.state.brain_index = BrainIndex(app.state.http, LITELLM_URL, LITELLM_KEY)
    app.state.brain.set_index(app.state.brain_index)

    asyncio.create_task(_initial_reindex(app))
    asyncio.create_task(_watch_wiki_changes(app))
    asyncio.create_task(_watch_import_dir(app))
    asyncio.create_task(_daily_alignment(app))
    asyncio.create_task(_weekly_line_import_reminder(app))
    asyncio.create_task(_daily_clone_feedback_digest(app))
    asyncio.create_task(_nightly_clone_learning_scan(app))
    asyncio.create_task(_daily_align_voice_digest(app))
    asyncio.create_task(_self_improve_loop(app))
    logger.info("LINE AI Agent + Brain Wiki + Vector Index + 自己改善ループ 起動完了")
    yield
    await app.state.http.aclose()
    await app.state.redis.aclose()

app = FastAPI(title="LINE AI Agent", lifespan=lifespan)
# ★2026-07-10 (世界基準評価 #3): directory を container 固定 "/app/static" にすると、CI runner
#   (checkout 先が異なる) で import main が RuntimeError('Directory does not exist') → fastapi 系
#   test が全滅していた。repo 相対に解決 + check_dir=False で import を壊さない (本番は /app/static)。
_STATIC_DIR = "/app/static" if os.path.isdir("/app/static") \
    else str(Path(__file__).resolve().parent / "static")
app.mount("/static", StaticFiles(directory=_STATIC_DIR, check_dir=False), name="static")

# ★2026-07-21 海山: whitespace ダッシュボードは SSO (owndays-platform.com) へ移設。旧 brain.example.com の
# /whitespace* ルートは SSO へ 302 redirect (下記) = 旧ブックマーク救済 + token 口の閉鎖。env で差し替え可。
# (store-survey は AM が入力中の実運用フォームのため retire せず据え置き — 別途 token gate 維持。)
WHITESPACE_SSO_URL = os.getenv("WHITESPACE_SSO_URL", "https://whitespace.owndays-platform.com")


def _whitespace_guard(token: str, path_hint: str) -> "HTMLResponse | None":
    """whitespace ダッシュボードの token gate。認可 NG なら 401 HTMLResponse、OK なら None。

    ★2026-07-02 監査 P1g: 旧実装は `if expected and token != expected` で、WHITESPACE_TOKEN 未設定時に
    **全公開 (fail-open)** だった (競合の展店空白分析が誰でも閲覧可)。fail-closed に反転:
    token 未設定 = ロック (401)。運用者が .env に WHITESPACE_TOKEN を設定して初めて開く。
    """
    expected = os.getenv("WHITESPACE_TOKEN", "")
    if not expected:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>503 not configured</h2><p>この dashboard は WHITESPACE_TOKEN 未設定のため停止中 "
            "(fail-closed)。運用者が .env に WHITESPACE_TOKEN を設定してください。</p>"
            "</body></html>", status_code=503)
    if token != expected:
        # ★2026-07-09 海山「過去に作ったURLを開いたら最新版のURLにリダイレクト」: 引退トークン
        # (WHITESPACE_TOKEN_OLD、カンマ区切り) の旧ブックマークは現行 URL へ 302。未知/無トークンは従来どおり
        # 401 (fail-closed 維持=redirect の Location に現行トークンが載るのは旧トークン保持者のみ)。
        retired = [t.strip() for t in os.getenv("WHITESPACE_TOKEN_OLD", "").split(",") if t.strip()]
        if token and token in retired:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(f"{path_hint}?token={expected}", status_code=302)
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            f"<h2>401 unauthorized</h2><p>access token 必要: <code>{path_hint}?token=...</code></p>"
            "</body></html>", status_code=401)
    return None


def _whitespace_retired_redirect():
    """★2026-07-21 海山: whitespace は SSO (owndays-platform.com) へ移設。旧 brain.example.com の
    ダッシュボード配信 (token 口) は retire、SSO ベース URL へ 302 (旧ブックマーク救済 + 旧口の閉鎖)。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(WHITESPACE_SSO_URL, status_code=302)


@app.get("/whitespace")
async def whitespace_dashboard(token: str = ""):
    """retired → SSO (owndays-platform.com)。旧 brain.example.com/whitespace は 302 redirect。"""
    return _whitespace_retired_redirect()


@app.get("/whitespace-tw")
async def whitespace_dashboard_tw(token: str = ""):
    """retired → SSO。旧 /whitespace-tw は 302 redirect。"""
    return _whitespace_retired_redirect()


@app.get("/whitespace-sg")
async def whitespace_dashboard_sg(token: str = ""):
    """retired → SSO。旧 /whitespace-sg は 302 redirect。"""
    return _whitespace_retired_redirect()


@app.get("/whitespace-th")
async def whitespace_dashboard_th(token: str = ""):
    """retired → SSO。旧 /whitespace-th は 302 redirect。"""
    return _whitespace_retired_redirect()


@app.get("/store-survey", response_class=HTMLResponse)
async def store_survey_form(token: str = ""):
    """既存店の通行量・立地タグ入力フォーム(★2026-07-14 一度破棄→海山指示で復活 = AM がこの URL で入力中)。
    whitespace と同じ token gate。"""
    denied = _whitespace_guard(token, "/store-survey")
    if denied is not None:
        return denied
    try:
        body = open("/app/data/brain/web/store_survey.html", encoding="utf-8").read()
        return HTMLResponse(body, headers={"Cache-Control": "no-cache, must-revalidate"})
    except FileNotFoundError:
        return HTMLResponse("<h2>入力フォーム未配置</h2>", status_code=404)


@app.get("/api/store-survey")
async def store_survey_load(token: str = ""):
    """統合済みの入力状況を返す(★2026-07-16 AM報告「他の人の完了が見えない」対応)。
    フォームが起動時に読み、全員分の完了表示に使う。"""
    denied = _whitespace_guard(token, "/api/store-survey")
    if denied is not None:
        return denied
    p = "/app/data/brain/import/store_survey/latest.json"
    try:
        return JSONResponse(json.load(open(p, encoding="utf-8")))
    except Exception:
        return JSONResponse({"stores": {}})


@app.post("/api/store-survey")
async def store_survey_save(request: Request, token: str = ""):
    """入力フォームの回答保存。追記 JSONL(履歴・完全保全) + latest.json は**店番ごとのマージ**。
    ★2026-07-16 AM報告「最後に入れた人が上書き?」→ 旧実装は全量置換で他 AM の分が latest から消えていた
    (履歴には全送信が残存=データ喪失なし)。マージ化: 送信に入っている店だけ更新、空レコードは無視。"""
    denied = _whitespace_guard(token, "/api/store-survey")
    if denied is not None:
        return denied
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    import datetime as _dt
    d = "/app/data/brain/import/store_survey"
    os.makedirs(d, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with open(f"{d}/submissions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": ts, "payload": payload}, ensure_ascii=False) + "\n")
    try:
        merged = json.load(open(f"{d}/latest.json", encoding="utf-8")).get("stores") or {}
    except Exception:
        merged = {}
    n_new = 0
    for code, rec in (payload.get("stores") or {}).items():
        if isinstance(rec, dict) and (rec.get("traffic") or rec.get("tags") or rec.get("memo")):
            merged[str(code)] = rec   # その店を実際に入力した内容のみ per-store 上書き
            n_new += 1
    with open(f"{d}/latest.json", "w", encoding="utf-8") as f:
        json.dump({"v": 2, "kind": "traffic_tags", "updated": ts, "stores": merged}, f, ensure_ascii=False, indent=1)
    return JSONResponse({"ok": True, "sent": n_new, "total": len(merged), "ts": ts})


@app.get("/eval-form/{month}", response_class=HTMLResponse)
async def eval_form(month: str, token: str = ""):
    """外部盲検評価 form の URL 配信 (★2026-07-03 ④ judge 人間校正の回収)。

    背景: form.html が Mac Studio 内の file で、評価者5名への配布摩擦が高く
    2ヶ月間回収ゼロ (= LLM judge が一度も人間と校正されていない) の主因だった。
    これで海山は URL 1 本を5名に送るだけになる。
    fail-closed: EVAL_FORM_TOKEN 未設定=503 / token 不一致=401。
    month は YYYY-MM 形式のみ受理 (path traversal 防止)。form は client-side 採点 →
    JSON export 設計のため配信は read-only (回答の POST 受け口は無い、回収は従来どおり
    clone_external_eval.py --import-file)。
    """
    import re as _re
    expected = os.getenv("EVAL_FORM_TOKEN", "")
    if not expected:
        return HTMLResponse("<h2>503 not configured</h2><p>EVAL_FORM_TOKEN 未設定 (fail-closed)</p>",
                            status_code=503)
    if token != expected:
        return HTMLResponse("<h2>401 unauthorized</h2>", status_code=401)
    if not _re.fullmatch(r"\d{4}-\d{2}", month):
        return HTMLResponse("<h2>400 bad month (YYYY-MM)</h2>", status_code=400)
    try:
        body = open(f"/app/data/brain/eval/external/{month}/form.html", encoding="utf-8").read()
        # 社員 query を含むため cache/index/referrer に残さない (reviewer E-1 + DA-5)
        return HTMLResponse(body, headers={
            "Cache-Control": "no-store, private",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        })
    except (FileNotFoundError, OSError):
        return HTMLResponse(f"<h2>404 {month} の form は未生成</h2>", status_code=404)


@app.get("/api/analyst/chart/{rid}/{name}")
async def analyst_chart(rid: str, name: str, token: str = ""):
    """Analyst Agent が生成した chart PNG を配信 (token gate・read-only、ADR §11 Phase 2)。"""
    expected = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected or token != expected:   # ★fail-closed: token 未設定でも開放しない (codex HIGH [1] 2026-06-29)
        return Response("unauthorized", status_code=401)
    if ".." in rid or ".." in name or "/" in rid or "/" in name or not name.endswith(".png"):
        return Response("bad request", status_code=400)
    p = f"/app/data/brain/analyst_output/{rid}/{name}"
    if not os.path.exists(p):
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="image/png")


@app.get("/api/consultant/chart/{rid}/{name}")
async def consultant_chart(rid: str, name: str, token: str = ""):
    """戦略アナリストが ask_analyst 経由で得た chart PNG を配信 (token gate・read-only)。"""
    expected = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected or token != expected:   # ★fail-closed: token 未設定でも開放しない (codex HIGH [1] 2026-06-29)
        return Response("unauthorized", status_code=401)
    if ".." in rid or ".." in name or "/" in rid or "/" in name or not name.endswith(".png"):
        return Response("bad request", status_code=400)
    p = f"/app/data/brain/consultant_output/{rid}/{name}"
    if not os.path.exists(p):
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="image/png")


@app.get("/api/consultant/deck/{rid}")
async def consultant_deck_html(rid: str, token: str = ""):
    """戦略アナリストが生成したスライド(自己完結 HTML、チャート埋込)を配信 (token gate)。"""
    expected = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected or token != expected:   # ★fail-closed: token 未設定でも開放しない (codex HIGH [1] 2026-06-29)
        return Response("unauthorized", status_code=401)
    if ".." in rid or "/" in rid:
        return Response("bad request", status_code=400)
    p = f"/app/data/brain/consultant_output/{rid}/deck.html"
    if not os.path.exists(p):
        return Response("not found", status_code=404)
    return FileResponse(p, media_type="text/html")


@app.get("/api/consultant/deck/{rid}/pptx")
async def consultant_deck_pptx(rid: str, token: str = ""):
    """戦略メモの編集可能な PPTX を配信 (token gate, download)。"""
    expected = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected or token != expected:   # ★fail-closed: token 未設定でも開放しない (codex HIGH [1] 2026-06-29)
        return Response("unauthorized", status_code=401)
    if ".." in rid or "/" in rid:
        return Response("bad request", status_code=400)
    p = f"/app/data/brain/consultant_output/{rid}/deck.pptx"
    if not os.path.exists(p):
        return Response("not found", status_code=404)
    return FileResponse(
        p, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"strategy_{rid}.pptx")


# ─── routes/ APIRouter 登録 (★2026-05-22 Phase 2 切り出し) ───
from routes.alignment_trial import router as _alignment_trial_router  # noqa: E402
from routes.brain_api import router as _brain_api_router  # noqa: E402

app.include_router(_alignment_trial_router)
app.include_router(_brain_api_router)

# ─── 管理コマンド admin gate (★2026-05-23 LEE §3.2) ───
from services.auth import is_admin, is_lw_admin, reject_message  # noqa: E402


# ─── 認証 ───
def require_api_key(request: Request) -> str:
    """
    API Key認証依存関数。
    - BRAIN_EXTENSION_KEY未設定なら全エンドポイント拒否（fail-closed）
    - query param "key" または X-Brain-Key / Authorization: Bearer ヘッダを受理
    - timing attack防止のためhmac.compare_digest使用

    ★2026-05-26 海山指示: dashboard と token 統一のため、VOICE_ALIGN_TOKEN
    (= ?token=... query param) も accept する fallback を追加。
    これで「brain map で `?key=` 別、dashboard で `?token=` 別」 の使い分け不要に。
    """
    if not BRAIN_EXTENSION_KEY:
        raise HTTPException(
            status_code=503,
            detail="BRAIN_EXTENSION_KEY is not configured on server",
        )
    # Priority: Authorization header > X-Brain-Key > query param "key"
    key = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:]
    if not key:
        key = request.headers.get("X-Brain-Key", "")
    if not key:
        key = request.query_params.get("key", "")
    if key and hmac.compare_digest(key, BRAIN_EXTENSION_KEY):
        return key

    # ★fallback: dashboard token (= VOICE_ALIGN_TOKEN) も accept
    voice_token = os.getenv("VOICE_ALIGN_TOKEN", "") or os.getenv("ALIGNMENT_TRIAL_TOKEN", "")
    if voice_token:
        supplied_token = request.query_params.get("token", "")
        if supplied_token and hmac.compare_digest(supplied_token, voice_token):
            return supplied_token

    raise HTTPException(status_code=401, detail="Invalid API key")


def brain_auth_tier(request: Request) -> str:
    """require_api_key を通過済みの request が、どの credential で認証したかを返す。

    'admin' = BRAIN_EXTENSION_KEY (Authorization: Bearer / X-Brain-Key / ?key=)。
    それ以外 (= VOICE_ALIGN_TOKEN 等の弱いダッシュボード token fallback) は 'token'。

    ★2026-07-11 海山指示「Brain Map は個人利用だから全部見れる様に」: Brain Map (グラフ +
    詳細ペイン) だけは admin tier で deep-private (personal/ + interview/) と
    clone_visibility: private を全開にする。判定はここ 1 点に集約 (require_api_key と同じ優先順位)。
    token tier・他の operator endpoint (wiki_page/knowledge/dashboard/search)・MCP・社員クローン・
    公開 LINE は #2 のハードニングを据え置き (= 深層/private 遮断のまま)。
    """
    key = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        key = auth[7:]
    if not key:
        key = request.headers.get("X-Brain-Key", "")
    if not key:
        key = request.query_params.get("key", "")
    if key and BRAIN_EXTENSION_KEY and hmac.compare_digest(key, BRAIN_EXTENSION_KEY):
        return "admin"
    return "token"


def require_admin_key(request: Request) -> str:
    """admin-tier (BRAIN_EXTENSION_KEY) 必須の依存。弱い VOICE_ALIGN_TOKEN fallback を拒否。

    ★2026-07-14 世界基準評価: /api/brain/chat は run_agent を呼び interview/ (家族/弱さ/金/体)
    + Gmail + Drive の最機微に到達するのに require_api_key のみ = 弱い ?token= fallback でも
    通っていた。LINE webhook 経路は #37 で is_admin fail-closed 済なのに HTTP 経路だけ漏れ、
    自らの脅威モデルと矛盾。require_api_key の 503/401 セマンティクスは維持しつつ、token tier は
    403 で弾く (= brain_auth_tier=='admin' 必須。正規 chat UI は Bearer <admin key> なので不変)。
    """
    require_api_key(request)  # BRAIN_EXTENSION_KEY 未設定=503 / 完全無効=401 を先に確定
    if brain_auth_tier(request) != "admin":
        raise HTTPException(
            status_code=403,
            detail="admin key required (weak dashboard token cannot reach this endpoint)",
        )
    return "admin"


# ─── 署名検証 ───
def verify_signature(body: bytes, signature: str) -> bool:
    hash = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    return hmac.compare_digest(signature, base64.b64encode(hash).decode("utf-8"))


# ─── LINE Reply API ───
async def reply_message(
    http: httpx.AsyncClient,
    reply_token: str,
    text: str,
    quick_reply: list[dict] | None = None,
):
    """LINEのReply APIでメッセージを返信

    quick_reply: [{"label": "表示名", "data": "postback=...",  "type": "postback"|"message"}]
                 最後のメッセージにのみ付与される。
    """
    # LINE は 1 メッセージ 5000 文字制限 → 分割
    chunks = [text[i : i + 4500] for i in range(0, len(text), 4500)]
    messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]

    if quick_reply and messages:
        items = []
        for qr in quick_reply[:13]:  # LINE Quick Reply 上限 13
            action_type = qr.get("type", "postback")
            if action_type == "postback":
                action = {
                    "type": "postback",
                    "label": qr["label"][:20],
                    "data": qr["data"][:300],
                    "displayText": qr.get("display", qr["label"])[:300],
                }
            else:
                action = {
                    "type": "message",
                    "label": qr["label"][:20],
                    "text": qr.get("data", qr["label"])[:300],
                }
            items.append({"type": "action", "action": action})
        messages[-1]["quickReply"] = {"items": items}

    await http.post(
        "https://api.line.me/v2/bot/message/reply",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"replyToken": reply_token, "messages": messages},
    )


# ─── 目的選択 (Wikiの更新 / システム修正) ───
PENDING_INPUT_TTL_SEC = int(os.getenv("PENDING_INPUT_TTL_SEC", "900"))  # 15分
SYSTEM_IMPROVEMENTS_DIR = Path(
    os.getenv("SYSTEM_IMPROVEMENTS_DIR", "/app/data/brain/system_improvements")
)

# ─── Claude Code ディスパッチ用キューディレクトリ ───
CLAUDE_TASKS_DIR = Path(
    os.getenv("CLAUDE_TASKS_DIR", "/app/data/brain/claude_tasks")
)
CLAUDE_TASKS_PENDING = CLAUDE_TASKS_DIR / "pending"


def _queue_claude_task(
    user_id: str,
    instruction: str,
    source: str = "line",
    mode: str = "plan",
    approved_plan: str = "",
    parent_task_id: str = "",
) -> Path:
    """Claude Code への指示をタスクファイルとして pending/ に書き出す。

    mode:
      "plan"    — 現状調査と変更計画だけを返させる（ファイル書換なし）
      "execute" — 承認済み plan を元に実装を実行

    ホスト側 claude_dispatcher.py が数秒後に拾って `claude -p` を
    --dangerously-skip-permissions で実行し、結果を main.py の
    /api/claude/notify に返す。そこからユーザーに LINE Push 通知。
    """
    CLAUDE_TASKS_PENDING.mkdir(parents=True, exist_ok=True)
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    task_path = CLAUDE_TASKS_PENDING / f"{task_id}.json"
    payload = {
        "task_id": task_id,
        "user_id": user_id,
        "instruction": instruction,
        "source": source,
        "mode": mode,
        "approved_plan": approved_plan,
        "parent_task_id": parent_task_id,
        "created_at": datetime.now().isoformat(),
    }
    tmp = task_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.rename(task_path)
    logger.info(
        f"claude task queued: {task_path.name} mode={mode} "
        f"({len(instruction)} chars)"
    )
    return task_path


# ─── Claude Code プラン承認フロー用 Quick Reply ───
def _plan_quick_reply(task_id: str) -> list[dict]:
    return [
        {"label": "✅ 実行", "data": f"claude=approve&task={task_id}", "display": "✅ 承認して実行"},
        {"label": "✏️ 修正", "data": f"claude=revise&task={task_id}", "display": "✏️ 修正指示を出す"},
        {"label": "❌ キャンセル", "data": f"claude=cancel&task={task_id}", "display": "❌ キャンセル"},
    ]


CLAUDE_REVISE_TTL = int(os.getenv("CLAUDE_REVISE_TTL", "3600"))  # 1h
CLAUDE_PLAN_TTL = int(os.getenv("CLAUDE_PLAN_TTL", "86400"))     # 24h

# ★2026-07-20 Umiyama AI Agent 正式化: 目的選択 Quick Reply は廃止 (通常テキスト = run_agent 直行)。
# PURPOSE_QUICK_REPLY / _store_pending_input / _has_pending_system_question は dead code として削除。
# _pop_pending_input と _handle_purpose_postback は過去メッセージの stale ボタン tap を
# 無害に処理するため残置 (数週間後に掃討可)。


async def _pop_pending_input(r: redis.Redis, user_id: str) -> str | None:
    val = await r.get(f"pending_input:{user_id}")
    if val is not None:
        await r.delete(f"pending_input:{user_id}")
    return val


async def _log_system_improvement(user_id: str, message: str) -> Path:
    """ユーザー申告の「システム修正」を日付ごとの .md に追記"""
    SYSTEM_IMPROVEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    path = SYSTEM_IMPROVEMENTS_DIR / f"{today}.md"
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = (
        f"\n## {timestamp} (user={user_id[:8]})\n"
        f"{message.strip()}\n"
        f"\n---\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(entry)
    return path


# ─── LINE Push API ───
ALIGNMENT_TARGET_USER = os.getenv("ALIGNMENT_TARGET_USER", "")


def _build_pending_minidigest(
    items: list[dict],
    kind: str,
    title: str | None = None,
    max_items: int = 3,
) -> tuple[str, list[dict]]:
    """clone_feedback / clone_learning の pending items から
    Quick Reply 付き mini digest を組み立てる。

    Args:
        items: clone_feedback.list_pending() / clone_learning.list_pending() の戻り値
        kind: "feedback" or "learning"
        title: 先頭タイトル (None なら自動生成)
        max_items: 1 メッセに乗せるアイテム数
            ★2026-05-12: 3 件 × 4 ボタン (取込/見送/既読/コメント) = 12 + 全件一覧 = 13 (LINE 上限)

    Returns:
        (text, quick_reply_list)
    """
    cmd_prefix = f"/clone-{kind}"
    icon = "📋" if kind == "feedback" else "🧠"
    label = "修正希望" if kind == "feedback" else "会話発見"

    visible = items[:max_items]
    remaining = len(items) - len(visible)

    if title is None:
        title = f"{icon} うみやまAI {label}ダイジェスト"

    lines = [
        title,
        f"未処理: {len(items)} 件" + (f" / 表示 {len(visible)} 件" if remaining > 0 else ""),
        "━━━━━━━━━━━━━━━",
    ]

    qr: list[dict] = []
    for i, r in enumerate(visible, start=1):
        ts = (r.get("timestamp") or "")[5:16].replace("T", " ")
        display = (r.get("user_display") or r.get("user_id", "")[:8])[:14]
        if kind == "feedback":
            q = (r.get("trigger_msg") or "")[:50]
            content = (r.get("feedback") or "")[:80]
            lines.append(f"\n[{i}] {ts} / {display}")
            if q:
                lines.append(f"  Q: {q}")
            lines.append(f"  ✏️ {content}")
            # ★2026-05-12: 既存コメントがあれば表示 (海山が前に書いた)
            cm = r.get("comments") or []
            if cm:
                last = cm[-1] if isinstance(cm, list) else None
                if last and last.get("text"):
                    lines.append(f"  💬 ({len(cm)}件) {last['text'][:60]}")
        else:  # learning
            cat = r.get("category", "")
            insight = (r.get("insight") or "")[:120]
            patch = (r.get("proposed_wiki_patch") or "")[:60]
            lines.append(f"\n[{i}] {ts} / {cat}")
            lines.append(f"  💡 {insight}")
            if patch:
                lines.append(f"  → {patch}")
            cm = r.get("comments") or []
            if cm:
                last = cm[-1] if isinstance(cm, list) else None
                if last and last.get("text"):
                    lines.append(f"  💬 ({len(cm)}件) {last['text'][:60]}")
        # Quick Reply 4 ボタン (★2026-05-12: コメント追加、3 item × 4 = 12 + 全件 = 13)
        item_id = r.get("id", "")
        qr.append({
            "type": "message",
            "label": f"{i}✅取込",
            "data": f"{cmd_prefix}-accept {item_id}",
        })
        qr.append({
            "type": "message",
            "label": f"{i}❌見送",
            "data": f"{cmd_prefix}-reject {item_id}",
        })
        qr.append({
            "type": "message",
            "label": f"{i}📝既読",
            "data": f"{cmd_prefix}-note {item_id}",
        })
        qr.append({
            "type": "message",
            "label": f"{i}💬コメ",
            "data": f"{cmd_prefix}-comment {item_id}",
        })

    if remaining > 0:
        lines.append(f"\n…他 {remaining} 件 (処理後に続きを表示)")

    # 1 つだけ余ってる Quick Reply 枠を「全件一覧」に当てる
    if len(qr) <= 12:
        qr.append({
            "type": "message",
            "label": "📋 全件一覧",
            "data": cmd_prefix,
        })

    return "\n".join(lines), qr

async def push_message(
    http: httpx.AsyncClient,
    user_id: str,
    text: str,
    quick_reply: list[dict] | None = None,
):
    """LINEのPush APIでメッセージを能動的に送信。quick_reply 対応。"""
    chunks = [text[i : i + 4500] for i in range(0, len(text), 4500)]
    messages = [{"type": "text", "text": chunk} for chunk in chunks[:5]]

    if quick_reply and messages:
        items = []
        for qr in quick_reply[:13]:
            action_type = qr.get("type", "postback")
            if action_type == "postback":
                action = {
                    "type": "postback",
                    "label": qr["label"][:20],
                    "data": qr["data"][:300],
                    "displayText": qr.get("display", qr["label"])[:300],
                }
            else:
                action = {
                    "type": "message",
                    "label": qr["label"][:20],
                    "text": qr.get("data", qr["label"])[:300],
                }
            items.append({"type": "action", "action": action})
        messages[-1]["quickReply"] = {"items": items}

    await http.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"to": user_id, "messages": messages},
    )


# ─── LINE メディア取得 & Wiki 取り込み ───
LINE_MEDIA_MAX_BYTES = int(os.getenv("LINE_MEDIA_MAX_BYTES", str(30 * 1024 * 1024)))  # 30MB


async def _fetch_line_media(http: httpx.AsyncClient, message_id: str) -> bytes:
    """LINE Messaging API からメディアコンテンツをダウンロード"""
    resp = await http.get(
        f"https://api-data.line.me/v2/bot/message/{message_id}/content",
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.content


async def _handle_line_media(app, event: dict, user_id: str, reply_token: str) -> None:
    """LINE 画像/動画/音声/ファイル を Wiki に取り込む

    フロー:
      1. LINE API からメディア取得
      2. 画像 → Vision API で OCR / 説明
         ファイル → content_extractor でテキスト抽出
      3. PrivacyGate 通過後、ingest_note → 即コンパイル
      4. 確認リプライ
    """
    from content_extractor import extract_file_text, extract_image_text
    import tempfile

    msg = event["message"]
    msg_type = msg.get("type")
    message_id = msg.get("id")

    # LINE が渡してくれるメタ情報
    original_filename = msg.get("fileName") or ""
    mime_content_type = msg.get("contentType") or ""
    file_size = msg.get("fileSize") or 0

    if file_size and file_size > LINE_MEDIA_MAX_BYTES:
        await reply_message(
            app.state.http, reply_token,
            f"⚠️ ファイルサイズが上限({LINE_MEDIA_MAX_BYTES // (1024*1024)}MB)を超えています。"
        )
        return

    try:
        raw_bytes = await _fetch_line_media(app.state.http, message_id)
    except Exception as e:
        logger.warning(f"LINE media fetch error ({msg_type}): {e}")
        await reply_message(
            app.state.http, reply_token,
            f"⚠️ メディアの取得に失敗しました: {e}"
        )
        return

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    extracted_text: str = ""
    title = ""

    try:
        if msg_type == "image":
            # 画像は Vision API に base64 で渡す
            b64 = base64.b64encode(raw_bytes).decode()
            data_url = f"data:image/jpeg;base64,{b64}"
            extracted_text = await extract_image_text(
                data_url, app.state.http, LITELLM_URL, LITELLM_KEY
            ) or ""
            title = f"line_image_{timestamp}"

        elif msg_type == "file":
            # ファイルは拡張子に応じて抽出
            safe_name = Path(original_filename or f"file_{timestamp}").name
            safe_name = "".join(
                c for c in safe_name if c.isalnum() or c in "._-" or ord(c) > 127
            ) or f"file_{timestamp}"

            # ─── .zip の場合、中の .txt を抽出（WhatsApp iOS エクスポートは .zip）───
            if safe_name.lower().endswith(".zip"):
                import zipfile, io
                try:
                    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
                        txt_names = [
                            n for n in zf.namelist()
                            if n.lower().endswith(".txt") and not n.startswith("__MACOSX")
                        ]
                        # WhatsApp は "_chat.txt" が定番。見つからなければ最初の .txt
                        preferred = next((n for n in txt_names if "_chat" in n.lower()), None)
                        chosen = preferred or (txt_names[0] if txt_names else None)
                        if chosen:
                            extracted_bytes = zf.read(chosen)
                            inner_name = Path(chosen).name
                            logger.info(
                                f"ZIP 解凍: {safe_name} → {inner_name} "
                                f"({len(extracted_bytes)/1024:.1f}KB)"
                            )
                            raw_bytes = extracted_bytes
                            # 相手名推定のためにファイル名は zip のものを保持
                            # （例: "WhatsApp Chat - John.zip" の内部は "_chat.txt" だが、
                            #  zip 名で相手を判別する）
                            zip_stem = Path(safe_name).stem
                            safe_name = f"{zip_stem}.txt"
                            original_filename = original_filename or f"{zip_stem}.txt"
                except zipfile.BadZipFile:
                    logger.warning(f"Bad zip: {safe_name}")

            # ─── chat エクスポートを最初に検出 → chat_import パイプラインへ ───
            # 通常の extract_file_text は 5000字で truncate するため、chat export は別ルート
            if safe_name.lower().endswith(".txt"):
                try:
                    sample = raw_bytes[:8192].decode("utf-8", errors="replace")
                except Exception:
                    sample = ""
                from chat_import import detect_chat_format
                fmt = detect_chat_format(sample)
                if fmt in ("line", "whatsapp"):
                    # chat export: import/ に保存 → process_chat_export で処理
                    import shutil
                    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
                    processed_dir = IMPORT_DIR / "processed"
                    processed_dir.mkdir(exist_ok=True)
                    fmt_label = "LINE" if fmt == "line" else "WhatsApp"
                    final_name = f"{fmt}_chat_{timestamp}_{safe_name}"
                    saved_path = IMPORT_DIR / final_name
                    saved_path.write_bytes(raw_bytes)

                    # 即時処理（ファイルウォッチャー待ちにしない、即フィードバック）
                    try:
                        result = await process_chat_export(
                            saved_path, app.state.privacy, app.state.brain
                        )
                        # processed/ に移動
                        try:
                            shutil.move(str(saved_path), str(processed_dir / final_name))
                        except Exception:
                            pass
                    except Exception as e:
                        logger.exception(f"{fmt_label} chat import failed")
                        await reply_message(
                            app.state.http, reply_token,
                            f"⚠️ {fmt_label} chat 取り込みでエラー: {e}"
                        )
                        return

                    size_kb = len(raw_bytes) / 1024
                    await reply_message(
                        app.state.http, reply_token,
                        f"✅ {fmt_label} chat 取り込み完了\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"📄 {original_filename or safe_name} ({size_kb:.1f}KB)\n"
                        f"📊 メッセージ: {result['total']:,} 件\n"
                        f"   ✓ 取り込み: {result['allowed']:,}\n"
                        f"   🔒 ブロック: {result['blocked']:,}\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"Wiki にコンパイル中…"
                    )
                    return  # ここで終了、generic ingest には流さない

            # ─── 通常ファイル（PDF / docx / xlsx / 普通の .txt 等） ───
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_path = Path(tmpdir) / safe_name
                tmp_path.write_bytes(raw_bytes)
                extracted_text = await extract_file_text(tmp_path) or ""
            title = f"line_file_{timestamp}_{Path(safe_name).stem}"[:60]

        elif msg_type == "audio":
            # 音声は現状テキスト化しない（Whisper 連携は別タスク）
            extracted_text = f"[LINE 音声メモ {timestamp}]\n音声ファイルを受信しました（文字起こし未対応）。"
            title = f"line_audio_{timestamp}"

        elif msg_type == "video":
            extracted_text = f"[LINE 動画 {timestamp}]\n動画を受信しました（内容抽出未対応）。"
            title = f"line_video_{timestamp}"

        else:
            logger.info(f"Skipping unsupported LINE message type: {msg_type}")
            return
    except Exception as e:
        logger.warning(f"LINE media extract error ({msg_type}): {e}")
        await reply_message(
            app.state.http, reply_token,
            f"⚠️ テキスト抽出に失敗しました: {e}"
        )
        return

    if not extracted_text or not extracted_text.strip():
        await reply_message(
            app.state.http, reply_token,
            f"⚠️ {msg_type} からテキストを抽出できませんでした（未対応形式 or 空ファイル）。"
        )
        return

    # PrivacyGate でフィルタ（家族会話等の除外）
    try:
        result = await app.state.privacy.filter(extracted_text, sender_id=user_id)
        if result.verdict.value != "allow":
            await reply_message(
                app.state.http, reply_token,
                f"🔒 PrivacyGate によりブロック（{result.verdict.value}）: Wiki には保存しません。"
            )
            return
        content_for_wiki = result.sanitized
    except Exception as e:
        logger.warning(f"PrivacyGate error on media: {e}")
        content_for_wiki = extracted_text

    # Wiki へ取り込み（明示的アップロードなので smart モデル）
    header = (
        f"source: LINE\n"
        f"type: {msg_type}\n"
        f"filename: {original_filename or '(なし)'}\n"
        f"mime: {mime_content_type or '(不明)'}\n"
        f"received: {datetime.now().isoformat()}\n"
        f"\n"
    )
    note_body = header + content_for_wiki

    try:
        await app.state.brain.ingest_note(
            user_id, note_body, title=title, model="smart"
        )
    except Exception as e:
        logger.exception("ingest_note failed for LINE media")
        await reply_message(
            app.state.http, reply_token,
            f"⚠️ Wiki 取り込みでエラーが発生しました: {e}"
        )
        return

    # 確認リプライ
    preview = content_for_wiki.strip().replace("\n", " ")[:100]
    size_kb = len(raw_bytes) / 1024
    label = {
        "image": "📷 画像",
        "file": f"📎 ファイル ({original_filename or '?'})",
        "audio": "🎙 音声",
        "video": "🎬 動画",
    }.get(msg_type, msg_type)
    await reply_message(
        app.state.http, reply_token,
        f"✅ Wiki に登録しました\n{label}（{size_kb:.1f}KB）\n\n"
        f"抽出: {len(content_for_wiki)}字\n「{preview}...」"
    )


# ─── NotebookLM URL 取り込み ───

async def _handle_notebooklm_urls(
    app, text: str, user_id: str, reply_token: str
) -> bool:
    """テキストに NotebookLM URL が含まれていたら抽出 → Wiki 取り込み。
    処理したら True（呼び出し側は通常フロー継続をスキップ）。
    """
    urls = find_notebooklm_urls(text or "")
    if not urls:
        return False

    # 重複排除（同じ URL を複数回書いていても1回だけ処理）
    seen: set[str] = set()
    unique_urls = []
    for u in urls:
        nid = get_notebook_id(u) or u
        if nid in seen:
            continue
        seen.add(nid)
        unique_urls.append(u)

    results_summary: list[str] = []
    needs_auth = False

    for url in unique_urls[:3]:  # 一度に処理する上限: 3 URLs
        try:
            result = await extract_notebooklm(url)
        except Exception as e:
            logger.exception("NotebookLM extract crashed")
            results_summary.append(f"⚠️ {url}\n  失敗: {e}")
            continue

        if result.get("needs_auth"):
            needs_auth = True
            results_summary.append(
                f"🔑 {url}\n  Google 認証が必要 → サーバ上で次を実行してください:\n"
                f"  `python3 notebooklm_extractor.py --login`"
            )
            continue

        if not result.get("ok"):
            err = result.get("error", "unknown")
            results_summary.append(f"⚠️ {url}\n  抽出失敗: {err}")
            continue

        text_content = result.get("text") or ""
        title = result.get("title") or f"notebooklm_{get_notebook_id(url) or 'unknown'}"
        notebook_id = get_notebook_id(url) or "unknown"

        # PrivacyGate
        try:
            privacy_result = await app.state.privacy.filter(
                text_content, sender_id=user_id
            )
            if privacy_result.verdict.value != "allow":
                results_summary.append(
                    f"🔒 {title}\n  PrivacyGate: {privacy_result.verdict.value}"
                )
                continue
            sanitized = privacy_result.sanitized
        except Exception as e:
            logger.warning(f"PrivacyGate error on NotebookLM: {e}")
            sanitized = text_content

        # 取り込み本文（source メタ情報をヘッダに）
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        header = (
            f"source: NotebookLM\n"
            f"url: {url}\n"
            f"notebook_id: {notebook_id}\n"
            f"title: {title}\n"
            f"ingested: {datetime.now().isoformat()}\n\n"
        )
        ingest_title = f"notebooklm_{notebook_id[:10]}_{ts}"

        try:
            await app.state.brain.ingest_note(
                user_id, header + sanitized, title=ingest_title, model="smart"
            )
        except Exception as e:
            logger.exception("NotebookLM ingest_note failed")
            results_summary.append(f"⚠️ {title}\n  Wiki 取り込み失敗: {e}")
            continue

        preview = sanitized.strip().replace("\n", " ")[:80]
        results_summary.append(
            f"📘 {title}\n  {len(sanitized):,}字 取り込み\n  「{preview}…」"
        )

    if not results_summary:
        return False

    msg_lines = ["NotebookLM 取り込み結果", "━━━━━━━━━━━━━━━"]
    msg_lines.extend(results_summary)
    if needs_auth:
        msg_lines.append(
            "\nログイン後はもう一度同じ URL を送ってください。"
        )
    await reply_message(app.state.http, reply_token, "\n".join(msg_lines))
    return True


# ─── ライブデータ取得 ───

async def _detect_intents_llm(
    http: httpx.AsyncClient,
    msg: str,
    conversation_history: list[dict] = None,
    calendar_summary: str = "",
) -> dict:
    """
    LLMで意図分類。会話履歴+スケジュールのコンテキストから推測。

    Returns:
        {
            "sources": ["calendar", "drive", ...],  # 必要なデータソース
            "search_query": "AOP FY27",              # Drive検索キーワード
            "clarify": null or "具体的にどの資料？"     # 不明時の質問返し
            "calendar_days": 1,                       # カレンダー取得日数
        }
    """
    # 直近の会話を要約
    conv_context = ""
    if conversation_history:
        recent = conversation_history[-6:]  # 直近3往復
        conv_lines = []
        for m in recent:
            role = "CEO" if m["role"] == "user" else "AI"
            conv_lines.append(f"{role}: {m['content'][:100]}")
        conv_context = f"\n\n【直近の会話】\n" + "\n".join(conv_lines)

    # 今日のスケジュール
    cal_context = ""
    if calendar_summary:
        cal_context = f"\n\n【本日のスケジュール】\n{calendar_summary}"

    prompt = f"""あなたはCEOのAIアシスタントの意図分類エンジンです。
CEOのメッセージを分析し、どのデータソースが必要か判定してください。

【データソース】
- calendar: 予定・スケジュール・会議情報
- mail: メール・受信・連絡
- drive: 社内資料・数値データ・PL・予算・報告書・スプレッドシート等
- wiki: 過去の会話・知識ベース・人物情報・方針

【判定ルール】
- 会話の流れから「あれ」「それ」「さっきの」等の指示語の意味を推測せよ
- スケジュールの文脈を使え。例: MTGの直前に「資料」と言えばそのMTGの関連資料
- 数値・目標・売上・利益・KPI等はdriveを含めよ
- 人について聞いているならwikiを含めよ
- 迷ったら複数のソースを含めよ
- 意図が本当に不明な場合のみ clarify に質問を入れよ（極力推測して答えること）
{conv_context}{cal_context}

【CEOのメッセージ】
{msg}

【回答形式】JSON のみ返せ。説明不要。
{{
  "sources": ["drive", "wiki"],
  "search_query": "検索キーワード（drive使用時）",
  "clarify": null,
  "calendar_days": 1
}}"""

    try:
        resp = await http.post(
            f"{LITELLM_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            json={
                "model": "fast",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0,
            },
            timeout=8.0,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()

        import re as _re
        match = _re.search(r'\{.*\}', text, _re.DOTALL)
        if match:
            result = json.loads(match.group())
            # sources の正規化
            valid = {"calendar", "mail", "drive", "wiki"}
            result["sources"] = [s for s in result.get("sources", []) if s in valid]
            if "wiki" not in result["sources"]:
                result["sources"].append("wiki")
            return result
    except Exception as e:
        logger.warning(f"Intent LLM error: {e}, falling back to keyword")

    # フォールバック
    kw_intents = _detect_intents_keyword(msg)
    return {"sources": list(kw_intents), "search_query": msg[:30], "clarify": None, "calendar_days": 1}


def _detect_intents_keyword(msg: str) -> set[str]:
    """キーワードベース意図検出（フォールバック用）"""
    intents = set()
    m = msg.lower()

    cal_kw = ["予定", "スケジュール", "カレンダー", "会議", "ミーティング", "mtg",
              "今日", "明日", "来週", "schedule"]
    mail_kw = ["メール", "mail", "gmail", "受信", "inbox", "返信"]
    drive_kw = ["ドライブ", "drive", "ファイル", "資料", "スプレッドシート", "ドキュメント",
                "doc", "sheet", "pl", "pnl", "aop", "予算", "企画書", "報告書", "議事録",
                "提案書", "見積", "契約", "マニュアル", "プレゼン", "売上", "目標", "数値",
                "kpi", "業績", "財務", "fy", "revenue", "profit"]
    wiki_kw = ["wiki", "ブレイン", "brain", "知識", "記憶", "以前", "前に"]

    if any(kw in m for kw in cal_kw):
        intents.add("calendar")
    if any(kw in m for kw in mail_kw):
        intents.add("mail")
    if any(kw in m for kw in drive_kw):
        intents.add("drive")
    if any(kw in m for kw in wiki_kw):
        intents.add("wiki")

    intents.add("wiki")
    return intents


def _fetch_calendar_context(days: int = 1) -> str:
    """Google Calendar API から今日の予定を取得"""
    try:
        from google_sync import get_credentials, sync_calendar
        creds = get_credentials()
        events = sync_calendar(creds, days=days, dry_run=True)
        if not events:
            return "（予定なし）"
        lines = []
        for ev in events:
            if "T" in ev["start"]:
                t = ev["start"][11:16]
            else:
                t = "終日"
            parts = [f"[{t}] {ev['summary']}"]
            if ev.get("location"):
                parts.append(f"@{ev['location']}")
            if ev.get("attendees"):
                parts.append(f"({', '.join(ev['attendees'][:3])})")
            lines.append(" ".join(parts))
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Calendar context error: {e}")
        return f"（カレンダー取得エラー: {e}）"


def _fetch_mail_context(days: int = 1, max_emails: int = 10) -> str:
    """Gmail API から直近メールのサマリーを取得"""
    try:
        from google_sync import get_credentials, sync_gmail
        creds = get_credentials()
        emails = sync_gmail(creds, days=days, max_emails=max_emails, dry_run=True)
        if not emails:
            return "（新着メールなし）"
        lines = []
        for em in emails:
            unread = "*" if em.get("unread") else " "
            sender = em["from"].split("<")[0].strip().strip('"')[:20]
            lines.append(f"{unread} {sender} | {em['subject'][:50]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Mail context error: {e}")
        return f"（メール取得エラー: {e}）"


def _fetch_drive_context(query: str, max_files: int = 10, read_content: bool = False) -> str:
    """Google Drive から関連ファイルを検索。read_content=Trueなら上位ファイルの中身も取得.

    ★2026-05-26 海山指示「給与・人事評価等の機密情報はアクセスできない機能」 強化:
    - gdrive_sync.DEFAULT_EXCLUDE_PATTERN を post-hoc filter で適用 (= 名前マッチ除外)
    - gdrive_sync.is_confidential_file で **親フォルダ名 も check** (= 「給与」 folder 配下を全 block)
    - Drive API `q` field に `not name contains '...'` 高頻出 keyword 注入で server-side 除外
    - 多層防御: bypass を防ぐため filter は read_content より前 (= 中身読み込み前) に適用
    """
    try:
        from google_sync import get_credentials, _download_and_extract
        from gdrive_sync import (
            DEFAULT_EXCLUDE_PATTERN, is_confidential_file, build_drive_exclude_clause,
        )
        creds = get_credentials()
        from googleapiclient.discovery import build as gbuild
        service = gbuild("drive", "v3", credentials=creds)

        # クエリのサニタイズ（シングルクォート除去）
        safe_query = query.replace("'", "").strip()

        # ★ Drive API レベル exclude 注入 (= 主要 keyword で server-side 除外)
        exclude_clause = build_drive_exclude_clause()

        # ★ 親フォルダ名 check 用 cache (= 同 batch 内の重複 API call 削減)
        parent_name_cache: dict = {}

        # ★ 共通の post-hoc confidential filter
        def _filter_confidential(file_list: list) -> list:
            filtered = []
            for f in file_list:
                is_conf, reason = is_confidential_file(f, drive_service=service, parent_name_cache=parent_name_cache)
                if is_conf:
                    logger.info(f"[drive-confidential] blocked: {reason}")
                    continue
                filtered.append(f)
            return filtered

        # ★ 共通 fields (= parents 追加で 親フォルダ check 可能化)
        FIELDS = "files(id, name, mimeType, modifiedTime, webViewLink, parents)"

        # ★ Drive API call ラッパー (= 400 BadRequest 時に exclude_clause を外して再試行)
        # exclude_clause が長すぎる / 互換性問題で 400 が返る可能性に備えた fallback。
        # その場合でも post-hoc filter `_filter_confidential` が full coverage 保証。
        #
        # ★2026-05-26: corpora="allDrives" + supportsAllDrives=True 追加。
        # 既存 path は MY drive のみで shared drive 不可視 → 機密 file 実在の path
        # (= shared drive) も含めて検索 + filter する。`/drive ai` (= discover()) と
        # scope 統一、機密 filter の意味を実効化。
        def _list_with_fallback(q_base: str, page_size: int) -> list:
            common_kw = dict(
                pageSize=page_size,
                fields=FIELDS,
                orderBy="modifiedTime desc",
                corpora="allDrives",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            try:
                r = service.files().list(
                    q=f"{q_base} and {exclude_clause}", **common_kw,
                ).execute()
                return r.get("files", [])
            except Exception as e:
                logger.warning(f"[drive] exclude clause failed ({e}), retry without it (post-hoc filter still applies)")
                try:
                    r = service.files().list(q=q_base, **common_kw).execute()
                    return r.get("files", [])
                except Exception as e2:
                    logger.warning(f"[drive] fallback also failed: {e2}")
                    return []

        files = []
        if safe_query:
            # ファイル名検索
            raw = _list_with_fallback(
                f"name contains '{safe_query}' and trashed = false", max_files * 2
            )
            files = _filter_confidential(raw)[:max_files]

            # 全文検索フォールバック
            if not files:
                raw = _list_with_fallback(
                    f"fullText contains '{safe_query}' and trashed = false", max_files * 2
                )
                files = _filter_confidential(raw)[:max_files]

        if not files:
            # 最近更新ファイル fallback
            raw = _list_with_fallback("trashed = false", 10)
            files = _filter_confidential(raw)[:5]

        if not files:
            return "（ファイルなし）"

        lines = []
        for f in files:
            mod = f.get("modifiedTime", "")[:10]
            link = f.get("webViewLink", "")
            lines.append(f"- {f['name']} ({mod}) {link}")

        result = "【検索結果】\n" + "\n".join(lines)

        # 中身の読み込み（上位2件まで）
        if read_content:
            max_chars_per_file = 12000  # PL等の大きなスプレッドシート対応
            for f in files[:2]:
                try:
                    logger.info(f"Drive reading: {f['name']} ({f['mimeType']})")
                    text = _download_and_extract(
                        service, f["id"], f["name"], f["mimeType"]
                    )
                    if text:
                        truncated = text[:max_chars_per_file]
                        if len(text) > max_chars_per_file:
                            truncated += f"\n...(以下省略、全{len(text)}文字)"
                        result += f"\n\n【ファイル内容: {f['name']}】\n{truncated}"
                    else:
                        result += f"\n\n【{f['name']}】（テキスト抽出結果なし）"
                except Exception as e:
                    logger.warning(f"Drive read error for {f['name']}: {e}")
                    result += f"\n\n【{f['name']}】読み込みエラー: {e}"

        return result
    except Exception as e:
        logger.warning(f"Drive context error: {e}")
        return f"（Drive検索エラー: {e}）"


# ─── LLM エージェント呼び出し ───
def _is_business_data_query(message: str) -> bool:
    """売上/客数等の業務データ・社内規程・施設商圏の照会か (= clone 回答エンジンへ pre-route)。
    ロジックは brain_wiki_helpers/business_intent (§1.12b、単体テスト可能)。施設検出は
    lookup_service を注入。"""
    def _facility(m):
        try:
            from scripts.tenpo import lookup_service as _tls
            return bool(_tls.clone_context(m, admin=True))
        except Exception:
            return False
    from brain_wiki_helpers.business_intent import is_business_data_query
    return is_business_data_query(message, facility_detector=_facility)


def _is_business_followup(message: str) -> bool:
    from brain_wiki_helpers.business_intent import is_business_followup
    return is_business_followup(message)


async def run_agent(
    app,
    http: httpx.AsyncClient,
    r: redis.Redis,
    user_id: str,
    user_message: str,
) -> str:
    """
    LiteLLM 経由でLLMを呼び出す。
    メッセージの意図に応じてカレンダー・メール・Drive・Wikiを横断取得。
    業務データ質問は先頭で clone 回答エンジンへ pre-route (verbatim, ガード保証)。
    """
    history_key = f"chat:{user_id}"

    # 直近の会話履歴を取得 (最大20ターン)
    raw_history = await r.lrange(history_key, -40, -1)
    messages = [json.loads(m) for m in raw_history]

    # ★2026-07-20 海山「うみやまAIと同じ質問・回答機能も持たせたい」+ §1.15 cross-check 反映:
    # 業務データ (売上/客数/予算比/前年比/制度規程/施設商圏) の質問は、社員向けと**同一の回答
    # エンジン** (_safe_clone_respond = canonical 注入 + sales_numeric_guard 桁事故ガード) へ決定論
    # pre-route し、その出力を **verbatim** で返す (外側 agent LLM を挟まない = ガード保証)。
    # ★フォローアップ対応 (「日本の」型): 直前が売上応答なら短い継続語も業務照会扱いにし、直前の
    #   売上質問と併合した effective query を渡す (単独では日付/次元が無く retrieval が失敗するため)。
    _prev_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    _biz_direct = _is_business_data_query(user_message)
    _biz_follow = (
        not _biz_direct
        and _is_business_data_query(_prev_user)          # 直前が売上質問
        and _is_business_followup(user_message)          # 今回が短い継続語
    )
    if _biz_direct or _biz_follow:
        effective = user_message
        if _biz_follow:
            # ★cross-check DA: 前クエリの次元(エリア/業態)ではなく日付だけ引き継ぐ =
            #   今回メッセージの次元(「日本の」「業態別」)を優先 (次元シャドウ防止)。
            from brain_wiki_helpers.business_intent import extract_date_phrase
            _dp = extract_date_phrase(_prev_user)
            effective = f"{_dp} {user_message}".strip() if _dp else user_message
        logger.info(f"[run_agent] business pre-route ({'follow' if _biz_follow else 'direct'}) "
                    f"→ clone: {effective[:60]!r}")
        reply = await _safe_clone_respond(
            app.state.brain, effective,
            history=messages[-6:],
            model=os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart"),
        )
        await r.rpush(history_key, json.dumps({"role": "user", "content": user_message}))
        await r.rpush(history_key, json.dumps({"role": "assistant", "content": reply}))
        await r.ltrim(history_key, -40, -1)
        await r.expire(history_key, 86400 * 7)
        return reply

    # 常にカレンダーを軽量取得（意図推測の文脈として使う）
    calendar_summary = ""
    try:
        calendar_summary = await asyncio.to_thread(_fetch_calendar_context, 1)
    except Exception:
        pass

    # LLM意図検出（会話履歴+スケジュール文脈込み）
    intent_result = await _detect_intents_llm(
        http, user_message,
        conversation_history=messages,
        calendar_summary=calendar_summary,
    )
    sources = set(intent_result.get("sources", ["wiki"]))
    search_query = intent_result.get("search_query", "")
    clarify = intent_result.get("clarify")
    cal_days = intent_result.get("calendar_days", 1)
    logger.info(f"Intent result: sources={sources}, query={search_query}, clarify={clarify}")

    # 意図不明 → 質問返し
    if clarify and not sources - {"wiki"}:
        # 質問返しも履歴に保存
        await r.rpush(history_key, json.dumps({"role": "user", "content": user_message}))
        await r.rpush(history_key, json.dumps({"role": "assistant", "content": clarify}))
        await r.ltrim(history_key, -40, -1)
        await r.expire(history_key, 86400 * 7)
        return clarify

    # 各データソースからコンテキストを並列取得（ブロッキングしない）
    context_sections = []
    tasks = {}

    if "wiki" in sources:
        async def _wiki():
            try:
                bi: BrainIndex = app.state.brain_index
                return await bi.build_context(user_message, max_chars=3000)
            except Exception as e:
                logger.warning(f"RAG context error: {e}")
                return ""
        tasks["wiki"] = asyncio.create_task(_wiki())

    if "calendar" in sources and cal_days > 1:
        tasks["calendar"] = asyncio.create_task(
            asyncio.to_thread(_fetch_calendar_context, cal_days)
        )

    if "mail" in sources:
        tasks["mail"] = asyncio.create_task(
            asyncio.to_thread(_fetch_mail_context, 1, 15)
        )

    if "drive" in sources:
        q = search_query or user_message[:30]
        tasks["drive"] = asyncio.create_task(
            asyncio.to_thread(_fetch_drive_context, q, 10, True)
        )

    # 並列待ち
    results = {}
    for k, t in tasks.items():
        try:
            results[k] = await t
        except Exception as e:
            logger.warning(f"Context {k} error: {e}")
            results[k] = ""

    if results.get("wiki"):
        context_sections.append(f"## Brain Wiki（関連知識）\n{results['wiki']}")
    if "calendar" in sources:
        cal_text = results.get("calendar") or calendar_summary
        context_sections.append(f"## 今後の予定（Google Calendar）\n{cal_text}")
    if results.get("mail"):
        context_sections.append(f"## 直近のメール（Gmail）\n{results['mail']}")
    if results.get("drive"):
        context_sections.append(f"## Google Drive（資料内容）\n{results['drive']}")

    # ★2026-07-11 施設/商圏の個別照会 → 空白地DB プロファイルを context 注入 (この経路は admin gate 済)
    try:
        from scripts.tenpo import lookup_service as _tls
        _fc = _tls.clone_context(user_message, admin=True)
        if _fc:
            context_sections.append("## " + _fc)
    except Exception as _e:
        logger.warning(f"[run_agent] facility lookup skip: {type(_e).__name__}: {_e}")

    # システムプロンプト
    live_context = ""
    if context_sections:
        live_context = "\n\n" + "\n\n".join(context_sections)

    # 自己改善パッチを読み込み
    prompt_patches = load_system_prompt_patches()

    # ★2026-07-20 agentic 化 (個人エージェント評価 #1): persona+owner-memory 常時注入 +
    # bounded tool-loop (reminder/task/memory 書込 + 追加検索)。ロジックは services/agent_core.py
    # (§1.12b)。既存 prefetch は round-0 context として維持 = 単純質問のレイテンシ不変。
    from services import agent_core as _ac, owner_memory as _om
    system = {"role": "system", "content": _ac.build_system_prompt(
        live_context, prompt_patches, datetime.now().strftime("%Y-%m-%d %H:%M"))}

    # モデル選択ロジック
    model = select_model(user_message)

    messages_payload = [system] + messages + [{"role": "user", "content": user_message}]

    _bi: BrainIndex = app.state.brain_index
    executors = _ac.merge_executors({
        "search_brain": lambda a: _bi.build_context(str(a.get("query", "")), max_chars=2000),
        "search_drive": lambda a: asyncio.to_thread(_fetch_drive_context, str(a.get("query", "")), 10, True),
        "get_calendar": lambda a: asyncio.to_thread(_fetch_calendar_context, max(1, min(int(a.get("days", 3)), 30))),
        "get_mail": lambda a: asyncio.to_thread(_fetch_mail_context, max(1, min(int(a.get("days", 1)), 7)), 15),
    })

    try:
        reply = await _ac.run_tool_loop(
            http, LITELLM_URL, LITELLM_KEY, model, messages_payload, executors,
        )
    except Exception as e:
        logger.error(f"LLM error: {e}")
        reply = f"エラーが発生しました: {str(e)[:200]}"

    # 会話履歴を保存
    await r.rpush(history_key, json.dumps({"role": "user", "content": user_message}))
    await r.rpush(history_key, json.dumps({"role": "assistant", "content": reply}))
    await r.ltrim(history_key, -40, -1)  # 直近20ターン分を保持
    await r.expire(history_key, 86400 * 7)  # 7日で期限切れ

    # ★恒久 owner-memory 抽出 (fire-and-forget = レイテンシ非影響、task 参照保持は spawn 側)
    _om.spawn_post_turn(http, user_message, reply, LITELLM_URL, LITELLM_KEY)

    return reply


def select_model(message: str) -> str:
    """
    モデル選択。デフォルト "smart" (Claude Opus)。
    `!fast` プレフィクスで軽量モデルを強制する場合は "fast" を返す。
    """
    msg = (message or "").lower().lstrip()
    if msg.startswith("!fast") or msg.startswith("!f "):
        return "fast"
    return "smart"


# ─── Webhook エンドポイント ───
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    data = json.loads(body)

    # ★2026-05-24 海山指示「silent skip は意図的に作ってない」: webhook 受信したが
    # message event を 1 件も処理せず終わったら bot_events に warning log。
    # bot_uptime_monitor が拾って LINE Push + auto-remediation 可能化。
    n_message_events = 0
    n_text_events_processed = 0
    last_user_id = ""

    for event in data.get("events", []):
        if event.get("type") == "message":
            n_message_events += 1
            last_user_id = event.get("source", {}).get("userId", "unknown")

        # ─── ★2026-07-10 (世界基準評価 #1): 個人 bot は海山専用 = fail-closed admin gate ───
        # 署名検証は「LINE からの呼び出し」を証明するだけで「相手が海山」は証明しない。
        # この webhook は run_agent (private wiki / interview/ / Gmail / Drive 到達)・
        # wiki 更新・自己改善 fix ループへの入口で、QR で誰でも友だち追加できる個人 OA。
        # 非 admin の message/postback は処理前に遮断 (第三者の poisoning / prompt injection 防止)。
        # 社員向けクローンは別 webhook (lineworks_webhook) なので本 gate の影響外。
        if event.get("type") in ("message", "postback"):
            _ev_uid = event.get("source", {}).get("userId", "")
            if not is_admin(_ev_uid):
                _rt = event.get("replyToken", "")
                # text のみ 1 行返す (media/postback は無音 drop、情報漏洩・engagement 回避)
                if _rt and event.get("type") == "message" \
                        and event.get("message", {}).get("type") == "text":
                    try:
                        await reply_message(
                            request.app.state.http, _rt,
                            "この AI は海山さん個人用です。社内での質問は LINE WORKS の"
                            "「うみやまAI」からどうぞ。",
                        )
                    except Exception:
                        pass
                # silent-skip 誤検知回避 (= 正しく遮断した = 処理済み) + 観測用 event
                n_text_events_processed += 1
                try:
                    from scripts.bot_events import log_bot_event  # type: ignore
                    log_bot_event("webhook", "non_admin_rejected",
                                  user_id=(_ev_uid or "")[:16],
                                  ev_type=event.get("type", ""))
                except Exception:
                    pass
                continue

        # ─── Postback: Quick Reply ボタンの処理 ───
        if event["type"] == "postback":
            user_id = event["source"].get("userId", "unknown")
            reply_token = event.get("replyToken", "")
            pb_data = event["postback"].get("data", "")
            # claude=approve|revise|cancel 系
            if pb_data.startswith("claude="):
                await _handle_claude_postback(
                    request.app, user_id, reply_token, pb_data
                )
                continue
            # purpose=wiki|fix|chat 系
            await _handle_purpose_postback(request.app, user_id, reply_token, pb_data)
            continue

        if event["type"] != "message":
            continue

        msg_type = event["message"].get("type")
        user_id = event["source"].get("userId", "unknown")
        reply_token = event.get("replyToken", "")

        # ─── 画像 / ファイル / 音声 / 動画 → Wiki 自動取り込み ───
        if msg_type in ("image", "file", "audio", "video"):
            logger.info(f"[{user_id[:8]}] media: {msg_type}")
            try:
                await _handle_line_media(request.app, event, user_id, reply_token)
            except Exception as e:
                logger.exception(f"LINE media ingest failed: {e}")
                try:
                    await reply_message(
                        request.app.state.http, reply_token,
                        f"⚠️ メディア処理でエラー: {e}",
                    )
                except Exception:
                    pass
            continue

        if msg_type != "text":
            # sticker, location 等は現状スキップ
            continue

        user_message = event["message"]["text"]

        logger.info(f"[{user_id[:8]}] {user_message[:50]}")

        # ─── Claude Code 修正指示モード中なら、今回のテキストを revision として扱う ───
        revise_raw = await request.app.state.redis.get(
            f"claude_revise:{user_id}"
        )
        if revise_raw:
            try:
                revise_data = json.loads(revise_raw)
            except Exception:
                revise_data = {}
            # 状態を消費
            await request.app.state.redis.delete(f"claude_revise:{user_id}")

            original_instruction = revise_data.get("original_instruction", "")
            previous_plan = revise_data.get("previous_plan", "")
            # 新しい指示を元の指示にマージし、前回プランの情報も文脈として渡す
            merged_instruction = (
                f"{original_instruction}\n\n"
                f"[ユーザーからの修正指示]\n{user_message}\n\n"
                f"[前回提案した計画（修正前）]\n{previous_plan[:3000]}"
            )
            try:
                task_path = _queue_claude_task(
                    user_id, merged_instruction,
                    source="line_claude_revise", mode="plan",
                    parent_task_id=revise_data.get("task_id", ""),
                )
            except Exception as e:
                logger.exception("queue revised plan task failed")
                await reply_message(
                    request.app.state.http, reply_token,
                    f"⚠️ 修正タスク登録失敗: {e}"
                )
                continue
            await reply_message(
                request.app.state.http, reply_token,
                f"✏️ 修正指示を反映して計画を再作成中: {task_path.name}\n"
                f"完了したら push で新しい計画を送ります。"
            )
            continue

        # ─── /claude — Claude Code に調査・計画依頼 (plan モード) ───
        if user_message.startswith("/claude ") or user_message.strip() == "/claude":
            # ★2026-05-23 LEE §3.2: admin gate (fail-closed)
            if not is_admin(user_id):
                await reply_message(request.app.state.http, reply_token, reject_message())
                continue
            instruction = user_message[len("/claude"):].strip()
            if not instruction:
                await reply_message(
                    request.app.state.http, reply_token,
                    "使い方: /claude <指示>\n"
                    "例: /claude main.py の webhook に X を追加して\n\n"
                    "→ まず Claude Code が計画を返します。内容を確認してから承認 or 修正を選べます。"
                )
                continue
            try:
                task_path = _queue_claude_task(
                    user_id, instruction,
                    source="line_claude", mode="plan",
                )
            except Exception as e:
                logger.exception("queue claude task failed")
                await reply_message(
                    request.app.state.http, reply_token,
                    f"⚠️ タスク登録に失敗: {e}"
                )
                continue
            preview = instruction.replace("\n", " ")[:80]
            await reply_message(
                request.app.state.http, reply_token,
                f"📥 Claude Code に計画依頼\n「{preview}…」\n\n"
                f"調査と変更案を出したら push で送ります（通常 30秒〜2分）。\n"
                f"内容確認後に「✅ 実行」で実装へ進みます。"
            )
            continue

        # ─── ★2026-05-24 Feature 3/4: 海山 daily audit UI (1-click verdict) ───
        # 海山が個人 LINE で直前 bot 応答に対し ○ / × / ! を送ると即 audit 記録。
        # 「/audit-recent」「/audit-stats」は brain_commands.py で受ける (= /-prefix 経由)。
        if is_admin(user_id):
            try:
                import clone_audit
                _verdict_parsed = clone_audit.parse_verdict_prefix(user_message)
            except Exception as e:
                logger.warning(f"audit parse failed: {e}")
                _verdict_parsed = None

            if _verdict_parsed is not None:
                _verdict, _rest = _verdict_parsed
                _rest = (_rest or "").strip()
                # _rest が空 or 単純 note → 直前 bot 応答対象
                # _rest が "数字" or "数字 note" → /audit-recent 一覧 index 指定
                _index = None
                _note = _rest
                if _rest:
                    _parts = _rest.split(maxsplit=1)
                    if _parts[0].isdigit():
                        _index = int(_parts[0])
                        _note = _parts[1] if len(_parts) > 1 else ""

                try:
                    if _index is not None:
                        # index 指定: list_recent_unrated から該当 item を audit
                        _candidates = clone_audit.list_recent_unrated(limit=20)
                        if _index < 1 or _index > len(_candidates):
                            await reply_message(
                                request.app.state.http, reply_token,
                                f"⚠️ index {_index} は範囲外 (1-{len(_candidates)})。/audit-recent で再表示。"
                            )
                            continue
                        _item = _candidates[_index - 1]
                        clone_audit.record_audit(
                            audited_by=user_id,
                            target_user_id=_item["user_id"],
                            user_query=_item["user_query"],
                            bot_response=_item["bot_response"],
                            verdict=_verdict,
                            note=_note,
                            target_channel_id=_item.get("channel_id"),
                            ts_target=_item["ts"],
                        )
                        await reply_message(
                            request.app.state.http, reply_token,
                            f"✓ #{_index} audit: verdict={_verdict}"
                            + (f", note={_note[:40]}" if _note else "")
                        )
                        continue
                    else:
                        # index 無し: 海山個人 LINE history の直前 bot 応答を audit
                        import clone_history
                        _recent = clone_history.load_recent(user_id, n=10)
                        _last_bot = None
                        _last_user_q = ""
                        for _r in reversed(_recent):
                            if _r["role"] == "assistant" and _last_bot is None:
                                _last_bot = _r["content"]
                            elif _r["role"] == "user" and _last_bot is not None:
                                _last_user_q = _r["content"]
                                break
                        if _last_bot is None:
                            await reply_message(
                                request.app.state.http, reply_token,
                                "⚠️ audit 対象の bot 応答が見つからない。先に /clone-public で test するか、/audit-recent で他 user の応答を選んでください。"
                            )
                            continue
                        clone_audit.record_audit(
                            audited_by=user_id,
                            target_user_id=user_id,
                            user_query=_last_user_q,
                            bot_response=_last_bot,
                            verdict=_verdict,
                            note=_note,
                        )
                        await reply_message(
                            request.app.state.http, reply_token,
                            f"✓ audit 記録: verdict={_verdict}"
                            + (f", note={_note[:40]}" if _note else "")
                            + f"\n対象: {_last_bot[:60]}..."
                        )
                        continue
                except Exception as e:
                    logger.exception(f"audit record failed: {e}")
                    await reply_message(
                        request.app.state.http, reply_token,
                        f"⚠️ audit 記録失敗: {e}"
                    )
                    continue

        # ─── Brain Wiki コマンド ───
        # ★2026-05-24 海山指示: 「気づき」「メモ」「note」「アイデア」「思いつき」開始の
        # prefix 無しメッセージを /memo の alias として自動扱い (= silent skip 防止)。
        # 海山「silent skip は意図的に作ってない」を反映、independent memo 文化を救済。
        _MEMO_ALIAS_PREFIXES = ("気づき", "メモ", "アイデア", "思いつき", "note", "Note", "NOTE", "覚書")
        _msg_stripped = user_message.lstrip()
        _is_memo_alias = (
            is_admin(user_id)
            and any(_msg_stripped.startswith(p) for p in _MEMO_ALIAS_PREFIXES)
            and not _msg_stripped.startswith("/")
        )
        if _is_memo_alias:
            # /memo {content} 形式に変換 (= prefix 部分も保持して content にする)
            user_message = "/memo " + _msg_stripped

        if user_message.startswith(("/brain", "/teach", "/memo", "/clone", "/lint", "/dedup", "/graph", "/wiki", "/forward", "/align", "/line-", "/audit", "/research", "/personal", "/reflux", "/bridge", "/diary", "/help", "/drive")):
            # ★2026-05-23 LEE §3.2: admin gate (fail-closed)
            if not is_admin(user_id):
                await reply_message(request.app.state.http, reply_token, reject_message())
                continue
            handled = await handle_brain_commands(
                request.app, user_id, user_message, reply_token
            )
            if handled:
                continue

        # ─── アライメント回答チェック（質問待ち状態なら回答として処理） ───
        aligned = await handle_alignment_answer(request.app, user_id, user_message)
        if aligned:
            await reply_message(
                request.app.state.http, reply_token,
                "回答をWikiに反映しました。\n次の質問は /align か、明日の通知で届きます。"
            )
            continue

        # ─── Privacy Gate コマンド ───
        if user_message.startswith(("/filter", "/block", "/unblock", "/quarantine")):
            # ★2026-05-23 LEE §3.2: admin gate (fail-closed)
            if not is_admin(user_id):
                await reply_message(request.app.state.http, reply_token, reject_message())
                continue
            response = await request.app.state.privacy.handle_command(user_message)
            if response:
                await reply_message(request.app.state.http, reply_token, response)
                continue

        # ─── 特殊コマンド ───
        if user_message.strip() == "/reset":
            # ★2026-05-23 LEE §3.2: admin gate (fail-closed)
            if not is_admin(user_id):
                await reply_message(request.app.state.http, reply_token, reject_message())
                continue
            await request.app.state.redis.delete(f"chat:{user_id}")
            await reply_message(
                request.app.state.http, reply_token, "会話履歴をリセットしました。"
            )
            continue

        if user_message.strip() == "/status":
            await reply_message(
                request.app.state.http, reply_token, "✅ Agent + Brain Wiki 稼働中"
            )
            continue

        # ─── NotebookLM URL があれば先に取り込み（Quick Reply より優先） ───
        if find_notebooklm_urls(user_message):
            try:
                handled = await _handle_notebooklm_urls(
                    request.app, user_message, user_id, reply_token
                )
                if handled:
                    continue
            except Exception as e:
                logger.exception(f"NotebookLM handler failed: {e}")
                try:
                    await reply_message(
                        request.app.state.http, reply_token,
                        f"⚠️ NotebookLM 取り込みでエラー: {e}",
                    )
                except Exception:
                    pass
                continue

        # ─── エージェント実行 (★2026-07-20 Umiyama AI Agent 正式化: 毎メッセージの
        # 目的選択 Quick Reply を廃止しデフォルト=会話に。海山「無用な通知等は極力なくす」。
        # Wiki 即時取込は /teach //memo、システム修正は /claude の明示コマンドへ。
        # 背景 ingest (_safe_ingest) は全会話で従来どおり走る = ノート投げ込みも wiki に届く) ───
        reply = await run_agent(
            request.app,
            request.app.state.http,
            request.app.state.redis,
            user_id,
            user_message,
        )
        await reply_message(request.app.state.http, reply_token, reply)
        n_text_events_processed += 1

        # ─── バックグラウンド学習 + 不満足応答の自動改善 (旧 purpose=chat と同等) ───
        asyncio.create_task(_safe_ingest(request.app, user_id, user_message, reply))
        asyncio.create_task(
            _auto_improve_if_unsatisfactory(request.app, "line_chat", user_id, user_message, reply)
        )

    # ★2026-05-24 海山指示 silent skip detection: message event 受信したが
    # text event を 1 件も応答処理しなかった場合 → silent skip 疑い event log。
    # bot_uptime_monitor が拾って LINE Push + auto-remediation candidate。
    # 既存応答経路 (/memo / /align / Quick Reply / 画像) は continue で抜けるので
    # n_text_events_processed = 0 でも問題ないが、n_message_events > 0 の時は記録する。
    if n_message_events > 0:
        try:
            from scripts.bot_events import log_bot_event  # type: ignore
            log_bot_event(
                "webhook", "webhook_processed",
                n_message_events=n_message_events,
                n_text_events_processed=n_text_events_processed,
                user_id=last_user_id[:16],
                potential_silent_skip=(n_text_events_processed == 0 and n_message_events > 0),
            )
        except Exception:
            pass

    return {"status": "ok"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# うみやまAI — LINE Works Bot Webhook
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def _run_feedback_backcheck(brain: BrainWiki, rec: dict) -> None:
    """修正希望に対して Wiki / 資料バックチェックを実行し、レコードに結果を付与。

    海山の daily digest に届く前にこれが走ることで、verdict 付きで見られる。
    """
    try:
        result = await brain.backcheck_feedback(
            trigger_msg=rec.get("trigger_msg", ""),
            response=rec.get("response", ""),
            feedback=rec.get("feedback", ""),
        )
        result["timestamp"] = datetime.now(JST).isoformat(timespec="seconds")
        clone_feedback.attach_backcheck(rec["id"], result)
        logger.info(
            f"clone_feedback backcheck done id={rec['id']} verdict={result.get('verdict')}"
        )
    except Exception as e:
        logger.warning(f"clone_feedback backcheck failed id={rec.get('id')}: {e}")




@app.post("/webhook/lineworks")
async def lineworks_webhook(request: Request, bg_tasks: BackgroundTasks):
    """LINE Works Bot の webhook 受信。即 ACK して非同期で処理。"""
    body = await request.body()
    sig = request.headers.get("X-WORKS-Signature", "")

    if not lineworks_bot.verify_signature(body, sig):
        logger.warning("LINE Works signature invalid")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(body)
    except Exception as e:
        logger.warning(f"LINE Works payload parse fail: {e}")
        raise HTTPException(status_code=400, detail="invalid payload")

    parsed = lineworks_bot.parse_webhook(payload)
    if not parsed or parsed["type"] not in ("text", "file", "image", "postback", "video", "audio"):
        # スタンプ / 位置情報 等は ACK のみ
        # ★2026-05-26 海山指示: Drive 検索 button の postback も受信対象に追加
        # ★2026-05-27 海山指示: video / audio は parse_webhook で受信認識、handler で
        #   「未対応」 を明示返信する path を追加 (= silent drop しない)
        return {"status": "ok"}

    # ★2026-06-10: webhook 到着を計装 (= 応答が必要なメッセージが receiver に届いた印)。
    # bot_uptime_monitor が「到着あり & turn ゼロ = 真の receiver 詰まり」と「到着ゼロ = 単なる無traffic」
    # を区別するための signal。これが無いと健康な bot を閑散時に毎 30 分 restart する flapping が起きていた。
    try:
        # ★2026-06-10: import 漏れ修正。この scope には log_bot_event の import が無く、
        #   毎 webhook NameError → except で握りつぶし → webhooks_in が常に 0 →
        #   bot_uptime_monitor の webhook_silent 検知 (is_silent=started0&webhooks_in>0) が
        #   永久 False = 本来直したい flapping 対策の前提計装が dead だった。
        from scripts.bot_events import log_bot_event  # type: ignore
        log_bot_event("lineworks", "webhook_received", msg_type=parsed.get("type", "?"))
    except Exception:
        pass  # 計装失敗で webhook 処理を止めない

    # ★fix 2026-05-25 MUST-FIX M-4: safety wrapper 経由で exception silent fail 防止
    bg_tasks.add_task(_lineworks_message_with_safety, request.app, parsed)
    return {"status": "ok"}


async def _handle_lineworks_group_message(
    app, brain, http, user_id: str, channel_id: str, text: str,
    user_display: Optional[str] = None,
) -> None:
    """★2026-05-24 Tier 0: LINE WORKS group/channel 内発言の処理。

    Policy v0:
      - @mention 無し: silent listen (= clone_history append + group context 更新のみ、reply 送らない)
      - @mention あり: 通常 reply flow + reply は send_channel_text 経由で channel に送信

    1:1 DM path (= _handle_lineworks_message の既存 flow) とは別 path で完結、
    既存挙動には一切影響しない。

    Privacy 境界:
      - per-user memory (= clone_memory) は user_id 内で完結
      - per-group context (= clone_group_context) は channel_id 内で完結
      - cross-leak は update_group_context の prompt で明示制約
    """
    # ★Tier 0: mention 判定は raw text (= <m> tag 含む) に対して実行
    # 判定後に tag strip して clone_history / LLM に渡す
    mentioned = lineworks_bot.is_mentioned(text)
    clean_text = lineworks_bot.strip_mention_tags(text)

    # 初回 group webhook で raw text を debug 用に log
    # (= 実 mention format 確認用、is_mentioned() fine-tune に使う)
    if "<m " in text.lower() or text != clean_text:
        logger.info(
            f"[lineworks-group-debug] raw text with mention tags: "
            f"channel={channel_id[:8]} user={user_id[:8]} "
            f"raw={text[:200]!r} clean={clean_text[:200]!r} "
            f"mentioned={mentioned}"
        )

    # silent listen 判定 (mention 無し)
    if not mentioned:
        # silent listen mode: history + group context 更新だけ、reply 送らない
        logger.info(
            f"[lineworks-group] silent listen: channel={channel_id[:8]} "
            f"user={user_id[:8]} text={clean_text[:50]!r}"
        )
        try:
            # clean_text を保存 (= tag 抜き、後続 LLM 注入時にクリーン)
            clone_history.append(
                user_id, "user", clean_text,
                user_display=user_display, channel_id=channel_id,
            )
        except Exception as e:
            logger.warning(f"clone_history.append failed (silent listen): {e}")
        # group context は silent listen でも更新 (= 文脈蓄積、ただし bot response は空)
        # ※ bot 応答が無い turn なので update_group_context は呼ばない (= LLM call 節約)
        # → 後続 mention turn で bot 応答時にまとめて context 化される設計
        return

    # mention あり: 通常 reply flow
    logger.info(
        f"[lineworks-group] mention detected: channel={channel_id[:8]} "
        f"user={user_id[:8]} text={clean_text[:80]!r}"
    )

    try:
        # ユーザ発言を history に保存 (= clean_text、tag 抜き)
        clone_history.append(
            user_id, "user", clean_text,
            user_display=user_display, channel_id=channel_id,
        )

        # ★2026-05-26 海山指示: group でも継続 signal を capture
        asyncio.create_task(
            _maybe_capture_conversation_continuation(
                user_id, clean_text, channel_id=channel_id,
            )
        )

        # 直近 20 件、group 内のみで filter (= scope='channel')
        # 異 group の発言が混ざらないよう scope 限定 (privacy + 文脈鮮度)
        history = clone_history.load_recent(
            user_id, n=21, channel_id=channel_id, scope="channel",
        )[:-1]

        # ★2026-05-26 海山指示: group でも Drive 明示 intent のみ Drive AI 検索 route。
        # 通常 mention reply は clone_respond のみ (= 通常会話に Drive 汚染しない)。
        # ★2026-06-20 世界基準評価 ④: admin(海山)限定に gate。非admin が Drive を引くと
        #   confidential な filename / owner 名が外部 Gemini へ egress + group へ過共有されるため
        #   (security 評価 RISK2)。非admin は Drive を引かず通常 clone_respond へフォールスルー。
        if _has_drive_intent(clean_text):
            from services.auth import is_lw_admin as _is_lw_admin
            if _is_lw_admin(user_id):
                handled = await _handle_drive_intent_query(
                    http, user_id, clean_text,
                    via_channel=True, channel_id=channel_id,
                )
                if handled:
                    return  # ★clone_respond skip

        prod_model = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
        # query は clean_text を渡す (= <m> tag が LLM context に漏れないように)
        # ★fix 2026-05-25 MUST-FIX M-4+M-5: _safe_clone_respond で Semaphore + 空 guard
        reply = await _safe_clone_respond(
            brain, clean_text, history=history, model=prod_model,
            user_id=user_id,
            user_display=user_display,
            channel_id=channel_id,
        )

        # assistant 応答を history に保存 (channel_id 付き)
        clone_history.append(
            user_id, "assistant", reply,
            user_display=user_display, channel_id=channel_id,
        )

        # channel 経由で送信 (= 1:1 send_text ではなく send_channel_text)
        await lineworks_bot.send_channel_text(http, channel_id, reply)

        # ★2026-05-26 海山指示 (= 再修正): group でも 「データ無い」 時に Drive 検索 ★提案
        # (= text 1 行案内、button_template 非対応のため)。user 明示 trigger でのみ execute。
        asyncio.create_task(
            _maybe_offer_drive_search(
                http, user_id, clean_text, reply,
                via_channel=True, channel_id=channel_id,
            )
        )

        # background: per-user memory 更新 (= 既存と同じ、clean_text を使う)
        if os.getenv("CLONE_MEMORY_ENABLED", "1") != "0":
            asyncio.create_task(
                brain.update_clone_memory(
                    user_id=user_id,
                    user_query=clean_text,
                    bot_response=reply,
                    user_display=user_display,
                )
            )

        # background: per-group context 更新 (★Tier 0 新規、clean_text を使う)
        if os.getenv("CLONE_GROUP_CONTEXT_ENABLED", "1") != "0":
            asyncio.create_task(
                brain.update_group_context(
                    channel_id=channel_id,
                    user_id=user_id,
                    user_query=clean_text,
                    bot_response=reply,
                    user_display=user_display,
                )
            )

        # background: sleep_time_agent は per-user 単位 (= 既存と同じ)
        # group 用 idle 整理は Tier 1 で別途検討
        if os.getenv("CLONE_SLEEP_TIME_ENABLED", "1") != "0":
            try:
                from scripts.clone_sleep_time_agent import schedule_sleep_time_agent
                asyncio.create_task(schedule_sleep_time_agent(user_id))
            except Exception as e:
                logger.warning(f"sleep_time_agent schedule failed (group): {e}")

    except Exception as e:
        logger.exception(f"[lineworks-group] handler failed: {e}")


# ─── ★fix 2026-05-25 MUST-FIX M-4 + M-5: 安全網 helpers ───────
# M-5: 200 人同時 DM 想定で LLM 呼出を app-wide で concurrency cap。
#      env LLM_CONCURRENCY_LIMIT (default 8)、超過分は async queue 待ち。
#      LiteLLM upstream の rate limit 緩和、429 cascade 防止。
_LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY_LIMIT", "8"))
_llm_semaphore = asyncio.Semaphore(_LLM_CONCURRENCY)


async def _safe_clone_respond(brain, text: str, **kwargs) -> str:
    """clone_respond_public のラッパー: Semaphore + exception 包み + empty guard。

    ★fix 2026-05-25 MUST-FIX M-4: exception で空応答 / 例外漏れ → user 永遠待ち
    の silent fail を防ぐ。LLM が "" を返したケースもフォールバック text に変換。
    ★fix 2026-05-25 MUST-FIX M-5: _llm_semaphore で同時 LLM 呼出 cap (default 8)。
    """
    # 施設/商圏 個別照会 (空白地DB) は clone_respond_public 内で自前検出 (§1.12b、2026-07-11)
    try:
        async with _llm_semaphore:
            reply = await brain.clone_respond_public(text, **kwargs)
    except Exception as e:
        logger.exception(f"[safe_clone_respond] failed for query={text[:60]!r}: {e}")
        reply = ""
    if not (reply or "").strip():
        logger.warning(f"[safe_clone_respond] EMPTY reply for query={text[:60]!r}")
        reply = (
            "うまく言葉が出なかった... もう一度送ってもらえる?\n"
            "(別の言い回しでも OK、何度も続くようなら教えて)"
        )
    return reply


# ─── Drive 検索 intent 検出 (★2026-05-26 海山指示) ──────────
# Drive auto follow-up を 撤廃、user が明示的に「Drive で〜」 と言った時のみ Drive 検索.
# 理由: data クレンジング不十分、proactive 表示は質低下リスク。

# 「Drive」「ドライブ」 が出てくる + 「探」「検索」「教え」「ある」 系動詞、または「資料」 等の文脈
# overly liberal にしないため keyword 両方 必須 (= 「Drive で〜」「ドライブで〜」 形式に限定)
_DRIVE_INTENT_PATTERNS = (
    "drive で", "drive を", "drive 内", "drive 探", "drive 検索", "drive から", "drive にある",
    "driveで", "driveを", "drive内", "drive探", "drive検索", "driveから", "driveにある",
    "ドライブで", "ドライブを", "ドライブ内", "ドライブ探", "ドライブ検索", "ドライブから",
    "ドライブにある", "ドライブの中",
    "google drive", "googledrive", "google ドライブ",
)


# ─── Drive button query cache (★2026-05-26 海山指示 button 化 v3) ──────────
# LINE Works button_template の `data` field は ASCII 推奨 (= 公式 example 全 ASCII)。
# 日本語 query を直接入れると 400 reject されるため、server-side dict に保存して
# 短い ASCII ID で reference する設計に変更.
# TTL 1h、bot restart で消えるが MVP には十分。
import secrets as _secrets

_DRIVE_QUERY_CACHE: dict = {}  # {q_id: (query_str, created_unix), ...}
_DRIVE_QUERY_TTL = 3600  # 1 hour


def _stash_drive_query(query: str) -> str:
    """Drive 検索 query を一時保存 + 短い ASCII ID return.

    返り値 ID は button data に embed 可能な ASCII only (URL-safe base64)。
    """
    # cleanup 古い entry (= memory leak 防止)
    now = _time_mod.time()
    cutoff = now - _DRIVE_QUERY_TTL
    for k in list(_DRIVE_QUERY_CACHE.keys()):
        if _DRIVE_QUERY_CACHE[k][1] < cutoff:
            del _DRIVE_QUERY_CACHE[k]
    q_id = _secrets.token_urlsafe(8)  # 約 11 chars、衝突確率 negligible
    _DRIVE_QUERY_CACHE[q_id] = (query, now)
    return q_id


def _pop_drive_query(q_id: str) -> Optional[str]:
    """ID から query を取出し (consume = 一度きり、即削除).

    None なら expired / 不在 (= user に「もう一度」 と通知すべき)。
    """
    entry = _DRIVE_QUERY_CACHE.pop(q_id, None)
    if entry is None:
        return None
    query, created = entry
    if _time_mod.time() - created > _DRIVE_QUERY_TTL:
        return None
    return query


# ─── Drive 検索 ★提案 button (★2026-05-26 海山指示 = 再再復活) ──────────
# bot 「データ無い」 系応答時 / user の follow-up 「分からない?」 系で 「Drive 内も検索しますか?」
# を 1 行案内 (= proactive 表示は NG、user の意思 tap で初めて Drive 検索 execute)。
# 文言: bot reply に hit / user query に hit / 両方 OR で判定 (= recall 重視、cost 低)。
# 誤発火しても 1 行余分な案内 が出るだけで害は小さい → liberal に拾う方針。

# bot reply 側 = 「データ無い」 系 (= 探しに行く必要性を bot 自身が認めた状態)
# ★海山指示 (= liberal 拾う): 「ピンポイント数値はこっちにまだ流し込めてない」 「今後集めて少しずつ更新」
# のような暗黙的な 「持ってない」 表現も拾う。誤発火しても 1 行余分なだけ → recall 優先.
# ★2026-07-12 海山「完成した戦略分析の後に『Drive で見てみる?』は違和感」:
#   bare「分からない/わからない」は多義 (「全店平均だと何も分からない」= 分析的レトリック ≠ データ不在)
#   → トリガーから除外。データ不在を **明示** する phrase のみ残す。
_BOT_NO_DATA_PHRASES = (
    # 直接的 「データ無い」
    "データ無", "データがな", "データはな", "データない", "データはない",
    "入ってない", "入ってません", "まだ入って", "見当たらな", "見当たりませ",
    # 将来 更新予定 = 今は持ってない
    "拡充候補", "今後拡充", "拡充して", "今後反映",
    "今後集めて", "今後収集", "今後更新", "順次更新", "随時更新", "少しずつ更新",
    "流し込めて", "流し込んで", "流し込まれて", "流し込み", "取り込めて", "取り込んで",
    "補完できて", "網羅できて", "カバーできて", "整備して", "整備中",
    # 他で見て 系 = 自分には無い、他 source を指す
    "BI で", "BI か", "営業管理で", "営業管理か",
    "社内 BI", "社内BI", "営業 dashboard", "営業ダッシュボード",
    # 持ってない 系
    "確認できな", "確認できません", "把握できて", "持ってない", "持っていな",
    "Brain には", "Brain にはない", "Brain には入って",
    "こっちには", "こっちにはまだ", "こちらには",
    "手元には", "手元にはまだ", "手元にはない",
)

# ★これが応答に無ければ「データ不在」と断定しない = 長い実質回答で weak phrase が
#   偶発ヒットしても Drive 提案を出さないための強マーカー (完成回答の誤発火抑制)。
_BOT_STRONG_NODATA = (
    "データ無", "データがな", "データはな", "データない", "データはない",
    "流し込めて", "流し込んで", "取り込めて", "手元にはまだ", "手元にはない",
    "持ってない", "持っていな", "Brain には", "確認できな", "整備中",
    "入ってない", "入ってません", "まだ入って", "見当たらな",
)

# user 側 = 「探したい意思 / 不在の追認」 follow-up
_USER_FOLLOWUP_PATTERNS = (
    "分からない?", "わからない?", "分からない？", "わからない？",
    "知らない?", "知らない？",
    "他にない?", "他に無い?", "他にある?",
    "探せる?", "探して", "探せない?",
    "他に手", "別の手", "別の方法",
)


def _should_offer_drive(user_text: str, bot_reply: str) -> bool:
    """Drive 検索 ★提案 を出すか判定.

    True 条件 (= OR):
      (a) bot reply に 「データ無い」 系 phrase が含まれる
      (b) user text に 「分からない?」 系 follow-up が含まれる
    両方 False なら 出さない (= 通常会話に余計な案内出さない)。
    ★2026-07-12 海山「この手の質問 (出店/商圏) に Drive 検索の提案は不適切。Drive に答えは
    ないから。なんでもかんでも Drive 検索に誘導すれば良いということではない」:
    出店/商圏/施設 系の質問 (tenpo intent or 空白地DB が場所を検出) では提案を抑制。
    """
    if not (user_text or bot_reply):
        return False
    try:
        from scripts.tenpo import lookup_service as _tls
        from scripts.tenpo import routing as _tr
        if _tr.is_trigger(user_text or "") or _tls.detect_query(user_text or ""):
            return False  # 答えの在処は空白地DB — Drive へ誘導しない
    except Exception:
        pass
    bot_t = (bot_reply or "").lower()
    user_t = (user_text or "").lower()
    # ★2026-07-12: 長い実質回答 (= 完成した分析/戦略) は、明示的なデータ不在マーカーが
    #   無い限り Drive 提案を出さない (「何も分からない」等のレトリックでの誤発火を抑制)。
    hit = any(p.lower() in bot_t for p in _BOT_NO_DATA_PHRASES)
    if hit and len(bot_reply or "") > 350:
        if not any(p.lower() in bot_t for p in _BOT_STRONG_NODATA):
            hit = False
    if hit:
        return True
    for p in _USER_FOLLOWUP_PATTERNS:
        if p.lower() in user_t:
            return True
    return False


async def _maybe_offer_drive_search(
    http: httpx.AsyncClient,
    user_id: str,
    user_text: str,
    bot_reply: str,
    via_channel: bool = False,
    channel_id: Optional[str] = None,
) -> None:
    """bot 「データ無い」系応答 or user 「分からない?」 follow-up を検知 → Drive 検索 ★提案 を送信.

    ★2026-05-26 海山指示「文字入力じゃなくボタンにしよう。表示は TOP3」:
    - 1:1 DM: button_template (= tap で 「Drive で <query>」 が user message として自動送信)
      → 既存 _has_drive_intent path で route される (= 新 handler 不要、code path 統一)
      → button_template 非対応 client は send_button_template 側で text fallback (= 既存実装)
    - group/channel: button_template 非対応 → 既存 text 1 行案内 維持

    silent fail OK (= 副次処理、main reply に影響させない)。
    """
    # ★2026-06-21 世界基準評価: 非admin には Drive 検索 button を出さない(chokepoint gate と二重防御)。
    from services.auth import is_lw_admin as _is_lw_admin_off
    if not _is_lw_admin_off(user_id):
        return
    try:
        if not _should_offer_drive(user_text, bot_reply):
            return
        # 検索語 (postback 用に 200 char 制限)
        q = (user_text or "").strip().replace("\n", " ")[:200]
        if not q:
            return
        hint = q[:40]

        try:
            if via_channel and channel_id:
                # group は button_template 非対応 (= LINE Works 仕様)
                offer = (
                    "💡 Drive 内も検索する?\n"
                    f"→ 「Drive で {hint}」 と返信"
                )
                await lineworks_bot.send_channel_text(http, channel_id, offer)
            else:
                # 1:1 DM: button (★2026-05-27 海山指示「Drive は 2 の手、casual に」)
                # = bot の人間らしい応答が「主」、button は補助 (= option)。文言は casual.
                q_id = _stash_drive_query(q)
                await lineworks_bot.send_button_template(
                    http, user_id,
                    content_text=(
                        f"📂 Drive で改めて見てみる?\n"
                        f"(※ link は umiyama-ai 権限、開けない時は owner に申請)"
                    ),
                    buttons=[{
                        "label": "🔍 Drive 検索",
                        # ASCII only data (= LINE Works button_template 仕様準拠)
                        # postback handler で "drv:{q_id}" を見て _pop_drive_query で原 query 復元
                        "data": f"drv:{q_id}",
                    }],
                )
        except Exception as e:
            logger.warning(f"_maybe_offer_drive_search send failed: {e}")
    except Exception as e:
        # 解析中の事故 etc — main flow を絶対 break させない
        logger.warning(f"_maybe_offer_drive_search failed: {e}")


# --- 暗黙の修正検出 (module-level) ---
# ★2026-06-10: 旧版は _handle_lineworks_message 内の nested 定義だったため、module-level の
#   _maybe_capture_conversation_continuation(L2112) から呼ぶと NameError → L2118 の except で
#   握りつぶされ、会話継続 = positive-signal の記録が silent fail していた。
#   両者から見える module-level へ昇格 (依存定数も一緒に移動)。
_CORRECTION_PREFIXES = (
    "違う", "ちがう", "それ違う", "それは違う",
    "間違", "まちがい", "訂正:", "訂正。",
    "正しくは", "正確には", "事実は",
)
_CORRECTION_MARKERS = (
    "間違ってる", "事実と違う", "事実誤認", "古い情報", "データ古い",
    "誤解してる", "誤ってる", "勘違いしてる", "それ嘘",
)


def _looks_like_correction(t: str) -> bool:
    """user 発言が直前 bot 応答への訂正っぽいか判定 (module-level: 2 箇所から参照)."""
    if len(t) < 2 or len(t) > 400:
        return False
    if t.startswith(_CORRECTION_PREFIXES):
        return True
    # "それは違うよ、実際は〜" 等の短い訂正パターン
    for marker in _CORRECTION_MARKERS:
        if marker in t:
            return True
    return False


async def _maybe_capture_conversation_continuation(
    user_id: str,
    text: str,
    channel_id: Optional[str] = None,
) -> None:
    """★2026-05-26 海山指示: bot 応答後 user が続けたら positive signal として記録.

    直前の clone_history を読み、bot 応答 → 今回の user message が 30 分以内かつ
    修正でない → conversation_success に append.

    silent fail OK (= 副次処理、main reply に影響させない)。
    """
    try:
        from services import conversation_success as cs
    except Exception:
        return
    try:
        # 最新 5 件を確認 (= user-bot-user-bot-user 程度の窓)
        history = clone_history.load_recent(user_id, n=5, channel_id=channel_id)
        # _is_correction は厳密版を使う (= main.py の _looks_like_correction)
        if _looks_like_correction(text):
            return
        continuation = cs.detect_continuation(
            user_id, text, history, channel_id=channel_id,
        )
        if continuation:
            cs.record_success(
                user_id=user_id,
                channel_id=channel_id,
                user_query=continuation["user_query"],
                bot_response=continuation["bot_response"],
                continuation=continuation["continuation"],
                elapsed_seconds=continuation.get("elapsed_seconds"),
            )
    except Exception as e:
        logger.warning(f"_maybe_capture_conversation_continuation failed: {e}")


def _has_drive_intent(text: str) -> bool:
    """user message に Drive 検索 intent が明示されてるか判定.

    保守的: 「Drive / ドライブ」 keyword が必須 (= 動詞は問わない、組合せ表現を検出)。
    「資料探して」 「ファイル教えて」 単独は intent と認めない (= 質低下 risk)。
    """
    if not text:
        return False
    t = text.lower().strip()
    for pat in _DRIVE_INTENT_PATTERNS:
        if pat in t:
            return True
    return False


async def _handle_drive_intent_query(
    http: httpx.AsyncClient,
    user_id: str,
    text: str,
    via_channel: bool = False,
    channel_id: Optional[str] = None,
) -> bool:
    """Drive intent 検出時、Drive AI 検索を実行して結果を返信.

    Returns: True なら handled (= 通常 clone_respond は skip)、False なら未処理。
    """
    # ★2026-06-21 世界基準評価 (security RISK2 button-bypass fix): chokepoint で admin gate。
    #   text 経路(group/DM)は呼出側で gate 済だが、button postback(drv:)経路が未 gate で
    #   非admin が Drive 検索→外部 Gemini 流出できた。全 call site をここで一括封鎖。
    from services.auth import is_lw_admin as _is_lw_admin_drv
    if not _is_lw_admin_drv(user_id):
        return False
    # ★2026-07-13 海山「時間がかかり過ぎ」: 即時 ack (旧実測 8-9 分 → 並列化で 1 分前後。
    # それでも無応答の待ちは不安なので着手を先に伝える)
    try:
        ack = "🔎 Drive を検索中… (通常 1〜2 分)"
        if via_channel and channel_id:
            await lineworks_bot.send_channel_text(http, channel_id, ack)
        else:
            await lineworks_bot.send_text(http, user_id, ack)
    except Exception:
        pass
    try:
        from services.gemini_query import search_drive_semantic  # type: ignore
        # ★2026-05-26 海山指示「表示は TOP 3 で良い」 (= top_n 5 → 3 で簡潔に)
        result = await search_drive_semantic(text, top_n=3, apply_default_filters=True)
    except Exception as e:
        logger.warning(f"_handle_drive_intent_query failed: {e}")
        # ★cross-check DA D4: ack を出した後に False で clone 応答へ fallthrough すると
        # 「検索中…」の直後に的外れな雑談回答が来る紛らわしい UX → 正直に失敗を伝えて handled 扱い
        try:
            note = "⚠️ Drive 検索でエラーが出た。少し時間を置いてもう一度試して。"
            if via_channel and channel_id:
                await lineworks_bot.send_channel_text(http, channel_id, note)
            else:
                await lineworks_bot.send_text(http, user_id, note)
        except Exception:
            pass
        return True

    # 結果 formatting (= /drive ai と同様、揃える)
    filt = result.get("filters_applied") or {}
    if filt.get("default_filters_on"):
        filter_label = f"絞込: 過去 {filt.get('since_days', 365)} 日 / sheets+docs+slides+PDF"
    else:
        filter_label = "絞込: 全期間 + 全 type"
    kw_disp = ", ".join(result.get("keywords") or []) or "(無し)"
    gem_tag = "✓ Gemini" if result.get("via_gemini") else "△ fallback"

    if result.get("total_hits", 0) == 0:
        reply_text = (
            f"🤖 Drive AI 検索: {text}\n"
            f"keyword: {kw_disp}\n"
            f"{filter_label}\n"
            f"hit 0 件、別の言い回し or「/drive ai {text} --all」 で拡大検索を"
        )
    elif not result.get("top"):
        # ★2026-05-26 海山指示: Drive hit はあるが Gemini が意味的に合うもの 0 件と判断
        # → 表面 keyword 一致 file を出すより 「該当無し」 を正直に伝える
        sample_names = [
            ((f.get("name") or "")[:50])
            for f in (result.get("all") or [])[:3]
        ]
        sample_disp = "\n  - ".join(sample_names) if sample_names else "(none)"
        reply_text = (
            f"🤖 Drive AI 検索 ({gem_tag}): {text}\n"
            f"keyword: {kw_disp}\n"
            f"{filter_label}\n"
            f"⚠️ Drive 内に {result['total_hits']} 件 hit したが、\n"
            f"   query の意味と一致する file は見つからなかった。\n"
            f"\n   hit した file (= 意味は違う可能性):\n  - {sample_disp}\n"
            f"\n別の言い回し or 具体的な keyword で再検索を:\n"
            f"  例: 「Drive で 副業 就業規則」「Drive で 副業 申請フロー」"
        )
    else:
        lines = [
            f"🤖 Drive AI 検索 ({gem_tag}): {text}",
            f"keyword: {kw_disp}",
            f"{filter_label}",
            f"hit: {result['total_hits']} 件 → top {len(result['top'])}",
            "",
        ]
        # ★2026-07-13 海山指示「Drive検索の精度が悪い」: 制約語 (地域等) に 1 件もヒット
        # しない時は正直に明示 (= 他地域の類似資料を「答え」の顔で出さない)
        if result.get("must_terms") and result.get("must_hits", 0) == 0:
            lines.insert(4, "⚠️ 「" + "、".join(result["must_terms"])
                         + "」に直接ヒットする file は無し。以下は周辺の類似資料:")
        # ★2026-07-13 海山指示「関連したものが見つからない場合は、無理に提示する必要がない」:
        # 確度で表示を出し分け (high 無し = 「答えが載っている」と言えない候補しか無い)
        _confs = {f.get("rerank_confidence", "high") for f in result["top"]}
        if "degraded" in _confs:
            lines.insert(4, "⚠️ 意味判定 (Gemini) が一時不調。以下は機械選別 = 参考程度:")
        elif "high" not in _confs:
            lines.insert(4, "⚠️ 質問に直接答える資料は見つからなかった。近い候補のみ (参考):")
        for i, f in enumerate(result["top"], start=1):
            name = (f.get("name") or "")[:60]
            mime_raw = f.get("mimeType") or ""
            mime_short = "?"
            if "spreadsheet" in mime_raw:
                mime_short = "📊sheet"
            elif "document" in mime_raw:
                mime_short = "📄doc"
            elif "presentation" in mime_raw:
                mime_short = "🎯slide"
            elif "pdf" in mime_raw:
                mime_short = "📕pdf"
            mod = (f.get("modifiedTime") or "")[:10]
            owner = ((f.get("owners") or [{}])[0].get("displayName") or "?")[:20]
            link = f.get("webViewLink") or ""
            reason = f.get("rerank_reason") or ""
            lines.append(f"{i}. [{mime_short}] {name}")
            if reason:
                lines.append(f"   ◉ {reason}")
            lines.append(f"   owner: {owner} / 更新: {mod}")
            if link:
                lines.append(f"   {link}")
            lines.append("")
        # ★2026-05-26 海山指示: bot (umiyama-ai) と質問者で Drive 権限が違う注意喚起
        # link を tap して 「権限ありません」 になる disappointing UX を予防、
        # owner 表示済なので user は申請先が分かる (= 直接申請の導線確保)
        lines.append(
            "⚠️ link は umiyama-ai の権限で取得。あなたに権限が無い場合は "
            "owner にアクセス申請を。"
        )
        reply_text = "\n".join(lines)[:4500]

    try:
        if via_channel and channel_id:
            await lineworks_bot.send_channel_text(http, channel_id, reply_text)
        else:
            await lineworks_bot.send_text(http, user_id, reply_text)
    except Exception as e:
        logger.warning(f"_handle_drive_intent_query send failed: {e}")
        return False
    return True


async def _lineworks_message_with_safety(app, parsed: dict) -> None:
    """★fix 2026-05-25 MUST-FIX M-4: bg_tasks 入口の外側 safety wrapper。

    bg_tasks.add_task で起動された handler の exception を FastAPI が 200 OK で
    食って終わると、user は応答ゼロのまま永遠に待つ silent fail に陥る。
    必ず fallback message を送って「届いたけど一時エラー」を user に通知する。
    """
    user_id = parsed.get("user_id", "")
    try:
        await _handle_lineworks_message(app, parsed)
    except Exception as e:
        logger.exception(
            f"[lineworks-safe] handler exception user={user_id[:8] if user_id else '?'}: {e}"
        )
        if user_id:
            try:
                await lineworks_bot.send_text(
                    app.state.http,
                    user_id,
                    "🐛 一時的にうまく応答できなかった。もう一度送ってもらえる?\n"
                    "(エラーは私の側で記録済。何度も続くようなら教えて)",
                )
            except Exception as e2:
                logger.exception(f"[lineworks-safe] fallback send failed: {e2}")


async def _handle_lineworks_message(app, parsed: dict) -> None:
    """LINE Works 受信メッセージを処理 (非同期)

    type 別 routing:
      - text: 既存の clone_respond_public フロー (修正検出含む)
      - file / image: download → extract → clone_respond_public(attached_content=...)

    ★2026-05-24 Tier 0 channel-aware:
      - channel_id is None: 1:1 DM (= 既存挙動と完全同じ、後方互換)
      - channel_id あり: group/channel 内発言
        - @mention 無し → silent listen (= history + group context 更新のみ、reply 送らない)
        - @mention あり → 通常 reply flow、ただし reply は send_channel_text 経由
    """
    user_id = parsed["user_id"]
    if not user_id:
        return

    channel_id = parsed.get("channel_id")  # ★Tier 0: None = DM, str = group

    msg_type = parsed.get("type", "text")

    # ★2026-05-26 v4: button_template (type:message) tap で message event を受信、
    # parsed["postback"] に postback 値が同梱される (= LINE Works 公式仕様)。
    # text 通常 path に進む前に postback prefix を check して route 切替.
    _pb_from_msg = (parsed.get("postback") or "").strip() if msg_type == "text" else ""
    if _pb_from_msg.startswith("drv:"):
        q_id = _pb_from_msg[len("drv:"):].strip()
        query = _pop_drive_query(q_id)
        http_pb: httpx.AsyncClient = app.state.http
        if not query:
            try:
                await lineworks_bot.send_text(
                    http_pb, user_id,
                    "⏱️ button が古くなった (= 1 時間経過 or bot 再起動)。"
                    "もう一度同じ質問をしてみて。"
                )
            except Exception as e:
                logger.warning(f"drive button expired notice failed: {e}")
            return
        try:
            clone_history.append(user_id, "user", f"[button] Drive で {query}")
        except Exception as e:
            logger.warning(f"clone_history.append postback (msg-embed) failed: {e}")
        await _handle_drive_intent_query(
            http_pb, user_id, query, via_channel=False,
        )
        return

    # ★2026-07-10 (世界基準評価 S3): 👍👎 rating postback (button_template = message event 同梱)。
    #   ロジックは services/feedback_prompt.py (CLAUDE.md §1.12b: main.py は wiring のみ)。
    if _pb_from_msg.startswith("clonefb:"):
        from services import feedback_prompt as _fbp
        # ★fix 2026-07-12: user_display はこの時点で未代入 (後段 L2763 で parsed から取る) —
        #   UnboundLocalError で 👍 tap がエラー返しになる実バグ (機能 OFF 期間の潜在、初 tap で発火)
        await _fbp.handle_rating_postback(
            app.state.http, user_id, _pb_from_msg,
            user_display=parsed.get("user_display") or parsed.get("display_name"))
        return

    # ★2026-05-26 海山指示 Drive button: postback event → ASCII data 経由で
    # _handle_drive_intent_query 起動 (★v3: data ASCII 化 + server-side cache reference)
    # ※ button_template の場合は message event 内 postback で来るので 上の path 経由
    #   下記 postback event path は カルーセル/クイックリプライ/リッチメニュー用の予備
    if msg_type == "postback":
        pb_data = parsed.get("data", "") or ""
        # ★v3: "drv:{q_id}" で server cache から query 復元 (= ASCII data 仕様準拠)
        if pb_data.startswith("drv:"):
            q_id = pb_data[len("drv:"):].strip()
            query = _pop_drive_query(q_id)
            http_pb: httpx.AsyncClient = app.state.http
            if not query:
                # expired (= 1h 超え or bot restart で消失) → user に通知
                try:
                    await lineworks_bot.send_text(
                        http_pb, user_id,
                        "⏱️ button が古くなった (= 1 時間経過 or bot 再起動)。"
                        "もう一度同じ質問をしてみて。"
                    )
                except Exception as e:
                    logger.warning(f"drive button expired notice failed: {e}")
                return
            # history append (= user 発話と同等扱い、後の memory 整合性)
            try:
                clone_history.append(user_id, "user", f"[button] Drive で {query}")
            except Exception as e:
                logger.warning(f"clone_history.append postback failed: {e}")
            await _handle_drive_intent_query(
                http_pb, user_id, query, via_channel=False,
            )
            return
        # 旧 schema DRIVE_SEARCH: (= ASCII 違反で reject されてた、念のため後方互換)
        if pb_data.startswith("DRIVE_SEARCH:"):
            query = pb_data[len("DRIVE_SEARCH:"):].strip()
            if query:
                http_pb = app.state.http
                await _handle_drive_intent_query(http_pb, user_id, query, via_channel=False)
            return
        # 未知 prefix は silent ACK (= 将来 button 追加用 reserved)
        logger.info(f"[lineworks-postback] unknown prefix data={pb_data[:60]!r}")
        return

    if msg_type in ("file", "image"):
        # 添付 path: 現状は DM 前提、group は ★Tier 0 では未対応 (= silent skip + log)
        if channel_id:
            logger.info(
                f"[lineworks-group] attachment from group {channel_id[:8]} "
                f"user={user_id[:8]} skipped (Tier 0 では DM のみ対応)"
            )
            return
        await _handle_lineworks_attachment(app, parsed)
        return

    # ★2026-05-27 海山指示「動画対応 進めて」: video → ffmpeg frame 抽出 + Vision 分析 →
    # clone_respond_public に渡す本実装. audio は未だ未実装 (= 「次は音声トレーニング
    # した海山音声で音声会話対応」 後続 task) → 引き続き notice 返信.
    if msg_type == "video":
        if channel_id:
            logger.info(
                f"[lineworks-group] video from group {channel_id[:8]} "
                f"user={user_id[:8]} skipped (Tier 0 DM only)"
            )
            return
        await _handle_lineworks_video(app, parsed)
        return

    if msg_type == "audio":
        if channel_id:
            logger.info(
                f"[lineworks-group] audio from group {channel_id[:8]} "
                f"user={user_id[:8]} skipped"
            )
            return
        # ★2026-05-27 海山指示「音声会話対応 (= Mac Studio で作業)」 の input scaffold.
        # flag AUDIO_TRANSCRIBE_ENABLED で gate (= default OFF、safer rollout).
        # 海山が litellm_config の whisper model + docker compose restart litellm + 本 flag ON で
        # 有効化、その後 LINE Works で音声送信 → Whisper 書き起こし → clone_respond で reply.
        # Output TTS (= 海山音声 clone) は Mac Studio で別途 ElevenLabs 統合予定.
        if os.getenv("AUDIO_TRANSCRIBE_ENABLED", "0") == "1":
            await _handle_lineworks_audio(app, parsed)
        else:
            try:
                http_a: httpx.AsyncClient = app.state.http
                await lineworks_bot.send_text(
                    http_a, user_id,
                    "🎤 音声ありがとう。書き起こし機能はまだ有効化前 (= Mac Studio 設定待ち)。\n"
                    "今は内容を text で送ってもらえれば そのまま処理可能。"
                )
            except Exception as e:
                logger.warning(f"audio notice send failed: {e}")
        return

    text = parsed.get("text", "").strip()
    if not text:
        return

    brain: BrainWiki = app.state.brain
    http: httpx.AsyncClient = app.state.http

    # ★Tier 0: group 経路に分岐 (= channel_id があれば group 処理)
    if channel_id:
        await _handle_lineworks_group_message(
            app, brain, http, user_id, channel_id, text,
            user_display=parsed.get("user_display") or parsed.get("display_name"),
        )
        return

    # ★2026-06-28 海山指示: /personal — 非OWNDAYS の個人 PJ/投資 (Example Garden 等) を
    #   personal ドメインだけで答える admin 専用モード。OWNDAYS クローン/コンサル/アナリストとは
    #   別系統で混線しない。DM のみ (group は上で return 済) + is_lw_admin gate。
    #   clone_history には保存しない (= personal を公開クローンの history 文脈に混ぜない leak 防止)。
    from services.auth import is_lw_admin as _is_lw_admin_personal
    if text.strip().startswith("/personal") and _is_lw_admin_personal(user_id):
        arg = text.strip()[len("/personal"):].strip()
        try:
            preply = await brain.personal_command(arg)
        except Exception as e:
            preply = f"/personal エラー: {e}"
        await lineworks_bot.send_text(http, user_id, preply)
        return  # ★OWNDAYS clone_respond + clone_history を bypass

    # ★2026-06-28 /reflux — 還流 (各PJ→Core 蒸留) の一覧/承認/却下。admin 限定・DM のみ。
    #   蒸留 (LLM) は cron 専任。ここは list/ok/ng のみ (Core 書込は海山の承認時のみ = 不変条件)。
    if text.strip().startswith("/reflux") and _is_lw_admin_personal(user_id):
        arg = text.strip()[len("/reflux"):].strip()
        try:
            from scripts import reflux as _reflux
            rmsg = _reflux.handle_command(arg)
        except Exception as e:
            rmsg = f"/reflux エラー: {e}"
        await lineworks_bot.send_text(http, user_id, rmsg)
        return

    # --- 修正フィードバック系コマンド ---
    FIX_TRIGGERS = {"/fix", "修正希望", "/修正希望", "✏️ 修正希望あり"}
    # 暗黙の修正検出ロジックは module-level _looks_like_correction() へ移動済
    # (★2026-06-10 NameError 修正。下記 (B2) と _maybe_capture_conversation_continuation の両方から参照)。

    try:
        # ★2026-06-20 /戦略・/分析: admin の戦略/分析クエリをエージェントに委譲。
        #   戦略アナリスト(consultant)= 構造化+提言 / アナリスト(analyst)= 定量計算。
        #   DM のみ・is_lw_admin 限定。単一判定点 classify() で consultant|analyst|none に振る
        #   (1 メッセージで二重発火しない / cross-check Reviewer)。即 ack + queue へ enqueue
        #   (bot は docker 非接触・persona 経由せず)。ホスト cron が実行し結果を push。
        #   例外時は fall-through (通常フロー)= 安全側。ADR §11 / 2026-06-20-strategy-analyst-agent.md。
        # ★2026-07-11 /出店: 出店候補 lane (2タイプ提案)。classify() より先 = consultant の
        #   「出店(戦略|方針|判断)」regex と二重発火しない。ロジックは scripts/tenpo/hook.py (§1.12b)。
        from scripts.tenpo import hook as _tenpo_hook
        if await _tenpo_hook.try_handle(
                text, user_id, channel_id,
                lambda _t: lineworks_bot.send_text(http, user_id, _t)):
            return

        if channel_id is None:
            try:
                from scripts.consultant import routing as _croute
                from scripts.analyst import queue as _aqueue
                from services.auth import is_lw_admin as _is_lw_admin
                _lane, _clean = _croute.classify(text, is_admin=_is_lw_admin(user_id))
            except Exception as _e:
                logger.warning(f"[agent-route] routing skip: {_e}")
                _lane, _clean = "none", text
            if _lane in ("consultant", "analyst"):
                _root = _croute.QUEUE_ROOT if _lane == "consultant" else _aqueue.DEFAULT_ROOT
                try:
                    _ok, _info = _aqueue.enqueue(_clean, user_id, root=_root)
                except Exception as _e:
                    logger.warning(f"[agent-route] enqueue fail: {_e}")
                    _ok, _info = False, "error"
                if _ok:
                    _ack = ("🧭 戦略の検討を始めます。社内データと分析を実際に当たって整理するので数分かかります。"
                            "終わったら意思決定メモを送ります。") if _lane == "consultant" else \
                           ("📊 分析を始めます。データを実際に実行して確かめるので数分かかります。"
                            "終わったら結果とグラフを送ります。")
                    await lineworks_bot.send_text(http, user_id, _ack)
                else:
                    _r = {"duplicate": "同じ依頼を処理中です。", "busy": "前の依頼がまだ実行中です。少し待ってね。",
                          "daily_cap": "今日の上限に達しました。明日また。", "empty": "内容を書いてね。"}
                    await lineworks_bot.send_text(
                        http, user_id,
                        _r.get(_info.split(":")[0], f"いま受けられません({_info})。少し待ってね。"))
                return

        # (A) 修正内容待ち状態だった場合: 今回の発言 = 修正内容
        if clone_feedback.is_awaiting(user_id):
            if text in ("/cancel", "/キャンセル", "キャンセル", "cancel"):
                clone_feedback.cancel(user_id)
                await lineworks_bot.send_text(
                    http, user_id, "了解、キャンセルしました。"
                )
                return
            # 修正テキストを保存
            rec = clone_feedback.save_feedback(user_id, text)
            if rec:
                await lineworks_bot.send_text(
                    http, user_id,
                    "ありがとう。\n"
                    "Wiki と資料で裏取りしてから、海山に共有します。\n"
                    "引き続き気軽に話しかけて。",
                )
                logger.info(f"clone_feedback received id={rec['id']} — backcheck 起動")
                # バックチェックを非同期で実行 (ユーザ体験をブロックしない)
                asyncio.create_task(_run_feedback_backcheck(brain, rec))
                return
            # 保存失敗 (TTL 切れ等) → 通常フローに戻す
            logger.warning(f"awaiting feedback save failed for {user_id}")

        # (B) /fix トリガー: 直前の応答に対する修正希望を開始
        if text in FIX_TRIGGERS:
            prior = clone_history.load_recent(user_id, n=4)
            last_user_msg = ""
            last_assistant_msg = ""
            for r in reversed(prior):
                if r["role"] == "assistant" and not last_assistant_msg:
                    last_assistant_msg = r["content"]
                elif r["role"] == "user" and not last_user_msg and last_assistant_msg:
                    last_user_msg = r["content"]
                    break
            if not last_assistant_msg:
                await lineworks_bot.send_text(
                    http, user_id,
                    "まだ私から応答してない状態だから、先に何か聞いてみて。",
                )
                return
            clone_feedback.start_awaiting(
                user_id,
                trigger_msg=last_user_msg,
                response=last_assistant_msg,
            )
            await lineworks_bot.send_text(
                http, user_id,
                "了解、ありがとう。直前の応答のどこが違ったか、次のメッセージで具体的に教えて。\n"
                "例: 「事実誤認」「トーン」「前提が違う」など\n"
                "見送る場合は /cancel と送って。",
            )
            return

        # (B2) 暗黙の修正検出: 直前に AI 応答があり、今回の発言が訂正っぽいなら
        #      ワンステップで feedback として取り込む
        if _looks_like_correction(text):
            recent = clone_history.load_recent(user_id, n=4)
            last_user = ""
            last_assistant = ""
            for r in reversed(recent):
                if r["role"] == "assistant" and not last_assistant:
                    last_assistant = r["content"]
                elif r["role"] == "user" and not last_user and last_assistant:
                    last_user = r["content"]
                    break
            if last_assistant:
                # 一時的に待ち状態にして即 save_feedback (context を成立させる)
                clone_feedback.start_awaiting(
                    user_id, trigger_msg=last_user, response=last_assistant,
                )
                rec = clone_feedback.save_feedback(user_id, text)
                if rec:
                    # 履歴にはユーザ発言として記録 (会話性を維持)
                    clone_history.append(user_id, "user", text)
                    await lineworks_bot.send_text(
                        http, user_id,
                        "訂正として受け取ったよ、ありがとう。\n"
                        "Wiki と資料で裏取りしてから海山に共有する。\n"
                        "会話はこのまま続けて大丈夫。",
                    )
                    logger.info(f"implicit feedback captured id={rec['id']}")
                    asyncio.create_task(_run_feedback_backcheck(brain, rec))
                    return

        # --- 通常フロー ---
        # 初回接触判定 (履歴が空なら welcome 送信)
        prior = clone_history.load_recent(user_id, n=1)
        if not prior:
            try:
                from services.lineworks_onboarding import send_welcome
                await send_welcome(http, user_id)
            except Exception as e:
                logger.warning(f"LINE Works welcome 送信失敗: {e}")

        # ★2026-08-10 (再ローンチ総点検): 「利用開始」ボタンのテキストを LLM に流さない。
        #   流すと bot が毎回即興の自己紹介を返し、初回画面が
        #   「静的 welcome + 例文ボタン + 即興挨拶」の 3 連投になってボタンが押し流される。
        #   しかも即興側は過去に「資料作成や分析の代行はやらない」等、業務エージェント転換
        #   (M4) と正反対の宣言をした実績がある。決定論で受け止めて終わる。
        if (text or "").strip() in ("利用開始", "利用を開始する", "開始"):
            if prior:  # 既存ユーザが押し直した場合だけ、例文ボタンを再掲する
                try:
                    from services.lineworks_onboarding import send_welcome
                    await send_welcome(http, user_id)
                except Exception as e:
                    logger.warning(f"利用開始 再welcome 失敗: {e}")
            return  # 初回は welcome 送信済み = 追加応答しない

        # ユーザメッセージを履歴に追加
        clone_history.append(user_id, "user", text)

        # ★2026-05-26 海山指示: bot 応答後 user が続けた = positive signal、background capture
        # (= 修正以外の follow-up なら conversation_success に記録、style 改善 dataset 化)
        asyncio.create_task(
            _maybe_capture_conversation_continuation(user_id, text, channel_id=None)
        )

        # 直近 20 件 (今回の発言含む) のうち、prior は history=前19件
        history = clone_history.load_recent(user_id, n=21)[:-1]

        # ★2026-05-13 v2: URL 検出 → fetch → attached_content で bot に渡す (拡大版)
        # 上限: 5 URL / 1 URL 50K char / total cap 180K char (smart-gpt の 200K token 余裕内)
        attached_content_url: Optional[str] = None
        try:
            import re as _re
            urls_in_text = _re.findall(r"https?://[^\s<>\"'`]+", text)
            if urls_in_text:
                # 同一 URL を除く + 上限 5
                seen_u: set[str] = set()
                target_urls: list[str] = []
                URL_MAX = int(os.getenv("URL_FETCH_MAX_URLS", "5"))
                URL_PER_MAX = int(os.getenv("URL_FETCH_MAX_CHARS_PER_URL", "50000"))
                URL_TOTAL_MAX = int(os.getenv("URL_FETCH_MAX_TOTAL_CHARS", "180000"))
                URL_TIMEOUT = float(os.getenv("URL_FETCH_TIMEOUT_SEC", "60"))
                for u in urls_in_text:
                    u_clean = u.rstrip(",.;:)】」')")
                    if u_clean in seen_u:
                        continue
                    seen_u.add(u_clean)
                    target_urls.append(u_clean)
                    if len(target_urls) >= URL_MAX:
                        break
                from content_extractor import extract_url
                fetched_parts = []
                total_chars = 0
                for u in target_urls:
                    logger.info(f"[lineworks-url] fetching: {u}")
                    try:
                        # 残り総量に応じて per-URL cap も動的調整
                        remaining = max(URL_TOTAL_MAX - total_chars, 1000)
                        per_cap = min(URL_PER_MAX, remaining)
                        content = await extract_url(u, http, max_chars=per_cap, timeout=URL_TIMEOUT)
                        if content:
                            piece = f"=== URL: {u} ===\n{content}"
                            fetched_parts.append(piece)
                            total_chars += len(piece)
                        if total_chars >= URL_TOTAL_MAX:
                            logger.info(f"[lineworks-url] total char cap reached: {total_chars:,}")
                            break
                    except Exception as e:
                        logger.warning(f"extract_url failed {u}: {e}")
                        fetched_parts.append(f"=== URL: {u} ===\n[取得失敗: {e}]")
                if fetched_parts:
                    attached_content_url = "\n\n".join(fetched_parts)
                    logger.info(
                        f"[lineworks-url] attached {len(target_urls)} URL(s), "
                        f"total {len(attached_content_url):,} chars"
                    )
        except Exception as e:
            logger.warning(f"URL extraction failed: {e}")

        # ★2026-05-26 海山指示: user が明示的に「Drive で〜」「ドライブを〜」 等と言ったら
        # 通常 clone_respond ではなく Drive AI 検索に route (= データ汚染 防止).
        # ★2026-06-20 世界基準評価 ④: admin(海山)限定。非admin の Drive 検索は confidential filename/owner を
        #   外部 Gemini へ egress するため封鎖(group 経路と同じ。security 評価 RISK2)。非admin は通常応答へ。
        from services.auth import is_lw_admin as _is_lw_admin_dm
        if _has_drive_intent(text) and _is_lw_admin_dm(user_id):
            handled = await _handle_drive_intent_query(
                http, user_id, text, via_channel=False,
            )
            if handled:
                # history に保存 (= 通常 turn と同じ扱い)
                user_display = parsed.get("user_display") or parsed.get("display_name")
                clone_history.append(user_id, "user", text, user_display=user_display)
                # Drive 結果は assistant turn として保存しない (= 命名衝突回避、また
                # 引き続く 会話で history 汚染を避けるため)
                return  # ★clone_respond 経路 skip

        # うみやまAI 応答生成
        # ★2026-05-07: production モデルを fast-gpt → smart (Claude Opus 4.7) に変更
        # ★2026-05-13: URL があれば attached_content として渡す
        # ★2026-05-14: user_id / user_display を渡して個別メモリー注入を有効化
        prod_model = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
        user_display = parsed.get("user_display") or parsed.get("display_name")
        # ★fix 2026-05-25 MUST-FIX M-4+M-5: _safe_clone_respond で Semaphore + 空 guard
        reply = await _safe_clone_respond(
            brain, text, history=history, model=prod_model,
            attached_content=attached_content_url,
            user_id=user_id,
            user_display=user_display,
        )

        # アシスタント応答を履歴に保存
        clone_history.append(user_id, "assistant", reply, user_display=user_display)

        # LINE Works へ送信 — 本文のみ。修正は下の入力欄に直接返信で OK
        # (暗黙検出が「違う」「正しくは」等を自動で feedback に取り込む)
        await lineworks_bot.send_text(http, user_id, reply)

        # ★2026-07-10 (世界基準評価 S3): sampled 👍👎 で利用者信号を再生 (default OFF、
        #   FEEDBACK_PROMPT_RATE で有効化)。ロジックは services/feedback_prompt (§1.12b)。
        from services import feedback_prompt as _fbp
        asyncio.create_task(_fbp.maybe_prompt(http, user_id, text, reply, user_display))

        # ★2026-05-26 海山指示 (= 再修正): 通常会話で bot 「データ無い」 系応答時、
        # 「Drive 内も検索しますか?」 を ★提案 (= button、tap で初めて Drive 検索 execute)。
        # データ自体は出さない (= proactive 表示 NG)、user の意思 tap で初めて出る。
        asyncio.create_task(
            _maybe_offer_drive_search(http, user_id, text, reply, via_channel=False)
        )

        # ★2026-05-14: バックグラウンドで個別メモリーを更新
        # (応答送信後なのでユーザ体験には影響しない、fast-gpt で軽量)
        if os.getenv("CLONE_MEMORY_ENABLED", "1") != "0":
            asyncio.create_task(
                brain.update_clone_memory(
                    user_id=user_id,
                    user_query=text,
                    bot_response=reply,
                    user_display=user_display,
                )
            )

        # ★2026-05-21 (項目 4): sleep-time agent をスケジュール
        # 30 秒 idle で memory 再整理 (smart モデル、深め整理)。次のターンで cancel。
        # 連続会話中は何回 schedule されても 1 回も走らず、会話が一区切りすると 1 回走る。
        if os.getenv("CLONE_SLEEP_TIME_ENABLED", "1") != "0":
            try:
                from scripts.clone_sleep_time_agent import schedule_sleep_time_agent
                asyncio.create_task(schedule_sleep_time_agent(user_id))
            except Exception as e:
                logger.warning(f"sleep_time_agent schedule failed: {e}")

    except Exception as e:
        logger.exception(f"うみやまAI handler failed: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、応答生成中にエラーが出ました。少し時間を置いてもう一度試して。",
            )
        except Exception:
            pass


# ─── うみやまAI 添付ファイル ハンドラ ───
def _safe_attachment_name(raw: str, fallback: str) -> str:
    """ファイル名を audit 保存用に sanitize"""
    base = Path(raw or "").name or fallback
    cleaned = "".join(
        c for c in base if c.isalnum() or c in "._-" or ord(c) > 127
    )
    cleaned = cleaned.strip("._-") or fallback
    return cleaned[:200]


async def _handle_lineworks_attachment(app, parsed: dict) -> None:
    """うみやまAI に届いた file / image を処理

    フロー:
      1. 100MB 超えたら受け付けない (即返信)
      2. "📎 資料受け取った、読み込み中..." を即送信
      3. lineworks_bot.download_attachment で取得
      4. data/brain/clone_attachments/<user_id>/<TS>_<filename> に audit 保存
      5. file → content_extractor.extract_file_text (大型対応)
         image → extract_image_bytes (Vision API)
      6. 抽出結果を attached_content として clone_respond_public へ
      7. 応答を送信、履歴に追記
    """
    user_id = parsed["user_id"]
    msg_type = parsed["type"]  # "file" or "image"
    file_id = parsed.get("file_id", "")
    file_name = parsed.get("file_name", "") or (
        f"image_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.jpg"
        if msg_type == "image"
        else f"file_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}"
    )
    file_size = int(parsed.get("file_size") or 0)

    brain: BrainWiki = app.state.brain
    http: httpx.AsyncClient = app.state.http

    # (1) サイズ上限チェック
    if file_size and file_size > CLONE_ATTACHMENT_MAX_BYTES:
        limit_mb = CLONE_ATTACHMENT_MAX_BYTES // (1024 * 1024)
        size_mb = file_size / (1024 * 1024)
        try:
            await lineworks_bot.send_text(
                http, user_id,
                f"⚠️ ファイルが大きすぎる ({size_mb:.1f}MB)。"
                f"上限 {limit_mb}MB まで対応してる。分割して送って。",
            )
        except Exception:
            pass
        return

    # 初回接触なら welcome を出してから受け取る
    prior = clone_history.load_recent(user_id, n=1)
    if not prior:
        try:
            from services.lineworks_onboarding import send_welcome
            await send_welcome(http, user_id)
        except Exception as e:
            logger.warning(f"LINE Works welcome 送信失敗: {e}")

    # (2) 即時 ack
    try:
        await lineworks_bot.send_text(
            http, user_id,
            f"📎 資料受け取った: {file_name}\n読み込み中…少し待って。",
        )
    except Exception as e:
        logger.warning(f"うみやまAI 添付 ack 失敗: {e}")

    # (3) ダウンロード
    try:
        if not file_id:
            raise RuntimeError("file_id が parse できませんでした")
        data = await lineworks_bot.download_attachment(http, file_id)
    except Exception as e:
        logger.exception(f"添付ダウンロード失敗: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、ファイルのダウンロードに失敗しました。"
                "もう一度送ってもらえると助かる。",
            )
        except Exception:
            pass
        return

    if not data:
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ファイルが取れなかった (空 or expired)。もう一度送って。",
            )
        except Exception:
            pass
        return

    # (4) audit 保存
    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_attachment_name(file_name, fallback=f"upload_{timestamp}")
    user_dir = CLONE_ATTACHMENTS_DIR / user_id
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        audit_path = user_dir / f"{timestamp}_{safe_name}"
        audit_path.write_bytes(data)
        logger.info(
            f"clone_attachment audit 保存: {audit_path} "
            f"({len(data):,} bytes, type={msg_type}, user={user_id[:12]}...)"
        )
    except Exception as e:
        logger.warning(f"audit 保存失敗 (続行): {e}")
        audit_path = None

    # (5) 抽出
    extracted = None
    file_type_label = msg_type
    try:
        if msg_type == "image":
            from content_extractor import extract_image_bytes, sniff_extension
            ext = Path(safe_name).suffix.lower()
            if not ext:
                ext = sniff_extension(data) or ".jpg"
            mime = "image/jpeg"
            if ext == ".png":
                mime = "image/png"
            elif ext == ".gif":
                mime = "image/gif"
            elif ext == ".webp":
                mime = "image/webp"
            elif ext in (".heic", ".heif"):
                mime = "image/heic"
            extracted = await extract_image_bytes(
                data, http, LITELLM_URL, LITELLM_KEY,
                mime=mime, model="smart", max_tokens=2000,
            )
            file_type_label = f"image ({mime})"
        else:
            from content_extractor import extract_file_text, sniff_extension
            import tempfile
            ext = Path(safe_name).suffix.lower()
            # LINE Works が fileName を返さないケース等で拡張子が無い場合は
            # 中身の magic byte から推定する (.docx / .pdf / .xlsx / .txt 等)
            if not ext:
                sniffed = sniff_extension(data)
                if sniffed:
                    logger.info(
                        f"file_name 拡張子なし → magic byte から推定: {sniffed}"
                    )
                    ext = sniffed
                    # audit ファイル名にも反映 (rename)
                    if audit_path is not None:
                        try:
                            renamed = audit_path.with_name(audit_path.name + ext)
                            audit_path.rename(renamed)
                            audit_path = renamed
                        except Exception:
                            pass
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=ext
            ) as tf:
                tf.write(data)
                tmp_path = Path(tf.name)
            try:
                extracted = await extract_file_text(
                    tmp_path,
                    max_chars=120_000,
                    max_pages=200,
                    max_sheets=20,
                    max_rows_per_sheet=500,
                )
            finally:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            file_type_label = f"file ({ext.lstrip('.') or 'unknown'})"
    except Exception as e:
        logger.exception(f"添付抽出失敗: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                f"ごめん、{file_name} の中身を読み出せなかった。"
                "PDF / Word / Excel / PowerPoint / 画像 / テキスト系には対応してる。",
            )
        except Exception:
            pass
        return

    if not extracted or not extracted.strip():
        try:
            await lineworks_bot.send_text(
                http, user_id,
                f"{file_name} は受け取ったけど、テキストが取り出せなかった。"
                "(スキャン PDF や暗号化されてるかも。"
                "テキスト化したものか、画像で送り直してくれると読める)",
            )
        except Exception:
            pass
        return

    # (6) clone 応答
    history = clone_history.load_recent(user_id, n=20)
    placeholder_user = f"[資料アップロード: {file_name}] (size={file_size:,} bytes)"
    user_display = parsed.get("user_display") or parsed.get("display_name")
    clone_history.append(user_id, "user", placeholder_user, user_display=user_display)

    try:
        prod_model = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
        # ★fix 2026-05-25 MUST-FIX M-4+M-5: _safe_clone_respond で Semaphore + 空 guard
        reply = await _safe_clone_respond(
            brain, "",
            history=history,
            attached_content=extracted,
            attached_meta={
                "file_name": file_name,
                "file_size": file_size or len(data),
                "file_type": file_type_label,
            },
            model=prod_model,
            user_id=user_id,
            user_display=user_display,
        )
    except Exception as e:
        logger.exception(f"clone_respond_public (添付付き) failed: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、資料は読み込めたけど応答生成でエラーが出た。少し時間置いてもう一度。",
            )
        except Exception:
            pass
        return

    clone_history.append(user_id, "assistant", reply, user_display=user_display)

    try:
        await lineworks_bot.send_text(http, user_id, reply)
    except Exception as e:
        logger.warning(f"うみやまAI 添付応答 送信失敗: {e}")

    # ★2026-05-14: 添付付き応答後もメモリー更新 (placeholder + 添付の context)
    if os.getenv("CLONE_MEMORY_ENABLED", "1") != "0":
        asyncio.create_task(
            brain.update_clone_memory(
                user_id=user_id,
                user_query=f"{placeholder_user}\n本文 (~先頭 500 字): {extracted[:500]}",
                bot_response=reply,
                user_display=user_display,
            )
        )

    # ★2026-05-21 (項目 4): sleep-time agent をスケジュール (添付経路でも)
    if os.getenv("CLONE_SLEEP_TIME_ENABLED", "1") != "0":
        try:
            from scripts.clone_sleep_time_agent import schedule_sleep_time_agent
            asyncio.create_task(schedule_sleep_time_agent(user_id))
        except Exception as e:
            logger.warning(f"sleep_time_agent schedule failed (attachment path): {e}")


# ─── うみやまAI 動画 ハンドラ (★2026-05-27 海山指示「動画対応 進めて」) ───
CLONE_VIDEO_MAX_BYTES = int(os.getenv("CLONE_VIDEO_MAX_BYTES", str(100 * 1024 * 1024)))  # 100MB


async def _video_thumbnail_prefetch(
    http: httpx.AsyncClient,
    user_id: str,
    video_bytes: bytes,
    file_name: str,
) -> None:
    """★2026-05-27 海山指示「動画 thumbnail prefetch」: 動画 ack 後 / 本解析前に
    最初の 1 frame を quick Vision で「最初は: 〇〇」 と速い user 確認 message.

    silent fail OK (= ack / 本解析を absolutely 妨げない).
    """
    try:
        from content_extractor import extract_video_thumbnail, extract_image_bytes
        thumb = await extract_video_thumbnail(video_bytes, max_width=640)
        if not thumb:
            return
        # Vision で 1 行 quick summary
        summary = await extract_image_bytes(
            thumb, http, LITELLM_URL, LITELLM_KEY,
            mime="image/jpeg",
            model="fast",      # GPT-4o low-cost
            max_tokens=80,     # 1 行で十分
            prompt=(
                "この画像の内容を 1 行 (40 字以内、句点無し) で日本語で簡潔に説明. "
                "場所 / 視点 / 主な被写体 のみ、評価コメント無し. "
                "例: 「武蔵小山の商店街、平日午後、歩行者多数」 「店舗内、レジ周り」 等."
            ),
        )
        if summary and summary.strip():
            await lineworks_bot.send_text(
                http, user_id,
                f"📸 最初の frame: {summary.strip()[:80]}"
            )
    except Exception as e:
        logger.info(f"thumbnail prefetch skipped (silent): {e}")


async def _handle_lineworks_video(app, parsed: dict) -> None:
    """うみやまAI に届いた video を ffmpeg frame 抽出 + Vision 分析 → clone_respond.

    フロー (= _handle_lineworks_attachment と類似、video 特化):
      1. サイズ上限チェック (= 100MB)
      2. "🎬 動画読み込み中..." を即送信 (= ffmpeg + Vision は時間かかるため)
      3. download_attachment で取得
      4. audit 保存 (= data/brain/clone_attachments/<user_id>/<TS>_video.mp4)
      5. extract_video_text で frame 抽出 + Vision 分析 (= 3 秒毎 / max 10 frames)
      6. 抽出 text を attached_content として clone_respond_public へ
      7. 応答送信、履歴追記

    cost: 10 frames × ~250 tokens (low-detail GPT-4o) = ~$0.0125/動画
    """
    user_id = parsed["user_id"]
    file_id = parsed.get("file_id", "")
    file_name = parsed.get("file_name", "") or (
        f"video_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.mp4"
    )
    file_size = int(parsed.get("file_size") or 0)

    brain: BrainWiki = app.state.brain
    http: httpx.AsyncClient = app.state.http

    # (1) サイズ上限
    if file_size and file_size > CLONE_VIDEO_MAX_BYTES:
        limit_mb = CLONE_VIDEO_MAX_BYTES // (1024 * 1024)
        size_mb = file_size / (1024 * 1024)
        try:
            await lineworks_bot.send_text(
                http, user_id,
                f"⚠️ 動画が大きすぎる ({size_mb:.1f}MB)。"
                f"上限 {limit_mb}MB まで。短い切り抜きにして再送して。",
            )
        except Exception:
            pass
        return

    # (2) 即時 ack (= download 前、user に体感速さ確保)
    try:
        await lineworks_bot.send_text(
            http, user_id,
            f"🎬 動画受け取った: {file_name}\n読み込み + 分析中…20-30 秒待って。",
        )
    except Exception as e:
        logger.warning(f"video ack 送信失敗: {e}")

    # (3) download
    try:
        if not file_id:
            raise RuntimeError("file_id が parse できませんでした")
        data = await lineworks_bot.download_attachment(http, file_id)
    except Exception as e:
        logger.exception(f"動画ダウンロード失敗: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、動画のダウンロードに失敗した。もう一度送ってもらえる?",
            )
        except Exception:
            pass
        return

    if not data:
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "動画が取れなかった (空 or expired)。もう一度送って。",
            )
        except Exception:
            pass
        return

    # (4) audit 保存 (= file format sniff で正しい拡張子)
    from content_extractor import sniff_extension
    sniffed_ext = sniff_extension(data) or ".mp4"  # default mp4 (= LINE Works デフォ)
    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    # file_name に拡張子無ければ sniffed を補完 (= .mov / .avi / .mkv 等)
    base_name = Path(file_name).stem
    if not Path(file_name).suffix:
        file_name = f"{base_name}{sniffed_ext}"
    safe_name = _safe_attachment_name(file_name, fallback=f"video_{timestamp}{sniffed_ext}")
    user_dir = CLONE_ATTACHMENTS_DIR / user_id
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        audit_path = user_dir / f"{timestamp}_{safe_name}"
        audit_path.write_bytes(data)
        logger.info(
            f"video audit 保存: {audit_path} ({len(data):,} bytes, format={sniffed_ext}, user={user_id[:12]}...)"
        )
    except Exception as e:
        logger.warning(f"video audit 保存失敗 (続行): {e}")

    # (5) ★2026-05-27 海山指示「動画 thumbnail prefetch」: ack 後 / 本解析前に
    # 最初の 1 frame を quick Vision で「最初は: 〇〇」 と速い user 確認 message.
    # public URL upload 経由の image 送信は複雑 → 視覚要約を text ack に乗せる
    # 簡略 path で UX 目的 (= 「これ送ってもらった動画?」 確認) を達成.
    asyncio.create_task(_video_thumbnail_prefetch(http, user_id, data, file_name))

    # (6) frame 抽出 + Vision 分析 (= duration-aware 動的調整)
    try:
        from content_extractor import extract_video_text
        extracted = await extract_video_text(
            data, http, LITELLM_URL, LITELLM_KEY,
            # ★2026-05-27 海山指示: duration から動的に決定 (= None 渡しで auto)
            #   short (< 10s) → 1s 毎、 medium (10-30s) → 3s 毎、 long (30-60s) → 5s 毎
            every_n_seconds=None,
            max_frames=None,
            model="fast",       # GPT-4o (= vision 精度 + 経済性)
        )
    except Exception as e:
        logger.exception(f"動画抽出失敗: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、動画の解析でエラーが出た。短いクリップで再送 or 静止画 1-3 枚で送って。",
            )
        except Exception:
            pass
        return

    if not extracted or not extracted.strip():
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "動画は受け取ったけど、frame を取り出せなかった (= format 非対応 or 空)。"
                "MP4 / MOV で短い切り抜きを送って。",
            )
        except Exception:
            pass
        return

    # (6) clone_respond_public で 海山らしい立地評価
    history = clone_history.load_recent(user_id, n=20)
    placeholder_user = f"[動画アップロード: {file_name}] (size={file_size:,} bytes)"
    user_display = parsed.get("user_display") or parsed.get("display_name")
    clone_history.append(user_id, "user", placeholder_user, user_display=user_display)

    try:
        prod_model = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
        reply = await _safe_clone_respond(
            brain, "",
            history=history,
            attached_content=extracted,
            attached_meta={
                "file_name": file_name,
                "file_size": file_size or len(data),
                "file_type": "video (mp4)",
            },
            model=prod_model,
            user_id=user_id,
            user_display=user_display,
        )
    except Exception as e:
        logger.exception(f"clone_respond_public (video) failed: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、動画は読めたけど応答生成でエラーが出た。少し置いてもう一度。",
            )
        except Exception:
            pass
        return

    clone_history.append(user_id, "assistant", reply, user_display=user_display)
    try:
        await lineworks_bot.send_text(http, user_id, reply)
    except Exception as e:
        logger.warning(f"video reply 送信失敗: {e}")

    # background memory + sleep-time (= attachment と同様)
    if os.getenv("CLONE_MEMORY_ENABLED", "1") != "0":
        asyncio.create_task(
            brain.update_clone_memory(
                user_id=user_id,
                user_query=f"{placeholder_user}\n動画解析: {extracted[:500]}",
                bot_response=reply,
                user_display=user_display,
            )
        )
    if os.getenv("CLONE_SLEEP_TIME_ENABLED", "1") != "0":
        try:
            from scripts.clone_sleep_time_agent import schedule_sleep_time_agent
            asyncio.create_task(schedule_sleep_time_agent(user_id))
        except Exception as e:
            logger.warning(f"sleep_time_agent schedule failed (video path): {e}")


# ─── うみやまAI 音声 input scaffold (★2026-05-27 海山指示、Mac Studio で TTS 統合予定) ───
CLONE_AUDIO_MAX_BYTES = int(os.getenv("CLONE_AUDIO_MAX_BYTES", str(25 * 1024 * 1024)))  # 25MB (= Whisper 上限)


async def _handle_lineworks_audio(app, parsed: dict) -> None:
    """うみやまAI に届いた audio を Whisper で書き起こし → clone_respond.

    フロー (= video handler の subset):
      1. サイズ上限チェック (= 25MB、Whisper 仕様)
      2. "🎤 音声書き起こし中..." 即送信
      3. download_attachment で取得
      4. audit 保存
      5. extract_audio_text で Whisper API 経由書き起こし (= ja default)
      6. 書き起こし text を user query として clone_respond_public へ
      7. 応答送信、履歴追記

    cost: Whisper $0.006/分、1 分音声 ~$0.006、月 100 メッセージ平均 30 秒 = $0.30/月.
    Output TTS (= 海山音声 clone) は別 task (= Mac Studio で ElevenLabs 統合).
    """
    user_id = parsed["user_id"]
    file_id = parsed.get("file_id", "")
    file_name = parsed.get("file_name", "") or (
        f"audio_{datetime.now(JST).strftime('%Y%m%d_%H%M%S')}.m4a"
    )
    file_size = int(parsed.get("file_size") or 0)

    brain: BrainWiki = app.state.brain
    http: httpx.AsyncClient = app.state.http

    # (1) サイズ上限
    if file_size and file_size > CLONE_AUDIO_MAX_BYTES:
        limit_mb = CLONE_AUDIO_MAX_BYTES // (1024 * 1024)
        size_mb = file_size / (1024 * 1024)
        try:
            await lineworks_bot.send_text(
                http, user_id,
                f"⚠️ 音声が大きすぎる ({size_mb:.1f}MB)。上限 {limit_mb}MB (Whisper 仕様)。"
                "短いクリップにして再送して。",
            )
        except Exception:
            pass
        return

    # (2) 即時 ack
    try:
        await lineworks_bot.send_text(
            http, user_id,
            f"🎤 音声受け取った: {file_name}\n書き起こし中…10-20 秒待って。",
        )
    except Exception as e:
        logger.warning(f"audio ack 送信失敗: {e}")

    # (3) download
    try:
        if not file_id:
            raise RuntimeError("file_id が parse できませんでした")
        data = await lineworks_bot.download_attachment(http, file_id)
    except Exception as e:
        logger.exception(f"音声ダウンロード失敗: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、音声のダウンロードに失敗した。もう一度送って。",
            )
        except Exception:
            pass
        return

    if not data:
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "音声が取れなかった (空 or expired)。もう一度送って。",
            )
        except Exception:
            pass
        return

    # (4) audit 保存
    timestamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    # 拡張子は file_name から、無ければ .m4a default (= LINE Works モバイル録音 form)
    ext = Path(file_name).suffix or ".m4a"
    base_name = Path(file_name).stem or f"audio_{timestamp}"
    safe_name = _safe_attachment_name(f"{base_name}{ext}", fallback=f"audio_{timestamp}{ext}")
    user_dir = CLONE_ATTACHMENTS_DIR / user_id
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        audit_path = user_dir / f"{timestamp}_{safe_name}"
        audit_path.write_bytes(data)
        logger.info(
            f"audio audit 保存: {audit_path} ({len(data):,} bytes, user={user_id[:12]}...)"
        )
    except Exception as e:
        logger.warning(f"audio audit 保存失敗 (続行): {e}")

    # (5) Whisper 書き起こし
    try:
        from content_extractor import extract_audio_text
        # MIME map (= 拡張子から ざっくり推定、Whisper 側 format auto-detect)
        mime_map = {
            ".m4a": "audio/m4a", ".mp3": "audio/mpeg", ".wav": "audio/wav",
            ".ogg": "audio/ogg", ".webm": "audio/webm", ".mp4": "audio/mp4",
        }
        mime = mime_map.get(ext.lower(), "audio/m4a")
        transcript = await extract_audio_text(
            data, http, LITELLM_URL, LITELLM_KEY,
            file_name=safe_name,
            mime=mime,
            language="ja",
            model="whisper",
        )
    except Exception as e:
        logger.exception(f"音声書き起こし失敗: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、音声の書き起こしでエラーが出た。format 確認 or text で送って。",
            )
        except Exception:
            pass
        return

    if not transcript or not transcript.strip():
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "音声は受け取ったけど、書き起こしが空だった (= 無音 or 雑音のみ?)。"
                "もう一度送るか、text に切替えて。",
            )
        except Exception:
            pass
        return

    # 確認 message (= user に書き起こし結果 expose、誤認識の早期検知)
    try:
        await lineworks_bot.send_text(
            http, user_id, f"📝 書き起こし: {transcript[:300]}",
        )
    except Exception:
        pass

    # (6) clone_respond_public で 通常 reply
    history = clone_history.load_recent(user_id, n=20)
    user_display = parsed.get("user_display") or parsed.get("display_name")
    clone_history.append(user_id, "user", transcript, user_display=user_display)

    try:
        prod_model = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
        reply = await _safe_clone_respond(
            brain, transcript,
            history=history,
            model=prod_model,
            user_id=user_id,
            user_display=user_display,
        )
    except Exception as e:
        logger.exception(f"clone_respond_public (audio) failed: {e}")
        try:
            await lineworks_bot.send_text(
                http, user_id,
                "ごめん、書き起こしは出来たけど応答生成でエラーが出た。少し置いてもう一度。",
            )
        except Exception:
            pass
        return

    clone_history.append(user_id, "assistant", reply, user_display=user_display)
    try:
        await lineworks_bot.send_text(http, user_id, reply)
    except Exception as e:
        logger.warning(f"audio reply 送信失敗: {e}")

    # background memory + sleep-time
    if os.getenv("CLONE_MEMORY_ENABLED", "1") != "0":
        asyncio.create_task(
            brain.update_clone_memory(
                user_id=user_id,
                user_query=f"[音声] {transcript}",
                bot_response=reply,
                user_display=user_display,
            )
        )
    if os.getenv("CLONE_SLEEP_TIME_ENABLED", "1") != "0":
        try:
            from scripts.clone_sleep_time_agent import schedule_sleep_time_agent
            asyncio.create_task(schedule_sleep_time_agent(user_id))
        except Exception as e:
            logger.warning(f"sleep_time_agent schedule failed (audio path): {e}")


# ─── Claude Code Postback ハンドラ (approve / revise / cancel) ───
async def _handle_claude_postback(
    app, user_id: str, reply_token: str, pb_data: str
) -> None:
    """Claude Code プラン承認ボタンの処理

    pb_data 例:
      claude=approve&task=20260422_120000_123456
      claude=revise&task=20260422_...
      claude=cancel&task=20260422_...
    """
    action = ""
    task_id = ""
    for pair in pb_data.split("&"):
        if pair.startswith("claude="):
            action = pair.split("=", 1)[1]
        elif pair.startswith("task="):
            task_id = pair.split("=", 1)[1]

    if not task_id:
        await reply_message(
            app.state.http, reply_token, "⚠️ task_id が見つかりません。"
        )
        return

    r_conn = app.state.redis
    plan_key = f"claude_plan:{task_id}"
    plan_data_raw = await r_conn.get(plan_key)
    if not plan_data_raw:
        await reply_message(
            app.state.http, reply_token,
            f"⚠️ プラン {task_id[:15]}… は期限切れ or 見つかりません。"
            "\n/claude で再依頼してください。"
        )
        return

    try:
        plan_data = json.loads(plan_data_raw)
    except Exception:
        await reply_message(
            app.state.http, reply_token, "⚠️ プランデータが壊れています。"
        )
        return

    if action == "approve":
        # 実行モードで新しいタスクを投入
        try:
            task_path = _queue_claude_task(
                user_id,
                plan_data.get("instruction", ""),
                source="line_claude_execute",
                mode="execute",
                approved_plan=plan_data.get("plan", ""),
                parent_task_id=task_id,
            )
        except Exception as e:
            logger.exception("queue execute task failed")
            await reply_message(
                app.state.http, reply_token, f"⚠️ 実行タスク登録失敗: {e}"
            )
            return
        # 承認済み plan は残しつつ、revise 状態は解除
        await r_conn.delete(f"claude_revise:{user_id}")
        await reply_message(
            app.state.http, reply_token,
            f"▶️ 実行開始: {task_path.name}\n"
            f"完了したら push で結果を送ります。"
        )
        return

    if action == "revise":
        # 次の text メッセージを「修正指示」として扱う状態に入る
        revise_payload = json.dumps(
            {
                "task_id": task_id,
                "original_instruction": plan_data.get("instruction", ""),
                "previous_plan": plan_data.get("plan", ""),
            },
            ensure_ascii=False,
        )
        await r_conn.setex(
            f"claude_revise:{user_id}", CLAUDE_REVISE_TTL, revise_payload
        )
        await reply_message(
            app.state.http, reply_token,
            "✏️ 修正指示モード\n"
            "次のメッセージで、計画のどこを直したいか教えてください。\n"
            "例: 「変更対象を X だけに絞って」「Y も追加して」\n\n"
            "（60 分以内に送信）"
        )
        return

    if action == "cancel":
        await r_conn.delete(plan_key)
        await r_conn.delete(f"claude_revise:{user_id}")
        await reply_message(
            app.state.http, reply_token,
            f"❌ キャンセルしました: {task_id[:15]}…"
        )
        return

    await reply_message(
        app.state.http, reply_token, f"⚠️ 未対応の操作: {action}"
    )


# ─── 目的選択 Postback ハンドラ ───
async def _handle_purpose_postback(app, user_id: str, reply_token: str, pb_data: str) -> None:
    """Quick Reply で選ばれた「目的」に応じて、保留中の入力を処理"""
    # postback data 例: "purpose=wiki", "purpose=fix", "purpose=chat"
    purpose = ""
    for pair in pb_data.split("&"):
        if pair.startswith("purpose="):
            purpose = pair.split("=", 1)[1]
            break

    r_conn = app.state.redis
    pending = await _pop_pending_input(r_conn, user_id)
    if not pending:
        await reply_message(
            app.state.http, reply_token,
            "⏱ このボタンは旧メニューのものです。今はそのまま送れば会話に、"
            "/teach <内容> で Wiki 保存、/claude <指示> でシステム修正になります。"
        )
        return

    if purpose == "wiki":
        # PrivacyGate → ingest_note(smart)
        try:
            result = await app.state.privacy.filter(pending, sender_id=user_id)
            if result.verdict.value != "allow":
                await reply_message(
                    app.state.http, reply_token,
                    f"🔒 PrivacyGate によりブロック（{result.verdict.value}）: Wiki には保存しません。"
                )
                return
            sanitized = result.sanitized
        except Exception as e:
            logger.warning(f"PrivacyGate error on wiki update: {e}")
            sanitized = pending

        try:
            await app.state.brain.ingest_note(
                user_id, sanitized, title="line_memo", model="smart"
            )
        except Exception as e:
            logger.exception("ingest_note failed for purpose=wiki")
            await reply_message(
                app.state.http, reply_token,
                f"⚠️ Wiki 取り込みでエラー: {e}"
            )
            return

        preview = sanitized.replace("\n", " ")[:100]
        await reply_message(
            app.state.http, reply_token,
            f"📝 Wiki に保存しました（{len(sanitized)}字）\n「{preview}…」"
        )
        return

    if purpose == "fix":
        # システム修正として記録
        try:
            path = await _log_system_improvement(user_id, pending)
        except Exception as e:
            logger.exception("system improvement log failed")
            await reply_message(
                app.state.http, reply_token,
                f"⚠️ システム修正ログの保存でエラー: {e}"
            )
            return

        # 直前の AI 応答を取得して改善フローを即起動（明示的フィードバック= force=True）
        try:
            raw_hist = await app.state.redis.lrange(f"chat:{user_id}", -2, -1)
            last_ai_reply = ""
            if raw_hist:
                last = json.loads(raw_hist[-1])
                if last.get("role") == "assistant":
                    last_ai_reply = last.get("content", "")
        except Exception:
            last_ai_reply = ""

        asyncio.create_task(
            _auto_improve_force(app, "line_manual_fix", user_id, pending, last_ai_reply)
        )

        # Claude Code には計画モードで依頼（承認ワークフロー）
        claude_task_name = ""
        try:
            task_path = _queue_claude_task(
                user_id, pending,
                source="line_fix_button", mode="plan",
            )
            claude_task_name = task_path.name
        except Exception as e:
            logger.warning(f"queue claude task (fix button) failed: {e}")

        preview = pending.replace("\n", " ")[:100]
        extra = (
            f"\n🤖 Claude Code に計画依頼: {claude_task_name}\n"
            "  内容確認後に実行承認へ進みます。"
            if claude_task_name else ""
        )
        await reply_message(
            app.state.http, reply_token,
            f"🔧 システム修正として記録しました\n→ {path.name}\n「{preview}…」\n\n"
            f"改善ループを起動しました（バックグラウンドで処理中）。{extra}"
        )
        return

    if purpose == "chat":
        # 普通のエージェント実行（RAG + LLM 応答）
        reply = await run_agent(
            app, app.state.http, app.state.redis, user_id, pending
        )
        await reply_message(app.state.http, reply_token, reply)
        asyncio.create_task(_safe_ingest(app, user_id, pending, reply))
        # 不満足な回答を自動検知 → 改善ループ起動
        asyncio.create_task(
            _auto_improve_if_unsatisfactory(app, "line_chat", user_id, pending, reply)
        )
        return

    # 不明なpurpose
    await reply_message(
        app.state.http, reply_token,
        f"⚠️ 未対応の目的です: {purpose or '(空)'}"
    )


# ─── リアルタイム自動改善（不満足回答の検知 → 即時改善） ───
async def _auto_improve_if_unsatisfactory(
    app, source: str, user_id: str, user_msg: str, ai_reply: str
) -> None:
    """バックグラウンドでパターン検知 → 必要なら改善パッチを生成・適用"""
    try:
        # 直前のユーザー発話（繰返し質問判定用）
        prev_user_msg = ""
        try:
            raw_hist = await app.state.redis.lrange(f"chat:{user_id}", -4, -3)
            if raw_hist:
                prev = json.loads(raw_hist[0])
                if prev.get("role") == "user":
                    prev_user_msg = prev.get("content", "")
        except Exception:
            pass

        result = await detect_and_improve(
            app, source=source, user_id=user_id,
            user_msg=user_msg, ai_reply=ai_reply,
            prev_user_msg=prev_user_msg, force=False,
        )
        if result.get("triggered"):
            logger.info(
                f"auto_improve applied: {len(result.get('applied_patches', []))} patches"
            )
    except Exception as e:
        logger.exception(f"_auto_improve_if_unsatisfactory failed: {e}")


async def _auto_improve_force(
    app, source: str, user_id: str, user_msg: str, ai_reply: str
) -> None:
    """明示フィードバック（システム修正ボタン等）による改善起動"""
    try:
        await detect_and_improve(
            app, source=source, user_id=user_id,
            user_msg=user_msg, ai_reply=ai_reply,
            prev_user_msg="", force=True,
        )
    except Exception as e:
        logger.exception(f"_auto_improve_force failed: {e}")


# ─── バックグラウンド学習（Privacy Gate → Brain Wiki） ───
async def _safe_ingest(app, user_id: str, user_msg: str, ai_reply: str):
    """会話をプライバシーフィルタ通過後にBrainWikiへ蓄積。非同期・非ブロッキング。"""
    try:
        result = await app.state.privacy.filter(user_msg, sender_id=user_id)
        if result.verdict.value == "allow":
            sanitized_reply = gate3_scrub_pii(ai_reply, app.state.privacy.config)
            await app.state.brain.ingest_conversation(
                user_id, result.sanitized, sanitized_reply
            )
    except Exception as e:
        logger.warning(f"Background ingest error: {e}")


# ─── Recall.ai webhook (Meet / Zoom / Teams 会議の bot 参加 + transcript 取得) ───
RECALL_WEBHOOK_SECRET = os.getenv("RECALL_WEBHOOK_SECRET", "")
RECALL_API_KEY = os.getenv("RECALL_API_KEY", "")
RECALL_API_BASE = os.getenv("RECALL_API_BASE", "https://api.recall.ai")


def _verify_recall_signature(body: bytes, headers: dict) -> bool:
    """Recall.ai webhook 署名検証 (★Svix 公式仕様準拠、2026-05-13 修正)。

    Svix 署名仕様:
      - Header: `svix-signature: v1,<base64-hash>` (空白区切りで複数 sig 可)
      - Sign content: `${msg_id}.${timestamp}.${payload}` (HMAC-SHA256 → base64)
      - Secret: `whsec_<base64>` の base64 部分を decode して鍵に使う

    Secret 未設定なら fail-closed で拒否 (★2026-06-10 Codex HIGH)。
    """
    if not RECALL_WEBHOOK_SECRET:
        # ★2026-06-10 (Codex HIGH): fail-open → fail-closed。secret 未設定では署名検証できず、
        # 任意 URL を fetch する SSRF / Brain 汚染の経路になる。検証不能なら処理しない (本番は
        # secret 設定済なので影響ゼロ、設定漏れ時の保険)。
        logger.error("RECALL_WEBHOOK_SECRET 未設定 → Recall webhook 拒否 (fail-closed)")
        return False

    sig_header = headers.get("svix-signature", "")
    msg_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")

    if not (sig_header and msg_id and timestamp):
        logger.warning(f"Recall webhook missing svix headers: sig={bool(sig_header)} id={bool(msg_id)} ts={bool(timestamp)}")
        return False

    try:
        # Secret の "whsec_" prefix を剥がして base64 decode
        secret_b64 = RECALL_WEBHOOK_SECRET
        if secret_b64.startswith("whsec_"):
            secret_b64 = secret_b64[len("whsec_"):]
        secret_bytes = base64.b64decode(secret_b64)

        # Sign content: ${msg_id}.${timestamp}.${body}
        signed_content = f"{msg_id}.{timestamp}.".encode("utf-8") + body
        expected_hash = hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
        expected_sig = base64.b64encode(expected_hash).decode("ascii")

        # svix-signature header は "v1,<sig> v1,<sig2> ..." の形式
        for entry in sig_header.split(" "):
            version, _, hash_part = entry.partition(",")
            if version == "v1" and hmac.compare_digest(hash_part, expected_sig):
                # ★replay 対策: 署名一致後に timestamp 鮮度を検証 (±5 分 tolerance)。
                # svix-timestamp は署名対象 (signed_content) に含まれるため改竄不可。
                # 古い (or 未来の) リクエストは replay 疑いとして拒否。
                try:
                    if abs(_time_mod.time() - int(timestamp)) > 300:
                        logger.warning(
                            f"Recall webhook timestamp stale/replay 疑い: ts={timestamp} (±300s 超過) → 拒否"
                        )
                        return False
                except (ValueError, TypeError):
                    logger.warning(f"Recall webhook timestamp 不正: ts={timestamp!r} → 拒否")
                    return False
                return True
        return False
    except Exception as e:
        logger.warning(f"Recall signature verify error: {e}")
        return False


async def _fetch_recall_transcript(http: httpx.AsyncClient, transcript_url: str) -> dict:
    """Recall.ai transcript URL から JSON 取得"""
    headers = {}
    if RECALL_API_KEY:
        headers["Authorization"] = f"Token {RECALL_API_KEY}"
    resp = await http.get(transcript_url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json()


def _build_transcript_text(transcript_json: dict) -> tuple[str, list[str]]:
    """Recall.ai transcript JSON を話者付き text に変換、参加者 list も返す。

    Recall の transcript 形式 (推測):
      {"transcript": [{"speaker": "...", "words": [{"text": "...", "start": ..., "end": ...}, ...]}, ...]}
    """
    parts: list[str] = []
    participants: set[str] = set()
    segments = transcript_json.get("transcript") or transcript_json.get("segments") or []
    for seg in segments:
        speaker = seg.get("speaker") or seg.get("participant", {}).get("name") or "Unknown"
        participants.add(speaker)
        words = seg.get("words") or []
        if words:
            text = " ".join(w.get("text", "") for w in words).strip()
        else:
            text = seg.get("text", "").strip()
        if text:
            parts.append(f"**{speaker}**: {text}")
    return "\n\n".join(parts), sorted(participants)


@app.post("/webhook/recall")
async def recall_webhook(request: Request, bg_tasks: BackgroundTasks):
    """Recall.ai webhook: 会議終了 + transcript 完成イベントを受けて議事録を生成。

    対応イベント:
      - 'bot.done' / 'recording.done' — recording 完成 (音声 URL を保存)
      - 'transcript.done' / 'transcription.done' — transcript 完成 (議事録を生成)
    """
    body = await request.body()
    # ★2026-05-13: Svix 仕様準拠 (svix-id / svix-timestamp / svix-signature 3 ヘッダ使う)
    # 一部 webhook 配信元 (Webhook.site test 等) は標準 Webhooks-* ヘッダを使う
    svix_headers = {
        "svix-signature": (
            request.headers.get("svix-signature")
            or request.headers.get("webhook-signature")
            or ""
        ),
        "svix-id": (
            request.headers.get("svix-id")
            or request.headers.get("webhook-id")
            or ""
        ),
        "svix-timestamp": (
            request.headers.get("svix-timestamp")
            or request.headers.get("webhook-timestamp")
            or ""
        ),
    }
    # ★2026-05-13 デバッグ: 全ヘッダを log (sensitive value はマスク)
    debug_headers = {k: v for k, v in request.headers.items() if not k.lower().startswith(("authorization", "cookie"))}
    logger.info(f"[Recall] webhook received, headers: {debug_headers}")
    if not _verify_recall_signature(body, svix_headers):
        logger.warning("Recall webhook signature invalid")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:
        logger.warning(f"Recall webhook payload parse error: {e}")
        raise HTTPException(status_code=400, detail="bad payload")

    event = payload.get("event") or payload.get("type") or ""
    data = payload.get("data") or {}
    bot = data.get("bot") or {}
    logger.info(
        f"[Recall] event={event} bot_id={bot.get('id')} "
        f"meeting={bot.get('meeting_url') or bot.get('meeting_metadata', {}).get('url')}"
    )

    # 即 ACK + バックグラウンド処理
    bg_tasks.add_task(_process_recall_event, request.app, event, payload)
    return {"ok": True}


# ─── 音声アラインメント (Vapi 電話) ★2026-05-18 ───

def _build_voice_config() -> dict:
    """Vapi assistant の voice config を組み立てる。

    ★2026-05-26 海山指示: ElevenLabs Pro Voice Clone (= 本人音声 training 済) を
    primary、Azure Keita を fallback とする。

    Env vars (= override 可、未設定なら下記 default):
        ELEVENLABS_VOICE_ID    Pro Voice Clone の Voice ID (= AI Studio 生成)
        VAPI_VOICE_PROVIDER    "11labs" (default) / "azure" (= 戻したい時)
        VAPI_VOICE_MODEL       ElevenLabs model 名 (default eleven_turbo_v2_5)
                                候補: eleven_turbo_v2_5 (= 新世代、JP 会話自然)
                                      eleven_multilingual_v2 (= 旧、accent
                                        interpolation で 外国人っぽくなる症状報告)
                                      eleven_flash_v2_5 (= 最速だが品質低め)
        VAPI_VOICE_STABILITY   0.0-1.0 (default 0.5、conversational baseline)
        VAPI_VOICE_SIMILARITY  0.0-1.0 (default 0.75、本人色 + artifact のバランス)
        VAPI_VOICE_STYLE       0.0-1.0 (default 0.0、artifact / 外国人化 防止で抑制)
        VAPI_VOICE_SPEAKER_BOOST "true"/"false" (default "false"、true は出力 artifact 源)

    ★2026-05-26 (1st pass): 「自然な JP に」 で stability↓ similarity↑ style↑ 設定
        → 結果「外国人の話す日本語みたい / バック雑音」 FB
        → (2nd pass、本コミット):
        * model: eleven_multilingual_v2 → eleven_turbo_v2_5
          (= 多言語 v2 の accent interpolation 解消、新世代 model)
        * style: 0.35 → 0.0 (= 抑揚過多が「外国人っぽい話し方」 誘発、ベースラインへ戻す)
        * useSpeakerBoost: True → False (= バックの雑音 / artifact 主因、停止で出力クリーン化)
        * stability: 0.4 → 0.5 (= 過度な variation も artifact 源、baseline へ)
        * similarityBoost: 0.85 → 0.75 (= 高すぎる similarity は限られた training data で
          artifact、baseline へ)

    fallbackPlan 動作:
        - 11labs primary が 5xx / quota 切れ / 認証エラー 時、自動的に Azure Keita に切替
        - 通話中断せず継続、UX への影響最小化
        - Vapi 公式 spec: voices[] は array で複数候補 OK だが、現状 Azure 1 つで十分

    前提: Vapi dashboard → Provider Keys に ElevenLabs API key 登録済
        登録されてないと 11labs 経路は使えず常に fallback 動作になる。
        登録方法: Vapi dashboard 左 menu → Provider Keys → ElevenLabs → API Key 貼付。
    """
    provider = os.getenv("VAPI_VOICE_PROVIDER", "11labs").strip()
    elevenlabs_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

    # ElevenLabs key 未設定の場合は Azure fallback (= 後方互換)
    if provider == "11labs" and not elevenlabs_voice_id:
        logger.warning(
            "[voice-align] ELEVENLABS_VOICE_ID 未設定 → Azure Keita に fallback"
        )
        return {
            "provider": "azure",
            "voiceId": "ja-JP-KeitaNeural",
            "speed": 1.05,
        }

    if provider == "11labs":
        # ★ ElevenLabs Pro Voice Clone (= 海山本人声) + Azure Keita fallback
        # ★2026-05-26 2nd pass: 「外国人 + 雑音」 FB → model + 4 params 見直し
        # ★2026-05-26 3rd pass: 「雑音まだ少し / 平穏すぎ / alpha 波寄り」 FB
        # ★2026-05-26 4th pass: 「スピード若干速い / 間が不自然 / 雑音もう少し」 FB
        # ★2026-05-26 5th pass: 「スピードまだ少し早い / ふむは音声 NG」 FB
        # ★2026-05-26 6th pass: 「もう少し抑揚 + スピード遅く」 FB
        #   - style 0.1 → 0.15 (= subtle 抑揚 増、0.2 超で外国人化リスクなので 0.15 が sweet spot)
        #   - stability 0.75 → 0.78 (= rhythm さらに smooth、perceived speed↓)
        #   - speed: 0.9 を ElevenLabs voice 設定に追加 (= 10% 遅く、Vapi 経由 ElevenLabs 直制御)
        speaker_boost_str = os.getenv("VAPI_VOICE_SPEAKER_BOOST", "false").strip().lower()
        return {
            "provider": "11labs",
            "voiceId": elevenlabs_voice_id,
            "model": os.getenv("VAPI_VOICE_MODEL", "eleven_turbo_v2_5").strip(),
            "stability": float(os.getenv("VAPI_VOICE_STABILITY", "0.78")),
            "similarityBoost": float(os.getenv("VAPI_VOICE_SIMILARITY", "0.55")),
            "style": float(os.getenv("VAPI_VOICE_STYLE", "0.15")),
            "useSpeakerBoost": speaker_boost_str in ("true", "1", "yes", "on"),
            # ★ ElevenLabs speed 0.5-2.0 (= 1.0 default、0.9 で 10% 遅く)
            # ★2026-05-26 6th pass で「スピードもう少し遅く」 FB に直接対応
            "speed": float(os.getenv("VAPI_VOICE_SPEED", "0.9")),
            # ★ 0 = 最高品質 (artifact / 雑音 最小)、latency は若干↑だが voice-align は
            # 品質最優先で OK。1-4 で latency 優先に振る (= 品質低下リスク)。
            "optimizeStreamingLatency": int(os.getenv("VAPI_VOICE_LATENCY", "0")),
            "language": "ja",
            "fallbackPlan": {
                "voices": [
                    {
                        "provider": "azure",
                        "voiceId": "ja-JP-KeitaNeural",
                    },
                ],
            },
        }

    # Provider 明示 "azure" or unknown → Azure Keita 直
    return {
        "provider": "azure",
        "voiceId": "ja-JP-KeitaNeural",
        "speed": 1.05,
    }


def _voice_align_caller_trusted(msg: dict, payload: dict) -> bool:
    """VOICE_ALIGN_CALLER_ALLOWLIST (E.164 comma 区切り、例 +8190...,+81...) による
    発信者検証 (★2026-07-04 cross-check DA)。**未設定なら True** (= opt-in、既存動作
    不変)。設定時は Vapi payload の call.customer.number を照合し、不一致 / 取得不能は
    False (= 蓄積サマリ・話のタネ非注入 + 蒸留 skip)。web 経路は token 認証済なので対象外。
    """
    allowlist = {
        n.strip().replace(" ", "").replace("-", "")
        for n in os.getenv("VOICE_ALIGN_CALLER_ALLOWLIST", "").split(",")
        if n.strip()
    }
    if not allowlist:
        return True
    call_obj = msg.get("call") or payload.get("call") or {}
    caller = str(
        ((call_obj.get("customer") or {}).get("number")) or ""
    ).replace(" ", "").replace("-", "")
    return bool(caller) and caller in allowlist


async def _build_voice_align_assistant_config(
    first_message: str = None, server_secret: str = None, trusted: bool = True,
    browser_delivered: bool = False,
) -> dict:
    """Vapi assistant 設定を組み立てる (phone + web 共通)。

    動的 prompt 生成: 過去蓄積サマリ + 現時点で薄い次元を毎回 system_prompt
    に注入して、雑談が「前回の続き」として進む + 薄い領域を自然に深掘る。

    ★2026-05-21: web (Vapi WebRTC) 経路追加に伴い、phone webhook 内の
    インライン logic からこの helper に切り出し。電話番号経由は telephony
    per-minute fee が乗るため、web 経路は 30-50% コスト削減になる。

    ★2026-07-04 security: server_secret を経路別に分離可能に。web-config はこの config を
    そのままブラウザへ返す = 含めた secret は露出する前提で、web には VAPI_WEB_SECRET を
    渡す (webhook 側で assistant-request には使えない = 深層 prompt の exfiltration 遮断)。
    """
    import alignment_interview as ai

    # ★2026-07-04 cross-check DA: trusted=False (= VOICE_ALIGN_CALLER_ALLOWLIST 外の
    # 発信者) には蓄積サマリ (interview/ 深層) も wiki 話のタネも注入しない縮退 config。
    # X-Vapi-Secret は「Vapi からの呼び出し」しか証明せず「電話の相手が海山」は証明しない。
    if not trusted:
        # 縮退 config: 人格 prompt は素 (蓄積・話のタネ無し)、冒頭も内容ゼロの雑談 opener。
        try:
            fm = first_message or ai.build_first_message()
        except Exception:
            fm = "お疲れさま。…どうも。"
        system_prompt = ai.build_interviewer_system_prompt()
        return _voice_align_assistant_dict(fm, system_prompt, server_secret)

    recent = ""
    try:
        cov = ai.load_coverage()
        log = cov.get("session_log", [])
        n_sess = len(log)
        from brain_wiki import WIKI_DIR as _WK
        idir = _WK / "interview"
        past_bits = []
        # ★2026-08-03 ブラウザ配送時は最深カテゴリを落とす (判定は voice_visibility、§1.12b)
        from brain_wiki_helpers.voice_visibility import interview_files_for_voice
        for f in interview_files_for_voice(idir, browser_delivered=browser_delivered):
            try:
                b = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if b.startswith("---"):
                e = b.find("\n---", 3)
                if e > 0:
                    b = b[e + 4:]
            b = b.strip()
            if b:
                # ★2026-07-04 fix: 先頭500字 = append-only file の最古 insight で永久固定
                # だった (13回話しても interviewer が見る本人像は5月のまま = 同じ角度の
                # 質問が再来 → 失速の一因)。末尾500字 = 最新 insight に変更。
                past_bits.append(f"[{f.stem}] {b[-500:]}")
        thin = [r["label"] for r in ai.coverage_report()[:3]]
        # ★2026-07-04 継続性: 直近セッションの session_summary を注入 (レビュー未了でも効かせる)。
        # 従来 EXTRACT_PROMPT が生成する session_summary はどこにも配線されておらず「前回の
        # 続き」が壊れていた (失速の構造要因)。
        # ★2026-08-03: 要約は次元フィルタが効かない (直近が family/shadow 回なら深層がそのまま
        # 出る) → ブラウザ配送時のみ丸ごと落とす (判定は voice_visibility、§1.12b)
        from brain_wiki_helpers.voice_visibility import redact_summaries_for_browser
        try:
            summaries = ai.recent_session_summaries(3)
        except Exception:
            summaries = []
        summaries = redact_summaries_for_browser(summaries, browser_delivered=browser_delivered)
        cont = (
            "\n\n【前回までの流れ (この続きから自然に)】\n"
            + "\n".join(f"- {s}" for s in summaries)
        ) if summaries else ""
        recent = (
            f"これまで {n_sess} 回雑談済。"
            + (f"前回 {log[-1].get('ts','')[:10]}。" if log else "")
            + cont
            + (
                "\n\n【これまでに蓄積した本人像 (続きとして踏まえる)】\n"
                + "\n".join(past_bits)
                if past_bits else ""
            )
            + f"\n\n【まだ薄い=今日できれば触れたい】{' / '.join(thin)}"
        )
    except Exception as e:
        logger.warning(f"[voice-align] recent build failed: {e}")

    # ★2026-07-04 海山指示「wiki 情報をある程度連携させて話をしたい」:
    # 最近更新の wiki (meetings / personal PJ / decisions / hobbies / knowledge) から
    # 話のタネを拾い prompt に注入。firstMessage の topic hook にも同じ種を流用。
    # env: VOICE_ALIGN_WIKI_TOPICS=0 で opt-out、件数/鮮度は TOPIC_MAX / TOPIC_DAYS。
    wiki_topics_block = ""
    topic_titles: list = []
    if os.getenv("VOICE_ALIGN_WIKI_TOPICS", "1").strip().lower() not in ("0", "false", "off"):
        try:
            topics = ai.collect_wiki_topics(
                max_items=int(os.getenv("VOICE_ALIGN_TOPIC_MAX", "6")),
                days=int(os.getenv("VOICE_ALIGN_TOPIC_DAYS", "21")),
            )
            # ★2026-08-03 §1.15 DA 迂回2: 話のタネは personal/ を意図的に含む (§1.17 4系統目)
            # が、その抜粋も **ブラウザ配送 config には平文で載る**。interview を絞っても
            # personal (投資 PJ・第三者の発話込み) が出るのは一貫しないので web では落とす。
            if browser_delivered:
                topics = [t for t in topics if t.get("dir") != "personal"]
            wiki_topics_block = ai.format_wiki_topics(topics)
            # ★2026-07-04 cross-check DA: 冒頭で「こちらから読み上げる」題名は
            # hobbies / personal PJ に限定。meetings / knowledge / decisions の題名
            # (M&A・人事・未公表数値等になり得る) は、誰が聞いているか分からない
            # 通話冒頭では発話しない (prompt 内の話のタネとしては使える)。
            topic_titles = [
                t["title"] for t in topics
                if t.get("dir") in ("hobbies", "personal")
            ]
        except Exception as e:
            logger.warning(f"[voice-align] wiki topics build failed: {e}")

    system_prompt = ai.build_interviewer_system_prompt(
        recent_summary=recent, wiki_topics=wiki_topics_block,
    )

    # ★2026-07-04 海山指示「冒頭の話し方は画一的じゃなくもっと自然に」:
    # 固定文言をやめ、時間帯 × 前回からの間隔 × 話のタネで毎回組み立てる
    # (LLM 不使用 = assistant-request の応答速度は不変)。失敗時は従来文言に fallback。
    if not first_message:
        try:
            _log = ai.load_coverage().get("session_log", [])
            first_message = ai.build_first_message(
                last_session_ts=(_log[-1].get("ts") if _log else None),
                last_summary=ai.latest_session_summary(),
                topic_hints=topic_titles,
            )
        except Exception as e:
            logger.warning(f"[voice-align] first message build failed: {e}")

    cfg = _voice_align_assistant_dict(
        first_message or (
            "お疲れさま。ちょっと一息つこっか。"
            "最近どう? なんか、いい話でもしんどい話でも。"
        ),
        system_prompt,
        server_secret,
    )
    # ★2026-07-12 brain_search tool (trusted のみ)。★2026-07-13 env gate default OFF —
    #   tool 追加 (3e79391) 後 2 ターン目に通話が落ちた (会話継続不良)。原因は tool の server
    #   未解決で Vapi が dispatch 失敗。詳細 docs/failure-log.md。=1 で再有効化。
    if os.getenv("VOICE_BRAIN_SEARCH_ENABLED", "0").strip().lower() in ("1", "true", "on", "yes"):
        from services.voice_tools import attach_brain_search
        return attach_brain_search(cfg)
    return cfg


def _voice_align_assistant_dict(
    first_message: str, system_prompt: str, server_secret: str = None,
) -> dict:
    """Vapi assistant dict の共通部 (trusted / 縮退 両 config が共有)。
    server_secret は経路別分離 (phone=VAPI_SECRET / web=VAPI_WEB_SECRET)、None なら VAPI_SECRET。"""
    return {
        "firstMessage": first_message,
        "model": {
            # ★2026-07-12 §1.19③ hardcode 是正: Vapi=OpenAI直叩き (litellm不可)→env注入。default不変
            "provider": os.getenv("VAPI_LLM_PROVIDER", "openai"),
            "model": os.getenv("VAPI_LLM_MODEL", "gpt-4o"),
            "temperature": float(os.getenv("VAPI_LLM_TEMPERATURE", "0.8")),
            "messages": [
                {"role": "system", "content": system_prompt}
            ],
        },
        # ★2026-05-18: 声チューニング遍歴
        #   onyx(英語声) → Nanami(綺麗だがアナウンサー的) →
        #   Aoi(子供っぽい・遅い) → Keita(落ち着いた大人男性・相棒感)
        # ★2026-05-26 海山指示: ElevenLabs Pro Voice Clone (= 本人音声) に切替。
        # 11labs primary + Azure Keita fallback で、ElevenLabs 障害 / quota 切れ時も
        # 通話継続可能 (= Vapi の fallbackPlan が自動切替)。
        # voice ID は env override 可 (= 別声 clone 切替 / 単純 A/B テスト用)。
        # 前提: Vapi dashboard → Provider Keys に 海山 ElevenLabs API key 登録済
        # でないと 11labs 経路は使えず常に fallback 動作になる。
        "voice": _build_voice_config(),
        # ★2026-07-09 海山指示「VAPI品質の向上」: STT を nova-2 → nova-3 (multilingual経路)。
        # Deepgram 公式 (developers.deepgram.com/docs/multilingual-code-switching):
        # 日本語 (かな/漢字混在・外来語発音) 強化は model=nova-3 + language=multi が公式手順
        # (nova-3 に日本語単独 named variant は無い)。ストリーミング WER 相対-21% (Deepgram 公表)。
        # env override可 = Vapi 側が値を拒否した場合 .env で旧値 (nova-2/ja) に戻し
        # `docker compose restart line-bot` のみで即時ロールバック (rebuild 不要、コード変更なし)。
        "transcriber": {
            "provider": "deepgram",
            "model": os.getenv("VAPI_STT_MODEL", "nova-3"),
            "language": os.getenv("VAPI_STT_LANGUAGE", "multi"),
        },
        # 車内ノイズ除去 (STT 精度↑)
        "backgroundDenoisingEnabled": True,
        # ★2026-05-26 海山指示「間が不自然」 対応:
        # startSpeakingPlan = bot が話し始める前の wait 制御。waitSeconds を 0.4 → 0.8
        # に伸ばし、user 発話終了から bot 応答までの「思考の間」 を演出。
        # smartEndpointingEnabled = user 発話の真の終端を AI 検出 (= 句読点 / 沈黙 /
        # 文脈で判定)、cutting off を回避。
        "startSpeakingPlan": {
            # ★2026-05-26 5th pass: 「スピードまだ少し早い」 FB
            # waitSeconds 0.8 → 1.0 (= 思考の間 さらに延長、ゆったり感↑)
            "waitSeconds": float(os.getenv("VAPI_WAIT_SECONDS", "1.0")),
            "smartEndpointingEnabled": True,
        },
        # ★ stopSpeakingPlan = bot が user 割込で止まる threshold、控えめに (= bot が
        # 喋り続けやすくする = 細切れにならない)。
        "stopSpeakingPlan": {
            "numWords": int(os.getenv("VAPI_STOP_NUM_WORDS", "2")),
            "voiceSeconds": float(os.getenv("VAPI_STOP_VOICE_SEC", "0.3")),
            "backoffSeconds": float(os.getenv("VAPI_STOP_BACKOFF_SEC", "1.0")),
        },
        # ★2026-07-04 DA: 「終わり」「じゃあまた」は文中に自然に出る substring
        # (死生観の話題「人生の終わり」/「じゃあまた今度その話を」) で通話が
        # 途中でブツ切りされる → 明確な終話語のみに絞る。
        "endCallPhrases": [
            "またね", "切るね", "ありがとう、また",
        ],
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 2400,
        # ★2026-05-27 海山指示「音声アラインメントが蒸留に進んでない?」 対応:
        # Vapi Web SDK で end-of-call-report webhook が届かない bug の根治。
        # phone path は Vapi の電話番号設定で webhook URL が決まる、web path は
        # assistant config に server URL を明示しないと送信先不明で event drop。
        # = 5/24 以降、voice-align は走るが transcript が distill flow に乗らなかった
        # (= wiki/interview/* も 5/23 が last update で停止)。
        # secret は env VAPI_SECRET、未設定なら webhook 側で署名検証 skip (= 開発時のみ)。
        "server": {
            "url": os.getenv(
                "VAPI_SERVER_URL",
                "https://brain.example.com/webhook/voice-alignment",
            ),
            "secret": (server_secret if server_secret is not None
                       else os.getenv("VAPI_SECRET", "")),
        },
    }


@app.post("/webhook/voice-alignment")
async def voice_alignment_webhook(request: Request, bg_tasks: BackgroundTasks):
    """Vapi 電話アラインメント webhook。

    海山が車内などから番号に電話 → AI と雑談 → 性格/過去/感覚が wiki に蒸留される。

    対応 message.type:
      - assistant-request : 着信時、その場でカバレッジ最新の「聞き手」設定を返す
                            (薄い次元を自然に突くプロンプトを毎回再生成)
      - end-of-call-report: 通話 transcript → record_session + bg extract → wiki蒸留案

    認証: X-Vapi-Secret ヘッダ == env VAPI_SECRET (Vapi が送る共有シークレット)
    """
    # ★2026-05-23 海山指示: voice 経路にも bot_events 構造化ログを追加
    # (= /api/cost-investigation / recent-failures で観測可能化、5/21 以降の
    # transcript 欠落事案で「webhook 着信があったか」を直接見られるよう)
    try:
        from scripts.bot_events import log_bot_event  # type: ignore
    except Exception:
        log_bot_event = None  # type: ignore

    secret_phone = os.getenv("VAPI_SECRET", "")
    secret_web = os.getenv("VAPI_WEB_SECRET", "")
    secret_got = (
        request.headers.get("x-vapi-secret")
        or request.headers.get("X-Vapi-Secret")
        or ""
    )
    # ★2026-06-10 (Codex HIGH): fail-open → fail-closed。VAPI_SECRET 未設定だと誰でも
    # transcript を投げて LLM 蒸留 (Brain 汚染 + コスト消費) を起動できた。secret 未設定 or
    # 不一致の両方を拒否 (本番は設定済なので正常通話に影響なし、設定漏れ時の保険)。
    # ★2026-07-04 security: 2-secret 分離。web-config はブラウザに secret を返さざるを得ない
    # (Vapi Web SDK の transient assistant 仕様) ため、web には VAPI_WEB_SECRET を配り、
    # webhook はどちらの secret も受理する。ただし **assistant-request (= 深層 private の
    # 人格サマリ入り prompt を返す) は電話用 VAPI_SECRET のみ** — ブラウザ露出 secret での
    # exfiltration を遮断。VAPI_WEB_SECRET 未設定時は従来動作 (web も VAPI_SECRET)。
    # 比較は hmac.compare_digest に統一 (timing 攻撃対策、/voice-align 側と整合)。
    def _match(expected: str) -> bool:
        return bool(expected) and hmac.compare_digest(secret_got, expected)

    is_phone_secret = _match(secret_phone)
    is_web_secret = _match(secret_web)
    if not (is_phone_secret or is_web_secret):
        logger.warning(
            "[voice-align] X-Vapi-Secret 拒否 "
            f"(expected_set={bool(secret_phone)}, got={bool(secret_got)})"
        )
        if log_bot_event:
            try:
                log_bot_event(
                    "voice_alignment", "auth_failed",
                    has_secret=bool(secret_got),
                    expected_set=bool(secret_phone),
                )
            except Exception:
                pass
        raise HTTPException(status_code=401, detail="invalid secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad payload")

    msg = payload.get("message") or {}
    mtype = msg.get("type") or ""
    logger.info(f"[voice-align] message.type={mtype}")
    # ★2026-07-14 音声診断 (海山「遅すぎるかそもそも動いてない」= 上りマイク無音の切り分け):
    # speech-update の role (user 発話を VAD が検知したか) と conversation の user turn 数を
    # server 側で可視化 = スクショ無しで「声が Vapi に届いたか」をログだけで確定できる
    if mtype == "speech-update":
        logger.info(f"[voice-align]   speech {msg.get('status','?')} role={msg.get('role','?')}")
    elif mtype == "conversation-update":
        _conv = msg.get("conversation") or []
        _u = sum(1 for m in _conv if isinstance(m, dict) and m.get("role") == "user")
        logger.info(f"[voice-align]   conversation: {len(_conv)} msgs, user turns={_u}")
    if log_bot_event:
        try:
            log_bot_event("voice_alignment", "webhook_received", message_type=mtype)
        except Exception:
            pass

    import alignment_interview as ai

    # 1. 着信 → assistant 設定を動的生成 (= 深層 private の人格サマリ入り prompt)
    # ★2026-07-04 security: 電話用 VAPI_SECRET のみ許可。web は web-config 経由で取得する
    # 正規経路があり assistant-request を使わない = ブラウザ露出の VAPI_WEB_SECRET で
    # この prompt を引き出す exfiltration を遮断。
    if mtype == "assistant-request":
        # main BATCH2: 電話用 secret のみ許可 (web secret での assistant-request 引き出し遮断)。
        if not is_phone_secret:
            logger.warning("[voice-align] assistant-request を web secret で拒否")
            raise HTTPException(status_code=403, detail="phone secret required")
        # ★2026-07-04 cross-check DA: X-Vapi-Secret は「Vapi からの呼び出し」を証明する
        # だけで「電話の相手が海山」は証明しない (誤発信 / 番号漏洩で誰でも着信できる)。
        # allowlist 外の発信者には蓄積サマリ・wiki 話のタネを注入しない縮退 config。
        trusted = _voice_align_caller_trusted(msg, payload)
        if not trusted:
            logger.warning("[voice-align] allowlist 外の発信者 → 縮退 config")
            if log_bot_event:
                try:
                    log_bot_event("voice_alignment", "untrusted_caller")
                except Exception:
                    pass
        config = await _build_voice_align_assistant_config(trusted=trusted)
        return {"assistant": config}

    # ★2026-07-12 音声 Phase 1: brain_search tool 実行。phone/web どちらの secret も受理
    #   (一次ゲートは config 層 = tool は trusted config のみ。X-Vapi-Secret は
    #   assistant.server の経路別 secret が公式 fallback で届く)。深層 prompt は返さない。
    #   allowlist は判定材料 (customer.number) がある時のみ追加検査 (無い時 403 だと正規
    #   通話の tool が全滅)。ロジックは services/voice_tools (§1.12b)
    if mtype == "tool-calls":
        if not (is_phone_secret or is_web_secret):
            raise HTTPException(status_code=403, detail="secret required")
        _caller = ((msg.get("call") or payload.get("call") or {}).get("customer") or {})
        if is_phone_secret and _caller.get("number") and not _voice_align_caller_trusted(msg, payload):
            raise HTTPException(status_code=403, detail="untrusted caller")
        from services.voice_tools import handle_tool_calls
        return await handle_tool_calls(msg, getattr(request.app.state, "brain", None))

    # 2. 通話終了 → transcript 保存 + 蒸留
    # ★2026-05-18: Vapi 実ペイロードの揺れに堅牢化。
    # end-of-call-report の transcript は版により以下のどこかに入る:
    #   artifact.transcript (整形済 string) /
    #   artifact.messages [{role,message}] (role: user|bot|assistant|system) /
    #   artifact.messagesOpenAIFormatted [{role,content}] /
    #   msg.transcript
    if mtype in ("end-of-call-report", "end-of-call", "call.ended"):
        artifact = msg.get("artifact") or {}
        # ★2026-07-13 観測性: 通話終了理由 (silence-timed-out / pipeline-error-* 等) を必ず記録
        _ended = (msg.get("endedReason") or (msg.get("call") or {}).get("endedReason")
                  or artifact.get("endedReason") or "?")
        logger.info(f"[voice-align] end-of-call endedReason={_ended}")

        def _from_msgs(arr):
            out = []
            for m in arr or []:
                role = (m.get("role") or "").lower()
                if role == "system":
                    continue
                content = (
                    m.get("message")
                    or m.get("content")
                    or m.get("text")
                    or ""
                )
                if isinstance(content, list):  # OpenAI 構造化 content
                    content = " ".join(
                        c.get("text", "") for c in content
                        if isinstance(c, dict)
                    )
                content = (content or "").strip()
                if not content:
                    continue
                who = "海山" if role in ("user", "customer") else "AI"
                out.append(f"{who}: {content}")
            return "\n".join(out)

        transcript = (
            (artifact.get("transcript") or "").strip()
            or (msg.get("transcript") or "").strip()
            or _from_msgs(artifact.get("messages"))
            or _from_msgs(artifact.get("messagesOpenAIFormatted"))
            or _from_msgs(msg.get("messages"))
        )

        if not transcript.strip():
            logger.warning(
                f"[voice-align] end-of-call empty transcript "
                f"(artifact keys={list(artifact.keys())})"
            )
            return {"ok": True}

        # 短すぎる (挨拶だけ等) は蒸留しない (ノイズ防止)
        if len(transcript) < 80:
            logger.info(
                f"[voice-align] transcript too short ({len(transcript)}字), skip"
            )
            return {"ok": True}

        # ★2026-07-04 cross-check DA: allowlist 外の発信者の通話は蒸留に乗せない
        # (transcript の user 発話は「海山」として wiki/interview/ へ向かうため、
        # 他人の発話が本人像に混入する = Brain 汚染)。raw だけ隔離保存して監査可能に。
        # 蒸留 pipeline (下の record_session/冪等/録音) より前で分岐する。
        if not _voice_align_caller_trusted(msg, payload):
            logger.warning("[voice-align] allowlist 外の通話 → 蒸留 skip (raw のみ保存)")
            # record_session は session_log (= 継続性・通話回数) も更新するので使わない。
            # raw 直書きで隔離保存 (監査は可能、本人像 pipeline には乗せない)。
            try:
                import alignment_interview as _aiu
                _aiu._ensure_dirs()
                _ts = datetime.now().astimezone()
                (_aiu.RAW_DIR / f"{_ts.strftime('%Y-%m-%d-%H%M%S')}-untrusted.md").write_text(
                    "---\ntype: alignment_interview\nsource: phone-untrusted\n"
                    f"recorded_at: {_ts.isoformat(timespec='seconds')}\n"
                    "clone_visibility: private\n---\n" + transcript.strip() + "\n",
                    encoding="utf-8",
                )
            except Exception:
                logger.exception("[voice-align] untrusted raw save failed")
            if log_bot_event:
                try:
                    log_bot_event("voice_alignment", "untrusted_call_skipped")
                except Exception:
                    pass
            return {"ok": True}

        # ★2026-07-04 raw 保存は同期 (数 ms のファイル書き) してから ACK。従来は bg task
        # 内で保存していたため、ACK 後〜保存前の container restart (auto_deploy / uptime
        # monitor の force-recreate) で 40 分の通話が無痕跡で消えた。LLM 蒸留のみ bg 化。
        import alignment_interview as _ai
        # Vapi end-of-call-report の at-least-once 再送で二重取込しない冪等ガード (call id)。
        call_id = (
            ((msg.get("call") or {}).get("id"))
            or ((artifact.get("call") or {}).get("id"))
            or ""
        )
        if _ai.is_call_processed(call_id):
            logger.info(f"[voice-align] duplicate end-of-call {str(call_id)[:12]} — ack & skip")
            return {"ok": True}
        try:
            raw_path = _ai.record_session(transcript, source="phone", call_id=call_id)
            logger.info(
                f"[voice-align] recorded {raw_path.name} ({len(transcript)} chars)"
            )
            if log_bot_event:
                try:
                    log_bot_event(
                        "voice_alignment", "raw_recorded",
                        raw_file=raw_path.name, transcript_chars=len(transcript),
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.exception(f"[voice-align] record_session failed: {e}")
            try:
                from scripts.clone_improve_lib import loud_fail
                loud_fail("voice_align_ingest", False,
                          f"通話 transcript の保存に失敗 ({type(e).__name__})。"
                          "raw 未保存 = 再取得不能。", threshold=1)
            except Exception:
                pass
            return {"ok": False}
        bg_tasks.add_task(_process_voice_alignment, request.app, raw_path.name, transcript)
        # ★2026-07-04 録音の保存: 本人が自然に長時間話す一級の声データ (ElevenLabs clone の
        # 追加訓練資産 + STT 誤転写時の再転写ソース) を Vapi 側に置き去りにしない。
        rec_url = (
            artifact.get("recordingUrl")
            or artifact.get("stereoRecordingUrl")
            or ((artifact.get("recording") or {}).get("url") if isinstance(artifact.get("recording"), dict) else "")
            or ""
        )
        # ★2026-07-14 Vapi 7/15 認証必須化: rec_url が無くても call_id があれば認証 endpoint
        # で取得を試みる (webhook の recordingUrl は 7/15 以降 non-fetchable)
        if rec_url or call_id:
            bg_tasks.add_task(_save_voice_recording, request.app, raw_path.stem,
                              rec_url, str(call_id or ""))
        return {"ok": True}

    # status-update / transcript ストリーム等は ACK のみ
    return {"ok": True}


async def _save_voice_recording(app, raw_stem: str, url: str, call_id: str = "") -> None:
    """通話録音を raw/alignment_voice/recordings/<stem>.<ext> に保存。

    ★2026-07-14 Vapi の breaking change (7/15〜 認証必須化) 対応:
    - VAPI_API_KEY (private key) があれば認証 endpoint
      GET api.vapi.ai/call/{id}/mono-recording (302 → 短命 signed URL) を優先
    - 無ければ legacy 公開 URL に fallback (7/15 以降は死ぬ)
    - 失敗は §1.18 loud_fail — 録音は「本人が自然に長時間話す一級の声データ」で
      silent 消失は実害 (ElevenLabs 追加訓練資産 + STT 誤転写時の再転写ソース)
    """
    try:
        import alignment_interview as ai
        http: httpx.AsyncClient = app.state.http
        api_key = os.getenv("VAPI_API_KEY", "")
        r = None
        if api_key and call_id:
            for _ep in ("mono-recording", "stereo-recording"):
                try:
                    _rr = await http.get(
                        f"https://api.vapi.ai/call/{call_id}/{_ep}",
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=120.0, follow_redirects=True,
                    )
                    if _rr.status_code == 200 and len(_rr.content) >= 1000:
                        r = _rr
                        break
                    logger.warning(
                        f"[voice-align] auth recording {_ep}: HTTP {_rr.status_code}"
                    )
                except Exception as _e2:
                    logger.warning(f"[voice-align] auth recording {_ep} failed: {_e2}")
        if r is None:
            if not url:
                raise RuntimeError(
                    "録音の取得手段なし (VAPI_API_KEY 未設定 or 認証取得失敗、legacy URL も無し)"
                )
            r = await http.get(url, timeout=120.0, follow_redirects=True)
            r.raise_for_status()
        # ★サイズガード: stereo WAV は 40分で数百MB になり得る — メモリ/ディスク保護
        clen = int(r.headers.get("content-length") or 0)
        if clen > 200 * 1024 * 1024:
            logger.warning(f"[voice-align] recording too large ({clen//1048576}MB) — skip")
            return
        data = r.content
        if len(data) < 1000:
            logger.warning(f"[voice-align] recording too small ({len(data)}B) — skip")
            return
        if len(data) > 200 * 1024 * 1024:
            logger.warning(f"[voice-align] recording too large ({len(data)//1048576}MB) — skip")
            return
        ctype = (r.headers.get("content-type") or "").lower()
        ext = ".mp3" if "mpeg" in ctype or "mp3" in ctype else ".wav"
        rec_dir = ai.RAW_DIR / "recordings"
        rec_dir.mkdir(parents=True, exist_ok=True)
        out = rec_dir / f"{raw_stem}{ext}"
        out.write_bytes(data)
        logger.info(f"[voice-align] recording saved {out.name} ({len(data)//1024}KB)")
        try:
            from scripts.clone_improve_lib import loud_fail
            loud_fail("voice_recording_download", True, "ok")  # 成功で streak リセット
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[voice-align] recording save failed: {e}")
        # ★§1.18: Vapi 7/15 認証必須化以降、VAPI_API_KEY 未設定だと全録音が silent 消失する
        try:
            from scripts.clone_improve_lib import loud_fail
            loud_fail(
                "voice_recording_download", False,
                f"通話録音の保存失敗 ({type(e).__name__})。Vapi が 7/15 から認証必須化 — "
                ".env に VAPI_API_KEY (dashboard→API Keys の private key) が必要。",
                threshold=2, cooldown_h=24,
            )
        except Exception:
            pass


async def _process_voice_alignment(app, raw_filename: str, transcript: str) -> None:
    """raw 保存済 transcript を LLM 蒸留 → レビュー待ち + 収穫を海山へ即 push。
    ★2026-07-04: raw 保存は webhook 側で同期済。ここは蒸留と『収穫』の即時 push のみ。"""
    import alignment_interview as ai
    http: httpx.AsyncClient = app.state.http
    # ★2026-05-23 bot_events 構造化ログ (= 5/21 以降欠落事案の追跡用)
    try:
        from scripts.bot_events import log_bot_event  # type: ignore
    except Exception:
        log_bot_event = None  # type: ignore

    try:
        # coverage delta 表示用に抽出前の depth を控える
        try:
            _before = {k: v.get("depth_score", 0)
                       for k, v in ai.load_coverage().get("dimensions", {}).items()}
        except Exception:
            _before = {}
        # ★2026-07-04 系列分離: 蒸留は smart-gpt (= clone respond / wiki compile の Opus と
        # 別系列)。interviewer は海山 style を模倣して喋るため、同系列 model が自分の文体を
        # 「本人らしい」と自己増幅する echo を遮断 (hallucination check と同方針)。
        result = await ai.extract_session(
            transcript, http, LITELLM_URL, LITELLM_KEY,
            raw_filename=raw_filename, model="smart-gpt",
        )
        if result.get("error"):
            logger.warning(f"[voice-align] extract failed: {result['error']}")
            if log_bot_event:
                try:
                    log_bot_event(
                        "voice_alignment", "extract_failed",
                        raw_file=raw_filename,
                        error_msg=str(result["error"])[:200],
                    )
                except Exception:
                    pass
        else:
            n_items = len(result.get('items', []))
            logger.info(
                f"[voice-align] extracted {n_items} items, "
                f"dims={result.get('dims_with_substance')}"
            )
            if log_bot_event:
                try:
                    log_bot_event(
                        "voice_alignment", "extracted",
                        raw_file=raw_filename,
                        n_items=n_items,
                        dims=result.get("dims_with_substance"),
                    )
                except Exception:
                    pass
            # ★2026-07-04 収穫の即時 push (督促型 digest → 報酬型 feedback へ反転)。
            # 電話を切った直後に「今回の収穫 + 埋まったバー」が届く = 行動と報酬を近づけ
            # 習慣化を助ける (失速の最大因=通話後に何も返らない、の解消)。
            try:
                await _push_voice_harvest(app, result, _before)
            except Exception as _pe:
                logger.warning(f"[voice-align] harvest push failed: {_pe}")
    except Exception as e:
        logger.exception(f"[voice-align] process failed: {e}")
        if log_bot_event:
            try:
                log_bot_event(
                    "voice_alignment", "process_failed",
                    error_class=type(e).__name__,
                    error_msg=str(e)[:200],
                )
            except Exception:
                pass
        try:
            from scripts.clone_improve_lib import loud_fail
            loud_fail("voice_align_extract_proc", False,
                      f"音声アラインメント蒸留 process が例外 ({type(e).__name__})。"
                      "raw は保存済 = /api/voice-align/extract-pending で再抽出可。",
                      threshold=2, cooldown_h=24)
        except Exception:
            pass


async def _push_voice_harvest(app, result: dict, before: dict) -> None:
    """通話直後に『今回の収穫 + coverage delta』を海山へ push。"""
    if not ALIGNMENT_TARGET_USER:
        return
    import alignment_interview as ai
    n_items = len(result.get("items", []))
    dims = result.get("dims_with_substance", []) or []
    after = {}
    try:
        after = {k: v.get("depth_score", 0)
                 for k, v in ai.load_coverage().get("dimensions", {}).items()}
    except Exception:
        pass

    def _bar(n: int) -> str:
        n = max(0, min(5, int(n)))
        return "■" * n + "□" * (5 - n)

    if n_items == 0:
        # 0 item は status=empty で pending に出ない — 誤誘導 CTA を出さない (reviewer)
        await push_message(
            app.state.http, ALIGNMENT_TARGET_USER,
            "🎙️ 通話を記録した (今回は蒸留 0 件 — 短い通話?)。raw は保存済。",
        )
        return
    lines = [f"🎙️ 今回の収穫: {n_items} insight"]
    touched = []
    for did in dims:
        d = ai.DIM_BY_ID.get(did)
        if not d:
            continue
        b = before.get(did, 0)
        a = after.get(did, b)
        arrow = f"{_bar(b)}→{_bar(a)}" if a != b else _bar(a)
        touched.append(f"  {d.get('label', did)}: {arrow}")
    if touched:
        lines.append("触れた次元:")
        lines += touched
    if result.get("chunks_failed"):
        lines.append(
            f"⚠ 長通話の一部チャンク抽出失敗 ({result['chunks_failed']}/{result.get('chunks')}) — "
            "raw は完全保存済"
        )
    if result.get("truncated_chars"):
        lines.append(f"⚠ 超長通話のため末尾 {result['truncated_chars']}字 は蒸留対象外 (raw には残存)")
    lines.append("━━━━━━━━━━━━━━━")
    lines.append("採用は /align-voice (まとめて✅でOK)")
    await push_message(app.state.http, ALIGNMENT_TARGET_USER, "\n".join(lines))


# ─── Vapi Web SDK 経路 (★2026-07-13 ページ HTML/JS は services/voice_align_page.py へ移設 §1.12b。
#     背景・使い方・診断計器の説明も同 module docstring 参照) ───
@app.get("/voice-align", response_class=HTMLResponse)
async def voice_align_web_page(token: str = ""):
    """Vapi WebRTC 経由の音声アラインメント (telephony fee 不要)。

    アクセス: brain.example.com/voice-align?token=<VOICE_ALIGN_TOKEN>
    iPhone Safari でホーム画面追加すれば PWA 風アイコンに。
    """
    # ★fix 2026-05-25 BLOCKER B-2: fail-closed 化 (旧コードは expected_token 空で完全 open)
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured in .env (fail-closed for safety)")
    if not hmac.compare_digest(token or "", expected_token or ""):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>401 unauthorized</h2><p>access token 必要。</p></body></html>",
            status_code=401,
        )
    public_key = os.getenv("VAPI_PUBLIC_KEY", "")
    if not public_key:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px;max-width:640px'>"
            "<h2>VAPI_PUBLIC_KEY 未設定</h2>"
            "<p>Vapi dashboard → API Keys → Create Public Key で <code>pk_...</code> を発行し、"
            "<code>.env</code> に <code>VAPI_PUBLIC_KEY=pk_...</code> を追加 → "
            "<code>docker compose restart line-bot</code> してから再アクセス。</p>"
            "<p>(任意) ページ自体への access token: "
            "<code>VOICE_ALIGN_TOKEN=任意文字列</code> も同様に追加。URL は "
            "<code>/voice-align?token=...</code> でアクセス。</p>"
            "</body></html>",
            status_code=503,
        )
    config_url = "/api/voice-alignment/web-config"
    if expected_token:
        config_url += f"?token={token}"
    from services.voice_align_page import render_page
    return HTMLResponse(render_page(public_key, config_url))


# ─── 人格補完フォーム 50問 (★2026-07-04 海山指示「精度の高いもので続けて」) ────
# wiki 採掘 (同日) で確定した「どのデータにも無い空白」だけを狙う回答式フォーム。
# 回答は音声と同一の蒸留パイプライン (record_session → extract → レビュー → coverage 加点)。
# 本人の能動回答なので加点は正当 (受動採掘と対照的)。HTML/質問は services/persona_form.py。
@app.get("/persona-form", response_class=HTMLResponse)
async def persona_form_page(token: str = ""):
    """人格補完 50問フォーム。アクセス: /persona-form?token=<VOICE_ALIGN_TOKEN>"""
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured (fail-closed)")
    if not hmac.compare_digest(token or "", expected_token or ""):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>401 unauthorized</h2><p>access token 必要。</p></body></html>",
            status_code=401,
        )
    from services.persona_form import build_form_html
    return HTMLResponse(build_form_html(f"/api/persona-form/submit?token={token}"))


# standalone 版 (単体 HTML file、origin=null) からの POST を許すための CORS。
# 認証は token query が引き続き必須なので ACAO * でも開放にはならない。
_PF_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


@app.options("/api/persona-form/submit")
async def persona_form_preflight():
    from fastapi.responses import Response
    return Response(status_code=204, headers=_PF_CORS)


@app.post("/api/persona-form/submit")
async def persona_form_submit(request: Request, bg_tasks: BackgroundTasks, token: str = ""):
    """フォーム回答を受領 → transcript 整形 → 同期 raw 保存 → bg 蒸留 (音声と同一経路)。"""
    from fastapi.responses import JSONResponse

    def _resp(payload: dict, status: int = 200):
        return JSONResponse(payload, status_code=status, headers=_PF_CORS)

    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        return _resp({"ok": False, "detail": "VOICE_ALIGN_TOKEN not configured (fail-closed)"}, 503)
    if not hmac.compare_digest(token or "", expected_token or ""):
        return _resp({"ok": False, "detail": "invalid token"}, 401)
    try:
        payload = await request.json()
        answers = payload.get("answers") or {}
        assert isinstance(answers, dict)
    except Exception:
        return _resp({"ok": False, "detail": "bad payload"}, 400)
    from services.persona_form import format_answers_transcript
    transcript = format_answers_transcript(answers)
    if not transcript:
        return _resp({"ok": False, "detail": "no answers"}, 400)
    import alignment_interview as _ai
    try:
        raw_path = _ai.record_session(transcript, source="form")
        logger.info(f"[persona-form] recorded {raw_path.name} ({len(transcript)} chars)")
    except Exception as e:
        logger.exception(f"[persona-form] record failed: {e}")
        try:
            from scripts.clone_improve_lib import loud_fail
            loud_fail("voice_align_ingest", False,
                      f"人格フォーム回答の保存に失敗 ({type(e).__name__})。", threshold=1)
        except Exception:
            pass
        return _resp({"ok": False, "detail": "save failed"}, 500)
    bg_tasks.add_task(_process_voice_alignment, request.app, raw_path.name, transcript)
    n = sum(1 for v in answers.values() if (v or "").strip())
    return _resp({"ok": True, "saved": n, "raw": raw_path.name})


# ─── 年代記 (自伝の章) 取込 ★2026-07-05 海山指示「0-42歳の記録を人格補完に」 ───
# 二層: ①原文を wiki/interview/chronicle.md へ無加工保存 (deep-private、/diary と同じ原文主義)
#       ②蒸留は音声/フォームと同一パイプライン (chunk 分割・レビュー・coverage 加点)。
# SSH 不要の公開 API = Studio へ直接アクセスできない時でも章を積める (auto_deploy 経由で配備)。
@app.post("/api/life-story/submit")
async def life_story_submit(request: Request, bg_tasks: BackgroundTasks, token: str = ""):
    from fastapi.responses import JSONResponse

    def _resp(payload: dict, status: int = 200):
        return JSONResponse(payload, status_code=status, headers=_PF_CORS)

    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        return _resp({"ok": False, "detail": "VOICE_ALIGN_TOKEN not configured (fail-closed)"}, 503)
    if not hmac.compare_digest(token or "", expected_token or ""):
        return _resp({"ok": False, "detail": "invalid token"}, 401)
    try:
        payload = await request.json()
        title = (payload.get("title") or "").strip()
        text = (payload.get("text") or "").strip()
    except Exception:
        return _resp({"ok": False, "detail": "bad payload"}, 400)
    from services.life_story import (
        validate_chapter, sanitize_chapter, chapter_header,
        build_transcript, chronicle_frontmatter,
    )
    err = validate_chapter(title, text)
    if err:
        return _resp({"ok": False, "detail": err}, 400)

    import alignment_interview as _ai
    # ① 原文の永久保存 (chronicle wiki、章 header で冪等)
    chron = _ai.WIKI_DIR / "interview" / "chronicle.md"
    header = chapter_header(title)
    chron_state = "exists"
    try:
        if not chron.exists():
            chron.parent.mkdir(parents=True, exist_ok=True)
            chron.write_text(chronicle_frontmatter(), encoding="utf-8")
        body = chron.read_text(encoding="utf-8")
        if header not in body:
            with chron.open("a", encoding="utf-8") as f:
                f.write("\n" + header + "\n\n" + sanitize_chapter(text) + "\n")
            chron_state = "appended"
    except Exception as e:
        logger.exception(f"[life-story] chronicle write failed: {e}")
        chron_state = f"error: {type(e).__name__}"

    # ② 蒸留パイプライン (raw 同期保存 → bg 蒸留 → レビュー → coverage)
    transcript = build_transcript(title, text)
    try:
        raw_path = _ai.record_session(transcript, source="life-story")
        logger.info(f"[life-story] recorded {raw_path.name} ({len(transcript)} chars)")
    except Exception as e:
        logger.exception(f"[life-story] record failed: {e}")
        try:
            from scripts.clone_improve_lib import loud_fail
            loud_fail("voice_align_ingest", False,
                      f"年代記の保存に失敗 ({type(e).__name__})。", threshold=1)
        except Exception:
            pass
        return _resp({"ok": False, "detail": "save failed", "chronicle": chron_state}, 500)
    bg_tasks.add_task(_process_voice_alignment, request.app, raw_path.name, transcript)
    return _resp({"ok": True, "chars": len(text), "raw": raw_path.name,
                  "chronicle": chron_state})


@app.get("/api/life-story/status")
async def life_story_status(token: str = ""):
    """年代記の取込状況 (SSH 不要の確認用)。"""
    from fastapi.responses import JSONResponse
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid token")
    import json as _json
    import alignment_interview as _ai
    chron = _ai.WIKI_DIR / "interview" / "chronicle.md"
    chapters = []
    if chron.exists():
        chapters = [ln[len("# 年代記: "):] for ln in chron.read_text(encoding="utf-8").splitlines()
                    if ln.startswith("# 年代記: ")]
    sessions = []
    for f in sorted(_ai.RAW_DIR.glob("*.md"), reverse=True)[:30]:
        if "life-story" not in f.read_text(encoding="utf-8")[:400] and "年代記" not in f.read_text(encoding="utf-8")[:200]:
            continue
        ext = _ai.EXTRACTED_DIR / f"{f.stem}.json"
        st = "raw_only"
        n_items = 0
        if ext.exists():
            try:
                d = _json.loads(ext.read_text(encoding="utf-8"))
                st = d.get("status", "?")
                n_items = len(d.get("items", []))
            except Exception:
                st = "parse_error"
        sessions.append({"raw": f.name, "extract": st, "items": n_items})
    return JSONResponse({"chapters": chapters, "sessions": sessions})


@app.get("/api/voice-alignment/web-config")
async def voice_align_web_config(token: str = ""):
    """Vapi Web SDK 用の assistant 設定 (phone と同じ動的 prompt logic)。"""
    # ★fix 2026-05-25 BLOCKER B-2: fail-closed 化 (旧コードは expected_token 空で完全 open)
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured in .env (fail-closed for safety)")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid token")
    # ★2026-07-04 security: この config はブラウザにそのまま返る = server.secret は露出前提。
    # 電話用 VAPI_SECRET は絶対に含めず、web 専用 VAPI_WEB_SECRET を配る (webhook 側は
    # end-of-call は両 secret 受理 / assistant-request は電話 secret 限定)。未設定時は
    # 従来どおり VAPI_SECRET (= 挙動不変の backward-compat、設定を促す warning のみ)。
    web_secret = os.getenv("VAPI_WEB_SECRET", "")
    if not web_secret:
        logger.warning(
            "[voice-align] VAPI_WEB_SECRET 未設定 — web-config が電話用 VAPI_SECRET を"
            "返しています (露出面が広い)。.env に VAPI_WEB_SECRET を設定してください。"
        )
    # ★2026-07-04: 固定文言をやめ、phone と同じ動的 firstMessage
    # (時間帯 × 前回からの間隔 × wiki 話のタネ) に統一。server_secret の経路別分離は維持。
    # ★2026-07-12 音声 Phase 1: brain_search tool も付く (secret 非包含 = tool-calls は
    # assistant.server 経由で web secret が届く。ブラウザ露出は従来どおり server_secret のみ)。
    # ★2026-08-03: この戻り値はブラウザに平文で渡る → 最深カテゴリを除外 (voice_visibility)
    return await _build_voice_align_assistant_config(
        server_secret=(web_secret or os.getenv("VAPI_SECRET", "")),
        browser_delivered=True,
    )


# ─── Video Align (HeyGen Streaming Avatar) ★2026-05-26 海山指示 ────────
# 海山指示「本人のモーションアバターを作りたい」 (= Live video 通話用、photorealistic 画質、
# HeyGen 1 週間 trial) を受けた scaffolding。本実装は Day 1-2 並行作業、Day 3 (海山 Custom
# Avatar 完了) 後に HEYGEN_AVATAR_ID を .env 投入 → 実 avatar 動作確認に移行。
# scaffold 段階では HeyGen demo avatar (Anna_public_3) で pipeline 動作 verify 可能。

_VIDEO_ALIGN_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>video-align (海山 motion avatar、scaffold)</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif;
         background: #0b0b0f; color: #eee; margin: 0; padding: 16px;
         display: flex; flex-direction: column; align-items: center; min-height: 100vh; }
  h1 { font-size: 18px; margin: 6px 0 12px; opacity: .82; }
  .status { font-size: 13px; opacity: .65; margin-bottom: 10px; min-height: 18px; }
  .video-wrap { width: 100%; max-width: 720px; aspect-ratio: 16/9;
                background: #16161c; border-radius: 12px; overflow: hidden;
                box-shadow: 0 8px 32px rgba(0,0,0,.4); margin-bottom: 14px;
                display: flex; align-items: center; justify-content: center; }
  video { width: 100%; height: 100%; object-fit: cover; background: #16161c; }
  .ctrls { display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap;
           justify-content: center; }
  button { background: #2d6ee0; color: #fff; border: 0; border-radius: 8px;
           padding: 12px 20px; font-size: 15px; font-weight: 600; cursor: pointer;
           min-width: 120px; }
  button[disabled] { opacity: .35; cursor: not-allowed; }
  button.stop { background: #c53030; }
  textarea { width: 100%; max-width: 720px; min-height: 80px; padding: 10px;
             background: #16161c; color: #eee; border: 1px solid #2a2a36;
             border-radius: 8px; font-size: 14px; resize: vertical; box-sizing: border-box; }
  .scaffold-note { font-size: 11px; opacity: .55; max-width: 720px; line-height: 1.5;
                   text-align: left; margin-top: 16px; background: #1a1a22;
                   padding: 12px; border-radius: 8px; border-left: 3px solid #2d6ee0; }
  .scaffold-note code { background: #0b0b0f; padding: 1px 6px; border-radius: 3px;
                        font-size: 10.5px; }
</style>
</head>
<body>
  <h1>video-align <small style="opacity:.5">(scaffold mode)</small></h1>
  <div class="status" id="status">待機中…</div>
  <div class="video-wrap">
    <video id="avatar-video" autoplay playsinline></video>
  </div>
  <div class="ctrls">
    <button id="btn-start">セッション開始</button>
    <button id="btn-stop" class="stop" disabled>停止</button>
  </div>
  <textarea id="text-input" placeholder="(scaffold) avatar に そのまま喋らせる text を入力…"></textarea>
  <div class="ctrls" style="margin-top: 10px">
    <button id="btn-speak" disabled>喋らせる (echo)</button>
  </div>

  <!-- ★2026-05-27 海山指示「Brain bridge 実装」 chat section: user query → clone_respond_public → avatar speak -->
  <div style="width:100%; max-width:720px; margin-top:24px; padding-top:16px; border-top:1px solid #2a2a36;">
    <h2 style="font-size:14px; opacity:.7; margin:0 0 10px;">💭 Brain 対話 (= wiki + style 反映、海山 1:1 壁打ち)</h2>
    <div id="chat-log" style="background:#0e0e14; border:1px solid #2a2a36; border-radius:8px; padding:10px; min-height:120px; max-height:300px; overflow-y:auto; font-size:13px; line-height:1.5;"></div>
    <textarea id="chat-input" style="margin-top:10px" placeholder="壁打ちしたい内容を入力 (Enter で送信、Shift+Enter で改行)…"></textarea>
    <div class="ctrls" style="margin-top: 10px">
      <button id="btn-chat-send" disabled>Brain に聞いて avatar に喋らせる</button>
      <button id="btn-chat-clear" style="background:#444">履歴クリア</button>
    </div>
  </div>

  <div class="scaffold-note">
    <strong>scaffold note</strong>: 上 (= echo モード) は HeyGen pipeline 動作 verify 用。<br>
    下 (= Brain 対話) は Personal Brain (= wiki + style + memory) を経由した 海山 1:1 壁打ち.<br>
    現在は HeyGen demo avatar (<code>Anna_public_3</code>) で pipeline テスト中。<br>
    Day 3 (= 海山 Custom Avatar 生成完了) 後に <code>HEYGEN_AVATAR_ID</code> を
    <code>.env</code> 投入 → 海山本人 avatar に切替。<br>
    Live 会議連携 (= Vapi 音声 + HeyGen 映像 同期) は Day 4-5 で wire-up 予定。
  </div>

<script src="https://unpkg.com/@heygen/streaming-avatar@2.0.16/dist/index.umd.min.js"></script>
<script>
const TOKEN_URL = "__TOKEN_URL__";
const CONFIG_URL = "__CONFIG_URL__";
const RESPOND_URL = "__RESPOND_URL__";
const statusEl = document.getElementById("status");
const videoEl = document.getElementById("avatar-video");
const btnStart = document.getElementById("btn-start");
const btnStop = document.getElementById("btn-stop");
const btnSpeak = document.getElementById("btn-speak");
const textIn = document.getElementById("text-input");
// ★Brain bridge UI
const chatLog = document.getElementById("chat-log");
const chatInput = document.getElementById("chat-input");
const btnChatSend = document.getElementById("btn-chat-send");
const btnChatClear = document.getElementById("btn-chat-clear");

let avatar = null;
let sessionId = null;
let chatHistory = [];  // [{role:"user"|"assistant", content:"..."}]

function setStatus(s) { statusEl.textContent = s; }

async function fetchToken() {
  const r = await fetch(TOKEN_URL);
  if (!r.ok) throw new Error("token fetch failed: " + r.status);
  return (await r.json()).token;
}

async function fetchConfig() {
  const r = await fetch(CONFIG_URL);
  if (!r.ok) throw new Error("config fetch failed: " + r.status);
  return await r.json();
}

btnStart.onclick = async () => {
  try {
    btnStart.disabled = true;
    setStatus("token 取得中…");
    const token = await fetchToken();
    const cfg = await fetchConfig();
    setStatus("session 初期化中… (avatar=" + cfg.avatar_id + ")");

    avatar = new StreamingAvatar.default({ token });
    avatar.on(StreamingAvatar.StreamingEvents.STREAM_READY, (ev) => {
      if (ev.detail) { videoEl.srcObject = ev.detail; setStatus("接続済 (live stream)"); }
    });
    avatar.on(StreamingAvatar.StreamingEvents.STREAM_DISCONNECTED, () => setStatus("切断"));

    const session = await avatar.createStartAvatar({
      avatarName: cfg.avatar_id,
      quality: "high",
      voice: cfg.voice_id ? { voiceId: cfg.voice_id } : undefined,
    });
    sessionId = session.session_id;
    btnStop.disabled = false;
    btnSpeak.disabled = false;
    btnChatSend.disabled = false;
    setStatus("接続済 (session=" + sessionId.slice(0,8) + "…)");
  } catch (e) {
    setStatus("error: " + e.message);
    btnStart.disabled = false;
    console.error(e);
  }
};

btnSpeak.onclick = async () => {
  const t = textIn.value.trim();
  if (!t || !avatar) return;
  btnSpeak.disabled = true;
  try {
    await avatar.speak({ text: t, taskType: "REPEAT" });
    textIn.value = "";
    setStatus("発話送信済");
  } catch (e) {
    setStatus("speak error: " + e.message);
  }
  btnSpeak.disabled = false;
};

btnStop.onclick = async () => {
  if (!avatar) return;
  try { await avatar.stopAvatar(); } catch(e){}
  avatar = null;
  sessionId = null;
  videoEl.srcObject = null;
  btnStart.disabled = false;
  btnStop.disabled = true;
  btnSpeak.disabled = true;
  btnChatSend.disabled = true;
  setStatus("停止");
};

// ★Brain bridge: chat input → /api/video-alignment/respond → avatar.speak()
function chatAppend(role, text) {
  const div = document.createElement("div");
  div.style.marginBottom = "8px";
  div.style.padding = "6px 10px";
  div.style.borderRadius = "8px";
  div.style.maxWidth = "85%";
  if (role === "user") {
    div.style.background = "#2d6ee0";
    div.style.color = "#fff";
    div.style.marginLeft = "auto";
    div.style.textAlign = "right";
  } else if (role === "assistant") {
    div.style.background = "#23232a";
    div.style.color = "#eee";
  } else {
    div.style.background = "transparent";
    div.style.color = "#888";
    div.style.fontSize = "12px";
    div.style.textAlign = "center";
  }
  div.textContent = text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

async function chatSend() {
  const q = chatInput.value.trim();
  if (!q || !avatar) return;
  chatInput.value = "";
  chatAppend("user", q);
  chatHistory.push({ role: "user", content: q });
  btnChatSend.disabled = true;
  try {
    const r = await fetch(RESPOND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q, history: chatHistory.slice(-10) }),
    });
    if (!r.ok) {
      const err = await r.text();
      throw new Error("respond " + r.status + ": " + err);
    }
    const data = await r.json();
    const reply = (data && data.reply) || "(empty reply)";
    chatAppend("assistant", reply);
    chatHistory.push({ role: "assistant", content: reply });
    // avatar に speak させる (= REPEAT モード、Brain reply を そのまま発音)
    try {
      await avatar.speak({ text: reply, taskType: "REPEAT" });
    } catch (e) {
      chatAppend("system", "avatar.speak error: " + e.message);
    }
  } catch (e) {
    chatAppend("system", "Brain respond error: " + e.message);
  } finally {
    btnChatSend.disabled = !avatar;
  }
}

btnChatSend.onclick = chatSend;
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatSend();
  }
});
btnChatClear.onclick = () => {
  chatHistory = [];
  chatLog.innerHTML = "";
};
</script>
</body>
</html>
"""


@app.get("/video-align", response_class=HTMLResponse)
async def video_align_page(token: str = ""):
    """HeyGen Streaming Avatar の scaffold web page。

    アクセス: brain.example.com/video-align?token=<VOICE_ALIGN_TOKEN>
    (= voice-align と token を共有、別 token 用意せず統一)

    現状: HeyGen demo avatar (Anna_public_3) で pipeline 動作 verify。
    Day 3 で 海山 Custom Avatar に切替予定。
    """
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="VOICE_ALIGN_TOKEN not configured in .env (fail-closed for safety)",
        )
    if not hmac.compare_digest(token or "", expected_token or ""):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px;color:#eee;background:#0b0b0f'>"
            "<h2>401 unauthorized</h2><p>access token 必要。</p></body></html>",
            status_code=401,
        )
    heygen_key = os.getenv("HEYGEN_API_KEY", "")
    if not heygen_key:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px;max-width:640px;"
            "color:#eee;background:#0b0b0f'>"
            "<h2>HEYGEN_API_KEY 未設定</h2>"
            "<p>HeyGen dashboard → Account → API Token で取得し、<code>.env</code> に "
            "<code>HEYGEN_API_KEY=...</code> を追加 → "
            "<code>docker compose up -d --force-recreate line-bot</code> してから再アクセス。</p>"
            "<p>Day 3 後は <code>HEYGEN_AVATAR_ID=...</code> も同様に追加 (= 海山 Custom Avatar 切替)。</p>"
            "</body></html>",
            status_code=503,
        )
    qstr = f"?token={token}" if token else ""
    html = (
        _VIDEO_ALIGN_HTML
        .replace("__TOKEN_URL__", f"/api/video-alignment/heygen-token{qstr}")
        .replace("__CONFIG_URL__", f"/api/video-alignment/avatar-config{qstr}")
        # ★2026-05-27 海山指示「Brain bridge 実装」 placeholder
        .replace("__RESPOND_URL__", f"/api/video-alignment/respond{qstr}")
    )
    return HTMLResponse(html)


@app.get("/api/video-alignment/heygen-token")
async def video_align_heygen_token(token: str = ""):
    """HeyGen 短期 streaming token を返す (= browser が直接 HeyGen WebRTC に接続するため)。

    API key そのものを browser に渡さず、HeyGen の create_token で発行した
    short-lived token のみ渡すことで key 漏洩 risk を抑える。
    """
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        from services.heygen_client import create_streaming_token
        t = await create_streaming_token()
        return {"token": t}
    except Exception as e:
        logger.warning(f"[video-align] heygen token fetch failed: {e}")
        raise HTTPException(status_code=503, detail=f"HeyGen unavailable: {str(e)[:120]}")


@app.get("/api/video-alignment/avatar-config")
async def video_align_avatar_config(token: str = ""):
    """browser 側で session 起動時に渡す avatar_id + voice_id を返す。"""
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid token")
    from services.heygen_client import _resolve_avatar_id, HEYGEN_VOICE_ID
    return {
        "avatar_id": _resolve_avatar_id(),
        "voice_id": HEYGEN_VOICE_ID or "",
        "using_demo": not bool(os.getenv("HEYGEN_AVATAR_ID", "")),
    }


# ★2026-05-27 海山指示「Brain bridge 実装」: chat box 経由 user query → clone_respond_public → reply
class VideoAlignRespondRequest(BaseModel):
    query: str
    history: Optional[list] = None  # [{"role": "user"|"assistant", "content": "..."}, ...]


@app.post("/api/video-alignment/respond")
async def video_align_respond(req: VideoAlignRespondRequest, token: str = ""):
    """user text → Brain clone_respond_public で reply text → browser 側で avatar.speak() に push.

    Phase A 仕様: stream なし、reply text 全体を return。
    Phase B (= 保留): LLM stream + Deepgram 等で latency 短縮.

    既存 /voice-align (= Vapi voice) と並列。本 endpoint は HeyGen avatar の text bridge.
    """
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid token")
    query = (req.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="empty query")
    brain: BrainWiki = app.state.brain
    # 既存 _safe_clone_respond (= Semaphore + empty guard) 経由
    # model = env で選択可能 (= smart=海山らしさ / fast-gpt=latency)
    model = os.getenv(
        "VIDEO_ALIGN_MODEL",
        os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart"),
    )
    reply = await _safe_clone_respond(
        brain, query,
        history=req.history or [],
        model=model,
        # video-align は 海山個人壁打ち用途、user_id は固定 sentinel
        user_id="video_align_local",
        user_display="海山 (壁打ち)",
    )
    return {"reply": reply}


@app.get("/api/video-alignment/healthcheck")
async def video_align_healthcheck(token: str = ""):
    """HeyGen 接続性 + 利用可能 avatar 一覧 (= debug / monitoring 用)。"""
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        from services.heygen_client import healthcheck
        return await healthcheck()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ─── Clone Voice (ElevenLabs Pro Voice Clone TTS) ★2026-05-21 / 復旧 2026-05-25 ───
# 海山さんの Pro Voice Clone を試聴・運用するための endpoints。
# Phase 1 (現状): 海山さん自身が UI で品質確認、社員配信は未接続。
# Phase 2 (将来): clone_respond_public の応答に音声出力経路を接続 (LINE Works
#                  audio message)。まず品質判断 + use case 確定してから繋ぐ。

_CLONE_VOICE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>うみやま 声 試聴</title>
<style>
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Hiragino Sans", sans-serif;
    padding: 32px 20px; max-width: 720px; margin: 0 auto;
    background: #f5f5f7; color: #1d1d1f;
  }
  h1 { font-size: 22px; margin-bottom: 6px; }
  .sub { color: #86868b; font-size: 13px; margin-bottom: 24px; }
  textarea {
    width: 100%; min-height: 140px; padding: 12px;
    font-size: 15px; border: 1px solid #d2d2d7; border-radius: 10px;
    font-family: inherit; resize: vertical;
  }
  .row { margin: 16px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  label { font-size: 13px; color: #424245; }
  select {
    padding: 9px 12px; border-radius: 8px; border: 1px solid #d2d2d7;
    background: white; font-size: 14px;
  }
  button {
    padding: 11px 24px; background: #007aff; color: white;
    border: none; border-radius: 8px; font-size: 15px; font-weight: 600;
    cursor: pointer; transition: opacity 0.15s;
  }
  button:hover:not(:disabled) { opacity: 0.85; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  .status { color: #86868b; font-size: 13px; min-height: 18px; }
  audio { width: 100%; margin-top: 18px; }
  .meta {
    margin-top: 8px; font-size: 12px; color: #86868b;
    font-family: ui-monospace, "SF Mono", monospace;
  }
  .preset {
    padding: 7px 12px; background: white; border: 1px solid #d2d2d7;
    border-radius: 16px; font-size: 12px; cursor: pointer;
    color: #424245;
  }
  .preset:hover { background: #f0f0f0; }
</style>
</head>
<body>
<h1>うみやま 声 試聴</h1>
<p class="sub">テキスト → 海山さんの声 (Pro Voice Clone) で読み上げ。社員配信は別 phase、まず品質判断用。</p>

<textarea id="text" placeholder="ここにテキストを入力">武蔵小山パルムの今日の売上は19,727円、客数は1人だね。少し小さくまとまってる感じ。明日の動きを見て、必要なら声かけるよ。</textarea>

<div class="row">
  <button class="preset" data-text="武蔵小山パルムの今日の売上は19,727円、客数は1人だね。少し小さくまとまってる感じ。明日の動きを見て、必要なら声かけるよ。">業績コメント</button>
  <button class="preset" data-text="お疲れさま。今日もありがとう。明日も頼りにしてる。">短い激励</button>
  <button class="preset" data-text="OWNDAYS の Vision は「OWNDAYS に関わる全ての人を豊かにする」。これが原点で、ここから全部派生してる。">VMV 説明</button>
  <button class="preset" data-text="Yesterday we closed at three point two billion yen, which is roughly a five percent increase year on year. Strong performance in Japan, slightly soft in Southeast Asia.">English</button>
</div>

<div class="row">
  <label for="model">モデル:</label>
  <select id="model">
    <option value="eleven_multilingual_v2" selected>Multilingual v2 (高品質、2 credits/char)</option>
    <option value="eleven_turbo_v2_5">Turbo v2.5 (低コスト、0.5 credits/char、低レイテンシ)</option>
  </select>
  <button id="play">▶ 読み上げ</button>
  <span class="status" id="status"></span>
</div>

<audio id="audio" controls></audio>
<div class="meta" id="meta"></div>

<script>
  const TOKEN = "__TOKEN__";
  const textEl = document.getElementById("text");
  const modelEl = document.getElementById("model");
  const btn = document.getElementById("play");
  const status = document.getElementById("status");
  const audio = document.getElementById("audio");
  const meta = document.getElementById("meta");

  for (const p of document.querySelectorAll(".preset")) {
    p.addEventListener("click", () => { textEl.value = p.dataset.text; });
  }

  btn.addEventListener("click", async () => {
    const text = textEl.value.trim();
    const model = modelEl.value;
    if (!text) { status.textContent = "テキスト未入力"; return; }
    if (text.length > 2000) { status.textContent = "2000字以内で"; return; }

    const credits = Math.ceil(text.length * (model === "eleven_turbo_v2_5" ? 0.5 : 2));
    btn.disabled = true;
    status.textContent = "生成中…";
    meta.textContent = `${text.length} 文字 × ${model === "eleven_turbo_v2_5" ? "0.5" : "2"} = 約 ${credits} credits 消費`;

    // ★fix 2026-05-25 MUST-FIX M-2: GET → POST、token は Authorization Bearer header
    // (text を URL に乗せない、access log / referrer 漏洩防止)
    const t0 = performance.now();
    try {
      const r = await fetch("/api/clone-voice/test", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": TOKEN ? `Bearer ${TOKEN}` : "",
        },
        body: JSON.stringify({text: text, model: model}),
      });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { const j = await r.json(); msg += `: ${j.detail || JSON.stringify(j)}`; }
        catch { msg += `: ${(await r.text()).slice(0, 120)}`; }
        status.textContent = `✗ ${msg}`;
        return;
      }
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      // 前回 blob URL 破棄 (メモリリーク防止)
      if (audio.src && audio.src.startsWith("blob:")) URL.revokeObjectURL(audio.src);
      audio.src = url;
      const ms = Math.round(performance.now() - t0);
      status.textContent = `✓ 生成完了 (${ms}ms, ${(blob.size/1024).toFixed(1)}KB)`;
      audio.play().catch((e) => { status.textContent = "再生失敗: " + e.message; });
    } catch (e) {
      status.textContent = "エラー: " + e.message;
    } finally {
      btn.disabled = false;
    }
  });
</script>
</body>
</html>
"""


@app.get("/clone-voice", response_class=HTMLResponse)
async def clone_voice_page(token: str = ""):
    """うみやま声 TTS の試聴 web ページ。"""
    # ★fix 2026-05-25 BLOCKER B-2: fail-closed 化 (旧コードは expected_token 空で完全 open)
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured in .env (fail-closed for safety)")
    if not hmac.compare_digest(token or "", expected_token or ""):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>401 unauthorized</h2><p>?token=... を URL に。</p></body></html>",
            status_code=401,
        )
    return HTMLResponse(
        _CLONE_VOICE_HTML.replace("__TOKEN__", token if expected_token else "")
    )


class CloneVoiceTestReq(BaseModel):
    """POST /api/clone-voice/test の body。"""
    text: str
    model: str = "eleven_multilingual_v2"


@app.post("/api/clone-voice/test")
async def clone_voice_test(
    req: CloneVoiceTestReq,
    authorization: str = Header(default=""),
):
    """テキストを海山声 mp3 で返す (試聴用、Pro Voice Clone)。

    ★fix 2026-05-25 MUST-FIX M-2: GET → POST 化。GET だと text が URL に乗り
    server access log / referrer / browser history に保存される + 副作用 (= ElevenLabs 課金)
    のある操作は GET 禁止。token も Authorization Bearer header に移動。

    ★fix 2026-05-25 MUST-FIX M-1: ElevenLabs 429 (rate limit) / 402 (quota) を
    別 exception で typed handling → user に正しい HTTP status code 返却。

    ★fix 2026-05-25 MUST-FIX M-3: pre-flight で daily credit cap check
    (clone_voice._check_and_record_credits)、超過なら 402 で reject。
    """
    # ★Bearer token 認証 (fail-closed)
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured in .env (fail-closed for safety)")
    if not hmac.compare_digest(token or "", expected_token or ""):
        raise HTTPException(status_code=401, detail="invalid Bearer token")

    text = (req.text or "").strip()
    model = req.model or "eleven_multilingual_v2"
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long (max 2000)")

    import clone_voice
    if not clone_voice.is_configured():
        raise HTTPException(
            status_code=503,
            detail="ELEVENLABS_API_KEY / ELEVENLABS_VOICE_ID 未設定 (.env 投入 + restart)"
        )

    http: httpx.AsyncClient = app.state.http
    try:
        audio = await clone_voice.tts_bytes(text, http, model=model)
    except clone_voice.ElevenLabsRateLimit as e:
        logger.warning(f"[clone-voice] 429 rate limit: {e}")
        raise HTTPException(
            status_code=429,
            detail="ElevenLabs rate limit (短時間アクセス集中)。数分後に再試行してください。",
        )
    except clone_voice.ElevenLabsQuotaExceeded as e:
        logger.error(f"🚨 [clone-voice] quota exhausted: {e}")
        raise HTTPException(
            status_code=402,
            detail=f"credit 枯渇 (daily cap or 月次 quota 到達)。本日 reset まで停止: {e}",
        )
    except httpx.HTTPStatusError as e:
        logger.warning(
            f"[clone-voice] ElevenLabs API {e.response.status_code}: "
            f"{e.response.text[:300]}"
        )
        raise HTTPException(
            status_code=502,
            detail=f"ElevenLabs API error {e.response.status_code}",
        )
    except Exception as e:
        logger.exception(f"[clone-voice] TTS failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


# ─── Clone Alignment Trial レビュー HTML serve ★2026-05-21 ───
# clone_alignment_trial.py が data/brain/clone_improve/alignment_trial/runs/
# 配下に生成する review HTML を MacBook 等の別端末から開くためのエンドポイント。
# 認証は VOICE_ALIGN_TOKEN を流用 (同じ「海山さん専用の内部レビュー UI」枠)。
# URL 例: https://brain.example.com/alignment-trial/2026-05-21_run1?token=<VOICE_ALIGN_TOKEN>

@app.get("/alignment-trial/{run_id}", response_class=HTMLResponse)
async def alignment_trial_html(run_id: str, token: str = ""):
    """clone_alignment_trial の review HTML を返す (MacBook 等の別端末用)。"""
    # ★fix 2026-05-25 BLOCKER B-2: fail-closed 化 (旧コードは expected_token 空で完全 open)
    expected_token = os.getenv("VOICE_ALIGN_TOKEN", "")
    if not expected_token:
        raise HTTPException(status_code=503, detail="VOICE_ALIGN_TOKEN not configured in .env (fail-closed for safety)")
    if not hmac.compare_digest(token or "", expected_token or ""):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2>401 unauthorized</h2><p>?token=... を URL に付けてください。</p>"
            "</body></html>",
            status_code=401,
        )
    # path traversal 防止 (run_id は alphanumeric + - _ . のみ)
    if not re.fullmatch(r"[\w._-]+", run_id):
        return HTMLResponse(
            "<html><body><h2>400</h2><p>invalid run_id</p></body></html>",
            status_code=400,
        )
    fp = Path(f"/app/data/brain/clone_improve/alignment_trial/runs/{run_id}.html")
    if not fp.exists():
        return HTMLResponse(
            f"<html><body style='font-family:sans-serif;padding:40px'>"
            f"<h2>404 not found</h2>"
            f"<p><code>{fp.name}</code> がまだ存在しません。"
            f"clone_alignment_trial の完走を待つか、別 run_id を試してください。</p>"
            f"</body></html>",
            status_code=404,
        )
    return HTMLResponse(fp.read_text(encoding='utf-8'))


# ─── Personal Brain リモート MCP (Claude スマホアプリ用) ★2026-05-18 ───
# 既存 Cloudflare tunnel (brain.example.com) に MCP Streamable-HTTP を生やし、
# Claude スマホアプリの custom connector として登録 → 音声雑談中に Claude が
# 過去 wiki / カバレッジを参照できる (= 会話の継続性、「前回の続き」が成立)。
# mcp SDK 非依存の素 JSON-RPC 実装。read-only ツールのみ (安全)。
_MCP_TOOLS = [
    {
        "name": "brain_search",
        "description": "Personal Brain (海山の知識ベース) をベクトル検索。過去の判断・人物・プロジェクト・価値観を調べる。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索クエリ"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "brain_wiki_read",
        "description": "Brain Wiki の特定ファイルを読む (例: identity.md, thinking.md, interview/biography.md)。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "brain_wiki_list",
        "description": "Brain Wiki のファイル一覧 (subdir 指定可、例: interview)。",
        "inputSchema": {
            "type": "object",
            "properties": {"subdir": {"type": "string", "default": ""}},
        },
    },
    {
        "name": "alignment_coverage",
        "description": "アラインメント雑談のカバレッジ。どの本人像の次元が薄い=次に話すと効くかを返す。雑談の冒頭で呼んで話題誘導に使う。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "alignment_recent",
        "description": "これまでの雑談で何を蓄積したか (wiki/interview/ の各カテゴリ要約)。会話の継続性のため冒頭で呼ぶ。「前回の○○の続き」を成立させる。",
        "inputSchema": {
            "type": "object",
            "properties": {"max_chars_per_cat": {"type": "integer", "default": 800}},
        },
    },
]


async def _mcp_call_tool(app, name: str, args: dict) -> str:
    """MCP tool 実行 → テキスト返す。read-only のみ。"""
    from brain_wiki import WIKI_DIR
    if name == "brain_search":
        q = args.get("query", "")
        n = int(args.get("max_results", 5))
        idx = getattr(app.state, "brain_index", None)
        if idx is None:
            return "(brain_index 未初期化)"
        hits = await idx.search(q, n_results=n, collection="wiki")
        if not hits:
            return f"「{q}」に該当なし。"
        out = [f"## Brain 検索: 「{q}」\n"]
        for i, h in enumerate(hits[:n], 1):
            src = (h.get("source") or h.get("metadata", {}).get("file") or "?")
            doc = (h.get("content") or h.get("document") or h.get("text") or "")[:700]
            out.append(f"### [{i}] {src}\n{doc}\n")
        return "\n".join(out)
    if name == "brain_wiki_read":
        path = args.get("path", "")
        fp = (WIKI_DIR / path)
        if not fp.exists():
            cands = list(WIKI_DIR.rglob(path)) or list(WIKI_DIR.rglob(f"*{path}*"))
            if not cands:
                return f"ファイル未発見: {path} (brain_wiki_list で一覧確認)"
            fp = cands[0]
        try:
            _relp = fp.resolve().relative_to(WIKI_DIR.resolve())
        except Exception:
            return "不正なパス"
        # ★2026-06-28 personal ドメイン分離: 非OWNDAYS PJ は内部 wiki tool 経由で返さない (/personal 専用)
        # ★2026-07-03 (v3 ADR DA R6): interview/ は**意図的に除外しない**。/mcp/brain は海山本人の
        #   スマホ connector (BRAIN_MCP_TOKEN = 海山のみ) で、alignment 雑談の継続性のために
        #   interview/ 参照が設計意図 (alignment_recent tool も同カテゴリを返す)。海山 admin 経路。
        if _relp.parts[:1] == ("personal",):
            return "personal ドメイン (非OWNDAYS) は /personal モード専用です。"
        return f"## {fp.relative_to(WIKI_DIR)}\n\n{fp.read_text(encoding='utf-8')[:12000]}"
    if name == "brain_wiki_list":
        sub = args.get("subdir", "") or ""
        base = (WIKI_DIR / sub) if sub else WIKI_DIR
        if not base.exists():
            return f"ディレクトリ無し: {sub}"
        # ★2026-06-28 personal ドメイン分離: 一覧に非OWNDAYS PJ を出さない
        files = sorted(
            str(p.relative_to(WIKI_DIR)) for p in base.rglob("*.md")
            if p.relative_to(WIKI_DIR).parts[:1] != ("personal",)
        )
        return f"## Wiki ファイル ({len(files)})\n" + "\n".join(files[:300])
    if name == "alignment_coverage":
        try:
            import alignment_interview as _ai
            return _ai.build_status_text()
        except Exception as e:
            return f"(coverage 取得失敗: {e})"
    if name == "alignment_recent":
        cap = int(args.get("max_chars_per_cat", 800))
        idir = WIKI_DIR / "interview"
        if not idir.exists():
            return "(まだ雑談蓄積なし。初回。)"
        parts = ["## これまで蓄積した本人像 (wiki/interview/)\n"]
        for f in sorted(idir.glob("*.md")):
            try:
                body = f.read_text(encoding="utf-8")
            except Exception:
                continue
            # frontmatter 除去して本文だけ
            if body.startswith("---"):
                end = body.find("\n---", 3)
                if end > 0:
                    body = body[end + 4:]
            parts.append(f"### {f.stem}\n{body.strip()[:cap]}\n")
        return "\n".join(parts) if len(parts) > 1 else "(まだ雑談蓄積なし。初回。)"
    return f"Unknown tool: {name}"


@app.api_route("/mcp/brain", methods=["POST", "GET"])
async def mcp_brain_endpoint(request: Request):
    """Personal Brain リモート MCP (Streamable-HTTP、JSON-RPC 2.0)。
    Claude スマホアプリの custom connector 用。read-only。
    認証: Authorization: Bearer <BRAIN_MCP_TOKEN> または ?token=
    """
    from fastapi.responses import JSONResponse, Response

    token_expected = os.getenv("BRAIN_MCP_TOKEN", "")
    if not token_expected:
        raise HTTPException(status_code=503, detail="BRAIN_MCP_TOKEN unset")
    auth = request.headers.get("Authorization", "")
    got = auth[7:] if auth.startswith("Bearer ") else (
        request.query_params.get("token", "")
    )
    if not got or not hmac.compare_digest(got, token_expected):
        raise HTTPException(status_code=401, detail="invalid token")

    # GET = SSE オープン試行。ステートレスなので即 200 (空 SSE)。
    if request.method == "GET":
        return Response(status_code=200, media_type="text/event-stream")

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "parse error"}},
            status_code=400,
        )

    rid = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}

    # 通知 (id 無し) は 202 で握る
    if rid is None and method.startswith("notifications/"):
        return Response(status_code=202)

    def ok(result):
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})

    if method == "initialize":
        proto = params.get("protocolVersion") or "2025-06-18"
        return ok({
            "protocolVersion": proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "personal-brain", "version": "1.0.0"},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": _MCP_TOOLS})
    if method == "tools/call":
        tname = params.get("name", "")
        targs = params.get("arguments") or {}
        try:
            text = await _mcp_call_tool(request.app, tname, targs)
            return ok({"content": [{"type": "text", "text": text}],
                       "isError": False})
        except Exception as e:
            logger.warning(f"[mcp/brain] tool {tname} failed: {e}")
            return ok({"content": [{"type": "text",
                                    "text": f"tool error: {e}"}],
                       "isError": True})

    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid,
         "error": {"code": -32601, "message": f"method not found: {method}"}},
    )


async def _process_recall_event(app, event: str, payload: dict) -> None:
    """Recall.ai イベントを処理: transcript fetch → meeting note 生成"""
    brain = app.state.brain
    privacy = app.state.privacy
    http: httpx.AsyncClient = app.state.http
    data = payload.get("data") or {}
    bot = data.get("bot") or {}
    bot_id = bot.get("id", "unknown")

    # 1. recording.done → 音声 URL を raw に記録のみ
    if event in ("bot.done", "recording.done", "bot.recording.done"):
        recording_url = (
            data.get("recording", {}).get("url")
            or bot.get("recording_url")
            or ""
        )
        meeting_url = bot.get("meeting_url") or ""
        meta_dir = Path("/app/data/brain/raw/voice/recall")
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_file = meta_dir / f"{date.today().isoformat()}-{bot_id}.recording.json"
        meta_file.write_text(
            json.dumps(
                {
                    "bot_id": bot_id,
                    "recording_url": recording_url,
                    "meeting_url": meeting_url,
                    "captured_at": datetime.now().isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        logger.info(f"[Recall] recording metadata saved: {meta_file.name}")
        return

    # 2. transcript.done → transcript fetch → meeting note 生成
    if event not in ("transcript.done", "transcription.done", "bot.transcription.done"):
        logger.info(f"[Recall] event {event} ignored")
        return

    transcript_url = (
        data.get("transcript", {}).get("url")
        or data.get("transcript_url")
        or ""
    )
    if not transcript_url:
        logger.warning("[Recall] transcript event without URL")
        return

    try:
        transcript_json = await _fetch_recall_transcript(http, transcript_url)
    except Exception as e:
        logger.error(f"[Recall] transcript fetch failed: {e}")
        return

    transcript_text, participants = _build_transcript_text(transcript_json)
    if not transcript_text.strip():
        logger.warning(f"[Recall] empty transcript for bot {bot_id}")
        return

    # PrivacyGate に流す (会議 transcript も既存の 5 カテゴリでフィルタ)
    try:
        result = await privacy.filter(transcript_text, sender_id=f"recall_{bot_id}")
        if result.verdict.value != "allow":
            logger.info(f"[Recall] transcript blocked by PrivacyGate (bot {bot_id})")
            return
        filtered_text = result.sanitized
    except Exception as e:
        logger.warning(f"[Recall] PrivacyGate error: {e}, passing through")
        filtered_text = transcript_text

    # メタデータ
    meeting_meta = bot.get("meeting_metadata") or {}
    title = (
        meeting_meta.get("title")
        or bot.get("meeting_url", "").split("/")[-1]
        or f"recall_meeting_{bot_id[:8]}"
    )
    meeting_date = (
        bot.get("created_at", "")[:10]
        or date.today().isoformat()
    )
    # 時間 (recording_duration in seconds → minutes)
    duration_s = data.get("recording", {}).get("duration") or bot.get("duration", 0)
    duration_min = int(duration_s / 60) if duration_s else None

    try:
        wiki_file = await brain.compile_meeting_note(
            transcript=filtered_text,
            source="recall",
            title=title,
            meeting_date=meeting_date,
            participants=participants,
            duration_minutes=duration_min,
            audio_paths=[data.get("recording", {}).get("url") or ""] if data.get("recording") else None,
            preserve_audio=False,
            extra_metadata={
                "recall_bot_id": bot_id,
                "meeting_url": bot.get("meeting_url", ""),
            },
        )
        logger.info(f"[Recall] meeting note created: {wiki_file.name}")
    except Exception as e:
        logger.error(f"[Recall] compile_meeting_note failed: {e}")


# ─── Plaud / 汎用音声 transcript の手動取り込み API ───


@app.post("/api/meeting/ingest")
async def api_meeting_ingest(request: Request, _: str = Depends(require_api_key)):
    """対面会議 transcript の取り込み API (Plaud / Owl / 手動 export 等の経路で使う)。

    body (JSON):
      transcript: str  (必須、PrivacyGate 通過前でよい、内部でフィルタ実施)
      source: str      (例: 'plaud' 'owl' 'manual')
      title: str       (任意)
      date: str        (任意、YYYY-MM-DD)
      participants: list[str] (任意)
      duration_minutes: int   (任意)
      preserve_audio: bool    (任意、True で voice clone 削除対象外)
    """
    data = await request.json()
    transcript = (data.get("transcript") or "").strip()
    source = data.get("source") or "manual"
    if not transcript:
        raise HTTPException(status_code=400, detail="transcript required")

    brain = request.app.state.brain
    privacy = request.app.state.privacy

    try:
        result = await privacy.filter(transcript, sender_id=f"{source}_meeting")
        if result.verdict.value != "allow":
            return {"ok": False, "reason": "blocked_by_privacy", "verdict": result.verdict.value}
        filtered = result.sanitized
    except Exception as e:
        logger.warning(f"[meeting_ingest] PrivacyGate error: {e}")
        # ★2026-07-03 meeting-autojoin DA R3: 自動参加 (recall) は fail-closed —
        # gate 停止中に無フィルタで wiki 入りさせない。poller が次 cycle で retry する
        # (attempts cap 有)。Plaud/manual (海山が能動的に投げる) は従来どおり passthrough。
        if source == "recall":
            raise HTTPException(status_code=503, detail="privacy gate unavailable (retry later)")
        filtered = transcript

    wiki_file = await brain.compile_meeting_note(
        transcript=filtered,
        source=source,
        title=data.get("title") or "",
        meeting_date=data.get("date"),
        participants=data.get("participants"),
        duration_minutes=data.get("duration_minutes"),
        preserve_audio=bool(data.get("preserve_audio", False)),
        extra_metadata=data.get("extra_metadata"),
    )
    return {
        "ok": True,
        "wiki_file": str(wiki_file.relative_to(Path("/app/data/brain"))),
    }


# ─── チャットエクスポート取り込みエンドポイント ───
MAX_IMPORT_SIZE = 50 * 1024 * 1024  # 50MB


@app.post("/import")
async def import_chat_export(
    request: Request,
    file: UploadFile = File(...),
    _: str = Depends(require_api_key),
):
    """LINE チャットエクスポート .txt をアップロードして Brain に取り込む"""
    # Path traversal防止: basename抽出 + sanitize
    raw_name = file.filename or "upload.txt"
    safe_name = Path(raw_name).name  # basename only、../ を除去
    safe_name = "".join(
        c for c in safe_name if c.isalnum() or c in "._-" or ord(c) > 127
    )
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if "/" in safe_name or "\\" in safe_name or safe_name.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = IMPORT_DIR / safe_name

    # IMPORT_DIR内に収まることを確認
    try:
        tmp_path.resolve().relative_to(IMPORT_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal detected")

    content = await file.read()
    if len(content) > MAX_IMPORT_SIZE:
        raise HTTPException(status_code=413, detail="File too large")

    tmp_path.write_bytes(content)

    result = await process_chat_export(
        tmp_path, request.app.state.privacy, request.app.state.brain
    )

    # 処理済みディレクトリに移動
    processed_dir = IMPORT_DIR / "processed"
    processed_dir.mkdir(exist_ok=True)
    tmp_path.rename(processed_dir / tmp_path.name)

    return {"status": "ok", **result}


# ─── Chrome 拡張 (Brain Clipper) 受信エンドポイント ───
@app.post("/api/ingest")
async def api_ingest(request: Request, _: str = Depends(require_api_key)):
    """Chrome拡張からClaude/ChatGPTの会話を受信し、BrainWikiに取り込む"""
    data = await request.json()
    source = data.get("source", "unknown")       # "claude" or "chatgpt"
    title = data.get("title", "Untitled")
    messages = data.get("messages", [])
    url = data.get("url", "")
    timestamp = data.get("timestamp", datetime.now().isoformat())

    if not messages:
        raise HTTPException(status_code=400, detail="No messages")

    # 会話をMarkdownテキストに変換
    lines = [
        f"# {title}",
        f"source: {source} | {url}",
        f"date: {timestamp[:10]}",
        "",
    ]
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"**User**: {content}")
        elif role == "assistant":
            lines.append(f"**AI ({source})**: {content}")
        else:
            lines.append(content)
        lines.append("")

    raw_text = "\n".join(lines)

    # PrivacyGate → BrainWiki
    privacy = request.app.state.privacy
    brain = request.app.state.brain

    allowed = 0
    blocked = 0

    # メッセージ単位でフィルタリング
    filtered_lines = [lines[0], lines[1], lines[2], ""]
    for msg in messages:
        content = msg.get("content", "")
        if not content.strip():
            continue
        result = await privacy.filter(content, sender_id=f"ext_{source}")
        if result.verdict.value == "allow":
            role = msg.get("role", "unknown")
            label = "User" if role == "user" else f"AI ({source})"
            filtered_lines.append(f"**{label}**: {result.sanitized}")
            filtered_lines.append("")
            allowed += 1
        else:
            blocked += 1

    if allowed > 0:
        # raw に保存してコンパイル
        slug = title.replace(" ", "_")[:40] or source
        safe_slug = "".join(c for c in slug if c.isalnum() or c in "_-")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{source}_{safe_slug}_{ts}"
        await brain.ingest_note(
            f"ext_{source}",
            "\n".join(filtered_lines),
            title=filename,
        )

    logger.info(f"[BrainClipper] {source}: {title[:30]} — {allowed} allowed, {blocked} blocked")

    # ─── Claude/ChatGPT の会話ターンごとに不満足検知 → 自動改善 ───
    # Chrome拡張で取り込まれる会話も「自分の質問に対する回答」として評価対象にする
    async def _scan_claude_conversation():
        try:
            prev_user = ""
            triggered_count = 0
            for i, msg in enumerate(messages):
                role = msg.get("role", "")
                content = msg.get("content", "")
                if not content.strip():
                    continue
                if role == "user":
                    prev_user = content
                    continue
                if role == "assistant" and prev_user:
                    result = await detect_and_improve(
                        request.app,
                        source=f"claude_ingest:{source}",
                        user_id=f"ext_{source}",
                        user_msg=prev_user,
                        ai_reply=content,
                        prev_user_msg="",  # 繰返し検知は Claude 側では弱くする
                        force=False,
                    )
                    if result.get("triggered"):
                        triggered_count += 1
                    prev_user = ""  # consumed
            if triggered_count:
                logger.info(
                    f"[BrainClipper] auto_improve triggered on {triggered_count} turn(s)"
                )
        except Exception as e:
            logger.warning(f"[BrainClipper] auto_improve scan failed: {e}")

    asyncio.create_task(_scan_claude_conversation())

    return {
        "status": "ok",
        "allowed": allowed,
        "blocked": blocked,
        "source": source,
    }


# ─── Claude Code dispatcher からの結果受信 ───
@app.post("/api/claude/notify")
async def api_claude_notify(request: Request, _: str = Depends(require_api_key)):
    """claude_dispatcher.py が `claude -p` の結果を POST してくる。

    mode="plan"    → plan を Redis に保存 + Quick Reply 付きで LINE Push
    mode="execute" → 実行結果を LINE Push で返す
    """
    data = await request.json()
    task_id = data.get("task_id", "")
    user_id = data.get("user_id", "")
    mode = data.get("mode", "plan")
    ok = data.get("ok", False)
    stdout = (data.get("stdout") or "").strip()
    stderr = (data.get("stderr") or "").strip()
    elapsed = data.get("elapsed", 0)
    err = data.get("error", "")
    instruction = data.get("instruction", "")

    app_state = request.app.state
    r_conn = app_state.redis
    http = app_state.http

    if not user_id:
        return {"status": "no_user"}

    # 失敗時
    if not ok:
        msg = err or stderr or stdout or "unknown"
        if len(msg) > 3800:
            msg = msg[:3800] + "…"
        await push_message(
            http, user_id,
            f"⚠️ Claude Code 失敗 ({mode})\n━━━━━━━━━━━━━━━\n{msg}",
        )
        return {"status": "failed_pushed"}

    # 成功時: mode で分岐
    if mode == "plan":
        plan_text = stdout or "(プラン出力が空でした)"
        # Redis に保存（承認時に execute で使う）
        await r_conn.setex(
            f"claude_plan:{task_id}",
            CLAUDE_PLAN_TTL,
            json.dumps(
                {
                    "task_id": task_id,
                    "instruction": instruction,
                    "plan": plan_text,
                    "created_at": datetime.now().isoformat(),
                },
                ensure_ascii=False,
            ),
        )
        # LINE Push with Quick Reply
        body = plan_text
        if len(body) > 3600:
            body = body[:3600] + "\n\n…(省略: 全文はサーバログ)"
        await push_message(
            http, user_id,
            f"📋 Claude Code プラン ({elapsed}s)\n━━━━━━━━━━━━━━━\n{body}\n\n"
            "この計画で進めますか？",
            quick_reply=_plan_quick_reply(task_id),
        )
        return {"status": "plan_pushed"}

    # execute
    body = stdout or "(出力なし)"
    if len(body) > 3800:
        body = body[:3800] + "\n\n…(省略)"
    await push_message(
        http, user_id,
        f"✅ Claude Code 実装完了 ({elapsed}s)\n━━━━━━━━━━━━━━━\n{body}",
    )
    return {"status": "execute_pushed"}


# ─── Brain Map (グラフビジュアライザ) ───
@app.get("/brain/graph", response_class=HTMLResponse)
async def brain_graph_page():
    """Wiki をマインドマップ風に表示する SPA (vis-network)"""
    from brain_graph import GRAPH_HTML
    return HTMLResponse(GRAPH_HTML)


@app.get("/api/brain/graph")
async def api_brain_graph(
    request: Request,
    surface: int = 40,
    all: int = 0,
    _: str = Depends(require_api_key),
):
    """Brain Map 用 JSON データ (nodes / edges / stats)

    Query params:
        surface: 表に出すノードの割合 (%)。デフォ40
        all: 1 にするとストレージにまとめず全ノード表示

    ★2026-07-11 海山指示「Brain Map は個人利用だから全部見れる」: admin tier
    (BRAIN_EXTENSION_KEY) の時だけ deep-private (personal/ + interview/) と
    clone_visibility: private (法務/人事 decision 等) もノードとして出す。token tier は
    従来どおり両方を build 段階で除外 (グラフに title/tags/path も出さない、operator #2 と一貫)。
    """
    from brain_graph import build_graph_data
    from brain_wiki import WIKI_DIR
    _admin = brain_auth_tier(request) == "admin"
    return build_graph_data(
        WIKI_DIR, surface_pct=surface, show_all=bool(all),
        admin=_admin,
    )


@app.get("/api/brain/wiki")
async def api_brain_wiki(
    request: Request, path: str, _: str = Depends(require_api_key)
):
    """Brain Map の詳細ペイン用: 1ファイルの本文を返す (path traversal 防止)"""
    from brain_wiki import WIKI_DIR
    # 安全性: 絶対パス・.. を弾き、WIKI_DIR 配下に収まることを確認
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    target = (WIKI_DIR / path).resolve()
    try:
        rel = target.relative_to(WIKI_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes WIKI_DIR")
    # ★2026-07-03 (v3 ADR DA R6 cross-check 7-1): Brain Map の node title を graph で隠しても
    #   同じ credential (?key= は LINE メッセージ URL にも埋め込まれる) で本 endpoint から
    #   任意 path 全文が読めた = 深層 private (personal/ + interview/) を path で拒否
    from brain_wiki_helpers.domain import is_deep_private_rel
    from brain_wiki_helpers.visibility import parse_clone_visibility
    # ★2026-07-11 海山指示「Brain Map は個人利用だから全部見れる」: admin tier
    #   (BRAIN_EXTENSION_KEY = /brain コマンドで admin にだけ配信される鍵) は全開。
    #   token tier (弱いダッシュボード token) は #2 のまま深層/private を遮断。
    #   兄弟 endpoint (wiki_page/knowledge/dashboard/search) と MCP は無条件遮断のまま
    #   (Brain Map だけの意図的例外、§1.17 の admin 消費者リストに登録)。
    _admin = brain_auth_tier(request) == "admin"
    if is_deep_private_rel(rel) and not _admin:
        raise HTTPException(status_code=404, detail="Not found")
    if not target.exists() or target.suffix != ".md":
        raise HTTPException(status_code=404, detail="Not found")
    _c = target.read_text(encoding="utf-8")
    # ★2026-07-10 (世界基準評価 #2): clone_visibility: private も operator key から遮断 (法務/人事 decision 等)
    #   ★2026-07-11: ただし admin tier の Brain Map は全開 (上記)。
    if parse_clone_visibility(_c) == "private" and not _admin:
        raise HTTPException(status_code=404, detail="Not found")
    return HTMLResponse(
        _c,
        media_type="text/plain; charset=utf-8",
    )


# ─── 初回ベクトル索引構築 ───
async def _initial_reindex(app):
    """起動時に既存Wiki/Rawをベクトル索引に登録（チャンクが空の場合のみ）"""
    # LiteLLM の起動を待つ（embedding API が使えるようになるまで）
    await asyncio.sleep(10)

    try:
        idx: BrainIndex = app.state.brain_index
        stats = idx.get_stats()
        if stats["total_chunks"] > 0:
            # ★2026-08-10: 全 skip をやめ、欠落分だけ突合補充 (詳細は
            #   BrainIndex.reconcile_missing_wiki の docstring。§1.12b: 実体は helpers 側)
            from brain_wiki import WIKI_DIR as _WD
            await idx.reconcile_missing_wiki(_WD)
            return

        from brain_wiki import WIKI_DIR, RAW_DIR
        logger.info("Initial vector index build starting...")
        summary = await idx.rebuild_all(WIKI_DIR, RAW_DIR)  # ★2026-08-14: 突合込み (§1.12b)
        # runbook の検証手順がこの行を grep するので key=value 形式で出す (dict repr は避ける)
        logger.info("Initial reindex complete: " + " ".join(f"{k}={v}" for k, v in summary.items()))
    except Exception as e:
        logger.warning(f"Initial reindex error: {e}")


# ─── wiki/ 変更監視 → 増分 Chroma 再索引 (★2026-05-19) ───
# 真因 B: _initial_reindex は起動時 + chunks==0 のみ。起動後に
# MacBook→git pull / Mac mini 編集 / scraper 生成 された wiki は
# Chroma 未登録 → vector search で hit せず bot が古い情報を返す。
# 5 分毎に WIKI_DIR を再帰走査、mtime 更新分だけ index_wiki_file。
WIKI_WATCH_INTERVAL_SEC = int(os.getenv("WIKI_WATCH_INTERVAL_SEC", "300"))
WIKI_WATCH_MAX_BATCH = int(os.getenv("WIKI_WATCH_MAX_BATCH", "40"))


async def _watch_wiki_changes(app):
    """WIKI_DIR を定期再帰走査し、mtime が進んだ .md を増分再索引。
    初回サイクルは baseline 確立のみ (起動時の大量再索引は _initial_reindex 担当)。
    同一プロセス内 asyncio 直列なので chromadb 並行アクセス問題は無い。"""
    await asyncio.sleep(40)  # LiteLLM + initial_reindex の後
    try:
        from brain_wiki import WIKI_DIR
    except Exception as e:
        logger.warning(f"[wiki-watch] WIKI_DIR import 失敗: {e}")
        return
    idx: BrainIndex = app.state.brain_index
    from brain_wiki_helpers.domain import is_personal_rel
    seen: dict[str, float] = {}
    first = True
    while True:
        try:
            changed: list[Path] = []
            for f in WIKI_DIR.rglob("*.md"):
                # ★2026-06-28 personal ドメイン分離: wiki/personal/ (非OWNDAYS) は OWNDAYS chroma に
                #   載せないので mtime 追跡も不要 (index_wiki_file でも skip 済の二重防御 + churn 回避)。
                # ★2026-07-03 (v3 ADR DA R6): interview/ は**意図的にここで除外しない** = 索引には
                #   載せ続ける。海山専用経路 (/mcp/brain の brain_search = P3b vector recall) が
                #   interview/ を引くため。公開経路は chroma where + runtime visibility gate +
                #   path 強制 private (いずれも brain_wiki.py 側) の三重で遮断される。
                if is_personal_rel(f.relative_to(WIKI_DIR)):
                    continue
                try:
                    m = f.stat().st_mtime
                except Exception:
                    continue
                key = str(f)
                if seen.get(key) != m:
                    if not first:
                        changed.append(f)
                    seen[key] = m
            if first:
                first = False
                logger.info(
                    f"[wiki-watch] baseline {len(seen)} wiki files "
                    f"(以後 mtime 変化分のみ再索引)"
                )
            else:
                for f in changed[:WIKI_WATCH_MAX_BATCH]:
                    try:
                        await idx.index_wiki_file(f)
                        logger.info(
                            f"[wiki-watch] reindexed {f.relative_to(WIKI_DIR)}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[wiki-watch] index 失敗 {f.name}: {e}"
                        )
                    await asyncio.sleep(1)
                if len(changed) > WIKI_WATCH_MAX_BATCH:
                    logger.info(
                        f"[wiki-watch] {len(changed)} 変更中 "
                        f"{WIKI_WATCH_MAX_BATCH} 件処理、残りは次サイクル"
                    )
        except Exception as e:
            logger.warning(f"[wiki-watch] cycle error: {e}")
        await asyncio.sleep(WIKI_WATCH_INTERVAL_SEC)


# ─── インポートディレクトリ監視 ───
IMPORT_RATE_LIMIT_SEC = float(os.getenv("BRAIN_IMPORT_RATE_LIMIT_SEC", "5"))
IMPORT_MAX_BATCH = int(os.getenv("BRAIN_IMPORT_MAX_BATCH", "10"))


async def _watch_import_dir(app):
    """data/brain/import/ を30秒間隔でポーリングし、.txt を自動取り込み

    大量バッチ（Apple Notes 全同期など）による Anthropic 残高枯渇を避けるため、
    1回のループで最大 IMPORT_MAX_BATCH ファイルまで、ファイル間に
    IMPORT_RATE_LIMIT_SEC 秒のクールダウンを入れる。
    """
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    processed_dir = IMPORT_DIR / "processed"
    processed_dir.mkdir(exist_ok=True)
    # ★2026-08-03: 重複 compile 抑止の state (本文 fingerprint) と計測ログ
    IMPORT_DEDUP_STATE = Path("/app/data/brain/.import_dedup_state.json")

    def _log_import_event(kind: str, filename: str) -> None:
        """skip/compile の件数を bot_events に残す (skip 率 100% = 取りこぼしの兆候を検知するため)。"""
        try:
            from scripts.bot_events import log_bot_event
            log_bot_event("import", kind, file=filename[:80])
        except Exception:
            pass

    # スクレイパーが既に wiki を決定論的に書いているため、
    # ingest_note 経由の LLM compile に流すと wiki が破壊される (LLMが要約・上書きする)。
    # これらは raw archive として processed/ に移すだけで、wiki 生成はスキップ。
    DETERMINISTIC_SCRAPER_PREFIXES = (
        "owndays_mobile_sales_",   # mobile_owndays_scraper.py が daily-{sales,stores}.md を生成
        "owndays_history_",        # mobile_owndays_historical.py が history-*.md を生成
    )

    # ★2026-05-11: 会議 transcript 用の subdir watcher (Plaud / Recall 経由)
    # import/plaud/ や import/recall/ に txt が落ちたら compile_meeting_note で処理。
    MEETING_SOURCE_SUBDIRS = ("plaud", "recall", "owl")

    # ★2026-04-28: .txt 以外も watch (PDF / DOCX / XLSX / PPTX)。
    # 以前は txt だけ拾っていたため、ユーザが import/ に PDF を置いても何も起きなかった。
    # ★2026-05-07: CSV / TSV / MD も追加 (Google Drive Sheets export 等のため)
    BINARY_EXTRACT_EXTS = {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".csv", ".tsv", ".md"}

    while True:
        await asyncio.sleep(30)
        try:
            # ── 会議 transcript subdir (Plaud / Recall / Owl) を先に処理 ──
            # ファイル名規約: import/{source}/YYYY-MM-DD_title.txt (or .md)
            for source in MEETING_SOURCE_SUBDIRS:
                sub_dir = IMPORT_DIR / source
                if not sub_dir.is_dir():
                    continue
                sub_processed = sub_dir / "processed"
                sub_processed.mkdir(exist_ok=True)
                for meet_file in sorted(sub_dir.glob("*.txt")) + sorted(sub_dir.glob("*.md")):
                    if meet_file.parent == sub_processed:
                        continue
                    logger.info(f"[meeting-watch] {source}: {meet_file.name}")
                    try:
                        text = meet_file.read_text(encoding="utf-8", errors="replace")
                        # ファイル名から日付/タイトル推定: "2026-05-11_経営会議.txt"
                        stem = meet_file.stem
                        m_date = None
                        m_title = stem
                        if len(stem) >= 10 and stem[:4].isdigit() and stem[4] == "-":
                            m_date = stem[:10]
                            m_title = stem[11:] or stem
                        # PrivacyGate
                        try:
                            pres = await app.state.privacy.filter(text, sender_id=f"{source}_watch")
                            if pres.verdict.value != "allow":
                                logger.info(f"[meeting-watch] {meet_file.name} blocked by PrivacyGate")
                                meet_file.rename(sub_processed / meet_file.name)
                                continue
                            filtered_text = pres.sanitized
                        except Exception as e:
                            logger.warning(f"[meeting-watch] PrivacyGate error: {e}")
                            filtered_text = text
                        await app.state.brain.compile_meeting_note(
                            transcript=filtered_text,
                            source=source,
                            title=m_title,
                            meeting_date=m_date,
                        )
                        meet_file.rename(sub_processed / meet_file.name)
                    except Exception as e:
                        logger.warning(f"[meeting-watch] error {meet_file.name}: {e}")
                    await asyncio.sleep(IMPORT_RATE_LIMIT_SEC)

            # .txt と バイナリ系を両方収集 (top-level only)
            txt_files = sorted(IMPORT_DIR.glob("*.txt"))
            bin_files = []
            for ext in BINARY_EXTRACT_EXTS:
                bin_files.extend(IMPORT_DIR.glob(f"*{ext}"))
            files = (txt_files + sorted(bin_files))[:IMPORT_MAX_BATCH]

            for i, src_file in enumerate(files):
                suffix = src_file.suffix.lower()

                # スクレイパーが既に wiki を決定論的に書いている → compile スキップ
                if src_file.name.startswith(DETERMINISTIC_SCRAPER_PREFIXES):
                    logger.info(
                        f"Skipping LLM compile (deterministic scraper output): "
                        f"{src_file.name}"
                    )
                    src_file.rename(processed_dir / src_file.name)
                    continue

                # ── ★2026-05-11: gdrive_sync 経由の会議 transcript を meeting note にルーティング
                # 例: gdrive_plaud-exports_<title>.txt → compile_meeting_note(source="plaud")
                # Plaud Cloud → Zapier → Drive (社長室/Plaud) → gdrive_sync で取得 → ここで議事録化
                if suffix == ".txt" and src_file.name.startswith("gdrive_plaud-exports_"):
                    logger.info(f"[meeting-watch] gdrive→plaud: {src_file.name}")
                    try:
                        text = src_file.read_text(encoding="utf-8", errors="replace")
                        # filename から title 推定: "gdrive_plaud-exports_<title>.txt"
                        m_title = src_file.stem.removeprefix("gdrive_plaud-exports_") or src_file.stem
                        try:
                            pres = await app.state.privacy.filter(text, sender_id="plaud_gdrive")
                            if pres.verdict.value != "allow":
                                logger.info(f"[meeting-watch] {src_file.name} blocked by PrivacyGate")
                                src_file.rename(processed_dir / src_file.name)
                                continue
                            filtered_text = pres.sanitized
                        except Exception as e:
                            logger.warning(f"[meeting-watch] PrivacyGate error: {e}")
                            filtered_text = text
                        await app.state.brain.compile_meeting_note(
                            transcript=filtered_text,
                            source="plaud",
                            title=m_title,
                        )
                        src_file.rename(processed_dir / src_file.name)
                    except Exception as e:
                        logger.warning(f"[meeting-watch] gdrive plaud error {src_file.name}: {e}")
                    if i < len(files) - 1:
                        await asyncio.sleep(IMPORT_RATE_LIMIT_SEC)
                    continue

                # ── ★2026-05-18: Claude 音声アラインメント雑談 → 蒸留パイプライン
                # 海山が専用 Claude Project (Personal Brain MCP 接続) で雑談 →
                # claude_scraper が import/claude_*.txt に吸う → ここで検出。
                # 検出キー: Project custom instructions で Claude が冒頭に付ける
                # マーカー ⟦ALIGN⟧ (海山は毎回何もしなくていい)。
                # 通常の Claude 作業会話 (マーカー無し) は従来通り ingest へ流す。
                if (
                    suffix == ".txt"
                    and src_file.name.startswith("claude_")
                ):
                    try:
                        c_text = src_file.read_text(
                            encoding="utf-8", errors="replace"
                        )
                    except Exception:
                        c_text = ""
                    if "⟦ALIGN⟧" in c_text or "[ALIGN]" in c_text:
                        logger.info(
                            f"[align-voice] Claude 雑談検出: {src_file.name}"
                        )
                        try:
                            # claude_scraper 形式:
                            #   [Claude.ai] <title>\n<date>\n\n
                            #   \t海山丈司\t<content>\n\tClaude\t<content>...
                            lines_in = c_text.splitlines()
                            convo: list[str] = []
                            for ln in lines_in:
                                if "\t" not in ln:
                                    continue
                                parts = ln.split("\t")
                                # ["", "海山丈司", "<content>"] 形式
                                if len(parts) >= 3 and parts[1]:
                                    who = (
                                        "海山"
                                        if "海山" in parts[1]
                                        else "AI"
                                    )
                                    body = "\t".join(parts[2:]).strip()
                                    body = body.replace("⟦ALIGN⟧", "").replace(
                                        "[ALIGN]", ""
                                    ).strip()
                                    if body:
                                        convo.append(f"{who}: {body}")
                            transcript = "\n".join(convo)
                            if transcript.strip():
                                import alignment_interview as _ai
                                raw_p = _ai.record_session(
                                    transcript, source="claude-voice"
                                )
                                logger.info(
                                    f"[align-voice] recorded {raw_p.name} "
                                    f"({len(transcript)} chars)"
                                )
                                res = await _ai.extract_session(
                                    transcript,
                                    app.state.http,
                                    LITELLM_URL,
                                    LITELLM_KEY,
                                    raw_filename=raw_p.name,
                                )
                                if res.get("error"):
                                    logger.warning(
                                        f"[align-voice] extract failed: "
                                        f"{res['error']}"
                                    )
                                else:
                                    logger.info(
                                        f"[align-voice] extracted "
                                        f"{len(res.get('items', []))} items, "
                                        f"dims={res.get('dims_with_substance')}"
                                    )
                            else:
                                logger.warning(
                                    f"[align-voice] empty transcript: "
                                    f"{src_file.name}"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[align-voice] error {src_file.name}: {e}"
                            )
                        src_file.rename(processed_dir / src_file.name)
                        if i < len(files) - 1:
                            await asyncio.sleep(IMPORT_RATE_LIMIT_SEC)
                        continue
                    # マーカー無し = 通常の Claude 作業会話 → 従来 ingest へ落ちる

                # ── バイナリ (PDF / DOCX / XLSX / PPTX) → extract → ingest_note ──
                if suffix in BINARY_EXTRACT_EXTS:
                    logger.info(f"Auto-importing binary ({i+1}/{len(files)}): {src_file.name}")
                    try:
                        from content_extractor import extract_file_text  # local import to avoid circular
                        # 大きめ上限 (組織図・スライド全文を逃したくない)
                        content = await extract_file_text(
                            src_file,
                            max_chars=50_000,
                            max_pages=80,
                            max_sheets=10,
                            max_rows_per_sheet=300,
                        )
                        if not content or not content.strip():
                            logger.warning(
                                f"binary extract empty: {src_file.name} — moved to processed/ without ingest"
                            )
                        elif content.startswith("[PDF text-extract empty]") or content.startswith("[PDF text mojibake"):
                            # 抽出失敗メッセージ。raw に明示記録して、ユーザがログで気付けるようにする
                            logger.warning(f"binary extract failed: {src_file.name}: {content[:120]}")
                            try:
                                await app.state.brain.ingest_note(
                                    user_id="system_import",
                                    content=content,
                                    title=f"{src_file.stem} (extract_failed)",
                                )
                            except Exception as e:
                                logger.warning(f"ingest_note (extract_failed marker) failed: {e}")
                        else:
                            logger.info(
                                f"binary extract OK: {src_file.name} ({len(content)} chars) → ingest_note"
                            )
                            try:
                                await app.state.brain.ingest_note(
                                    user_id="system_import",
                                    content=content,
                                    title=src_file.stem,
                                )
                            except Exception as e:
                                logger.warning(f"ingest_note failed for {src_file.name}: {e}")
                        src_file.rename(processed_dir / src_file.name)
                    except Exception as e:
                        logger.warning(f"binary import error for {src_file.name}: {e}")
                    if i < len(files) - 1:
                        await asyncio.sleep(IMPORT_RATE_LIMIT_SEC)
                    continue

                # ── 既存 .txt パイプ ──
                logger.info(f"Auto-importing ({i+1}/{len(files)}): {src_file.name}")
                try:
                    result = await process_chat_export(
                        src_file, app.state.privacy, app.state.brain
                    )
                    # chat format じゃない (STAPA scraper 等の生テキスト) の場合、
                    # raw notes パイプに fallback して BrainWiki にコンパイルさせる
                    if result and result.get("format") == "unknown":
                        logger.info(f"Non-chat format → ingest_note fallback: {src_file.name}")
                        try:
                            content = src_file.read_text(encoding="utf-8", errors="replace")
                            title = src_file.stem
                            # ★2026-08-03 コスト実測: LINE Works は 2h おきに全ルームの全文を
                            # 書き出すため、新規発言が無いルームも毎回 LLM compile されていた
                            # (実測 3 回中 1-2 回が同一本文 = 支出の約4割)。本文 hash で skip。
                            from brain_wiki_helpers import import_dedup as _dd
                            _dd_state = _dd.load_state(IMPORT_DEDUP_STATE)
                            if _dd.is_duplicate(_dd_state, src_file.name, content):
                                logger.info(
                                    f"重複 skip (本文が前回と同一): {src_file.name} "
                                    f"— compile せず processed/ へ")
                                _log_import_event("dedup_skip", src_file.name)
                            else:
                                await app.state.brain.ingest_note(
                                    user_id="system_import",
                                    content=content,
                                    title=title,
                                )
                                _dd.save_state(
                                    IMPORT_DEDUP_STATE,
                                    _dd.remember(_dd_state, src_file.name, content))
                                _log_import_event("compiled", src_file.name)
                        except Exception as e:
                            logger.warning(f"ingest_note fallback failed ({src_file.name}): {e}")
                    src_file.rename(processed_dir / src_file.name)
                except Exception as e:
                    logger.warning(f"Import error for {src_file.name}: {e}")
                # ファイル間クールダウン（API残高/レート対策）
                if i < len(files) - 1:
                    await asyncio.sleep(IMPORT_RATE_LIMIT_SEC)
        except Exception as e:
            logger.warning(f"Import watcher error: {e}")


# ─── 定期アライメント質問（毎日21:00） ───
async def _daily_alignment(app):
    """毎日21時にアライメント質問をPush通知で送信"""
    import json

    while True:
        now = datetime.now()
        # 次の21:00までの秒数を計算
        target = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)

        if not ALIGNMENT_TARGET_USER:
            logger.warning("ALIGNMENT_TARGET_USER 未設定 — アライメント通知スキップ")
            continue

        try:
            brain: BrainWiki = app.state.brain
            q = await brain.generate_alignment_question()

            if not isinstance(q, dict):
                logger.warning(f"Alignment question not dict: {type(q)} — skipped")
                continue
            question_text = q.get("question") or ""
            if not question_text.strip():
                logger.warning(f"Alignment question empty (keys={list(q.keys())}) — skipped")
                continue

            category_labels = {
                "orientation": "指向",
                "thinking": "思考",
                "taste": "趣向",
                "reaction": "反応",
                "contradiction": "矛盾探索",
            }
            cat = category_labels.get(q.get("category", ""), q.get("category", ""))
            text = f"[Alignment: {cat}]\n{question_text}"

            # 質問データをRedisに保存
            r = app.state.redis
            await r.set(
                f"align:{ALIGNMENT_TARGET_USER}",
                json.dumps(q, ensure_ascii=False),
                ex=86400,
            )

            await push_message(app.state.http, ALIGNMENT_TARGET_USER, text)
            logger.info(f"Alignment push sent: {cat}")
        except Exception as e:
            logger.warning(f"Daily alignment error: {e}", exc_info=True)


# ─── 定期 LINE chat 取り込みリマインド（毎週日曜20:00） ───
async def _weekly_line_import_reminder(app):
    """毎週日曜20:00に「今週のLINE chats を取り込みましたか？」を Push 通知。

    LINE 個人チャットは LINE 社の制約で API 経由では取得できないため、
    ユーザーが LINE アプリで「トーク履歴を送信」して Brain Bot に共有する必要がある。
    この週次リマインドで習慣化を補助する。

    無効化: BRAIN_LINE_REMINDER=0 または
           data/brain/line_reminder_disabled.txt を作成
    """
    if os.getenv("BRAIN_LINE_REMINDER", "1") != "1":
        logger.info("LINE 取り込みリマインド: 無効化されています（env）")
        return

    disabled_flag = Path("/app/data/brain/line_reminder_disabled.txt")

    while True:
        now = datetime.now()
        days_ahead = (6 - now.weekday()) % 7
        target = (now + timedelta(days=days_ahead)).replace(
            hour=20, minute=0, second=0, microsecond=0
        )
        if target <= now:
            target = target + timedelta(days=7)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"次回 LINE 取り込みリマインド: {target} ({wait_seconds/3600:.1f}h 後)")
        await asyncio.sleep(wait_seconds)

        # 動的無効化チェック
        if disabled_flag.exists():
            logger.info("LINE 取り込みリマインド: フラグにより無効化中")
            continue

        if not ALIGNMENT_TARGET_USER:
            continue

        try:
            text = (
                "📱 チャット取り込みリマインド\n"
                "━━━━━━━━━━━━━━━━━\n"
                "今週分の LINE / WhatsApp トークを Brain に取り込みませんか？\n\n"
                "【LINE】\n"
                "1. トークを開く → 右上「︙」→ その他 → トーク履歴を送信\n"
                "2. 共有先で Brain Bot を選択\n\n"
                "【WhatsApp】\n"
                "1. チャットを開く → 名前タップ → 「チャットをエクスポート」\n"
                "2. 「メディアなし」を選択 → Brain Bot にシェア\n\n"
                "→ 自動判定・PrivacyGate・Wikiコンパイルまで実行します。"
            )
            # push_message は flat 形式 {"type","label","data"} を期待する
            quick_reply = [
                {"type": "message", "label": "今やる", "data": "/forward"},
                {"type": "message", "label": "今週はスキップ", "data": "/line-skip"},
                {"type": "message", "label": "リマインド停止", "data": "/line-reminder-off"},
            ]
            await push_message(
                app.state.http, ALIGNMENT_TARGET_USER, text, quick_reply=quick_reply
            )
            logger.info("LINE 取り込みリマインド送信完了")
        except Exception as e:
            logger.warning(f"LINE reminder error: {e}", exc_info=True)


# ─── うみやまAI 修正希望 デイリーダイジェスト (毎朝9:00) ───
CLONE_FEEDBACK_DIGEST_HOUR = int(os.getenv("CLONE_FEEDBACK_DIGEST_HOUR", "9"))


async def _daily_clone_feedback_digest(app):
    """毎日 CLONE_FEEDBACK_DIGEST_HOUR 時 JST (デフォルト 09:00 JST) に、
    うみやまAI の pending 修正希望サマリーを海山の LINE Bot に Push。

    - 0件 なら送信しない (ノイズ回避)
    - 1件以上なら summary + 操作コマンド案内
    - コンテナは UTC だが、計算は JST-aware で行う
    """
    while True:
        now = datetime.now(JST)
        target = now.replace(
            hour=CLONE_FEEDBACK_DIGEST_HOUR, minute=0, second=0, microsecond=0
        )
        if now >= target:
            target = target + timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(
            f"次回 clone_feedback digest: {target.strftime('%Y-%m-%d %H:%M %Z')} ({wait_seconds/3600:.1f}h 後)"
        )
        await asyncio.sleep(wait_seconds)

        if not ALIGNMENT_TARGET_USER:
            continue

        try:
            pending = clone_feedback.list_pending(limit=20)
            if not pending:
                logger.info("clone_feedback digest: pending 0件 — skip")
                continue

            text, qr = _build_pending_minidigest(
                pending,
                kind="feedback",
                title="📋 うみやまAI 修正希望ダイジェスト",
            )
            await push_message(
                app.state.http,
                ALIGNMENT_TARGET_USER,
                text,
                quick_reply=qr,
            )
            logger.info(f"clone_feedback digest sent: {len(pending)}件")
        except Exception as e:
            logger.warning(f"clone_feedback digest error: {e}", exc_info=True)


# ─── うみやまAI 会話学習ループ (nightly) ────────────────────────────
CLONE_LEARNING_SCAN_HOUR = int(os.getenv("CLONE_LEARNING_SCAN_HOUR", "2"))  # 02:00 JST
CLONE_LEARNING_DIGEST_HOUR = int(os.getenv("CLONE_LEARNING_DIGEST_HOUR", "16"))  # 16:00 JST

# ★2026-05-19: 音声アラインメント未処理の自動リマインド
# 問題: 電話雑談→蒸留は自動だが /align-voice レビューを海山が忘れると滞留
# (5/18 は 4件16 insight が丸1日宙に浮いた)。毎日 20:00 JST に
# pending>0 なら Push してループを自走させる。
ALIGN_VOICE_DIGEST_HOUR = int(os.getenv("ALIGN_VOICE_DIGEST_HOUR", "20"))


async def _daily_align_voice_digest(app):
    """毎日 ALIGN_VOICE_DIGEST_HOUR 時 JST に、音声アラインメントの
    未レビュー蒸留が溜まっていれば海山へ Push (pending 0 なら無音)。"""
    while True:
        now = datetime.now(JST)
        target = now.replace(
            hour=ALIGN_VOICE_DIGEST_HOUR, minute=0, second=0, microsecond=0
        )
        if now >= target:
            target = target + timedelta(days=1)
        wait = (target - now).total_seconds()
        logger.info(
            f"次回 align_voice digest: {target.strftime('%Y-%m-%d %H:%M %Z')} "
            f"({wait/3600:.1f}h 後)"
        )
        await asyncio.sleep(wait)

        if not ALIGNMENT_TARGET_USER:
            continue
        try:
            import alignment_interview as _ai
            pending = _ai.list_pending_extractions()
            if not pending:
                logger.info("align_voice digest: pending 0件 — skip")
                continue
            total_items = sum(p.get("item_count", 0) for p in pending)
            lines = [
                "🎙️ 音声アラインメント 未処理リマインド",
                f"通話 {len(pending)} 本ぶん / 蒸留 {total_items} insight が"
                "レビュー待ち。",
                "━━━━━━━━━━━━━━━",
            ]
            for p in pending[:5]:
                fid = p["file"].replace(".json", "")
                lines.append(f"・{fid} ({p.get('item_count',0)}件)")
                if p.get("summary"):
                    lines.append(f"  {p['summary'][:50]}")
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("下のボタンでワンタップ採用、詳細は /align-voice。")
            # ★2026-07-04 UX: digest から 3 ステップ (コマンド入力→一覧→✅) かかっていた採用を
            # 通知内 quick reply 1 タップに (clone_feedback digest と同パターン)。
            qr = [{"label": "✅まとめて採用", "data": "/align-voice-accept all",
                   "type": "message"},
                  {"label": "📄一覧", "data": "/align-voice", "type": "message"}]
            for p in pending[:3]:
                fid = p["file"].replace(".json", "")
                short = fid[-4:]
                qr.append({"label": f"✅{short}", "data": f"/align-voice-accept {fid}",
                           "type": "message"})
            await push_message(
                app.state.http, ALIGNMENT_TARGET_USER, "\n".join(lines),
                quick_reply=qr,
            )
            logger.info(
                f"align_voice digest sent: {len(pending)}本 {total_items}件"
            )
        except Exception as e:
            logger.warning(f"align_voice digest error: {e}", exc_info=True)


async def _nightly_clone_learning_scan(app):
    """毎日 CLONE_LEARNING_SCAN_HOUR 時 JST に clone_history を走査して発見抽出。
    抽出結果は data/brain/clone_learning/YYYY-MM-DD.jsonl に保存。
    同日 CLONE_LEARNING_DIGEST_HOUR 時に海山へダイジェスト Push。
    """
    while True:
        now = datetime.now(JST)
        # 次回スキャン実行時刻
        target_scan = now.replace(
            hour=CLONE_LEARNING_SCAN_HOUR, minute=0, second=0, microsecond=0
        )
        if now >= target_scan:
            target_scan = target_scan + timedelta(days=1)
        wait = (target_scan - now).total_seconds()
        logger.info(
            f"次回 clone_learning scan: {target_scan.strftime('%Y-%m-%d %H:%M %Z')} ({wait/3600:.1f}h 後)"
        )
        await asyncio.sleep(wait)

        try:
            saved = await clone_learning.run_scan(
                app.state.http,
                LITELLM_URL,
                LITELLM_KEY,
                app.state.brain,
                model=os.getenv("CLONE_LEARNING_MODEL", "fast-gpt"),
            )
            logger.info(f"clone_learning scan complete: {saved} insights")
        except Exception as e:
            logger.exception(f"clone_learning scan failed: {e}")
            continue

        # 同日 DIGEST_HOUR 時まで待って digest 送信
        if not ALIGNMENT_TARGET_USER:
            continue
        try:
            now2 = datetime.now(JST)
            target_dg = now2.replace(
                hour=CLONE_LEARNING_DIGEST_HOUR, minute=0, second=0, microsecond=0
            )
            if now2 >= target_dg:
                target_dg = target_dg + timedelta(days=1)
            wait2 = (target_dg - now2).total_seconds()
            await asyncio.sleep(wait2)

            pending = clone_learning.list_pending(limit=20)
            if not pending:
                logger.info("clone_learning digest: pending 0件 — skip")
                continue

            text, qr = _build_pending_minidigest(
                pending,
                kind="learning",
                title="🧠 うみやまAI 会話発見ダイジェスト",
            )
            await push_message(
                app.state.http,
                ALIGNMENT_TARGET_USER,
                text,
                quick_reply=qr,
            )
            logger.info(f"clone_learning digest sent: {len(pending)}件")
        except Exception as e:
            logger.warning(f"clone_learning digest error: {e}", exc_info=True)


# ─── 自己改善ループ (★2026-05-22 Phase 4: tasks/self_improve.py に切り出し) ───
# 互換のため module-level に同名 alias を提供 (= 旧 import を壊さない)。
from tasks.self_improve import (  # noqa: E402
    SELF_IMPROVE_INTERVAL_SEC,
    SELF_IMPROVE_STATE_FILE,
    read_last_self_improve_ts as _read_last_self_improve_ts,
    write_last_self_improve_ts as _write_last_self_improve_ts,
    self_improve_loop as _self_improve_loop_impl,
)


async def _self_improve_loop(app):
    """★2026-05-22 Phase 4: tasks/self_improve.py に委譲 (push_message を DI)。"""
    await _self_improve_loop_impl(app, push_message_fn=push_message)


# ─── ヘルスチェック ───
@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/ready")
async def ready(request: Request):
    """readiness probe — 依存 (redis / chromadb / litellm) の疎通を確認。

    ★2026-06-08 システム評価 SRE 深刻度3: /health (liveness) は無条件 ok を返すため
    「緑なのに retrieval/LLM が壊れてる」状態を検知できなかった。本 /ready は実依存を
    確認し、全 OK で 200・1 つでも NG で 503 を返す (= 外形/合成監視がこちらを見るべき)。
    注: bot_uptime_monitor の自動 restart 配線は transient blip での誤 restart を避けるため別途。
    """
    from fastapi.responses import JSONResponse
    checks: dict = {}
    # redis
    try:
        checks["redis"] = bool(await request.app.state.redis.ping())
    except Exception as e:
        checks["redis"] = False
        checks["redis_error"] = type(e).__name__
    # chromadb (wiki collection に chunk があるか = 索引生存)
    try:
        cnt = request.app.state.brain_index.wiki_col.count()
        checks["chromadb_wiki_chunks"] = cnt
        checks["chromadb"] = cnt > 0
    except Exception as e:
        checks["chromadb"] = False
        checks["chromadb_error"] = type(e).__name__
    # litellm (/models 疎通 = LLM proxy 生存)
    try:
        r = await request.app.state.http.get(
            f"{LITELLM_URL}/models",
            headers={"Authorization": f"Bearer {LITELLM_KEY}"},
            timeout=5.0,
        )
        checks["litellm"] = r.status_code == 200
    except Exception as e:
        checks["litellm"] = False
        checks["litellm_error"] = type(e).__name__
    ok = bool(checks.get("redis") and checks.get("chromadb") and checks.get("litellm"))
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ready": ok, "checks": checks, "time": datetime.now().isoformat()},
    )


# ─── Brain API（外部クライアント用）───

@app.get("/api/brain/knowledge")
async def api_brain_knowledge(request: Request, _: str = Depends(require_admin_key)):
    """Brain Wiki + カレンダー + メールの最新ナレッジを返す。

    ★2026-07-14 世界基準評価 #1 cross-check: mail (Gmail 直近10件の差出人+件名) +
    calendar (2日分) を返す = /api/brain/dashboard と同クラスの CEO 個人データ。
    弱い ?token= fallback で到達できた穴を require_admin_key で封鎖 (chat/drive/dashboard と一貫)。
    """
    wiki_content = ""
    wiki_dir = Path("/app/data/brain/wiki")
    if wiki_dir.exists():
        from brain_wiki_helpers.domain import is_owndays_facing
        from brain_wiki_helpers.visibility import parse_clone_visibility
        for f in sorted(wiki_dir.rglob("*.md")):
            rel = f.relative_to(wiki_dir)
            # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): 外部 API
            #   (/api/brain/knowledge) に深層 private (非OWNDAYS PJ + 人格深層) を出さない
            if not is_owndays_facing(rel):
                continue
            text = f.read_text(encoding="utf-8").strip()
            # ★2026-07-10 (世界基準評価 #2): clone_visibility: private も operator key から遮断
            if parse_clone_visibility(text) == "private":
                continue
            if text:
                wiki_content += f"\n## {rel}\n{text}\n"

    # 並列化して非ブロッキング
    cal, mail = await asyncio.gather(
        asyncio.to_thread(_fetch_calendar_context, 2),
        asyncio.to_thread(_fetch_mail_context, 1, 10),
    )

    return {
        "wiki": wiki_content,
        "calendar": cal,
        "mail": mail,
        "updated_at": datetime.now().isoformat(),
    }


@app.get("/api/brain/search")
async def api_brain_search(request: Request, _: str = Depends(require_api_key)):
    """Brain Wikiベクトル検索"""
    query = request.query_params.get("q", "")
    if not query:
        raise HTTPException(status_code=400, detail="q parameter required")

    try:
        brain_index: BrainIndex = request.app.state.brain_index
        # ★2026-07-03 (R6): 既定 public 限定 (fail-safe)。唯一の consumer = hallucination
        # verifier は public=1 明示で無影響。
        # ★2026-07-10 (世界基準評価 #2 DA): private=1 の deep-private opt-in を廃止。
        #   同じ operator key (?key= は LINE URL に埋まる) で interview/ (家族/弱さ/金/体)・
        #   personal/ が search 経由で全文 snippet 露出できた = file-read 版 (#2) を塞いでも
        #   search が抜け穴として残る片手落ち。private=1 の実 consumer は無し (verifier は
        #   public=1)。深層検索が要る海山 admin 経路は /clone・/mcp/brain (海山専用) が担う。
        context = await brain_index.build_context(query, max_chars=5000,
                                                  public_only=True)
        return {"query": query, "results": context or "該当なし"}
    except Exception as e:
        logger.exception("api_brain_search failed")
        return {"query": query, "error": "internal error"}


@app.get("/api/brain/drive")
async def api_brain_drive(request: Request, _: str = Depends(require_admin_key)):
    """Google Driveファイル検索+内容取得。★2026-07-14: CEO 個人 Drive に到達するため
    admin-tier 必須 (弱い ?token= fallback 不可、chat/#37 と一貫)。"""
    query = request.query_params.get("q", "")
    if not query:
        raise HTTPException(status_code=400, detail="q parameter required")

    result = await asyncio.to_thread(_fetch_drive_context, query, 10, True)
    return {"query": query, "results": result}


# ─── Web Chat UI ───

@app.get("/chat")
async def chat_page():
    """Web Chat UI"""
    return FileResponse("/app/static/chat.html", media_type="text/html")


@app.post("/api/brain/chat")
async def api_brain_chat(request: Request, _: str = Depends(require_admin_key)):
    """Web Chat API — LINE Botと同じrun_agentを使用。

    ★2026-07-14: run_agent は interview/ + Gmail + Drive に到達するため admin-tier 必須
    (require_admin_key)。弱い VOICE_ALIGN_TOKEN fallback では叩けない (LINE 経路 #37 と一貫)。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    # Session別ID: ブラウザがCookieを持つ、なければ新規発行
    # これで同じkey使っても複数ブラウザの会話履歴は分離される
    session_id = body.get("session_id", "").strip()
    if not session_id or not session_id.replace("-", "").replace("_", "").isalnum():
        import secrets
        session_id = "web_" + secrets.token_urlsafe(12)
    user_id = f"web_chat:{session_id}"

    reply = await run_agent(
        request.app,
        request.app.state.http,
        request.app.state.redis,
        user_id,
        message,
    )

    return {"reply": reply, "session_id": session_id}


# ─── Dashboard ───

@app.get("/dashboard")
async def dashboard_page():
    """Dashboard UI"""
    return FileResponse("/app/static/dashboard.html", media_type="text/html")


@app.get("/api/brain/dashboard")
async def api_brain_dashboard(request: Request, _: str = Depends(require_admin_key)):
    """ダッシュボード用: カレンダー/メール/Wiki/アクティビティを一括取得。
    ★2026-07-14: CEO 個人 Gmail/カレンダーに到達するため admin-tier 必須 (弱 token 不可)。"""
    from datetime import timedelta

    # --- Calendar + Emails を並列取得 ---
    def _load_calendar():
        try:
            from google_sync import get_credentials, sync_calendar
            creds = get_credentials()
            return sync_calendar(creds, days=2, dry_run=True) or []
        except Exception as e:
            logger.warning(f"Dashboard calendar load error: {e}")
            return []

    def _load_emails():
        try:
            from google_sync import get_credentials, sync_gmail
            creds = get_credentials()
            return sync_gmail(creds, days=1, max_emails=15, dry_run=True) or []
        except Exception as e:
            logger.warning(f"Dashboard email load error: {e}")
            return []

    events, emails_raw = await asyncio.gather(
        asyncio.to_thread(_load_calendar),
        asyncio.to_thread(_load_emails),
    )

    # --- Calendar (today + tomorrow) ---
    calendar_data = {"today": [], "tomorrow": []}
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        for ev in events:
            is_allday = "T" not in ev["start"]
            time_str = "" if is_allday else ev["start"][11:16]
            date_str = ev["start"][:10]
            item = {
                "time": time_str,
                "allday": is_allday,
                "title": ev["summary"],
                "location": ev.get("location", ""),
                "attendees": ev.get("attendees", [])[:5],
                "date": date_str,
            }
            if date_str == today_str:
                calendar_data["today"].append(item)
            elif date_str == tomorrow_str:
                calendar_data["tomorrow"].append(item)
            else:
                calendar_data["today"].append(item)
    except Exception as e:
        logger.warning(f"Dashboard calendar error: {e}")

    # --- Emails ---
    emails_data = []
    for em in emails_raw:
        sender = em["from"].split("<")[0].strip().strip('"')[:30]
        emails_data.append({
            "from": sender,
            "subject": em["subject"][:80],
            "unread": em.get("unread", False),
            "date": em.get("date", ""),
        })

    # --- Wiki pages ---
    wiki_data = {"pages": [], "stats": {"total_pages": 0, "total_chars": 0}}
    wiki_dir = Path("/app/data/brain/wiki")
    if wiki_dir.exists():
        from brain_wiki_helpers.domain import is_owndays_facing
        from brain_wiki_helpers.visibility import parse_clone_visibility
        for f in sorted(wiki_dir.rglob("*.md")):
            rel = f.relative_to(wiki_dir)
            # ★2026-06-28 personal / ★2026-07-03 interview (v3 ADR DA R6): wiki_data export に
            #   深層 private (非OWNDAYS PJ + 人格深層) の metadata を含めない
            if not is_owndays_facing(rel):
                continue
            content = f.read_text(encoding="utf-8").strip()
            # ★2026-07-10 (世界基準評価 #2): clone_visibility: private (法務/人事 decision 等) も遮断
            if parse_clone_visibility(content) == "private":
                continue
            rel_str = str(rel)

            # Category from directory
            category = "other"
            parts = rel_str.split("/")
            if parts[0] in ("people", "projects", "knowledge", "decisions"):
                category = parts[0]
            elif rel_str in ("identity.md", "style.md", "thinking.md", "index.md"):
                category = "core"

            # Tags from frontmatter
            tags = []
            if content.startswith("---"):
                fm_end = content.find("---", 3)
                if fm_end > 0:
                    fm = content[3:fm_end]
                    import re
                    tag_match = re.search(r"tags:\s*\[([^\]]*)\]", fm)
                    if tag_match:
                        tags = [t.strip().strip("'\"") for t in tag_match.group(1).split(",")]
                    updated_match = re.search(r"updated:\s*(\S+)", fm)

            # Modified time
            stat = f.stat()
            mod_date = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")

            wiki_data["pages"].append({
                "name": f.name,
                "path": rel_str,
                "category": category,
                "updated": mod_date,
                "size": len(content),
                "tags": tags[:5],
            })
            wiki_data["stats"]["total_pages"] += 1
            wiki_data["stats"]["total_chars"] += len(content)

    # --- Activity (recent imports + wiki updates) ---
    activity_data = []

    # Recent conversation logs
    conv_dir = Path("/app/data/brain/raw/conversations")
    if conv_dir.exists():
        for f in sorted(conv_dir.glob("*.md"), reverse=True)[:3]:
            stat = f.stat()
            activity_data.append({
                "type": "conversation",
                "message": f"会話ログ: {f.stem}",
                "time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    # Recent imports (processed)
    processed_dir = Path("/app/data/brain/import/processed")
    if processed_dir.exists():
        for f in sorted(processed_dir.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
            stat = f.stat()
            activity_data.append({
                "type": "import",
                "message": f"インポート: {f.name}",
                "time": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })

    # Sort by time desc
    activity_data.sort(key=lambda x: x.get("time", ""), reverse=True)

    # --- System info ---
    tunnel_url = ""
    tunnel_file = Path("/app/data/brain/tunnel_url.txt")
    if tunnel_file.exists():
        tunnel_url = tunnel_file.read_text().strip()

    system_data = {
        "tunnel_url": tunnel_url,
        "model": "Claude Opus",
        "uptime": _get_uptime(),
    }

    return {
        "calendar": calendar_data,
        "emails": emails_data,
        "wiki": wiki_data,
        "activity": activity_data[:10],
        "system": system_data,
    }


def _get_uptime() -> str:
    """サーバー稼働時間を返す"""
    try:
        import time
        uptime_seconds = time.time() - _start_time
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "--"


# ─── 自動改善ログ閲覧 ───
@app.get("/api/brain/improvements")
async def api_brain_improvements(
    request: Request, _: str = Depends(require_api_key), days: int = 7
):
    """自動検知された不満足回答と改善ログを返す（直近N日）"""
    from improvement_trigger import AUTO_IMPROVE_LOG, SYSTEM_IMPROVEMENTS_DIR

    entries = []
    if AUTO_IMPROVE_LOG.exists():
        try:
            with open(AUTO_IMPROVE_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"read improvements log error: {e}")

    # 日付フィルタ
    cutoff = datetime.now() - timedelta(days=days)
    filtered = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e.get("timestamp", ""))
            if ts >= cutoff:
                filtered.append(e)
        except Exception:
            continue

    # サマリ
    summary = {
        "total_detected": len(filtered),
        "triggered": sum(1 for e in filtered if e.get("applied")),
        "skipped_cooldown": sum(1 for e in filtered if e.get("skipped") == "cooldown"),
        "skipped_llm_ok": sum(1 for e in filtered if e.get("skipped") == "llm_satisfied"),
    }
    by_source = {}
    for e in filtered:
        src = e.get("source", "?")
        by_source[src] = by_source.get(src, 0) + 1
    summary["by_source"] = by_source

    return {
        "summary": summary,
        "entries": filtered[-50:],  # 直近50件
        "log_files": [
            f.name for f in sorted(SYSTEM_IMPROVEMENTS_DIR.glob("*.md"), reverse=True)[:14]
        ] if SYSTEM_IMPROVEMENTS_DIR.exists() else [],
    }


@app.get("/api/brain/usage")
async def api_brain_usage(
    request: Request, _: str = Depends(require_api_key), days: int = 30
):
    """うみやまAI 利用状況 (= 全社員) を返す。

    ★2026-05-27 海山指示「全社員のうみやまAI 利用状況はダッシュボードに反映されてる?」
    対応。data/brain/metrics/daily/YYYY-MM-DD.json を直近 N 日分集約して返す。
    今まで dashboard endpoint には含まれてなかった (= calendar / email / wiki のみ)。

    Returns:
        {
          "days": int,
          "daily": list[dict],         # 日毎の集計 (= JSON 直)
          "summary": {                  # 過去 N 日合計
            "total_conversations": int,
            "total_turns": int,
            "unique_users_in_period": int,
            "avg_conversations_per_day": float,
            "topic_distribution": dict[str, int],
          },
          "trend": {                    # トレンド (= 直近 7 日 vs 前 7 日)
            "conversations_change_pct": float,
            "users_change_pct": float,
          },
          "missing_days": list[str],    # daily file 欠落日 (= 健全性 check)
        }
    """
    from pathlib import Path as _P
    metrics_dir = _P("/app/data/brain/metrics/daily")
    if not metrics_dir.exists():
        return {"error": "metrics dir not found", "daily": [], "summary": {}, "trend": {}}

    cutoff = (datetime.now() - timedelta(days=days)).date()
    daily = []
    missing = []
    for i in range(days):
        d = (datetime.now() - timedelta(days=i + 1)).date()  # 昨日から N 日遡る
        f = metrics_dir / f"{d.isoformat()}.json"
        if f.exists():
            try:
                daily.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"usage metrics parse error {f.name}: {e}")
                missing.append(d.isoformat())
        else:
            missing.append(d.isoformat())
    daily.sort(key=lambda x: x.get("date", ""))

    # サマリ集計
    total_conv = sum((d.get("volume") or {}).get("total_conversations", 0) for d in daily)
    total_turns = sum((d.get("volume") or {}).get("total_turns", 0) for d in daily)
    unique_users_all = set()
    topic_dist: dict = {}
    for d in daily:
        users = d.get("users") or {}
        for u in (users.get("power_users") or []) + (users.get("new_users") or []):
            unique_users_all.add(u)
        for cat, n in ((d.get("topics") or {}).get("distribution") or {}).items():
            topic_dist[cat] = topic_dist.get(cat, 0) + n

    summary = {
        "total_conversations": total_conv,
        "total_turns": total_turns,
        "unique_users_in_period": len(unique_users_all),
        "avg_conversations_per_day": round(total_conv / max(len(daily), 1), 2),
        "topic_distribution": topic_dist,
    }

    # トレンド (= 直近 7 日 vs 前 7 日)
    trend = {}
    if len(daily) >= 14:
        recent_7 = daily[-7:]
        prev_7 = daily[-14:-7]
        recent_conv = sum((d.get("volume") or {}).get("total_conversations", 0) for d in recent_7)
        prev_conv = sum((d.get("volume") or {}).get("total_conversations", 0) for d in prev_7)
        recent_users = set()
        prev_users = set()
        for d in recent_7:
            for u in ((d.get("users") or {}).get("power_users") or []) + ((d.get("users") or {}).get("new_users") or []):
                recent_users.add(u)
        for d in prev_7:
            for u in ((d.get("users") or {}).get("power_users") or []) + ((d.get("users") or {}).get("new_users") or []):
                prev_users.add(u)
        trend["conversations_change_pct"] = round(
            (recent_conv - prev_conv) / max(prev_conv, 1) * 100, 1
        )
        trend["users_change_pct"] = round(
            (len(recent_users) - len(prev_users)) / max(len(prev_users), 1) * 100, 1
        )

    return {
        "days": days,
        "daily": daily,
        "summary": summary,
        "trend": trend,
        "missing_days": missing,
        "total_history_users": sum(1 for _ in _P("/app/data/brain/clone_history").glob("*.jsonl")) if _P("/app/data/brain/clone_history").exists() else 0,
    }


@app.get("/api/brain/wiki/{wiki_path:path}")
async def api_brain_wiki_page(
    wiki_path: str, request: Request, _: str = Depends(require_api_key)
):
    """Wiki個別ページ取得"""
    wiki_dir = Path("/app/data/brain/wiki")
    file_path = wiki_dir / wiki_path

    # Path traversal防止
    try:
        rel = file_path.resolve().relative_to(wiki_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    # ★2026-07-10 (世界基準評価 #2): 兄弟 endpoint /api/brain/wiki?path= (6602) は
    #   深層 private (personal/ + interview/) を is_deep_private_rel で拒否しているのに、
    #   本 path 版だけ gate 漏れ = 同じ operator key (?key= は LINE URL に埋まる) で
    #   interview/ (家族・弱さ・金・体) が全文読めた。片系 bypass を閉じる (可視性一貫化)。
    from brain_wiki_helpers.domain import is_deep_private_rel
    from brain_wiki_helpers.visibility import parse_clone_visibility
    if is_deep_private_rel(rel):
        raise HTTPException(status_code=404, detail="Page not found")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Page not found")

    content = file_path.read_text(encoding="utf-8")
    # ★2026-07-10 (世界基準評価 #2): clone_visibility: private も operator key から遮断
    if parse_clone_visibility(content) == "private":
        raise HTTPException(status_code=404, detail="Page not found")
    return {"path": wiki_path, "content": content}
