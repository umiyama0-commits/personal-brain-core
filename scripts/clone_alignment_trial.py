"""
clone_alignment_trial.py — うみやまAI v1 正式公開前の集中アラインメント sprint

設計:
  100 件の仮想質問 → 一括応答取得 → HTML レポート → 海山コメント →
  wiki / prompt 反映 → 再応答 → diff 比較 のループ。

  既存の clone_style_regression (= 30 質問の夜間自動採点) や
  clone_external_eval (= 月次第三者 blind 採点) とは目的が違い、
  「**正式公開前に海山が集中して 1-2 時間で精度合わせ**」する用。

質問テンプレート構成 (海山指定の比重):
  店舗 70 件 (店長 40 / SV 15 / AM 5 / スタッフ 10)
  本部 30 件 (営業 8 / 商品 5 / マーケ 5 / 人事 4 / 経理財務 4 / IT/DX 2 / 法務 2)

フロー:
  1. parse_questions(questions.md) → list[dict]
  2. run_trial(questions, model="smart") → 各質問に bot 応答付与
  3. generate_html(...) → browser で開けるレビュー UI
  4. 海山がコメント記入 + JSON エクスポート
  5. ingest_review(review.json) → 結果に反映
  6. (option) rerun(base) → 改善後の再応答
  7. diff(run1, run2) → 改善 trend レポート

実行:
  python3 scripts/clone_alignment_trial.py --generate
  python3 scripts/clone_alignment_trial.py --run --run-id 2026-05-21_run1
  open data/brain/clone_improve/alignment_trial/runs/2026-05-21_run1.html
  python3 scripts/clone_alignment_trial.py --ingest-review review.json --run-id 2026-05-21_run1
  python3 scripts/clone_alignment_trial.py --rerun --base 2026-05-21_run1 --run-id 2026-05-22_run2
  python3 scripts/clone_alignment_trial.py --diff 2026-05-21_run1 2026-05-22_run2
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_alignment_trial")

APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
DATA_BRAIN = APP_ROOT / "data" / "brain"
TRIAL_DIR = DATA_BRAIN / "clone_improve" / "alignment_trial"
# questions.md は docs/ 配下に置いて tracked (生成物の runs/ は data/ 配下、ignored)
QUESTIONS_PATH = APP_ROOT / "docs" / "alignment_trial" / "questions.md"
RUNS_DIR = TRIAL_DIR / "runs"
JST = timezone(timedelta(hours=9))


# ─── 質問テンプレート parse ─────────────────────
QUESTION_HEADER_RE = re.compile(
    r"^##\s+([a-z]+(?:-[a-z]+)*-\d+)\s+\((?P<role>[^/]+)/\s*(?P<category>[^)]+)\)\s*$",
    re.MULTILINE,
)


def parse_questions(path: Path | None = None) -> list[dict]:
    """questions.md から質問 list を抽出。

    フォーマット:
      ## <id> (役職 / カテゴリ)
      <シナリオ本文>

      **expected_axes**:
      - 軸1
      - 軸2

      ---
    """
    p = path or QUESTIONS_PATH
    if not p.exists():
        raise SystemExit(f"questions file not found: {p}")
    text = p.read_text(encoding="utf-8")

    blocks = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    questions = []
    for block in blocks:
        m = QUESTION_HEADER_RE.search(block)
        if not m:
            continue
        qid = m.group(1).strip()
        role = m.group("role").strip()
        category = m.group("category").strip()

        # 本文 = header 行の後、**expected_axes** の前まで
        after_header = block[m.end():].strip()
        ax_m = re.search(r"\*\*expected_axes\*\*\s*:?\s*", after_header)
        if ax_m:
            scenario = after_header[: ax_m.start()].strip()
            axes_text = after_header[ax_m.end():].strip()
            expected_axes = [
                line.lstrip("- ").strip()
                for line in axes_text.splitlines()
                if line.strip().startswith("-")
            ]
        else:
            scenario = after_header.strip()
            expected_axes = []

        if not scenario:
            continue

        questions.append({
            "id": qid,
            "role": role,
            "category": category,
            "scenario": scenario,
            "expected_axes": expected_axes,
        })
    return questions


def count_by_role(questions: list[dict]) -> dict:
    """役職別件数の集計。"""
    counts: dict[str, int] = {}
    for q in questions:
        counts[q["role"]] = counts.get(q["role"], 0) + 1
    return counts


# ─── 一括応答取得 ──────────────────────────────
# ─── 応答制約 prefix (alignment trial 用) ──────
# 各質問に prepend する制約。AI が敬語化 / 長文化するのを抑える。
# ★2026-05-21 海山指示: 応答が長すぎる + 急に敬語になる問題への対処。
TRIAL_PROMPT_PREFIX = """★この社員からの質問への返答にあたり、以下を厳守:

