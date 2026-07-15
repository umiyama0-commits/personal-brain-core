#!/usr/bin/env python3
"""scripts/prompt_patches_compact.py — system_prompt_patches.json の統合 GC (★2026-07-05 監査)

背景 (prompt 監査 finding #11/#40/#41):
  self_improve (03:00 daily) の system_prompt_additions は言い換え重複で 196 件 ≈13K 字まで
  肥大し、毎 turn 個人アシスタント (main.py run_agent) の prompt に全量注入されていた。
  さらに「政府の公式発表を参照して回答」「業界平均から推定値を提供」等、捏造を招待する
  entry が混入。dead bucket (intent_keywords / drive_search_patterns、2026-06-07 reader 廃止)
  も残存。

本 script の 3 機能 (reflux と同じ propose-only 思想 = 無監督の LLM 書き戻しはしない):
  1. --gc-dead      : dead bucket を物理削除 (決定論、reader ゼロ確認済み。backup 作成)
  2. (default)      : LLM (cross-family judge) で 196 件 → canonical セット (~10-15 件) への
                      統合案を生成し、**pending 提案として保存するだけ** (書き戻さない)。
                      各既存 entry がどの canonical に対応するかの対応表を要求し、
                      対応不能 entry は retained として原文残置 (握り潰さない)。
  3. --approve <id> : 海山承認で書き戻し。提案時点から additions が変わっていたら
                      (= 夜間 append と競合) loud に abort して再提案を促す (lost update 防止)。

運用 (§1.14): patches.json は git tracked + Mac mini の 03:00 self_improve が書き換えるため、
実行は Mac mini 上で行う (MacBook 手編集 push はしない)。03:00-04:00 帯を避けて実行する。
★propose → approve は**同日中に完結** (夜間 self_improve の append/cap で base_sha が変わると
approve が abort する設計のため)。
★cap との相互作用 (DA cross-check): self_improve 側の上限 60 (SELF_IMPROVE_MAX_ADDITIONS) は
次の夜間 run で発動し既存 196 件の最古 136 件を dumb に drop する。「LLM 統合を先にやりたい」
場合は deploy 当日中に --gc-dead → propose → approve を回すこと (drop されても patches.json は
git tracked なので履歴から復元可能、影響は海山個人アシスタント経路のみ)。

実行:
  python3 scripts/prompt_patches_compact.py --gc-dead
  python3 scripts/prompt_patches_compact.py            # 統合案の生成 (propose-only)
  python3 scripts/prompt_patches_compact.py --list     # pending 提案一覧
  python3 scripts/prompt_patches_compact.py --approve <proposal-file 名>
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from clone_improve_lib import (  # noqa: E402
    call_llm, extract_json, IMPROVE_DIR, DATA_BRAIN, JST,
    pick_cross_family_judge,
)
from self_improve import _addition_denied  # noqa: E402  (deny-filter を canonical 出力にも適用)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("prompt_patches_compact")

PATCHES = DATA_BRAIN / "system_prompt_patches.json"
PROPOSAL_DIR = IMPROVE_DIR / "patches_compact"
DEAD_BUCKETS = ("intent_keywords", "drive_search_patterns")

COMPACT_PROMPT = """あなたは system prompt の編集者。以下は社内 AI アシスタントの
「自己改善による追加ルール」リスト ({n} 件)。夜間バッチが言い換え重複を大量に append した
結果で、実質的なテーマは少数。これを **canonical なルール集合 (最大 15 件、日本語、各 100 字以内)**
に統合してほしい。

【絶対条件】
- 新しいルールを発明しない。既存 entry の意味の統合・重複除去のみ
- 「外部ソース (公式発表 / データベース / web) を参照して回答」「推定値を作って提供」系の
  entry は canonical に**含めない** (捏造への招待のため廃棄対象)
- どの canonical にも対応付けられない固有の entry は無理に統合せず "retained" に index を挙げる
- 全 entry について対応表 (mapping) を出す: entry index → canonical index / "drop" (廃棄) / "retained"

【既存 entry (index: 内容)】
{entries}

