"""
test_pdf_ocr_routing.py — 画像化 PDF の Vision-OCR fallback の
ルーティング/除外ロジック (純粋関数、LLM・fitz 不要) の回帰テスト。

★2026-05-19: OCR は画像化 PDF の最終手段だが、売上数値系ファイルは
スクレイパーが権威ソースなので OCR しない (誤読を精度クリティカル領域に
注入しない = CLAUDE.md 信頼性原則)。その境界が崩れないことを保証する。

実行: python3 -m pytest tests/test_pdf_ocr_routing.py -v
"""
from pathlib import Path

import pytest

from content_extractor import (
    _pdf_extract_was_empty,
    _should_skip_ocr_for_sales,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        # 売上数値系 = OCR スキップ (スクレイパーが権威ソース、冗長+誤読リスク)
        ("2_20260511_営業数値.pdf", True),
        ("2_20260519_営業数値.pdf", True),
        ("売上速報_2026.pdf", True),
        ("売上日報.pdf", True),
        ("月次売上_202604.pdf", True),
        ("Sales_Figures_Q1.pdf", True),
        ("sales-report.pdf", True),
        ("Sales Data 2026.pdf", True),
        # 質的/ナラティブ文書 = OCR 対象 (固有の価値、権威ソース無し)
        ("1_20260519_議事録.pdf", False),
        ("3_20260519_各部署KPI進捗.pdf", False),
        ("組織図_2026.pdf", False),
        ("契約書_OWNDAYS.pdf", False),
        ("経営会議メモ.pdf", False),
    ],
)
def test_should_skip_ocr_for_sales(name, expected):
    assert _should_skip_ocr_for_sales(Path(f"/tmp/{name}")) is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("[PDF text-extract empty] file='x.pdf', pages=1. 画像化...", True),
        ("  [PDF text-extract empty] 先頭に空白あり", True),
        ("\n[PDF text-extract empty] 改行始まり", True),
        ("普通に抽出できた本文テキストです。", False),
        ("[PDF: 2/3 ページからテキスト抽出] ...本文...", False),
        ("[PDF text mojibake suspected — ...]", False),
        ("", False),
        (None, False),
    ],
)
def test_pdf_extract_was_empty(text, expected):
    assert _pdf_extract_was_empty(text) is expected


def test_sales_skip_is_substring_not_anchored():
    """日付 prefix 付きでも (例 2_20260511_営業数値.pdf) 確実に検出されること
    (実際の Monday Dash 命名規則。アンカー縛りだと取り逃す回帰を防ぐ)。"""
    assert _should_skip_ocr_for_sales(Path("99_99999999_営業数値_最終版.pdf")) is True
