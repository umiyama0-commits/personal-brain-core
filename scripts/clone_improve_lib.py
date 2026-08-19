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
    # ★2026-08-03: temperature 非対応モデル (Opus 4.8 / Fable 5) に送ると Anthropic が 400 →
    # litellm が無言で gpt-4o へ fallback する。judge (model="smart", temperature=0.0) がこれで
    # gpt-4o に落ち、bot (GPT-5.4) と同一 provider = §1.15 の self-eval 防壁が無効化されていた。
    # 呼び元が temperature を渡していても、非対応モデルなら落とす (呼び元総当たり修正を不要に)。
    try:
        import sys as _sys
        if str(APP_ROOT) not in _sys.path:
            _sys.path.insert(0, str(APP_ROOT))
        from brain_wiki_helpers.model_params import supports_temperature
        if not supports_temperature(model):
            payload_base.pop("temperature", None)
    except Exception as _mp_err:
        # 落ちても従来動作 (temperature 送信) に戻るだけ。ただし silent にしない
        logger.warning(f"model_params guard 適用失敗 (従来動作で継続): {_mp_err}")
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


_LINE_QUOTA_STATE = IMPROVE_DIR / ".line_quota_month.json"


def _days_left_in_month(now: datetime) -> int:
    """当日を含む「今月の残り日数」。"""
    import calendar
    return calendar.monthrange(now.year, now.month)[1] - now.day + 1


