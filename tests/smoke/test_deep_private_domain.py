"""smoke: 深層 private (interview/ + personal/) の path 防御 leak 証明 (★2026-07-03 v3 ADR DA R6)。

設計: wiki/interview/ = 人格深層 (家族/弱さ/金/体、v3「脳の複製」)。従来は frontmatter
clone_visibility: private の一枚防御だったが、personal/ と同じ path 防御 (is_deep_private_rel)
に統合。ここでは「frontmatter が欠落/public 誤記でも path だけで漏れない」ことを
OWNDAYS-facing consumer ごとに機械的に証明する。

海山 admin 経路 (/mcp/brain スマホ connector・/clone・alignment 質問生成・chroma 索引) は
**意図的に対象外** = interview/ を引き続き読める (下の deliberate 系テストで固定)。
network・LLM・chromadb 非依存 (純粋関数 + tmp ディレクトリ)。
"""
import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "consultant"))

from brain_wiki_helpers.domain import (  # noqa: E402
    DEEP_PRIVATE_DIRS, INTERVIEW_DOMAIN,
    is_deep_private_rel, is_owndays_facing, is_personal_rel, domain_of,
)

MARK = "ZZDEEPSECRET"


# ── 単一真実源 (domain.py) ──

def test_is_deep_private_rel_covers_interview_and_personal():
    assert INTERVIEW_DOMAIN == "interview"
    assert set(DEEP_PRIVATE_DIRS) == {"personal", "interview"}
    assert is_deep_private_rel("interview/shadow.md") is True
    assert is_deep_private_rel("interview/family.md") is True
    assert is_deep_private_rel("personal/example-garden/plan.md") is True
    # 非該当: OWNDAYS 既定
    assert is_deep_private_rel("knowledge/owndays-vmv.md") is False
    assert is_deep_private_rel("interviewish/x.md") is False       # 部分一致で誤判定しない
    assert is_deep_private_rel("decisions/interview/x.md") is False  # 非先頭は対象外
    assert is_deep_private_rel("") is False                          # fail-safe = OWNDAYS 既定


def test_is_owndays_facing_is_negation_of_deep_private():
    """§1.17 規律② の統一: facing 判定は is_deep_private_rel の否定 1 点のみ。"""
    assert is_owndays_facing("knowledge/x.md") is True
    assert is_owndays_facing("style.md") is True                 # Core は facing (基盤共有)
    assert is_owndays_facing("interview/money-personal.md") is False
    assert is_owndays_facing("personal/example/x.md") is False
    # interview は personal ではない (既存 13 箇所の personal 専用判定に巻き込まれない)
    assert is_personal_rel("interview/shadow.md") is False
    assert domain_of("interview/shadow.md") == "interview"


# ── seed: frontmatter 無し (= R6 の想定脅威「visibility 一枚防御の剥落」) ──

def _seed_wiki(tmp_path):
    wiki = tmp_path / "wiki"
    (wiki / "knowledge").mkdir(parents=True)
    (wiki / "interview").mkdir(parents=True)
    (wiki / "personal" / "pj").mkdir(parents=True)
    (wiki / "knowledge" / "owndays.md").write_text(
        "---\nclone_visibility: public\n---\n# OWNDAYS\n売上 戦略の話。" + "x" * 40,
        encoding="utf-8")
    # ★frontmatter 無し = 最悪ケース (dedup merge / 手編集で header が剥がれた想定)
    (wiki / "interview" / "shadow.md").write_text(
        f"# 弱さ・後悔\n借金時代の {MARK}。家族の話。", encoding="utf-8")
    (wiki / "personal" / "pj" / "plan.md").write_text(
        f"# PJ\n評価額 {MARK}-P。", encoding="utf-8")
    return wiki


# ── OWNDAYS-facing consumers (leak したら fail) ──

def test_brain_graph_excludes_interview(tmp_path):
    import brain_graph
    wiki = _seed_wiki(tmp_path)
    data = brain_graph.build_graph_data(wiki_dir=wiki)
    blob = str(data)
    assert "interview/" not in blob and MARK not in blob, "interview が graph に leak"
    assert "knowledge" in blob  # OWNDAYS node は出る