【トーン】
- **普段の砕けたトーン** で返す (= 敬語じゃない、です/ます を取る、距離感を縮める)
- 相手が役職持ち / 敬語で来ても、こっちのトーンは変えない (= 海山らしさを保つ)
- 「私」を多用しない、主語省略がデフォルト

【冒頭と語尾 ★2026-05-22 海山指摘】
- **「なるほど」を冒頭で使わない** (= 1/10 ターンくらいの頻度に抑える、別の入り方を使う)
  別の入り方: 「TSA 落ちてるか。〜」「そういう時もあるよね」「いつもお疲れさま。なに?」
  「分かるよ」「いいね、それやろう」「いやー、それはどうかな」等
- **「推測も入るけど」「ここは推測だけど」を多用しない** (= 自然なヘッジで処理)
  代替: 「〜じゃないかな」「たぶん〜」「〜な気がする」「〜寄りに見える」等
- 末尾に教訓・メタ説明を付けない、答えで止まる

【語彙 ★2026-05-22 海山指摘】
- **店舗 (店長 / SV / AM / スタッフ) 向けには横文字を避ける**
  ❌ レバレッジ点 / レバレッジになる / CVR 落ちてる / 客の wanted / Imperative
  ✅ 一番効くポイント / ここを動かすと全体が動く / 入店から購入の取りこぼし
- **本部 (経理 / 経営企画 / IT / 法務 / マーケ 等) 向けは横文字 OK** (= 共通言語)

【役職 ★2026-05-22 海山指摘】
- **質問者が役職を前置きしてくれる前提で組み立てない** (= 実運用では前置きナシが多い)
- 内容から自然に判断する。不明なら最後の 1 行で「店舗 側? 本部 側?」型で軽く確認
- 「店舗の方なら...」「本部の方なら...」型の分岐列挙はしない (= しんどい応答になる)

【長さと構造】
- **150-300 字程度を目安** に簡潔に (= 過剰説明しない、列挙しない)
- 結論 → 補足 → (時々) 短い問い返し、で組み立てる
- 1 番効くポイント 1 つに絞る + (必要なら) 開かれた問いで止まる

