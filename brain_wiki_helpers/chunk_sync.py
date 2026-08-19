"""chunk_sync — 再索引で「余剰 chunk だけ」を消すためのプラン (★2026-08-14 ADR 案 A-2、純関数)

**肥大の真因 (2026-08-15 に chromadb 1.5.8 の実コードと実機実験で確定)**:
犯人は「内容が変わること」ではなく、再索引のたびに
`delete(where={"file": rel})` → `upsert(全 chunk)` と**消してから入れ直していた**こと。

- 既存 id への `upsert` は、chroma の Rust segment が `id_to_label` から**既存ラベルを再利用**し
  (`local_hnsw.rs`)、hnswlib 側も `updatePoint` で **in-place 更新**する (`cur_element_count` は増えない)。
  → 実測: 200 chunk を 12 サイクル upsert し続けても `data_level0.bin` は 34,400B のまま不変。
- ところが `delete` は `id_to_label` を落とし、`total_elements_added` は単調増加のみ。
  しかも削除スロットの再利用は、C++ バインディングが `replace_deleted` 引数を転送していないため
  **到達不能なデッドコード**になっている。→ 同条件の delete→upsert は 200 → 2,600 slot へ単調増加。

つまり **delete をやめれば肥大は止まる**。必要なのは「chunk 数が減った時に**余剰 id だけ**を消す」ことだけで、
内容ハッシュ id も差分 embedding も要らない (Karpathy: Keep It Simple。§1.11)。

本 module は **chroma に触らない純関数だけ**。副作用は `brain_index` 側。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChunkSyncPlan:
    """この file の索引をどう揃えるかの計画。

    - `upsert_ids` : 今回入れる id (既存ラベルは in-place 更新 = スロットを消費しない)
    - `delete_ids` : 索引から消すべき余剰 id (= chunk 数が減った分。ここだけ delete する)
    """
    upsert_ids: list[str] = field(default_factory=list)
    delete_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"upsert={len(self.upsert_ids)} del={len(self.delete_ids)}"


def plan_chunk_sync(existing_ids: list[str] | set[str], new_ids: list[str]) -> ChunkSyncPlan:
    """既存 id 集合と今回の id 列から、消すべき余剰 id を出す (純関数)。

    ★呼び出し側の責務: `existing_ids` が**漏れなく取得できた時だけ**このプランを使うこと。
    取得に失敗したまま「余剰なし」と判断すると、縮んだ file の**古い chunk が索引に残り続け**、
    消したはずの内容が検索に出る。取得できなかった場合は従来どおり file 単位の全削除に
    フォールバックする (肥大するが、正しさを落とさない)。
    """
    return ChunkSyncPlan(
        upsert_ids=list(new_ids),
        # 決定論的な順序 (ログと test の再現性のため)
        delete_ids=sorted(set(existing_ids) - set(new_ids)),
    )
