#!/usr/bin/env python3
"""
clone_style_regression.py — うみやまAI 応答スタイルの夜間 regression test

毎晩 cron で:
  1. response-bank.md (30 質問の海山本人記入参考回答) を gold set として読む
  2. bot (docker exec line-bot 経由 clone_respond_public) に同じ 30 質問を投げる
  3. 3 軸で採点:
     A. cosine 類似度 (embedding)
     B. LLM-as-judge (smart-gpt 採点、海山らしさ 1-10)
     C. style 違反 regex check (ドヤ語 / 概念語 / うん連発 / 等)
  4. スコアを保存し、前日比劣化 (> 0.15 ポイント低下 or judge < 5 or 違反 > 3) で LINE Push

cron: 03:30 JST 毎日 (auto_improve 03:00 の直後)
  bash scripts/clone_cron.sh regression

出力:
  data/brain/clone_improve/regression/YYYY-MM-DD.json (30 Q 全結果 + サマリ)
  data/brain/clone_improve/regression.log.jsonl       (異常ログ蓄積)

exit code:
  0 — 全 OK
  1 — 劣化検出あり (Push 済)
  2 — bot 不応答 (BOT_UNAVAILABLE)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clone_improve_lib import (
    call_llm, line_push, line_push_digest, append_jsonl, ensure_dirs,
    IMPROVE_DIR, WIKI_DIR, JST, pick_cross_family_judge,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clone_style_regression")

REGRESSION_DIR = IMPROVE_DIR / "regression"
LOG_PATH = IMPROVE_DIR / "regression.log.jsonl"

# ★2026-07-02 監査 P2 (eval-model-vs-prod-model-mismatch): 評価対象を本番 clone と同じモデルに
# 追随 (旧: smart=Opus ハードコード。本番は CLONE_PUBLIC_PROD_MODEL=smart-gpt で不一致 =
# 品質ゲートの妥当性が無い上に Opus 分コストが割高)。judge は self-eval ループ回避のため
# 常に「bot と別系列」: 判定は clone_improve_lib.pick_cross_family_judge に一元化
# (★2026-07-05 監査: hallucination/response_quality と重複していた inline 判定を chokepoint 化。挙動不変)。
# 注: モデル切替直後の数夜は cosine/judge の分布が変わり baseline 比で FAIL がシフトし得る (想定内)。
BOT_MODEL = os.getenv("CLONE_PUBLIC_PROD_MODEL", "smart")
JUDGE_MODEL = pick_cross_family_judge(BOT_MODEL)

def _resolve_response_bank() -> Path:
    """response-bank.md を探す。docker 内 (WIKI_DIR=/app/data/brain/wiki) 優先、
    なければ repo root から (MacBook smoke test 用 fallback)。"""
    p = WIKI_DIR / "style" / "response-bank.md"
    if p.exists():
        return p
    # repo root fallback (MacBook で smoke test 等)
    repo_root = Path(__file__).resolve().parent.parent
    p2 = repo_root / "data" / "brain" / "wiki" / "style" / "response-bank.md"
    return p2

RESPONSE_BANK = _resolve_response_bank()

# 異常検知の閾値 (前日比 / 絶対値)
THRESHOLD_COSINE_DROP = 0.15        # 前日比 0.15 ポイント以上低下で異常
THRESHOLD_JUDGE_ABS = 5             # LLM-as-judge スコア絶対 < 5 で異常
THRESHOLD_VIOLATIONS = 3            # style 違反 3 件以上で異常
THRESHOLD_TOTAL_DROP = 0.10         # 総合スコア前日比 0.10 ポイント低下で異常

# Docker bin 解決 (sales_accuracy_check と同じパターン)
def _resolve_docker_bin() -> str:
    import shutil
    found = shutil.which("docker")
    if found:
        return found
    for cand in (
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
        "/Applications/Docker.app/Contents/Resources/bin/docker",
    ):
        if os.path.exists(cand):
            return cand
    return "docker"

_DOCKER_BIN = _resolve_docker_bin()


# ─── style 違反 regex 集 (各 style wiki から抽出) ──────────────────
STYLE_VIOLATIONS = [
    # style-aizuchi.md NG
    (r"^うん(?:[。、\s]|$)", "「うん」単独使用 (style-aizuchi NG)"),
    (r"へえ[。、\s]", "「へえ」使用 (style-aizuchi NG)"),
    (r"了解です", "「了解です」丁寧体すぎ"),
    # style-no-bragging.md ドヤ語
    (r"私が[、,]?\s*OWNDAYS|私が[、,]?\s*会社", "経歴自慢「私が〜」"),
    (r"年商[\d,]+億|3,?000\s*億の借金", "数字マウント"),
    (r"フランス留学(?:時代|の時|から).*[、。]\s*自分", "学歴・経歴ドヤ"),
    # style-depth-as-undercurrent.md 概念語回避 / 直接引用 NG
    (r"本体な気がする|本質的に[、,]|核は[、,]", "概念語 (本体/本質/核) 使用"),
    (r"自分も(?:同じ|借金時代|フランスで|20\s*代)", "深層の直接引用"),
    (r"3\s*月のライオン.*?[、。].*?自分も|借金時代に\s*10\s*年", "個人体験の直接引用"),
    # style-first-person-minimal.md 一人称過剰
    (r"私が.{0,30}私の.{0,30}私[はが]", "「私」3 回以上"),
    # style-personal-flavor.md 出典ドヤ顔
    (r"[「『]([^」』]{5,40})[」』]\s*(?:って|と)\s*(?:言って|書いて)", "ドヤ顔引用 (「○○って○○が言ってた」)"),
    (r"(?:島田八段|Hard Things|Ben Horowitz|林田先生).*?(?:が言|と書)", "明示的出典引用"),
    # style-soften-cliche.md カッコ良すぎる断定
    (r"^楽になる日は来ない、慣れるだけ", "断定 (コーティング無し)"),
    (r"結局\s*[^。、]{5,30}\s*に尽きる", "「結局〜に尽きる」型決め台詞"),
    # CLONE_PUBLIC_PROMPT 3.5「絶対厳守: markdown 太字は LINE で表示されない」
    # ★2026-07-05 監査: prompt が絶対厳守と言う唯一の機械検出可能ルールが regression で
    # 未監視だった (few-shot 側に **太字** 混入が実在 = 違反再発リスク高) → regex 追加
    (r"\*\*[^*\n]+\*\*", "markdown 太字使用 (3.5 絶対厳守違反、LINE で表示されない)"),
]


def detect_style_violations(reply: str) -> list[dict]:
    """応答テキストに style 違反があるか regex で検出。"""
    violations = []
    for pattern, label in STYLE_VIOLATIONS:
        m = re.search(pattern, reply, re.MULTILINE)
        if m:
            violations.append({
                "pattern": label,
                "match": m.group(0)[:50],
            })
    return violations


# ─── response-bank.md parse ────────────────────────────────────────
def parse_response_bank() -> list[dict]:
    """response-bank.md から全質問 (★Q0-N の XS 挨拶ミラーリング含む) と gold 回答を抽出。

    フォーマット (実例):
      ### Q1: 最近、休日は何してる?
      **想定スケール**: S

      > 小さい子供が二人いるから子育てかな。送り迎えとか。
      > 少しまとまって休みが取れそうな時は旅行とかに行くことが多い。
    """
    if not RESPONSE_BANK.exists():
        logger.error(f"gold set 未発見: {RESPONSE_BANK}")
        return []
    content = RESPONSE_BANK.read_text(encoding="utf-8")
    questions = []
    # ### QN: <question>\n**想定スケール**: <S/M/L>\n\n> <ans line 1>\n> <ans line 2>\n
    # ★2026-06-07 評価: 旧 (Q\d+) は XS 挨拶ミラーリングの Q0-1〜Q0-4 を silent drop していた
    #   (= ai_smell の急所が regression 網から漏れ)。(Q[\d-]+) で Q0-N も拾う ("Quarterly:" 等は誤爆せず)。
    pat = re.compile(
        r"^###\s+(Q[\d-]+):\s*([^\n]+?)\n(?:\*\*想定スケール\*\*:\s*([^\n]+)\n)?\s*\n((?:>\s*[^\n]*\n?)+)",
        re.MULTILINE,
    )
    for m in pat.finditer(content):
        qid = m.group(1).strip()
        question = m.group(2).strip()
        scale = (m.group(3) or "S").strip()
        gold_block = m.group(4)
        gold = "\n".join(
            line.lstrip("> ").rstrip() for line in gold_block.splitlines() if line.strip().startswith(">")
        ).strip()
        if gold and gold != "*(未記入)*":
            questions.append({
                "id": qid,
                "question": question,
                "scale": scale,
                "gold": gold,
            })
    logger.info(f"gold set parsed: {len(questions)} Q")
    return questions


# ─── bot 応答取得 (docker exec 経由) ───────────────────────────────
async def query_bot(query: str, model: str = BOT_MODEL) -> tuple[str, dict]:
    """clone_respond_public を docker exec で呼ぶ。"""
    from clone_improve_lib import eval_turn_guard
    eval_turn_guard()  # ★2026-06-11 コスト保護 (EVAL_MAX_BOT_TURNS 超で停止)
    cmd = [
        _DOCKER_BIN, "exec", "line-bot", "python3", "-c",
        (
            "import os, asyncio, httpx, json\n"
            "from brain_wiki import BrainWiki\n"
            "async def main():\n"
            "    async with httpx.AsyncClient(timeout=180) as h:\n"
            "        bw = BrainWiki(http=h, "
            "litellm_url=os.getenv('LITELLM_URL','http://litellm:4000'), "
            "litellm_key=os.getenv('LITELLM_MASTER_KEY',''))\n"
            f"        r = await bw.clone_respond_public(query={query!r}, history=[], model={model!r})\n"
            "        print(json.dumps({'reply': r}, ensure_ascii=False))\n"
            "asyncio.run(main())"
        ),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=200)
    except subprocess.TimeoutExpired:
        return "", {"kind": "timeout"}
    except FileNotFoundError:
        return "", {"kind": "docker_missing"}
    except Exception as e:
        return "", {"kind": "subprocess_error", "detail": str(e)[:200]}

    if proc.returncode != 0:
        stderr = (proc.stderr or "")[:300]
        kind = "container_not_running" if "No such container" in stderr or "is not running" in stderr else "exec_error"
        return "", {"kind": kind, "detail": stderr}

    out = (proc.stdout or "").strip()
    if not out:
        return "", {"kind": "empty_reply"}
    # 最終 JSON 行を取る
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
                return d.get("reply", ""), {}
            except Exception:
                continue
    return "", {"kind": "parse_error", "detail": out[:200]}


# ─── embedding cosine 類似度 ───────────────────────────────────────
async def embed(text: str, http: httpx.AsyncClient) -> Optional[list[float]]:
    """LiteLLM 経由で embedding を取得。失敗時 None。"""
    url = os.getenv("LITELLM_URL", "http://litellm:4000")
    key = os.getenv("LITELLM_MASTER_KEY", "")
    try:
        resp = await http.post(
            f"{url}/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "text-embedding-3-small", "input": text[:4000]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except Exception as e:
        logger.warning(f"embed failed: {e}")
        return None


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ─── LLM-as-judge ────────────────────────────────────────────────
JUDGE_PROMPT = """以下、海山丈司本人が書いた参考回答 (gold) と、うみやまAI が実際に返した応答 (candidate)。

