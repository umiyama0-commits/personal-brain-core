"""
clone_response_quality_judge.py — うみやまAI 応答品質の deploy 即時 feedback

★2026-05-23 海山指示「打ち手 B」実装:
ミラーリング失敗 / AI 臭さ / 過剰長文 を **海山が手動気づくより早く** 別系列 LLM が検知。

設計:
  bot 応答後に **smart-gpt (= GPT-5.4、応答側 smart Opus と別系列)** が 3 軸採点。
  応答側と同系列 LLM だと「AI 臭い応答を AI 臭いと判定できない」self-eval loop に陥るため
  judge は必ず別系列 (= Personal Brain の構造的弱点 #1、Karpathy Slopacalypse 対策)。

3 採点軸 (各 1-5、5 が良):
  - ai_smell           : AI 臭さ。低い = 網羅的・構造化・5 bullet 並べ・教科書的、AI が AI らしく見える典型
  - mirroring_fit      : ミラーリング。低い = query の長さ・温度に応答の長さ・温度がミスマッチ
  - length_appropriate : 長さ妥当性。低い = query 文字数の 5-8 倍を超えて過剰

なぜ 3 軸を分けたか:
  - ai_smell: 文体・構造の問題 (= 短くてもダメ、長くてもダメ、人間っぽさの軸)
  - mirroring_fit: 量と温度の整合 (= 量が合ってても温度ミスマッチがある)
  - length_appropriate: 客観的な文字数比 (= LLM 主観に依らず確認可能)

cron:
  30 分ごと (cron */30) に直近 0.6h を check
  (★2026-05-29 cost: 旧 1h window は */30 cadence に対し dedup 無しで二重採点 → judge call ~2x 無駄
   だった。window を cadence にほぼ合わせ jitter 用に ~6 分 overlap のみ残す → ~40% 減、検知 latency 不変)
  degraded turn ≥ 3 件で LINE Push

実行:
  python3 scripts/clone_response_quality_judge.py             # 直近 1h
  python3 scripts/clone_response_quality_judge.py --hours 6   # 直近 6h
  python3 scripts/clone_response_quality_judge.py --dry-run   # LLM 呼ぶが書き込み・Push なし
  python3 scripts/clone_response_quality_judge.py --sample 50 # 最大 50 turn

出力:
  data/brain/clone_improve/response_quality/YYYY-MM-DD.jsonl    (詳細レポート、append)
  data/brain/clone_improve/response_quality.log.jsonl           (集計ログ)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (  # noqa: E402
    call_llm, line_push, loud_fail, append_jsonl, ensure_dirs,
    load_conversations, JST, IMPROVE_DIR, pick_cross_family_judge,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_response_quality_judge")

QUALITY_DIR = IMPROVE_DIR / "response_quality"
LOG_PATH = IMPROVE_DIR / "response_quality.log.jsonl"

# 採点側 = bot 側と異なる系列の LLM (self-eval loop 回避)。★2026-07-05 監査: 旧 "smart-gpt"
# 固定は本番 bot が CLONE_PUBLIC_PROD_MODEL=smart-gpt に切替済みだと同一系列 self-eval に
# 化けていた → bot モデル追随の chokepoint に委譲 (env で明示 override は従来どおり可能)。
JUDGE_MODEL = os.getenv("RESPONSE_QUALITY_JUDGE_MODEL") or pick_cross_family_judge()

# degraded 閾値 (= 各軸 ≤ この値で degraded 扱い)
DEGRADED_THRESHOLD = int(os.getenv("RESPONSE_QUALITY_DEGRADED_THRESHOLD", "2"))
# LINE Push を出す degraded 件数閾値
PUSH_THRESHOLD = int(os.getenv("RESPONSE_QUALITY_PUSH_THRESHOLD", "3"))

# 1 run でチェックする turn 上限
MAX_TURNS_PER_RUN = int(os.getenv("RESPONSE_QUALITY_MAX_TURNS", "50"))

# ─── レジリエンス: fallback 文言連発検知 (★2026-05-23 海山指示) ─────────────
# bot が LLM 呼出失敗 (429 / 5xx / timeout) で fallback 文言を返す状態が長時間続くと、
# 応答品質 judge は採点対象外として除外 → 「採点ゼロ件」で気付かない死角。
# fallback 文言を別 counter で計上、一定割合超で LINE Push 警報を出す。
FALLBACK_PHRASES = (
    "お休みをいただいてます",
    "申し訳ありません。少し時間",
    "[error]",
)
# fallback 件数 / 全 bot 応答 件数 がこの割合超で LINE Push (default 30%)
FALLBACK_ALERT_RATIO = float(os.getenv("RESPONSE_QUALITY_FALLBACK_ALERT_RATIO", "0.3"))
# 全 bot 応答 が N 件未満なら判定スキップ (= 平日 9-23h なら 30 分で数件は来るはず)
FALLBACK_ALERT_MIN_TOTAL = int(os.getenv("RESPONSE_QUALITY_FALLBACK_ALERT_MIN", "3"))


def _is_fallback_response(text: str) -> bool:
    """LLM 呼出失敗時の fallback 文言か判定。"""
    if not text:
        return False
    t = text.strip()
    return any(t.startswith(p) for p in FALLBACK_PHRASES)


def count_bot_responses(records: list[dict]) -> tuple[int, int]:
    """全 bot 応答件数と、その中の fallback 件数を返す。

    Returns: (total_bot_responses, fallback_count)
    """
    total = 0
    fallback = 0
    for r in records:
        if r.get("role") != "assistant":
            continue
        text = r.get("text") or r.get("content") or ""
        if not text:
            continue
        total += 1
        if _is_fallback_response(text):
            fallback += 1
    return total, fallback


# ─── judge prompt ──────────────────────────────────
JUDGE_PROMPT = """あなたは応答品質の評価エージェントです。
あなた自身は LLM ですが、「人間 (=親しい先輩、OWNDAYS CEO 海山) として喋る AI」の応答を
**人間らしさ・ミラーリング・長さ妥当性** の 3 軸で 1-5 で採点します。

