"""
clone_external_eval.py — 月次 第三者 blind 採点ループ

設計:
  self-evaluation loop (= LLM が自分の出力を採点 → 自分が改善 → 自分が読む) は
  Personal Brain の構造的弱点。これを断ち切るため、月 1 で**人間 5 名**に
  bot 応答を blind 採点してもらう仕組み。

  生成: 月初 1 日 10:00 (host cron)
  - 過去 30 日の clone_history から 20-30 ターンを sampling (substantive + 多様)
  - HTML form を作って data/brain/eval/external/YYYY-MM/form.html
  - 海山がそれを 5 名の評価者にメールで送る (URL 共有か添付)
  - 評価者は 5 段階 (内容正確性 / 海山っぽさ / 役立ち / トーン / 全体) で採点
  - 5-7 日後に results.json が集まる (海山が手動 import or 自動)
  - LLM-judge との agreement (Kendall's τ など) を計算 → LLM judge の信頼性を監視

  これで「LLM judge が偏った方向に応答を強化していくモデル崩壊」が起きてないか
  外部視点で検証できる。

cron: 月初 1 日 10:00 (host cron、host crontab に追加)
  - 月によって sampling 数を絞る (cost & 評価者疲れ)

出力:
  data/brain/eval/external/YYYY-MM/
    ├── form.html        # 評価フォーム (海山が配布)
    ├── responses.json   # sampled ターン (form の正解)
    ├── results.json     # 評価者から集まった採点 (海山が手動 import)
    └── agreement.json   # LLM-judge との agreement (results.json import 後に計算)

実行:
  python3 scripts/clone_external_eval.py --generate           # 今月分の form 生成
  python3 scripts/clone_external_eval.py --generate --month 2026-05  # 月指定
  python3 scripts/clone_external_eval.py --import results.csv # 評価結果取り込み
  python3 scripts/clone_external_eval.py --report             # 直近月の集計サマリ
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (  # noqa: E402
    load_conversations, JST, DATA_BRAIN, pick_cross_family_judge,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_external_eval")

EVAL_DIR = DATA_BRAIN / "eval" / "external"

# 1 form あたりの target ターン数 (評価者が 30 分で終わる量)
DEFAULT_N_TURNS = int(os.getenv("EXT_EVAL_N_TURNS", "20"))
# 評価軸 (5 段階)
EVAL_AXES = [
    ("accuracy", "内容正確性 (事実誤りがないか)"),
    ("authenticity", "海山っぽさ (本人らしいトーン/視点か)"),
    ("usefulness", "役立ち (相手にとって有意義か)"),
    ("tone", "トーン (押し付けがましくないか、温度が適切か)"),
    ("overall", "全体評価"),
]


# ─── sampling ─────────────────────────────────
def _is_substantive(text: str) -> bool:
    if not text or len(text) < 80:
        return False
    if re.match(r"^(はい|了解|分かった|ありがとう|OK|ok)[。、!.！]?\s*$", text.strip()):
        return False
    return True


def _is_substantive_query(text: str) -> bool:
    """user query 側: 短い相槌 / 単純確認は除外、30 字以上 or 業務 keyword 含む。"""
    if not text:
        return False
    if len(text) >= 30:
        return True
    # 業務 keyword (短くても eval 対象に入れる)
    biz_kw = ("売上", "AOP", "判断", "店長", "決裁", "迷", "悩", "考え方", "戦略", "投資")
    return any(kw in text for kw in biz_kw)


def sample_turns_for_eval(days: int, n_turns: int) -> list[dict]:
    """過去 days 日の clone_history から評価対象 pair を sampling。

    多様性のため:
    - 同じ user が連続で 5 pair 以上は取らない
    - 同じ日に 4 pair 以上は取らない
    - 短すぎる / 確認応答は除外
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    records = load_conversations(since)

    # user 別に並べる
    by_user: dict[str, list[dict]] = {}
    for r in records:
        by_user.setdefault(r.get("user_id", ""), []).append(r)

    # 隣接 user→assistant pair で substantive 条件を満たす候補集合
    candidates: list[dict] = []
    for uid, lst in by_user.items():
        lst.sort(key=lambda x: x.get("timestamp", ""))
        for i in range(len(lst) - 1):
            a, b = lst[i], lst[i + 1]
            if a.get("role") != "user" or b.get("role") != "assistant":
                continue
            user_text = a.get("text") or a.get("content") or ""
            bot_text = b.get("text") or b.get("content") or ""
            if not _is_substantive_query(user_text):
                continue
            if not _is_substantive(bot_text):
                continue
            candidates.append({
                "user_id": uid[:8],
                "timestamp": b.get("timestamp", ""),
                "user_query": user_text,
                "bot_response": bot_text,
            })

    if not candidates:
        return []

    # 多様性 sampling
    random.seed(42)  # 再現性のため
    random.shuffle(candidates)
    seen_per_user: dict[str, int] = {}
    seen_per_day: dict[str, int] = {}
    selected: list[dict] = []
    for c in candidates:
        uid = c["user_id"]
        day = c["timestamp"][:10]
        if seen_per_user.get(uid, 0) >= 5:
            continue
        if seen_per_day.get(day, 0) >= 4:
            continue
        selected.append(c)
        seen_per_user[uid] = seen_per_user.get(uid, 0) + 1
        seen_per_day[day] = seen_per_day.get(day, 0) + 1
        if len(selected) >= n_turns:
            break

    return selected


