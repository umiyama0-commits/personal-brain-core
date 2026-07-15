"""
clone_improve_lib.py — clone (うみやまAI) 自動改善・トラッキング系の共通 lib

提供:
- clone_history 読み込み (直近 N 時間 / N 日)
- LLM 呼び出し (LiteLLM 経由)
- ログ書き込み (auto_edit_log.jsonl)
- LINE Push 送信
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# パス
# ★2026-07-02 監査 P2 (prompt-diff-check-dead-since-june): default "/app" は container 基準。
# host で BRAIN_APP_ROOT 未設定のまま import されると (auto_deploy 直下等、cron_env 非経由)
# /app への mkdir が Read-only で落ち、prompt_diff_check が 6/1 から丸ごと死んでいた。
# /app が実在しない環境 (= host) では repo root (= scripts/ の親) に fallback。container は不変。
_DEFAULT_APP_ROOT = "/app" if os.path.isdir("/app") else str(Path(__file__).resolve().parents[1])
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", _DEFAULT_APP_ROOT))
DATA_BRAIN = APP_ROOT / "data" / "brain"
HISTORY_DIR = DATA_BRAIN / "clone_history"
IMPROVE_DIR = DATA_BRAIN / "clone_improve"
DRAFTS_DIR = IMPROVE_DIR / "drafts"
QUEUE_DIR = DRAFTS_DIR / "queue"
REPORTS_DIR = IMPROVE_DIR / "reports"
METRICS_DIR = DATA_BRAIN / "metrics" / "daily"
AUTO_EDIT_LOG = IMPROVE_DIR / "auto_edit_log.jsonl"
WIKI_DIR = DATA_BRAIN / "wiki"

# LiteLLM
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

JST = timezone(timedelta(hours=9))


def ensure_dirs():
    for d in [IMPROVE_DIR, DRAFTS_DIR / "judgment", DRAFTS_DIR / "decisions",
              QUEUE_DIR, REPORTS_DIR, METRICS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# === clone_history 読み込み ===
def load_conversations(since: datetime) -> list[dict]:
    """全 user の clone_history から since 以降の record を集める。"""
    records = []
    if not HISTORY_DIR.exists():
        return records
    for f in HISTORY_DIR.glob("*.jsonl"):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = r.get("timestamp", "")
                try:
                    rt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except Exception:
                    continue
                if rt >= since:
                    records.append(r)
        except Exception as e:
            logger.warning(f"failed to read {f}: {e}")
    records.sort(key=lambda x: x.get("timestamp", ""))
    return records


def group_by_session(records: list[dict], gap_minutes: int = 30) -> list[list[dict]]:
    """records を user_id 別 + 時間 gap でセッション化。"""
    by_user: dict[str, list[dict]] = {}
    for r in records:
        by_user.setdefault(r.get("user_id", ""), []).append(r)
    sessions = []
    for uid, lst in by_user.items():
        lst.sort(key=lambda x: x.get("timestamp", ""))
        cur: list[dict] = []
        last_ts = None
        for r in lst:
            try:
                ts = datetime.fromisoformat(r.get("timestamp", "").replace("Z", "+00:00"))
            except Exception:
                continue
            if last_ts and (ts - last_ts).total_seconds() > gap_minutes * 60:
                if cur:
                    sessions.append(cur)
                cur = []
            cur.append(r)
            last_ts = ts
        if cur:
            sessions.append(cur)
    return sessions


# === LLM 呼び出し ===
def _log_llm_usage(component: str, model: str, usage: dict) -> None:
    """背景ジョブの LLM usage を events.jsonl に記録(cost 計測の穴埋め、★2026-06-30)。
    aggregate_cost が turn_finished.usage を集計 → 夜間ジョブ等の Claude/OpenAI コストが
    dashboard に出る(従来は clone_respond のみ=下限だった)。fail-safe で本処理を止めない。"""
    try:
        if not usage:
            return
        from bot_events import log_bot_event   # scripts/ sibling、stdlib のみ=CI-safe
        log_bot_event(component, "turn_finished", model=model, usage=usage)
    except Exception:
        pass


async def call_llm(
    prompt: str,
    model: str = "smart",
    max_tokens: int = 6000,
    temperature: float | None = 0.2,
    timeout: float = 180.0,
    retries: int = 3,
    component: str = "background",
) -> str:
    """LiteLLM 経由 LLM 呼び出し (シンプル版、リトライ込み)。
    component: cost 集計の機能名(既定 'background')。呼び元が渡せば機能別コストが見える。
    temperature=None で payload から除外 (★2026-07-10: Claude Fable 5 は temperature/top_p
    送信で 400 拒否。supervisor 経路の呼び元は None を渡す)。"""
    last_err = None
    payload_base = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        payload_base["temperature"] = temperature
    async with httpx.AsyncClient() as http:
        for attempt in range(retries):
            try:
                resp = await http.post(
                    f"{LITELLM_URL}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                    json={
                        **payload_base,
                    },
                    timeout=timeout,
                )
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                resp.raise_for_status()
                data = resp.json()
                _log_llm_usage(component, data.get("model") or model, data.get("usage"))
                return data["choices"][0]["message"]["content"]
            except Exception as e:
                last_err = e
                logger.warning(f"LLM call failed (attempt {attempt+1}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"LLM failed after {retries} retries: {last_err}")


def extract_json(text: str) -> dict:
    """LLM 応答から JSON ブロックを抽出してパース。"""
    # ```json ... ``` を最優先
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # 先頭の { から最後の } を取る
    s = text.find("{")
    e = text.rfind("}")
    if s >= 0 and e > s:
        return json.loads(text[s:e+1])
    raise ValueError("No JSON found in LLM response")


# === ログ書き込み ===
def append_jsonl(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


# === LINE Push (海山個人 LINE Bot 経由、失敗時は LINE Works DM に fallback) ===
_LINE_PUSH_STATE = IMPROVE_DIR / ".line_push_daily.json"


def _personal_quota_ok(enforce: bool = True) -> bool:
    """personal LINE の日次送信上限 (★2026-06-11 海山指示「通知の数は減らしてよい」)。

    無料枠 200通/月 を alert storm (flapping monitor 等) が数日で食い潰した再発防止。
    LINE_PUSH_DAILY_CAP (default 6、0=無効) 超の非critical は **drop** (★2026-07-10
    LW 迂回廃止 — 海山「LW は社員公開用」)。critical は enforce=False で呼ばれ、
    カウントは記録しつつ cap では止めない (= 月間会計の可視性は維持、配達優先)。
    """
    cap = int(os.getenv("LINE_PUSH_DAILY_CAP", "6") or 6)
    if cap <= 0:
        return True
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
    try:
        st = json.loads(_LINE_PUSH_STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    if st.get("date") != today:
        st = {"date": today, "n": 0}
    if enforce and int(st.get("n") or 0) >= cap:
        logger.warning(f"line_push 日次上限 {cap} 到達 (非critical は drop)")
        return False
    st["n"] = int(st.get("n") or 0) + 1
    try:
        _LINE_PUSH_STATE.parent.mkdir(parents=True, exist_ok=True)
        _LINE_PUSH_STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass  # 記録失敗は送信を止めない
    return True


# ─── 系列分離 judge の単一判定点 (★2026-07-05 Fable5 prompt 監査) ──────────────
# 従来は clone_style_regression.py だけが動的分離を持ち、hallucination_check /
# external_eval / response_quality_judge は "smart-gpt" ハードコード = 本番 clone が
# CLONE_PUBLIC_PROD_MODEL=smart-gpt (GPT-5.4) に移行した時点で 3 本とも同一系列
# self-eval に無音転落していた。判定式をここに 1 本化し、次のモデル切替での drift を防ぐ。
# alias 名の "gpt" 部分文字列判定は fast/default (実体 gpt-4o) を誤判定するため、
# litellm_config.yaml の実プロバイダで列挙 (regression の cross-check 済みロジックを移植)。
_OPENAI_ALIASES = {"smart-gpt", "smart-gpt-pro", "fast-gpt", "fast", "default",
                   "smart-fallback", "code", "code-max"}


def pick_cross_family_judge(bot_model: str = "") -> str:
    """bot 側 model alias から「別系列の judge alias」を返す (self-eval loop 遮断)。

    bot が OpenAI 系 → judge は Claude (smart)。bot が Claude 系 (または未知 alias) →
    judge は GPT (smart-gpt)。bot_model 未指定は env CLONE_PUBLIC_PROD_MODEL (default smart)。
    """
    bot = (bot_model or os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")).strip().lower()
    return "smart" if bot in _OPENAI_ALIASES else "smart-gpt"


def supervisor_model() -> str:
    """システム全体の監督者層 (synthesis/判断) の model alias (★2026-07-10 海山指示)。

    litellm `supervisor` = Claude Fable 5 (fallback: smart=Opus 4.8 → smart-fallback)。
    対象 = 低頻度・高judgment の synthesis 系のみ (clone_auto_improve 日次判断 /
    clone_weekly_report / ai_research_agent 提案 ≈ 月40回 → トークン微小)。
    **judge/verifier 層 (regression/hallucination/external-eval) は対象外** —
    bot (Claude) と別系列で self-eval loop を遮断する原則 (pick_cross_family_judge)
    を維持する。同系列の Fable 5 を judge に使うとこの防壁が消える。
    env SUPERVISOR_MODEL で override 可 (= 即時ロールバック用)。
    """
    return os.getenv("SUPERVISOR_MODEL", "supervisor").strip() or "supervisor"


def line_push(text: str, critical: bool = False) -> bool:
    """海山への通知。主経路 = personal LINE (ALIGNMENT_TARGET_USER 宛)。

    ★2026-07-10 海山指示「LINE WORKS はあくまで社員公開用」: うみやまAI DM への
    LW fallback は **critical=True のみ** (= bot 死/security/watchdog/loud_fail 等、
    配達保証が必要な系統)。info/warning/レポート類 (default critical=False) は
    personal LINE 限定 — quota 超過・送信失敗時は log を残して False (LW に流さない)。
    critical は日次 cap もバイパスして personal を先に試す (LW は本当に届かない時だけ)。
    env `LW_FALLBACK_DISABLE=1` で critical でも LW 完全遮断 (通知は personal のみ)。
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user = os.getenv("ALIGNMENT_TARGET_USER")
    allow_lw = critical and os.getenv("LW_FALLBACK_DISABLE", "") != "1"
    # 日次 cap は非 critical のみに適用 (critical は月200通を割いてでも personal 優先。
    # enforce=False でカウントだけ記録 = 月間会計の可視性を維持、DA 指摘反映)
    if token and user:
        if not _personal_quota_ok(enforce=not critical) and not critical:
            logger.warning("line_push 日次上限 (非critical) → 通知 drop (LW には流さない)")
            return False
    if token and user:
        try:
            with httpx.Client(timeout=15) as http:
                resp = http.post(
                    "https://api.line.me/v2/bot/message/push",
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json"},
                    json={"to": user, "messages": [{"type": "text", "text": text[:4900]}]},
                )
                if resp.status_code == 200:
                    return True
                # ★2026-06-11: 非200 を可視化 (旧実装は握りつぶし → 無料枠200通/月の
                # 枯渇 (429) で全通知が6日間 silent fail してたのを誰も検知できなかった)
                logger.warning(
                    f"line_push 非200: {resp.status_code} {resp.text[:120]}"
                    f" → {'LW fallback' if allow_lw else 'drop (非critical)'}")
        except Exception as e:
            logger.warning(
                f"line_push failed: {e} → {'LW fallback' if allow_lw else 'drop (非critical)'}")
    else:
        logger.info(
            "LINE_CHANNEL_ACCESS_TOKEN or ALIGNMENT_TARGET_USER not set"
            f" → {'LW fallback' if allow_lw else 'drop (非critical)'}")
    return _lw_admin_push(text) if allow_lw else False