def test_sync_to_claude_project_excludes_interview(tmp_path):
    # sync_to_claude_project は import 時に本番 root を sys.path[0] へ insert する
    # (line 29)。worktree での test 実行時に後続 import が本番 module を拾わないよう復元。
    _prior = list(sys.path)
    import sync_to_claude_project as stc
    sys.path[:] = _prior
    wiki = _seed_wiki(tmp_path)
    orig = stc.WIKI_DIR
    try:
        stc.WIKI_DIR = wiki
        out = stc.gather_wiki()
    finally:
        stc.WIKI_DIR = orig
    assert MARK not in out and "interview/" not in out, "Claude.ai export に interview が leak"
    assert "knowledge" in out


def test_mcp_brain_server_excludes_interview(tmp_path):
    mcp = pytest.importorskip("mcp")  # noqa: F841 (stdio MCP 環境のみ)
    import mcp_brain_server as mbs
    wiki = _seed_wiki(tmp_path)
    orig = mbs.WIKI_DIR
    try:
        mbs.WIKI_DIR = wiki
        read = asyncio.run(mbs._brain_wiki_read({"path": "interview/shadow.md"}))
        listing = asyncio.run(mbs._brain_wiki_list({}))
    finally:
        mbs.WIKI_DIR = orig
    assert MARK not in read[0].text, "MCP read で interview 本文が leak"
    assert "interview/" not in listing[0].text and "personal/" not in listing[0].text
    assert "knowledge" in listing[0].text


def test_rebuild_index_excludes_interview(tmp_path):
    import brain_wiki
    wiki = _seed_wiki(tmp_path)
    orig = brain_wiki.WIKI_DIR
    try:
        brain_wiki.WIKI_DIR = wiki
        bw = object.__new__(brain_wiki.BrainWiki)
        asyncio.run(bw._rebuild_index())
        idx = (wiki / "index.md").read_text(encoding="utf-8")
    finally:
        brain_wiki.WIKI_DIR = orig
    assert "interview/" not in idx and "personal/" not in idx, "index.md catalog に leak"
    assert "knowledge/owndays" in idx


def test_read_wiki_state_excludes_interview_by_default(tmp_path):
    """compile / lint context (default) は interview/ を読まない。
    海山 admin 消費者 (/clone・alignment 質問生成) は include_interview=True で従来どおり。"""
    import brain_wiki
    wiki = _seed_wiki(tmp_path)
    orig = brain_wiki.WIKI_DIR
    try:
        brain_wiki.WIKI_DIR = wiki
        bw = object.__new__(brain_wiki.BrainWiki)
        for full in (False, True):
            state = bw._read_wiki_state(full=full)
            assert MARK not in state, f"full={full} で interview が compile context に leak"
        admin = bw._read_wiki_state(full=True, include_interview=True)
        assert MARK in admin, "海山 admin 経路 (/clone) が interview を読めなくなった"
        assert f"{MARK}-P" not in admin, "personal/ は include_interview でも常に除外"
    finally:
        brain_wiki.WIKI_DIR = orig


def test_read_wiki_state_public_forces_private_by_path(tmp_path):
    """公開 dump: interview/ が public 誤記でも path で private 強制 (belt-and-suspenders)。"""
    import brain_wiki
    wiki = _seed_wiki(tmp_path)
    # ★誤記の再現: interview/ に clone_visibility: public を書いてしまったケース
    (wiki / "interview" / "misconfig.md").write_text(
        f"---\nclone_visibility: public\n---\n# 誤記\n{MARK}-PUB", encoding="utf-8")
    orig = brain_wiki.WIKI_DIR
    try:
        brain_wiki.WIKI_DIR = wiki
        bw = object.__new__(brain_wiki.BrainWiki)
        out = bw._read_wiki_state_public()
    finally:
        brain_wiki.WIKI_DIR = orig
    assert f"{MARK}-PUB" not in out and MARK not in out, "public 誤記の interview が dump に leak"
    assert "OWNDAYS" in out


# ── 意図的な非対象 (deliberate、変えたら気付くための固定) ──

