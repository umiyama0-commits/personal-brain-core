"""
clone_hallucination_check.py — うみやまAI 応答の post-hoc fact verifier

設計:
  bot 応答後に **別 LLM** が「bot が出した事実主張」を抽出 → 各 claim を wiki 等と
  照合 → 矛盾 or 根拠なしなら hallucination として flag。

  既存の style regression / auto-improve とは独立した「世界基準で hole」
  (= post-hoc fact verification) を埋めるためのレイヤー。

なぜ別 LLM か:
  - 同じ system (style judge と同じ LLM) が採点すると self-evaluation loop に陥り、
    偏った方向に自己強化される (Personal Brain の構造的弱点 #1)
  - judge を分散させるため smart-gpt (GPT-5.4) を採点側、応答側は smart (Claude Opus 4.8)

判定 3 値:
  - supported   : claim を支える明確な wiki 根拠あり
  - unsupported : 根拠が見つからない (= 推測 / 知識ベース外)
  - contradicted: wiki / raw に矛盾する記述がある (= 危険な hallucination)

判定対象から除外する claim:
  - 主観的意見 ("私はこう思う" 系)
  - 質問 ("どう思う?" 系)
  - 一般常識・社会通念 (wiki 外の世界知識)
  - 数字を含まないあいまい表現

cron: 毎日 03:45 JST (improve 03:00 / regression 03:30 / privacy-review 04:00 の間)

出力:
  data/brain/clone_improve/hallucination/YYYY-MM-DD.json    (詳細レポート)
  data/brain/clone_improve/hallucination.log.jsonl          (集計ログ)

LINE Push:
  hallucination 件数 (contradicted) >= 3 で通知

実行:
  python3 scripts/clone_hallucination_check.py                  # 直近 24h
  python3 scripts/clone_hallucination_check.py --hours 48      # 直近 48h
  python3 scripts/clone_hallucination_check.py --dry-run       # LLM 呼ぶが書き込み無し
  python3 scripts/clone_hallucination_check.py --sample 20     # 最大 20 ターン
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx  # ★2026-07-02 P1c: evidence を稼働 bot の /api/brain/search から取る

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (  # noqa: E402
    call_llm, line_push, append_jsonl, ensure_dirs,
    load_conversations, JST, IMPROVE_DIR, WIKI_DIR,
    pick_cross_family_judge,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_hallucination_check")

HALL_DIR = IMPROVE_DIR / "hallucination"
LOG_PATH = IMPROVE_DIR / "hallucination.log.jsonl"

# 採点側 = bot 側と異なる LLM を使う (self-evaluation loop 回避)。
# ★2026-07-05 監査 fix: "smart-gpt" ハードコードは本番 clone の smart-gpt 移行
# (CLONE_PUBLIC_PROD_MODEL) で同一系列 self-eval に転落していた → 単一判定点に追随。
VERIFIER_MODEL = os.getenv("HALLUCINATION_VERIFIER_MODEL") or pick_cross_family_judge()
# claim 抽出は速度優先 (内容シンプル)
EXTRACTOR_MODEL = os.getenv("HALLUCINATION_EXTRACTOR_MODEL", "fast-gpt")

# 1 turn あたりの claim 上限 (cost 制御)
MAX_CLAIMS_PER_TURN = int(os.getenv("HALLUCINATION_MAX_CLAIMS", "5"))
# 1 run でチェックする turn 上限
MAX_TURNS_PER_RUN = int(os.getenv("HALLUCINATION_MAX_TURNS", "30"))


# ─── claim 抽出 ──────────────────────────────────
CLAIM_EXTRACT_PROMPT = """あなたは fact-extraction エージェント。
以下の AI 応答から **検証可能な事実主張 (factual claim)** を atomic に抽出。

【AI 応答】
{response}

【判定対象とする claim】
- 数字・店名・人名・部署名・日付・場所・施設名 を含む具体的主張
- "○○エリアの売上は X 円" "△△店長は □□" 等
- "FY26 の AOP は X%" 等の業績数値

