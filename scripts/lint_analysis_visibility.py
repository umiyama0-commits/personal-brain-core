#!/usr/bin/env python3
"""lint_analysis_visibility.py — analysis/ wiki の public 昇格 lint (cross-check S2).

共有 emitter (build_analysis_wiki) を通せば public は allow_public co-sign を強制されるが、
emitter を通さず手動で `clone_visibility: public` を書く human error は素通りする。
それを commit/deploy 前に loud-fail で止める。public にするには本文に `<!-- ALLOW_PUBLIC: <理由> -->`
の明示 co-sign コメントを必須にする (意図的 public の friction)。

exit: 0 = OK / 1 = co-sign 無き public 検出。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS = ROOT / "data" / "brain" / "wiki" / "analysis"


def main() -> int:
    if not ANALYSIS.exists():
        print("✓ analysis/ なし (lint skip)")
        return 0
    bad = []
    n = 0
    for f in sorted(ANALYSIS.glob("*.md")):
        n += 1
        txt = f.read_text(encoding="utf-8")
        if "clone_visibility: public" in txt and "ALLOW_PUBLIC" not in txt:
            bad.append(f.name)
    if bad:
        print(f"❌ analysis/ に co-sign 無き public wiki: {bad}", file=sys.stderr)
        print("   分析PJ は機密の可能性が高く、社員 bot への公開は事故源 (cross-check S2)。", file=sys.stderr)
        print("   意図的に公開する場合のみ本文に `<!-- ALLOW_PUBLIC: <理由> -->` を追記すること。", file=sys.stderr)
        return 1
    print(f"✓ analysis/ visibility lint OK ({n} files、co-sign 無き public なし)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
