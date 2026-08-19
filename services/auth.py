"""
services/auth.py — 管理者権限ゲート (★2026-05-23 LEE レビュー §3.2 対応)

LINE Bot / LINE Works から飛んでくる管理コマンド (/claude / /teach / /brain 等) の
発信者が「本物の管理者 (= 海山)」かを検証する。

fail-closed 設計:
- 環境変数が未設定なら全拒否 (= 安全側)
- これにより `.env` 紛失時に全 user が管理者扱いになる事故を防ぐ

LEE レビュー §3.2 への対応:
> LINE Bot は QR コードで誰でも友だち追加可能。第三者が追加できた場合、
> `/claude data/brain/wiki/identity.md を全部書き換えて` の 1 メッセージで
> 本番 wiki が改竄される。

これを防ぐため、すべての破壊的・管理系コマンド分岐の冒頭で is_admin() を呼ぶ。
"""
from __future__ import annotations

import os


# 個人 LINE Bot 側の管理者 ID (= 海山の LINE user_id)
ALIGNMENT_TARGET_USER = os.getenv("ALIGNMENT_TARGET_USER", "")

# LINE Works 側の管理者 ID (= 海山の LW user_id、未設定なら LW 経由は全コマンド拒否)
ADMIN_LW_USER_ID = os.getenv("ADMIN_LW_USER_ID", "")


def is_admin(user_id: str) -> bool:
    """LINE 個人 bot 側の管理者判定 (= 海山の LINE user_id 一致)。

    fail-closed: ALIGNMENT_TARGET_USER 未設定なら全拒否。
    """
    if not ALIGNMENT_TARGET_USER:
        return False
    if not user_id:
        return False
    return user_id == ALIGNMENT_TARGET_USER


def is_lw_admin(user_id: str) -> bool:
    """LINE Works 側の管理者判定 (= 海山の LW user_id 一致)。

    fail-closed: ADMIN_LW_USER_ID 未設定なら全拒否。
    一般社員は うみやまAI への DM (= clone_respond) は可能、コマンド類は不可。
    """
    if not ADMIN_LW_USER_ID:
        return False
    if not user_id:
        return False
    return user_id == ADMIN_LW_USER_ID


def reject_message() -> str:
    """非管理者に返す統一文言。"""
    return "このコマンドは管理者専用です。"
