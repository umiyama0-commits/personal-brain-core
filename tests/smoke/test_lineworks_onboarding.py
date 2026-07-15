"""tests/smoke/test_lineworks_onboarding.py — 初回 welcome の内容 + 配線 pin.

★2026-07-11 採用/定着レビュー #1: welcome が M4 転換と正反対を宣言していた回帰を防ぐ。
"""
from __future__ import annotations

from pathlib import Path

from services import lineworks_onboarding as ob

_ROOT = Path(__file__).resolve().parents[2]


def test_welcome_has_no_anti_m4_language():
    """旧 welcome の「引き受けない」宣言 (資料/議事録/まとめ作業拒否) が残っていない。"""
    t = ob.WELCOME_TEXT
    assert "引き受けない" not in t
    assert "資料・メール・議事録・企画書の作成" not in t
    assert "分析やまとめ作業の代行" not in t


def test_welcome_advertises_m4_and_boundaries():
    """M4 代行を案内しつつ境界 (外部AI推奨+プロンプト作成 / 人事文書NG) を保持。"""
    t = ob.WELCOME_TEXT
    assert "簡単な作業の代行" in t
    assert ("依頼プロンプト" in t) or ("プロンプトはこちらで作る" in t)
    assert "人事評価" in t and "代筆しない" in t
    # 施設/商圏・データ即答も案内 (全社公開したコア機能)
    assert "業務データの即答" in t


def test_example_buttons_fit_label_limit_and_are_green():
    """例文ボタンは LW label 20 chars 制約内 + 未取込の制度系を含まない (green のみ)。"""
    assert 2 <= len(ob.EXAMPLE_QUERIES) <= 4
    for q in ob.EXAMPLE_QUERIES:
        assert len(q) <= 20, q
    # 制度系 (Drive 403 で未 green) は今は載せない
    joined = "".join(ob.EXAMPLE_QUERIES)
    assert "公休" not in joined and "産休" not in joined and "副業" not in joined


def test_main_wires_send_welcome_at_both_sites():
    """main.py が旧定数直送でなく service の send_welcome を両接触点で呼ぶ。"""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "LINEWORKS_WELCOME" not in src, "旧定数が残存 (移設漏れ)"
    assert src.count("from services.lineworks_onboarding import send_welcome") == 2
    assert src.count("await send_welcome(http, user_id)") == 2
