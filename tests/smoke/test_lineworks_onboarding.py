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
    """例文ボタンは LW label 20 chars 制約内。
    ★2026-08-10 更新: 規程 54 PDF は取込済 + 索引 reconcile 済のため、
    制度系 (公休) を例文に **含める** のが正 (旧: Drive 403 時代は除外していた)。
    制度質問は社員の頻出領域なのに入口に出ていなかった = 44% 無言離脱の一因。"""
    assert 2 <= len(ob.EXAMPLE_QUERIES) <= 4
    for q in ob.EXAMPLE_QUERIES:
        assert len(q) <= 20, q
    joined = "".join(ob.EXAMPLE_QUERIES)
    assert "公休" in joined, "規程FAQ の例文が入口に無い"
    assert "売上" in joined, "コアの売上例文が消えている"


def test_main_wires_send_welcome_at_both_sites():
    """main.py が旧定数直送でなく service の send_welcome を両接触点で呼ぶ。"""
    src = (_ROOT / "main.py").read_text(encoding="utf-8")
    assert "LINEWORKS_WELCOME" not in src, "旧定数が残存 (移設漏れ)"
    # ★2026-08-10: 3 箇所目 = 「利用開始」ボタン受け (既存ユーザの押し直しに例文を再掲。
    #   初回はテキストを LLM に流さず welcome 済みのため無応答)
    assert src.count("from services.lineworks_onboarding import send_welcome") == 3
    assert src.count("await send_welcome(http, user_id)") == 3
