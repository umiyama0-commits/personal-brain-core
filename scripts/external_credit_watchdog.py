"""
external_credit_watchdog.py — 外部 service 残高切れ silent fail を防ぐ daily 監視
                              (★2026-05-23 海山指示、LEE §5.7 circuit breaker の billing 側)

今日 1 日で 2 件発生:
  1. 5/22 OpenAI quota 枯渇 → bot 応答全 fail 9h 気付かれず
  2. 5/21-23 Vapi クレジット切れ → Assistant 起動失敗、transcript 0 件

→ 残高側を毎日 ping して **閾値以下で LINE Push** することで、24h 前に気付ける layer。

監視対象 (= 設定済なら ping、未設定なら silent skip):
  - Vapi (= VAPI_PRIVATE_API_KEY、通話0件を credit-out proxy に)
  - LiteLLM (= proxy 経由の今日使用率、max_budget の % 計算。OpenAI/Anthropic は全て litellm 経由
    なのでこの予算% が両社の aggregate spend proxy になる)
  - ElevenLabs (= ELEVENLABS_API_KEY、残文字数 character_limit-character_count) ★2026-06-08
  - HeyGen (= HEYGEN_API_KEY、残 quota /v2/user/remaining_quota) ★2026-06-08

★2026-06-08 海山指示「各種 API の残高枯渇を自動連絡」+ fact-check (公式 docs):
  残高/残量を直接 ping できる API があるのは ElevenLabs と HeyGen のみ。
  - OpenAI / Anthropic: 残高 endpoint 無し (Admin Key で消費額のみ) → LiteLLM 予算% で aggregate 監視。
  - Gemini / Cohere: 残高/集計 API 無し → 枯渇は runtime の error code 検知が現実的 (別途 TODO)。

cron: 09:00 + 21:00 daily で 2 回 (= 朝夕、平日業務時間内)

実行:
  python3 scripts/external_credit_watchdog.py             # 通常 check (= NG なら LINE Push)
  python3 scripts/external_credit_watchdog.py --dry-run   # Push せず stdout のみ
  python3 scripts/external_credit_watchdog.py --verbose   # 全 OK でも Push (= 動作確認用)

閾値 (= env で override 可):
  VAPI_CALLS_WINDOW_H=48       (Vapi: この時間内に通話0件で警告)
  LITELLM_HIGH_USAGE_PCT=80    (今日使用 / max_budget が 80% 超で警告)
  ELEVENLABS_LOW_CHARS=5000    (ElevenLabs: 残文字数がこれ未満で警告)
  HEYGEN_LOW_QUOTA=60          (HeyGen: 残 quota がこれ未満で警告。単位は初回 --verbose の raw で要確認)

★2026-06-07 エージェント評価で修正: Vapi 残高 REST は非公開 (旧コードは実在しない endpoint を叩き
  silent)、LiteLLM は documented な /global/spend/report に統一。詳細は各 check 関数の comment。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("external_credit_watchdog")

JST = timezone(timedelta(hours=9))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import line_push  # noqa: E402

# env
VAPI_API_KEY = os.getenv("VAPI_PRIVATE_API_KEY", "")
VAPI_API_BASE = os.getenv("VAPI_API_BASE", "https://api.vapi.ai")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")
LITELLM_MAX_BUDGET = float(os.getenv("LITELLM_MAX_BUDGET", "50"))  # USD/day

# 閾値
VAPI_LOW_BALANCE_USD = float(os.getenv("VAPI_LOW_BALANCE_USD", "20"))
LITELLM_HIGH_USAGE_PCT = float(os.getenv("LITELLM_HIGH_USAGE_PCT", "80"))

# ★2026-06-08 海山指示「各種 API の残高枯渇を自動連絡」+ fact-check (公式 docs 検証):
# 残高/残量を直接 ping できるのは ElevenLabs と HeyGen の 2 つのみ (OpenAI/Anthropic は残高 API 無し
# = LiteLLM 予算% で aggregate 監視、Gemini/Cohere は残高 API 無し = 枯渇時 error 検知が現実的)。
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_API_BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
ELEVENLABS_LOW_CHARS = int(os.getenv("ELEVENLABS_LOW_CHARS", "5000"))  # 残文字数 がこれ未満で警報
HEYGEN_API_KEY = os.getenv("HEYGEN_API_KEY", "")
HEYGEN_API_BASE = os.getenv("HEYGEN_API_BASE", "https://api.heygen.com")
HEYGEN_LOW_QUOTA = float(os.getenv("HEYGEN_LOW_QUOTA", "60"))  # 残 quota 閾値 (単位は初回 raw で要確認)


# ★2026-06-07 エージェント評価: Vapi は残高取得 REST を公開していない (公式 docs 2026-06 WebSearch 確認)。
# 旧実装は実在しない /customer/me 等を叩き、取れないと ok:True で silent → Vapi 残高がいくら減っても
# 永遠に無警報 = watchdog の主目的 (5/21-23 credit-out 再発防止) が未達だった。
# 代わりに実在する GET /call で直近 N 時間の通話数を取り、0 件を credit-out/起動失敗の proxy 指標にする。
VAPI_CALLS_WINDOW_H = int(os.getenv("VAPI_CALLS_WINDOW_H", "48"))


RECALL_API_BASE = os.getenv("RECALL_API_BASE", "https://ap-northeast-1.recall.ai/api/v1")
RECALL_API_KEY = os.getenv("RECALL_API_KEY", "")


def check_recall() -> dict:
    """Recall.ai の bot 作成可否を **実際に作れるか** で見る (★2026-08-06)。

    事故: 2026-08-04 22:00 から POST /bot/ が 402 Payment Required を返し続け、
    web 会議の自動議事録が **5 日間・6 会議分** 止まっていた。本 watchdog の監視対象は
    vapi/litellm/elevenlabs/heygen だけで **Recall が入っておらず**、
    meeting_autojoin 側の loud_fail が 111 連続失敗まで積み上がって初めて表に出た。

    残高 API は公開されていないので、「クレジット切れなら 402 を返す」性質を使う。
    GET /bot/ は残高ゼロでも 200 を返すため死活監視にならない — 判定は POST の
    ステータスで行い、**実際には作らない** (join_at を過去にして即失敗させる等はせず、
    直近 cycle の meeting_autojoin ログに残る 402 を読む方式にする = 課金も副作用も無い)。
    """
    # ★2026-08-09 海山判断: Recall をやめ Plaud Desktop に一本化 → 使っていないものを
    # 監視して鳴り続けないよう、自動参加が無効なら skip する (再有効化すれば自動で復帰)。
    if os.getenv("MEETING_AUTOJOIN_ENABLED", "0") != "1":
        return {"service": "recall", "ok": True,
                "skipped": "MEETING_AUTOJOIN_ENABLED != 1 (Recall 不使用)"}
    if not RECALL_API_KEY:
        return {"service": "recall", "ok": True, "skipped": "RECALL_API_KEY 未設定"}
    log = Path(os.getenv("BRAIN_APP_ROOT", "/app")) / "data" / "brain" / "meeting_autojoin.log"
    if not log.exists():
        return {"service": "recall", "ok": True, "degraded": "meeting_autojoin.log 不在"}
    try:
        tail = log.read_text(encoding="utf-8", errors="ignore")[-200_000:]
    except Exception as e:
        return {"service": "recall", "ok": True, "degraded": f"{type(e).__name__}: {e}"}
    n402 = tail.count("402 Payment Required")
    # 直近の 402 がいつか (ログ行頭の timestamp)
    last = ""
    for line in reversed(tail.splitlines()):
        if "402 Payment Required" in line:
            last = line[:19]
            break
    return {
        "service": "recall", "ok": n402 == 0, "n_402": n402, "last_402": last,
        "note": ("bot 予約が 402 (クレジット切れ) で失敗している = 会議の自動議事録が止まる"
                 if n402 else ""),
    }


def check_vapi() -> dict:
    """Vapi の直近通話数を GET /call で取得し、0 件なら credit-out / assistant 起動失敗 の疑いとして警報。"""
    if not VAPI_API_KEY:
        return {"service": "vapi", "ok": True, "skipped": "VAPI_PRIVATE_API_KEY 未設定"}
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{VAPI_API_BASE}/call",
                           headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
                           params={"limit": 100})
    except Exception as e:
        return {"service": "vapi", "ok": True, "degraded": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"service": "vapi", "ok": True, "degraded": f"GET /call HTTP {r.status_code}", "error": r.text[:200]}
    try:
        calls = r.json()
    except Exception as e:
        return {"service": "vapi", "ok": True, "degraded": f"json: {e}"}
    if not isinstance(calls, list):
        return {"service": "vapi", "ok": True, "degraded": "unexpected response (not list)"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=VAPI_CALLS_WINDOW_H)
    recent = 0
    for c in calls:
        ts = (c.get("createdAt") or "") if isinstance(c, dict) else ""
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff:
                recent += 1
        except Exception:
            continue
    ok = recent > 0
    return {
        "service": "vapi", "ok": ok, "n_calls_window": recent, "window_h": VAPI_CALLS_WINDOW_H,
        "note": ("通話0件 = credit-out/assistant起動失敗の疑い、要 Vapi dashboard 確認" if not ok else ""),
    }


def _sum_litellm_spend(data) -> float:
    """spend report の spend を defensive に合算 (shape 不定のため list/dict 両対応、二重計上しない)。"""
    if isinstance(data, list):
        return sum(_sum_litellm_spend(x) for x in data)
    if isinstance(data, dict):
        for k in ("total_spend", "spend"):
            v = data.get(k)
            if isinstance(v, (int, float)):
                return float(v)
    return 0.0


def check_litellm() -> dict:
    """LiteLLM の今日 spend を documented な /global/spend/report で取得し max_budget の % を計算。

    ★2026-06-07 評価: 旧 /spend は非標準 endpoint、/spend/logs?limit=1000 は高 volume 日に当日 log が
    1000 件超で取りこぼし → used 過少 → 閾値到達してても見逃し。/global/spend/report?start&end (UTC、
    公式 docs 確認) に統一。
    """
    if not LITELLM_KEY:
        return {"service": "litellm", "ok": True, "skipped": "LITELLM_MASTER_KEY 未設定"}
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(
                f"{LITELLM_URL}/global/spend/report",
                headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                params={"start_date": today_utc, "end_date": today_utc},
            )
    except Exception as e:
        return {"service": "litellm", "ok": True, "degraded": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"service": "litellm", "ok": True,
                "degraded": f"/global/spend/report HTTP {r.status_code}", "error": r.text[:200]}
    try:
        used_usd = _sum_litellm_spend(r.json())
    except Exception as e:
        return {"service": "litellm", "ok": True, "degraded": f"parse: {e}"}

    pct = (used_usd / LITELLM_MAX_BUDGET * 100) if LITELLM_MAX_BUDGET > 0 else 0
    return {
        "service": "litellm",
        "ok": pct < LITELLM_HIGH_USAGE_PCT,
        "used_usd": round(used_usd, 2),
        "budget_usd": LITELLM_MAX_BUDGET,
        "usage_pct": round(pct, 1),
        "threshold_pct": LITELLM_HIGH_USAGE_PCT,
    }


def check_elevenlabs() -> dict:
    """ElevenLabs の残文字数 (character_limit - character_count) を閾値以下で警報。

    ★2026-06-08 fact-check (公式 docs 確認): GET /v1/user/subscription, header `xi-api-key`,
    fields character_count(使用済)/character_limit(上限)。残量 = limit - count。
    """
    if not ELEVENLABS_API_KEY:
        return {"service": "elevenlabs", "ok": True, "skipped": "ELEVENLABS_API_KEY 未設定"}
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{ELEVENLABS_API_BASE}/v1/user/subscription",
                           headers={"xi-api-key": ELEVENLABS_API_KEY})
    except Exception as e:
        return {"service": "elevenlabs", "ok": True, "degraded": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"service": "elevenlabs", "ok": True,
                "degraded": f"HTTP {r.status_code}", "error": r.text[:200]}
    try:
        d = r.json()
        used = int(d.get("character_count", 0))
        limit = int(d.get("character_limit", 0))
    except Exception as e:
        return {"service": "elevenlabs", "ok": True, "degraded": f"parse: {e}"}
    remaining = limit - used
    return {
        "service": "elevenlabs", "ok": remaining >= ELEVENLABS_LOW_CHARS,
        "remaining_chars": remaining, "used": used, "limit": limit,
        "threshold_chars": ELEVENLABS_LOW_CHARS,
    }


def _extract_heygen_quota(d):
    """HeyGen response から残 quota を defensive に抽出 (= field 名/階層が docs 未記載のため)。"""
    if not isinstance(d, dict):
        return None
    for path in (("data", "remaining_quota"), ("remaining_quota",),
                 ("data", "quota"), ("quota",), ("data", "remaining")):
        cur = d
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, (int, float)):
            return float(cur)
    return None


def check_heygen() -> dict:
    """HeyGen の残 quota を GET /v2/user/remaining_quota で取得し閾値以下で警報。

    ★2026-06-08 fact-check: path (/v2/user/remaining_quota) と auth (X-Api-Key) は公式で確定。
    ただし response の field 名/単位は docs 未記載 → defensive 抽出。取れなければ false alarm を
    避けて degraded で raw を残し、初回 --verbose で実 shape を確認 → HEYGEN_LOW_QUOTA を調整する。
    """
    if not HEYGEN_API_KEY:
        return {"service": "heygen", "ok": True, "skipped": "HEYGEN_API_KEY 未設定"}
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{HEYGEN_API_BASE}/v2/user/remaining_quota",
                           headers={"X-Api-Key": HEYGEN_API_KEY})
    except Exception as e:
        return {"service": "heygen", "ok": True, "degraded": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"service": "heygen", "ok": True,
                "degraded": f"HTTP {r.status_code}", "error": r.text[:200]}
    try:
        d = r.json()
    except Exception as e:
        return {"service": "heygen", "ok": True, "degraded": f"json: {e}"}
    q = _extract_heygen_quota(d)
    if q is None:
        return {"service": "heygen", "ok": True,
                "degraded": "remaining_quota field 不明 (要実機確認)",
                "raw": json.dumps(d, ensure_ascii=False)[:300]}
    return {
        "service": "heygen", "ok": q >= HEYGEN_LOW_QUOTA,
        "remaining_quota": q, "threshold": HEYGEN_LOW_QUOTA,
        "note": "単位 (生quota/USD/分) は初回 raw で要確認",
    }


# 監視対象 (= check 関数のリスト。設定済なら ping、未設定なら skipped)
CHECKS = [check_vapi, check_litellm, check_elevenlabs, check_heygen, check_recall]


def _alert_line(r: dict) -> list:
    """1 service の警告メッセージ行 (service 別に hint を付ける)。"""
    s = r.get("service")
    if s == "vapi":
        return [f"  • Vapi: 直近 {r.get('window_h', '?')}h の通話 {r.get('n_calls_window', 0)} 件"
                f" — {r.get('note') or 'credit-out/assistant起動失敗の疑い'}",
                "    → Vapi dashboard 確認: https://dashboard.vapi.ai"]
    if s == "litellm":
        return [f"  • LiteLLM: 今日 ${r.get('used_usd', 0)} / 予算 ${r.get('budget_usd')} "
                f"(= {r.get('usage_pct', 0)}% 使用、閾値 {r.get('threshold_pct')}%)",
                "    → cap 到達で 503→bot fallback。OpenAI/Anthropic 残高は LiteLLM 経由なので"
                " この予算% が aggregate proxy"]
    if s == "elevenlabs":
        return [f"  • ElevenLabs: 残 {r.get('remaining_chars', 0):,} 文字 "
                f"(使用 {r.get('used', 0):,}/{r.get('limit', 0):,}、閾値 {r.get('threshold_chars'):,})",
                "    → 音声生成が枯渇間近。ElevenLabs dashboard で追加: https://elevenlabs.io/app"]
    if s == "recall":
        return [f"  • Recall.ai: bot 予約が 402 (クレジット切れ) — 直近ログに {r.get('n_402', 0)} 回"
                f" (最新 {r.get('last_402') or '?'})",
                "    → **web 会議の自動議事録が止まっています**。Recall dashboard で"
                " クレジット追加: https://www.recall.ai/dashboard"]
    if s == "heygen":
        return [f"  • HeyGen: 残 quota {r.get('remaining_quota', '?')} (閾値 {r.get('threshold')}、"
                f"{r.get('note', '')})",
                "    → 動画生成が枯渇間近。HeyGen dashboard 確認: https://app.heygen.com"]
    return [f"  • {s}: {r}"]


def run_check(dry_run: bool = False, verbose: bool = False) -> dict:
    """全 service check + 閾値超で LINE Push。"""
    results = {"ts": datetime.now(JST).isoformat(timespec="seconds")}
    for fn in CHECKS:
        r = fn()
        results[r["service"]] = r

    alerts = [(r["service"], r) for r in results.values()
              if isinstance(r, dict) and not r.get("skipped") and not r.get("ok", True)]

    print(json.dumps(results, ensure_ascii=False, indent=2))

    if alerts and not dry_run:
        lines = ["⚠️ 外部 API 残高警告 (= silent fail 前に気付くための watchdog)"]
        for _service, r in alerts:
            lines.extend(_alert_line(r))
        # 残高枯渇は bot silent fail の前兆 = 配達保証必須 → critical (LW fallback 可)
        line_push("\n".join(lines), critical=True)
        logger.warning(f"LINE Push sent ({len(alerts)} alerts)")
    elif verbose and not dry_run:
        lines = ["✓ 外部 API 残高 watchdog (= 全 service 閾値内 / skip 内訳)"]
        for r in results.values():
            if not isinstance(r, dict) or "service" not in r:
                continue
            s = r["service"]
            if r.get("skipped"):
                lines.append(f"  • {s}: skipped ({r['skipped']})")
            elif r.get("degraded"):
                extra = f" raw={r['raw']}" if r.get("raw") else ""
                lines.append(f"  • {s}: degraded ({r['degraded']}){extra}")
            else:
                lines.append("  • " + _alert_line(r)[0].strip().lstrip("• ").strip())
        line_push("\n".join(lines))
        logger.info("LINE Push sent (verbose ok)")

    return {"alerts": len(alerts), "details": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Push せず stdout のみ")
    parser.add_argument("--verbose", action="store_true", help="全 OK でも Push (= 動作確認用)")
    args = parser.parse_args()

    result = run_check(dry_run=args.dry_run, verbose=args.verbose)
    return 1 if result["alerts"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
