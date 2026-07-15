"""tests/test_salary_public_override.py — 給与集計データ override (★2026-05-26 海山指示)

「個人と紐付かない、公開されてる」 集計給与情報 (= 給与レンジ、給与体系、
SV/AM 給与テーブル、リーグ別店長給与 等) は機密 exclude を override で通す.
ただし個別 marker hit すれば override 拒否、評価/健康/採用系は引き続き block.

検証対象:
- gdrive_sync.is_confidential_file(file_dict)  -> (is_confidential, reason)
- gdrive_sync._check_salary_public_override(text) -> bool
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _f(name: str) -> dict:
    """Drive file dict のミニマル fixture (= 名前 only)."""
    return {"name": name, "parents": []}


@pytest.fixture
def mod():
    """gdrive_sync.py 全体は heavy import (google.auth 等) → 必要 symbol のみ AST 抽出 exec.

    抽出: DEFAULT_EXCLUDE_PATTERN / SALARY_PUBLIC_PATTERN / PERSONAL_MARKER_PATTERN
          + _check_salary_public_override / is_confidential_file
    """
    src = (REPO_ROOT / "gdrive_sync.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted_assign = {
        "DEFAULT_EXCLUDE_PATTERN",
        "SALARY_PUBLIC_PATTERN",
        "PERSONAL_MARKER_PATTERN",
        # ★2026-07-03 P2a: is_confidential_file が規程 override を参照するようになったため追加
        "REGULATION_DOC_PATTERN",
        "REGULATION_OVERRIDE_DENY",
    }
    wanted_func = {"_check_salary_public_override", "_check_regulation_doc_override",
                   "is_confidential_file"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in wanted_assign:
                    selected.append(node)
                    break
        elif isinstance(node, ast.FunctionDef):
            if node.name in wanted_func:
                selected.append(node)
    # Python 3.9 で `dict | None` 等 PEP 604 を parse 通すため __future__ を先頭に
    future_imp = ast.parse("from __future__ import annotations").body[0]
    minimod = ast.Module(body=[future_imp] + selected, type_ignores=[])
    code = compile(minimod, "<extracted-gdrive_sync>", "exec")
    ns: dict = {}
    exec(code, ns)

    class _Mod:
        pass
    m = _Mod()
    for name in wanted_assign | wanted_func:
        setattr(m, name, ns[name])
    return m


# ─── L1: SALARY_PUBLIC override で通る集計データ ─────
@pytest.mark.parametrize("name", [
    "給与レンジ.xlsx",
    "給与体系 2026.docx",
    "給与テーブル_全店長.xlsx",
    "給与表 2026年度.pdf",
    "報酬体系_全社.pdf",
    "報酬制度 2026.docx",
    "店長給与 リーグ別.xlsx",
    "店長報酬テーブル.xlsx",
    "SV給与テーブル.xlsx",
    "SV報酬 2026.pdf",
    "AM給与表 2026.xlsx",
    "AM報酬 リーグ別.pdf",
    "職位別 給与テーブル.xlsx",
    "役職別 給与レンジ.xlsx",
    "Salary Range 2026.xlsx",
    "Compensation Band Q2.pdf",
    "Pay Grade Table.xlsx",
])
def test_salary_public_override_passes(mod, name):
    """集計/公開 marker hit + 個別 marker 無 → 通過 (= 海山指示)."""
    is_conf, reason = mod.is_confidential_file(_f(name))
    assert not is_conf, f"通すべきだが block された: {name!r} reason={reason!r}"


# ─── L2: 個別 marker hit で override 拒否 (= 引き続き block) ─────
# 全 fixture は DEFAULT_EXCLUDE_PATTERN (= 「給与」「報酬」 等) hit が前提
# (= そうでない file は そもそも DEFAULT を 回避してるので override 議論の対象外)
@pytest.mark.parametrize("name", [
    "給与一覧 個人別.xlsx",
    "給与テーブル_社員別.xlsx",
    "給与体系_個別評価.xlsx",   # = SALARY_PUBLIC + 個別 marker hit → block
    "店長給与 氏名付き.xlsx",
    "AM給与 名簿.xlsx",
    "Salary Per Employee 2026.xlsx",
    "Compensation by Name.pdf",
])
def test_salary_public_override_rejected_when_personal_marker(mod, name):
    """SALARY_PUBLIC marker あっても 個別 marker hit → override 拒否で block."""
    is_conf, reason = mod.is_confidential_file(_f(name))
    assert is_conf, f"block すべきだが通った: {name!r}"


# ─── L3: SALARY_PUBLIC marker 無い 給与系 → 安全側 block 維持 ─────
@pytest.mark.parametrize("name", [
    "給与一覧 全社員.xlsx",       # SALARY_PUBLIC 無し + 「全社員」 で個別性 ある
    "給与 2026.xlsx",            # 単独「給与」 = 解釈曖昧、safe side で block
    "賃金台帳.xlsx",             # (g) 給与詳細 系
    "源泉徴収票 2026.pdf",       # (g)
])
def test_default_exclude_keeps_blocking_when_no_public_marker(mod, name):
    """SALARY_PUBLIC override が hit しなければ 既存 exclude で block 維持."""
    is_conf, _reason = mod.is_confidential_file(_f(name))
    assert is_conf, f"block すべきだが通った: {name!r}"


def test_retirement_regulation_now_passes_via_regulation_override(mod):
    """★2026-07-03 P2a 方針変更: 「退職金規程」は rulebook (公開規程原文) として通る。

    旧方針では (g) 退職金 hit で block していたが、6/15 決定 (公開規程の社内周知) +
    海山指示 P2a により REGULATION_DOC override で通す。record 系 (「退職金 支給額一覧」等)
    は deny marker で引き続き block (tests/test_regulation_override.py が cover)。
    """
    is_conf, _ = mod.is_confidential_file(_f("退職金規程.docx"))
    assert is_conf is False
    # record 版は block のまま
    is_conf2, _ = mod.is_confidential_file(_f("退職金規程_支給額一覧.xlsx"))
    assert is_conf2 is True


# ─── L4: 評価/健康/採用 系は引き続き block (= override 対象外) ─────
@pytest.mark.parametrize("name", [
    "人事評価 2026.xlsx",
    "個人評価シート.xlsx",
    "考課 Q2.pdf",
    "健康診断 結果.xlsx",
    "懲戒処分 通知.pdf",
    "履歴書_応募者.pdf",
    "採用面接 記録.docx",
])
def test_non_salary_confidential_still_blocked(mod, name):
    """評価/健康/採用 系は SALARY_PUBLIC override 対象外、引き続き block."""
    is_conf, _reason = mod.is_confidential_file(_f(name))
    assert is_conf, f"block すべきだが通った (override 対象外): {name!r}"


# ─── L4b: 相談 / 面談 / 個別 communication 系 (★2026-05-27 海山指示) ─────
# 「相談対応ログ」 が Drive 検索 top に出てた事象に基づき category (k) 追加
@pytest.mark.parametrize("name", [
    "相談対応ログ.xlsx",
    "相談記録 2026.docx",
    "相談ログ_Q1.xlsx",
    "相談履歴 全社.csv",
    "相談窓口 受付.xlsx",
    "ハラスメント相談 記録.docx",
    "メンタル相談 履歴.xlsx",
    "キャリア相談 ノート.docx",
    "個別相談 記録.pdf",
    "面談記録 2026-05.xlsx",
    "面談ログ_チーム.docx",
    "面談履歴 全社.csv",
    "個別面談 メモ.docx",
    "1on1 記録 2026.xlsx",
    "1 on 1 ログ.docx",
    "通報 受付ログ.xlsx",
    "内部通報 履歴.pdf",
    "Counseling Log 2026.xlsx",
    "Consultation Log Q2.csv",
    "Grievance Report.docx",
    "Harassment Report.pdf",
    "Whistleblow Cases.xlsx",
])
def test_consultation_communication_blocked(mod, name):
    """★2026-05-27 海山指示: 相談 / 面談 / 個別 communication 系は PII 高 risk → block."""
    is_conf, _reason = mod.is_confidential_file(_f(name))
    assert is_conf, f"相談 / 面談 系は block すべき: {name!r}"


# ─── L5: 通常 file (= 給与/評価系 無) は当然通る ─────
@pytest.mark.parametrize("name", [
    "Monday Dash 2026-05-26.xlsx",
    "WBR 2026 Q2.docx",
    "店舗一覧 マスター.xlsx",
    "売上 daily 2026-05.csv",
    "副業規程.docx",          # 規程 keyword (= 給与ではない)
])
def test_normal_file_passes(mod, name):
    """非機密 file は通常通り通る."""
    is_conf, reason = mod.is_confidential_file(_f(name))
    assert not is_conf, f"block されるべきでない: {name!r} reason={reason!r}"


# ─── L6: helper 単独 test ─────
def test_check_salary_public_override_helper(mod):
    """_check_salary_public_override の boolean 動作 直 verify."""
    # 集計 hit + 個別 無 → True
    assert mod._check_salary_public_override("給与レンジ.xlsx")
    assert mod._check_salary_public_override("店長給与 リーグ別.xlsx")
    assert mod._check_salary_public_override("SV給与テーブル.xlsx")
    # 集計 hit + 個別 hit → False
    assert not mod._check_salary_public_override("給与レンジ 個人別.xlsx")
    assert not mod._check_salary_public_override("給与テーブル 社員別.xlsx")
    # 集計 無 → False
    assert not mod._check_salary_public_override("給与 一覧.xlsx")
    assert not mod._check_salary_public_override("人事評価.xlsx")
    assert not mod._check_salary_public_override("")
