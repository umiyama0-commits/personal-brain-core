"""brain_wiki_helpers/voice_visibility.py — 音声アラインメントの interview 露出制御 (pure function)。

★2026-08-03 露出封鎖。web 経路 (Vapi Web SDK) は `vapi.start(cfg)` の仕様上、assistant config が
**ブラウザに平文 JSON で返る**。ゲートは `?token=` の URL クエリのみで、ブラウザ履歴 / access log /
Cloudflare log / LINE 転送に残る。実測では 深層 interview の末尾を集めた要約
(家族・弱さ・金・体・内的独白を含む) がそのまま出ていた。

電話経路 (assistant-request) は server→Vapi でブラウザを通らないため **全深度を維持**し、
**ブラウザ配送時のみ深層を除外**する。

★設計: **allowlist (fail-safe)**。初版は除外リスト (denylist) で書いたが、§1.15 Reviewer が
深層プロファイルの一次資料が素通りすることを
実証した。interview/ は人格深化で file が増え続ける場所なので、denylist は**新しい深層 file が
増えるたびに黙って漏れる**。既知の安全次元だけを通す形に反転し、未知の stem は既定で遮断する。

§1.12b により main.py には置かない (endpoint は wiring のみ)。
"""
from __future__ import annotations

from pathlib import Path

# ブラウザに平文で渡してよい interview 次元 (= interview/<stem>.md の stem)。
# 仕事文脈で語られる、社外に出ても実害の小さい層のみ。**ここに無い stem は全て遮断**。
# 追加は「ブラウザ履歴と access log に平文で残ってよいか」を基準に判断すること。
BROWSER_SAFE_DIMS = frozenset({
    "aesthetics",      # 美意識・デザイン観 (公開クローンでも語る層)
    "biography",       # 経歴 (対外的に語っている範囲)
    "embodiment",      # 身体化された作業習慣 (体調 = body-health とは別)
    "judgment",        # 判断軸
    "philosophy",      # 仕事哲学
    "reflex",          # 反射的な判断
    "relationships",   # 仕事上の関係性の作法
    "style",           # 文体・話し方
    "value-roots",     # 価値観の由来 (対外的に語っている範囲)
})


def interview_files_for_voice(idir: Path, *, browser_delivered: bool) -> list[Path]:
    """音声アラインメントの「これまでの話」に載せてよい interview file を返す。

    browser_delivered=True (web-config) のときは BROWSER_SAFE_DIMS のみ。
    fail-safe: 判定不能な入力は空リスト (載せない側に倒す)。
    """
    try:
        if not idir or not idir.exists():
            return []
        files = sorted(idir.glob("*.md"))
    except Exception:
        return []
    if not browser_delivered:
        return files
    return [f for f in files if f.stem in BROWSER_SAFE_DIMS]


def redact_summaries_for_browser(summaries: list[str], *, browser_delivered: bool) -> list[str]:
    """直近セッション要約の注入をブラウザ配送時は落とす。

    ★Reviewer H2: interview file を絞っても、`recent_session_summaries()` は次元フィルタ無しで
    「前回までの流れ」に入る。直近が family / shadow セッションなら要約経由でそのまま漏れる
    (要約は本文より短いだけで、深さは同じ)。session 単位で次元を判定する術が無いので、
    ブラウザ配送時は**要約ごと落とす**。web 経路の継続性は失われるが、web は主経路ではない
    (主経路は電話 = 全深度維持)。
    """
    if browser_delivered:
        return []
    return list(summaries or [])
