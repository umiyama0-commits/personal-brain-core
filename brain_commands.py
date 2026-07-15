"""
brain_commands.py — LINE Bot 用ブレインWikiコマンドハンドラ

使い方:
  普通にメッセージ → AI応答 + バックグラウンドでraw蓄積 → 閾値超えたら自動コンパイル
  /teach ○○     → 明示的にWikiに教える（即コンパイル）
  /clone ○○     → 自分のクローンとして応答
  /brain         → Wiki蓄積状況を表示
  /lint          → Wiki健康診断を実行
  /dedup         → Wiki の重複フロントマター / H2 セクションを統合
  /graph         → Brain Map（力学ネットワーク図）のURLを返す
  /line-skip     → 今週のLINE取り込みリマインドをスキップ（次週は通常通り）
  /line-reminder-off → LINE取り込みリマインドを永続停止
  /line-reminder-on  → LINE取り込みリマインドを再開
  /wiki ○○      → Wikiから特定の記事を検索・表示
  /align         → アライメント質問を1問出す（回答でWikiに反映）
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta

from brain_wiki import BrainWiki

JST = timezone(timedelta(hours=9))
logger = logging.getLogger(__name__)


async def handle_brain_commands(app, user_id, message, reply_token):
    """ブレインWikiコマンドを処理。処理した場合Trueを返す。"""
    brain: BrainWiki = app.state.brain
    http = app.state.http

    from main import reply_message

    # ─── ★2026-05-12: コメント待機中なら次メッセを comment として記録 ───
    # (Quick Reply の「💬コメ」ボタン → このフローへ)
    import clone_feedback, clone_learning
    ctx_comment = clone_feedback.get_comment_awaiting(user_id)
    if ctx_comment:
        text = (message or "").strip()
        # キャンセル指示
        if text.lower() in ("キャンセル", "cancel", "やめる", "中止"):
            clone_feedback.cancel_comment(user_id)
            await reply_message(http, reply_token, "コメント待機をキャンセルしました。")
            return True
        # 別コマンド (/ で始まる) なら待機を解除して通常処理に流す
        if not text.startswith("/"):
            target_fid = ctx_comment.get("target_fid", "")
            kind = ctx_comment.get("kind", "feedback")
            ok = False
            if kind == "feedback":
                ok = clone_feedback.add_comment(target_fid, text, reviewer="umiyama")
            elif kind == "learning":
                ok = clone_learning.add_comment(target_fid, text, reviewer="umiyama")
            clone_feedback.cancel_comment(user_id)
            if ok:
                await reply_message(
                    http, reply_token,
                    f"💬 コメント記録 → [{target_fid}]\n"
                    f"次のレビューや学習で参照されます。"
                )
            else:
                await reply_message(http, reply_token, f"対象が見つからず: {target_fid}")
            return True

    # ─── /brain — 蓄積状況 + Brain Map リンク ───
    if message.strip() == "/brain":
        stats = brain.get_stats()
        cats = "\n".join(
            f"  {k}: {v}件" for k, v in stats["categories"].items()
        )
        # ★2026-07-01 海山指示「/brain でブレインマップが出ない」: マップの実体は /graph だが、
        #   /brain を打つ直感に合わせて Brain Map リンクを併記する。ここは admin gate 済
        #   (main.py:1675 is_admin fail-closed) なので鍵付き URL の露出は安全 (/graph と同条件)。
        import os
        from pathlib import Path
        _tf = Path("/app/data/brain/tunnel_url.txt")
        _turl = _tf.read_text().strip() if _tf.exists() else ""
        _key = os.getenv("BRAIN_EXTENSION_KEY", "")
        if _turl:
            _murl = f"{_turl}/brain/graph?key={_key}" if _key else f"{_turl}/brain/graph"
            _map = (
                f"\n━━━━━━━━━━━━━━━━━\n"
                f"🧠 Brain Map（ネットワーク図・タップで開く）\n{_murl}\n"
                f"（マップ単体は /graph）"
            )
        else:
            _map = "\n━━━━━━━━━━━━━━━━━\n🧠 Brain Map: /graph（Tunnel 未起動）"
        text = (
            f"Brain Wiki status\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"Wiki記事:    {stats['wiki_articles']}件\n"
            f"Rawソース:   {stats['raw_sources']}件\n"
            f"総文字数:    {stats['total_chars']:,}文字\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"カテゴリ別:\n{cats}"
            f"{_map}"
        )
        await reply_message(http, reply_token, text)
        return True

    # ─── /teach — 明示的教示 → 即コンパイル（smart モデル使用） ───
    if message.startswith("/teach "):
        content = message[7:].strip()
        if not content:
            await reply_message(http, reply_token, "使い方: /teach 覚えてほしいこと")
            return True
        # ユーザー明示の教示は高価値なので smart (Opus) を使う
        await brain.ingest_note(user_id, content, title="teach", model="smart")
        await reply_message(http, reply_token, f"Wikiにコンパイルしました")
        return True

    # ─── /memo — AI返答をスキップして Wiki にだけ保存（軽量・低コスト） ───
    if message.startswith("/memo "):
        content = message[6:].strip()
        if not content:
            await reply_message(http, reply_token, "使い方: /memo 覚えておきたいこと")
            return True
        # PrivacyGate
        try:
            result = await app.state.privacy.filter(content, sender_id=user_id)
            if result.verdict.value != "allow":
                await reply_message(
                    http, reply_token,
                    f"🔒 PrivacyGate によりブロック（{result.verdict.value}）: Wiki には保存しません。"
                )
                return True
            content = result.sanitized
        except Exception as e:
            logger.warning(f"PrivacyGate error on /memo: {e}")
        await brain.ingest_note(user_id, content, title="memo", model="smart")
        await reply_message(
            http, reply_token,
            f"📝 メモを Wiki に保存しました（{len(content)}字）"
        )
        return True

    # ─── /clone — クローンモード応答 ───
    if message.startswith("/clone "):
        query = message[7:].strip()
        if not query:
            await reply_message(http, reply_token, "使い方: /clone 質問")
            return True
        reply = await brain.clone_respond(query)
        await reply_message(http, reply_token, f"[Clone] {reply}")
        return True

    # ─── /personal — 非OWNDAYS の個人 PJ/投資 (Example Garden 等) 専用モード ───
    # ★2026-06-28 海山指示。OWNDAYS クローンとは別系統で wiki/personal/ のみを参照 (混線なし)。
    # ここは個人 LINE Bot 経路 (main.py:1635 で is_admin gate 済)。clone_history は触らない。
    if message.startswith("/personal"):
        arg = message[len("/personal"):].strip()
        try:
            reply = await brain.personal_command(arg)
        except Exception as e:
            reply = f"/personal エラー: {e}"
        await reply_message(http, reply_token, f"[Personal] {reply}")
        return True

    # ─── /reflux — 還流 (各PJ→Core 蒸留) の一覧/承認/却下 (admin、Core 書込は海山承認時のみ) ───
    # ★2026-06-28 Step 2。蒸留 (LLM) は cron 専任、ここは list/ok/ng のみ。
    if message.startswith("/reflux"):
        arg = message[len("/reflux"):].strip()
        try:
            from scripts import reflux as _reflux
            reply = _reflux.handle_command(arg)
        except Exception as e:
            reply = f"/reflux エラー: {e}"
        await reply_message(http, reply_token, reply)
        return True

    # ─── /bridge — 孤島接続 (graph エッジ) の一覧/バッチ承認/却下 (admin、propose-only) ───
    # ★2026-07-05 Phase 1 (ADR wiki-ontology-multilayer §3)。提案生成は cron 専任 (bridge_proposer)、
    # ここは list/ok/ng のみ。承認エッジは sidecar → /graph 描画にだけ効く (wiki/retrieval 不変)。
    # /reflux と同じ is_admin gate 済経路。
    if message.startswith("/bridge"):
        arg = message[len("/bridge"):].strip()
        try:
            from scripts import bridge_proposer as _bridge
            reply = _bridge.handle_command(arg)
        except Exception as e:
            reply = f"/bridge エラー: {e}"
        await reply_message(http, reply_token, reply)
        return True

    # ─── /diary — シーン単位の自伝的記憶を1行で放り込む (★2026-07-03 v3「脳の複製」) ───
    # 音声セッション (週次) の隙間を埋める日常エピソードの軽量導線。蒸留なしで原文のまま
    # interview/episodes.md (private = 社員クローン非露出) へ。この経路は is_admin gate 済。
    if message == "/diary" or message.startswith("/diary "):
        arg = message[len("/diary"):].strip()
        try:
            import alignment_interview as _ai
            if not arg:
                reply = ("/diary <メモ> — 記憶に残った場面を1-3行で。\n"
                         "例: /diary 天神の新店で若い店長が客の名前を全部覚えてた。鳥肌立った\n"
                         "→ interview/episodes.md (private) に原文のまま保存")
            else:
                r = _ai.record_diary_entry(arg)
                reply = (f"📔 記録した ({r['chars']}字 → {r['file']})" if r.get("ok")
                         else f"/diary 失敗: {r.get('reason')}")
        except Exception as e:
            reply = f"/diary エラー: {e}"
        await reply_message(http, reply_token, reply)
        return True

    # ─── /clone-public — うみやまAI 公開版応答を海山自身が検証 ───
    # 検証用なので品質優先 (smart = Claude Opus 4.8)。実運用は fast-gpt で高速化
    if message.startswith("/clone-public "):
        query = message[len("/clone-public "):].strip()
        if not query:
            await reply_message(http, reply_token, "使い方: /clone-public 質問")
            return True
        reply = await brain.clone_respond_public(query, model="smart")
        await reply_message(http, reply_token, f"[うみやまAI / smart] {reply}")
        return True

    # ─── /clone-public-fast — 実運用と同じ fast-gpt で試す ───
    if message.startswith("/clone-public-fast "):
        query = message[len("/clone-public-fast "):].strip()
        if not query:
            await reply_message(http, reply_token, "使い方: /clone-public-fast 質問")
            return True
        reply = await brain.clone_respond_public(query, model="fast-gpt")
        await reply_message(http, reply_token, f"[うみやまAI / fast-gpt] {reply}")
        return True

    # ─── /clone-log — LINE Works 1:1 履歴閲覧 (海山専用) ───
    if message.strip() == "/clone-log":
        import clone_history
        users = clone_history.list_users()
        if not users:
            await reply_message(http, reply_token, "うみやまAI の会話履歴はまだありません。")
            return True
        lines = [f"うみやまAI 会話履歴 (計 {len(users)} 名)"]
        for u in users[:20]:
            disp = u.get("display") or u["user_id"][:12] + "..."
            lines.append(f"  {disp}: {u['message_count']}件 (id={u['user_id'][:12]}...)")
        lines.append("\n個別確認: /clone-log <user_id_prefix>")
        await reply_message(http, reply_token, "\n".join(lines))
        return True

    if message.startswith("/clone-log "):
        import clone_history
        prefix = message[len("/clone-log "):].strip()
        if not prefix:
            await reply_message(http, reply_token, "使い方: /clone-log <user_id_prefix>")
            return True
        users = clone_history.list_users()
        matches = [u for u in users if u["user_id"].startswith(prefix)]
        if not matches:
            await reply_message(http, reply_token, f"該当なし: {prefix}")
            return True
        if len(matches) > 1:
            names = "\n".join(f"  {u['user_id'][:20]}" for u in matches[:10])
            await reply_message(
                http, reply_token,
                f"複数マッチ ({len(matches)}件):\n{names}\nより詳しい prefix で指定してください"
            )
            return True
        dump = clone_history.dump_user(matches[0]["user_id"], n=30)
        # LINE は 5000 字制限
        if len(dump) > 4800:
            dump = dump[:4800] + "\n...(truncated)"
        await reply_message(http, reply_token, dump)
        return True

    # ─── /clone-feedback — うみやまAI 修正希望レビュー (海山専用) ───
    if message.strip() == "/clone-feedback":
        import clone_feedback
        from main import _build_pending_minidigest
        pending = clone_feedback.list_pending(limit=20)
        if not pending:
            await reply_message(http, reply_token, "🎉 修正希望、未処理ゼロ。")
        else:
            text, qr = _build_pending_minidigest(
                pending, kind="feedback",
                title="📋 修正希望 (未処理一覧)",
            )
            await reply_message(http, reply_token, text, quick_reply=qr)
        return True

    if message.startswith("/clone-feedback "):
        import clone_feedback
        arg = message[len("/clone-feedback "):].strip()
        if not arg:
            await reply_message(
                http, reply_token,
                "使い方:\n"
                "  /clone-feedback          — 未レビュー一覧 (Quick Reply 操作付き)\n"
                "  /clone-feedback <id>     — 詳細表示\n"
                "  /clone-feedback-accept <id> — Wiki に取り込み (要 /teach 手動)\n"
                "  /clone-feedback-reject <id> — 見送り\n"
                "  /clone-feedback-note <id>   — 既読マーク"
            )
            return True
        # id で詳細
        await reply_message(http, reply_token, clone_feedback.detail(arg))
        return True

    # ─── 処理ヘルパー: 共通処理 + 残 mini digest 添付 ───
    async def _handle_feedback_action(fid: str, status: str, label: str):
        import clone_feedback
        from main import _build_pending_minidigest
        rec = clone_feedback.find_by_id(fid)
        if not rec:
            await reply_message(http, reply_token, f"❌ 見つかりません: {fid}")
            return
        clone_feedback.update_status(fid, status)
        # 残 pending を取得
        remaining = clone_feedback.list_pending(limit=20)
        confirm = f"{label}: {fid}"
        if status == "accepted":
            teach_body = (
                f"Q: {rec.get('trigger_msg','')[:80]}\n"
                f"修正: {rec.get('feedback','')[:200]}"
            )
            confirm += f"\n💡 Wiki 反映なら → /teach {teach_body[:240]}"
        if remaining:
            text, qr = _build_pending_minidigest(
                remaining, kind="feedback",
                title=f"{confirm}\n\n📋 残り {len(remaining)} 件",
            )
            await reply_message(http, reply_token, text, quick_reply=qr)
        else:
            await reply_message(
                http, reply_token,
                f"{confirm}\n\n🎉 全件処理完了。お疲れさま。",
            )

    if message.startswith("/clone-feedback-accept "):
        fid = message[len("/clone-feedback-accept "):].strip()
        await _handle_feedback_action(fid, "accepted", "✅ accepted")
        return True

    if message.startswith("/clone-feedback-reject "):
        fid = message[len("/clone-feedback-reject "):].strip()
        await _handle_feedback_action(fid, "rejected", "❌ rejected")
        return True

    if message.startswith("/clone-feedback-note "):
        fid = message[len("/clone-feedback-note "):].strip()
        await _handle_feedback_action(fid, "noted", "📝 noted")
        return True

    # ─── ★2026-05-12: /clone-feedback-comment <id> — コメント追加待機 ───
    if message.startswith("/clone-feedback-comment "):
        import clone_feedback
        fid = message[len("/clone-feedback-comment "):].strip()
        rec = clone_feedback.find_by_id(fid)
        if not rec:
            await reply_message(http, reply_token, f"見つかりません: {fid}")
            return True
        clone_feedback.start_comment_awaiting(user_id, fid, kind="feedback")
        # 既存コメント数表示
        existing = rec.get("comments") or []
        existing_n = len(existing) if isinstance(existing, list) else 0
        existing_hint = f" (既存 {existing_n} 件)" if existing_n else ""
        q = (rec.get("trigger_msg") or "")[:60]
        await reply_message(
            http, reply_token,
            f"💬 コメントを次のメッセージで送ってください{existing_hint}\n"
            f"対象 [{fid}]: {q}\n"
            f"(30 分以内 / キャンセルは「キャンセル」)"
        )
        return True

    # ─── ★2026-05-12: /clone-learning-comment <id> — 同上、learning 用 ───
    if message.startswith("/clone-learning-comment "):
        import clone_learning, clone_feedback
        fid = message[len("/clone-learning-comment "):].strip()
        rec = clone_learning.find_by_id(fid)
        if not rec:
            await reply_message(http, reply_token, f"見つかりません: {fid}")
            return True
        clone_feedback.start_comment_awaiting(user_id, fid, kind="learning")
        existing = rec.get("comments") or []
        existing_n = len(existing) if isinstance(existing, list) else 0
        existing_hint = f" (既存 {existing_n} 件)" if existing_n else ""
        insight = (rec.get("insight") or "")[:60]
        await reply_message(
            http, reply_token,
            f"💬 コメントを次のメッセージで送ってください{existing_hint}\n"
            f"対象 [{fid}]: {insight}\n"
            f"(30 分以内 / キャンセルは「キャンセル」)"
        )
        return True

    # ─── /clone-feedback-recheck <id> — バックチェック再実行 ───
    if message.startswith("/clone-feedback-recheck "):
        import clone_feedback
        from datetime import datetime
        from zoneinfo import ZoneInfo
        fid = message[len("/clone-feedback-recheck "):].strip()
        rec = clone_feedback.find_by_id(fid)
        if not rec:
            await reply_message(http, reply_token, f"見つかりません: {fid}")
            return True
        await reply_message(http, reply_token, f"⏳ バックチェック実行中: {fid}")
        try:
            result = await brain.backcheck_feedback(
                trigger_msg=rec.get("trigger_msg", ""),
                response=rec.get("response", ""),
                feedback=rec.get("feedback", ""),
            )
            result["timestamp"] = datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(timespec="seconds")
            clone_feedback.attach_backcheck(fid, result)
            # 結果を再通知は reply_message 使い切ってるので push は不要、detail コマンドで見てもらう
        except Exception as e:
            # 送信失敗は log のみ (reply_token 消費済)
            import logging
            logging.getLogger(__name__).warning(f"recheck failed: {e}")
        return True

    # ─── /drive — Google Drive 個別ファイル / 検索取り込み (海山専用) ───
    if message.strip() == "/drive":
        await reply_message(
            http, reply_token,
            "使い方:\n"
            "  /drive search <キーワード>          — Drive 検索 (= fullText、中身も対象)\n"
            "    (`<query> --name-only` で filename only 旧 mode)\n"
            "  /drive ai <自然言語 query>          — ★Gemini augmented 検索 (= 拡張 + re-rank)\n"
            "  /drive ingest <URL or ID> [label]   — 単一ファイルを wiki に取込\n"
            "  /drive sync                          — 設定済フォルダを selective sync\n"
            "  /drive folders                       — 取り込み対象フォルダ一覧"
        )
        return True

    if message.startswith("/drive search "):
        # ★2026-05-26 海山指示: filename only → fullText (= name OR 中身) で検索範囲拡大.
        # 既存 default は fullText、`--name-only` 付ければ旧 filename 検索 mode へ。
        raw = message[len("/drive search "):].strip()
        if not raw:
            await reply_message(http, reply_token, "キーワードを指定してください")
            return True
        # オプション parse: 「<query> --name-only」 で旧 mode
        name_only = False
        kw = raw
        if raw.endswith(" --name-only"):
            name_only = True
            kw = raw[:-len(" --name-only")].strip()
        try:
            import subprocess
            cmd = ["python3", "/app/gdrive_sync.py", "--discover", kw]
            if not name_only:
                cmd.append("--fulltext")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout or result.stderr
            mode_label = "filename" if name_only else "fullText (= 中身含)"
            await reply_message(http, reply_token, f"🔍 Drive 検索 ({mode_label}): {kw}\n\n{output[:4500]}")
        except Exception as e:
            await reply_message(http, reply_token, f"検索失敗: {e}")
        return True

    if message.startswith("/drive ai "):
        # ★2026-05-26 海山指示 Phase 1: Gemini augmented Drive 検索.
        # default 過去 365 日 + sheets/docs/slides/PDF + top 5.
        # `--all` で全期間 + 全 type へ拡大検索 (= no-hit 時の fallback)。
        raw = message[len("/drive ai "):].strip()
        if not raw:
            await reply_message(http, reply_token,
                "Gemini augmented 検索: 自然言語で query を渡す\n"
                "例: /drive ai 武蔵小山店の今月予算は?\n"
                "    /drive ai 27 卒採用 --all   (= 全期間 + 全 type で拡大検索)")
            return True
        # --all option parse
        apply_default = True
        query = raw
        if raw.endswith(" --all"):
            apply_default = False
            query = raw[:-len(" --all")].strip()
        try:
            from services.gemini_query import search_drive_semantic
            # ★2026-05-26 海山指示「表示は TOP 3 で良い」 (= UX 一貫性で button 経由と合わせる)
            result = await search_drive_semantic(
                query, top_n=3, apply_default_filters=apply_default,
            )
        except Exception as e:
            await reply_message(http, reply_token,
                f"Drive AI 検索失敗: {type(e).__name__}: {str(e)[:200]}")
            return True

        # filter status 表示用
        filt = result.get("filters_applied") or {}
        filter_label = ""
        if filt.get("default_filters_on"):
            filter_label = f"絞込: 過去 {filt.get('since_days', 365)} 日 / sheets+docs+slides+PDF"
        else:
            filter_label = "絞込: 全期間 + 全 type (= 拡大)"

        kw_disp = ", ".join(result["keywords"]) if result["keywords"] else "(無し)"
        gem_tag = "✓ Gemini" if result["via_gemini"] else "△ fallback"

        # 0 件 時: 「全期間で再検索」案内
        if result["total_hits"] == 0:
            suggestion = ""
            if apply_default:
                suggestion = "\n\n💡 全期間 + 全 type で再検索:\n   「/drive ai " + query + " --all」"
            await reply_message(http, reply_token,
                f"🤖 Drive AI 検索: {query}\n"
                f"keyword: {kw_disp}\n"
                f"{filter_label}\n"
                f"hit 0 件、別の言い回しで再試行 or 範囲拡大を" + suggestion)
            return True

        # ★過多 (= 100+ 件) 警告
        oversaturated_warn = ""
        if result["total_hits"] >= 100:
            oversaturated_warn = (
                f"\n⚠ hit {result['total_hits']} 件 (= 多すぎ)、 "
                "keyword 追加で絞ると精度上がる"
            )

        lines = [
            f"🤖 Drive AI 検索 ({gem_tag}): {query}",
            f"keyword: {kw_disp}",
            f"{filter_label}",
            f"hit: {result['total_hits']} 件 → top {len(result['top'])}{oversaturated_warn}",
            "",
        ]
        for i, f in enumerate(result["top"], start=1):
            name = (f.get("name") or "")[:60]
            mime_raw = (f.get("mimeType") or "")
            # mime を 短縮 tag に
            mime_short = "?"
            if "spreadsheet" in mime_raw:
                mime_short = "📊sheet"
            elif "document" in mime_raw:
                mime_short = "📄doc"
            elif "presentation" in mime_raw:
                mime_short = "🎯slide"
            elif "pdf" in mime_raw:
                mime_short = "📕pdf"
            mod = (f.get("modifiedTime") or "")[:10]
            owner = ((f.get("owners") or [{}])[0].get("displayName") or "?")[:20]
            link = f.get("webViewLink") or ""
            reason = f.get("rerank_reason") or ""
            lines.append(f"{i}. [{mime_short}] {name}")
            if reason:
                lines.append(f"   ◉ {reason}")
            lines.append(f"   owner: {owner} / 更新: {mod}")
            if link:
                lines.append(f"   {link}")
            lines.append("")
        # 拡大検索 ヒント (= default mode で hit はあるが少なかった時)
        if apply_default and result["total_hits"] < 5:
            lines.append("💡 もっと探したいなら全期間 + 全 type:")
            lines.append(f"   「/drive ai {query} --all」")
        # ★2026-05-26 海山指示: umiyama-ai と質問者で Drive 権限が違う注意喚起
        # owner 表示 ↑ で 申請先 (= owner) が分かる → user は直接申請可能
        lines.append(
            "⚠️ link は umiyama-ai の権限で取得。あなたに権限が無い場合は "
            "owner にアクセス申請を。"
        )
        await reply_message(http, reply_token, "\n".join(lines)[:4500])
        return True

    if message.startswith("/drive ingest "):
        rest = message[len("/drive ingest "):].strip()
        parts = rest.split(maxsplit=1)
        url_or_id = parts[0] if parts else ""
        label = parts[1] if len(parts) > 1 else "manual"
        if not url_or_id:
            await reply_message(http, reply_token, "URL or ファイル ID を指定してください")
            return True
        try:
            import subprocess
            result = subprocess.run(
                ["python3", "/app/gdrive_sync.py", "--file", url_or_id, "--label", label],
                capture_output=True, text=True, timeout=60,
            )
            output = result.stdout or result.stderr
            await reply_message(http, reply_token, f"📥 Drive ingest:\n{output[:4500]}")
        except Exception as e:
            await reply_message(http, reply_token, f"取込失敗: {e}")
        return True

    if message.strip() == "/drive sync":
        await reply_message(http, reply_token, "⏳ Drive selective sync 実行中...")
        try:
            import subprocess
            result = subprocess.run(
                ["python3", "/app/gdrive_sync.py", "--all"],
                capture_output=True, text=True, timeout=300,
            )
            output = result.stdout + result.stderr
            await reply_message(
                http, reply_token,
                f"✅ Drive sync 完了 (詳細はサーバ log)\n{output[-1500:] if output else '(no output)'}"
            )
        except Exception as e:
            await reply_message(http, reply_token, f"sync 失敗: {e}")
        return True

    if message.strip() == "/drive folders":
        try:
            import json
            from pathlib import Path
            f = Path("/app/data/brain/.gdrive_sources.json")
            if not f.exists():
                await reply_message(http, reply_token, "(設定ファイル無し)")
                return True
            sources = json.loads(f.read_text())
            lines = ["📁 取り込み対象フォルダ:"]
            for src in sources:
                lines.append(
                    f"\n• {src['label']} ({src.get('visibility','?')})"
                    f"\n  {src.get('_note', '')}"
                    f"\n  age≤{src.get('max_age_days','?')}d max={src.get('max_files','?')}"
                )
            await reply_message(http, reply_token, "\n".join(lines))
        except Exception as e:
            await reply_message(http, reply_token, f"読込失敗: {e}")
        return True

    # ─── /clone-learning — うみやまAI 会話発見レビュー (海山専用) ───
    if message.strip() == "/clone-learning":
        import clone_learning
        from main import _build_pending_minidigest
        pending = clone_learning.list_pending(limit=20)
        if not pending:
            await reply_message(http, reply_token, "🎉 会話発見、未処理ゼロ。")
        else:
            text, qr = _build_pending_minidigest(
                pending, kind="learning",
                title="🧠 会話発見 (未処理一覧)",
            )
            await reply_message(http, reply_token, text, quick_reply=qr)
        return True

    if message.startswith("/clone-learning "):
        import clone_learning
        arg = message[len("/clone-learning "):].strip()
        if not arg:
            await reply_message(
                http, reply_token,
                "使い方:\n"
                "  /clone-learning          — 未レビュー一覧 (Quick Reply 操作付き)\n"
                "  /clone-learning <id>     — 詳細表示\n"
                "  /clone-learning-accept <id> — Wiki 取込 (要 /teach 手動)\n"
                "  /clone-learning-reject <id> — 見送り\n"
                "  /clone-learning-note <id>   — 既読マーク\n"
                "  /clone-learning-scan         — 手動スキャン実行"
            )
            return True
        await reply_message(http, reply_token, clone_learning.detail(arg))
        return True

    # ─── 処理ヘルパー: 共通処理 + 残 mini digest 添付 ───
    async def _handle_learning_action(fid: str, status: str, label: str):
        import clone_learning
        from main import _build_pending_minidigest
        rec = clone_learning.find_by_id(fid)
        if not rec:
            await reply_message(http, reply_token, f"❌ 見つかりません: {fid}")
            return
        clone_learning.update_status(fid, status)
        remaining = clone_learning.list_pending(limit=20)
        confirm = f"{label}: {fid}"
        if status == "accepted":
            patch = rec.get("proposed_wiki_patch") or ""
            insight = rec.get("insight", "")
            teach_body = f"{insight}\n\n提案: {patch}" if patch else insight
            # ★2026-06-20 (監査②b): 手動 /teach 再入力を撤廃。accept で直接 Wiki に反映
            #   (/teach と同じ ingest_note 即コンパイル。海山が明示 accept した内容のみ=承認は維持)。
            if teach_body.strip():
                try:
                    await brain.ingest_note(user_id, teach_body, title="learning", model="smart")
                    confirm += "\n💡 Wiki に反映しました"
                except Exception as _e:
                    confirm += f"\n⚠️ 反映失敗、手動で → /teach {teach_body[:200]}"
            else:
                confirm += "\n(反映する内容なし)"
        if remaining:
            text, qr = _build_pending_minidigest(
                remaining, kind="learning",
                title=f"{confirm}\n\n🧠 残り {len(remaining)} 件",
            )
            await reply_message(http, reply_token, text, quick_reply=qr)
        else:
            await reply_message(
                http, reply_token,
                f"{confirm}\n\n🎉 全件処理完了。お疲れさま。",
            )

    if message.startswith("/clone-learning-accept "):
        fid = message[len("/clone-learning-accept "):].strip()
        await _handle_learning_action(fid, "accepted", "✅ accepted")
        return True

    if message.startswith("/clone-learning-reject "):
        fid = message[len("/clone-learning-reject "):].strip()
        await _handle_learning_action(fid, "rejected", "❌ rejected")
        return True

    if message.startswith("/clone-learning-note "):
        fid = message[len("/clone-learning-note "):].strip()
        await _handle_learning_action(fid, "noted", "📝 noted")
        return True

    if message.strip() == "/clone-learning-scan":
        import clone_learning, os as _os
        await reply_message(http, reply_token, "⏳ 会話ログ走査中...")
        try:
            saved = await clone_learning.run_scan(
                http, _os.getenv("LITELLM_URL", "http://litellm:4000"),
                _os.getenv("LITELLM_MASTER_KEY", ""),
                brain,
                model=_os.getenv("CLONE_LEARNING_MODEL", "fast-gpt"),
            )
            import logging as _lg
            _lg.getLogger(__name__).info(f"manual clone_learning scan: {saved} insights")
        except Exception as e:
            import logging as _lg
            _lg.getLogger(__name__).warning(f"manual scan failed: {e}")
        return True

    # ─── /clone-forget — 特定ユーザの履歴削除 (海山専用) ───
    if message.startswith("/clone-forget "):
        import clone_history
        prefix = message[len("/clone-forget "):].strip()
        if not prefix:
            await reply_message(http, reply_token, "使い方: /clone-forget <user_id>")
            return True
        users = clone_history.list_users()
        matches = [u for u in users if u["user_id"].startswith(prefix)]
        if len(matches) != 1:
            await reply_message(http, reply_token, f"一意にマッチしません (件数={len(matches)})")
            return True
        ok = clone_history.forget(matches[0]["user_id"])
        await reply_message(http, reply_token, "削除しました" if ok else "削除失敗")
        return True

    # ─── /clone-memory — うみやまAI 個別メモリー閲覧 (海山専用) ★2026-05-14 ───
    if message.strip() == "/clone-memory":
        import clone_memory
        users = clone_memory.list_users()
        if not users:
            await reply_message(
                http, reply_token,
                "うみやまAI の個別メモリーはまだありません。\n"
                "(社員と会話が始まると自動的に蓄積されます)"
            )
            return True
        lines = [f"うみやまAI 個別メモリー (計 {len(users)} 名)"]
        for u in users[:20]:
            disp = u.get("display") or u["user_id"][:12] + "..."
            lines.append(
                f"  {disp}: turn {u['turn_count']} / {u['size']}字 "
                f"(id={u['user_id'][:12]}...)"
            )
        lines.append("\n個別表示: /clone-memory <user_id_prefix>")
        lines.append("削除:    /clone-memory-forget <user_id_prefix>")
        await reply_message(http, reply_token, "\n".join(lines))
        return True

    if message.startswith("/clone-memory "):
        import clone_memory
        prefix = message[len("/clone-memory "):].strip()
        if not prefix:
            await reply_message(http, reply_token, "使い方: /clone-memory <user_id_prefix>")
            return True
        matches = clone_memory.find_users(prefix)
        if not matches:
            await reply_message(http, reply_token, f"該当メモリーなし: {prefix}")
            return True
        if len(matches) > 1:
            preview = "\n".join(f"  {m[:20]}" for m in matches[:10])
            await reply_message(
                http, reply_token,
                f"複数マッチ ({len(matches)}件):\n{preview}\nより詳しい prefix で指定してください"
            )
            return True
        dump = clone_memory.dump_user(matches[0])
        if len(dump) > 4800:
            dump = dump[:4800] + "\n...(truncated)"
        await reply_message(http, reply_token, dump)
        return True

    if message.startswith("/clone-memory-forget "):
        import clone_memory
        prefix = message[len("/clone-memory-forget "):].strip()
        if not prefix:
            await reply_message(http, reply_token, "使い方: /clone-memory-forget <user_id_prefix>")
            return True
        matches = clone_memory.find_users(prefix)
        if len(matches) != 1:
            await reply_message(
                http, reply_token,
                f"一意にマッチしません (件数={len(matches)})。詳しい prefix で指定してください"
            )
            return True
        ok = clone_memory.forget(matches[0])
        await reply_message(
            http, reply_token,
            f"✅ メモリー削除: {matches[0][:20]}" if ok else "削除失敗"
        )
        return True

    # ─── ★2026-05-24 Feature 3/4: /audit-recent — bot 応答 audit 待ち一覧 (海山専用) ───
    if message.strip().startswith("/audit-recent"):
        import clone_audit
        parts = message.strip().split()
        limit = 10
        if len(parts) >= 2 and parts[1].isdigit():
            limit = min(int(parts[1]), 30)
        items = clone_audit.list_recent_unrated(limit=limit)
        if not items:
            await reply_message(
                http, reply_token,
                "📋 audit 待ち応答 ゼロ\n"
                "今 audit 可能な bot 応答は無いです。/clone-public でテスト or 社員質問待ち。"
            )
            return True
        lines = [f"📋 audit 待ち {len(items)} 件 (verdict 送信で記録):\n"]
        for it in items:
            ch = f" [G:{it['channel_id'][:6]}]" if it.get("channel_id") else " [DM]"
            disp = it.get("user_display") or it["user_id"][:8]
            lines.append(
                f"[{it['index']}] {it['ts'][:16]} {disp}{ch}\n"
                f"  Q: {it['user_query'][:60]}\n"
                f"  A: {it['bot_response'][:80]}\n"
            )
        lines.append(
            "\n操作:\n"
            "  ○ <番号>     = good (= 正しい)\n"
            "  × <番号> note = bad (= 誤り)\n"
            "  ! <番号> 正しくは XXX = fix (= 修正案)"
        )
        await reply_message(http, reply_token, "\n".join(lines))
        return True

    # ─── /audit-stats — audit 統計 (海山専用) ───
    if message.strip().startswith("/audit-stats"):
        import clone_audit
        parts = message.strip().split()
        days = 30
        if len(parts) >= 2 and parts[1].isdigit():
            days = int(parts[1])
        stats = clone_audit.audit_stats(days=days)
        lines = [
            f"📊 audit 統計 (過去 {days} 日)",
            f"━━━━━━━━━━━━━━━━━",
            f"総 audit 数: {stats['n_total_audits']}",
            f"  good (○): {stats['n_good']}",
            f"  bad  (×): {stats['n_bad']}",
            f"  fix  (!): {stats['n_fix']}",
            f"good 率:    {stats['good_rate_pct']}%",
            f"━━━━━━━━━━━━━━━━━",
        ]
        if stats["needs_attention"]:
            lines.append("\n要 attention (= bad/fix 最新 5 件):")
            for r in stats["needs_attention"][-5:]:
                lines.append(
                    f"  [{r['verdict']}] {r['ts']} {r['user_query'][:40]}\n"
                    f"    → {r['bot_response'][:50]}"
                )
                if r.get("note"):
                    lines.append(f"    note: {r['note'][:60]}")
        await reply_message(http, reply_token, "\n".join(lines))
        return True

    # ─── ★2026-05-24 AI Research Agent: /research 系 (海山専用、海山指示「リサーチャー追加」) ───
    if message.strip().startswith("/research"):
        import os as _os
        from pathlib import Path as _P
        RESEARCH_DIR = _P(_os.getenv("BRAIN_APP_ROOT", "/app")) / "data" / "brain" / "ai_research"

        # /research-run — 即時 1 回 research run (= debug / on-demand 取得)
        if message.strip() == "/research-run":
            await reply_message(
                http, reply_token,
                "🔬 AI research 開始 (= 通常 2-3 分)。完了したら LINE Push します。"
            )
            # background で実行
            import asyncio
            async def _run_bg():
                try:
                    import sys as _sys
                    if "/app/scripts" not in _sys.path:
                        _sys.path.insert(0, "/app/scripts")
                    from ai_research_agent import run  # type: ignore
                    await run()
                except Exception as e:
                    logger.exception(f"ai_research_agent run failed: {e}")
            asyncio.create_task(_run_bg())
            return True

        # /research-list — 過去 digest 一覧
        if message.strip() == "/research-list":
            if not RESEARCH_DIR.exists():
                await reply_message(http, reply_token,
                                    "📁 research 履歴なし (= まだ 1 度も走ってない)")
                return True
            files = sorted(RESEARCH_DIR.glob("*-digest.md"), reverse=True)[:20]
            if not files:
                await reply_message(http, reply_token, "📁 digest 履歴なし")
                return True
            lines = [f"📁 AI research digest 履歴 ({len(files)} 件):"]
            for f in files:
                size_kb = f.stat().st_size / 1024
                lines.append(f"  {f.stem}  ({size_kb:.1f}KB)")
            lines.append("\n操作:")
            lines.append("  /research  → 最新 digest 表示")
            lines.append("  /research <YYYY-MM-DD> → 特定日表示")
            lines.append("  /research-proposals → 提案 一覧")
            lines.append("  /research-run → 即時 1 回 research 実行")
            await reply_message(http, reply_token, "\n".join(lines))
            return True

        # /research-proposals — pending proposals 一覧
        if message.strip().startswith("/research-proposals"):
            prop_file = RESEARCH_DIR / "proposals.jsonl"
            if not prop_file.exists():
                await reply_message(http, reply_token, "📋 提案 履歴なし")
                return True
            try:
                lines_raw = prop_file.read_text(encoding="utf-8").strip().splitlines()
                proposals = []
                for ln in lines_raw:
                    try:
                        proposals.append(json.loads(ln))
                    except Exception:
                        continue
            except Exception as e:
                await reply_message(http, reply_token, f"❌ 提案 file 読込失敗: {e}")
                return True
            pending = [p for p in proposals if p.get("status") == "pending"]
            accepted = [p for p in proposals if p.get("status") == "accepted"]
            lines = [
                f"📋 AI Research 提案",
                f"━━━━━━━━━━━━━━━━",
                f"pending:  {len(pending)} 件",
                f"accepted: {len(accepted)} 件",
                f"total:    {len(proposals)} 件",
                f"━━━━━━━━━━━━━━━━",
            ]
            for p in pending[-10:]:
                lines.append(
                    f"  [{p['id']}] {p.get('title', '?')[:60]}"
                )
            lines.append("\n操作:")
            lines.append("  /research-accept <id>  → 提案 accept (= 反映決定)")
            lines.append("  /research-reject <id>  → 提案 reject")
            await reply_message(http, reply_token, "\n".join(lines))
            return True

        # /research-accept <id> / /research-reject <id>
        for verb in ("accept", "reject"):
            prefix = f"/research-{verb}"
            if message.strip().startswith(prefix):
                _parts = message.strip().split(maxsplit=1)
                if len(_parts) < 2:
                    await reply_message(http, reply_token,
                                        f"使い方: {prefix} <proposal_id>")
                    return True
                target_id = _parts[1].strip()
                prop_file = RESEARCH_DIR / "proposals.jsonl"
                if not prop_file.exists():
                    await reply_message(http, reply_token, "提案 file 無し")
                    return True
                try:
                    lines_raw = prop_file.read_text(encoding="utf-8").strip().splitlines()
                    updated = []
                    matched = False
                    matched_p = None
                    new_status = "accepted" if verb == "accept" else "rejected"
                    for ln in lines_raw:
                        try:
                            p = json.loads(ln)
                            if p.get("id") == target_id:
                                p["status"] = new_status
                                p["reviewed_at"] = datetime.now(JST).isoformat()
                                matched = True
                                matched_p = p
                            updated.append(p)
                        except Exception:
                            continue
                    if not matched:
                        await reply_message(http, reply_token,
                                            f"⚠️ proposal id 不一致: {target_id}")
                        return True
                    # ★2026-06-20 (監査③): accept した提案を行き止まりにせず system_issue 化 (actionable backlog へ)
                    _extra = ""
                    if verb == "accept" and matched_p:
                        try:
                            from services import system_issues as _si
                            _sid = _si.add_entry(
                                description=f"[AI Research 提案] {matched_p.get('title', '')}\n\n"
                                            f"{(matched_p.get('body', '') or '')[:1500]}",
                                expected="AI research 提案の実装/検討 (/戦略 で深掘り可)",
                                reviewer="umiyama")
                            matched_p["system_issue_id"] = _sid
                            _extra = f"\n→ system_issue {_sid} 化 (/system で確認)"
                        except Exception as _e:
                            _extra = f"\n⚠️ system_issue 化失敗: {type(_e).__name__}"
                    prop_file.write_text(
                        "\n".join(json.dumps(p, ensure_ascii=False) for p in updated) + "\n",
                        encoding="utf-8",
                    )
                    await reply_message(http, reply_token,
                                        f"✓ {target_id} → {new_status}{_extra}")
                except Exception as e:
                    await reply_message(http, reply_token, f"❌ 更新失敗: {e}")
                return True

        # /research [YYYY-MM-DD] — 最新 or 指定日 digest 表示
        # ※ 厳密 prefix match なので "/research-XXX" は上 branch で処理済
        if message.strip().startswith("/research"):
            _parts = message.strip().split(maxsplit=1)
            target_date = None
            if len(_parts) >= 2:
                target_date = _parts[1].strip()
                # 日付 format 簡易 validation
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", target_date):
                    await reply_message(http, reply_token,
                                        f"⚠️ 日付 format 不正: {target_date} (YYYY-MM-DD)")
                    return True
            if target_date:
                target_file = RESEARCH_DIR / f"{target_date}-digest.md"
            else:
                # 最新 1 件
                files = sorted(RESEARCH_DIR.glob("*-digest.md"), reverse=True)
                target_file = files[0] if files else None
            if not target_file or not target_file.exists():
                await reply_message(http, reply_token,
                                    "📁 digest 無し。/research-run で実行 or /research-list で確認。")
                return True
            try:
                content = target_file.read_text(encoding="utf-8")
                # LINE 送信上限 ~5000 char、超過は preview のみ
                if len(content) > 4500:
                    content = content[:4500] + "\n\n... (truncated、ssh で全文閲覧)"
                await reply_message(http, reply_token,
                                    f"📄 {target_file.name}\n\n{content}")
            except Exception as e:
                await reply_message(http, reply_token, f"❌ 読込失敗: {e}")
            return True

    # ─── /align-voice-status — 雑談アラインメント カバレッジ (海山専用) ★2026-05-18 ───
    if message.strip() == "/align-voice-status":
        import alignment_interview as ai
        await reply_message(http, reply_token, ai.build_status_text())
        return True

    # ─── /align-voice — 蒸留案レビュー (海山専用) ───
    if message.strip() == "/align-voice":
        # ★2026-05-20: silent fail 撲滅 — 例外時もユーザに反応を返す
        try:
            import alignment_interview as ai
            pending = ai.list_pending_extractions()
            if not pending:
                await reply_message(
                    http, reply_token,
                    "🎙️ 蒸留レビュー、未処理ゼロ。\n"
                    "電話で雑談すると溜まる。状況: /align-voice-status"
                )
                return True
            # ★2026-07-06 backfill 洪水対応: magazine-* は filename sort で常に音声より上に
            # 来て top-3 を占有する (音声 = coverage を持つ本来の主役が数週間埋没) →
            # 音声を先頭に並べ替え、magazine は件数のまとめ行 + 残り枠で表示
            voice = [r for r in pending if not r["file"].startswith("magazine-")]
            mags = [r for r in pending if r["file"].startswith("magazine-")]
            lines = [
                "🎙️ 雑談アラインメント 蒸留レビュー",
                f"未処理: {len(pending)} 件"
                + (f" (うち📖magazine {len(mags)} 件 → まとめて採用可)" if mags else ""),
                "━━━━━━━━━━━━━━━",
            ]
            qr: list[dict] = []
            for r in (voice + mags)[:3]:
                fid = r["file"].replace(".json", "")
                # magazine-<id> は日時 slice だと崩れる → mag-<id> ラベル
                short = fid.replace("magazine-", "mag-") if fid.startswith("magazine-") else fid[-9:]  # MM-DD-HHMM
                lines.append(f"\n[{short}] {r['item_count']}件")
                summary = (r.get("summary") or "")[:70]
                if summary:
                    lines.append(f"  全体: {summary}")
                # ★ 2026-05-20: 各 item の見出し 1 行を digest 段階で表示
                #    📄 を押さなくても中身が見えて即 ✅/❌ 判断できる
                try:
                    d = ai.get_extraction(r["file"])
                    if d:
                        items_full = d.get("items", [])
                        for i, it in enumerate(items_full[:5], 1):
                            cat = it.get("category", "")
                            insight = (it.get("insight") or "")[:55]
                            lines.append(f"  ・[{i}] {cat}: {insight}")
                        if len(items_full) > 5:
                            lines.append(f"  ・…他 {len(items_full)-5} 件 (📄 で全件)")
                except Exception:
                    pass  # item preview は best-effort、失敗しても digest 自体は出す
                qr.append({"label": f"✅{short}", "data": f"/align-voice-accept {fid}", "type": "message"})
                qr.append({"label": f"❌{short}", "data": f"/align-voice-reject {fid}", "type": "message"})
                qr.append({"label": f"📄{short}", "data": f"/align-voice {fid}", "type": "message"})
            await reply_message(http, reply_token, "\n".join(lines), quick_reply=qr or None)
            return True
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            logger.error(f"/align-voice failed: {e}\n{tb}")
            try:
                await reply_message(
                    http, reply_token,
                    f"⚠️ /align-voice エラー\n{type(e).__name__}: {str(e)[:200]}\n"
                    f"docker logs line-bot | grep align-voice で詳細"
                )
            except Exception:
                pass
            return True

    # ─── /align-voice <fid> — 蒸留案の詳細 ───
    if message.startswith("/align-voice ") and not message.startswith(
        ("/align-voice-accept", "/align-voice-reject")
    ):
        import alignment_interview as ai
        fid = message[len("/align-voice "):].strip()
        d = ai.get_extraction(fid + ".json") or ai.get_extraction(fid)
        if not d:
            await reply_message(http, reply_token, f"見つかりません: {fid}")
            return True
        lines = [f"🎙️ 蒸留案 {fid}", f"状態: {d.get('status')}", "━━━━━━━━━━━━━━━"]
        for i, it in enumerate(d.get("items", []), 1):
            # ★2026-07-04: 書込先の真実源は category → _CATEGORY_WIKI (wiki_target は廃止 field)
            target = ai._CATEGORY_WIKI.get(it.get("category", ""), "interview/misc.md")
            lines.append(
                f"\n[{i}] {it.get('category')} ({it.get('confidence')}) → {target}"
            )
            lines.append(f"  {it.get('insight','')[:140]}")
            if it.get("evidence_quote"):
                lines.append(f"  「{it['evidence_quote'][:60]}」")
        lines.append("\n━━━━━━━━━━━━━━━")
        lines.append(f"全採用: /align-voice-accept {fid}")
        lines.append(f"却下:   /align-voice-reject {fid}")
        await reply_message(http, reply_token, "\n".join(lines))
        return True

    # ─── /align-voice-accept all — pending 一括採用 (★2026-07-04 digest ワンタップ用) ───
    # ★DA HIGH 対策: 無条件全採用は「唯一の人間ゲート」を飾りにする。high/medium のみ反映し、
    # low・『推測:』は pending に残す = ワンタップの便利さと実効ゲートの両立。
    if message.strip() == "/align-voice-accept all":
        import alignment_interview as ai
        pend = ai.list_pending_extractions()
        if not pend:
            await reply_message(http, reply_token, "レビュー待ちは 0 件。")
            return True
        total, held, files = 0, 0, set()
        for p in pend:
            r = ai.apply_extraction_confident(p["file"])
            total += r.get("applied", 0)
            held += r.get("held", 0)
            files.update(r.get("files", []))
        flist = "\n".join(f"  - {f}" for f in sorted(files))
        msg = (f"✅ {total} 件を wiki に反映 ({len(pend)} 通話ぶん)\n{flist}\n"
               f"(全て clone_visibility: private = 真クローン用)")
        if held:
            msg += (f"\n⏸ 保留 {held} 件 (low・推測 = 個別判断待ち)"
                    f"\n→ /align-voice で確認")
        await reply_message(http, reply_token, msg)
        return True

    # ─── /align-voice-accept <fid> — 蒸留案を wiki に反映 ───
    if message.startswith("/align-voice-accept "):
        import alignment_interview as ai
        fid = message[len("/align-voice-accept "):].strip()
        r = ai.apply_extraction(fid + ".json")
        if r.get("error"):
            await reply_message(http, reply_token, f"❌ {r['error']}: {fid}")
            return True
        files = "\n".join(f"  - {f}" for f in r.get("files", []))
        remaining = ai.list_pending_extractions()
        msg = (
            f"✅ {r['applied']} 件を wiki に反映 ({fid})\n{files}\n"
            f"(全て clone_visibility: private = 真クローン用)\n"
            f"残レビュー: {len(remaining)} 件"
        )
        await reply_message(http, reply_token, msg)
        return True

    # ─── /align-voice-reject <fid> — 蒸留案を却下 ───
    if message.startswith("/align-voice-reject "):
        import alignment_interview as ai
        fid = message[len("/align-voice-reject "):].strip()
        ok = ai.reject_extraction(fid + ".json")
        remaining = ai.list_pending_extractions()
        await reply_message(
            http, reply_token,
            (f"🗑️ 却下: {fid}\n残レビュー: {len(remaining)} 件"
             if ok else f"見つかりません: {fid}")
        )
        return True

    # ─── /lint — Wiki健康診断 ───
    if message.strip() == "/lint":
        await reply_message(http, reply_token, "Wiki をスキャン中...")
        result = await brain.lint()

        score = result.get("health_score", "?")
        issues = result.get("issues", [])
        coverage = result.get("coverage", {})

        cov_text = "\n".join(f"  {k}: {v}" for k, v in coverage.items())
        issue_text = "\n".join(
            f"  [{i['severity']}] {i['type']}: {i['description']}"
            for i in issues[:5]
        )

        text = (
            f"Wiki Health: {score}/100\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"カバレッジ:\n{cov_text}\n\n"
            f"検出された問題 ({len(issues)}件):\n"
            f"{issue_text or '  なし'}"
        )
        await reply_message(http, reply_token, text)
        return True

    # ─── /dedup — Wiki の重複統合 ───
    if message.strip() == "/dedup":
        await reply_message(http, reply_token, "Wiki を dedup 中…")
        try:
            result = await brain.dedup_all()
        except Exception as e:
            logger.exception("dedup_all failed")
            await reply_message(http, reply_token, f"⚠️ dedup エラー: {e}")
            return True

        changed_files = result.get("files", [])
        lines = [
            "Wiki Dedup 完了",
            "━━━━━━━━━━━━━━━",
            f"対象ファイル:   {result['total']} 件",
            f"変更あり:       {result['changed']} 件",
            f"削減バイト:     {result['bytes_saved']:,} bytes",
        ]
        if changed_files:
            lines.append("")
            lines.append("変更ファイル（上位5件）:")
            for r in changed_files[:5]:
                saved = r.get("before_bytes", 0) - r.get("after_bytes", 0)
                lines.append(f"  - {r['path']}  -{saved:,}B")
        await reply_message(http, reply_token, "\n".join(lines))
        return True

    # ─── /line-skip / /line-reminder-off / /line-reminder-on ───
    if message.strip() in ("/line-skip", "/line-reminder-off", "/line-reminder-on"):
        from pathlib import Path as _Path
        disabled_flag = _Path("/app/data/brain/line_reminder_disabled.txt")
        cmd = message.strip()
        if cmd == "/line-skip":
            await reply_message(
                http,
                reply_token,
                "👍 今週はスキップしました。次の日曜20時に再度お知らせします。",
            )
            return True
        elif cmd == "/line-reminder-off":
            disabled_flag.parent.mkdir(parents=True, exist_ok=True)
            disabled_flag.write_text("disabled\n", encoding="utf-8")
            await reply_message(
                http,
                reply_token,
                "🔕 LINE 取り込みリマインドを停止しました。\n"
                "再開する場合は /line-reminder-on を送ってください。",
            )
            return True
        elif cmd == "/line-reminder-on":
            if disabled_flag.exists():
                disabled_flag.unlink()
            await reply_message(
                http,
                reply_token,
                "🔔 LINE 取り込みリマインドを再開しました。次回は日曜20時です。",
            )
            return True

    # ─── /graph — Brain Map（力学ネットワーク図）URL を返す ───
    if message.strip() == "/graph":
        import os
        from pathlib import Path
        tunnel_file = Path("/app/data/brain/tunnel_url.txt")
        tunnel_url = tunnel_file.read_text().strip() if tunnel_file.exists() else ""
        key = os.getenv("BRAIN_EXTENSION_KEY", "")
        if not tunnel_url:
            await reply_message(
                http,
                reply_token,
                "Cloudflare Tunnel が未起動のようです。\n"
                "ローカルで見るには: http://localhost:8000/brain/graph",
            )
            return True
        url = f"{tunnel_url}/brain/graph?key={key}" if key else f"{tunnel_url}/brain/graph"
        text = (
            "🧠 Brain Map\n"
            f"{url}\n"
            "━━━━━━━━━━━━━━━━━\n"
            "タップで開きます（モバイル対応）\n"
            "• ノードクリック → 全文プレビュー\n"
            "• ドラッグ / ピンチでズーム\n"
            "• 検索 / カテゴリフィルタあり"
        )
        await reply_message(http, reply_token, text)
        return True

    # ─── /wiki — Wiki記事を検索・表示 ───
    if message.startswith("/wiki "):
        query = message[6:].strip().lower()
        from pathlib import Path
        from brain_wiki import WIKI_DIR

        matches = []
        for f in WIKI_DIR.rglob("*.md"):
            name = f.stem.lower()
            content_preview = f.read_text(encoding="utf-8")[:200]
            if query in name or query in content_preview.lower():
                matches.append((f.relative_to(WIKI_DIR), content_preview))

        if matches:
            text = f"「{query}」の検索結果:\n\n"
            for rel, preview in matches[:3]:
                text += f"[{rel}]\n{preview[:150]}...\n\n"
        else:
            text = f"「{query}」に一致する記事が見つかりません"

        await reply_message(http, reply_token, text)
        return True

    # ─── /forward — 転送メッセージを即時学習 ───
    if message.startswith("/forward "):
        content = message[9:].strip()
        if not content:
            await reply_message(http, reply_token, "使い方: /forward 転送したいメッセージ")
            return True
        privacy = app.state.privacy
        result = await privacy.filter(content, sender_id=user_id)
        if result.verdict.value == "allow":
            # 転送は明示的な保存指示なので smart を使用
            await brain.ingest_note(
                user_id, result.sanitized, title="forwarded", model="smart"
            )
            await reply_message(http, reply_token, "転送メッセージをWikiに取り込みました")
        else:
            await reply_message(http, reply_token, f"取り込みスキップ: {result.verdict.value}")
        return True

    # ─── /align — アライメント質疑 ───
    if message.strip() == "/align":
        await reply_message(http, reply_token, "考え中...")
        q = await brain.generate_alignment_question()
        question = q.get("question", "")
        category_labels = {
            "orientation": "指向",
            "thinking": "思考",
            "taste": "趣向",
            "reaction": "反応",
            "contradiction": "矛盾探索",
        }
        cat = category_labels.get(q.get("category", ""), q.get("category", ""))
        # 質問データをRedisに保存（回答待ち）
        import json
        r = app.state.redis
        await r.set(
            f"align:{user_id}",
            json.dumps(q, ensure_ascii=False),
            ex=86400,  # 24時間有効
        )
        text = f"[{cat}]\n{question}"
        await reply_message(http, reply_token, text)
        return True

    return False


async def handle_alignment_answer(app, user_id: str, message: str) -> bool:
    """アライメント質問への回答を処理。回答待ち状態ならTrue。"""
    import json
    r = app.state.redis
    brain: BrainWiki = app.state.brain
    pending = await r.get(f"align:{user_id}")
    if not pending:
        return False

    question_data = json.loads(pending)
    await brain.process_alignment_answer(question_data, message)
    await r.delete(f"align:{user_id}")
    return True


async def background_ingest(brain: BrainWiki, user_id: str, user_msg: str, ai_reply: str):
    """通常会話後にバックグラウンドで raw 蓄積（非ブロッキング）"""
    try:
        await brain.ingest_conversation(user_id, user_msg, ai_reply)
    except Exception as e:
        logger.info(f"Background ingest failed: {e}")


# ─── Cron用: 定期Lint ───
async def scheduled_lint(brain: BrainWiki):
    """毎日1回実行: Wiki全体をLintして自動修正"""
    result = await brain.lint()
    if result.get("health_score", 100) < 70:
        await brain.auto_fix(result)
    return result
