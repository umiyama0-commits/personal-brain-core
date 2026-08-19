#!/usr/bin/env python3
"""scripts/dev_journal_sync.py — Claude Code 開発セッション → personal/dev 蓄積 (v2)。

★2026-07-01 海山指示「Claude Code 上の指示・やり取り・改善判断を履歴/癖として残し、将来 Personal
Brain に開発判断をさせる時、過去傾向 + wiki + データからスムーズに進むように」。
方針 (海山): Claude 会話も人格形成の参考として Brain に「接続」はする。ただし **人格パイプラインへ
どこまで取り込むかは段階的に別途設計**。完全な機微は ChatGPT 側で扱う。

★同日 cross-check (fact-checker / reviewer / devil's-advocate 3体) を受けた v2 設計:
- **人格パイプライン非直結** (DA): personal/dev は reflux.list_domains() の除外対象 = 開発の癖が
  海山の Core 人格 (judgment/reflux-distilled.md) へ**自動注入されない**。dev 傾向は personal/dev に
  貯まり将来の /dev advisor が参照する専用ストア。人格への昇格は将来の別 gate (段階的)。
- **増分取込** (reviewer B1/B2): session は長寿命 (主 session は 100MB・数ヶ月) なので per-session の
  byte offset high-water mark で**新規分のみ stream 処理** (memory O(window))。初見 session は
  offset=EOF (導入以降を前向き捕捉、巨大な歴史 backfill はしない。--session で個別 backfill 可)。
- **スコープ限定** (DA): PB 開発に触れた window のみ蒸留 (repo 非参照の雑務は skip)。
- **機微ハードフィルタ** (DA/S4): SENSITIVE_RE 一致は confidential:true (= 下流 reflux が無条件 skip
  する層) + secret 形状は redact。prompt 依存でなくコードの制御。
- **話者帰属** (DA): 癖は海山の user 発話 (指示/feedback) を根拠に限定。Claude の動作を海山の判断軸に
  しない。umiyama_evidence に根拠発話を保存。

privacy: personal/ は main repo gitignore (nested snapshot 管理) + is_personal_rel で OWNDAYS/社員
クローンの retrieval から自動除外。§1.9 機微は confidential:true でさらに遮断。

実行 (host cron, daily): python3 scripts/dev_journal_sync.py [--dry-run] [--limit N] [--session <id>]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ sibling import

from clone_improve_lib import call_llm, extract_json  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("dev_journal_sync")

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "brain" / ".dev_journal_state.json"
DEV_DIR = (ROOT / "data" / "brain" / "wiki" / "personal" / "dev").resolve()
SESSION_DIRS = [
    Path(os.path.expanduser("~/.claude/projects/-Users-brain-brain-agent-data")),
    Path(os.path.expanduser("~/.claude/projects/-Users-brain-brain-agent")),
    # ★2026-07-25 海山「Claude code の開発履歴は記憶されている?」→ 実測でほぼ空と判明し追加。
    #   開発の主戦場は MacBook 側 (`-Users-umiyamatakeshi-brain-agent*`) だが、この cron は
    #   Mac Studio で走るため MacBook のファイルが見えず、上 2 dir だけを見て空振りしていた
    #   (wiki/personal/dev/ が 3 件・2026-07-07 で停止)。MacBook の
    #   scripts/dev_journal_push.sh が JSONL をここへ配送し、蒸留/書込は従来どおり本 script。
    #   flatten 配送だが session id が UUID のため衝突せず、state の増分 offset も正しく効く。
    Path(os.path.expanduser("~/.claude/projects/_macbook-brain-agent")),
]
# 追加 dir は env で足せる (`:` 区切り。運用中の別マシン追加を code 変更なしで)
SESSION_DIRS += [Path(os.path.expanduser(p)) for p in
                 os.getenv("DEV_JOURNAL_EXTRA_DIRS", "").split(":") if p.strip()]

MIN_NEW_CHARS = 400            # 新規区間がこれ未満なら蒸留しない (雑多な短い増分を無視)
WINDOW_CAP = 40_000            # LLM に渡す区間の上限 (超過は末尾優先=直近の判断)
MAX_PER_RUN = 12               # cost spike 防止
DISTILL_MODEL = "smart-gpt"    # 別系列 (Claude 自作業の meta 評価で self-eval ループ回避)

# 機微 (§1.9): 一致した区間の記録は confidential:true で下流 reflux/persona へ遮断
SENSITIVE_RE = re.compile(
    r"(退職|懲戒|解雇|給与|給料|報酬|賞与|人事評価|考課|相談対応|相談記録|面談記録|1on1|"
    r"ハラスメント|メンタル|通報|内部通報|健康診断|病歴|counseling|consultation|grievance|"
    r"harassment|whistleblow|個人情報|顧客名簿)"
)
# secret 形状 → redact
SECRET_RE = re.compile(
    # ★2026-08-03 実測で穴を検出: 旧 `sk-[A-Za-z0-9]{16,}` は **ハイフンで止まる**ため、
    # 本システム自身が使う Anthropic 形式 `sk-ant-api03-…` / `sk-litellm-…` が素通りしていた。
    # 加えて `KEY=値` / `PASS="値"` 形式の代入も対象外だった (gitleaks 側にはある rule)。
    # dev session には両方が頻出するため拡張する。
    r"(sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|gho_[A-Za-z0-9]{20,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{20,}|-----BEGIN [A-Z ]+PRIVATE KEY-----|"
    # KEY / TOKEN / SECRET / PASS(WORD) への代入 (値が 6 文字以上)
    # ★§1.15 Reviewer M2: 当初は識別子の **先頭側** しかワイルドカードにしておらず、
    # `LITELLM_MASTER_KEY=` `BRAIN_EXTENSION_KEY=` (API_?KEY 非該当) と
    # `AWS_SECRET_ACCESS_KEY=` (SECRET の後に _ が続く) が素通りしていた。
    # 前者 2 つは本 repo の dev セッションに最頻出の secret (§1.17 の rotate 対象そのもの)。
    # 識別子の **末尾側にも** ワイルドカードを許して閉じる。
    # 値は **ASCII の資格情報らしい形** に限定 (8 文字以上)。当初 `[^\s...]{6,}` と広く取ったら
    # 「この KEY: 設計を見直す」のような日本語の地の文まで潰した (会話ログが読めなくなる)。
    r"(?i:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS)[A-Z0-9_]*)"
    r"\s*[=:]\s*[\"']?[A-Za-z0-9_\-./+=]{8,})"
)
# PB 開発に触れた区間か (repo/dev シグナル)。雑務 (browser errand 等) を除外
DEV_SIGNAL_RE = re.compile(
    r"(brain-agent|scripts/|\.py\b|\.md\b|\.yaml\b|wiki/|litellm|reflux|docker|compose|"
    r"cron|commit|git |rebase|main\.py|brain_wiki|content_extractor|EXTRACT_MODEL|"
    r"personal/|judgment/|CLAUDE\.md|ADR|cross-check|deploy|rebuild)"
)


# ─── state (per-session byte high-water mark) ────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seen": {}}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ─── session transcript parse (user/assistant の text のみ) ───
def _extract_turn(o: dict):
    t = o.get("type")
    if t not in ("user", "assistant"):
        return None
    msg = o.get("message") if isinstance(o.get("message"), dict) else o
    content = msg.get("content")
    role = msg.get("role") or t
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        parts += [b for b in content if isinstance(b, str)]
        text = "\n".join(p for p in parts if p)
    else:
        return None
    text = text.strip()
    if not text:
        return None
    if role == "user" and text.startswith("<") and "system-reminder" in text[:40]:
        return None  # ハーネス注入 = 指示ではない
    return role, text


def _safe_day(day) -> str:
    if isinstance(day, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", day):
        return day
    return date.today().isoformat()


def read_new(path: Path, start_offset: int) -> dict:
    """offset 以降を stream 読みして新規 turn を抽出。memory O(delta)。"""
    turns: list[tuple[str, str]] = []
    day = None
    size = path.stat().st_size
    if start_offset > size:      # ファイルが縮んだ (rotate 等) → 頭から
        start_offset = 0
    end = start_offset
    with path.open("rb") as f:    # binary = tell() が真の byte offset (st_size と比較可)
        f.seek(start_offset)
        while True:
            raw = f.readline()
            if not raw:
                break
            end = f.tell()
            s = raw.decode("utf-8", "ignore").strip()
            if not s:
                continue
            try:
                o = json.loads(s)
            except Exception:
                continue          # 不完全行 (書込途中) は次回 offset で拾い直す
            if day is None:
                ts = o.get("timestamp") or (o.get("message") or {}).get("timestamp")
                if isinstance(ts, str) and re.match(r"^\d{4}-\d{2}-\d{2}", ts):
                    day = ts[:10]
            r = _extract_turn(o)
            if r:
                turns.append(r)
    body = "\n\n".join(f"[{'海山' if role == 'user' else 'Claude'}] {text}"
                       for role, text in turns)
    if len(body) > WINDOW_CAP:
        body = "…(前略)…\n\n" + body[-WINDOW_CAP:]
    # ★2026-08-03 実測で判明した穴: SECRET_RE の redact は `_redact` → `_clean` 経由でしか
    # 呼ばれず、`_clean` は **LLM 出力の markdown 書き出し時にしか**適用されていなかった。
    # つまり LLM へ送る 40,000 字の window は完全に無加工で、開発セッション中に出た
    # API key / token が平文で外部 (GPT-5.4) に渡っていた (実測 39 call / 518,435 token 送信済)。
    # docstring は「機微ハードフィルタ」を謳っていたが入力側には成立していなかった。
    # 送信前に redact を掛ける (出力側の _clean は frontmatter 無害化も兼ねるので据え置き)。
    body = _redact(body)
    return {"window": body, "end": end, "date": _safe_day(day)}


def _redact(s) -> str:
    if not isinstance(s, str):
        s = str(s)
    return SECRET_RE.sub("[REDACTED]", s)


def _clean(s) -> str:
    """frontmatter/fence injection 無害化 (reflux._sanitize と同趣旨) + secret redact。"""
    return _redact(s).replace("---", "—").replace("```", "ʼʼʼ").replace(
        "clone_visibility", "clone_vis").replace("confidential", "conf.").strip()


# ─── 蒸留 (話者帰属: 癖は海山の発話に根拠) ─────────────────────
DISTILL_PROMPT = """以下は OWNDAYS CEO 海山丈司 (=[海山]) が自身の Personal Brain を Claude (=[Claude])
と開発しているセッションの新しい区間です (ツール出力は除去済)。

