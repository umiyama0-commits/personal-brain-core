#!/usr/bin/env python3
"""prompt_version.py — clone prompt の content hash (eval を変更の因果に紐付ける)。

★2026-06-08 システム評価 LLMOps G2: prompt が version 管理されておらず、eval_summary /
regression / A-B のどの数字も「どの prompt 版で測ったか」を後から再現できなかった。
non-deterministic な LLM で因果を取り戻すため、prompt 定義の short content hash を eval
レコードに刻む。

prompt 定義 = brain_wiki.py の CLONE_PROMPT / CLONE_PUBLIC_PROMPT (triple-quoted) +
data/brain/wiki/style/few-shot-examples-v1.json。brain_wiki.py は import せず text として
読み regex 抽出する (= import 重量回避 + 他者の同ファイル編集と非衝突)。抽出失敗時は
whole-file hash に fallback (coarse だが stable)。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN_WIKI = ROOT / "brain_wiki.py"
FEWSHOT = ROOT / "data" / "brain" / "wiki" / "style" / "few-shot-examples-v1.json"

_PROMPT_NAMES = ("CLONE_PROMPT", "CLONE_PUBLIC_PROMPT")


def _extract_prompts(src: str) -> str:
    """brain_wiki.py から CLONE_PROMPT / CLONE_PUBLIC_PROMPT の triple-quoted 本体を抽出。"""
    parts = []
    for name in _PROMPT_NAMES:
        m = re.search(rf'{name}\s*=\s*"""(.*?)"""', src, re.DOTALL)
        if m:
            parts.append(m.group(1))
    return "\n---\n".join(parts)


def prompt_version() -> str:
    """prompt 定義 (CLONE_PROMPT + CLONE_PUBLIC_PROMPT + few-shot) の 12 桁 content hash。"""
    h = hashlib.sha256()
    try:
        src = BRAIN_WIKI.read_text(encoding="utf-8")
        extracted = _extract_prompts(src)
        # 抽出できれば prompt のみ、失敗なら whole-file (= 少なくとも version は動く)
        h.update((extracted if extracted else src).encode("utf-8"))
    except Exception:
        pass
    try:
        h.update(FEWSHOT.read_bytes())
    except Exception:
        pass
    return h.hexdigest()[:12]


if __name__ == "__main__":
    print(prompt_version())