# ─── HTML form 生成 ───────────────────────────────
HTML_HEAD = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>うみやまAI 第三者評価 ({month})</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Helvetica Neue", "Hiragino Sans", sans-serif;
          max-width: 800px; margin: 32px auto; padding: 0 16px; line-height: 1.7; color: #222; }}
  h1 {{ font-size: 20px; }}
  h2 {{ font-size: 16px; margin-top: 24px; border-top: 1px solid #ccc; padding-top: 16px; }}
  .meta {{ color: #888; font-size: 13px; }}
  .qa {{ background: #f7f7f7; padding: 12px; border-radius: 6px; margin: 8px 0; }}
  .qa .q {{ color: #666; }}
  .qa .a {{ margin-top: 8px; white-space: pre-wrap; }}
  .axes {{ margin: 8px 0; }}
  .axes label {{ display: block; margin: 4px 0; }}
  textarea {{ width: 100%; min-height: 60px; }}
  .submit {{ margin: 24px 0; padding: 12px 24px; background: #2563eb; color: white; border: 0; border-radius: 6px; cursor: pointer; }}
  .intro {{ background: #fff8e1; padding: 16px; border-radius: 8px; margin: 16px 0; }}
</style>
</head>
<body>
<h1>うみやまAI 第三者評価 — {month}</h1>
<div class="intro">
  <p>うみやまAI (海山さんの AI 分身) が社員からの質問にどう答えてるかを、
     <b>blind</b> で評価してください (発信者匿名、内容のみで判断)。</p>
  <p>各ターンに 5 段階で採点:</p>
  <ul>
    <li><b>5</b>: 完璧 (海山本人と判別不能、内容正しい、トーン適切)</li>
    <li><b>4</b>: 良い (1-2 点だけ気になるが大半 OK)</li>
    <li><b>3</b>: 標準 (許容範囲、改善余地あり)</li>
    <li><b>2</b>: 微妙 (違和感ある、事実 or トーン に問題)</li>
    <li><b>1</b>: 悪い (本人なら絶対こう答えない、誤情報含む等)</li>
  </ul>
  <p><b>合計 {n_turns} ターン</b> / 所要時間目安: 30-40 分</p>
  <p>記入後に最下部の「結果をエクスポート」を押すと、ファイルがダウンロードされます。
     そのファイルを<b>海山に返送</b>してください。</p>
</div>

<form id="evalForm">
"""

HTML_TURN = """<h2>ターン {idx} / {total}</h2>
<div class="meta">id: {tid} / 日付: {date}</div>
<div class="qa">
  <div class="q">社員: {user_query_html}</div>
  <div class="a">AI: {bot_response_html}</div>
</div>
<div class="axes">
  {axes_html}
</div>
<label>コメント (任意): <textarea name="t{idx}__comment" rows="2"></textarea></label>
"""

HTML_FOOT = """
<button type="button" class="submit" onclick="exportResults()">結果をエクスポート (JSON)</button>
</form>

<script>
function exportResults() {
  const form = document.getElementById('evalForm');
  const data = {};
  const fd = new FormData(form);
  for (const [k, v] of fd.entries()) data[k] = v;
  const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'eval_results__' + (new Date()).toISOString().replace(/[:.]/g, '-') + '.json';
  a.click();
}
</script>
</body>
</html>
"""


def build_axes_html(idx: int) -> str:
    lines: list[str] = []
    for key, label in EVAL_AXES:
        opts = "".join(
            f'<label style="display:inline-block;margin-right:12px;"><input type="radio" name="t{idx}__{key}" value="{s}" required> {s}</label>'
            for s in (1, 2, 3, 4, 5)
        )
        lines.append(f'<div><b>{label}</b>: {opts}</div>')
    return "\n".join(lines)


def generate_form(month: str, n_turns: int = DEFAULT_N_TURNS,
                  days: int = 30, dry_run: bool = False) -> Path | None:
    """月初に呼ばれて当月分の form を生成。"""
    turns = sample_turns_for_eval(days=days, n_turns=n_turns)
    if not turns:
        logger.info(f"no turns to evaluate for {month}")
        return None

    out_dir = EVAL_DIR / month
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        responses_path = out_dir / "responses.json"
        responses_path.write_text(
            json.dumps(turns, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # HTML 構築
    body_parts: list[str] = []
    body_parts.append(HTML_HEAD.format(month=month, n_turns=len(turns)))
    for idx, t in enumerate(turns, start=1):
        body_parts.append(HTML_TURN.format(
            idx=idx,
            total=len(turns),
            tid=f"{month}-{idx:03d}",
            date=(t["timestamp"] or "")[:10],
            user_query_html=html.escape(t["user_query"][:800]),
            bot_response_html=html.escape(t["bot_response"][:1500]),
            axes_html=build_axes_html(idx),
        ))
    body_parts.append(HTML_FOOT)
    html_text = "".join(body_parts)

    if dry_run:
        logger.info(f"[DRY] would write form: {len(turns)} turns, {len(html_text)} chars")
        return None

    form_path = out_dir / "form.html"
    form_path.write_text(html_text, encoding="utf-8")
    logger.info(f"form generated: {form_path} ({len(turns)} turns)")
    return form_path


# ─── 結果取り込み ─────────────────────────────────
def import_results(json_path: Path, month: str | None = None) -> dict:
    """評価者から受け取った eval_results__*.json を当月の results.json に取り込み。

    複数評価者の結果を array で蓄積する。
    JSON format: { "t1__accuracy": "5", "t1__authenticity": "4", ..., "t1__comment": "..." }
    """
    if not json_path.exists():
        raise SystemExit(f"file not found: {json_path}")
    raw = json.loads(json_path.read_text(encoding="utf-8"))

    if month is None:
        # ファイル時刻から月推定
        month = datetime.now(JST).strftime("%Y-%m")

    out_dir = EVAL_DIR / month
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"
    existing = []
    if results_path.exists():
        try:
            existing = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception:
            existing = []

    # 1 評価者分の record を作る
    rater_id = json_path.stem  # filename がそのまま id
    rater = {"rater_id": rater_id, "imported_at": datetime.now(JST).isoformat(),
             "scores": {}, "comments": {}}

    # t1__accuracy=5, t1__comment="..." を整理
    # JSON serialization 都合で key は str で持つ (json read 後も同じ key で引ける)
    pat = re.compile(r"^t(\d+)__(\w+)$")
    for k, v in raw.items():
        m = pat.match(k)
        if not m:
            continue
        idx_str = m.group(1)
        axis = m.group(2)
        if axis == "comment":
            rater["comments"][idx_str] = v
        else:
            try:
                rater["scores"].setdefault(idx_str, {})[axis] = int(v)
            except Exception:
                pass

    existing.append(rater)
    results_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"imported rater {rater_id} → {results_path} (total {len(existing)} raters)")
    return rater


# ─── 集計レポート ─────────────────────────────────
def build_report(month: str) -> dict:
    """results.json から axis 別 mean / std / inter-rater agreement / ターン別 mean を集計。

    ★2026-05-21 拡張: inter-rater agreement (ICC + 2-rater Pearson) + lowest turns。
    """
    out_dir = EVAL_DIR / month
    results_path = out_dir / "results.json"
    responses_path = out_dir / "responses.json"
    if not results_path.exists():
        return {"status": "no_results", "month": month}
    raters = json.loads(results_path.read_text(encoding="utf-8"))
    turns = json.loads(responses_path.read_text(encoding="utf-8")) if responses_path.exists() else []

    # axis 別集計
    axis_scores: dict[str, list[int]] = {k: [] for k, _ in EVAL_AXES}
    # turn × axis × rater のマトリクス (agreement 算出用)
    by_turn_axis: dict[str, dict[str, dict[str, int]]] = {}
    # ↑ structure: {turn_idx_str: {axis: {rater_id: score}}}

    for rater in raters:
        rid = rater.get("rater_id", "?")
        for idx_str, scores in rater.get("scores", {}).items():
            for axis, val in scores.items():
                axis_scores.setdefault(axis, []).append(val)
                by_turn_axis.setdefault(idx_str, {}).setdefault(axis, {})[rid] = val

    def _mean(xs: list[int]) -> float:
        return round(sum(xs) / len(xs), 2) if xs else 0.0

    def _std(xs: list[float]) -> float:
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        return round((sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5, 2)

    # Inter-rater agreement (per axis):
    # - 各 turn で「rater 全員が同じ score だったか」「差が 1 以内」「差が 2 以上」を集計
    # - ICC (Intraclass Correlation Coefficient) のシンプル実装 (ICC(2,1) 近似):
    #     ICC = (between_turn_variance - within_turn_variance) / between_turn_variance
    #     1.0 に近いほど rater 一致、0 以下なら不一致
    def _inter_rater_agreement(axis_key: str) -> dict:
        per_turn_scores = []
        all_scores = []
        for tidx, ax_map in by_turn_axis.items():
            r_map = ax_map.get(axis_key, {})
            if len(r_map) < 2:
                continue
            scores = list(r_map.values())
            per_turn_scores.append(scores)
            all_scores.extend(scores)

        if not per_turn_scores:
            return {"n_turn_with_2plus_raters": 0}

        n_turns_eligible = len(per_turn_scores)
        # 完全一致 / 1 以内 / 2 以上ズレ
        exact = sum(1 for s in per_turn_scores if max(s) == min(s))
        within1 = sum(1 for s in per_turn_scores if (max(s) - min(s)) <= 1)
        far = sum(1 for s in per_turn_scores if (max(s) - min(s)) >= 2)

        # ICC(2,1) シンプル近似
        grand_mean = sum(all_scores) / len(all_scores)
        # MS_between (各 turn の mean を grand_mean から距離)
        ms_between = 0.0
        ms_within = 0.0
        for s in per_turn_scores:
            tm = sum(s) / len(s)
            ms_between += (tm - grand_mean) ** 2 * len(s)
            ms_within += sum((x - tm) ** 2 for x in s)
        ms_between /= max(1, n_turns_eligible - 1)
        # within DF = N_total - N_turns
        within_df = max(1, len(all_scores) - n_turns_eligible)
        ms_within /= within_df

        if ms_between > 0:
            icc = round((ms_between - ms_within) / ms_between, 3)
        else:
            icc = 0.0

        return {
            "n_turn_with_2plus_raters": n_turns_eligible,
            "exact_agreement_rate": round(exact / n_turns_eligible, 3),
            "within_1_rate": round(within1 / n_turns_eligible, 3),
            "far_2plus_rate": round(far / n_turns_eligible, 3),
            "icc_approx": icc,
        }

    summary = {
        "month": month,
        "n_raters": len(raters),
        "n_turns": len(turns),
        "axis_mean": {k: _mean(v) for k, v in axis_scores.items()},
        "axis_std": {k: _std([float(x) for x in v]) for k, v in axis_scores.items()},
        "agreement": {k: _inter_rater_agreement(k) for k, _ in EVAL_AXES},
        "lowest_turns": [],
    }

    # 最低スコア turn (overall mean が低い順 top 5)
    turn_overall = []
    for idx_str, ax_map in by_turn_axis.items():
        ov = ax_map.get("overall", {})
        if ov:
            turn_overall.append({"idx": int(idx_str), "overall_mean": _mean(list(ov.values()))})
    turn_overall.sort(key=lambda x: x["overall_mean"])
    summary["lowest_turns"] = turn_overall[:5]

    out_path = out_dir / "report.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


# ─── LLM-as-judge vs Human agreement ─────────────
def _pearson(xs: list, ys: list):
    """Pearson 相関 (numpy 非依存)。分散 0 / n<2 は None。"""
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def _weighted_cohen_kappa(xs: list, ys: list, lo: int = 1, hi: int = 5):
    """ordinal rating の quadratic weighted Cohen's κ (numpy 非依存)。

    ★2026-06-08 評価 LLMOps G3: pearson_r は「judge が一貫して甘い/辛い」systematic bias を
    検出できない (定数オフセットでも r=1.0 になりうる)。weighted κ は chance 補正 + 距離重みで
    systematic bias を捉える。production 標準は κ<0.6 で再較正アラート。
    入力 (human_mean は float) は最近接 int に丸め [lo,hi] に clamp して category 化。
    """
    n = len(xs)
    if n < 2 or len(ys) != n:
        return None

    def _q(v):
        return max(lo, min(hi, int(round(v))))

    cats = list(range(lo, hi + 1))
    k = len(cats)
    cidx = {c: i for i, c in enumerate(cats)}
    obs = [[0] * k for _ in range(k)]
    for a, b in zip(xs, ys):
        obs[cidx[_q(a)]][cidx[_q(b)]] += 1
    row = [sum(obs[i]) for i in range(k)]
    col = [sum(obs[i][j] for i in range(k)) for j in range(k)]
    denom = (k - 1) ** 2
    num = den = 0.0
    for i in range(k):
        for j in range(k):
            w = (i - j) ** 2 / denom
            num += w * obs[i][j]
            den += w * (row[i] * col[j] / n)
    if den == 0:
        return 1.0  # 重み付き不一致が期待値ゼロ = 完全一致
    return 1.0 - num / den


async def _judge_overall(call_llm, extract_json, user_query: str, bot_response: str, model: str):
    """1 ターンの応答 overall を 1-5 で採点 (bot と別系列 model で self-eval loop 遮断)。"""
    prompt = (
        "あなたは OWNDAYS 社内 AI「うみやまAI」の応答品質を採点する第三者評価者。\n"
        # ★2026-07-05 監査 fix: 採点 anchor を人間 form (build_form の 5 段階定義) と
        # 一字一句揃える。anchor が別物だと κ が「judge の偏り」でなく「基準差の offset」を測ってしまう。
        "以下 1 ターンの応答 overall を 1-5 整数で採点:\n"
        "5: 完璧 (海山本人と判別不能、内容正しい、トーン適切)\n"
        "4: 良い (1-2 点だけ気になるが大半 OK)\n"
        "3: 標準 (許容範囲、改善余地あり)\n"
        "2: 微妙 (違和感ある、事実 or トーン に問題)\n"
        "1: 悪い (本人なら絶対こう答えない、誤情報含む等)\n\n"
        f"質問: {user_query[:500]}\n応答: {bot_response[:1500]}\n\n"
        'output JSON: {"overall": <1-5 整数>}'
    )
    try:
        raw = await call_llm(prompt, model=model, max_tokens=120, temperature=0.0)
        data = extract_json(raw)
        v = data.get("overall") if isinstance(data, dict) else None
        return float(v) if v is not None else None
    except Exception:
        return None


async def compute_llm_human_agreement(month: str, judge_model: str = "", max_turns: int = 30) -> dict:
    """LLM-as-judge と人間採点 (external eval) の overall 一致率を計算 (judge 信頼性の月次メタ監視)。

    ★2026-06-07 エージェント評価 Phase 2 実装: 旧実装は regression (固定30Q) との turn 対応が無く
    phase_2_pending の永久 stub だった (= judge が偏ってないか人間基準で検算する最終防衛線が未稼働)。
    external eval が採点した **まさにその sampled turn** を LLM judge にも通すことで turn-aligned に。
    judge は bot と別系列 (self-eval loop 遮断)。★2026-07-05 監査: 旧 "smart-gpt" 固定は本番 bot が
    smart-gpt 化済みだと同一系列に化ける → 既定は pick_cross_family_judge で bot モデル追随。

    返り値: {status, n_overlap_turns, mean_abs_diff, within_1_rate, pearson_r, judge_model}
    """
    judge_model = judge_model or pick_cross_family_judge()
    out_dir = EVAL_DIR / month
    results_path = out_dir / "results.json"
    responses_path = out_dir / "responses.json"
    if not results_path.exists() or not responses_path.exists():
        return {"status": "no_data"}

    raters = json.loads(results_path.read_text(encoding="utf-8"))
    turns = json.loads(responses_path.read_text(encoding="utf-8"))

    # turn ごとの human mean (overall axis)
    human_per_turn: dict[int, list] = {}
    for r in raters:
        for idx_str, scores in r.get("scores", {}).items():
            if isinstance(scores, dict) and "overall" in scores:
                try:
                    human_per_turn.setdefault(int(idx_str), []).append(float(scores["overall"]))
                except Exception:
                    continue
    human_mean = {k: sum(vs) / len(vs) for k, vs in human_per_turn.items() if vs}
    if not human_mean:
        return {"status": "no_human_overall"}

    # 人間が採点した **その turn** を LLM judge にも通す (turn-aligned)
    # ★2026-07-05 監査 fix: form の turn id は 1 始まり (build 時 enumerate(start=1) →
    # t1__overall → import_results の key "1"..) だが responses.json は 0 始まり list。
    # 従来の turns[idx] 直引きは「人間が採点した turn の次の turn」を judge に渡しており、
    # κ/pearson が全 pair で misaligned だった (最終 turn は silent drop)。1-based → 0-based 変換。
    from clone_improve_lib import call_llm, extract_json  # type: ignore
    pairs = []  # (human, llm)
    for idx in sorted(human_mean.keys())[:max_turns]:
        if idx < 1 or idx > len(turns):
            continue
        t = turns[idx - 1]
        score = await _judge_overall(call_llm, extract_json,
                                     t.get("user_query", ""), t.get("bot_response", ""), judge_model)
        if score is not None:
            pairs.append((human_mean[idx], score))
    if len(pairs) < 2:
        return {"status": "too_few_pairs", "n_pairs": len(pairs), "n_human_turns": len(human_mean)}

    diffs = [abs(h - l) for h, l in pairs]
    hs = [h for h, _ in pairs]
    ls = [l for _, l in pairs]
    r = _pearson(hs, ls)
    kappa = _weighted_cohen_kappa(hs, ls)
    return {
        "status": "ok",
        "n_overlap_turns": len(pairs),
        "mean_abs_diff": round(sum(diffs) / len(diffs), 3),
        "within_1_rate": round(sum(1 for d in diffs if d <= 1.0) / len(diffs), 3),
        "pearson_r": round(r, 3) if r is not None else None,
        # ★2026-06-08 評価 G3: systematic bias (judge が一貫して甘い/辛い) を捉える weighted κ。
        "cohen_kappa_weighted": round(kappa, 3) if kappa is not None else None,
        "judge_model": judge_model,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true", help="今月分の form を生成")
    ap.add_argument("--month", default=None, help="対象月 (YYYY-MM)、省略時は今月")
    ap.add_argument("--n-turns", type=int, default=DEFAULT_N_TURNS, help="form の turn 数")
    ap.add_argument("--days", type=int, default=30, help="sampling 期間 (日)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--import-file", help="評価結果 JSON を取り込み")
    ap.add_argument("--report", action="store_true", help="集計レポート出力")
    ap.add_argument("--agreement", action="store_true",
                    help="LLM judge vs Human agreement を出す (Phase 2 機能、stub あり)")
    ap.add_argument("--regression-path", default=None,
                    help="--agreement 用、clone_style_regression の JSON path")
    args = ap.parse_args()

    month = args.month or datetime.now(JST).strftime("%Y-%m")

    if args.generate:
        path = generate_form(month, n_turns=args.n_turns,
                              days=args.days, dry_run=args.dry_run)
        if path:
            print(f"form: {path}")
        return 0

    if args.import_file:
        rater = import_results(Path(args.import_file), month=month)
        print(json.dumps({"rater_id": rater["rater_id"],
                          "n_scores": sum(len(v) for v in rater["scores"].values())},
                         ensure_ascii=False, indent=2))
        return 0

    if args.report:
        summary = build_report(month)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.agreement:
        import asyncio
        out = asyncio.run(compute_llm_human_agreement(month))
        # 結果を agreement.json に保存 + judge と人間の乖離が大きければ LINE 警報 (judge 信頼性メタ監視)
        try:
            adir = EVAL_DIR / month
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "agreement.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        _kappa = out.get("cohen_kappa_weighted")
        # ★2026-06-08 評価 G3: κ<0.6 (systematic bias) も警報条件に追加 (pearson だけでは漏れる)
        if out.get("status") == "ok" and (
            out.get("mean_abs_diff", 0) >= 1.5
            or out.get("within_1_rate", 1) < 0.5
            or (_kappa is not None and _kappa < 0.6)
        ):
            try:
                from clone_improve_lib import line_push
                line_push(f"⚠️ LLM judge vs 人間 agreement 低下 ({month}): 平均差 {out.get('mean_abs_diff')}, "
                          f"±1一致 {out.get('within_1_rate')}, r={out.get('pearson_r')}, "
                          f"weighted κ={_kappa} → judge 偏り疑い (κ<0.6 は systematic bias)")
            except Exception:
                pass
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