仕事: この区間で **開発上の判断・方針転換・海山の feedback や修正・改善決定** が起きたかを見極め、
起きていれば構造化する。雑談/失敗試行のみで判断が無ければ occurred=false。

厳守:
- **patterns (癖・判断軸) は [海山] の発話 (指示・判断・feedback) を根拠にできるものだけ**抽出する。
  [Claude] の動作・安全則・段取りを海山の判断軸として書かない (別人の癖を混ぜない)。根拠が [海山] の
  発話に無ければ patterns は空配列。
- **機密の具体値を書かない** (給与/個人名/顧客/PII/secret/具体数値は落とし、判断と癖だけ)。
- 憶測で足さない。会話に無い判断を創作しない。

JSON のみ返す:
{{"occurred": true/false,
  "title": "20字以内の要約",
  "instruction": "海山の指示/依頼の要旨 (1-2文)",
  "decision": "実際に採った判断 (1-3文)",
  "rationale": "その根拠 (1-2文)",
  "outcome": "結果/状態 (1文、未完なら『継続中』)",
  "umiyama_evidence": "patterns の根拠にした [海山] の発話の短い引用 (無ければ空文字)",
  "patterns": ["海山の発話に根ざす project 非依存の開発判断軸", ...],
  "commits": ["関連コミット要旨があれば", ...]}}

