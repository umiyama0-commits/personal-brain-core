"""smoke test: _maybe_offer_drive_search の定義 + trigger 文言 carefully covered

★2026-05-26 海山指示「予算は分からない?」 follow-up でも Drive 提案を trigger.
★bug 再発防止: e30de5b で caller のみ復活、definition 削除のまま remain
              → screenshot で 「応答生成中にエラーが出ました」 NameError fallback.

この test は:
- _maybe_offer_drive_search が main.py に definition として存在する
- _should_offer_drive 判定が bot 「データ無い」 系 + user follow-up 系 OR で True
- 通常会話 (= 両方 None) は False
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))


# ─── L1: main.py の AST で関数定義の存在を verify ─────
@pytest.mark.smoke
def test_maybe_offer_drive_search_is_defined_in_main():
    """e30de5b の 「caller 復活、definition 削除のまま」 regression 再発防止."""
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_maybe_offer_drive_search" in funcs, (
        "_maybe_offer_drive_search の definition が main.py に無い。"
        "caller 2 箇所 (DM + group) が NameError を起こす。"
    )
    assert "_should_offer_drive" in funcs


@pytest.mark.smoke
def test_main_imports_cleanly():
    """main.py が import 時点で NameError 等を起こさない (= module load 成功)."""
    # main.py は heavy import を持つので AST level の symbol 解決を quick check
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # 主要 reference symbol
    must_exist = [
        "_maybe_offer_drive_search",
        "_handle_drive_intent_query",
        "_has_drive_intent",
        "_should_offer_drive",
    ]
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in must_exist:
        assert name in defined, f"{name} の definition が無い"


# ─── L2: _should_offer_drive の trigger logic ─────
def _load_should_offer():
    """_should_offer_drive を import (heavy dependency 込みで main を import)."""
    # main.py 直接 import は重い → 関数本体だけ exec 抽出
    import importlib.util
    spec = importlib.util.spec_from_file_location("main", REPO / "main.py")
    # heavy import side-effect 避けるため、source string から関数 + 定数だけ抽出 exec
    src = (REPO / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"_BOT_NO_DATA_PHRASES", "_USER_FOLLOWUP_PATTERNS", "_should_offer_drive"}
    selected = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in wanted:
                    selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in wanted:
                selected.append(node)
    mod = ast.Module(body=selected, type_ignores=[])
    code = compile(mod, "<extracted>", "exec")
    ns: dict = {}
    exec(code, ns)
    return ns["_should_offer_drive"]


@pytest.mark.smoke
def test_should_offer_drive_bot_no_data_phrases():
    """bot reply に 「データ無い」 系 → True."""
    fn = _load_should_offer()
    triggers = [
        ("売上は?", "ダイバーシティ店の予算はこっちにはまだ入ってない"),
        ("Monday Dash 何ある?", "今後拡充候補"),
        ("予算教えて", "BI で見るのが早い"),
        ("人事評価は?", "Brain には入ってない"),
        ("店舗の数は?", "確認できない"),
        # ★海山指示 (= 今回 screenshot ケース): 「ピンポイント数値はこっちにまだ流し込めてない」
        ("ダイバーシティ店の今月の予算は?",
         "ただ、予算のピンポイント数値はこっちにまだ流し込めてないね。今後集めて少しずつ更新する予定。"),
        ("今期 plan は?", "今後収集する予定"),
        ("店舗別 budget は?", "順次更新していく予定"),
        ("社員別 KPI は?", "Brain には整備中"),
        ("カテゴリ別 売上は?", "手元にはまだ入って"),
        ("ある wiki は?", "まだ取り込めていない"),
    ]
    for user, bot in triggers:
        assert fn(user, bot), f"trigger 期待: user={user!r} bot={bot!r}"


@pytest.mark.smoke
def test_should_offer_drive_user_followup():
    """user follow-up 「分からない?」 系 → True (bot reply は 関係なし)."""
    fn = _load_should_offer()
    triggers = [
        "予算は分からない?",
        "他にない?",
        "別の方法ある?",
        "知らない？",
        "探せる?",
    ]
    for ut in triggers:
        assert fn(ut, "通常の回答"), f"user follow-up trigger 期待: {ut!r}"


@pytest.mark.smoke
def test_should_offer_drive_normal_conversation():
    """通常会話 (= 両方 hit せず) → False."""
    fn = _load_should_offer()
    cases = [
        ("おはよう", "おはよう"),
        ("今日の売上は?", "今日の売上は 100M 円"),
        ("会議の予定", "14:00 から WBR"),
    ]
    for ut, br in cases:
        assert not fn(ut, br), f"通常会話で False 期待: user={ut!r} bot={br!r}"


@pytest.mark.smoke
def test_should_offer_drive_empty_inputs():
    """空 input → False (= 安全側)."""
    fn = _load_should_offer()
    assert not fn("", "")
    assert not fn(None, None)


# ─── L3: button 化 (★2026-05-26 海山指示「文字入力じゃなくボタンに」) ─────
@pytest.mark.smoke
def test_maybe_offer_drive_uses_button_template_for_dm():
    """1:1 DM では send_button_template を呼ぶ (= 文字入力指示ではなく button)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _maybe_offer_drive_search")
    assert idx > 0
    body = src[idx : idx + 3000]
    # DM 経路で button_template を使う
    assert "send_button_template" in body
    # ★v3 schema (LINE Works 公式仕様準拠): data は ASCII only "drv:{q_id}"
    # 旧 v2 の "DRIVE_SEARCH:<日本語>" は 400 reject されてた (= ASCII 推奨仕様違反)
    assert '"data": f"drv:{q_id}"' in body
    # query は事前に server-side cache に保存される (= _stash_drive_query)
    assert "_stash_drive_query(q)" in body
    # button label
    assert "Drive で検索" in body or "Drive 検索" in body
    # group は依然 text 1 行 (= button_template 非対応)
    assert "send_channel_text" in body