def _daily_cap() -> int:
    """日次の **即時 push** 上限。月次枠の実残量から動的に決める。

    ★2026-08-03: 従来は静的 6 通/日 固定だった。6×31=186 通は digest 配信 (2/日=62) と
    critical を足すと無料枠 200 を確実に超える一方、朝の同時刻に info が集中すると
    午前中に 6 を使い切って残り終日 drop、という「枠は余っているのに落ちる」形になっていた
    (実測 8/3: 月枠 200 中 33 通しか使っていないのに 09:00 で日次上限到達)。
    残量ベースなら月初は緩く月末は締まり、締まった分は digest に回るだけで欠落しない。
    state 不在 / 月替り直後 / 取得失敗時は従来の静的既定へフォールバック (fail-open)。
    """
    static = int(os.getenv("LINE_PUSH_DAILY_CAP", "6") or 6)
    if static <= 0:
        return 0  # 明示 0 = 上限無効 (従来互換)
    try:
        st = json.loads(_LINE_QUOTA_STATE.read_text(encoding="utf-8"))
    except Exception:
        return static
    now = datetime.now(JST)
    if st.get("month") != now.strftime("%Y-%m"):
        return static  # 月替り後まだ refresh していない
    limit = int(st.get("limit") or 0)
    lo = int(os.getenv("LINE_PUSH_DAILY_MIN", "2") or 2)
    hi = int(os.getenv("LINE_PUSH_DAILY_MAX", "12") or 12)
    if limit <= 0:
        return hi  # 無制限プラン (type=none) — 締める理由が無い
    days_left = max(1, _days_left_in_month(now))
    # critical 用の取り置き + digest 配信 (1日2回) の実費を先に確保してから按分する
    reserve = int(os.getenv("LINE_PUSH_CRITICAL_RESERVE", "40") or 40)
    budget = max(0, limit - int(st.get("used") or 0) - reserve - 2 * days_left)
    return max(lo, min(hi, budget // days_left))


def refresh_line_quota(force: bool = False) -> dict:
    """LINE 公式 API から月次枠と当月消費を取得し state に保存する (1日1回)。

    ★2026-08-03: 7/24〜7/31 に 429 (月次上限) で通知が落ちていたのを、月替りまで誰も
    検知できなかった (枯渇してから気付く状態)。消費率を毎日 state に持ち、
    LINE_QUOTA_WARN_PCT (既定 80%) 超で critical 通知する。
    実 API 呼び出しは 1日1回だけ (digest flush = 10:00/19:00 から呼ばれる)。
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    try:
        st = json.loads(_LINE_QUOTA_STATE.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    if not force and st.get("checked") == today and st.get("month") == now.strftime("%Y-%m"):
        return st
    if not token:
        return st
    try:
        with httpx.Client(timeout=15) as http:
            h = {"Authorization": f"Bearer {token}"}
            q = http.get("https://api.line.me/v2/bot/message/quota", headers=h).json()
            c = http.get("https://api.line.me/v2/bot/message/quota/consumption", headers=h).json()
    except Exception as e:
        logger.warning(f"LINE quota 取得失敗: {type(e).__name__}: {e}")
        return st
    limit = int(q.get("value") or 0) if q.get("type") == "limited" else 0
    used = int(c.get("totalUsage") or 0)
    st = {"checked": today, "month": now.strftime("%Y-%m"),
          "type": q.get("type", "?"), "limit": limit, "used": used}
    try:
        _LINE_QUOTA_STATE.parent.mkdir(parents=True, exist_ok=True)
        _LINE_QUOTA_STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass  # 保存失敗は静的 cap へ自然に縮退する
    pct = round(used * 100 / limit) if limit else 0
    warn = int(os.getenv("LINE_QUOTA_WARN_PCT", "80") or 80)
    # state を書いた **後** に呼ぶ (loud_fail → line_push → _daily_cap は state 読取のみ = 再帰しない)
    loud_fail("line_quota_pressure", not (limit and pct >= warn),
              detail=f"LINE 月次枠 {used}/{limit} 通 ({pct}%) を消費。残り {_days_left_in_month(now)} 日。"
                     "\n超過分は自動でダイジェストへ回送されるため欠落はしないが、"
                     "critical 通知の余裕が細るためプラン見直しの検討時期。",
              threshold=1, cooldown_h=48)
    return st


def _personal_quota_ok(enforce: bool = True) -> bool:
    """personal LINE の日次送信上限 (★2026-06-11 海山指示「通知の数は減らしてよい」)。

    無料枠 200通/月 を alert storm (flapping monitor 等) が数日で食い潰した再発防止。
    上限超の非critical は **digest queue へ回送** (★2026-08-03 — 従来は drop で欠落していた。
    ★2026-07-10 LW 迂回廃止は維持 — 海山「LW は社員公開用」)。critical は enforce=False で
    呼ばれ、カウントは記録しつつ cap では止めない (= 月間会計の可視性は維持、配達優先)。
    """
    cap = _daily_cap()
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


def model_family(model: str) -> str:
    """model alias / 生 ID から系列 ("openai" / "anthropic" / "other") を返す。

    ★2026-08-03 §1.15 DA: 従来は alias 集合の等値判定だけだったため、.env に **生の ID**
    (`CLONE_PUBLIC_PROD_MODEL=gpt-5.4` 等。§1.19③ は推論経路のコード hardcode を禁じているが
    .env の値は対象外なので現実に起こりうる) を入れると「未知 alias → Claude 扱い」に落ち、
    judge も OpenAI になって **self-eval 防壁が無音で消える**ことが実証された。
    alias 表に無い時は生 ID の provider marker で補う。
    """
    m = (model or "").strip().lower()
    if not m:
        return "other"
    if m in _OPENAI_ALIASES:
        return "openai"
    if m in {"smart", "supervisor", "smart-sonnet", "smart-legacy"}:
        return "anthropic"
    # 生 ID / 未知 alias の fallback (alias 表の保守漏れを塞ぐ)
    if any(k in m for k in ("gpt", "openai/", "o1-", "o3-", "luna", "sol")):
        return "openai"
    if any(k in m for k in ("claude", "anthropic/", "opus", "sonnet", "haiku", "fable", "mythos")):
        return "anthropic"
    return "other"


def pick_cross_family_judge(bot_model: str = "") -> str:
    """bot 側 model から「別系列の judge alias」を返す (self-eval loop 遮断)。

    bot が OpenAI 系 → judge は Claude (smart)。bot が Anthropic 系 → judge は GPT (smart-gpt)。
    **系列不明** は安全側に倒して Claude (smart) を返す — 未知 model は OpenAI 系である可能性が
    あり、"other→smart-gpt" だと同系列に当たりうるため (DA 実証の穴)。
    bot_model 未指定は env CLONE_PUBLIC_PROD_MODEL (default smart)。
    """
    bot = bot_model or os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
    fam = model_family(bot)
    if fam == "openai":
        return "smart"
    if fam == "anthropic":
        return "smart-gpt"
    return "smart"


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


def line_push(text: str, critical: bool = False, *, allow_digest: bool = True,
              bypass_cap: bool = False) -> bool:
    """海山への通知。主経路 = personal LINE (ALIGNMENT_TARGET_USER 宛)。

    ★2026-07-10 海山指示「LINE WORKS はあくまで社員公開用」: うみやまAI DM への
    LW fallback は **critical=True のみ** (= bot 死/security/watchdog/loud_fail 等、
    配達保証が必要な系統)。info/warning/レポート類 (default critical=False) は
    personal LINE 限定 — quota 超過・送信失敗時は log を残して False (LW に流さない)。
    critical は日次 cap もバイパスして personal を先に試す (LW は本当に届かない時だけ)。
    env `LW_FALLBACK_DISABLE=1` で critical でも LW 完全遮断 (通知は personal のみ)。

    ★2026-08-03: 日次上限に当たった非 critical は **drop せず digest queue へ回送** する。
    digest は 1日2回 1通に集約されるので追加コストは実質ゼロで、内容だけが保全される。
    `allow_digest=False` は digest flush 自身と queue 書込失敗 fallback からの呼び出し用

    ★2026-08-17 デッドロック修正 (実害 15 日): 月枠 200 の残量から日次 cap が動的に決まる
    (実測 8/17: 171/200 使用 → cap=2)。critical が先に cap を使い切ると、非 critical は
    digest queue へ回送されるが、**その digest flush 自身も同じ cap に弾かれて drop** し、
    draining file に溜まったまま二度と出ない。結果、info 通知は **8/2 を最後に 15 日間
    1 通も届かず 97 件が滞留**していた (loud_fail を配線しても届かない = 監視全体が盲目)。
    digest は最大 20 件を 1 通に集約したものなので、cap で止める意味が無い (止めるほど
    1 通あたりの情報量が増えるだけ)。`bypass_cap=True` で cap を回避する — カウントは
    従来どおり記録するので月間会計の可視性は落ちない。
    (= 回送先が自分自身になる無限ループの防止。この 2 経路だけが False を渡す)。
    """
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
    user = os.getenv("ALIGNMENT_TARGET_USER")
    allow_lw = critical and os.getenv("LW_FALLBACK_DISABLE", "") != "1"
    # 日次 cap は非 critical のみに適用 (critical は月200通を割いてでも personal 優先。
    # enforce=False でカウントだけ記録 = 月間会計の可視性を維持、DA 指摘反映)
    if token and user:
        _exempt = critical or bypass_cap
        if not _personal_quota_ok(enforce=not _exempt) and not _exempt:
            if allow_digest and os.getenv("NOTIFY_DIGEST_DISABLE", "") != "1":
                logger.info("line_push 日次上限 → digest queue へ回送 (drop しない)")
                return line_push_digest(text, "遅延配信")
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


# ★2026-07-20 Umiyama AI Agent 正式化 (海山「無用な通知等は極力なくす」):
# info/report 系の push は即時送信せず queue に積み、1日2回 (10:00/19:00 cron) に
# 1 通へ集約して配信する。空なら配信自体しない。critical/actionable (bot死・売上FAIL・
# CI赤・リマインダー・週次承認待ち等) は従来どおり line_push 即時。
NOTIFY_DIGEST_QUEUE = DATA_BRAIN / "notify_digest_queue.jsonl"


DIGEST_MAX_PER_FLUSH = 20
DIGEST_STALE_H = 26  # queue 最古 entry がこれを超えたら flush 不動作疑い (dead-man)


def line_push_digest(text: str, component: str = "") -> bool:
    """info 系通知をダイジェスト queue へ積む (即時 push しない)。

    escape hatch: env NOTIFY_DIGEST_DISABLE=1 で従来の即時 line_push に戻る。
    queue 書込失敗時は通知を失わないよう即時 push へ fallback (fail-open)。
    ★dead-man (cross-check DA): queue 最古 entry が DIGEST_STALE_H 超 = flush cron が
    回っていない疑い → writer 側から loud_fail (flush 自身が死んでいても検知できる網)。
    """
    if os.getenv("NOTIFY_DIGEST_DISABLE", "") == "1":
        return line_push(text, allow_digest=False)
    import fcntl
    import time as _time
    try:
        NOTIFY_DIGEST_QUEUE.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts": datetime.now(JST).strftime("%m/%d %H:%M"),
            "epoch": int(_time.time()),
            "component": component or "info",
            "text": text[:2000],
        }, ensure_ascii=False)
        with open(NOTIFY_DIGEST_QUEUE, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(entry + "\n")
        try:
            first = NOTIFY_DIGEST_QUEUE.read_text(encoding="utf-8").split("\n", 1)[0]
            oldest = json.loads(first).get("epoch", 0)
            stale = bool(oldest) and (_time.time() - oldest) > DIGEST_STALE_H * 3600
            loud_fail("notify_digest_stale", not stale,
                      detail=f"digest queue 最古 entry が {DIGEST_STALE_H}h 超 = flush cron 不動作疑い",
                      threshold=3, cooldown_h=24)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.warning(f"digest queue append failed ({e}) → 即時 push に fallback")
        return line_push(text, allow_digest=False)


def _digest_entry_text(e: dict, cap: int = 600) -> str:
    """entry 本文の整形。長文は head+tail (末尾の「詳細: <path>」ポインタを保全)。"""
    t = e.get("text", "")
    if len(t) <= cap:
        return t
    return t[:cap - 110] + "\n…\n" + t[-100:]


def flush_notify_digest(dry_run: bool = False) -> int:
    """queue を 1 通に集約して line_push。空なら何もしない (=0)。戻り値は配信件数。

    ★cross-check 反映 (2026-07-20):
    - rename-drain 方式: queue を .draining へ atomic rename してから読む。truncate 方式だと
      container writer (owner_memory 等) の append が read〜truncate 窓で消えていた
      (bind mount 越しの flock は host↔container 間で無効 = Docker Desktop virtiofs の実態)。
      rename 後の新規 append は新 queue file へ入り、消えない。
    - 21 件目以降は捨てず queue へ書き戻して次回 flush へ持ち越し
    - 配信済 entry は notify_digest_sent.jsonl に監査保存 (housekeeping が rotate)
    - 成否確定点で loud_fail (§1.18 — 17+ 系統の配達を集約した単一 chokepoint のため必須)
    """
    import time as _time
    refresh_line_quota()  # 1日2回のここが月次枠 snapshot の更新点 (§1.18 枯渇の事前検知)
    draining = NOTIFY_DIGEST_QUEUE.with_suffix(".draining.jsonl")
    try:
        if not draining.exists():  # 前回失敗の持ち越しがあればそれを先に処理
            if not NOTIFY_DIGEST_QUEUE.exists():
                return 0
            os.rename(NOTIFY_DIGEST_QUEUE, draining)
            _time.sleep(0.05)  # rename 直前に open 済みの in-flight write を着地させる
        lines = [ln for ln in draining.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            draining.unlink(missing_ok=True)
            return 0
        entries = []
        for ln in lines:
            try:
                entries.append(json.loads(ln))
            except Exception:
                entries.append({"ts": "", "component": "raw", "text": ln[:500]})
        send, carry = entries[:DIGEST_MAX_PER_FLUSH], entries[DIGEST_MAX_PER_FLUSH:]
        parts = [f"🤖 Umiyama AI Agent — まとめ ({len(send)}件"
                 + (f"、他 {len(carry)} 件は次回" if carry else "") + ")"]
        for e in send:
            parts.append(f"\n■ {e.get('component', 'info')} ({e.get('ts', '')})\n{_digest_entry_text(e)}")
        msg = "\n".join(parts)[:4500]
        if dry_run:
            logger.info(f"[dry-run] digest {len(send)} 件:\n{msg[:500]}")
            return len(send)
        # bypass_cap: digest は最大 20 件の集約なので **日次** cap で止めない (2026-08-17
        # デッドロック修正)。ただし **月枠** は critical のために温存する — 残り MONTH_RESERVE
        # 通を切ったら info の集約配信は止め、queue に持ち越して翌月に回す (bot 死亡通知等の
        # 配達保証を info より優先。ここで止めても drop ではないので情報は失われない)。
        _reserve = int(os.getenv("NOTIFY_DIGEST_MONTH_RESERVE", "5"))
        try:
            _q = json.loads((IMPROVE_DIR / ".line_quota_month.json").read_text(encoding="utf-8"))
            _left = int(_q.get("limit") or 0) - int(_q.get("used") or 0)
        except Exception:
            _left = 10 ** 6            # 取得失敗は fail-open (従来どおり送る)
        if _left <= _reserve:
            logger.warning(f"digest flush: 月枠の残り {_left} 通 ≤ 予備 {_reserve} → "
                           "critical 温存のため今回は送らず持ち越し")
            return 0
        ok = line_push(msg, allow_digest=False, bypass_cap=True)
        loud_fail("notify_digest_flush", ok, detail="info 通知まとめ配信", threshold=3, cooldown_h=24)
        if not ok:
            logger.warning("digest flush の line_push 失敗 → draining 保持 (次回持ち越し)")
            return 0
        # 配信済を監査 log へ、超過分は queue へ書き戻し
        try:
            sent_log = NOTIFY_DIGEST_QUEUE.with_name("notify_digest_sent.jsonl")
            with open(sent_log, "a", encoding="utf-8") as f:
                for e in send:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        except Exception:
            pass
        for e in carry:
            line_push_digest(e.get("text", ""), e.get("component", "info"))
        draining.unlink(missing_ok=True)
        return len(send)
    except Exception as e:
        logger.warning(f"digest flush failed: {type(e).__name__}: {e}")
        loud_fail("notify_digest_flush", False, detail=f"flush 例外: {e}", threshold=3, cooldown_h=24)
        return 0


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



def _ensure_frontmatter(content: str) -> str:
    """新規 wiki に frontmatter を必ず付ける (★2026-08-06)。

    frontmatter が無い wiki は visibility の fail-safe で **private** に落ちる。
    実測: 自動生成された 63 件が全て private = 社員クローンから永久に読めず、
    knowledge_gap の自動修正アームが「書いた瞬間に見えない場所へ落ちる」状態だった。
    既定は internal (人が確認して public へ昇格させる) = 未レビュー文を社員に晒さない。
    """
    if content.lstrip().startswith("---"):
        return content
    today = datetime.now(JST).strftime("%Y-%m-%d")
    return (f"---\nupdated: {today}\nconfidence: medium\n"
            f"clone_visibility: internal\nreview: pending\n---\n" + content.lstrip("\n"))


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
        p.write_text(_ensure_frontmatter(content), encoding="utf-8")
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
