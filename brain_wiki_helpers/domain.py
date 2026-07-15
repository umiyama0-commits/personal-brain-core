"""
brain_wiki_helpers/domain.py — 知識ドメイン registry (★2026-06-28)

★2026-06-28 (海山指示): システムの重心を「OWNDAYS 専用」から「海山丈司個人の Personal Brain」へ。
 - **Core 層** = project 非依存の海山丈司 基盤 (人格・文体・判断軸・趣向)。どのドメインでも「基盤」
   として使える。各PJの知見はここに**還流**する (還流の自動蒸留+承認は次段、ADR 参照)。
 - **OWNDAYS domain** = 事業の1つ (knowledge/ analysis/ decisions/ 等)。OWNDAYS 出力に出る。
 - **personal/<project>** = 非OWNDAYS の各PJ/投資 (個人PJ 等)。OWNDAYS 出力には出さない。
   /personal 専用モードが「Core + その PJ」で参照する。

ここは「ドメインとは何か」の **単一の真実源** (pure function)。判定は path 先頭で行い frontmatter 非依存
(fail-safe)。全 reader/indexer がこの 1 箇所を import = 除外漏れ・drift を防ぐ。
詳細: docs/decisions/2026-06-28-personal-domain-segregation.md / 2026-06-28-personal-brain-core-and-registry.md
"""
from __future__ import annotations

import re
from pathlib import Path

# ── Core 層 (project 非依存の基盤。OWNDAYS でも personal PJ でも共有=「基盤を使う」) ──
CORE_DIRS = ("style", "judgment", "hobbies")          # wiki/<dir>/ = 文体 / 判断軸 / 趣向
CORE_FILES = ("identity.md", "style.md", "thinking.md")  # wiki 直下 = 人格 / 文体 / 思考

# ── 非OWNDAYS PJ の親ディレクトリ。wiki/personal/<project>/ の各 subdir = 1 project ──
PERSONAL_DOMAIN = "personal"

# ── 人格深層の親ディレクトリ。alignment 蒸留 (_CATEGORY_WIKI) / /diary の行き先 ──
# ★2026-07-03 v3「脳の複製」で家族/弱さ/金/体が入る。海山自身の brain / 将来の self-clone 専用。
INTERVIEW_DOMAIN = "interview"

# ── 深層 private = OWNDAYS-facing 出力 (クローン/MCP/API/export/graph/索引一覧) に出さない path 群 ──
# ★2026-07-03 (v3 ADR DA R6): interview/ は従来 frontmatter clone_visibility: private の一枚防御
# だった → personal/ と同じ path 防御に統合。visibility-only filter の consumer が素通しする穴を塞ぐ。
DEEP_PRIVATE_DIRS = (PERSONAL_DOMAIN, INTERVIEW_DOMAIN)


def _parts(rel) -> tuple:
    try:
        return Path(rel).parts
    except Exception:
        return ()


def is_personal_rel(rel) -> bool:
    """WIKI_DIR 相対 path が personal ドメイン (非OWNDAYS) か。先頭ディレクトリ一致。

    例 'personal/example-project/plan.md' → True。空・不正は False (= OWNDAYS 既定で安全側)。
    """
    p = _parts(rel)
    return len(p) > 0 and p[0] == PERSONAL_DOMAIN


def is_core_rel(rel) -> bool:
    """rel が Core 層 (project 非依存の基盤) か。

    先頭が CORE_DIRS、または直下の CORE_FILES。personal/style/... は先頭が 'personal' なので
    **False** (= Core dir 名と衝突しない、DA cross-check)。
    """
    p = _parts(rel)
    if not p:
        return False
    if p[0] in CORE_DIRS:
        return True
    return len(p) == 1 and p[0] in CORE_FILES


def is_deep_private_rel(rel) -> bool:
    """rel が深層 private (= OWNDAYS-facing 出力禁止) か。先頭ディレクトリ一致。

    personal/ (非OWNDAYS PJ) + interview/ (人格深層、★2026-07-03 v3 ADR DA R6)。
    frontmatter 非依存の fail-safe path 判定。空・不正は False (= OWNDAYS 既定で安全側)。
    ★海山自身の admin 経路 (/personal モード・compact の private 読み・海山専用 vector recall)
    はこの判定の対象外 = 呼ばない。OWNDAYS-facing の出口だけがここを通る。
    """
    p = _parts(rel)
    return len(p) > 0 and p[0] in DEEP_PRIVATE_DIRS


