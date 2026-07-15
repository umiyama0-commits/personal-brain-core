"""bot_uptime_monitor.py — Personal Brain bot 常時稼働監視 (★2026-05-24 海山指示)

「bot が作動してない、システム全体の稼働状況を常時確認するエージェント」を受けて新設。
5 分おきに /health + bot_events 受信状況を check、異常検出で海山に LINE Push alert。

# 検知 case

1. **bot プロセス死亡** (= /health が ok 返さない / connection error)
2. **LINE webhook 受信停止** (= 直近 N 分 turn_started 0 件、N=30 default)
3. **turn_failed 急増** (= 直近 1h で turn_failed >= 5 件)
4. **context_prefix_leak** (= fix #1 漏れ critical alert)
5. **特定 component の連続失敗** (= clone_respond の連続失敗 3 回 +)

# Alert mechanism

- LINE Push (= clone_improve_lib.line_push、ALIGNMENT_TARGET_USER 宛)
- 同 alert は 30 分 cooldown (= flood 防止、`data/brain/bot_uptime_alerts.jsonl` で履歴管理)
- exit code 0 = healthy, 1 = warning, 2 = critical

# usage

```
python3 scripts/bot_uptime_monitor.py                  # 1 回 check
python3 scripts/bot_uptime_monitor.py --silence-min 0  # cooldown 無視
python3 scripts/bot_uptime_monitor.py --check-only     # alert なし check のみ
```

# cron

`scripts/clone_cron.sh uptime-monitor` 経由で 5 分おき (= */5 * * * *)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

logger = logging.getLogger("bot_uptime_monitor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
ALERT_LOG = APP_ROOT / "data" / "brain" / "bot_uptime_alerts.jsonl"

# health endpoint (= docker network 経由 / 外部 URL fallback)
HEALTH_URL_LOCAL = "http://localhost:8000/health"
HEALTH_URL_PROD = "https://brain.example.com/health"

# bot_events 経由で受信状況確認
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from bot_events import iter_events, parse_since  # type: ignore
except Exception as e:
    logger.warning(f"bot_events import failed: {e}")
    iter_events = None  # type: ignore
    parse_since = None  # type: ignore

# LINE Push helper
try:
    from clone_improve_lib import line_push  # type: ignore
except Exception as e:
    logger.warning(f"clone_improve_lib import failed: {e}")
    def line_push(text: str, **_kw) -> bool:  # type: ignore  # critical= 互換 (stub)
        logger.error(f"[LINE PUSH stub] {text}")
        return False


# ─── 検知 thresholds ─────────────────────────────────────────
HEALTH_TIMEOUT_SEC = 10
TURN_SILENT_THRESHOLD_MIN = 30   # 30 分 turn 0 件で alert
TURN_FAILED_THRESHOLD = 5         # 1h で 5 件以上 failed で alert
COMPONENT_FAIL_STREAK = 3         # 連続 3 件 failed で alert
ALERT_COOLDOWN_MIN = 30           # 同 alert は 30 分待機

# ─── auto-remediation (★2026-05-25 海山指示「検知 + 自動修正」) ──────────
# 監視 alert (= bot_dead / webhook_silent) 検知時に「docker compose restart line-bot」
# を試行する。rate limit + health 再 check で recovery 判定、LINE Push は
# 「自動復旧 ✅」 vs 「自動復旧 失敗 🚨」 で文言切替。
#
# 制約:
# - 1 時間 max 3 回 restart (= flood / restart loop 防止)
# - destructive 度: line-bot コンテナのみ、cloudflared / litellm / redis 等は触らない
# - AUTO_REMEDIATE_ENABLED=0 で opt-out
# - 全 restart attempt は ALERT_LOG に「type=auto_restart」で記録
AUTO_REMEDIATE_ENABLED = os.getenv("AUTO_REMEDIATE_ENABLED", "1") == "1"
RESTART_RATE_LIMIT = 3       # 60 分 max
RESTART_RATE_WINDOW_MIN = 60
RESTART_WAIT_SEC = 30        # restart 後 health 再 check までの wait
REPO_ROOT_HOST = Path(os.getenv("BRAIN_REPO_ROOT", "/Users/brain/brain-agent"))
# ★2026-06-07 評価: 深夜低 traffic 帯 (既定 23:00-08:00 JST) の「turn 0 件」は正常な無 traffic であり
# receiver 詰まりではない。health OK の時はこの帯で webhook_silent の auto-restart/alert を skip し、
# 健全 bot の不要 restart を防ぐ (bot_dead = health 失敗 は hour 不問で別途処理)。
QUIET_HOURS_START_JST = int(os.getenv("UPTIME_QUIET_START_JST", "23"))
QUIET_HOURS_END_JST = int(os.getenv("UPTIME_QUIET_END_JST", "8"))


def _in_quiet_hours(hour: int) -> bool:
    """hour (JST 0-23) が quiet 帯か。start>end の夜跨ぎ (例 23-8) に対応。"""
    if QUIET_HOURS_START_JST <= QUIET_HOURS_END_JST:
        return QUIET_HOURS_START_JST <= hour < QUIET_HOURS_END_JST
    return hour >= QUIET_HOURS_START_JST or hour < QUIET_HOURS_END_JST


def _alert_seen_recently(alert_type: str, cooldown_min: int) -> bool:
    """過去 cooldown_min 分以内に同 alert_type が発火してたら True (= flood 防止)。"""
    if not ALERT_LOG.exists():
        return False
    cutoff = datetime.now(JST) - timedelta(minutes=cooldown_min)
    try:
        for line in reversed(ALERT_LOG.read_text(encoding="utf-8").splitlines()[-100:]):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("type") != alert_type:
                continue
            ts_str = r.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts > cutoff:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _log_alert(alert_type: str, severity: str, message: str, extra: dict | None = None):
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(JST).isoformat(),
        "type": alert_type,
        "severity": severity,
        "message": message,
    }
    if extra:
        rec.update(extra)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ─── 個別 check ───────────────────────────────────────────────
def check_health() -> dict:
    """bot プロセス /health を check。"""
    for url in (HEALTH_URL_LOCAL, HEALTH_URL_PROD):
        try:
            r = httpx.get(url, timeout=HEALTH_TIMEOUT_SEC)
            if r.status_code == 200:
                return {"ok": True, "url": url, "body": r.text[:120]}
        except Exception as e:
            logger.warning(f"health check failed {url}: {type(e).__name__}: {e}")
            continue
    return {"ok": False, "error": "all health URLs failed"}


READY_URL_LOCAL = "http://localhost:8000/ready"


def check_readiness() -> dict:
    """bot の /ready (readiness) を check (★2026-06-08 評価 SRE: /health は無条件 ok で
    「緑なのに retrieval/LLM が壊れてる」を検知できなかった。/ready は redis/chromadb/litellm
    の実疎通を見る)。restart はしない (依存側 transient blip での不要 restart 回避)。

    transient 除外: 503/error なら短い wait 後に 1 回だけ再 check し、両方失敗で not-ready 判定。
    """
    import time as _t

    def _probe():
        try:
            r = httpx.get(READY_URL_LOCAL, timeout=HEALTH_TIMEOUT_SEC)
            body = {}
            try:
                body = r.json()
            except Exception:
                pass
            return r.status_code, body
        except Exception as e:
            return None, {"error": f"{type(e).__name__}: {e}"}

    code, body = _probe()
    if code == 200:
        return {"ok": True, "status_code": 200}
    _t.sleep(8)  # transient blip 除外の短い wait (monitor は 5 分間隔なので許容)
    code2, body2 = _probe()
    if code2 == 200:
        return {"ok": True, "status_code": 200, "note": "recovered on recheck"}
    b = body2 if body2 else body
    checks = b.get("checks") if isinstance(b, dict) else {}
    failed = [k for k, v in (checks or {}).items() if v is False]
    return {"ok": False, "status_code": code2 or code, "failed_deps": failed, "body": b}


def check_recent_turns(silent_min: int = TURN_SILENT_THRESHOLD_MIN) -> dict:
    """過去 silent_min 分の turn_started 件数を確認。0 件なら webhook 停止疑い。"""
    if iter_events is None or parse_since is None:
        return {"ok": False, "error": "bot_events module unavailable"}
    try:
        since_sec = silent_min * 60
        events = list(iter_events(since_sec=since_sec))
        started = sum(1 for e in events if e.get("event") == "turn_started")
        finished = sum(1 for e in events if e.get("event") == "turn_finished")
        failed = sum(1 for e in events if e.get("event") == "turn_failed")
        # ★2026-06-10: webhook 到着数 (応答が必要なメッセージが届いた件数)。
        # 「到着あり & turn ゼロ = 真の receiver 詰まり (restart 価値あり)」 vs
        # 「到着ゼロ = 単なる無 traffic (restart 不要)」を区別する。
        # ★2026-07-02 監査 P2 (webhook-silent-counts-vapi-stream): component を lineworks に限定。
        # Vapi 音声通話の conversation-update/speech-update stream (毎秒1-2件、応答不要) を
        # 「応答すべき到着」と誤カウント → 通話中に webhook_silent 誤検知 (6/30 通話中 restart の
        # 実害) を根治。voice_alignment 等は webhooks_other として可視化のみ。
        _wh_all = [e for e in events if e.get("event") == "webhook_received"]
        webhooks_in = sum(1 for e in _wh_all if e.get("component") == "lineworks")
        webhooks_other = len(_wh_all) - webhooks_in
        return {
            "ok": True,
            "silent_min": silent_min,
            "started": started, "finished": finished, "failed": failed,
            "webhooks_in": webhooks_in,
            "webhooks_other": webhooks_other,
            # is_silent = 「応答すべき到着があったのに turn が 1 件も始まっていない」= 真の詰まり。
            # 到着ゼロ (= 無 traffic) は is_silent=False にして閑散時の不要 restart を根絶。
            "is_silent": started == 0 and webhooks_in > 0,
            "no_traffic": started == 0 and webhooks_in == 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_failure_burst(within_min: int = 60, threshold: int = TURN_FAILED_THRESHOLD) -> dict:
    """過去 within_min 分の turn_failed 件数を確認。threshold 以上で alert。"""
    if iter_events is None:
        return {"ok": False, "error": "bot_events module unavailable"}
    try:
        events = list(iter_events(since_sec=within_min * 60))
        failed = [e for e in events if e.get("event") == "turn_failed"]
        return {
            "ok": True,
            "within_min": within_min,
            "n_failed": len(failed),
            "is_burst": len(failed) >= threshold,
            "samples": [
                {"ts": e.get("ts", ""), "comp": e.get("component", ""),
                 "err": (e.get("error_class") or "") + ":" + (e.get("error_msg") or "")[:80]}
                for e in failed[-5:]
            ],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_auth_denied_burst(within_min: int = 60, threshold: int = 10) -> dict:
    """過去 within_min 分の admin 認証失敗 (auth_denied) 件数。threshold 以上で brute-force 疑い alert。

    ★2026-06-08 評価 Security: 特権 token の認証失敗を記録するようにした (alignment_trial._log_auth_denied)
    のを actionable にする = burst を検知して海山に通知 (= 「記録するが誰も見ない」を回避)。
    """
    if iter_events is None:
        return {"ok": False, "error": "bot_events module unavailable"}
    try:
        events = list(iter_events(since_sec=within_min * 60))
        denied = [e for e in events if e.get("event") == "auth_denied"]
        return {
            "ok": True,
            "within_min": within_min,
            "n_denied": len(denied),
            "is_burst": len(denied) >= threshold,
            "actions": sorted({e.get("action", "?") for e in denied}),
            "distinct_tokens": len({e.get("token_id", "") for e in denied}),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_context_leak(within_min: int = 60) -> dict:
    """context_prefix_leak event (= fix #1 漏れ critical) を確認。"""
    if iter_events is None:
        return {"ok": False, "error": "bot_events module unavailable"}
    try:
        events = list(iter_events(since_sec=within_min * 60))
        leaks = [e for e in events if e.get("event") == "context_prefix_leak"]
        return {
            "ok": True,
            "within_min": within_min,
            "n_leaks": len(leaks),
            "has_critical": len(leaks) > 0,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _commits_behind() -> int:
    """deployed HEAD が origin/main の何 commit 後ろか (= 未 deploy commit 数)。

    ★2026-06-25 誤検知 fix: 自前で fetch して真の remote と比較する。これにより
    (a) 新 commit が無い健全な安定 container を stale 扱いしない、
    (b) auto_deploy が fetch ごと止まっていても「新 commit があるのに古い」を検知できる。
    失敗時は 0 (= behind 不明なら uptime だけで誤発火させない安全側)。
    """
    try:
        repo = str(Path(__file__).resolve().parents[1])
        subprocess.run(["git", "-C", repo, "fetch", "--quiet", "origin", "main"],
                       capture_output=True, timeout=25)
        out = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD..origin/main"],
                             capture_output=True, text=True, timeout=10)
        return int((out.stdout or "0").strip() or "0")
    except Exception:
        return 0


def check_deploy_freshness(stale_threshold_hours: float = 24.0) -> dict:
    """container uptime + auto_deploy log から「deploy stale」判定 (★2026-05-24 海山指示)。

    silent staleness (= auto_deploy build failure 無通知の状態) を即発見可能化。
    /api/admin/deploy-status を内部叩いて結果取得。
    """
    try:
        r = httpx.get(
            "http://localhost:8000/api/admin/deploy-status",
            params={"token": os.getenv("ALIGNMENT_TRIAL_TOKEN", "")},
            timeout=10,
        )
        if r.status_code != 200:
            r = httpx.get(
                f"{HEALTH_URL_PROD.rsplit('/', 1)[0]}/api/admin/deploy-status",
                params={"token": os.getenv("ALIGNMENT_TRIAL_TOKEN", "")},
                timeout=10,
            )
        if r.status_code != 200:
            return {"ok": False, "error": f"endpoint status {r.status_code}"}
        d = r.json()
        uptime_h = d.get("container_uptime_hours", 0) or 0
        build_failures = d.get("build_failures_24h", 0)
        alerts = d.get("alerts", [])
        behind = _commits_behind()
        return {
            "ok": True,
            "uptime_hours": uptime_h,
            "build_failures_24h": build_failures,
            # ★2026-06-25 誤検知 fix: uptime 単独で stale としない。新 commit が無い健全な安定 container でも
            #   24h 超で誤発火していた (alert 疲労)。真の stale = build 失敗 or「未 deploy commit があるのに古いまま」。
            "is_stale": build_failures > 0 or (behind > 0 and uptime_h > stale_threshold_hours),
            "commits_behind": behind,
            "git_head": d.get("git_head_commit", ""),
            "alerts_from_endpoint": alerts,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ★2026-05-24 Tier 1: 個別 component の連続失敗 monitoring
#
# 既存 check_failure_burst は全 component 合算で 5 件閾値、
# component 単独 (= Cohere Rerank API 死亡 / Drive auth 失敗 / group context 更新失敗等)
# は検知漏れする可能性。component 別 streak で早期 alert。
CRITICAL_COMPONENTS = [
    ("clone_respond", "うみやまAI 応答生成"),
    ("clone_memory_update", "個別メモリー更新"),
    ("clone_group_context_update", "グループ context 更新"),
    ("sleep_time", "memory idle 整理"),
    ("drive_ingest", "Google Drive URL fetch"),
    ("cohere_rerank", "Cohere Rerank 呼出"),
]


def check_component_streak(within_min: int = 60, threshold: int = 3) -> dict:
    """tracked component 別に過去 within_min 分の turn_failed 件数を確認.

    1 component 単独で threshold 以上の連続失敗があれば is_burst_per_component に list。
    例: cohere_rerank が API key expire で 5 連続 失敗 → 即 alert (= 全体 5 件閾値より早い)
    """
    if iter_events is None:
        return {"ok": False, "error": "bot_events module unavailable"}
    try:
        events = list(iter_events(since_sec=within_min * 60))
        per_component = {}
        for comp, _label in CRITICAL_COMPONENTS:
            failed = [
                e for e in events
                if e.get("event") == "turn_failed" and e.get("component") == comp
            ]
            per_component[comp] = {
                "n_failed": len(failed),
                "is_burst": len(failed) >= threshold,
                "samples": [
                    {"ts": e.get("ts", ""),
                     "err": (e.get("error_class") or "") + ":" + (e.get("error_msg") or "")[:80]}
                    for e in failed[-3:]
                ],
            }
        bursts = [comp for comp, info in per_component.items() if info["is_burst"]]
        return {
            "ok": True,
            "within_min": within_min,
            "threshold": threshold,
            "per_component": per_component,
            "burst_components": bursts,
            "has_any_burst": bool(bursts),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def check_silent_skip(within_min: int = 30, threshold: int = 3) -> dict:
    """webhook_processed event (= 4173024) で potential_silent_skip=true が連続するか確認。

    silent skip 連続 = 「気づき/メモ alias 拡張で拾えない type のメッセージ」が漏れてる疑い。
    threshold 件以上の連続発生で warning。
    """
    if iter_events is None:
        return {"ok": False, "error": "bot_events module unavailable"}
    try:
        events = list(iter_events(since_sec=within_min * 60))
        webhooks = [e for e in events if e.get("event") == "webhook_processed"]
        silent_skips = [e for e in webhooks if e.get("potential_silent_skip")]
        return {
            "ok": True,
            "within_min": within_min,
            "n_webhooks": len(webhooks),
            "n_silent_skips": len(silent_skips),
            "is_silent_skip_burst": len(silent_skips) >= threshold,
            "samples": [
                {"ts": e.get("ts", ""), "user": (e.get("user_id") or "")[:8]}
                for e in silent_skips[-5:]
            ],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── auto-remediation (= 海山指示「検知 + 修正まで」、慎重 path) ─────────
# CLAUDE.md 1.3 で destructive command (= docker restart 等) は海山承認後のみ実行可。
# 自動実行はせず、LINE Push に「1 click 復旧 URL」を含めて 海山即実行可能化。
# 将来 enhancement: 海山が opt-in した auto-remediation pattern を学習 (= 連続 N 回成功なら自動化)

REMEDIATION_HINTS = {
    "deploy_stale": (
        "🛠️ auto_deploy 停止 or build failure 疑い。\n"
        "Mac Studio で:\n"
        "  cd ~/brain-agent && git pull\n"
        "  docker compose build line-bot && docker compose up -d --force-recreate line-bot\n"
        "  tail -50 data/brain/auto_deploy.log で原因確認"
    ),
    "bot_dead": (
        "🛠️ 即時復旧: SSH で Mac Studio に入り `cd ~/brain-agent && "
        "docker compose up -d --force-recreate line-bot`"
    ),
    "webhook_silent": (
        "🛠️ 真因候補: (a) LINE webhook URL が CF tunnel 経由で死亡 / "
        "(b) bot プロセス内部で receiver 停止 / (c) prefix logic で silent skip。\n"
        "確認: `docker logs line-bot --tail 50 | grep -iE \"webhook|error\"`"
    ),
    "failure_burst": (
        "🛠️ 真因候補: LLM proxy 接続失敗 / chromadb 並行アクセス / 個別 component bug。\n"
        "確認: `docker logs line-bot --tail 100 | grep -E \"ERROR|exception\"`"
    ),
    "context_leak": (
        "🛠️ critical fix: brain_index.py:search() の strip_context_prefix() 動作確認、\n"
        "brain_wiki_helpers/contextual.py の docstring 通り、retrieval 出口で剥がす invariant 厳守。"
    ),
    "silent_skip_burst": (
        "🛠️ /memo alias logic (= 4173024) で拾えない type のメッセージ漏れ疑い。\n"
        "確認: `grep webhook_processed data/brain/bot_events.jsonl | tail -5` で\n"
        "potential_silent_skip=true の query 内容を確認、新 prefix alias 検討。"
    ),
    # ★2026-05-24 Tier 1: component 別 hints
    "component_streak_clone_respond": (
        "🛠️ うみやまAI 応答生成連続失敗。LLM proxy (litellm) 確認:\n"
        "  docker logs litellm --tail 50 | grep -iE \"error|429|timeout\""
    ),
    "component_streak_clone_memory_update": (
        "🛠️ 個別メモリー更新失敗。fast-gpt 接続 or memory file write 確認:\n"
        "  docker logs line-bot --tail 100 | grep clone_memory_update"
    ),
    "component_streak_clone_group_context_update": (
        "🛠️ グループ context 更新失敗 (Tier 0)。fast-gpt または group_context file write 確認:\n"
        "  docker logs line-bot --tail 100 | grep clone_group_context_update"
    ),
    "component_streak_sleep_time": (
        "🛠️ sleep_time agent 失敗。smart モデル接続 or LLM JSON parse 確認:\n"
        "  docker logs line-bot --tail 100 | grep sleep_time"
    ),
    "component_streak_drive_ingest": (
        "🛠️ Google Drive 連携失敗。OAuth token / Service Account credential 確認:\n"
        "  ls -la data/brain/.google_token.json\n"
        "  Token expire/refresh 失敗の可能性、bot-account@example.co.jp で再 authorize 推奨"
    ),
    "component_streak_cohere_rerank": (
        "🛠️ Cohere Rerank API 失敗。API key / quota / network 確認:\n"
        "  curl -X POST https://api.cohere.com/v2/rerank -H 'Authorization: Bearer $COHERE_API_KEY' \\\n"
        "    -H 'Content-Type: application/json' -d '{\"model\":\"rerank-v3.5\",\"query\":\"test\",\"documents\":[\"a\"]}'\n"
        "  失敗時: dashboard.cohere.com で key revoke + 再発行"
    ),
}


# ─── auto-remediation helpers (★2026-05-25) ──────────────────
def _count_recent_restarts(within_min: int = RESTART_RATE_WINDOW_MIN) -> int:
    """ALERT_LOG から type=auto_restart の直近件数 count (= rate limit 用)。"""
    if not ALERT_LOG.exists():
        return 0
    cutoff = datetime.now(JST) - timedelta(minutes=within_min)
    n = 0
    try:
        for line in reversed(ALERT_LOG.read_text(encoding="utf-8").splitlines()[-300:]):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "auto_restart":
                continue
            ts_str = r.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts > cutoff:
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    return n


def attempt_auto_restart(reason: str) -> dict:
    """`docker compose restart line-bot` を試行 → 30s 待ち → health 再 check.

    Returns:
        {
          "attempted": bool,  # restart 試行したか (= rate limit / disabled 時 False)
          "ok": bool,         # restart コマンド成功
          "recovered": bool,  # restart 後 /health OK
          "detail": str,
        }
    """
    if not AUTO_REMEDIATE_ENABLED:
        return {
            "attempted": False, "ok": False, "recovered": False,
            "detail": "AUTO_REMEDIATE_ENABLED=0 → skip",
        }

    n_recent = _count_recent_restarts()
    if n_recent >= RESTART_RATE_LIMIT:
        return {
            "attempted": False, "ok": False, "recovered": False,
            "detail": f"rate limit: {n_recent} restarts in last {RESTART_RATE_WINDOW_MIN} min (>= {RESTART_RATE_LIMIT})",
        }

    logger.info(f"attempting auto-restart for: {reason} (cwd={REPO_ROOT_HOST})")
    try:
        result = subprocess.run(
            ["docker", "compose", "restart", "line-bot"],
            cwd=str(REPO_ROOT_HOST), capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:300]
            _log_alert("auto_restart", "warning",
                       f"reason={reason} restart_failed",
                       {"reason": reason, "rc": result.returncode, "stderr": err})
            return {
                "attempted": True, "ok": False, "recovered": False,
                "detail": f"restart rc={result.returncode}: {err}",
            }
    except FileNotFoundError:
        _log_alert("auto_restart", "warning", f"reason={reason} docker not found",
                   {"reason": reason})
        return {
            "attempted": True, "ok": False, "recovered": False,
            "detail": "docker not found in PATH (= cron_env.sh source?)",
        }
    except subprocess.TimeoutExpired:
        _log_alert("auto_restart", "warning", f"reason={reason} timeout",
                   {"reason": reason})
        return {
            "attempted": True, "ok": False, "recovered": False,
            "detail": "restart timeout (60s)",
        }
    except Exception as e:
        _log_alert("auto_restart", "warning", f"reason={reason} exception",
                   {"reason": reason, "error": f"{type(e).__name__}: {e}"})
        return {
            "attempted": True, "ok": False, "recovered": False,
            "detail": f"exception: {type(e).__name__}: {e}",
        }

    # restart 成功 → health 再 check 待ち
    logger.info(f"restart issued, waiting {RESTART_WAIT_SEC}s for /health...")
    time.sleep(RESTART_WAIT_SEC)
    health = check_health()
    recovered = bool(health.get("ok"))
    _log_alert(
        "auto_restart", "info" if recovered else "warning",
        f"reason={reason} recovered={recovered}",
        {"reason": reason, "recovered": recovered, "health_after": health},
    )
    return {
        "attempted": True, "ok": True, "recovered": recovered,
        "detail": f"restart ok, health {'OK' if recovered else 'still NG'} after {RESTART_WAIT_SEC}s",
    }


# ─── alert dispatcher ─────────────────────────────────────────
def evaluate_and_alert(silence_min: int = 0) -> int:
    """全 check 実行 + 異常時に LINE Push。返り値は exit code (0=healthy, 1=warning, 2=critical)。"""
    severity = 0  # 0=ok, 1=warning, 2=critical
    alerts_sent = 0

    # 1. /health (★2026-05-25: auto-restart 試行 → 復旧確認 → 文言切替)
    health = check_health()
    if not health.get("ok"):
        if not _alert_seen_recently("bot_dead", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
            restart = attempt_auto_restart(reason="bot_dead")
            if restart.get("recovered"):
                line_push(critical=True, text=
                    f"✅ [Personal Brain] bot 自動復旧\n"
                    f"/health NG 検知 → `docker compose restart line-bot` → OK\n"
                    f"原因調査推奨: `docker logs line-bot --tail 100 | grep -E ERROR`"
                )
                _log_alert("bot_dead", "warning", "auto-recovered",
                           {"health_initial": health, "restart": restart})
            else:
                attempted_label = "auto-restart 試行" if restart.get("attempted") else "auto-restart skip"
                line_push(critical=True, text=
                    f"🚨 [Personal Brain] bot プロセス無応答\n"
                    f"/health 接続失敗。\n"
                    f"detail: {health.get('error', '')[:100]}\n"
                    f"{attempted_label}: {restart.get('detail', '?')[:200]}\n\n"
                    f"{REMEDIATION_HINTS['bot_dead']}"
                )
                _log_alert("bot_dead", "critical", "/health failed (auto-restart 失敗)",
                           {"detail": health.get("error", ""), "restart": restart})
            alerts_sent += 1
        severity = max(severity, 2)
    logger.info(f"health: {health}")

    # 1b. /ready (★2026-06-08 評価 SRE: readiness を監視に接続)。/health OK の時だけ見る
    #     (= 「bot は生存だが依存が壊れてる = 緑なのに使えない」を検知)。restart は自動でしない
    #     — 依存 (redis/chromadb/litellm) 側の問題で bot を不要 restart しないため alert のみ。
    if health.get("ok"):
        ready = check_readiness()
        if not ready.get("ok"):
            if not _alert_seen_recently(
                "readiness_degraded", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN
            ):
                failed = ready.get("failed_deps") or "不明"
                line_push(critical=True, text=
                    f"⚠️ [Personal Brain] readiness NG (bot 生存だが依存が異常)\n"
                    f"/ready HTTP {ready.get('status_code')}、異常依存: {failed}\n"
                    f"= 「/health は緑だが retrieval/LLM が壊れてる」状態。restart は自動でしない。\n"
                    f"確認: redis / chromadb / litellm の状態 + docker logs line-bot"
                )
                _log_alert("readiness_degraded", "warning",
                           f"/ready not-ready: {failed}", {"ready": ready})
                alerts_sent += 1
            severity = max(severity, 1)
        logger.info(f"readiness: {ready}")

    # 2. turn silence (= webhook 受信停止)
    # ★2026-05-25 海山指示: alert 出すだけでなく auto-remediation 試行。
    # webhook_silent は health OK でも receiver state 詰まり (= docker restart で解消可能性)。
    # CF tunnel 死亡なら restart では治らないが、低コスト試行 → 結果に応じて hint。
    turns = check_recent_turns(silent_min=TURN_SILENT_THRESHOLD_MIN)
    if turns.get("is_silent"):
        # ★2026-06-10: is_silent は「webhook 到着あり (webhooks_in>0) かつ turn 0」= 真の receiver 詰まり
        #   のみ True (check_recent_turns で判定済)。到着ゼロの無 traffic は is_silent=False なので
        #   ここには来ない = 閑散時の不要 restart を根絶 (6/1-6/10 に毎日 22-41 回の flapping の根治)。
        # ★2026-06-07 評価: quiet 帯 skip は二重防御として残置 (到着あり×深夜は稀だが restart せず alert のみ)。
        _wh_in = turns.get("webhooks_in", 0)
        jst_hour = datetime.now(JST).hour
        _quiet_skip = bool(health.get("ok")) and _in_quiet_hours(jst_hour)
        # ★2026-06-30 fix: webhook_received は lineworks(スタンプ等 ACK 専用含む) + voice_alignment の
        #   両方が出すため、clone turn を生まない webhook 到着 (スタンプ/voice/Vapi keepalive) で is_silent が
        #   誤発火し、健全 bot を毎時 restart していた (6/10 修正後も 6/29-6/30 継続、in-flight 応答が落ちる
        #   =「回答が途切れる」の一因)。/health も /ready も緑なら receiver は機能している → restart せず
        #   alert のみ。真の receiver 詰まり (redis/chromadb/litellm 停止) は /ready が落ちるので従来どおり restart。
        _ready_ok = bool(health.get("ok")) and bool(ready.get("ok"))
        if _quiet_skip:
            logger.info(f"webhook_silent (webhook到着{_wh_in}件・turn0) だが quiet hours ({jst_hour}時) → restart skip、alert のみ")
        if not _quiet_skip and not _alert_seen_recently("webhook_silent", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
            if _ready_ok:
                # /health+/ready 緑 = receiver 機能中。非 clone webhook (スタンプ/voice/keepalive) を
                # silence と誤認した可能性が高い → restart せず alert のみ (健全 bot を churn しない)。
                logger.info(f"webhook_silent (到着{_wh_in}件・turn0) だが /health+/ready 緑 → restart 見送り")
                line_push(
                    f"ℹ️ [Personal Brain] 直近 {TURN_SILENT_THRESHOLD_MIN} 分 webhook {_wh_in} 件・clone turn 0\n"
                    f"ただし /health・/ready 共に緑 = bot は機能中 (スタンプ/voice 等の非 clone webhook か低 traffic)。\n"
                    f"restart は見送り、監視継続。続く様なら CF tunnel / 受信経路を確認。"
                )
                _log_alert("webhook_silent", "info", "ready緑のため restart 見送り (健全)", {**turns})
            else:
                restart = attempt_auto_restart(reason="webhook_silent")
                if restart.get("recovered"):
                    line_push(critical=True, text=
                        f"✅ [Personal Brain] bot 自動再起動\n"
                        f"直近 {TURN_SILENT_THRESHOLD_MIN} 分で webhook 到着 {_wh_in} 件あるのに turn 0 + /ready NG = receiver 詰まり → docker restart → /health OK\n"
                        f"次回 webhook で受信再開 期待。1-2 turn 様子見、まだ silent なら CF tunnel 疑い:\n"
                        f"  `brew services restart cloudflared` (or 同等)\n"
                        f"  https://one.dash.cloudflare.com/?to=/:account/networks/tunnels"
                    )
                    _log_alert("webhook_silent", "warning", "auto-restart 試行 + 自動復旧",
                               {**turns, "restart": restart})
                else:
                    attempted_label = "auto-restart 試行" if restart.get("attempted") else "auto-restart skip"
                    line_push(critical=True, text=
                        f"⚠️ [Personal Brain] 直近 {TURN_SILENT_THRESHOLD_MIN} 分 webhook 到着 {_wh_in} 件あるのに turn 0 件 + /ready NG\n"
                        f"LINE webhook 受信停止 or bot silent skip 疑い。\n"
                        f"{attempted_label}: {restart.get('detail', '?')[:200]}\n\n"
                        f"{REMEDIATION_HINTS['webhook_silent']}"
                    )
                    _log_alert("webhook_silent", "warning",
                               f"{TURN_SILENT_THRESHOLD_MIN}min turns=0 (auto-restart 失敗 or rate-limited)",
                               {**turns, "restart": restart})
            alerts_sent += 1
        if not _quiet_skip:
            severity = max(severity, 1)
    logger.info(f"turns: {turns}")

    # 3. failure burst
    burst = check_failure_burst()
    if burst.get("is_burst"):
        if not _alert_seen_recently("failure_burst", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
            samples = burst.get("samples", [])
            sample_str = "\n".join(
                f"  {s.get('ts','')[:19]}: {s.get('comp','')} / {s.get('err','')[:60]}"
                for s in samples[:3]
            )
            line_push(
                f"⚠️ [Personal Brain] 1h で turn_failed {burst['n_failed']} 件\n"
                f"閾値 {TURN_FAILED_THRESHOLD} 超、調査推奨。\n"
                f"recent samples:\n{sample_str}\n\n"
                f"{REMEDIATION_HINTS['failure_burst']}"
            )
            _log_alert("failure_burst", "warning",
                       f"1h failed={burst['n_failed']}", burst)
            alerts_sent += 1
        severity = max(severity, 1)
    logger.info(f"burst: {burst}")

    # 3b. admin 認証失敗 burst (★2026-06-08 評価 Security: brute-force/scan 検知)
    authb = check_auth_denied_burst()
    if authb.get("is_burst"):
        if not _alert_seen_recently("auth_denied_burst", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
            line_push(critical=True, text=
                f"🚨 [Personal Brain] admin 認証失敗 {authb['n_denied']} 件/1h "
                f"(distinct token {authb.get('distinct_tokens')}、対象 {authb.get('actions')})\n"
                f"= 特権 token への brute-force / scan の疑い。\n"
                f"対処: Cloudflare 側で該当 path をレート制限 / DEPLOY_ADMIN_TOKEN・閲覧 token のローテ検討。"
            )
            _log_alert("auth_denied_burst", "warning",
                       f"1h auth_denied={authb['n_denied']}", authb)
            alerts_sent += 1
        severity = max(severity, 1)
    logger.info(f"auth_denied: {authb}")

    # 4. context_prefix_leak (= critical、即対応要)
    leaks = check_context_leak()
    if leaks.get("has_critical"):
        if not _alert_seen_recently("context_leak", 0):  # critical は cooldown 短く
            line_push(critical=True, text=
                f"🚨 [Personal Brain] context_prefix_leak 発生 {leaks['n_leaks']} 件\n"
                f"fix #1 strip_context_prefix() 漏れ、即調査。\n\n"
                f"{REMEDIATION_HINTS['context_leak']}"
            )
            _log_alert("context_leak", "critical", f"leaks={leaks['n_leaks']}", leaks)
            alerts_sent += 1
        severity = max(severity, 2)
    logger.info(f"context_leak: {leaks}")

    # 5. deploy freshness (★2026-05-24 海山指示「常時監視」核心)
    deploy = check_deploy_freshness()
    if deploy.get("is_stale"):
        if not _alert_seen_recently("deploy_stale", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
            uptime_h = deploy.get("uptime_hours", 0)
            bf = deploy.get("build_failures_24h", 0)
            behind = deploy.get("commits_behind", 0)
            line_push(
                f"⚠️ [Personal Brain] auto_deploy stale 疑い\n"
                f"container uptime: {uptime_h:.1f}h\n"
                f"未 deploy commit: {behind} 件\n"
                f"build failures (24h): {bf}\n"
                f"git HEAD: {deploy.get('git_head', '')[:8]}\n\n"
                f"{REMEDIATION_HINTS['deploy_stale']}"
            )
            _log_alert("deploy_stale", "warning",
                       f"uptime={uptime_h:.1f}h failures={bf}", deploy)
            alerts_sent += 1
        severity = max(severity, 1)
    logger.info(f"deploy: {deploy}")

    # 7. component-level streak (★2026-05-24 Tier 1)
    # 個別 component 単独で 3+ 連続失敗で alert (= 全体 burst より早期)
    comp_streak = check_component_streak()
    if comp_streak.get("has_any_burst"):
        for comp in comp_streak.get("burst_components", []):
            alert_type = f"component_streak_{comp}"
            if _alert_seen_recently(alert_type, silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
                continue
            info = comp_streak["per_component"][comp]
            label = next((l for c, l in CRITICAL_COMPONENTS if c == comp), comp)
            samples = info.get("samples", [])
            sample_str = "\n".join(
                f"  {s.get('ts','')[:19]}: {s.get('err','')[:80]}"
                for s in samples[-3:]
            )
            hint = REMEDIATION_HINTS.get(alert_type, f"🛠️ {comp} の log 確認:\n  docker logs line-bot --tail 100 | grep {comp}")
            line_push(
                f"⚠️ [Personal Brain] {label} 連続失敗 {info['n_failed']} 件\n"
                f"component={comp}、過去 60 分。\n"
                f"recent samples:\n{sample_str}\n\n"
                f"{hint}"
            )
            _log_alert(alert_type, "warning",
                       f"{comp} streak={info['n_failed']}", info)
            alerts_sent += 1
        severity = max(severity, 1)
    logger.info(f"component_streak: {comp_streak}")

    # 6. silent skip burst (★2026-05-24 海山指示「検知 + 修正まで」)
    silent = check_silent_skip()
    if silent.get("is_silent_skip_burst"):
        if not _alert_seen_recently("silent_skip_burst", silence_min if silence_min > 0 else ALERT_COOLDOWN_MIN):
            line_push(
                f"⚠️ [Personal Brain] silent skip 連続 {silent['n_silent_skips']} 件\n"
                f"webhook 受信あるが text 応答経路未到達。\n\n"
                f"{REMEDIATION_HINTS['silent_skip_burst']}"
            )
            _log_alert("silent_skip_burst", "warning",
                       f"silent={silent['n_silent_skips']}", silent)
            alerts_sent += 1
        severity = max(severity, 1)
    logger.info(f"silent_skip: {silent}")

    summary = {
        "ts": datetime.now(JST).isoformat(),
        "severity": severity,
        "alerts_sent": alerts_sent,
        "checks": {"health": health, "turns": turns, "burst": burst,
                   "context_leak": leaks, "deploy": deploy,
                   "component_streak": comp_streak, "silent_skip": silent},
    }
    logger.info(f"bot_uptime_monitor summary: severity={severity}, alerts_sent={alerts_sent}")
    print(json.dumps(summary, ensure_ascii=False))
    return severity


def main():
    parser = argparse.ArgumentParser(description="bot 常時稼働監視 (★2026-05-24 海山指示)")
    parser.add_argument("--silence-min", type=int, default=ALERT_COOLDOWN_MIN,
                        help="同 alert の cooldown (分、default 30、0 で cooldown 無視)")
    parser.add_argument("--check-only", action="store_true",
                        help="check のみ、LINE Push しない (debug 用)")
    args = parser.parse_args()

    if args.check_only:
        # check のみ実行、alert は dry-run
        health = check_health()
        turns = check_recent_turns()
        burst = check_failure_burst()
        leaks = check_context_leak()
        deploy = check_deploy_freshness()
        comp_streak = check_component_streak()
        silent = check_silent_skip()
        print(json.dumps({
            "ts": datetime.now(JST).isoformat(),
            "check_only": True,
            "health": health, "turns": turns, "burst": burst,
            "context_leak": leaks, "deploy": deploy,
            "component_streak": comp_streak, "silent_skip": silent,
        }, ensure_ascii=False, indent=2))
        return

    sev = evaluate_and_alert(silence_min=args.silence_min)
    sys.exit(sev)


if __name__ == "__main__":
    main()