【判定対象から除外する claim】
- "私はこう思う" "個人的には〜" 系の主観
- 質問 ("どう思う?" 等)
- 一般常識 ("挨拶は大事" 等)
- 数字や固有名詞を含まないあいまい表現

【出力 (JSON only)】
```json
{{
  "claims": [
    "<atomic な事実主張 1 (80 字以内)>",
    "<atomic な事実主張 2>",
    ...
  ]
}}
```

最大 {max_claims} 件まで。0 件なら `"claims": []` を返す。"""


# ─── 検証 (LLM-as-judge) ──────────────────────────
VERIFY_PROMPT = """あなたは fact-verification エージェント。
以下の **claim** が、提示された **evidence** によって支持されるか判定。

【claim】
{claim}

【evidence (wiki retrieval 結果の連結抜粋。長い場合は途中で切れていることがある)】
{evidence}

【元の AI 応答 (context)】
{response}

【判定基準】
- supported   : claim を直接支持する記述が evidence にある (数字一致、固有名一致、明確に書いてある)
- unsupported : evidence に該当する記述が無い、または極めて間接的にしか言及されてない
- contradicted: evidence に **矛盾** する記述が明確にある (例: claim "X=100" / evidence "X=200")

【重要】
- 一般常識 (例: 「OWNDAYS はメガネ屋」) を持って判定しない、evidence のみで判定
- evidence が不十分な場合は unsupported (qualitatively grey は contradicted じゃなく unsupported)
- evidence は抜粋のため途中で切れていることがある。切れた先の内容を推測して supported/contradicted に
  しない (見えている範囲だけで判定、範囲内に無ければ unsupported)
- 数字の若干誤差 (5% 以内) は supported 扱い (LLM の丸めは許容)
- claim が複合的 (X=100 かつ Y=Z) の場合、片方が contradicted なら全体 contradicted