質問: {question}

【gold (海山本人)】
{gold}

【candidate (bot 応答)】
{candidate}

海山丈司テイストの style 軸:
- 一人称ミニマル (「私」は滅多に使わない)
- 自慢しない (経歴 / 学歴 / 数字でマウントしない)
- 直接引用しない (「自分も〜だった」「フランスで〜」NG)
- 概念語回避 (「本体」「本質」「核」「構造的に」NG)
- 断定弱化 (「〜かもね」「〜と思う」「〜よね」)
- 共感先行・並走 (「気持ちわかるよ」「あるよね」)
- ニヒルなコーティング (「— 知らんけど」)
- 業務系は数字 3 セット、雑談・趣味は 1 個 + 一言 (徐々に掘る)

candidate が海山テイストとして どれくらい再現できているか、1-10 で採点してください。

★出力フォーマット (JSON only)
{{"score": <1-10>, "reason": "<60 字以内、何が良かった/何がズレた か>"}}
"""

async def llm_judge(question: str, gold: str, candidate: str) -> dict:
    prompt = JUDGE_PROMPT.format(question=question, gold=gold[:500], candidate=candidate[:1500])
    try:
        out = await call_llm(prompt, model=JUDGE_MODEL, max_tokens=300, temperature=0.0)
        # JSON 抽出
        m = re.search(r'\{[^{}]*"score"[^{}]*\}', out, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception as e:
        logger.warning(f"judge failed: {e}")
    return {"score": 0, "reason": "judge LLM error"}


async def judge_healthcheck() -> bool:
    """judge model (BOT が GPT 系なら smart=Opus) が生きているか 1 発で確認。
    ★2026-07-05: judge が全滅すると全 Q が judge=0 → 全 FAIL の誤報になり、かつ bot を全問
    (smart-gpt, ~22k context) 無駄撃ちする。先に 1 回だけ叩いて死活判定し、死んでいれば本番
    ループを skip (誤報防止 + コスト保護)。実測 2026-07-03/05 に Opus(smart) 全滅で全FAIL誤報。"""
    probe = await llm_judge(
        question="好きな色は?",
        gold="青かな。空の色。",
        candidate="青が好き。空みたいで落ち着くよね。",
    )
    ok = float(probe.get("score", 0) or 0) > 0 and probe.get("reason") != "judge LLM error"
    logger.info(f"judge healthcheck ({JUDGE_MODEL}): {'OK' if ok else 'DEAD'} ({probe})")
    return ok


# ─── 前日 metrics で比較 ──────────────────────────────────────────
def load_prev(date_str: str) -> Optional[dict]:
    """前日の regression レポートを読む。"""
    prev_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    p = REGRESSION_DIR / f"{prev_date}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


# ─── main ────────────────────────────────────────────────────────
async def main():
    ensure_dirs()
    REGRESSION_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now(JST).strftime("%Y-%m-%d")
    logger.info(f"=== clone_style_regression {today} ===")

    questions = parse_response_bank()
    if not questions:
        logger.error("gold set が読めない、abort")
        return 1

    # ★2026-07-05 judge 死活チェック (Opus down → 全 FAIL 誤報 + bot 全問無駄撃ちを防ぐ)。
    # judge が死んでいれば bot ループに入る前に skip = smart-gpt の 34 問分 (~750k tok) を節約。
    if not await judge_healthcheck():
        msg = (
            f"⚠️ style regression 計測不能 ({today})\n"
            f"judge ({JUDGE_MODEL}) が応答不能 = {JUDGE_MODEL} down の疑い。\n"
            f"({JUDGE_MODEL}=smart なら Opus 4.8。本番クローンの一部/ wiki compile も smart 依存)\n"
            f"要確認: docker logs litellm --tail 50 | grep -i error /"
            f" .env の ANTHROPIC_API_KEY / docker compose restart litellm\n"
            f"regression 本体は skip (bot {len(questions)} 問の無駄撃ち回避)。"
        )
        logger.error(msg)
        try:
            line_push_digest(msg, "文体回帰")
        except Exception:
            pass
        append_jsonl(LOG_PATH, {"date": today, "status": "judge_unavailable",
                                "judge_model": JUDGE_MODEL, "n_questions": len(questions)})
        return 2

    # 環境変数チェック (docker 内 prefer / localhost fallback)
    # bot 不応答カウンタ
    bot_unavailable_count = 0

    results = []
    async with httpx.AsyncClient(timeout=60) as http:
        for q in questions:
            qid = q["id"]
            question = q["question"]
            gold = q["gold"]

            # 1. bot 応答取得
            logger.info(f"[{qid}] querying bot: {question[:40]}...")
            reply, err = await query_bot(question, model=BOT_MODEL)
            if err:
                logger.warning(f"[{qid}] bot error: {err}")
                bot_unavailable_count += 1
                results.append({
                    "id": qid, "question": question, "scale": q["scale"],
                    "bot_reply": "", "error": err,
                    "cosine": 0.0, "judge_score": 0, "judge_reason": "", "violations": [],
                    "verdict": "BOT_UNAVAILABLE",
                })
                continue

            # 2. cosine 類似度
            gold_emb = await embed(gold, http)
            reply_emb = await embed(reply, http)
            cos = cosine(gold_emb, reply_emb) if gold_emb and reply_emb else 0.0

            # 3. LLM-as-judge
            judge = await llm_judge(question, gold, reply)
            judge_score = float(judge.get("score", 0) or 0)
            judge_reason = judge.get("reason", "")

            # 4. style 違反 regex
            violations = detect_style_violations(reply)

            # 5. 総合スコア
            total = (cos * 0.4) + (judge_score / 10.0 * 0.5) + max(0.0, (1.0 - len(violations) * 0.2)) * 0.1

            # verdict
            if judge_score < THRESHOLD_JUDGE_ABS or len(violations) >= THRESHOLD_VIOLATIONS:
                verdict = "FAIL"
            elif judge_score >= 7 and len(violations) == 0:
                verdict = "PASS"
            else:
                verdict = "WARN"

            results.append({
                "id": qid, "question": question, "scale": q["scale"],
                "bot_reply": reply[:800],
                "cosine": round(cos, 4),
                "judge_score": judge_score,
                "judge_reason": judge_reason,
                "violations": violations,
                "total": round(total, 4),
                "verdict": verdict,
            })
            logger.info(f"[{qid}] cos={cos:.3f} judge={judge_score:.1f} violations={len(violations)} verdict={verdict}")

    # 集計
    n = len(results)
    n_ok = sum(1 for r in results if r["verdict"] == "PASS")
    n_warn = sum(1 for r in results if r["verdict"] == "WARN")
    n_fail = sum(1 for r in results if r["verdict"] == "FAIL")
    n_bot_unavail = sum(1 for r in results if r.get("verdict") == "BOT_UNAVAILABLE")
    valid = [r for r in results if r.get("verdict") not in ("BOT_UNAVAILABLE",)]
    avg_cos = sum(r["cosine"] for r in valid) / max(1, len(valid))
    avg_judge = sum(r["judge_score"] for r in valid) / max(1, len(valid))
    avg_violations = sum(len(r["violations"]) for r in valid) / max(1, len(valid))
    avg_total = sum(r["total"] for r in valid) / max(1, len(valid)) if valid else 0

    summary = {
        "date": today,
        # ★2026-07-02 cross-check DA: モデル切替前後の trend を後から分離できるよう記録
        "bot_model": BOT_MODEL,
        "judge_model": JUDGE_MODEL,
        "n_questions": n,
        "n_pass": n_ok,
        "n_warn": n_warn,
        "n_fail": n_fail,
        "n_bot_unavailable": n_bot_unavail,
        "avg_cosine": round(avg_cos, 4),
        "avg_judge_score": round(avg_judge, 2),
        "avg_violations_per_q": round(avg_violations, 2),
        "avg_total_score": round(avg_total, 4),
    }
    logger.info(f"summary: {summary}")

    # 前日比 diff
    prev = load_prev(today)
    deltas = {}
    if prev:
        deltas = {
            "cosine": round(summary["avg_cosine"] - prev.get("avg_cosine", 0), 4),
            "judge": round(summary["avg_judge_score"] - prev.get("avg_judge_score", 0), 2),
            "total": round(summary["avg_total_score"] - prev.get("avg_total_score", 0), 4),
            "violations": round(summary["avg_violations_per_q"] - prev.get("avg_violations_per_q", 0), 2),
        }
        summary["deltas_vs_prev"] = deltas
        logger.info(f"deltas vs prev day: {deltas}")

    # 保存
    out = {"summary": summary, "questions": results}
    out_path = REGRESSION_DIR / f"{today}.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"wrote {out_path}")

    # 異常検知 + LINE Push
    alerts = []
    if n_bot_unavail >= n // 2:
        alerts.append(f"🚨 bot 不応答 {n_bot_unavail}/{n}、container 死亡疑い")
    if prev:
        if deltas.get("total", 0) <= -THRESHOLD_TOTAL_DROP:
            alerts.append(f"⚠️ 総合スコア前日比 {deltas['total']:+.3f} 低下")
        if deltas.get("cosine", 0) <= -THRESHOLD_COSINE_DROP:
            alerts.append(f"⚠️ cosine 平均が前日比 {deltas['cosine']:+.3f} 低下")
        if deltas.get("judge", 0) <= -1.5:
            alerts.append(f"⚠️ LLM-as-judge 平均が前日比 {deltas['judge']:+.2f} 低下")
    if n_fail >= 5:
        # FAIL の Q を 3 件まで列挙
        fails = [r for r in results if r["verdict"] == "FAIL"][:3]
        fails_desc = "\n".join(f"  - {r['id']}: judge={r['judge_score']:.1f} violations={len(r['violations'])}" for r in fails)
        alerts.append(f"⚠️ FAIL {n_fail} 件:\n{fails_desc}")

    if alerts:
        msg = f"📉 うみやまAI style regression ({today})\n" + "\n".join(alerts)
        msg += f"\n\nsummary: PASS {n_ok} / WARN {n_warn} / FAIL {n_fail}"
        msg += f"\n詳細: {out_path}"
        line_push_digest(msg, "文体回帰")
        append_jsonl(LOG_PATH, {**summary, "alerts": alerts})
        return 1
    else:
        # 健全、log だけ追記
        append_jsonl(LOG_PATH, summary)
        logger.info("all OK, no push")
        return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