@pytest.mark.smoke
def test_drive_query_cache_helpers_exist():
    """server-side query cache (= _stash_drive_query / _pop_drive_query) 定義 + TTL."""
    import ast
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_stash_drive_query" in funcs
    assert "_pop_drive_query" in funcs
    src = (REPO / "main.py").read_text(encoding="utf-8")
    # TTL 設定
    assert "_DRIVE_QUERY_TTL" in src
    # secrets.token_urlsafe で ASCII safe ID 生成
    assert "_secrets.token_urlsafe" in src
    # module-level cache dict
    assert "_DRIVE_QUERY_CACHE" in src


@pytest.mark.smoke
def test_handle_postback_pops_cached_query_with_expired_fallback():
    """postback handler が drv: prefix → _pop_drive_query → expired 時 user 通知."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_lineworks_message")
    assert idx > 0
    body = src[idx : idx + 6400]  # ★2026-07-13 ack + 失敗経路 + 確度表示の追加分で窓を拡張
    # drv: prefix dispatch
    assert 'pb_data.startswith("drv:")' in body
    # query 復元
    assert "_pop_drive_query(q_id)" in body
    # expired 時 user 通知 (= 1h 経過 or bot restart で消失)
    assert "button が古くなった" in body or "古くなった" in body
    assert "もう一度" in body


@pytest.mark.smoke
def test_send_button_template_uses_message_type_with_postback_field():
    """★v4 schema: button_template の actions は type:message + postback field (nested).

    LINE Works 公式 compatibility matrix で button_template は postback action 非対応、
    type:message + postback field (nested) のみが動作する.
    """
    src = (REPO / "lineworks_bot.py").read_text(encoding="utf-8")
    idx = src.find("async def send_button_template")
    assert idx > 0
    body = src[idx : idx + 2500]
    # actions 部の実装 (docstring 除外)
    actions_start = body.find('"actions"')
    assert actions_start > 0
    actions_window = body[actions_start : actions_start + 800]
    # ★v4 = type:message
    assert '"type": "message"' in actions_window, "v4 では type:message が正解 (公式仕様)"
    # postback field (nested)
    assert '"postback"' in actions_window
    # v3 残骸 (type:postback / data field / displayText) が actions 部に無いこと
    assert '"type": "postback"' not in actions_window, \
        "type:postback は button_template で使えない (= 400 reject 原因)"
    assert '"displayText"' not in actions_window, \
        "displayText は message action に存在しない field"


@pytest.mark.smoke
def test_parse_webhook_extracts_postback_from_message_event():
    """text message event の content.postback も parsed に含める (= v4 schema 対応)."""
    src = (REPO / "lineworks_bot.py").read_text(encoding="utf-8")
    idx = src.find("def parse_webhook")
    assert idx > 0
    body = src[idx : idx + 3000]
    # text ctype の return に postback field 追加
    text_branch = body.find('if ctype == "text"')
    assert text_branch > 0
    text_window = body[text_branch : text_branch + 500]
    assert '"postback":' in text_window  # parsed dict に postback key
    assert 'content.get("postback"' in text_window


@pytest.mark.smoke
def test_handle_message_event_routes_drv_postback_to_drive_search():
    """v4: message event 内 postback が "drv:" prefix なら drive 検索に route."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_lineworks_message")
    assert idx > 0
    body = src[idx : idx + 5000]
    # message event 内 postback 抽出 + drv: prefix check
    assert "_pb_from_msg" in body or "parsed.get(\"postback\"" in body
    # drv: prefix dispatch
    assert '_pb_from_msg.startswith("drv:")' in body or '"drv:"' in body
    # _pop_drive_query + _handle_drive_intent_query 呼出
    assert "_pop_drive_query" in body
    assert "_handle_drive_intent_query" in body