【質問】
"""


async def run_trial(
    questions: list[dict],
    model: str = "smart",
    brain_wiki=None,
    max_concurrency: int = 3,
    sleep_between_sec: float = 0.5,
    use_prefix: bool = True,
) -> list[dict]:
    """全 100 件を bot に流して応答取得。

    brain_wiki が None なら BrainWiki を import して動かす。
    test 環境では brain_wiki に Mock 渡して LLM 呼び避けられる。
    """
    import asyncio
    import time

    if brain_wiki is None:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from brain_wiki import BrainWiki  # type: ignore
            from brain_index import BrainIndex  # type: ignore
            import httpx
            http = httpx.AsyncClient(timeout=120.0)
            brain_wiki = BrainWiki(http, os.getenv("LITELLM_URL"),
                                    os.getenv("LITELLM_MASTER_KEY"))
            bi = BrainIndex(http, os.getenv("LITELLM_URL"),
                             os.getenv("LITELLM_MASTER_KEY"))
            brain_wiki.set_index(bi)
        except Exception as e:
            logger.error(f"BrainWiki init failed: {e}")
            raise

    results = []
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(idx: int, q: dict):
        async with sem:
            try:
                from clone_improve_lib import eval_turn_guard
                eval_turn_guard()  # ★2026-06-11 コスト保護 (EVAL_MAX_BOT_TURNS 超で停止)
                # ★2026-05-21: 応答短め + 砕けたトーン保持の prefix
                query_text = (TRIAL_PROMPT_PREFIX + q["scenario"]) if use_prefix \
                    else q["scenario"]
                resp = await brain_wiki.clone_respond_public(
                    query_text, model=model,
                )
                logger.info(f"[{idx+1}/{len(questions)}] {q['id']} done "
                            f"({len(resp)} chars)")
                return {
                    **q,
                    "response": resp,
                    "model": model,
                    "ts": datetime.now(JST).isoformat(timespec="seconds"),
                }
            except Exception as e:
                logger.warning(f"[{idx+1}/{len(questions)}] {q['id']} failed: {e}")
                return {**q, "response": f"[ERROR] {e}", "model": model,
                        "ts": datetime.now(JST).isoformat(timespec="seconds")}
            finally:
                await asyncio.sleep(sleep_between_sec)

    tasks = [_one(i, q) for i, q in enumerate(questions)]
    results = await asyncio.gather(*tasks)
    return results


# ─── HTML レポート生成 ─────────────────────────
HTML_HEAD = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>うみやまAI Alignment Trial — {tag}</title>
<style>
  body {{
    font-family: -apple-system, "Segoe UI", "Helvetica Neue", "Hiragino Sans", sans-serif;
    max-width: 980px; margin: 24px auto; padding: 0 16px; line-height: 1.7;
    color: #222; background: #fafafa;
  }}
  h1 {{ font-size: 22px; }}
  h2 {{
    font-size: 16px; margin-top: 36px; padding: 8px 12px;
    background: #2563eb; color: white; border-radius: 6px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  h2 .meta {{ font-size: 12px; opacity: 0.85; font-weight: normal; }}
  .scenario {{
    background: #fff8e1; padding: 14px 16px; border-radius: 6px; margin: 10px 0;
    white-space: pre-wrap; font-size: 14.5px;
  }}
  .response {{
    background: white; padding: 14px 16px; border-radius: 6px; margin: 10px 0;
    border: 1px solid #ddd; white-space: pre-wrap; font-size: 14.5px;
  }}
  .axes {{ font-size: 13px; color: #666; margin: 8px 0 16px 0; }}
  .axes ul {{ margin: 4px 0; padding-left: 20px; }}
  .axes li {{ margin: 2px 0; }}
  .verdict {{ margin: 8px 0; }}
  .verdict label {{ margin-right: 12px; cursor: pointer; }}
  .editable {{
    width: 100%; padding: 14px 16px; box-sizing: border-box;
    border: 2px solid #b8c4d6; border-radius: 6px; font-family: inherit;
    font-size: 14.5px; line-height: 1.65;
    resize: vertical; transition: border-color 0.15s ease;
  }}
  .editable:focus {{
    outline: none; border-color: #2563eb;
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
  }}
  .editable.q-box {{
    min-height: 120px; background: #fff8e1;
  }}
  .editable.r-box {{
    min-height: 220px; background: #fefefe;
  }}
  .edit-label {{
    display: block; margin: 14px 0 6px 0; font-weight: 600;
    font-size: 14.5px; color: #333;
  }}
  .submit {{
    display: block; margin: 32px 0; padding: 14px 28px;
    background: #2563eb; color: white; border: none; border-radius: 6px;
    font-size: 16px; cursor: pointer;
  }}
  .intro {{
    background: #e8f5e9; padding: 16px; border-radius: 8px; margin: 16px 0;
    font-size: 14px;
  }}
  .stats {{
    display: flex; gap: 16px; font-size: 13px; color: #666; margin: 8px 0;
  }}
  .stats span {{ background: white; padding: 4px 10px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>うみやまAI Alignment Trial — {tag}</h1>
<div class="stats">
  <span>📋 質問数: {n_questions}</span>
  <span>🤖 モデル: {model}</span>
  <span>📅 実行: {ts}</span>
</div>
<div class="intro">
  <p>各質問とその AI 応答を確認、<b>直接書き換えて理想形に修正</b>してください。
     最下部の「サーバに送信」で取り込みます。</p>
  <ul>
    <li><b>質問</b>: 想定質問を直接 edit (= 質問の聞き方が違う、現場感不足 etc. を直す)</li>
    <li><b>AI 応答</b>: 応答を直接 edit (= 海山の理想の応答に直接書き換え)</li>
    <li><b>判定</b>: 採用 (= 修正不要) / 修正 (= 書き換えた) / 却下 (= この質問自体不要)</li>
  </ul>
  <p>所要時間: <b>1.5-2.5 時間</b> (1 件あたり 1-1.5 分)</p>
</div>
<form id="trialForm">
"""