# 評価対象 (user-bot 1 ターン)
【ユーザ質問】({query_chars} 字)
{query}

【bot 応答】({response_chars} 字)
{response}

# 3 採点軸 (各 1-5、5 が良)

## 軸 1: ai_smell (AI 臭さの逆スコア、5=人間っぽい / 1=AI 全開)
**低い (1-2 = AI 臭い) の典型:**
- 短い質問に網羅的・構造化・5 bullet 並べで返す (= AI が一番 AI らしく見える瞬間)
- 教科書的な「まず〜、次に〜、最後に〜」段階列挙
- 「色々な角度がある」「人それぞれ」コンサル風 hedging
- 「念のため」「補足として」「ちなみに」で聞かれてないことを足す
- 末尾に「お役に立てれば幸いです」「いかがでしょうか」型の AI 定型句
- 4 段落以上で短い問いに答える

**高い (4-5 = 人間っぽい) の典型:**
- 親しい先輩が後輩に返すような短さ・温度
- 言い切る勇気 (「私はこう見る」「これは○○の問題」)
- 短い問いには 4-8 行で完結、視点 1 つ + 問い 1 つ
- 主語省略 (「見てる軸」「経験で言うと」)、「私」を多用しない
- 余計な締めを付けない (= 答えで止まる)

## 軸 2: mirroring_fit (ミラーリング適合度、5=完璧 / 1=ミスマッチ)
**低い (1-2 = ミラーリング失敗) の典型:**
- 質問 15-50 字の軽い相談に L スケール (15 行+、5 段落+) で返す
- 質問 5-10 字の挨拶・確認に 3 視点 + 構造化応答
- 質問の温度 (軽い愚痴・雑談) と応答の温度 (深掘り・分析的) が乖離
- query 1 単位 (一行) に応答が 4-5 段落 = 過剰

