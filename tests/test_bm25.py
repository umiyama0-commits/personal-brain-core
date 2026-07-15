"""brain_wiki_helpers/bm25.py の test (tokenize / BM25 / RRF)。

★2026-06-08 hybrid search。固有名詞 exact match と RRF 融合を固定する。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain_wiki_helpers.bm25 import tokenize, BM25Index, rrf_fuse  # noqa: E402


def test_tokenize_alnum_and_cjk():
    toks = tokenize("TSA とは")
    assert "tsa" in toks            # 英数字は小文字 1 トークン
    assert "とは" in toks            # CJK bigram


def test_tokenize_proper_noun_preserved():
    toks = tokenize("FY27 の AOP")
    assert "fy27" in toks
    assert "aop" in toks


def test_bm25_ranks_matching_doc_first():
    docs = [
        ("a.md", "OWNDAYS の TSA について説明する文書"),
        ("b.md", "売上と客数の集計について"),
        ("c.md", "店舗開発の方針"),
    ]
    idx = BM25Index(docs)
    res = idx.search("TSA とは", top_n=3)
    assert res, "TSA を含む doc がヒットすべき"
    assert res[0][0] == "a.md"      # TSA を含む a.md が首位


def test_bm25_no_match_returns_empty():
    idx = BM25Index([("a.md", "売上の集計")])
    assert idx.search("まったく無関係なゼブラ x9z9q") == []


def test_bm25_idf_rewards_rare_term():
    # 全 doc に共通する語より希少語の方が効く
    docs = [
        ("a.md", "共通 共通 共通 JCS"),
        ("b.md", "共通 共通 共通"),
        ("c.md", "共通 共通 共通"),
    ]
    idx = BM25Index(docs)
    res = idx.search("JCS", top_n=3)
    assert res[0][0] == "a.md"


def test_rrf_fuse_basic():
    # dense: [a, b, c] / bm25: [c, a, d] → a が両方上位で融合首位寄り
    fused = rrf_fuse([["a", "b", "c"], ["c", "a", "d"]], k=60)
    assert fused[0] == "a"          # a は両リストで上位 → 最大
    assert set(fused) == {"a", "b", "c", "d"}


def test_rrf_only_in_one_list():
    fused = rrf_fuse([["a", "b"], ["x", "y"]], k=60)
    # a と x は同 rank1 → 同 score、b と y は同 rank2。全要素含む
    assert set(fused) == {"a", "b", "x", "y"}


def test_rrf_top_n():
    fused = rrf_fuse([["a", "b", "c", "d"]], k=60, top_n=2)
    assert fused == ["a", "b"]