# ★2026-07-02 監査 バッチC (loud-fail 標準、CLAUDE.md §1.18): 背景プロセスの silent 死対策の
# 共通ゲート。監査で「自動化が死んでも通知が出ない」経路が 5 系統実害化していた
# (consultant 配信 6.7日 / hallucination 33日 / cron-install 37連敗 / prompt_diff 6/1〜 / sales_accuracy)。
LOUD_FAIL_STATE = IMPROVE_DIR / "loud_fail_state.json"


def loud_fail(component: str, ok: bool, detail: str = "", *,
              threshold: int = 3, cooldown_h: float = 24.0) -> bool:
    """背景プロセスの成否確定点で毎回呼ぶ (成功時も呼んで streak をリセットさせる)。

    ok=False の連続回数を component 毎に数え、threshold 連続で line_push (LW fallback 付) に
    エスカレーション。以後 cooldown_h おきに再通知。戻り値 = 通知を送ったか。
    state は fcntl lock で RMW 保護 (JSONL queue の lock 無し RMW 事故の教訓)。
    通知自体の失敗も握らない (streak は進み、次回また試行される)。
    """
    import fcntl
    import time as _time
    try:
        LOUD_FAIL_STATE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOUD_FAIL_STATE, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            raw = f.read().strip()
            try:
                st = json.loads(raw) if raw else {}
            except Exception:
                st = {}
            rec = st.get(component) or {}
            alerted = False
            if ok:
                rec = {"streak": 0, "last_alert": rec.get("last_alert", 0)}
            else:
                rec["streak"] = int(rec.get("streak", 0)) + 1
                now = _time.time()
                if rec["streak"] >= threshold and \
                        now - float(rec.get("last_alert", 0)) > cooldown_h * 3600:
                    # loud-fail = silent 死の配達保証が目的そのもの → critical (LW fallback 可)
                    alerted = line_push(
                        f"🔇→🔊 loud-fail: {component} が {rec['streak']} 回連続で失敗/縮退。"
                        f" {detail[:200]}", critical=True)
                    if alerted:
                        rec["last_alert"] = now
            st[component] = rec
            f.seek(0)
            f.truncate()
            f.write(json.dumps(st, ensure_ascii=False))
        return alerted
    except Exception as e:
        logger.warning(f"loud_fail 自体が失敗 (非致命): {e}")
        return False


