"""smoke: personal (非OWNDAYS) ドメイン分離の leak 証明 (★2026-06-28 海山指示)。

設計: wiki/personal/ 配下 = 海山の非OWNDAYS PJ/投資 (Example Garden 等)。OWNDAYS 出力
(公開クローン・コンサル・アナリスト・MCP・索引) からは全経路で除外、/personal 専用モードのみ参照。

ここでは「単一の真実源 (domain helper)」と、cross-check で最高リスクと判定された
コンサル brain_search (markdown rglob、visibility filter 無し) の除外を機械的に証明する。
network・LLM・chromadb 非依存 (純粋関数 + tmp ディレクトリ)。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "consultant"))

from brain_wiki_helpers.domain import (  # noqa: E402
    is_personal_rel, is_personal_path, PERSONAL_DOMAIN,
    is_core_rel, is_owndays_facing, domain_of, core_files, list_personal_projects,
    safe_project_slug, personal_project_dir,
)


def test_is_personal_rel_only_top_level_personal():
    assert PERSONAL_DOMAIN == "personal"
    assert is_personal_rel("personal/example-garden/plan.md") is True
    assert is_personal_rel("personal/x.md") is True
    # 非該当: 先頭が personal でないものは全て OWNDAYS 既定
    assert is_personal_rel("knowledge/owndays-vmv.md") is False
    assert is_personal_rel("analysis/ai-trends-owndays.md") is False
    assert is_personal_rel("personalish/x.md") is False        # 部分一致で誤判定しない
    assert is_personal_rel("decisions/personal/x.md") is False  # 非先頭は対象外
    assert is_personal_rel("") is False                          # fail-safe = OWNDAYS 既定


def test_is_personal_path_absolute(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "personal" / "pj").mkdir(parents=True)
    (wiki / "knowledge").mkdir(parents=True)
    p_personal = wiki / "personal" / "pj" / "a.md"
    p_owndays = wiki / "knowledge" / "b.md"
    p_personal.write_text("x"); p_owndays.write_text("y")
    assert is_personal_path(p_personal, wiki) is True
    assert is_personal_path(p_owndays, wiki) is False
    # WIKI_DIR 外は False (= OWNDAYS 既定で安全側、巻き込まない)
    assert is_personal_path(tmp_path / "outside.md", wiki) is False


def _seed_wiki(tmp_path):
    """tmp wiki に OWNDAYS file と personal file (秘密マーカー入り) を置く。"""
    wiki = tmp_path / "wiki"
    (wiki / "knowledge").mkdir(parents=True)
    (wiki / "personal" / "example-garden").mkdir(parents=True)
    # OWNDAYS 側 (引けてよい)
    (wiki / "knowledge" / "owndays.md").write_text(
        "---\nclone_visibility: public\n---\n# OWNDAYS\n売上 戦略 ZZTOPSECRET は OWNDAYS には無い。",
        encoding="utf-8")
    # personal 側 (秘密マーカー ZZTOPSECRET。OWNDAYS reader に絶対出てはならない)
    (wiki / "personal" / "example-garden" / "plan.md").write_text(
        "---\nclone_visibility: private\n---\n# Example Garden\n評価額と戦略 ZZTOPSECRET。",
        encoding="utf-8")
    return wiki


def test_domain_registry_classification():
    """domain_of / is_core_rel / is_owndays_facing の3ドメイン分類。"""
    assert domain_of("style.md") == "core"
    assert domain_of("judgment/x.md") == "core"
    assert domain_of("hobbies/books/a.md") == "core"
    assert domain_of("knowledge/owndays.md") == "owndays"
    assert domain_of("decisions/x.md") == "owndays"
    assert domain_of("personal/example-garden/plan.md") == "personal/example-garden"
    # Core と OWNDAYS は facing、personal は非 facing
    assert is_owndays_facing("style.md") and is_owndays_facing("knowledge/x.md")
    assert not is_owndays_facing("personal/example/x.md")


def test_personal_project_named_like_core_is_not_core():
    """DA cross-check: personal/style/ (Core dir 名の PJ) は personal であって Core ではない。"""
    assert is_personal_rel("personal/style/x.md") is True
    assert is_core_rel("personal/style/x.md") is False
    assert is_core_rel("style/x.md") is True   # 本物の Core は True


def test_core_files_curates_compact(tmp_path):
    """core_files は persona+judgment+style/*.md+hobbies index のみ (全 hobby entry を含めない=bloat 回避)。"""
    wiki = tmp_path / "wiki"
    (wiki / "style").mkdir(parents=True)
    (wiki / "judgment").mkdir(parents=True)
    (wiki / "hobbies" / "books").mkdir(parents=True)
    (wiki / "style.md").write_text("persona", encoding="utf-8")
    (wiki / "style" / "patterns.md").write_text("p", encoding="utf-8")
    (wiki / "style" / "few-shot.json").write_text("{}", encoding="utf-8")   # JSON は除外
    (wiki / "judgment" / "axes.md").write_text("j", encoding="utf-8")
    (wiki / "hobbies" / "index.md").write_text("h-idx", encoding="utf-8")
    (wiki / "hobbies" / "books" / "1984.md").write_text("book", encoding="utf-8")  # 個別 entry は除外
    names = {p.name for p in core_files(wiki)}
    assert "style.md" in names and "patterns.md" in names and "axes.md" in names
    assert "index.md" in names               # hobbies の index は入る
    assert "few-shot.json" not in names      # JSON は入らない
    assert "1984.md" not in names            # 個別 hobby entry は入らない (bloat 回避)


def test_list_personal_projects(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "personal" / "example-garden").mkdir(parents=True)
    (wiki / "personal" / "invest-x").mkdir(parents=True)
    (wiki / "knowledge").mkdir(parents=True)
    assert list_personal_projects(wiki) == ["example-garden", "invest-x"]


def test_brain_graph_excludes_personal(tmp_path):
    """DA cross-check 1A: 知識グラフ (/api/brain/graph) に personal ノードを出さない。"""
    import brain_graph
    wiki = _seed_wiki(tmp_path)
    (wiki / "personal" / "example-garden" / "plan.md").write_text(
        "# Example\n[[knowledge/owndays]] 評価額 ZZTOPSECRET", encoding="utf-8")
    data = brain_graph.build_graph_data(wiki_dir=wiki)
    blob = str(data)
    assert "personal/example" not in blob and "ZZTOPSECRET" not in blob, "personal が graph に leak"


# ── ★2026-06-28 personal PJ 管理 + path-injection 安全性 ──

def test_safe_project_slug_blocks_injection():
    assert safe_project_slug("example-garden") == "example-garden"
    assert safe_project_slug("Example Garden") == "example-garden"
    assert safe_project_slug("../../etc") == "etc"      # path 区切りは潰れる (injection 不能)
    assert safe_project_slug("a/b") == "a-b"
    assert safe_project_slug("/etc/passwd") == "etc-passwd"
    assert safe_project_slug("..") == ""                # → 呼び出し側で reject


def test_personal_project_dir_stays_inside_personal(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "personal").mkdir(parents=True)
    base = (wiki / "personal").resolve()
    for bad in ["../../etc", "..", "/etc", "a/../../b", ""]:
        d = personal_project_dir(wiki, bad)
        assert d is None or str(d.resolve()).startswith(str(base)), f"escape: {bad}"
    good = personal_project_dir(wiki, "Example Garden")
    assert good is not None and good.name == "example-garden"


def test_personal_add_and_scoped_read(tmp_path):
    import brain_wiki
    wiki = tmp_path / "wiki"
    (wiki / "style").mkdir(parents=True)
    (wiki / "style.md").write_text("# 文体\nPERSONA_MARK", encoding="utf-8")
    brain_wiki.WIKI_DIR = wiki
    bw = object.__new__(brain_wiki.BrainWiki)
    assert "example-garden" in bw.personal_add_project("Example Garden")
    assert (wiki / "personal" / "example-garden" / "_index.md").exists()
    bw.personal_add_note("example-garden", "評価額 SECRET_T。現場の声を起点に。")
    bw.personal_add_project("invest-x")
    bw.personal_add_note("invest-x", "別案件 SECRET_X")
    # scoped: example のみ + 基盤、他PJ 混入なし
    scoped = bw._read_personal_state(project="example-garden")
    assert "SECRET_T" in scoped and "PERSONA_MARK" in scoped
    assert "SECRET_X" not in scoped
    # 全PJ read は両方
    allp = bw._read_personal_state()
    assert "SECRET_T" in allp and "SECRET_X" in allp
    # 書込 file は private + personal/ 配下 (= OWNDAYS 除外対象)
    idx = (wiki / "personal" / "example-garden" / "_index.md").read_text(encoding="utf-8")
    assert "clone_visibility: private" in idx
    assert is_personal_rel("personal/example-garden/_index.md")


def test_personal_add_rejects_bad_name(tmp_path):
    import brain_wiki
    wiki = tmp_path / "wiki"
    (wiki / "personal").mkdir(parents=True)
    brain_wiki.WIKI_DIR = wiki
    bw = object.__new__(brain_wiki.BrainWiki)
    assert "不正" in bw.personal_add_project("..")
    # personal/ の外に何も作られていない (injection 防御)
    assert not (tmp_path / "etc").exists() and not (tmp_path / "passwd").exists()
