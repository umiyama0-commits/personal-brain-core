"""
brain_wiki_helpers/ontology.py — 記憶層オントロジー (★2026-07-05 Phase 0)

wiki の path から認知科学の記憶層 (エピソード/意味/手続き/核) を **決定論で導出**する
単一の真実源 (pure function、domain.py と同パターン)。frontmatter 非依存・ファイル書込ゼロ:
「LLM も人間も維持しない層」なので Karpathy 判定基準 (誰が維持するか) を無条件に通る。

用途: brain_graph.py の記憶層配色 (Phase 0)。将来の intent routing / bridge_proposer の素材 (Phase 1+)。
retrieval には接続しない (ADR §3 Phase 3 封印)。
注意: 入力は WIKI_DIR **相対** path。絶対 path や不正値は semantic に落ちる (total 関数、例外なし) —
呼び出し側の誤用を隠すので、新規 caller は rel を渡すこと (code-review 2026-07-05 D-2)。
詳細: docs/decisions/2026-07-05-wiki-ontology-multilayer.md
"""
from __future__ import annotations

# 単一真実源の共有 (★code-review 2026-07-05 reuse-1/2): _parts の path 正規化と
# CORE root files 定義は domain.py と分岐させない (drift = 深層 private 判定と層判定が
# 同じ rel で食い違う静かな事故の元)。
from brain_wiki_helpers.domain import CORE_FILES as _DOMAIN_CORE_FILES
from brain_wiki_helpers.domain import _parts

# ── 記憶層 (認知科学の三分法 + 人格核) ──
#   episodic   = 一回性の出来事記録 (海馬の速い符号化)。時刻を持ち非破壊で積まれる
#   semantic   = 反復・compile で抽象化された一般知識 (皮質の遅い固定記憶)
#   procedural = 判断のやり方・文体・反射 (手続き記憶)。クローン人格の挙動を決める
#   core       = 人格の核 (identity/style/thinking)。全ドメイン共有の基盤 (§1.17 Core 層の根)
LAYER_EPISODIC = "episodic"
LAYER_SEMANTIC = "semantic"
LAYER_PROCEDURAL = "procedural"
LAYER_CORE = "core"

LAYERS = (LAYER_CORE, LAYER_PROCEDURAL, LAYER_SEMANTIC, LAYER_EPISODIC)

# dir 先頭 (小文字比較) → 層。ここに無い dir は semantic (wiki の既定 = 意味記憶) に落ちる。
# ★deep-private (personal/ interview/) にも層を与える: 上流 (brain_graph 等) が
#   is_deep_private_rel で除外済みでも、本関数は全 rel に対して total であるべき (fail 無し)。
_LAYER_BY_DIR = {
    # episodic — 出来事・記録
    "meetings": LAYER_EPISODIC,
    "sales": LAYER_EPISODIC,
    "interview": LAYER_EPISODIC,   # 人格深層の対話記録 (graph には出ない、層としては episodic)
    "personal": LAYER_EPISODIC,    # 非OWNDAYS PJ の記録 (同上)
    # semantic — 知識・分析・嗜好・決定・エンティティ
    "knowledge": LAYER_SEMANTIC,
    "analysis": LAYER_SEMANTIC,
    "hobbies": LAYER_SEMANTIC,
    "decisions": LAYER_SEMANTIC,
    "people": LAYER_SEMANTIC,
    "projects": LAYER_SEMANTIC,
    # procedural — 文体・判断軸
    "style": LAYER_PROCEDURAL,
    "judgment": LAYER_PROCEDURAL,
}

# root 直下の人格核 = domain.CORE_FILES から導出 (小文字化して case-variant path にも一致)
_CORE_ROOT_FILES = frozenset(f.lower() for f in _DOMAIN_CORE_FILES)

# 記憶層の低彩度パレット (brain_graph GROUP_COLORS と同系統の解剖学的トーン、★2026-06-20 方針)
#   core       → dusty garnet (人格の核 = hub ニューロン)
#   procedural → muted sage (運動野/小脳のイメージ)
#   semantic   → muted steel-blue (皮質の固定記憶)
#   episodic   → muted ochre (海馬の新しい記録)
LAYER_COLORS = {
    LAYER_CORE:       {"bg": "#A85A6B", "bd": "#6E3A48"},
    LAYER_PROCEDURAL: {"bg": "#6B9080", "bd": "#476155"},
    LAYER_SEMANTIC:   {"bg": "#5A86A0", "bd": "#3A5A70"},
    LAYER_EPISODIC:   {"bg": "#B89760", "bd": "#856A3E"},
}

LAYER_LABELS = {
    LAYER_CORE: "核 (人格)",
    LAYER_PROCEDURAL: "手続き (文体・判断)",
    LAYER_SEMANTIC: "意味 (知識・嗜好)",
    LAYER_EPISODIC: "エピソード (出来事)",
}

# ── 型付きリレーションの閉じた語彙 (★Phase 1、ADR §3) ──
# 追加はこのファイルの編集必須 (= レビューが入る)。未知の relation は "related" に落とす
# (compile 由来の自由文 relation をそのまま権威表示しない = 捏造エッジ抑制の一部)。
RELATIONS = ("related", "evidence_for", "influences", "supersedes")
DEFAULT_RELATION = "related"


def normalize_relation(rel: str) -> str:
    """relation 文字列 → 閉じた語彙へ正規化 (未知/自由文は related)。"""
    r = str(rel or "").strip().lower()
    return r if r in RELATIONS else DEFAULT_RELATION


def layer_of(rel) -> str:
    """WIKI_DIR 相対 path → 記憶層。total function (どんな入力でも層を返す、例外なし)。

    root 直下の CORE files → core、dir 先頭一致 → 対応層、未知は semantic (既定)。
    比較は小文字 (★code-review 2026-07-05 A-3: node_kind_of と正規化を揃える —
    APFS の case-variant path で層だけズレる silent 誤分類を防ぐ)。
    """
    p = _parts(rel)
    if not p:
        return LAYER_SEMANTIC
    if len(p) == 1:
        return LAYER_CORE if str(p[0]).lower() in _CORE_ROOT_FILES else LAYER_SEMANTIC
    return _LAYER_BY_DIR.get(str(p[0]).lower(), LAYER_SEMANTIC)


def node_kind_of(rel) -> str:
    """ノードの種別: 'index' | 'decision' | 'analysis' | 'note'。

    graph の視覚減衰用 (index はハブ骨格なので目立たせない、ADR §1 星型対策の表示側)。
    ★名前は node_kind: frontmatter の `type:` は style_extractor が占有 (ADR §5-1) — 本関数は
    frontmatter を書かない導出値だが、混同を避けるため語自体を分ける。
    """
    p = _parts(rel)
    if not p:
        return "note"
    if str(p[-1]).lower() in ("index.md", "_index.md"):
        return "index"
    head = str(p[0]).lower()
    if head == "decisions":
        return "decision"
    if head == "analysis":
        return "analysis"
    return "note"