def test_main_api_endpoints_use_owndays_facing_source_level():
    """last-mile 回帰 (v3 の /diary dead-code 事故と同型対策): main.py の外部 API 2 endpoint が
    is_owndays_facing を実際に呼んでいることを source level で固定。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    for fn in ("def api_brain_knowledge", "def api_brain_dashboard"):
        i = src.find(fn)
        assert i > 0, f"{fn} が見つからない (rename されたらこの test を更新)"
        body = src[i:i + 6000]
        # 文字列存在でなく実際の判定行を照合 (import だけ残して if を消す退行を false-pass させない)
        assert "if not is_owndays_facing(rel):" in body, \
            f"{fn} が is_owndays_facing で除外していない"
    # /api/brain/wiki (Brain Map 本文ペイン): ★2026-07-11 海山指示で admin tier は全開・
    #   token tier は深層 private 拒否のまま。admin 条件付きゲートが存在することを固定
    #   (無条件遮断に戻す=Brain Map が再び 404 / 無ゲートで全開=弱 token 露出、双方を検知)。
    i = src.find("async def api_brain_wiki")   # query 版が先 (6620 < api_brain_wiki_page 7870)
    assert i > 0
    _bw = src[i:i + 3000]
    assert "brain_auth_tier(request)" in _bw, \
        "/api/brain/wiki が admin tier 判定 (brain_auth_tier) を使っていない"
    assert "if is_deep_private_rel(rel) and not _admin:" in _bw, \
        "/api/brain/wiki の深層 private 拒否 (token tier) が admin 条件付きで存在しない"
    # /api/brain/search (★2026-07-10 世界基準評価 #2 DA): deep-private opt-in (private=1) を廃止し
    #   常に public_only=True。operator key 経由で interview/ が search snippet 露出する穴を封鎖。
    i = src.find("async def api_brain_search")
    assert i > 0
    body = src[i:i + 2500]
    assert "public_only=True" in body, \
        "/api/brain/search が public_only=True 固定でない (deep-private 露出穴の再発)"
    assert 'get("private", "") != "1"' not in body, \
        "/api/brain/search に private=1 opt-in が復活 (operator key で deep-private search 可能に)"
    # ★2026-07-10 (世界基準評価 #2): path 版 /api/brain/wiki/{path} (Brain dashboard 詳細ペイン)
    #   も deep-private 拒否。query 版 (上) だけゲートして path 版が漏れる片系 bypass の回帰防止。
    i = src.find("async def api_brain_wiki_page")
    assert i > 0, "api_brain_wiki_page が見つからない (rename されたらこの test を更新)"
    assert "if is_deep_private_rel(rel):" in src[i:i + 2500], \
        "/api/brain/wiki/{path} の深層 private 拒否が消えた (query 版との片系 bypass)"


def test_operator_endpoints_block_clone_visibility_private_source_level():
    """★2026-07-10 (世界基準評価 #2): operator-key API は deep-private (path) に加え
    clone_visibility: private (法務/人事 decision 等) も遮断する。operator key は ?key= で LINE URL
    に埋まる共有 secret のため、private frontmatter の全文露出を塞ぐ (可視性一貫化)。

    ★2026-07-11 海山指示「Brain Map は個人利用だから全部見れる」の例外: api_brain_wiki (query 版=
    Brain Map 詳細ペイン) だけ admin tier で全開。他の兄弟 endpoint (wiki_page/knowledge/dashboard)
    は**無条件遮断のまま**で、admin bypass を持たないことを固定 (Brain Map 以外へ全開が伝播しない)。
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # ── 無条件遮断を維持する 3 endpoint (admin bypass 混入も検知) ──
    for name in ("async def api_brain_wiki_page", "def api_brain_knowledge",
                 "def api_brain_dashboard"):
        i = src.find(name)
        assert i > 0, f"{name} が見つからない (rename されたらこの test を更新)"
        body = src[i:i + 6000]
        assert 'parse_clone_visibility(' in body and '== "private"' in body, \
            f"{name} が clone_visibility: private を遮断していない (operator key 経由の私的 decision 露出)"
        assert "brain_auth_tier" not in body and "not _admin" not in body, \
            f"{name} に admin bypass が混入 (Brain Map 以外は全開にしない、#2 据え置き)"
    # ── Brain Map の api_brain_wiki (query 版) は admin tier 条件付き遮断 ──
    i = src.find("async def api_brain_wiki")   # query 版が先 (6620 < wiki_page 7870)
    assert i > 0
    body = src[i:i + 3000]
    assert '_admin = brain_auth_tier(request) == "admin"' in body, \
        "api_brain_wiki が admin tier 判定 (brain_auth_tier) を持たない"
    assert 'parse_clone_visibility(_c) == "private" and not _admin' in body, \
        "api_brain_wiki の private 遮断が admin 条件付きでない (無条件 or 無ゲート全開の誤り)"


def test_brain_auth_tier_admin_gate_source_level():
    """★2026-07-11: Brain Map 全開の唯一のゲート = brain_auth_tier。admin は BRAIN_EXTENSION_KEY
    (compare_digest) のみ。VOICE_ALIGN_TOKEN 等の弱い token は admin に昇格しないことを固定。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("def brain_auth_tier")
    assert i > 0, "brain_auth_tier が無い (Brain Map admin ゲートの本体)"
    body = src[i:i + 1800]
    assert "hmac.compare_digest(key, BRAIN_EXTENSION_KEY)" in body, \
        "brain_auth_tier が BRAIN_EXTENSION_KEY を compare_digest で照合していない"
    assert 'return "admin"' in body and 'return "token"' in body
    # docstring は説明で token 名に言及するため、実コード部分 (docstring 以降) だけで混入を判定
    parts = body.split('"""', 2)
    code = parts[2] if len(parts) >= 3 else body
    assert "VOICE_ALIGN_TOKEN" not in code and "ALIGNMENT_TRIAL_TOKEN" not in code, \
        "brain_auth_tier の実コードに弱い token が混入 (admin 昇格の穴)"


def test_api_brain_graph_admin_optin_source_level():
    """★2026-07-11: /api/brain/graph は admin tier の時だけ admin=True を build_graph_data に渡す
    (token/公開はデフォ False = 深層 private + clone_visibility: private を build 除外)。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("async def api_brain_graph")
    assert i > 0, "api_brain_graph が見つからない"
    body = src[i:i + 1500]
    assert 'brain_auth_tier(request) == "admin"' in body, \
        "api_brain_graph が admin tier を判定していない"
    assert "admin=_admin" in body, \
        "api_brain_graph が admin フラグを build_graph_data に渡していない"


def test_brain_graph_admin_optin_includes_interview(tmp_path):
    """★2026-07-11 海山指示: admin tier (admin=True) の Brain Map は deep-private も
    ノード化する。デフォルト (False) は従来どおり除外 (test_brain_graph_excludes_interview と対)。"""
    import brain_graph
    wiki = _seed_wiki(tmp_path)
    data = brain_graph.build_graph_data(wiki_dir=wiki, admin=True)
    blob = str(data)
    assert MARK in blob or "interview" in blob, "admin opt-in で interview が graph に出ていない"
    # 対比: デフォルトは従来どおり除外 (回帰防止)
    default = str(brain_graph.build_graph_data(wiki_dir=wiki))
    assert "interview/" not in default and MARK not in default, \
        "デフォルト build_graph_data が深層 private を出している (公開/非 admin へ漏れる)"


def test_brain_graph_token_tier_excludes_clone_visibility_private(tmp_path):
    """★2026-07-11 (b60f6b1 の §1.15 adversarial 検証で surfaced): /api/brain/graph の token tier
    (admin=False) は deep-private path に加え clone_visibility: private ノード (法務/人事 decision 等)
    も build 段階で除外する。graph は従来 path 防御だけで frontmatter private を素通りし、弱い token
    でも private ノードの title/tags/path が JSON 露出していた穴を封鎖 (operator endpoint #2 と一貫)。

    deep-private path とは独立の穴なので seed は非 deep-private dir (decisions/) の private を使う。"""
    import brain_graph
    wiki = _seed_wiki(tmp_path)
    (wiki / "decisions").mkdir(parents=True)
    (wiki / "decisions" / "legal.md").write_text(
        f"---\nclone_visibility: private\ntags: [法務, 係争]\n---\n# 法務メモ\n係争 {MARK}-LEGAL の件。"
        + "y" * 40, encoding="utf-8")
    # token tier: private node は title/path/tag も出さず除外
    token = str(brain_graph.build_graph_data(wiki_dir=wiki))
    assert "decisions/legal" not in token and f"{MARK}-LEGAL" not in token \
        and "法務メモ" not in token and "係争" not in token, \
        "token tier graph に clone_visibility: private の title/path/tag が leak"
    assert "knowledge" in token, "public ノードまで過剰除外している"
    # admin tier: 従来どおり private も全開 (海山の「全部見れる」を壊さない)
    admin = str(brain_graph.build_graph_data(wiki_dir=wiki, admin=True))
    assert "decisions/legal" in admin or "法務メモ" in admin, \
        "admin tier で clone_visibility: private の decision が出ていない (全開が壊れた)"


def test_indexing_keeps_interview_deliberately_source_level():
    """chroma 索引 (brain_index.index_wiki_file / main._watch_wiki_changes) は interview/ を
    **索引除外しない** (海山専用 vector recall = P3b が引くため)。誤って is_deep_private_rel に
    変えると海山経路が沈黙 break するので source level で固定。公開経路の遮断は
    chroma where + runtime visibility gate + path 強制 private (別 test) が担う。"""
    idx_src = (ROOT / "brain_index.py").read_text(encoding="utf-8")
    i = idx_src.find("async def index_wiki_file")
    assert i > 0
    body = idx_src[i:i + 5000]
    # 早期 return (索引 skip) は read より前 = head 部。コメント行を除いた code 行のみ照合。
    head = body[:body.find("file_path.read_text")]
    head_code = "\n".join(
        ln for ln in head.splitlines() if not ln.strip().startswith("#"))
    assert "if is_personal_rel(rel_path):" in head_code, \
        "索引 chokepoint の is_personal_rel skip が消えた"
    assert "is_deep_private_rel" not in head_code, \
        "索引 skip が deep_private 化されている (海山専用 vector recall が壊れる)"
    # 一方 read 後の metadata 強制 private (chroma where 句の実効化、DA 1a) は必須
    assert 'metadata["clone_visibility"] = "private"' in body, \
        "深層 private の metadata 強制付与が消えた ($ne where 句が metadata 欠落 chunk に無力化)"
    # _watch_wiki_changes (索引 chokepoint の watcher 側) も同じ理由で personal のみ維持
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    j = main_src.find("async def _watch_wiki_changes")
    assert j > 0
    wbody = main_src[j:j + 3500]
    assert "if is_personal_rel(f.relative_to(WIKI_DIR)):" in wbody, \
        "_watch_wiki_changes の is_personal_rel 判定が消えた"
    assert "is_deep_private_rel(f.relative_to" not in wbody, \
        "_watch_wiki_changes が deep_private 化されている (interview/ の増分再索引が止まる)"


def test_include_interview_optin_does_not_proliferate_source_level():
    """DA cross-check 6: include_interview=True (海山 admin opt-in) が「便利 flag」として
    非 admin 経路へコピペ増殖しないよう、出現箇所を 2 (= /clone と alignment 質問生成) に固定。
    正当に増やす時はこの test と ADR の非対象リストを同時更新すること。"""
    src = (ROOT / "brain_wiki.py").read_text(encoding="utf-8")
    calls = [ln for ln in src.splitlines()
             if "include_interview=True" in ln and "_read_wiki_state(" in ln
             and not ln.strip().startswith("#")]
    assert len(calls) == 2, \
        f"include_interview=True の実呼び出しが {len(calls)} 箇所 (期待 2 = /clone と alignment 質問生成)"


def test_build_context_public_only_covers_all_deep_private_dirs():
    """DA cross-check 5: brain_index.build_context の public_only path filter はハードコード第3
    コピー。DEEP_PRIVATE_DIRS に dir を足した時の追従漏れを source-level で検出する。"""
    src = (ROOT / "brain_index.py").read_text(encoding="utf-8")
    i = src.find("async def build_context")
    assert i > 0
    body = src[i:i + 3500]
    for d in DEEP_PRIVATE_DIRS:
        assert f'"{d}/"' in body, \
            f"build_context の path filter が DEEP_PRIVATE_DIRS の '{d}' に未追従 (brain_index.py)"


def test_phone_mcp_keeps_interview_deliberately_source_level():
    """/mcp/brain (海山スマホ connector) の brain_wiki_read/list は interview/ を返してよい
    (alignment 雑談の継続性が設計意図)。personal/ のみ除外を維持。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("async def _mcp_call_tool")
    assert i > 0
    body = src[i:src.find("async def mcp_brain_endpoint")]
    assert '("personal",)' in body, "/mcp/brain の personal 除外が消えた"
    assert "is_deep_private_rel" not in body and "is_owndays_facing" not in body, \
        "/mcp/brain (海山 admin) に deep_private 除外が混入 (interview 参照は設計意図)"
