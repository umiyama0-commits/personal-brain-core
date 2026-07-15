"""tests/test_regulation_override.py — ★2026-07-03 P2a 規程原文 override の回帰テスト。

§1.9 の DEFAULT_EXCLUDE は record (評価記録/相談ログ/個人データ) を止める目的だが、
話題語 name match で rulebook (公開規程原文) も誤 block していた。override の
「規程 marker + record/secret marker 無し」二条件と fail-safe 側の維持を固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ★2026-07-10 (世界基準評価 #3): gdrive_sync は import 時に google-api libs を要求する。
#   CI 最小依存 / MacBook では未導入で、bare import だと collection ERROR となり
#   `pytest tests/` 全体を中断させていた (skip でなく error = 他 test も巻き添え)。
#   importorskip でモジュール単位の graceful skip にする。
pytest.importorskip("google.auth", reason="gdrive_sync が google-api libs を要求 (最小依存では skip)")

from gdrive_sync import (  # noqa: E402
    _check_regulation_doc_override,
    is_confidential_file,
)


# ─── 実 corpus (4_人事関係規程 等) の公開規程が通ること ───
def test_real_regulation_files_pass():
    for name in [
        "【規程_人事】 社員給与規程_20260101.pdf",        # 給与 hit → override
        "【規程_人事】育児・介護休業規程_20251001.pdf",   # 休業 hit → override
        "【規程_人事】懲戒手続に関する細則_20260101.pdf",  # 懲戒 hit → override
        "【規程_業務】個人情報保護規程",                   # 個人情報 hit → override
        "【規程_業務】内部通報規程",                       # 通報 hit → override
        "社宅利用規定（20160315）.pdf",                    # 規定 (別表記) + block hit 無し
    ]:
        assert _check_regulation_doc_override(name) or not is_confidential_file({"name": name})[0], name
        # override 経由でも直接 pass でも、最終判定が「機密でない」こと
        assert is_confidential_file({"name": name})[0] is False, name


# ─── record/secret は規程 marker があっても block 維持 (fail-safe) ───
def test_records_stay_blocked_even_with_regulation_marker():
    for name in [
        "懲戒処分記録_規程違反者一覧.xlsx",   # 記録 hit → deny
        "相談対応ログ 規程まとめ.xlsx",       # 相談対応 + ログ → deny
        "給与一覧 個人別 規程.xlsx",          # 個人別 → deny
        "健康診断結果 規則対応.xlsx",         # 結果 → deny
        "人事評価規程 運用評価シート.xlsx",   # 評価 → deny
        "機密 就業規則 改訂案.pdf",           # 機密 → deny
        # ★cross-check reviewer C-1: 「人の集合」文書 (一覧/リスト/シート/考課/予定表)
        "給与一覧(規程改定版).xlsx",          # 一覧 → deny
        "懲戒処分者一覧_規程違反.xlsx",       # 一覧 → deny
        "退職者リスト 就業規則対応.xlsx",     # リスト → deny
        "考課表_規程準拠.xlsx",               # 考課 → deny
        "採用面接シート(規程様式).xlsx",      # シート → deny
        "健康診断予定表 規則.xlsx",           # 予定表 → deny
        # ★DA-2: 個別事案系 marker
        "懲戒処分一覧_規程対応.xlsx",         # 処分+一覧 → deny
        "給与規程_支給額明細.xlsx",           # 支給額+明細 → deny
        "退職金規程_計算例.xlsx",             # 計算例 → deny
        "メンタルヘルス相談対応規程.pdf",     # 相談対応 (§1.9(k)) → deny
    ]:
        assert _check_regulation_doc_override(name) is False, name
        assert is_confidential_file({"name": name})[0] is True, name


# ─── 規程 marker が無い record は従来どおり block ───
def test_non_regulation_records_unchanged():
    for name in ["人事評価 2026.xlsx", "給与一覧 全社員.xlsx", "面談記録_5月.docx"]:
        assert is_confidential_file({"name": name})[0] is True, name


# ─── 従来 pass していた無害 file は無影響 ───
def test_benign_files_unchanged():
    for name in ["Monday Dash 2026-06.pdf", "FY27 AOP サマリー.xlsx"]:
        assert is_confidential_file({"name": name})[0] is False, name