【出力 (JSON only)】
```json
{{
  "verdict": "supported" | "unsupported" | "contradicted",
  "reason": "<60 字以内、判定理由>",
  "evidence_snippet": "<60 字以内、判定根拠になった evidence の引用>"
}}
```"""


async def extract_claims(response: str, max_claims: int = MAX_CLAIMS_PER_TURN) -> list[str]:
    """bot 応答から atomic claim を抽出。"""
    if len(response) < 50:
        return []  # 短すぎる応答は対象外 (相槌 / 確認)
    prompt = CLAIM_EXTRACT_PROMPT.format(
        response=response[:3000],
        max_claims=max_claims,
    )
    try:
        out = await call_llm(prompt, model=EXTRACTOR_MODEL, max_tokens=800, temperature=0.0)
    except Exception as e:
        logger.warning(f"claim extract LLM failed: {e}")
        return []

    # JSON 抽出
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", out, re.DOTALL) or \
        re.search(r"\{[^{}]*\"claims\".*?\}", out, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(1) if m.group(0).startswith("```") else m.group(0))
    except Exception:
        return []

    claims = data.get("claims", [])
    if not isinstance(claims, list):
        return []
    # 各 claim を str に正規化 + 80 字 cap + 重複除去
    cleaned = []
    seen = set()
    for c in claims[:max_claims]:
        if not isinstance(c, str):
            continue
        c = c.strip()[:200]
        if not c or c in seen:
            continue
        seen.add(c)
        cleaned.append(c)
    return cleaned


async def gather_evidence(claim: str, brain_wiki=None) -> list[dict]:
    """claim に関連する wiki 内容を集める。

    ★2026-07-02 監査 P1c: host 実行では §1.5 (chromadb 並行アクセス禁止) のため line-bot が開いて
    いる chroma を直接開けず、BrainWiki(http=http) 直 init は index=None で毎晩 fallback していた
    (33日間 fact 検証ゼロ)。稼働中 bot の /api/brain/search (= brain_index.build_context) を HTTP で
    叩いて evidence を得る (§1.5 回避、単一 index の in-process 経路)。brain_wiki 引数は互換のため残置。
    """
    base = os.getenv("HALLUCINATION_BOT_URL", "http://localhost:8000")
    # require_api_key は key= を BRAIN_EXTENSION_KEY と、token= を VOICE_ALIGN_TOKEN と比較する
    # (reviewer 指摘: fallback を key= で送ると常に 401 = 死んだ fallback)。param 名を出し分ける。
    # ★2026-07-03 persona-v3: public=1 = 公開クローンと同じ epistemic 境界で検証
    # (private 知識で公開応答を誤 contradicted にしない + 深層人格を evidence に流さない)
    params = {"q": claim, "public": "1"}
    if os.getenv("BRAIN_EXTENSION_KEY"):
        params["key"] = os.getenv("BRAIN_EXTENSION_KEY")
    elif os.getenv("VOICE_ALIGN_TOKEN"):
        params["token"] = os.getenv("VOICE_ALIGN_TOKEN")
    else:
        logger.warning("BRAIN_EXTENSION_KEY/VOICE_ALIGN_TOKEN 未設定 → evidence 取得不可 (degraded)")
        return []
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(f"{base}/api/brain/search", params=params)
        if r.status_code != 200:
            logger.warning(f"/api/brain/search 非200: {r.status_code} {r.text[:120]}")
            return []
        results = r.json().get("results", "")
        if not results or results == "該当なし":
            return []
        # build_context は 1 本の text を返す → verify_claim が使える dict list に包む
        return [{"text": results, "source": "api/brain/search"}]
    except Exception as e:
        logger.warning(f"evidence search (HTTP) failed: {e}")
        return []


async def verify_claim(claim: str, evidence: list[dict], response: str) -> dict:
    """1 claim に対して supported / unsupported / contradicted 判定。"""
    # evidence をテキスト化
    if not evidence:
        ev_text = "(evidence 無し)"
    else:
        parts = []
        for i, h in enumerate(evidence[:5]):
            src = h.get("source") or h.get("file") or "?"
            # ★2026-07-02 P1c: gather_evidence は "text" で返す (旧 chroma 経路は "content")。両対応。
            # ★2026-07-05 監査 fix: 現行経路 (P1c の /api/brain/search) は常に 1 item (連結 text)
            # のため、per-item [:1200] が全体 cap [:3000] を実質 1200 に締め、evidence 後半の根拠が
            # 落ちて unsupported 誤判定を生んでいた → per-item cap を全体 cap に揃える。
            content = (h.get("text") or h.get("content") or "")[:3000]
            parts.append(f"[{i+1}] {src}\n{content}")
        ev_text = "\n\n".join(parts)

    prompt = VERIFY_PROMPT.format(
        claim=claim[:500],
        evidence=ev_text[:3000],
        response=response[:1000],
    )

    try:
        out = await call_llm(prompt, model=VERIFIER_MODEL, max_tokens=500, temperature=0.0)
    except Exception as e:
        return {"verdict": "error", "reason": f"llm_failed: {e}", "evidence_snippet": ""}

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", out, re.DOTALL) or \
        re.search(r"\{[^{}]*\"verdict\".*?\}", out, re.DOTALL)
    if not m:
        return {"verdict": "parse_error", "reason": "no_json", "evidence_snippet": ""}
    try:
        data = json.loads(m.group(1) if m.group(0).startswith("```") else m.group(0))
    except Exception as e:
        return {"verdict": "parse_error", "reason": str(e), "evidence_snippet": ""}

    verdict = data.get("verdict", "unsupported")
    if verdict not in {"supported", "unsupported", "contradicted"}:
        verdict = "unsupported"
    return {
        "verdict": verdict,
        "reason": (data.get("reason") or "")[:200],
        "evidence_snippet": (data.get("evidence_snippet") or "")[:200],
    }


# ─── turn pair sampling ──────────────────────────
def _is_substantive(text: str) -> bool:
    if not text or len(text) < 80:
        return False
    # 単純な確認応答は除外
    if re.match(r"^(はい|了解|分かった|ありがとう|OK|ok)[。、!.！]?\s*$", text.strip()):
        return False
    return True


def sample_turns(records: list[dict], max_turns: int) -> list[tuple[dict, dict]]:
    """user→assistant pair を集める。

    1 record = 1 turn (user or assistant)、隣接 ペアを抽出。
    """
    pairs: list[tuple[dict, dict]] = []
    by_user: dict[str, list[dict]] = {}
    for r in records:
        by_user.setdefault(r.get("user_id", ""), []).append(r)

    for uid, lst in by_user.items():
        lst.sort(key=lambda x: x.get("timestamp", ""))
        for i in range(len(lst) - 1):
            a, b = lst[i], lst[i + 1]
            if (a.get("role") == "user" and b.get("role") == "assistant"
                    and _is_substantive(b.get("text") or b.get("content") or "")):
                pairs.append((a, b))

    # 後ろから取って max_turns 件 (= 最新優先)
    return pairs[-max_turns:]


# ─── main flow ──────────────────────────────────
async def run_check(
    hours: int = 24,
    sample: int = MAX_TURNS_PER_RUN,
    dry_run: bool = False,
    brain_wiki=None,
) -> dict:
    """24h 以内の bot 応答を fact-verify。"""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    records = load_conversations(since)
    pairs = sample_turns(records, max_turns=sample)
    logger.info(f"loaded {len(records)} records, sampling {len(pairs)} pairs")

    if not pairs:
        return {"status": "no_data", "n_turns": 0}

    turn_results = []
    counter = {"supported": 0, "unsupported": 0, "contradicted": 0, "error": 0}
    n_evidence_hits = 0  # ★2026-07-02 P1c: evidence が取れた claim 数 (HTTP retrieval 生死の指標)

    for i, (u, a) in enumerate(pairs):
        user_text = u.get("text") or u.get("content") or ""
        bot_text = a.get("text") or a.get("content") or ""
        ts = a.get("timestamp", "")
        user_id_short = (a.get("user_id") or "")[:8]
        claims = await extract_claims(bot_text)
        if not claims:
            logger.info(f"[{i+1}/{len(pairs)}] no claims")
            continue

        claim_verdicts = []
        for c in claims:
            evidence = await gather_evidence(c, brain_wiki=brain_wiki)
            if evidence:
                n_evidence_hits += 1
            v = await verify_claim(c, evidence, bot_text)
            claim_verdicts.append({
                "claim": c,
                "verdict": v["verdict"],
                "reason": v["reason"],
                "evidence_snippet": v["evidence_snippet"],
            })
            counter[v["verdict"] if v["verdict"] in counter else "error"] += 1

        turn_results.append({
            "timestamp": ts,
            "user_id": user_id_short,
            "user_query": user_text[:200],
            "bot_response": bot_text[:400],
            "n_claims": len(claims),
            "claims": claim_verdicts,
        })

        logger.info(
            f"[{i+1}/{len(pairs)}] {user_id_short} claims={len(claims)} "
            f"{[c['verdict'] for c in claim_verdicts]}"
        )

    today = datetime.now(JST).strftime("%Y-%m-%d")
    summary = {
        "date": today,
        "timestamp": datetime.now(JST).isoformat(),
        "window_hours": hours,
        "n_turns_checked": len(turn_results),
        "n_claims_total": sum(t["n_claims"] for t in turn_results),
        "n_evidence_hits": n_evidence_hits,
        "verdicts": counter,
        "extractor_model": EXTRACTOR_MODEL,
        "verifier_model": VERIFIER_MODEL,
        "turns": turn_results,
    }
    # ★2026-07-02 P1c: claim があるのに evidence が 1 件も取れない = retrieval 経路 (HTTP/bot) 死亡。
    n_claims = summary["n_claims_total"]
    summary["retrieval_degraded"] = bool(n_claims > 0 and n_evidence_hits == 0)

    # 出力
    if not dry_run:
        HALL_DIR.mkdir(parents=True, exist_ok=True)
        out_path = HALL_DIR / f"{today}.json"
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        append_jsonl(LOG_PATH, {
            "timestamp": summary["timestamp"],
            "date": today,
            "n_turns_checked": summary["n_turns_checked"],
            "n_claims_total": summary["n_claims_total"],
            "verdicts": counter,
        })
        logger.info(f"saved → {out_path.name} / log → {LOG_PATH.name}")

    # LINE Push (contradicted >= 3 で通知)
    if counter["contradicted"] >= 3 and not dry_run:
        top_contradicted = []
        for t in turn_results:
            for c in t["claims"]:
                if c["verdict"] == "contradicted":
                    top_contradicted.append(f"・{c['claim'][:50]} ({c['reason'][:30]})")
                    if len(top_contradicted) >= 5:
                        break
            if len(top_contradicted) >= 5:
                break
        msg = (
            f"⚠️ うみやまAI hallucination 検出 ({today})\n"
            f"contradicted: {counter['contradicted']} 件 / "
            f"unsupported: {counter['unsupported']} 件\n"
            f"checked: {summary['n_turns_checked']} turn / {summary['n_claims_total']} claim\n\n"
            f"主な矛盾:\n" + "\n".join(top_contradicted) + "\n\n"
            f"詳細: {out_path}"
        )
        line_push(msg)

    return summary


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="検査対象期間 (時間)")
    ap.add_argument("--sample", type=int, default=MAX_TURNS_PER_RUN, help="最大検査 turn 数")
    ap.add_argument("--dry-run", action="store_true", help="LLM 呼ぶが書き込み無し")
    args = ap.parse_args()

    ensure_dirs()
    HALL_DIR.mkdir(parents=True, exist_ok=True)

    # ★2026-07-02 監査 P1c: evidence は稼働 bot の /api/brain/search を HTTP で叩いて取る
    # (gather_evidence 内)。host から chroma 直開き (§1.5 抵触) の BrainWiki 直 init は廃止。
    # retrieval_degraded は run_check が evidence hit 率から判定する (bot down/key 欠落で True)。
    result = await run_check(
        hours=args.hours,
        sample=args.sample,
        dry_run=args.dry_run,
    )

    # ★degraded を silent にしない: retrieval 無しで走った run は警報 (検証が機能してない死角)。
    # ★2026-07-02 監査 P1c: 根治済 = evidence は /api/brain/search HTTP 経由 (§1.5 回避)。
    #   これ以降 retrieval_degraded=True は「bot down / BRAIN_EXTENSION_KEY 欠落」= 真の障害を意味する。
    #   loud_fail (§1.18): 連続2日で発火、以後 72h おき再通知 (alert 疲れ回避 + loud 維持)。
    if not args.dry_run:
        try:
            from clone_improve_lib import loud_fail  # type: ignore
            loud_fail(
                "hallucination_retrieval",
                not result.get("retrieval_degraded"),
                "hallucination check の evidence 取得が全 claim で失敗 (/api/brain/search が "
                "無応答 or key 欠落)。bot 死活と BRAIN_EXTENSION_KEY を確認",
                threshold=2, cooldown_h=72,
            )
            # ★2026-07-10 (世界基準評価 #7): evidence は取れても verifier LLM が全滅すると
            #   全 claim が verdict=error になり retrieval_degraded は False のまま = fact 検証が
            #   silent に死ぬ (2026-06 の 33日 silent 死と同型)。verifier 生存も独立に loud 化。
            _vc = result.get("verdicts", {}) or {}
            _total = sum(_vc.values())
            _errs = _vc.get("error", 0) + _vc.get("parse_error", 0)
            verifier_alive = (_total == 0) or (_errs < _total)
            loud_fail(
                "hallucination_verifier", verifier_alive,
                f"verifier={VERIFIER_MODEL} が全 {_total} claim で採点失敗 "
                f"(error/parse_error)。judge LLM 死亡の疑い",
                threshold=2, cooldown_h=72,
            )
        except Exception as pe:
            logger.warning(f"degraded loud_fail failed: {pe}")

    print(json.dumps({
        "n_turns_checked": result.get("n_turns_checked", 0),
        "verdicts": result.get("verdicts", {}),
        "retrieval_degraded": result.get("retrieval_degraded", False),
    }, ensure_ascii=False, indent=2))
    # exit code: contradicted >= 1 で 1
    contradicted = result.get("verdicts", {}).get("contradicted", 0)
    return 1 if contradicted >= 1 else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