--- 区間 ---
{window}
"""


async def distill(window: str, llm=None, stats: dict | None = None) -> dict | None:
    llm = llm or call_llm
    try:
        raw = await llm(DISTILL_PROMPT.format(window=window), model=DISTILL_MODEL,
                        max_tokens=1500, temperature=0.2, component="dev_journal")
    except TypeError:
        raw = await llm(DISTILL_PROMPT.format(window=window), model=DISTILL_MODEL,
                        max_tokens=1500, temperature=0.2)
    except Exception as e:
        # ★2026-07-25 §1.18: LLM 恒常失敗を silent にしない (呼び手が loud_fail へ集約)。
        #   実際に発生: LITELLM_URL 未解決で 3 retry 全滅 → warning だけで pipeline は
        #   "ok" を返していた (取込が無言で止まる = 本件の failure mode そのもの)。
        if stats is not None:
            stats["llm_fail"] = stats.get("llm_fail", 0) + 1
        logger.warning(f"distill LLM 失敗 (soft): {type(e).__name__}: {e}")
        return None
    try:
        d = extract_json(raw)
    except Exception:
        return None
    if not isinstance(d, dict) or not d.get("occurred"):
        return None
    return d


# ─── 記録の書込 (per-window file、path-injection 不能) ─────────
def render_record(rec: dict, session_id: str, day: str, sensitive: bool) -> str:
    def _b(items):
        return "\n".join(f"- {_clean(x)}" for x in (items or []) if str(x).strip()) or "- (なし)"
    conf = "\nconfidential: true" if sensitive else ""
    ev = _clean(rec.get("umiyama_evidence") or "")
    ev_block = f"\n## 根拠にした海山の発話\n> {ev}\n" if ev else ""
    return f"""---