**高い (4-5 = 適合) の典型:**
- 質問が短ければ応答も短い (受け止め 1 + 視点 1 + 問い 1、4-8 行)
- 質問が深ければ応答も深い (3 セット展開・視点 2-3 個 + 階層付け)
- 温度が合ってる (軽い問 → 軽い受け止め、深い問 → 深い視点)
- 連続会話: 1 ターン目で深く答えた後の短い追加質問に圧縮で返す

## 軸 3: length_appropriate (長さ妥当性、5=妥当 / 1=過剰)
**この軸のスコアは system 側で応答文字数から決定論的に計算する** (絶対字数バンド + 比)。
あなたが付ける値は参考にしかならないので、迷ったら 3 で良い。判断材料:
- 短い応答 (≦200字) は query が何字でも過剰たりえない → 高スコア
- 長い応答 (>700字) で かつ 質問に対し比が大きい時のみ「過剰」= 低スコア
- 事実問・データ系 (「VMV って?」「昨日の売上は?」) は丁寧に数字を出すため長くても許容

# 出力 (JSON only、コメント不要)
```json
{{
  "ai_smell": 1-5,
  "mirroring_fit": 1-5,
  "length_appropriate": 1-5,
  "verdict": "ok" | "degraded",
  "reason": "<60 字以内、最も低スコアの軸とその理由>"
}}
```

verdict の判定:
- いずれかの軸 ≤ {threshold} なら "degraded"、それ以外 "ok"