【出力 (JSON only)】
```json
{{
  "canonical": ["<統合ルール 1>", "..."],
  "mapping": {{"0": 0, "1": "drop", "2": 1, "3": "retained", "...": "..."}},
  "drop_reasons": {{"1": "<30字以内>"}}
}}
```"""


def _sha_additions(adds: list[str]) -> str:
    return hashlib.sha256(json.dumps(adds, ensure_ascii=False).encode()).hexdigest()


def _load() -> dict:
    return json.loads(PATCHES.read_text(encoding="utf-8"))


def _backup() -> Path:
    ts = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
    dst = PATCHES.with_suffix(f".json.bak-{ts}")
    shutil.copy2(PATCHES, dst)
    return dst


def gc_dead() -> dict:
    """dead bucket (reader 廃止済み) を物理削除。決定論・LLM 不使用。"""
    patches = _load()
    removed = [k for k in DEAD_BUCKETS if k in patches]
    if not removed:
        return {"ok": True, "removed": [], "note": "dead bucket なし"}
    bak = _backup()
    for k in removed:
        del patches[k]
    PATCHES.write_text(json.dumps(patches, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"dead bucket 削除: {removed} (backup: {bak.name})")
    return {"ok": True, "removed": removed, "backup": bak.name}


async def propose() -> dict:
    """統合案を生成して pending 保存 (書き戻さない)。"""
    patches = _load()
    adds = patches.get("system_prompt_additions", [])
    if len(adds) < 20:
        return {"ok": True, "note": f"additions {len(adds)} 件 = 統合不要 (<20)"}

    entries = "\n".join(f"{i}: {a}" for i, a in enumerate(adds))
    model = pick_cross_family_judge()  # bot と別系列 (self-eval loop 回避、既存慣例)
    raw = await call_llm(COMPACT_PROMPT.format(n=len(adds), entries=entries[:40000]),
                         model=model, max_tokens=4000, temperature=0.0)
    data = extract_json(raw)
    raw_canonical = [c for c in data.get("canonical", []) if isinstance(c, str) and c.strip()]
    mapping = data.get("mapping", {})
    if not raw_canonical or not mapping:
        raise RuntimeError(f"統合案の生成失敗 (canonical={len(raw_canonical)}, mapping={len(mapping)}) — 再実行して")

    # ★Reviewer MAJOR-2 反映: deny-filter は「filter 前の index 空間」で判定する。
    # 先に filter すると mapping の index がシフトし、(a) 正当 canonical 宛が out-of-range で
    # retained に化ける (統合が骨抜き)、(b) denied canonical 宛がシフト後の別 canonical に
    # 「統合済み」扱いされ黙って消える。denied canonical 宛の entry は retained へ回す。
    denied_idx = {i for i, c in enumerate(raw_canonical) if _addition_denied(c)}
    canonical = [c for i, c in enumerate(raw_canonical) if i not in denied_idx]

    def _resolve(m):
        # ★Reviewer MAJOR-1 反映: LLM 出力の型ゆれを吸収 ("0" → 0)。
        # 認識できない値 (garbage) は安全側 = retained (原文残置、握り潰さない)
        if isinstance(m, str) and m.strip().lstrip("-").isdigit():
            return int(m.strip())
        return m

    retained = []
    for i, a in enumerate(adds):
        m = _resolve(mapping.get(str(i), "retained"))
        if m == "drop":
            continue  # LLM の明示廃棄 (drop_reasons + proposal JSON で人間可視、承認 gate 越し)
        if isinstance(m, int) and 0 <= m < len(raw_canonical) and m not in denied_idx:
            continue  # 有効な canonical に統合済み
        retained.append(a)  # "retained" / 不明値 / out-of-range / denied canonical 宛 → 原文残置
    retained = [a for a in retained if not _addition_denied(a)]

    new_adds = canonical + retained
    PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
    pid = datetime.now(JST).strftime("proposal-%Y%m%d-%H%M%S-%f.json")  # %f = 同秒衝突防止
    (PROPOSAL_DIR / pid).write_text(json.dumps({
        "status": "pending",
        "created": datetime.now(JST).isoformat(),
        "model": model,
        "base_sha": _sha_additions(adds),
        "before_count": len(adds),
        "after_count": len(new_adds),
        "canonical": canonical,
        "retained": retained,
        "mapping": mapping,
        "drop_reasons": data.get("drop_reasons", {}),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"統合案を保存: {pid} ({len(adds)} → {len(new_adds)} 件)。承認は --approve {pid}")
    return {"ok": True, "proposal": pid, "before": len(adds), "after": len(new_adds)}


def approve(pid: str) -> dict:
    """海山承認で書き戻し。提案後に additions が変わっていたら abort (lost update の大幅縮小 —
    sha 検証と write の間の ms 窓は残るため、03:00-04:00 帯を避ける運用と併用)。

    ★運用注意 (DA 指摘): 夜間 self_improve の append/cap で sha は毎晩変わり得る =
    **propose → approve は同日中に完結**が前提。翌日に持ち越すと abort → 再 propose になる。"""
    ppath = PROPOSAL_DIR / pid
    if not ppath.exists():
        return {"ok": False, "error": f"提案 file が無い: {pid} (--list で確認)"}
    prop = json.loads(ppath.read_text(encoding="utf-8"))
    if prop.get("status") != "pending":
        return {"ok": False, "error": f"status={prop.get('status')} (pending のみ承認可)"}
    patches = _load()
    adds = patches.get("system_prompt_additions", [])
    if _sha_additions(adds) != prop["base_sha"]:
        return {"ok": False, "error": "提案後に additions が変更されている (夜間 append と競合)。"
                                      "再提案して (旧提案は破棄が安全)"}
    bak = _backup()
    patches["system_prompt_additions"] = prop["canonical"] + prop["retained"]
    PATCHES.write_text(json.dumps(patches, ensure_ascii=False, indent=2), encoding="utf-8")
    prop["status"] = "applied"
    prop["applied_at"] = datetime.now(JST).isoformat()
    ppath.write_text(json.dumps(prop, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"適用: {prop['before_count']} → {len(patches['system_prompt_additions'])} 件 (backup: {bak.name})")
    return {"ok": True, "applied": len(patches["system_prompt_additions"]), "backup": bak.name}


def main() -> int:
    ap = argparse.ArgumentParser(description="system_prompt_patches.json の統合 GC (propose-only)")
    ap.add_argument("--gc-dead", action="store_true", help="dead bucket を物理削除")
    ap.add_argument("--list", action="store_true", help="pending 提案一覧")
    ap.add_argument("--approve", metavar="PROPOSAL", help="提案 file 名を承認・適用")
    a = ap.parse_args()
    if not PATCHES.exists():
        print(json.dumps({"ok": False, "error": f"{PATCHES} なし"}))
        return 1
    if a.gc_dead:
        r = gc_dead()
    elif a.list:
        items = sorted(PROPOSAL_DIR.glob("proposal-*.json")) if PROPOSAL_DIR.is_dir() else []
        r = {"ok": True, "proposals": [
            {"file": p.name, "status": json.loads(p.read_text(encoding='utf-8')).get("status")}
            for p in items]}
    elif a.approve:
        r = approve(a.approve)
    else:
        r = asyncio.run(propose())
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
