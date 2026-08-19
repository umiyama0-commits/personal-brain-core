#!/usr/bin/env python3
"""scripts/reflux.py — 還流 (各PJ → Core への知見蒸留 + 海山承認)。

★2026-06-28 海山指示 (Step 2、ADR docs/decisions/2026-06-28-personal-brain-core-and-registry.md)。
Personal Brain の重心反転に伴い、各プロジェクト (owndays / personal/<pj>) の記憶から
**project 非依存の判断軸・原則・経験則** を蒸留し、Core (基盤) へ還流する。

絶対の不変条件 (破ると海山の人格を汚染しうる):
1. **propose-only**: 蒸留は queue (data/brain/reflux_queue.jsonl) に status=pending で積むだけ。
   Core への書込は海山の `--approve <id>` (または /reflux ok <id>) 承認時のみ。自動適用は一切無い。
2. **出所主義 (捏造禁止)**: 各候補は実際の記憶を出所 (file + 引用) として持つ。承認時に
   evidence_quote が出所 file に実在するか検証し、無ければ捏造疑いで適用を拒否 (要 --force)。
3. **汎用化**: Core に入るのは固有名詞・数値・案件名を剥がした汎用原則のみ
   (= Core は内部クローン/personal が使う共有層。特定情報を残さない)。
4. **機密 skip**: §1.9 系の機微 decisions (退職/懲戒/相談/面談/顧客incident 等) は蒸留入力から除外。
5. **private 書込**: 還流先 wiki/judgment/reflux-distilled.md は clone_visibility: private
   (= 公開クローン=社員には出さない。内部クローン + /personal のみ)。read-only on project memory。

実行: python3 scripts/reflux.py [--dry-run|--list|--approve <id>|--reject <id>] (host cron、02:10)
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ sibling import

from clone_improve_lib import (  # noqa: E402
    append_jsonl, call_llm, extract_json, line_push, read_jsonl,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reflux")

ROOT = Path(__file__).resolve().parents[1]          # repo root (host-safe、ai_advisor と同流儀)
WIKI_DIR = ROOT / "data" / "brain" / "wiki"
QUEUE = ROOT / "data" / "brain" / "reflux_queue.jsonl"
CORE_TARGET = WIKI_DIR / "judgment" / "reflux-distilled.md"  # 還流先 (private)

RECENT_DAYS = int(os.getenv("REFLUX_RECENT_DAYS", "45"))  # 新しい学びに絞る (DA #7: 静かな夜は安い)

# ★2026-07-01 還流の対象外にする personal サブドメイン。dev = Claude Code 開発ログ
# (dev_journal_sync)。開発の癖を CEO 人格 (judgment) へ自動注入しないための遮断
# (cross-check DA: 人格汚染 + 話者帰属不能 + 機微 skip=False 経路)。人格への昇格は将来の別 gate。
REFLUX_EXCLUDE_PERSONAL = {"dev", ".git"}  # .git = personal_snapshot の入れ子リポ (ドメインではない)


@contextlib.contextmanager
def _queue_lock():
    """queue read-modify-write の排他 (DA #4: distiller append ↔ 承認 rewrite の race)。

    flock ベース (同一 OS 文脈で確実。host cron ↔ container bot の cross-mount は best-effort だが、
    _apply_to_core の冪等 + status==pending gate + dedup 自己修復で二重適用/喪失は無害化される)。
    """
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = QUEUE.with_suffix(".jsonl.lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
MEMORY_CAP = 36_000                                  # 1 ドメインの蒸留入力上限
MAX_CANDIDATES_PER_DOMAIN = 5

# §1.9 系 + 機密/法務/incident: 機微 decisions は蒸留入力から除外 (Core 汚染・機密漏れ防止)。
# ★cross-check Fact-checker が decisions/ の機密 8件 (termination/salary/jcs-case/tsuyama-incident 等) を
#   特定 → 名前 + 本文先頭で確実に捕捉する pattern に強化。
SENSITIVE_RE = re.compile(
    r"(退職|休職|離職|懲戒|処分|解雇|人事評価|考課|給与|賃金|報酬|交渉|健康|メンタル|病|"
    r"相談|面談|1on1|ハラスメント|通報|"
    r"法務|訴訟|係争|裁判|和解|示談|機密|社外秘|極秘|不祥事|事故|インシデント|クレーム|トラブル|JCS|"
    r"termination|harassment|grievance|incident|salary|payroll|negotiation|legal|lawsuit|litigation|confidential)",
    re.IGNORECASE,
)


def _sanitize_core_text(s: str) -> str:
    """Core 追記前に principle / quote を無害化 (frontmatter/構造 注入を封じる)。

    ★cross-check Reviewer: LLM 出力が `---` や clone_visibility:public を密輸して reflux-distilled.md の
    frontmatter (= private) を覆すのを防ぐ。改行を潰し、--- と visibility キーを無害化。
    """
    s = re.sub(r"[\r\n]+", " ", s or "")
    s = s.replace("---", "—").replace("clone_visibility", "clone_vis").replace("```", "ʼʼʼ")
    return s.strip()[:600]


def _recent_md(base: Path) -> list[Path]:
    """base 配下の *.md を mtime 新しい順 (RECENT_DAYS 以内のみ)。

    ★DA #7: 全件 fallback はしない = 静かな夜は何も蒸留せず安い。過去全体を一度 seed したい時は
    env REFLUX_RECENT_DAYS=3650 で実行 (cron は既定 45 日)。
    """
    if not base.exists():
        return []
    cutoff = datetime.now().timestamp() - RECENT_DAYS * 86400
    recent = [p for p in base.rglob("*.md") if p.is_file() and p.stat().st_mtime >= cutoff]
    return sorted(recent, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_capped(paths: list[Path], cap: int, *, skip_sensitive: bool) -> str:
    out, acc = [], 0
    for p in paths:
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        rel = p.relative_to(WIKI_DIR)
        # confidential: true frontmatter は常に除外 (DA #2/#6: 手動キュレーション gate、将来の機微 file 用)
        if re.search(r"^confidential:\s*true", content[:600], re.IGNORECASE | re.MULTILINE):
            logger.info(f"reflux: confidential:true を蒸留入力から除外 {rel}")
            continue
        if skip_sensitive and (SENSITIVE_RE.search(p.name) or SENSITIVE_RE.search(content[:2500])):
            logger.info(f"reflux: 機微 file を蒸留入力から除外 {rel}")
            continue
        block = f"=== {rel} ===\n{content}"
        if acc + len(block) > cap:
            block = block[: max(0, cap - acc)]
            out.append(block)
            break
        out.append(block)
        acc += len(block)
    return "\n\n".join(out)


def list_domains() -> list[str]:
    """蒸留対象ドメイン: owndays + personal/<pj> 各々。"""
    domains = ["owndays"]
    pbase = WIKI_DIR / "personal"
    if pbase.exists():
        domains += [f"personal/{p.name}" for p in sorted(pbase.iterdir())
                    if p.is_dir() and p.name not in REFLUX_EXCLUDE_PERSONAL]
    return domains


def _domain_memory(domain: str) -> str:
    """ドメインの「記憶」を読む (read-only)。owndays=decisions+analysis (判断の宝庫、機密skip)、
    personal/<pj>=その PJ 全体。"""
    if domain == "owndays":
        paths = _recent_md(WIKI_DIR / "decisions") + _recent_md(WIKI_DIR / "analysis")
        return _read_capped(paths, MEMORY_CAP, skip_sensitive=True)
    if domain.startswith("personal/"):
        paths = _recent_md(WIKI_DIR / "personal" / domain.split("/", 1)[1])
        return _read_capped(paths, MEMORY_CAP, skip_sensitive=False)
    return ""


def _core_text(cap: int = 24_000) -> str:
    """既存 Core 判断軸 (judgment/) を dedup 用に読む。"""
    jd = WIKI_DIR / "judgment"
    if not jd.exists():
        return ""
    out, acc = [], 0
    for p in sorted(jd.rglob("*.md")):
        try:
            t = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if acc + len(t) > cap:
            out.append(t[: cap - acc]); break
        out.append(t); acc += len(t)
    return "\n".join(out)


DISTILL_PROMPT = """あなたは海山丈司の Personal Brain の「還流」蒸留器です。
プロジェクト「{domain}」の記憶から、**project に依存しない汎用的な判断軸・原則・経験則** を抽出し、
海山の Core (基盤) へ還流する候補を出します。これは海山の人格・判断の土台に入る重い操作です。

# 絶対制約 (破ると有害)
- 出所主義: 各候補は下記「プロジェクト記憶」に**実際に書かれている**ことだけを根拠にする。
  海山が述べていない判断・原則を創作しない。曖昧なら出さない。evidence_quote は記憶からの**逐語の短い引用**。
- 汎用化: Core に入るのは「どの仕事でも使える原則」。固有名詞・店舗名・案件名・数値・金額は**必ず剥がす**。
  例: 「天神店の客単価が低い」→ ×(特定事実) / 「現場の数値異常は当て推量せず要因分解で詰める」→ ○(汎用判断軸)
- 既出除外: 下記「既存 Core」に既にある原則は出さない。
- 1-{maxn} 件。無理に埋めない (0 件で良い)。質 > 量。

# プロジェクト記憶 ({domain})
{memory}

# 既存 Core (重複させない)
{core}

# 出力 (JSON のみ)
{{"candidates": [
  {{"principle": "<汎用原則 1-2文・固有情報なし>", "type": "judgment|knowledge|experience",
    "evidence_file": "<出所 file の相対パス>", "evidence_quote": "<記憶からの逐語の短い引用>",
    "generalizable_note": "<なぜ project 非依存と言えるか 1文>"}}
]}}"""


DIGEST_STATE = ROOT / "data" / "brain" / ".reflux_input_digest.json"


_RUN_FAILURES: list[str] = []
# ★§1.15 Reviewer M1: digest を distill() 内 (LLM 成功直後) に保存すると、--dry-run が
# 「LLM は成功、queue には入れない」ので **その候補が入力不変の間ずっと queue に入らなくなる**
# (静かな恒久喪失)。queue 投入を見届けてから run() 側で確定させる。
_PENDING_DIGESTS: dict[str, str] = {}


def _note_failure(domain: str, detail: str) -> None:
    """蒸留失敗を run 単位で集約 (§1.18: 記録は 1 実行 1 箇所 = loud_fail は run() 末尾で 1 回)。"""
    _RUN_FAILURES.append(f"{domain}: {detail}")


def _input_digest(domain: str, memory: str, core: str = "") -> str:
    """蒸留入力の指紋。

    **memory だけでなく core と prompt template も含める**。承認 (`/reflux ok`) は Core を
    書き換えるので、memory 不変でも「Core に既に有るか」の判断材料が変わる = 再蒸留すべき。
    prompt を含めるのは、蒸留方針を変えた時に古い skip が効き続けないようにするため。
    """
    h = hashlib.sha256()
    for part in (memory, core, DISTILL_PROMPT):
        h.update(hashlib.sha256(part.encode("utf-8", "ignore")).digest())
    return h.hexdigest()[:16]


def _load_digests() -> dict:
    try:
        return json.loads(DIGEST_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_digest(domain: str, digest: str) -> None:
    try:
        d = _load_digests()
        d[domain] = digest
        DIGEST_STATE.parent.mkdir(parents=True, exist_ok=True)
        DIGEST_STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.warning(f"digest 保存失敗 (非致命): {e}")


async def distill(domain: str, llm=None) -> list[dict]:
    llm = llm or call_llm
    memory = _domain_memory(domain)
    if not memory or len(memory) < 200:
        return []
    # ★2026-08-03 実測: 入力が前回と 1 バイトも変わっていないのに毎晩 LLM に投げていた。
    # 特に personal/example-garden は個人 LINE 全文 (630 メッセージ・第三者2名の発話込み) を
    # 36,000 字そのまま **年 365 回** Anthropic へ送っていた (実測 47,935 prompt tok/日 が
    # 8/10 以降ずっと同値)。同じ入力から新しい候補は出ないので、送信自体を止める。
    # = コストだけでなく「外部に出る回数」を 365 → 1 に減らす privacy 施策でもある。
    _core = _core_text()[:24_000]
    _dg = _input_digest(domain, memory, _core)
    if _load_digests().get(domain) == _dg:
        logger.info(f"distill {domain}: 入力が前回と同一 → skip (外部送信なし)")
        return []
    prompt = DISTILL_PROMPT.format(
        domain=domain, memory=memory[:MEMORY_CAP], core=_core,
        maxn=MAX_CANDIDATES_PER_DOMAIN)
    try:
        raw = await llm(prompt, model="smart", max_tokens=2500, temperature=0.2)
        data = extract_json(raw)
    except Exception as e:
        logger.warning(f"distill {domain} failed: {type(e).__name__}: {e}")
        _note_failure(domain, f"{type(e).__name__}: {e}")
        return []
    # ★§1.15 DA: digest を「LLM が例外を出さなかった」だけで保存すると、LLM が list を返した /
    # candidates キーが無い / 全 principle が空 といった **例外にならない外れ応答** の夜に
    # digest が焼き付き、入力が静的なドメイン (example 等) は永久に蒸留されなくなる。
    # 「候補 0 件」は正当な答え (質>量、0 件で良い と prompt が明示) なので skip 対象に含めるが、
    # **応答の形が壊れている場合は skip 対象にしない** = 次回やり直す。
    if not isinstance(data, dict) or "candidates" not in data:
        logger.warning(f"distill {domain}: 応答形式が不正 (candidates 無し) → digest 記録せず再試行")
        _note_failure(domain, "malformed response (no candidates key)")
        return []
    _PENDING_DIGESTS[domain] = _dg   # 記録は run() が queue 投入まで見届けてから (M1)
    cands = data.get("candidates", [])
    if not isinstance(cands, list):
        logger.warning(f"distill {domain}: candidates が list でない → digest 記録せず再試行")
        _PENDING_DIGESTS.pop(domain, None)
        _note_failure(domain, "malformed response (candidates not a list)")
        return []
    out = []
    for c in cands[:MAX_CANDIDATES_PER_DOMAIN]:
        p = (c.get("principle") or "").strip()
        if not p:
            continue
        out.append({
            "principle": p,
            "type": (c.get("type") or "judgment").strip(),
            "evidence_file": (c.get("evidence_file") or "").strip(),
            "evidence_quote": (c.get("evidence_quote") or "").strip(),
            "generalizable_note": (c.get("generalizable_note") or "").strip(),
            "source_domain": domain,
        })
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _candidate_id(principle: str) -> str:
    return "rfx-" + hashlib.sha1(_norm(principle).encode("utf-8")).hexdigest()[:10]


def _dedup(cands: list[dict]) -> list[dict]:
    """既存 queue (全 status) と Core text に対して重複/既出を落とす。"""
    seen_ids = {r.get("id") for r in read_jsonl(QUEUE)}
    core_norm = _norm(_core_text())
    out = []
    for c in cands:
        cid = _candidate_id(c["principle"])
        if cid in seen_ids:
            continue
        # principle の核フレーズが既に Core にあれば既出扱い
        head = _norm(c["principle"])[:18]
        if head and head in core_norm:
            continue
        c["id"] = cid
        out.append(c)
    return out


async def run(*, dry_run: bool = False, llm=None, push_fn=None) -> dict:
    push_fn = push_fn or line_push
    all_new: list[dict] = []
    _RUN_FAILURES.clear()
    _PENDING_DIGESTS.clear()
    _domains = list_domains()
    for domain in _domains:
        cands = await distill(domain, llm=llm)
        fresh = _dedup(cands)
        all_new.extend(fresh)
        if fresh:
            logger.info(f"reflux {domain}: {len(fresh)} new candidate(s)")
    if dry_run:
        for c in all_new:
            print(f"[{c['id']}] ({c['source_domain']}/{c['type']}) {c['principle']}")
        # digest は保存しない = 次の本番 run で必ず蒸留し直す (M1: dry-run が候補を消さない)
        return {"ok": True, "dry_run": True, "candidates": len(all_new)}
    now = datetime.now().isoformat(timespec="seconds")
    with _queue_lock():
        for c in all_new:
            append_jsonl(QUEUE, {**c, "status": "pending", "ts": now})
    # queue 投入を見届けてから digest を確定 (append_jsonl が落ちた domain は次回も再蒸留)
    for _d, _v in _PENDING_DIGESTS.items():
        _save_digest(_d, _v)
    if all_new:
        try:
            push_fn(build_push(all_new))
        except Exception as e:
            logger.warning(f"reflux push failed: {e}")
            _note_failure("push", f"{type(e).__name__}: {e}")
    # ★2026-08-03 §1.18 配線: 全ドメインが例外で落ちても従来は log 1 行で無音だった
    # (= 還流が何日止まっても気付けない)。静かな日 (入力同一で skip = 失敗ゼロ) は成功扱いに
    # して alert 疲れを避け、**失敗が 1 つでも出た run は ok=False** にする。
    # ★§1.15 Reviewer M3: 当初 `_RUN_FAILURES and not all_new` にしていたが、push は
    # all_new 非空の時しか走らないので **push 失敗は永久に streak に乗らない**デッド配線だった
    # (§1.18 は「配信」の silent 死を明示対象にしている)。
    try:
        from clone_improve_lib import loud_fail
        _ok = not _RUN_FAILURES
        loud_fail("reflux", _ok,
                  "; ".join(_RUN_FAILURES[:4]) or "全ドメインの蒸留に失敗",
                  threshold=3, cooldown_h=24)
    except Exception as e:
        logger.warning(f"loud_fail 記録失敗 (非致命): {e}")
    return {"ok": True, "candidates": len(all_new), "queued": [c["id"] for c in all_new]}


def build_push(cands: list[dict]) -> str:
    lines = [f"🌀 還流 提案 {len(cands)}件 (各PJ→基盤の判断軸。承認まで Core 不変)"]
    for c in cands[:8]:
        lines.append(f"・[{c['id']}] ({c['source_domain']}) {c['principle'][:60]}")
    lines.append("\n承認: /reflux ok <id> / 却下: /reflux ng <id> / 一覧: /reflux")
    return "\n".join(lines)


# ── queue 操作 / 承認 ──

def _load_queue() -> list[dict]:
    return read_jsonl(QUEUE)


def _rewrite_queue(records: list[dict]) -> None:
    tmp = QUEUE.with_suffix(".jsonl.tmp")
    import json
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    tmp.replace(QUEUE)   # atomic


def list_pending() -> list[dict]:
    return [r for r in _load_queue() if r.get("status") == "pending"]


def _verify_evidence(rec: dict) -> bool:
    """evidence_quote が出所 file に実在するか (捏造検出)。逐語 or 正規化部分一致。"""
    ef, q = rec.get("evidence_file", ""), rec.get("evidence_quote", "")
    if not ef or not q:
        return False
    src = WIKI_DIR / ef
    if not src.exists():
        # 出所 file 名が相対でない/別表記なら domain 配下を緩く探す
        cands = list(WIKI_DIR.rglob(Path(ef).name)) if ef else []
        if not cands:
            return False
        src = cands[0]
    try:
        body = _norm(src.read_text(encoding="utf-8"))
    except Exception:
        return False
    qn = _norm(q)
    return len(qn) >= 6 and (qn in body or qn[:20] in body)


def approve(cid: str, *, force: bool = False) -> dict:
    with _queue_lock():
        recs = _load_queue()
        target = next((r for r in recs if r.get("id") == cid and r.get("status") == "pending"), None)
        if not target:
            return {"ok": False, "reason": "pending 候補に該当 id 無し (承認済/却下済/不正)"}
        if not force and not _verify_evidence(target):
            return {"ok": False, "reason": "evidence が出所に見つからない (捏造の疑い)。"
                    f" 出所={target.get('evidence_file')} を確認。強制は --force / ok! <id>"}
        _apply_to_core(target)
        for r in recs:
            if r.get("id") == cid:
                r["status"] = "approved"
                r["approved_at"] = datetime.now().isoformat(timespec="seconds")
                r["evidence_verified"] = not force
        _rewrite_queue(recs)
    return {"ok": True, "applied": cid, "principle": target["principle"]}


def reject(cid: str) -> dict:
    with _queue_lock():
        recs = _load_queue()
        found = False
        for r in recs:
            if r.get("id") == cid and r.get("status") == "pending":
                r["status"] = "rejected"
                r["rejected_at"] = datetime.now().isoformat(timespec="seconds")
                found = True
        if not found:
            return {"ok": False, "reason": "pending 候補に該当 id 無し"}
        _rewrite_queue(recs)
    return {"ok": True, "rejected": cid}


def approve_many(ids: list[str], *, force: bool = False) -> dict:
    """バッチ承認 (★2026-07-05 海山指示「一括承認できるように」)。

    per-item の evidence 検証 (捏造ゲート) は維持 — 通らないものは skip して報告
    (個別に /reflux ok! で強制可)。加えて**バッチ内の類似重複を保留**する:
    蒸留は言い換え違いの同旨を複数出す (実測 47 件中に「…確認できるようにする」vs
    「…できるようにすることが重要である」等の対) — 無検査の一括だと Core が同文で膨れる。
    類似 (difflib ratio ≥ 0.8) は適用せず pending に残す = 海山が個別に ng できる。
    """
    import difflib
    applied, ev_skipped, dup_held = [], [], []
    applied_norms: list[str] = []
    for cid in ids:
        rec = next((r for r in list_pending() if r.get("id") == cid), None)
        if not rec:
            ev_skipped.append((cid, "pending に無し"))
            continue
        norm = _norm(rec.get("principle", ""))
        if any(difflib.SequenceMatcher(None, norm, a).ratio() >= 0.8 for a in applied_norms):
            dup_held.append(cid)
            continue
        r = approve(cid, force=force)
        if r.get("ok"):
            applied.append(cid)
            applied_norms.append(norm)
        else:
            ev_skipped.append((cid, r.get("reason", "")[:48]))
    return {"ok": True, "applied": applied, "ev_skipped": ev_skipped, "dup_held": dup_held}


def _resolve_ids(given: list[str], pend: list[dict]) -> tuple[list[str], list[str]]:
    """完全一致 → 一意 prefix (rfx- 省略可、≥4字) で pending id へ解決 (/bridge と同 UX)。"""
    pool = [r["id"] for r in pend]
    ok, bad = [], []
    for g in given:
        g2 = g if g.startswith("rfx-") else f"rfx-{g}"
        if g in pool or g2 in pool:
            ok.append(g if g in pool else g2)
            continue
        stem = g[4:] if g.startswith("rfx-") else g
        hits = [i for i in pool if i.startswith(f"rfx-{stem}")] if len(stem) >= 4 else []
        if len(hits) == 1:
            ok.append(hits[0])
        else:
            bad.append(g)
    return ok, bad


def _apply_to_core(rec: dict) -> None:
    """承認候補を Core (reflux-distilled.md、private) に追記。冪等 (id 既出なら skip)。"""
    CORE_TARGET.parent.mkdir(parents=True, exist_ok=True)
    if not CORE_TARGET.exists():
        CORE_TARGET.write_text(
            "---\n"
            "type: judgment_digest\n"
            "id: reflux-distilled\n"
            "domain: multi\n"
            "clone_visibility: private\n"
            "confidence: medium\n"
            "note: 還流(各PJ→Core 蒸留)で海山が承認した汎用判断軸。出所付き。scripts/reflux.py。\n"
            "---\n"
            "# 還流 蒸留 判断軸(海山 承認済・project 非依存)\n\n", encoding="utf-8")
    existing = CORE_TARGET.read_text(encoding="utf-8")
    if f"id: {rec['id']}" in existing:   # 二重適用防止 (冪等)
        return
    entry = (
        f"## {_sanitize_core_text(rec['principle'])}\n"
        f"- type: {rec.get('type','judgment')} / 出所: {rec.get('source_domain','?')}"
        f" / {rec.get('evidence_file','?')} / id: {rec['id']} / 承認: {date.today().isoformat()}\n"
        f"> {_sanitize_core_text(rec.get('evidence_quote',''))}\n\n"
    )
    with CORE_TARGET.open("a", encoding="utf-8") as f:
        f.write(entry)


def handle_command(arg: str) -> str:
    """LINE /reflux <arg> の処理 (admin)。arg='' (一覧) | 'ok <id>' | 'ok! <id>' | 'ng <id>'。

    呼び出し元 (main.py / brain_commands.py) で is_lw_admin / is_admin gate 済前提。
    LLM 蒸留 (run) は呼ばない = 重い distillation は cron 専任。ここは一覧/承認/却下のみ。
    """
    arg = (arg or "").strip()
    if not arg:
        pend = list_pending()
        if not pend:
            return "🌀 還流: pending 候補は無し。"
        lines = [f"🌀 還流 pending {len(pend)}件 (承認まで Core 不変):"]
        for r in pend[:15]:
            lines.append(f"・[{r['id']}] ({r.get('source_domain')}) {r['principle'][:64]}")
        lines.append("承認: /reflux ok <id> / 却下: /reflux ng <id> / 強制(捏造確認済): /reflux ok! <id>")
        return "\n".join(lines)
    # ★2026-07-05 海山指示「一括承認できるように」: ok all / 複数 id / prefix (/bridge と同 UX)
    parts = arg.split()
    op, ids = parts[0].lower(), parts[1:]
    if not ids:
        return "id を指定してください (例: /reflux ok rfx-xxxx / 一括: /reflux ok all)。一覧は /reflux"
    if op not in ("ok", "ok!", "approve", "ng", "reject", "no"):
        return "使い方: /reflux | /reflux ok all | /reflux ok <id>… | /reflux ng <id>… | /reflux ok! <id>"
    force = (op == "ok!")
    is_ok = op in ("ok", "ok!", "approve")
    pend = list_pending()
    if "all" in ids or "all!" in ids:
        if len(ids) > 1:
            return "⚠️ all と個別 id は同時指定できません。"
        if not is_ok:
            if ids[0] != "all!":   # 全却下は再蒸留されない (dedup) → 確認付き
                return "⚠️ 全却下は同じ候補が再提案されません。本当に良ければ /reflux ng all!"
            for r in pend:
                reject(r["id"])
            return f"🗑 一括却下: {len(pend)} 件"
        r = approve_many([p["id"] for p in pend], force=force)
        msg = f"✅ Core(基盤)に還流: {len(r['applied'])} 件"
        if r["dup_held"]:
            msg += f"\n⏸ 類似重複につき保留 (pending のまま): {len(r['dup_held'])} 件 — 個別に /reflux ng を"
        if r["ev_skipped"]:
            heads = ", ".join(i for i, _ in r["ev_skipped"][:5])
            msg += f"\n⚠️ evidence 未検証 skip: {len(r['ev_skipped'])} 件 ({heads}) — 確認済なら /reflux ok! <id>"
        return msg
    resolved, bad = _resolve_ids(ids, pend)
    if is_ok:
        r = approve_many(resolved, force=force) if resolved else {"applied": [], "ev_skipped": [], "dup_held": []}
        msg = f"✅ Core(基盤)に還流: {len(r['applied'])} 件"
        if r["ev_skipped"]:
            msg += f" / ⚠️ skip {len(r['ev_skipped'])} 件: " + "; ".join(f"{i} ({why})" for i, why in r["ev_skipped"][:3])
        if r["dup_held"]:
            msg += f" / ⏸ 類似保留 {len(r['dup_held'])} 件"
    else:
        done = [reject(i) for i in resolved]
        msg = f"🗑 却下: {sum(1 for d in done if d.get('ok'))} 件"
    if bad:
        msg += f" (不明 id: {', '.join(bad[:5])})"
    return msg


def main() -> int:
    ap = argparse.ArgumentParser(description="還流 (各PJ→Core 蒸留 + 海山承認、host cron)")
    ap.add_argument("--dry-run", action="store_true", help="蒸留して候補を表示 (queue/push しない)")
    ap.add_argument("--list", action="store_true", help="pending 候補を一覧")
    ap.add_argument("--approve", metavar="ID", help="候補を承認 → Core 適用")
    ap.add_argument("--reject", metavar="ID", help="候補を却下")
    ap.add_argument("--force", action="store_true", help="evidence 未検証でも承認 (捏造確認済の時のみ)")
    a = ap.parse_args()
    if a.list:
        for r in list_pending():
            print(f"[{r['id']}] ({r.get('source_domain')}/{r.get('type')}) {r['principle']}")
        return 0
    if a.approve:
        print(approve(a.approve, force=a.force)); return 0
    if a.reject:
        print(reject(a.reject)); return 0
    r = asyncio.run(run(dry_run=a.dry_run))
    print(r)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