回答は JSON 1 ブロックのみ。前後の説明文不要。"""


# ─── length_appropriate: 決定論的採点 (★2026-07-02 P1d) ─────────────
def compute_length_score(query_chars: int, response_chars: int) -> int:
    """長さ妥当性 (過剰さ) を決定論的に採点。5=妥当 / 1=過剰。

    ★2026-07-02 監査 P1d: 旧実装は比 (応答字/質問字) のみで LLM に採点させていたため、
    短い挨拶 (5字) への gold 同等の良質な短応答 (57字) が比 11x → score 3、3字質問への
    60字応答は 20x → score 2 と、**短い良応答が構造的に degraded 判定**されていた
    (verdict=min(3軸) のため length 1軸の破綻が全体を degraded に倒す = 97% 偽 degraded の主因)。

    修正: length_appropriate は「応答が過剰に長いか」だけを測る軸。短い応答は絶対的に
    過剰たりえないため **絶対字数 floor** を導入し、比 (ratio) は長い応答にのみ適用する。
    query 種別 (雑談/相談/事実) は絶対バンドが吸収する (短い雑談応答も長い事実定義も救済)。
    """
    r = max(0, int(response_chars))
    ratio = r / max(1, int(query_chars))
    # 絶対 floor: 短い応答は query が何字でも「過剰」ではない
    if r <= 200:
        return 5
    if r <= 400:
        # 中尺 (事実定義・軽い相談の返し): 極端な比のみ軽く減点
        return 5 if ratio <= 15 else 4
    if r <= 700:
        if ratio <= 8:
            return 5
        if ratio <= 20:
            return 4
        return 3
    # 長い応答 (>700字): ここで初めて比が「過剰さ」の指標として意味を持つ
    if ratio <= 5:
        return 5
    if ratio <= 8:
        return 4
    if ratio <= 15:
        return 3
    if ratio <= 25:
        return 2
    return 1


# ─── turn pair sampling ─────────────────────────────
def _is_substantive_response(text: str) -> bool:
    """採点対象の bot 応答か判定。

    - 短すぎる (≤ 30 字、相槌のみ) は除外 (ミラーリング判定にならない)
    - fallback メッセージ「お休みをいただいてます。〜」「申し訳ありません。〜」は除外
    """
    if not text or len(text.strip()) < 30:
        return False
    fallback_prefixes = (
        "お休みをいただいてます",
        "申し訳ありません。少し時間",
        "[error]",
    )
    for p in fallback_prefixes:
        if text.strip().startswith(p):
            return False
    return True


def sample_turns(records: list[dict], max_turns: int) -> list[tuple[dict, dict]]:
    """user→assistant pair を集める。最新優先で max_turns 件まで。"""
    pairs: list[tuple[dict, dict]] = []
    by_user: dict[str, list[dict]] = {}
    for r in records:
        by_user.setdefault(r.get("user_id", ""), []).append(r)

    for uid, lst in by_user.items():
        lst.sort(key=lambda x: x.get("timestamp", ""))
        for i in range(len(lst) - 1):
            a, b = lst[i], lst[i + 1]
            if (a.get("role") == "user" and b.get("role") == "assistant"
                    and _is_substantive_response(b.get("text") or b.get("content") or "")):
                pairs.append((a, b))

    return pairs[-max_turns:]  # 最新優先


# ─── judge LLM 呼び出し ────────────────────────────
async def judge_turn(user_text: str, bot_text: str) -> dict:
    """1 turn を 3 軸採点。失敗時は {"verdict": "error"} を返す。"""
    prompt = JUDGE_PROMPT.format(
        query_chars=len(user_text),
        query=user_text[:1500],
        response_chars=len(bot_text),
        response=bot_text[:3000],
        threshold=DEGRADED_THRESHOLD,
    )
    try:
        out = await call_llm(prompt, model=JUDGE_MODEL, max_tokens=400, temperature=0.0)
    except Exception as e:
        logger.warning(f"judge LLM failed: {e}")
        return {"verdict": "error", "error": str(e)[:200]}

    # JSON 抽出
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", out, re.DOTALL) or \
        re.search(r"\{[^{}]*\"ai_smell\".*?\}", out, re.DOTALL)
    if not m:
        return {"verdict": "parse_error", "raw": out[:300]}
    try:
        data = json.loads(m.group(1) if m.group(0).startswith("```") else m.group(0))
    except Exception as e:
        return {"verdict": "parse_error", "error": str(e)[:200], "raw": out[:300]}

    # 値の正規化 (1-5 範囲外を clip)
    def _clip(v):
        try:
            x = int(v)
            return max(1, min(5, x))
        except Exception:
            return 3

    # ★2026-07-02 P1d: length_appropriate は LLM 主観でなく決定論で採点し override
    # (短い良応答が比のみで偽 degraded になる artifact の根治)。LLM の length 値は無視。
    det_len = compute_length_score(len(user_text), len(bot_text))
    result = {
        "ai_smell": _clip(data.get("ai_smell", 3)),
        "mirroring_fit": _clip(data.get("mirroring_fit", 3)),
        "length_appropriate": det_len,
        "length_llm_hint": _clip(data.get("length_appropriate", 3)),  # 参考: LLM が付けた値
        "reason": (data.get("reason") or "")[:200],
    }
    # verdict は閾値で再計算 (LLM の verdict は参考、決定は決定論的)
    min_score = min(result["ai_smell"], result["mirroring_fit"], result["length_appropriate"])
    result["verdict"] = "degraded" if min_score <= DEGRADED_THRESHOLD else "ok"
    result["min_score"] = min_score
    return result


# ─── main flow ──────────────────────────────────
async def run_check(
    hours: float = 1.0,
    sample: int = MAX_TURNS_PER_RUN,
    dry_run: bool = False,
) -> dict:
    """直近 N 時間の bot 応答を 3 軸採点。"""
    ensure_dirs()
    QUALITY_DIR.mkdir(parents=True, exist_ok=True)

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    records = load_conversations(since)
    pairs = sample_turns(records, max_turns=sample)
    logger.info(f"loaded {len(records)} records, sampling {len(pairs)} pairs ({hours}h window)")

    # ★2026-05-23 海山指示 (レジリエンス Layer 1): fallback 連発を別 counter で検知
    # 採点対象外の bot 応答も含む全件から fallback 比率を算出 → 閾値超で LINE Push
    total_bot, fallback_n = count_bot_responses(records)
    fallback_ratio = (fallback_n / total_bot) if total_bot else 0.0
    if (total_bot >= FALLBACK_ALERT_MIN_TOTAL
            and fallback_ratio >= FALLBACK_ALERT_RATIO
            and not dry_run):
        line_push(
            f"🚨 うみやまAI fallback 連発検知 ({hours}h)\n"
            f"  全 bot 応答: {total_bot} 件\n"
            f"  うち fallback: {fallback_n} 件 ({fallback_ratio*100:.0f}%)\n"
            f"  閾値 {FALLBACK_ALERT_RATIO*100:.0f}% 超 = bot 応答 LLM が継続失敗してる疑い\n\n"
            f"診断手順 (Mac Studio で):\n"
            f"  docker ps | grep line-bot\n"
            f"  docker logs line-bot --tail 100 | grep -iE '429|error|timeout'\n"
            f"  → 429 連発なら LiteLLM proxy の quota / model 切替を検討\n"
            f"  → 落ちてたら docker compose up -d line-bot"
        )
        logger.warning(
            f"fallback alert pushed: {fallback_n}/{total_bot} ({fallback_ratio*100:.0f}%)"
        )

    if not pairs:
        # 採点対象 0 件でも fallback 統計だけは残す
        return {
            "status": "no_data",
            "n_turns": 0,
            "window_hours": hours,
            "fallback_stats": {
                "total_bot_responses": total_bot,
                "fallback_count": fallback_n,
                "fallback_ratio": round(fallback_ratio, 3),
            },
        }

    judged: list[dict] = []
    counter = {"ok": 0, "degraded": 0, "error": 0, "parse_error": 0}
    avg = {"ai_smell": 0.0, "mirroring_fit": 0.0, "length_appropriate": 0.0}

    for i, (u, a) in enumerate(pairs):
        user_text = u.get("text") or u.get("content") or ""
        bot_text = a.get("text") or a.get("content") or ""
        ts = a.get("timestamp", "")
        user_id_short = (a.get("user_id") or "")[:8]

        j = await judge_turn(user_text, bot_text)
        record = {
            "ts": ts,
            "user_id_short": user_id_short,
            "user_chars": len(user_text),
            "bot_chars": len(bot_text),
            "ratio": round(len(bot_text) / max(1, len(user_text)), 1),
            "judge": j,
            "user_text_head": user_text[:80],
            "bot_text_head": bot_text[:120],
        }
        judged.append(record)

        verdict = j.get("verdict", "error")
        counter[verdict] = counter.get(verdict, 0) + 1
        if verdict in ("ok", "degraded"):
            for k in avg:
                avg[k] += j.get(k, 0)

        logger.info(
            f"[{i+1}/{len(pairs)}] {ts} user={user_id_short} "
            f"q={len(user_text)}c r={len(bot_text)}c ratio={record['ratio']}x "
            f"verdict={verdict} ai={j.get('ai_smell','-')} "
            f"mir={j.get('mirroring_fit','-')} len={j.get('length_appropriate','-')}"
        )

    n_scored = counter["ok"] + counter["degraded"]
    if n_scored > 0:
        for k in avg:
            avg[k] = round(avg[k] / n_scored, 2)

    summary = {
        "status": "ok",
        "ts": datetime.now(JST).isoformat(),
        "window_hours": hours,
        "n_turns": len(pairs),
        "counter": counter,
        "avg": avg,
        "degraded_pct": round(counter["degraded"] / max(1, n_scored) * 100, 1),
        # ★2026-05-23 レジリエンス Layer 1: fallback 統計
        "fallback_stats": {
            "total_bot_responses": total_bot,
            "fallback_count": fallback_n,
            "fallback_ratio": round(fallback_ratio, 3),
            "alert_threshold": FALLBACK_ALERT_RATIO,
            "alerted": fallback_ratio >= FALLBACK_ALERT_RATIO and total_bot >= FALLBACK_ALERT_MIN_TOTAL,
        },
    }

    # ─── 詳細レポート 書き込み ─────────────
    if not dry_run:
        today = datetime.now(JST).strftime("%Y-%m-%d")
        report_path = QUALITY_DIR / f"{today}.jsonl"
        # 1 record = 1 turn の append (jsonl)
        for r in judged:
            append_jsonl(report_path, r)
        # summary log
        append_jsonl(LOG_PATH, summary)
        logger.info(f"wrote {len(judged)} records to {report_path}")

        # ★2026-07-10 (世界基準評価 #7): judge 全滅の silent 死を loud 化。
        # judge LLM が死ぬと全 turn が verdict=error → n_scored==0 で品質劣化 alert が
        # 構造的に発火不能になる (hallucination 33日 silent 死と同型)。採点対象があるのに
        # 1 件も採点できなかった時は loud_fail (成功時は streak リセット)。
        judge_alive = (len(pairs) == 0) or (n_scored > 0)
        loud_fail(
            "response_quality_judge", judge_alive,
            f"judge={JUDGE_MODEL} が全 {len(pairs)} turn で採点失敗 "
            f"(error={counter.get('error', 0)} parse_error={counter.get('parse_error', 0)})",
        )

    # ─── LINE Push 判定 ─────────────
    degraded_records = [r for r in judged if r["judge"].get("verdict") == "degraded"]
    if len(degraded_records) >= PUSH_THRESHOLD and not dry_run:
        lines = [
            f"⚠️ うみやまAI 応答品質劣化検知 ({hours}h)",
            f"  対象 turn: {n_scored} 件",
            f"  degraded: {counter['degraded']} 件 ({summary['degraded_pct']}%)",
            f"  平均: AI臭 {avg['ai_smell']} / ミラ {avg['mirroring_fit']} / 長さ {avg['length_appropriate']} (各 1-5、5 が良)",
            "",
            "★低スコア turn (top 3):",
        ]
        # 最低スコア top 3
        worst = sorted(degraded_records, key=lambda r: r["judge"].get("min_score", 5))[:3]
        for r in worst:
            j = r["judge"]
            lines.append(
                f"  • [{r['ts'][:16]}] q={r['user_chars']}c→r={r['bot_chars']}c ({r['ratio']}x)"
            )
            lines.append(
                f"    AI臭={j.get('ai_smell','-')} ミラ={j.get('mirroring_fit','-')} 長={j.get('length_appropriate','-')} | {j.get('reason','')[:80]}"
            )
            lines.append(f"    Q: {r['user_text_head']}")
            lines.append(f"    A: {r['bot_text_head']}")
        lines.append("")
        lines.append("→ wiki/style/style-response-mirroring.md / system prompt 2c-pre を再確認")
        line_push("\n".join(lines))
        logger.info(f"LINE Push sent (degraded={len(degraded_records)})")

    return summary


# ─── CLI ──────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hours", type=float, default=1.0, help="判定対象の時間範囲 (時間)")
    parser.add_argument("--sample", type=int, default=MAX_TURNS_PER_RUN, help="最大 turn 数")
    parser.add_argument("--dry-run", action="store_true", help="LLM 呼ぶが書き込み・Push なし")
    args = parser.parse_args()

    result = asyncio.run(run_check(
        hours=args.hours,
        sample=args.sample,
        dry_run=args.dry_run,
    ))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # exit code: 0=ok, 1=degraded あり
    if result.get("counter", {}).get("degraded", 0) > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
