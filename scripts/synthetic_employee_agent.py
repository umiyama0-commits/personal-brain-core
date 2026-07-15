"""scripts/synthetic_employee_agent.py — 社員に扮した synthetic user による proactive QA + 改善

★2026-06-07 海山指示「社員に扮して仮想環境でシステムを利用し、改善点と改修を進め続ける
エージェント。ある程度修正は許可なく自律的にやってよい」
ADR: docs/decisions/2026-06-07-synthetic-employee-auto-remediation.md

ループ (5 段):
  persona → query 生成 (smart-gpt) → 仮想環境で bot 応答 (query_bot, 非永続) →
  診断 (smart-gpt) → dedup → routing → report

仮想環境 (= 汚染ゼロ):
  query_bot は docker exec 内で clone_respond_public を user_id 無し・history=[] で呼ぶ。
  clone_respond_public は read-only (= clone_history/clone_memory を書かない)。会話を一切
  persist せず、clone_learning/quality scan を汚さない。

★自律境界 (cross-check 3種 2026-06-07 で確定):
  うみやまAI clone を「安全かつ有効」に自律直接編集する手段は無い (事実は要検証 / prompt・
  retrieval は §1.15 / 旧 patches.json keyword は dead-write)。よって:
  - 既定 = **propose-only**: 全カテゴリを queue に提案、適用は海山が /admin/review で 1-click。
  - 唯一の自律レバー (Phase1b、SYNTHETIC_AGENT_AUTOFIX=1 で有効) =
    **drive_search_aliases.json への findability alias 候補の自律記録のみ** (事実/prompt/code 不介入)。
    ★cross-check 2026-06-07: rerank は Gemini 失敗/候補≤top_n 等で意味判定を bypass する経路があり
    「最終フィルタ」にならない。未検証 alias を即座に検索へ効かせると誤リンクを再生産しうる。
    よって **verify-before-activate**: keyword_miss の「確実な別表記」を **enabled=False (未承認) で記録** し、
    海山が `--approve <term>` で enabled=True にして初めて search_drive_semantic が検索に使う。
    日次 cap (MAX_AUTOFIX) + 全件 audit ログ + 海山へ LINE 通知 (承認 1 コマンド)。

queue 先 (= §1.15 境界で振分け):
  - keyword (alias 化できない分) / wiki_content (事実) → clone_learning.add_manual_entry
  - prompt / retrieval / code (高リスク)              → services.system_issues.add_entry
  いずれも content-hash dedup で重複登録を防ぐ (7 日 TTL)。

安全:
  - chromadb reindex は呼ばない (§1.5)。MAX_QUERIES で cost cap、MAX_AUTOFIX で自律追記 cap。
  - bot=smart(Opus) / 生成・診断=smart-gpt(GPT-5.4) 系列分離。同一 model なら起動中止。
  - gate: SYNTHETIC_AGENT_CRON=1 (.env) で初めて cron 起動。kill-switch SYNTHETIC_AGENT_ENABLED=0。

usage:
  python3 scripts/synthetic_employee_agent.py --dry-run      # query 生成だけ (bot 投げない)
  python3 scripts/synthetic_employee_agent.py --sample 2     # persona 2 件だけ
  python3 scripts/synthetic_employee_agent.py                # full run (cron 想定)
  python3 scripts/synthetic_employee_agent.py --no-push      # LINE push 抑止
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("synthetic_employee_agent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

JST = timezone(timedelta(hours=9))
APP_ROOT = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
SYN_DIR = APP_ROOT / "data" / "brain" / "synthetic_agent"
SEEN_SIGS_PATH = SYN_DIR / "seen_sigs.json"
SEEN_TTL_DAYS = 7
# Phase1b 自律レバーの唯一の write 先 (= services/gemini_query.py が読む findability alias)
ALIASES_PATH = APP_ROOT / "data" / "brain" / "drive_search_aliases.json"

# ─── config (env) ────────────────────────────────────────────────────
ENABLED = os.getenv("SYNTHETIC_AGENT_ENABLED", "1") != "0"          # kill-switch
AUTOFIX = os.getenv("SYNTHETIC_AGENT_AUTOFIX", "0") == "1"          # Phase1b alias 自律追記 gate (既定 OFF)
MAX_QUERIES = int(os.getenv("SYNTHETIC_AGENT_MAX_QUERIES", "20"))   # cost 暴走防止
MAX_AUTOFIX = int(os.getenv("SYNTHETIC_AGENT_MAX_AUTOFIX", "3"))    # alias 自律追記/run 上限
QUERIES_PER_PERSONA = int(os.getenv("SYNTHETIC_AGENT_QPP", "4"))

# bot=smart(Opus) / 生成・診断=smart-gpt(GPT-5.4) 系列分離 (self-eval loop 遮断, §1.15)
BOT_MODEL = os.getenv("SYNTHETIC_AGENT_BOT_MODEL", "smart")
GEN_MODEL = os.getenv("SYNTHETIC_AGENT_GEN_MODEL", "smart-gpt")

# ─── 既存 helper 再利用 (= 二重実装回避) ──────────────────────────────
_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from clone_improve_lib import call_llm, append_jsonl, line_push, extract_json  # type: ignore  # noqa: E402
# drive_search_aliases.json への load→mutate→write を並行安全化 (cron 追記 ↔ 海山承認の lost update 防止)
from services._review_store import locked, write_text_atomic  # type: ignore  # noqa: E402

# query_bot (docker exec で live bot を叩く非永続 path) と queue は遅延 import


# ─── 社員ペルソナ ─────────────────────────────────────────────────────
PERSONAS: list[dict] = [
    {
        "id": "store_manager",
        "role": "店長",
        "context": "OWNDAYS の一店舗を任される店長。自店の数字と現場運営に関心。",
        "topics": ["自店の今月売上・予算・客数", "近隣店との比較", "販促・キャンペーン", "在庫・商品", "シフト・人員"],
    },
    {
        "id": "area_manager",
        "role": "エリアマネージャ",
        "context": "複数店舗を統括する AM。担当エリアの業績と不振店対策に関心。",
        "topics": ["担当エリアの売上・前年比", "店舗間ランキング", "不振店の要因", "エリア施策", "新店の進捗"],
    },
    {
        "id": "supervisor",
        "role": "SV (スーパーバイザー)",
        "context": "業態・リーグ横断で店舗を支援する SV。オペレーションと新店に関心。",
        "topics": ["業態・リーグ別の数字", "新店会議・出店PJ", "オペレーション標準", "店舗マスター", "固有の案件名・取引先"],
    },
    {
        "id": "hq_staff",
        "role": "本部スタッフ",
        "context": "本部で全社を見るスタッフ。全社/国別業績と制度に関心。",
        "topics": ["全社・国別の業績", "Monday Dash / KPI", "就業規則・副業・評価制度", "経営方針・戦略", "用語・略語の意味"],
    },
    {
        "id": "new_staff",
        "role": "新人スタッフ",
        "context": "入社して間もない新人。基本的な制度・用語・固有名詞を知りたい。",
        "topics": ["福利厚生・休暇の基本", "社内用語・略語", "システム・PJ の名前の意味", "理念・カルチャー", "困った時の連絡先"],
    },
]


# ─── 1. query 生成 ────────────────────────────────────────────────────
async def generate_queries(persona: dict, n: int) -> list[str]:
    """persona になりきった現実的な社員質問を n 件生成 (smart-gpt、bot と系列分離)。"""
    prompt = (
        f"あなたは OWNDAYS の社員「{persona['role']}」になりきって、社内 AI アシスタント"
        f"「うみやまAI」に投げる**現実的な質問**を {n} 個 考える。\n\n"
        f"立場: {persona['context']}\n"
        f"関心トピック: {', '.join(persona['topics'])}\n\n"
        "ルール:\n"
        "- 実際にその立場の社員が LINE で打ちそうな自然な口語 (= 短文・略語・話し言葉 OK)\n"
        "- 数字を聞く質問 / 制度を聞く質問 / 固有名詞の意味を聞く質問 / 雑談 を混ぜる\n"
        "- 多様に (= 同じ話題を繰り返さない)。たまに曖昧・打ち間違い・口語崩れも入れる\n"
        "- 1 質問 = 1 文 or 2 文程度\n\n"
        'output JSON: {"queries": ["質問1", "質問2", ...]}'
    )
    try:
        raw = await call_llm(prompt, model=GEN_MODEL, max_tokens=700, temperature=0.85)
        data = extract_json(raw)
        qs = data.get("queries", []) if isinstance(data, dict) else []
        return [str(q).strip() for q in qs if str(q).strip()][:n]
    except Exception as e:
        logger.warning(f"generate_queries failed for {persona['id']}: {e}")
        return []


# ─── 2-3. 仮想環境で bot 応答 (非永続) ─────────────────────────────────
async def run_bot(query: str) -> tuple[str, dict]:
    """live bot を docker exec 経由で叩く (= clone_respond_public は read-only、汚染ゼロ)。"""
    try:
        from clone_style_regression import query_bot  # type: ignore  # late import (docker 依存)
    except Exception as e:
        return "", {"kind": "import_error", "detail": str(e)[:200]}
    try:
        resp, err = await query_bot(query, model=BOT_MODEL)
        return resp, (err or {})
    except Exception as e:
        return "", {"kind": "query_bot_error", "detail": str(e)[:200]}


# ─── 4. 診断 (smart-gpt) ──────────────────────────────────────────────
async def diagnose(persona: dict, query: str, response: str) -> dict:
    """応答を診断し、改善 issue と fix category を返す。

    ★うみやまAI は「人間らしいコメントが第一、Drive 検索は2の手、未知は正直に」設計。
    短い/casual/「手元に無い」は多くが意図通りで issue ではない。過剰検知を避ける。
    """
    prompt = (
        "あなたは OWNDAYS の社内 AI「うみやまAI」の品質監査担当。\n"
        "ある社員の質問と bot 応答を見て、**明確な改善点があるか**だけを厳しく判定する。\n\n"
        "## うみやまAI の設計意図 (= これらは issue ではない、誤検知するな)\n"
        "- 第一の役割は人間らしいコメントバック。短い / casual / 共感だけ は **正常**。\n"
        "- データ retrieval は2の手。「手元の公開データに無い」と正直に言うのは **正常**。\n"
        "- 未知の固有名詞を別名へ断定変換せず正直に返すのは **正常** (むしろ良い)。\n\n"
        "## issue として拾うべきもの (= これらだけ)\n"
        "- hallucination: 事実や固有名詞を **でっち上げ / 勝手に別名へ断定変換** している\n"
        "- wrong_data: 数字・制度の説明が論理的に矛盾 / 明らかに不整合\n"
        "- keyword_miss: 実在しそうな話題に「全く分からない」と返し、検索語の言い換えで拾えたはず\n"
        "- evasive: 質問に対し何も答えず質問返し / たらい回しだけで終わる\n"
        "- style: AI 臭い定型文 / 海山の人格と乖離した不自然な口調\n\n"
        f"## 入力\n社員: {persona['role']} ({persona['context']})\n"
        f"質問: {query}\n"
        f"bot 応答: {response}\n\n"
        "## 出力 (JSON)\n"
        "has_issue が false なら他は空で良い。true の時のみ fix を書く。迷ったら false。\n"
        "fix_category (いずれも適用は海山レビュー): keyword/wiki_content/prompt/retrieval/code。\n"
        "★ keyword_miss の時だけ、**確実な別表記/略称がある場合に限り** alias を出す:\n"
        "  alias_term = 探していた固有名詞そのもの (query の綴り)、\n"
        "  alias_synonyms = その **確実な** 別表記/正式名/略称のみ (例: 略称↔正式名)。\n"
        "  連想・推測の同義語は入れない。確実なものが無ければ alias_synonyms は [] にする。\n"
        ' {"has_issue": bool, "issue_type": "hallucination|wrong_data|keyword_miss|evasive|style|none",\n'
        '  "severity": "low|medium|high", "root_cause": "簡潔に",\n'
        '  "fix_category": "keyword|wiki_content|prompt|retrieval|code|none",\n'
        '  "proposed_fix": "具体的に",\n'
        '  "alias_term": "(keyword_miss時) 探していた固有名詞", "alias_synonyms": ["確実な別表記のみ"],\n'
        '  "wiki_target": "knowledge/xxx.md 等 (wiki_content の時のみ、不明なら空)"}'
    )
    try:
        raw = await call_llm(prompt, model=GEN_MODEL, max_tokens=600, temperature=0.1)
        data = extract_json(raw)
        if not isinstance(data, dict):
            return {"has_issue": False}
        return data
    except Exception as e:
        logger.warning(f"diagnose failed: {e}")
        return {"has_issue": False, "error": str(e)[:120]}


# ─── Phase1b: 検証済み自律レバー (drive_search_aliases.json への findability alias 追記) ──
def _append_drive_alias(term: str, synonyms: list, from_query: str) -> bool:
    """term -> {aliases, enabled, src, added, from_query} を自律記録 (findability のみ、事実不介入)。

    ★cross-check 2026-06-07: 未検証 alias を即座に検索へ効かせると、rerank の bypass 経路で
    誤リンクを再生産しうる。よって **enabled=False (= 未承認) で記録** し、海山が approve_alias で
    enabled=True にして初めて検索に効く (verify-before-activate)。既に承認済の term は enabled 維持。
    既存 term は synonyms を統合 (dedup)。変化が無ければ False。
    """
    term = (term or "").strip()
    syns = [str(s).strip() for s in (synonyms or []) if str(s).strip() and str(s).strip() != term]
    if not term or len(term) < 2 or not syns:
        return False
    try:
        # ★load→mutate→write を同一 lock 下で直列化 (cron 追記中に承認が走っても lost update しない)
        with locked(ALIASES_PATH):
            data: dict = {}
            if ALIASES_PATH.exists():
                loaded = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            entry = data.get(term)
            existing = entry.get("aliases", []) if isinstance(entry, dict) else []
            was_enabled = bool(entry.get("enabled", False)) if isinstance(entry, dict) else False
            merged = list(dict.fromkeys([*existing, *syns]))  # 順序保持 dedup
            if set(merged) == set(existing):
                return False  # 既に全部入ってる
            data[term] = {
                "aliases": merged, "enabled": was_enabled, "src": "synthetic-agent",
                "added": datetime.now(JST).strftime("%Y-%m-%d"), "from_query": (from_query or "")[:100],
            }
            ALIASES_PATH.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(ALIASES_PATH, json.dumps(data, ensure_ascii=False, indent=2))
        logger.info(f"  alias 記録 (未承認): {term} → {syns}")
        return True
    except Exception as e:
        logger.warning(f"_append_drive_alias failed: {e}")
        return False


def approve_alias(term: str, enabled: bool = True) -> bool:
    """海山が alias を承認 (enabled=True) / 取消 (False)。承認済のみ検索に効く。"""
    term = (term or "").strip()
    try:
        # ★load→mutate→write を同一 lock 下で直列化 (承認中に cron 追記が走っても lost update しない)
        with locked(ALIASES_PATH):
            if not ALIASES_PATH.exists():
                return False
            data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or term not in data or not isinstance(data[term], dict):
                return False
            data[term]["enabled"] = bool(enabled)
            write_text_atomic(ALIASES_PATH, json.dumps(data, ensure_ascii=False, indent=2))
        logger.info(f"alias {'承認' if enabled else '取消'}: {term}")
        return True
    except Exception as e:
        logger.warning(f"approve_alias failed: {e}")
        return False


def list_aliases() -> dict:
    """alias 一覧を返す (pending / enabled 区別用)。"""
    try:
        if ALIASES_PATH.exists():
            data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


# ─── dedup (content-hash, 7 日 TTL) ───────────────────────────────────
def _sig(persona: dict, diag: dict) -> str:
    raw = f"{persona['id']}|{diag.get('issue_type')}|{str(diag.get('proposed_fix',''))[:80]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _load_seen_sigs() -> dict:
    try:
        if SEEN_SIGS_PATH.exists():
            data = json.loads(SEEN_SIGS_PATH.read_text(encoding="utf-8"))
            cutoff = (datetime.now(JST) - timedelta(days=SEEN_TTL_DAYS)).isoformat()
            return {k: v for k, v in data.items() if isinstance(v, str) and v >= cutoff}
    except Exception as e:
        logger.warning(f"_load_seen_sigs failed: {e}")
    return {}


def _save_seen_sigs(sigs: dict) -> None:
    try:
        SYN_DIR.mkdir(parents=True, exist_ok=True)
        SEEN_SIGS_PATH.write_text(json.dumps(sigs, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"_save_seen_sigs failed: {e}")


# ─── 5. routing ───────────────────────────────────────────────────────
def route(diag: dict, persona: dict, query: str, response: str, seen: dict, autofix_remaining: int = 0) -> dict:
    """診断結果を §1.15 境界で振分け。keyword_miss は (AUTOFIX 有効時) alias 自律追記、他は queue。"""
    cat = diag.get("fix_category", "none")
    sev = diag.get("severity", "low")
    base = {
        "persona": persona["id"], "query": query, "issue_type": diag.get("issue_type"),
        "severity": sev, "fix_category": cat, "root_cause": diag.get("root_cause", ""),
        "proposed_fix": diag.get("proposed_fix", ""),
    }
    if cat == "none":
        return {**base, "action": "logged"}

    # dedup (= 同義 issue を毎晩重複登録して /admin/review を埋めない)
    sig = _sig(persona, diag)
    if sig in seen:
        return {**base, "action": "deduped", "sig": sig}
    seen[sig] = datetime.now(JST).isoformat()

    # keyword: 確実な alias があり AUTOFIX 有効 & 残枠ありなら自律追記、なければ queue
    if cat == "keyword":
        term = str(diag.get("alias_term", "")).strip()
        syns = [s for s in (diag.get("alias_synonyms") or []) if str(s).strip()]
        if AUTOFIX and term and syns and autofix_remaining > 0 and _append_drive_alias(term, syns, query):
            # ★未承認 (enabled=False) で記録 = 提案。海山 approve まで検索に効かない。
            return {**base, "action": "proposed_alias", "term": term, "synonyms": syns, "sig": sig}
        try:
            from clone_learning import add_manual_entry  # type: ignore
            eid = add_manual_entry(
                insight=f"[synthetic:{persona['id']}] keyword不足: {query} → {diag.get('root_cause','')}",
                proposed_wiki_patch=f"検索語/同義語の追加候補: {diag.get('proposed_fix','')}"
                                    + (f" / alias 候補 {term}→{syns}" if term and syns else ""),
                reviewer="synthetic-agent",
            )
            return {**base, "action": "queued_keyword", "entry": eid, "sig": sig}
        except Exception as e:
            return {**base, "action": "queue_failed", "detail": str(e)[:120]}

    # 事実 wiki content → clone_learning queue (人間検証)
    if cat == "wiki_content":
        try:
            from clone_learning import add_manual_entry  # type: ignore
            eid = add_manual_entry(
                insight=f"[synthetic:{persona['id']}] {diag.get('issue_type')}: {query}",
                proposed_wiki_patch=(
                    f"対象: {diag.get('wiki_target','(未定)')}\n根拠質問: {query}\n"
                    f"bot応答: {response[:300]}\n提案: {diag.get('proposed_fix','')}\n"
                    "※事実の正否は海山が検証してから適用"
                ),
                reviewer="synthetic-agent",
            )
            return {**base, "action": "queued_wiki_content", "entry": eid, "sig": sig}
        except Exception as e:
            return {**base, "action": "queue_failed", "detail": str(e)[:120]}

    # 高リスク = prompt / retrieval / code → system_issues queue (§1.15 a/b、自己適用しない)
    if cat in ("prompt", "retrieval", "code"):
        try:
            from services.system_issues import add_entry as add_system_issue  # type: ignore
            eid = add_system_issue(
                description=(
                    f"[synthetic:{persona['id']}] {cat}/{diag.get('issue_type')} (sev={sev})\n"
                    f"質問: {query}\nbot応答: {response[:300]}\n"
                    f"原因仮説: {diag.get('root_cause','')}\n提案: {diag.get('proposed_fix','')}"
                ),
                expected="うみやまAI が質問に正確・自然に答える",
                reviewer="synthetic-agent",
            )
            return {**base, "action": "queued_system", "entry": eid, "sig": sig}
        except Exception as e:
            return {**base, "action": "queue_failed", "detail": str(e)[:120]}

    return {**base, "action": "logged"}


# ─── orchestration ────────────────────────────────────────────────────
async def run_all(sample: int | None = None, dry_run: bool = False, push: bool = True) -> dict:
    if not ENABLED:
        logger.info("SYNTHETIC_AGENT_ENABLED=0 → skip")
        return {"skipped": "disabled"}

    # ★系列分離の強制 (env 上書きで bot=judge が同一 model = self-eval loop に退化する事故を防ぐ)
    # 注: litellm alias は smart(=Opus)/smart-gpt(=GPT) のように別系列でも prefix 共有 → 完全一致で判定。
    if BOT_MODEL == GEN_MODEL:
        logger.error(f"model 衝突: bot と 生成・診断 が同一 model ({BOT_MODEL}) → self-eval loop。中止 (§1.15)。")
        return {"error": "model_family_collision", "bot_model": BOT_MODEL, "gen_model": GEN_MODEL}

    if AUTOFIX:
        logger.warning(f"SYNTHETIC_AGENT_AUTOFIX=1: keyword_miss の確実な別表記のみ自律追記 (cap {MAX_AUTOFIX}/run)")

    personas = PERSONAS[:sample] if sample else PERSONAS
    logger.info(f"synthetic_employee_agent start: personas={len(personas)}, dry_run={dry_run}, AUTOFIX={AUTOFIX}")

    pairs: list[tuple[dict, str]] = []
    for persona in personas:
        qs = await generate_queries(persona, QUERIES_PER_PERSONA)
        for q in qs:
            pairs.append((persona, q))
    pairs = pairs[:MAX_QUERIES]

    if dry_run:
        logger.info(f"[dry-run] {len(pairs)} queries generated:")
        for persona, q in pairs:
            logger.info(f"  ({persona['id']}) {q}")
        return {"dry_run": True, "n_queries": len(pairs),
                "queries": [{"persona": p["id"], "query": q} for p, q in pairs]}

    if not pairs:
        summary = {"run_at": datetime.now(JST).isoformat(), "n_queries": 0, "degraded": "no_queries_generated"}
        _write_run_log(summary, [])
        if push:
            try:
                line_push("⚠️ synthetic社員エージェント: query 生成に失敗 (LLM 不調?)。本日は監査をスキップ。")
            except Exception:
                pass
        logger.error("no queries generated → degraded, skip")
        return summary

    seen = _load_seen_sigs()
    findings: list[dict] = []
    n_ok = 0
    autofix_used = 0
    for i, (persona, q) in enumerate(pairs):
        logger.info(f"  [{i+1}/{len(pairs)}] ({persona['id']}) {q[:50]}")
        resp, err = await run_bot(q)
        if err or not resp:
            logger.warning(f"    bot error: {err}")
            findings.append({"persona": persona["id"], "query": q, "action": "bot_error", "detail": err})
            continue
        diag = await diagnose(persona, q, resp)
        if not diag.get("has_issue"):
            n_ok += 1
            continue
        action = route(diag, persona, q, resp, seen, autofix_remaining=MAX_AUTOFIX - autofix_used)
        if action.get("action") == "proposed_alias":
            autofix_used += 1  # 未承認 alias 記録も write 数として cap (暴走防止)
        findings.append(action)

    _save_seen_sigs(seen)

    summary = {
        "run_at": datetime.now(JST).isoformat(),
        "n_queries": len(pairs),
        "n_ok": n_ok,
        "n_findings": len([f for f in findings if str(f.get("action", "")).startswith("queued")]),
        "n_autofix": autofix_used,
        "n_deduped": len([f for f in findings if f.get("action") == "deduped"]),
        "n_bot_error": len([f for f in findings if f.get("action") == "bot_error"]),
        "autofix_enabled": AUTOFIX,
        "by_action": _count_by(findings, "action"),
        "by_category": _count_by([f for f in findings if str(f.get("action", "")).startswith("queued")], "fix_category"),
    }
    _write_run_log(summary, findings)
    if push:
        _push_summary(summary, findings)
    logger.info(f"done: {summary['by_action']}")
    return summary


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        v = str(r.get(key, "?"))
        out[v] = out.get(v, 0) + 1
    return out


def _write_run_log(summary: dict, findings: list[dict]) -> None:
    try:
        SYN_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(JST).strftime("%Y-%m-%d")
        append_jsonl(SYN_DIR / f"{date_str}.jsonl", {"summary": summary, "findings": findings})
    except Exception as e:
        logger.warning(f"_write_run_log failed: {e}")


def _push_summary(summary: dict, findings: list[dict]) -> None:
    """海山に日次サマリを LINE push。"""
    try:
        mode = "alias提案ON" if summary.get("autofix_enabled") else "提案のみ"
        lines = [
            f"🤖 synthetic社員エージェント [{mode}]",
            f"質問 {summary['n_queries']} 件 / 問題なし {summary['n_ok']} / 新規提案 {summary['n_findings']}",
        ]
        # 自律記録した alias 候補 (未承認)。海山が承認して初めて検索に効く (verify-before-activate)
        aliases = [f for f in findings if f.get("action") == "proposed_alias"]
        if aliases:
            lines.append("alias 候補 (未承認、承認で検索反映):")
            for a in aliases[:5]:
                lines.append(f"  ・{a.get('term')} → {a.get('synonyms')}")
            lines.append("承認: python3 scripts/synthetic_employee_agent.py --approve <term>")
        if summary.get("n_deduped"):
            lines.append(f"(重複 skip {summary['n_deduped']} 件)")
        if summary["by_category"]:
            lines.append("提案内訳: " + ", ".join(f"{k}:{v}" for k, v in summary["by_category"].items() if k != "None"))
        if summary["n_findings"]:
            lines.append("→ /admin/review で確認・承認")
        line_push("\n".join(lines))
    except Exception as e:
        logger.warning(f"_push_summary failed: {e}")


# ─── CLI ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="社員に扮した synthetic user による proactive QA + 改善")
    parser.add_argument("--dry-run", action="store_true", help="query 生成だけ (bot 投げない)")
    parser.add_argument("--sample", type=int, default=None, help="persona N 件だけ")
    parser.add_argument("--no-push", action="store_true", help="LINE push 抑止")
    parser.add_argument("--list-aliases", action="store_true", help="alias 候補一覧 (pending/enabled)")
    parser.add_argument("--approve", metavar="TERM", help="alias を承認 (= 検索に反映)")
    parser.add_argument("--revoke", metavar="TERM", help="alias 承認を取消")
    args = parser.parse_args()

    if args.list_aliases:
        for term, meta in list_aliases().items():
            mark = "✅" if isinstance(meta, dict) and meta.get("enabled") else "⏳"
            al = meta.get("aliases") if isinstance(meta, dict) else meta
            print(f"{mark} {term} → {al}")
        return
    if args.approve:
        print("承認" if approve_alias(args.approve, True) else "該当 alias 無し", args.approve)
        return
    if args.revoke:
        print("取消" if approve_alias(args.revoke, False) else "該当 alias 無し", args.revoke)
        return

    summary = asyncio.run(run_all(sample=args.sample, dry_run=args.dry_run, push=not args.no_push))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