HTML_QUESTION = """<h2>{idx}. {qid}<span class="meta">{role} / {category}</span></h2>

<label class="edit-label">📝 想定質問 (= 違和感あれば直接書き換え)</label>
<textarea class="editable q-box" name="{qid}__question">{scenario_raw}</textarea>

<div class="axes">
  <strong>期待される軸 (= 参考、AI 応答にこれらが踏まれてるか):</strong>
  <ul>{axes_html}</ul>
</div>

<label class="edit-label">🤖 AI 応答 (= 直接書き換えて海山の理想形に)</label>
<textarea class="editable r-box" name="{qid}__response">{response_raw}</textarea>

<div class="verdict">
  <strong>判定:</strong>
  <label><input type="radio" name="{qid}__verdict" value="ok" required> ✅ 採用 (= AI 応答そのまま OK)</label>
  <label><input type="radio" name="{qid}__verdict" value="fix"> ✏️ 修正 (= 書き換えた)</label>
  <label><input type="radio" name="{qid}__verdict" value="reject"> ❌ 却下 (= この質問自体不要)</label>
</div>
"""

HTML_FOOT = """
<div style="display:flex; gap:12px; flex-wrap:wrap;">
  <button type="button" class="submit" onclick="submitToServer()">🚀 サーバに送信して即取込</button>
  <button type="button" class="submit" style="background:#666;" onclick="exportReview()">📥 JSON ダウンロード (バックアップ用)</button>
</div>
<div id="result" style="margin-top:16px; padding:12px; background:#fff8e1; border-radius:6px; display:none; white-space:pre-wrap;"></div>
</form>

<script>
function collectFormData() {
  const form = document.getElementById('trialForm');
  const data = {};
  const fd = new FormData(form);
  for (const [k, v] of fd.entries()) data[k] = v;
  return data;
}

function exportReview() {
  const data = collectFormData();
  const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'alignment_review__' + (new Date()).toISOString().replace(/[:.]/g, '-') + '.json';
  a.click();
}

function submitToServer() {
  const data = collectFormData();
  const params = new URLSearchParams(window.location.search);
  const token = params.get('token');
  if (!token) {
    alert('URL に token=... が含まれていません');
    return;
  }
  // path から run_id を抽出 (= /alignment-trial/<run_id>)
  const m = window.location.pathname.match(/alignment-trial\\/([^/]+)/);
  if (!m) {
    alert('run_id を URL から抽出できませんでした');
    return;
  }
  const runId = m[1];
  const resultDiv = document.getElementById('result');
  resultDiv.style.display = 'block';
  resultDiv.textContent = '送信中…';
  fetch('/alignment-trial/' + runId + '/review?token=' + encodeURIComponent(token), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data),
  })
    .then(r => r.json())
    .then(j => {
      resultDiv.textContent = '✅ 送信完了\\n' + JSON.stringify(j, null, 2);
    })
    .catch(e => {
      resultDiv.textContent = '❌ 送信失敗: ' + e.message;
    });
}
</script>
</body>
</html>
"""


def generate_html(results: list[dict], tag: str = "v1") -> str:
    """results (= run_trial 戻り値) を HTML レポートに。"""
    if not results:
        return "<!doctype html><html><body><h1>no results</h1></body></html>"

    model = results[0].get("model", "?")
    ts = results[0].get("ts", "?")
    parts = [HTML_HEAD.format(
        tag=html.escape(tag),
        n_questions=len(results),
        model=html.escape(model),
        ts=html.escape(ts),
    )]
    for idx, r in enumerate(results, start=1):
        axes_html = "".join(
            f"<li>{html.escape(a)}</li>" for a in r.get("expected_axes", [])
        )
        # ★2026-05-22: 軸スコア・コメント廃止、editable な question/response に
        parts.append(HTML_QUESTION.format(
            idx=idx,
            qid=html.escape(r["id"]),
            role=html.escape(r["role"]),
            category=html.escape(r["category"]),
            scenario_raw=html.escape(r["scenario"]),
            response_raw=html.escape(r.get("response", "(応答なし)")),
            axes_html=axes_html,
        ))
    parts.append(HTML_FOOT)
    return "".join(parts)