clone_visibility: private
domain: personal
project: dev{conf}
source: claude-code session {session_id}
imported: {day}
tags: [開発, 判断, dev-journal, 癖]
---

# {_clean(rec.get('title') or '開発判断')[:60]}

## 指示・依頼 (海山)
{_clean(rec.get('instruction') or '(記録なし)')}

## 判断
{_clean(rec.get('decision') or '(記録なし)')}

## 根拠
{_clean(rec.get('rationale') or '(記録なし)')}

## 結果
{_clean(rec.get('outcome') or '継続中')}
{ev_block}
## 観測された癖・判断軸 (海山の発話に根拠。※人格へは自動昇格しない=/dev advisor 用)
{_b(rec.get('patterns'))}

## 関連コミット
{_b(rec.get('commits'))}
"""


def write_record(rec: dict, session_id: str, day: str, offset: int, sensitive: bool) -> Path:
    DEV_DIR.mkdir(parents=True, exist_ok=True)
    sid8 = re.sub(r"[^a-z0-9]", "", session_id.lower())[:8] or "session"
    path = (DEV_DIR / f"{day}-{sid8}-{int(offset)}.md").resolve()
    if not str(path).startswith(str(DEV_DIR) + os.sep):   # path-injection 最終防御
        raise ValueError(f"unsafe record path: {path}")
    path.write_text(render_record(rec, session_id, day, sensitive), encoding="utf-8")
    return path


# ─── run ─────────────────────────────────────────────────────
def _find_sessions() -> list[Path]:
    out: list[Path] = []
    for d in SESSION_DIRS:
        if d.exists():
            out += list(d.glob("*.jsonl"))   # top-level のみ (subagents/ は除外)
    return sorted(out, key=lambda p: p.stat().st_mtime, reverse=True)


async def run(*, dry_run: bool = False, limit: int | None = None,
              session: str | None = None, llm=None) -> dict:
    st = _load_state()
    seen = st.setdefault("seen", {})
    cap = limit if limit is not None else MAX_PER_RUN
    processed, written, skipped = 0, 0, 0
    stats: dict = {"llm_fail": 0}
    for path in _find_sessions():
        sid = path.stem
        if session and sid != session:
            continue
        prev = seen.get(sid, {})
        size = path.stat().st_size
        if session:
            start = 0                                # 明示指定 = 頭から backfill
        elif sid not in seen:
            seen[sid] = {"offset": size, "mtime": path.stat().st_mtime}  # 初見=前向き捕捉
            skipped += 1
            continue
        else:
            start = int(prev.get("offset", 0))
            if size <= start:                        # 新規増分なし
                continue
        if processed >= cap:
            logger.info(f"cap {cap} 到達、残りは次回 cron で drain")
            break
        info = read_new(path, start)
        seen[sid] = {"offset": info["end"], "mtime": path.stat().st_mtime}
        win = info["window"]
        if len(win) < MIN_NEW_CHARS or not DEV_SIGNAL_RE.search(win):
            skipped += 1                              # 短すぎ / PB 開発に非関連 (雑務)
            continue
        processed += 1
        sensitive = bool(SENSITIVE_RE.search(win))
        if dry_run:
            logger.info(f"  [dry] {sid[:8]} date={info['date']} chars={len(win)} "
                        f"sensitive={sensitive}")
            continue
        rec = await distill(win, llm=llm, stats=stats)
        if rec:
            p = write_record(rec, sid, info["date"], info["end"], sensitive)
            written += 1
            logger.info(f"  記録: {p.name}  「{rec.get('title')}」 "
                        f"patterns={len(rec.get('patterns') or [])} sensitive={sensitive}")
        else:
            logger.info(f"  判断なし/soft-skip: {sid[:8]}")
    if not dry_run:
        _save_state(st)
    # ★2026-07-25 §1.18 loud-fail: 蒸留を試みたのに全滅 (LLM 断/URL 誤り) は取込の silent 死。
    #   ok=False は「処理対象があったのに 1 件も蒸留できなかった」時のみ (対象ゼロ=正常なので ok)。
    #   1 実行 1 箇所で記録 (streak 相殺を避ける)。cron からは cron_env.sh source 済で呼ばれる。
    ok = not (processed > 0 and stats["llm_fail"] >= processed)
    return {"ok": ok, "processed": processed, "written": written,
            "skipped": skipped, "llm_fail": stats["llm_fail"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Claude Code 開発セッション → personal/dev (v2, 増分)")
    ap.add_argument("--dry-run", action="store_true", help="検出だけ (蒸留/書込しない)")
    ap.add_argument("--limit", type=int, default=None, help=f"1回の上限 (default {MAX_PER_RUN})")
    ap.add_argument("--session", help="特定 session-id を頭から backfill (state 無視)")
    ap.add_argument("--push", action="store_true",
                    help="§1.18 loud_fail 通知も出す (cron 用)")
    a = ap.parse_args()
    r = asyncio.run(run(dry_run=a.dry_run, limit=a.limit, session=a.session))
    print(r)
    if a.push and not a.dry_run:
        try:
            from clone_improve_lib import loud_fail
            loud_fail("dev_journal_sync", bool(r.get("ok")),
                      f"蒸留 {r.get('processed')} 件中 LLM 失敗 {r.get('llm_fail')} 件 "
                      f"(書込 {r.get('written')})",
                      threshold=2, cooldown_h=24.0)
        except Exception as e:                      # 通知失敗で取込自体は落とさない
            logger.warning(f"loud_fail 通知失敗 (非致命): {e}")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
