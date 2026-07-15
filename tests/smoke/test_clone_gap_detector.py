"""smoke: clone_gap_detector の分類/フィルタ(2026-07-01)。"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
import clone_gap_detector as g

def test_classify_B_store_sales():
    assert g._classify("東京都の店舗の月次平均売り上げは？") == "B-retrieval"
    assert g._classify("最近閉店した店舗はどこ？") == "B-retrieval"

def test_classify_A_historical_and_brand():
    assert g._classify("2008年の買収から現在までの規模対比を教えて") == "A-fill"
    assert g._classify("Mellerの本日までの売上本数を教えて") == "A-fill"

def test_question_filter_rejects_non_question():
    # 詩的独白・相槌は QUESTION に当たらない
    assert g.QUESTION.search("店舗運営だよ。") is None
    assert g.QUESTION.search("東京都の店舗の月次平均売り上げは？") is not None
    assert g.DATA.search("EBITDAはいくら？") is not None