# ─── run の保存 ───────────────────────────────
def save_run(results: list[dict], run_id: str, tag: str = "v1") -> Path:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RUNS_DIR / f"{run_id}.json"
    html_path = RUNS_DIR / f"{run_id}.html"
    json_path.write_text(
        json.dumps({"run_id": run_id, "tag": tag, "results": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    html_path.write_text(generate_html(results, tag=tag), encoding="utf-8")
    logger.info(f"saved run: {json_path.name} + {html_path.name}")
    return json_path


def load_run(run_id: str) -> dict:
    p = RUNS_DIR / f"{run_id}.json"
    if not p.exists():
        raise SystemExit(f"run not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


# ─── レビュー結果取り込み ────────────────────
def ingest_review(review_path: Path, run_id: str) -> dict:
    """HTML form から export された review.json を run JSON に統合。

    review.json format: {
      "store-001__verdict": "ok",
      "store-001__axis0": "4",
      "store-001__axis1": "3",
      "store-001__comment": "...",
      ...
    }
    """
    if not review_path.exists():
        raise SystemExit(f"review not found: {review_path}")
    raw = json.loads(review_path.read_text(encoding="utf-8"))

    run = load_run(run_id)
    results = run["results"]

    # qid 別にレビュー結果を整理
    by_qid: dict[str, dict] = {}
    for k, v in raw.items():
        m = re.match(r"^(\S+?)__(\w+)$", k)
        if not m:
            continue
        qid = m.group(1)
        field = m.group(2)
        by_qid.setdefault(qid, {})[field] = v

    # results に統合 (★2026-05-22: 新 schema = edited_question / edited_response / verdict)
    n_question_edited = 0
    n_response_edited = 0
    for r in results:
        review = by_qid.get(r["id"])
        if not review:
            continue
        r["verdict"] = review.get("verdict", "")

        # 修正された質問 (= textarea で書き換えられたもの) を保存
        edited_q = review.get("question", "").strip()
        if edited_q and edited_q != (r.get("scenario") or "").strip():
            r["edited_question"] = edited_q
            n_question_edited += 1

        # 修正された応答 (= textarea で書き換えられたもの) を保存
        edited_r = review.get("response", "").strip()
        if edited_r and edited_r != (r.get("response") or "").strip():
            r["edited_response"] = edited_r
            n_response_edited += 1

    # 保存
    out_path = RUNS_DIR / f"{run_id}_reviewed.json"
    out_path.write_text(json.dumps(run, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    # サマリ
    verdicts = {"ok": 0, "fix": 0, "reject": 0, "none": 0}
    for r in results:
        v = r.get("verdict", "")
        verdicts[v if v in verdicts else "none"] += 1
    logger.info(f"ingested {sum(verdicts.values())} reviews")
    logger.info(f"verdicts: ok={verdicts['ok']}, fix={verdicts['fix']}, "
                f"reject={verdicts['reject']}, none={verdicts['none']}")
    logger.info(f"edited: question {n_question_edited}, response {n_response_edited}")
    return {
        "verdicts": verdicts,
        "n_question_edited": n_question_edited,
        "n_response_edited": n_response_edited,
        "reviewed_path": str(out_path),
    }


# ─── diff (run1 vs run2) ──────────────────────
def diff_runs(run_id_a: str, run_id_b: str) -> dict:
    """2 つの run を比較して改善 trend を集計。"""
    a = load_run(run_id_a)["results"]
    b = load_run(run_id_b)["results"]
    by_qid_a = {r["id"]: r for r in a}
    by_qid_b = {r["id"]: r for r in b}

    common_qids = set(by_qid_a) & set(by_qid_b)
    pairs = []
    for qid in sorted(common_qids):
        ra = by_qid_a[qid]
        rb = by_qid_b[qid]
        pairs.append({
            "id": qid,
            "role": ra.get("role"),
            "category": ra.get("category"),
            "response_a": ra.get("response", "")[:300],
            "response_b": rb.get("response", "")[:300],
            "len_a": len(ra.get("response", "")),
            "len_b": len(rb.get("response", "")),
            "changed": ra.get("response", "")[:200] != rb.get("response", "")[:200],
        })
    n_changed = sum(1 for p in pairs if p["changed"])
    return {
        "run_a": run_id_a,
        "run_b": run_id_b,
        "n_common": len(pairs),
        "n_changed": n_changed,
        "unchanged_rate": round(1 - n_changed / max(1, len(pairs)), 3),
        "pairs": pairs,
    }


# ─── CLI ──────────────────────────────────────
async def _run_trial_cli(args):
    questions = parse_questions()
    logger.info(f"loaded {len(questions)} questions")
    logger.info(f"by role: {count_by_role(questions)}")
    if args.dry_run:
        print(json.dumps({"n_questions": len(questions),
                          "by_role": count_by_role(questions)},
                         ensure_ascii=False, indent=2))
        return
    results = await run_trial(questions, model=args.model)
    save_run(results, args.run_id, tag=args.tag)


async def _rerun_cli(args):
    base_run = load_run(args.base)
    questions = [{k: r[k] for k in ("id", "role", "category", "scenario",
                                     "expected_axes")} for r in base_run["results"]]
    logger.info(f"rerunning {len(questions)} questions from base {args.base}")
    results = await run_trial(questions, model=args.model)
    save_run(results, args.run_id, tag=f"{args.tag} (rerun of {args.base})")


def main() -> int:
    import asyncio
    ap = argparse.ArgumentParser()
    ap.add_argument("--generate", action="store_true",
                    help="質問テンプレ parse のみ (= 動作確認)")
    ap.add_argument("--run", action="store_true", help="100 件を bot に流す")
    ap.add_argument("--rerun", action="store_true", help="既存 run と同質問で再応答")
    ap.add_argument("--regenerate-html", action="store_true",
                    help="既存 run の JSON から HTML だけ再生成 (= rerun せずに UI 更新)")
    ap.add_argument("--base", help="--rerun の base run-id")
    ap.add_argument("--run-id", default=datetime.now(JST).strftime("%Y-%m-%d_%H%M"))
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--model", default="smart")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ingest-review", dest="ingest_review",
                    help="HTML form export の review.json を取り込み")
    ap.add_argument("--diff", nargs=2, metavar=("RUN_A", "RUN_B"),
                    help="2 つの run-id を比較")
    args = ap.parse_args()

    if args.generate:
        questions = parse_questions()
        print(json.dumps({"n_questions": len(questions),
                          "by_role": count_by_role(questions)},
                         ensure_ascii=False, indent=2))
        return 0

    if args.run:
        asyncio.run(_run_trial_cli(args))
        return 0

    if args.rerun:
        if not args.base:
            print("--rerun には --base が必要")
            return 1
        asyncio.run(_rerun_cli(args))
        return 0

    if args.regenerate_html:
        # rerun せずに HTML だけ再生成 (= CSS / JS の更新を反映する用)
        run = load_run(args.run_id)
        results = run["results"]
        tag = run.get("tag", "v1")
        html_path = RUNS_DIR / f"{args.run_id}.html"
        html_path.write_text(generate_html(results, tag=tag), encoding="utf-8")
        print(f"regenerated HTML: {html_path} ({len(results)} questions, tag={tag})")
        return 0

    if args.ingest_review:
        summary = ingest_review(Path(args.ingest_review), args.run_id)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.diff:
        report = diff_runs(args.diff[0], args.diff[1])
        # 短縮表示
        print(json.dumps({
            "run_a": report["run_a"],
            "run_b": report["run_b"],
            "n_common": report["n_common"],
            "n_changed": report["n_changed"],
            "unchanged_rate": report["unchanged_rate"],
        }, ensure_ascii=False, indent=2))
        out_path = RUNS_DIR / f"diff_{args.diff[0]}_vs_{args.diff[1]}.json"
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"detailed: {out_path}")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