def _lw_build_assertion(client_id: str, service_account: str, pem: str) -> str:
    """LINE Works token 用の RS256 JWT (claims は lineworks_bot._build_jwt と同一)。

    ホスト python に pyjwt が無いため cryptography で直接構築 (= lineworks_bot 非依存。
    docker/コンテナが死んでいてもアラートが届く独立経路を維持する)。
    """
    import base64
    import time as _time
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def _b64u(b: bytes) -> bytes:
        return base64.urlsafe_b64encode(b).rstrip(b"=")

    now = int(_time.time())
    header = _b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64u(json.dumps({
        "iss": client_id, "sub": service_account,
        "iat": now, "exp": now + 3600,
    }).encode())
    signing_input = header + b"." + claims
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return (signing_input + b"." + _b64u(sig)).decode()


def _lw_admin_push(text: str) -> bool:
    """LINE Works DM (うみやまAI → 海山) への fallback 送信 (★2026-06-11)。

    personal LINE の月間無料枠 (200通) 枯渇・token失効時の迂回路。宛先は
    ADMIN_LW_USER_ID (services/auth.py の admin gate と共用)。未設定なら loud-skip。
    cron (sync) からのみ呼ばれる前提 (main.py/brain_wiki.py に line_push 呼出なし確認済)。
    """
    admin = os.getenv("ADMIN_LW_USER_ID", "")
    if not admin:
        logger.warning("LW fallback 不可: ADMIN_LW_USER_ID 未設定 (通知は届いていない)")
        return False
    client_id = os.getenv("LW_CLIENT_ID", "")
    client_secret = os.getenv("LW_CLIENT_SECRET", "")
    service_account = os.getenv("LW_SERVICE_ACCOUNT", "")
    bot_id = os.getenv("LW_BOT_ID", "")
    key_path = os.getenv("LW_PRIVATE_KEY_PATH", "")
    # .env の path はコンテナ基準 (/app/...)。ホスト cron では APP_ROOT に remap
    # (cron_env.sh が BRAIN_APP_ROOT=repo root を export 済)
    if key_path.startswith("/app/") and not os.path.exists(key_path):
        key_path = str(APP_ROOT / key_path[len("/app/"):])
    pem = ""
    if key_path and os.path.exists(key_path):
        try:
            pem = Path(key_path).read_text(encoding="utf-8")
        except Exception:
            pem = ""
    if not pem:
        pem = os.getenv("LW_PRIVATE_KEY", "")
    if not all([client_id, client_secret, service_account, bot_id, pem]):
        logger.warning("LW fallback 不可: LW_* env 不足")
        return False
    try:
        assertion = _lw_build_assertion(client_id, service_account, pem)
        with httpx.Client(timeout=20) as http:
            tok = http.post(
                "https://auth.worksmobile.com/oauth2/v2.0/token",
                data={"assertion": assertion,
                      "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                      "client_id": client_id, "client_secret": client_secret,
                      "scope": "bot"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            tok.raise_for_status()
            access = tok.json()["access_token"]
            resp = http.post(
                f"https://www.worksapis.com/v1.0/bots/{bot_id}/users/{admin}/messages",
                json={"content": {"type": "text",
                                  "text": ("📟 [system]\n" + text)[:1900]}},
                headers={"Authorization": f"Bearer {access}",
                         "Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("LW fallback: sent")
        return True
    except Exception as e:
        logger.warning(f"LW fallback failed: {e}")
        return False


# === eval 暴走ガード (★2026-06-11、6/10 の bot 1,012 turn ≈ $700 スパイク再発防止) ===
_EVAL_TURNS = {"n": 0}


def eval_turn_guard(default: int = 300) -> None:
    """bulk eval が bot (smart=Opus) を叩く直前に呼ぶ。プロセス内カウンタが
    EVAL_MAX_BOT_TURNS (default 300、0=無効) を超えたら RuntimeError で停止。
    意図的な大規模 run は env を明示して上げる (= コスト承認の代わり)。"""
    limit = int(os.getenv("EVAL_MAX_BOT_TURNS", str(default)) or default)
    if limit <= 0:
        return
    _EVAL_TURNS["n"] += 1
    if _EVAL_TURNS["n"] > limit:
        raise RuntimeError(
            f"eval_turn_guard: bot 呼出 {limit} 回超過 (コスト保護)。"
            f"意図的なら EVAL_MAX_BOT_TURNS={limit * 4} 等で明示して再実行")


# === wiki ファイル操作 (自動編集用) ===
def wiki_path(rel: str) -> Path:
    """wiki/ からの相対パスを絶対パスに。"""
    return WIKI_DIR / rel


def _replace_section(path: Path, anchor: str, new_content: str) -> bool:
    """markdown の `anchor` (例 "## XXX") 見出しで始まる section のみを new_content で置換。

    ★2026-06-07 エージェント評価: 旧実装は replace_section を overwrite に map し、LLM の section
    content で **ファイル全文を上書き = data loss** させていた。anchor を使う真の部分置換に。
    section = anchor 行 〜 次の同 level 以下 (同じか上位) の見出し直前まで。anchor 未発見なら False。
    """
    anchor = (anchor or "").strip()
    if not anchor.startswith("#") or not path.exists():
        return False
    a_level = len(anchor) - len(anchor.lstrip("#"))
    lines = path.read_text(encoding="utf-8").split("\n")
    start = next((i for i, ln in enumerate(lines) if ln.strip() == anchor), None)
    if start is None:
        return False  # anchor 見つからず → 新規 section は append で (全文上書きしない)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("#") and (len(s) - len(s.lstrip("#"))) <= a_level:
            end = j
            break
    body = new_content.rstrip("\n")
    if not body.lstrip().startswith(anchor):  # LLM が見出しを content に含めてなければ保持
        body = anchor + "\n" + body
    new_lines = lines[:start] + body.split("\n") + lines[end:]
    path.write_text("\n".join(new_lines), encoding="utf-8")
    return True


def safe_write_wiki(rel: str, content: str, mode: str = "append", section_anchor: str = "") -> bool:
    """wiki ファイルを安全に作成/追記/部分置換。

    mode:
      - create: 新規作成 (既存なら fail)
      - append: 末尾追記 (改行込み)
      - replace_section: section_anchor の section のみ置換 (★全文上書きしない)
      - overwrite: 全文上書き (★激減 guard 付き、auto-edit からは原則 replace_section 推奨)
    """
    p = wiki_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "create":
        if p.exists():
            return False
        p.write_text(content, encoding="utf-8")
        return True
    if mode == "append":
        with p.open("a", encoding="utf-8") as f:
            f.write("\n\n" + content if p.exists() else content)
        return True
    if mode == "replace_section":
        return _replace_section(p, section_anchor, content)
    if mode == "overwrite":
        # ★2026-06-07 評価: 激減 (= 既存の 50% 未満) は全文消失の疑い → 安全側で拒否 (data loss 防止)。
        if p.exists():
            existing = p.read_text(encoding="utf-8")
            if len(existing) >= 500 and len(content) < len(existing) * 0.5:
                logger.warning(f"safe_write_wiki overwrite 拒否: {rel} 激減 ({len(existing)}->{len(content)}字)")
                return False
        p.write_text(content, encoding="utf-8")
        return True
    return False
