"""core_budget.py — core wiki 常駐の全体予算スケーラ (pure function)。

★2026-07-03 P3b (世界水準評価 最低次元 C+ の根治、ADR 2026-07-02-world-class-evaluation.md):
core 常駐は per-file target (priority×1500×intent boost) のみで **全体の上限が無く**、
実測 118K字 (general) が gate max_chars=90K を常時超過 → vector 検索が本番 100% skip していた。
本関数は「各 file の projected 長の合計が予算を超えたら比例縮小する」だけの純関数。
brain_wiki.build_context の core loop が truncate 前に呼ぶ。

設計判断:
- **比例縮小** (priority 比は per-file target が既に encode 済 = 縮小後も相対比が保存される)
- **floor で小 file を保護** (完全に消える file を作らない — 消すか残すかの選別は
  CORE_WIKI_REGISTRY の責務であって予算の責務ではない)
- 売上 critical の core_files_full (owndays-daily-sales/stores) は呼び出し側で予算から
  先取り控除 = 本関数の縮小対象外 (E2E 15/15 検証が守る決定論注入層は触らない)
- CORE_BUDGET_CHARS=0 (default) = 縮小なし従来挙動。有効化は .env (regression で計測しつつ段階降下)
"""
from __future__ import annotations


def scale_core_targets(
    projected: list[tuple[str, int]],
    budget_avail: int,
    floor: int = 800,
) -> dict[str, int]:
    """projected = [(name, min(len(content), target)), ...] の合計が budget_avail を
    超える場合のみ、比例縮小した per-file target dict を返す。超えなければ {} (= 縮小不要)。

    floor: 縮小後の per-file 下限。floor 適用で合計が budget を微超過し得る
    (= 全 file が floor 張り付きの極端ケース) が、それは「予算が非現実的に小さい」
    設定ミスであり、silent に file を消すより floor 保護を優先する。
    """
    if budget_avail <= 0:
        return {}
    total = sum(p for _, p in projected)
    if total <= budget_avail:
        return {}
    scale = budget_avail / total
    return {name: max(floor, int(p * scale)) for name, p in projected}