@pytest.mark.smoke
def test_lineworks_parse_webhook_accepts_postback():
    """parse_webhook が postback event を取り込む (= 旧 type:message のみ accept → bug)."""
    src = (REPO / "lineworks_bot.py").read_text(encoding="utf-8")
    idx = src.find("def parse_webhook")
    assert idx > 0
    body = src[idx : idx + 3000]
    # postback event 分岐 (= カルーセル/クイックリプライ/リッチメニュー用 予備 path)
    assert 'ev_type == "postback"' in body
    # data field を抽出 (= top-level or postback.data 両対応)
    assert 'payload.get("data")' in body
    # 戻り値 type
    assert '"type": "postback"' in body


@pytest.mark.smoke
def test_handle_lineworks_message_routes_postback_to_drive_search():
    """_handle_lineworks_message が postback type + DRIVE_SEARCH: prefix を _handle_drive_intent_query に route."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_lineworks_message")
    assert idx > 0
    # ★2026-07-10: window 3500→4400 (S3 の clonefb postback branch 挿入で DRIVE_SEARCH: が
    #   関数先頭から ~3855 字目へ後退。source-level 固定 window の legit なメンテ)。
    body = src[idx : idx + 4400]
    # postback branch
    assert 'msg_type == "postback"' in body
    # DRIVE_SEARCH: prefix dispatch
    assert "DRIVE_SEARCH:" in body
    # _handle_drive_intent_query 呼出
    assert "_handle_drive_intent_query" in body


@pytest.mark.smoke
def test_lineworks_webhook_handler_accepts_postback_type():
    """webhook handler の type check に postback が含まれる (= drop されない)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find('parsed["type"] not in')
    assert idx > 0
    window = src[idx : idx + 200]
    assert '"postback"' in window


# ─── L4: Drive 権限注意喚起 (★2026-05-26 海山指示) ─────
# bot (= umiyama-ai) と user の Drive 権限が違うため link が開けない可能性を予め通知
@pytest.mark.smoke
def test_drive_offer_button_mentions_permission_caveat():
    """提案 button の content_text に権限注意書きが含まれる (= tap 前に user に通知)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _maybe_offer_drive_search")
    assert idx > 0
    body = src[idx : idx + 3500]
    # button content_text に「権限」 と「umiyama-ai」 が含まれる
    # (= bot 権限で取得、user 権限と異なる可能性ある旨を提案時点で通知)
    assert "umiyama-ai" in body
    assert "権限" in body
    # **実装の** send_button_template 呼出 (docstring 言及ではなく) で content_text を確認
    # `await lineworks_bot.send_button_template(` で実装 call を特定
    call_marker = "await lineworks_bot.send_button_template("
    call_idx = body.find(call_marker)
    assert call_idx > 0, "_maybe_offer_drive_search 内に send_button_template 呼出無し"
    btn_window = body[call_idx : call_idx + 800]
    assert "権限" in btn_window
    assert "umiyama-ai" in btn_window


@pytest.mark.smoke
def test_drive_intent_query_result_includes_permission_caveat():
    """Drive AI 検索結果末尾に権限注意書き (= link 開けない時の owner 申請導線)."""
    src = (REPO / "main.py").read_text(encoding="utf-8")
    idx = src.find("async def _handle_drive_intent_query")
    assert idx > 0
    body = src[idx : idx + 6400]  # ★2026-07-13 ack + 失敗経路 + 確度表示の追加分で窓を拡張
    # 末尾に注意 phrase
    assert "umiyama-ai" in body
    assert "owner にアクセス申請" in body or "アクセス申請" in body
    assert "権限" in body


@pytest.mark.smoke
def test_drive_ai_command_result_includes_permission_caveat():
    """/drive ai コマンド経路 (brain_commands.py) も同じ権限注意書き (= UX 統一)."""
    src = (REPO / "brain_commands.py").read_text(encoding="utf-8")
    # /drive ai handler section
    idx = src.find('message.startswith("/drive ai ")')
    assert idx > 0
    # 同 handler の終端まで (= 次の handler `/drive ingest` まで)
    end_idx = src.find('message.startswith("/drive ingest")', idx)
    body = src[idx : end_idx if end_idx > 0 else idx + 5000]
    # 同 phrase が含まれる
    assert "umiyama-ai" in body
    assert "アクセス申請" in body or "owner に" in body
    assert "権限" in body


@pytest.mark.smoke
def test_drive_intent_query_uses_top_n_3():
    """★海山指示「表示は TOP 3」: top_n=3 に統一."""
    main_src = (REPO / "main.py").read_text(encoding="utf-8")
    bc_src = (REPO / "brain_commands.py").read_text(encoding="utf-8")
    # main.py:_handle_drive_intent_query
    assert "top_n=3" in main_src
    # brain_commands.py:/drive ai
    assert "top_n=3" in bc_src
    # top_n=5 が main.py / brain_commands.py に残ってない (= 5 → 3 完全置換)
    # ただし他の path で top_n=5 を使う test fixture 等は無視 (= source 内 grep のみ)
    assert "top_n=5" not in main_src, "main.py に top_n=5 が残ってる"
    assert "top_n=5" not in bc_src, "brain_commands.py に top_n=5 が残ってる"