def is_owndays_facing(rel) -> bool:
    """OWNDAYS 出力に出してよいか = 深層 private (personal/ + interview/) でないこと。

    Core も OWNDAYS-domain も True (Core は全ドメイン共有の基盤)。
    ★2026-07-03 (§1.17 規律② の設計債解消): 除外サイトは is_personal_rel 個別呼びでなく
    本関数 (= not is_deep_private_rel) に統一済。新しい「非 facing ドメイン」を足す時は
    DEEP_PRIVATE_DIRS に 1 語足せば全 chokepoint に波及する。
    """
    return not is_deep_private_rel(rel)


def domain_of(rel) -> str:
    """rel のドメイン名を返す: 'core' | 'personal/<project>' | 'interview' | 'owndays'。"""
    if is_core_rel(rel):
        return "core"
    p = _parts(rel)
    if p and p[0] == PERSONAL_DOMAIN:
        return "/".join(p[:2]) if len(p) >= 2 else PERSONAL_DOMAIN
    if p and p[0] == INTERVIEW_DOMAIN:
        return INTERVIEW_DOMAIN
    return "owndays"


def is_personal_path(path, wiki_dir) -> bool:
    """絶対 path が WIKI_DIR 配下の personal ドメインか (絶対 path 版、WIKI_DIR 外は False)。"""
    try:
        rel = Path(path).resolve().relative_to(Path(wiki_dir).resolve())
    except Exception:
        return False
    return is_personal_rel(rel)


def list_personal_projects(wiki_dir) -> list[str]:
    """wiki/personal/<project>/ の project 名一覧 (registry の自動発見)。"""
    base = Path(wiki_dir) / PERSONAL_DOMAIN
    if not base.exists():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())


def safe_project_slug(name: str) -> str:
    """PJ名 → 安全な dir slug ([a-z0-9-] のみ)。

    ★path injection 不可: 英数字以外 (/ . スペース 記号 全部) を - に潰し前後 - を除去。
    '../../etc' → 'etc'、'a/b' → 'a-b'、'..' → '' (= 呼び出し側で reject)。最大 60 字。
    """
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:60]


def personal_project_dir(wiki_dir, name):
    """wiki/personal/<slug>/ の絶対 path を返す。slug 空 or 解決後に personal/ 外なら None (fail-safe)。

    slug 自体に / . が無いので injection は構造的に不能だが、resolve 後の relative_to で二重に保証。
    """
    slug = safe_project_slug(name)
    if not slug:
        return None
    base = (Path(wiki_dir) / PERSONAL_DOMAIN).resolve()
    target = (base / slug).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def core_files(wiki_dir) -> list[Path]:
    """Core 層の「基盤の核」ファイル一覧 (project mode が読む)。存在するものだけ、決定論順。

    人格の要点を優先・コンパクトに curate (Reviewer/DA cross-check: hobbies は 1000+件で bloat):
      1. persona (identity/style/thinking .md)  2. judgment(判断軸、小・重要)
      3. style/ 直下 *.md (文体パターン。few-shot JSON 等は除外)  4. hobbies は **index 系のみ**(趣向の要約)
    is_core_rel (ドメイン判定) は style/judgment/hobbies 全体が Core だが、こちらは reader 用に核を絞る。
    """
    base = Path(wiki_dir)
    out: list[Path] = []
    for f in CORE_FILES:                      # 1. persona
        p = base / f
        if p.is_file():
            out.append(p)
    jd = base / "judgment"                     # 2. 判断軸
    if jd.is_dir():
        out.extend(sorted(jd.rglob("*.md")))
    sd = base / "style"                        # 3. 文体パターン (直下 *.md のみ)
    if sd.is_dir():
        out.extend(sorted(sd.glob("*.md")))
    hd = base / "hobbies"                      # 4. 趣向 (index 系のみ = 要約)
    if hd.is_dir():
        out.extend(sorted(p for p in hd.rglob("*.md") if p.name in ("index.md", "_index.md")))
    return out
