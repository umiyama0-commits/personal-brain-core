#!/bin/bash
# cron_install.sh — Mac Studio 側の crontab を必要な行で揃える (冪等)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ★2026-05-20: MacBook から push した cron 設定を、Mac Studio で
# 物理的に触らずに auto_deploy 経由で自動登録するため。
#
# 使い方:
#   - auto_deploy.sh の最後で呼ばれる (毎 cycle)
#   - 手動: bash scripts/cron_install.sh
#
# 動作:
#   1. 必要な cron 行を REQUIRED_CRONS 配列で定義
#   2. 現在の crontab を読む
#   3. 必要行が無ければ追加 (script path + mode で同定、重複追加しない)
#   4. 変更があれば crontab に書き戻す
#   5. 変更ログを stdout に出す (auto_deploy.log に残る)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

set -uo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/brain/brain-agent}"
LOG_DIR="$REPO_ROOT/data/brain/clone_improve"
mkdir -p "$LOG_DIR"

# ★2026-07-02 cross-check DA (必須#2): LaunchAgent (com.brain.cron-install) 文脈は .env 未読で
# LINE/LW token が無く、下の loud_fail 通知が 1 通も送れなかった (= P1 の loud 化が当の文脈で無効)。
# cron_env.sh を source して PATH + .env + LITELLM_URL を揃える (auto_deploy 経由でも冪等・無害)。
[ -f "$REPO_ROOT/scripts/cron_env.sh" ] && . "$REPO_ROOT/scripts/cron_env.sh"

# ★2026-07-02 監査 P1f + cross-check DA: git hooks (gitleaks pre-commit) を冪等再設置。
# .git/hooks は非追跡 = 再 clone で消える per-clone 資産。ここで毎 cycle 設置し直すことで
# 「hook が無いまま secret を commit」の silent gap (§1.18) を塞ぐ。失敗は非致命 (cron 継続)。
[ -f "$REPO_ROOT/scripts/install_git_hooks.sh" ] && bash "$REPO_ROOT/scripts/install_git_hooks.sh" >/dev/null 2>&1 || true

# ─── 登録すべき cron 行 (1 entry = 1 行、フル絶対パスで) ───
# フォーマット: "<schedule> <command>"
REQUIRED_CRONS=(
"30 2 * * * $REPO_ROOT/scripts/clone_cron.sh metrics >> $LOG_DIR/cron.log 2>&1"
"0 3 * * * $REPO_ROOT/scripts/clone_cron.sh improve >> $LOG_DIR/cron.log 2>&1"
"30 3 * * * $REPO_ROOT/scripts/clone_cron.sh regression >> $LOG_DIR/cron.log 2>&1"
"45 3 * * * $REPO_ROOT/scripts/clone_cron.sh hallucination >> $LOG_DIR/cron.log 2>&1"
"50 3 * * * $REPO_ROOT/scripts/clone_cron.sh monitor-daily >> $LOG_DIR/cron.log 2>&1"
"40 4 * * * $REPO_ROOT/scripts/clone_cron.sh ingest-audit >> $LOG_DIR/cron.log 2>&1"
"*/5 * * * * $REPO_ROOT/scripts/clone_cron.sh uptime-monitor >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-15: host_docker_watchdog は crontab でなく LaunchAgent (com.brain.docker-watchdog) で
#   登録する — cron 起動文脈は crontab 書込み(TCC)も open -a Docker(GUI)も不可なため。
#   → deploy/launchagents/com.brain.docker-watchdog.plist + scripts/install_launchagents.sh 参照。
"0 4 * * * $REPO_ROOT/scripts/clone_cron.sh eval-baseline >> $LOG_DIR/cron.log 2>&1"
"5 4 * * * $REPO_ROOT/scripts/clone_cron.sh privacy-review >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-10 開発方針 (CLAUDE.md/docs/decisions) 変更の Fable5 最終チェック (変更無い日は LLM 0 call)
"10 7 * * * $REPO_ROOT/scripts/clone_cron.sh policy-check >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-10 世界基準評価 #3: CI(main)の赤を毎日 poll → 赤転換時のみ通知 (gh 認証済み前提)
"15 7 * * * $REPO_ROOT/scripts/clone_cron.sh ci-check >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-10 世界基準評価 S4b: retrieval golden eval を週次運用 (BM25-only=bot停止不要)
"0 5 * * 0 $REPO_ROOT/scripts/clone_cron.sh golden-eval >> $LOG_DIR/cron.log 2>&1"
"*/30 * * * * $REPO_ROOT/scripts/clone_cron.sh response-quality >> $LOG_DIR/cron.log 2>&1"
"0 9 * * * $REPO_ROOT/scripts/clone_cron.sh cost-daily >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-20 Umiyama AI Agent 正式化: info 系通知の 1日2回まとめ配信 (空なら無音)
"0 10,19 * * * $REPO_ROOT/scripts/clone_cron.sh notify-digest >> $LOG_DIR/cron.log 2>&1"
"5 9,21 * * * $REPO_ROOT/scripts/clone_cron.sh credit-check >> $LOG_DIR/cron.log 2>&1"
"0 9 * * 1 $REPO_ROOT/scripts/clone_cron.sh weekly >> $LOG_DIR/cron.log 2>&1"
"30 9 * * 1 $REPO_ROOT/scripts/clone_cron.sh ai-research >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-20 OWNDAYS 事業向け AI 活用提言(ai_research 直後、public wiki 化 + 海山 push)
"35 9 * * 1 $REPO_ROOT/scripts/clone_cron.sh ai-advisor >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-29 海山指示「ギャップ狙い撃ち質問を定期的に」: 人格の薄い次元を問う(月曜 08:00 起動、
# script 側 --cadence biweekly で隔週に間引き=偶数 ISO 週のみ実 push、ピルアップ回避)
"0 8 * * 1 $REPO_ROOT/scripts/clone_cron.sh persona-gap >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-29 海山指示「Example PJ 全会話を自動取込(両方)」: 監視フォルダの export zip を取込む backstop(日次 06:30)
"30 6 * * * $REPO_ROOT/scripts/clone_cron.sh export-watch >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-01 海山指示: クローンがデータ不足で答えられなかった質問を週次で surface(埋める backlog、月曜 08:15)
"15 8 * * 1 $REPO_ROOT/scripts/clone_cron.sh gap-detect >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-01 海山指示: Claude Code 開発セッションを増分で personal/dev に蓄積(人格非直結、日次 06:40)
"40 6 * * * $REPO_ROOT/scripts/clone_cron.sh dev-journal >> $LOG_DIR/cron.log 2>&1"
"0 18 * * 1 $REPO_ROOT/scripts/kpi_dash_cron.sh >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-08 海山指摘 bug fix: 月曜(1)を追加。Section 1「最新完結週(Mon-Sun)週次合計」は
#   get_latest_completed_week(月) が前日(日)で終わる週 = 先週を正しく出すが、cron が火/水のみ
#   だと月曜に rebuild されず、月曜に「先週の売上は?」と聞くと先々週(直近 build 時の週)を返す
#   bug があった (例: 6/8 月に 5/25〜5/31 を返答、正は 6/1〜6/7)。totaldaily は日曜 23:00 scrape
#   で前日分まで入るので月曜 03:00 build で 6/1〜6/7 を集計可能。Section 2-4(Monday Dash report
#   /kpi/Drive)は月曜夕方に source が landする = 月曜 03:00 時点では先週分のまま(火/水 build で
#   最新化、これは現状と同じで regression 無し)。
"0 3 * * 1,2,3 $REPO_ROOT/scripts/monday_dash_latest_cron.sh >> $LOG_DIR/cron.log 2>&1"
"35 23 * * * $REPO_ROOT/scripts/stores_by_customer_range_cron.sh >> $LOG_DIR/cron.log 2>&1"
"5 4 * * * $REPO_ROOT/scripts/quality_metrics_cron.sh >> $LOG_DIR/cron.log 2>&1"
"10 4 * * 1 $REPO_ROOT/scripts/style_reflux_cron.sh >> $LOG_DIR/cron.log 2>&1"
"20 4 * * 1 $REPO_ROOT/scripts/clone_cron.sh synthetic >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-20 Analyst Agent Phase2 (ADR §11 B): /分析 依頼を 2 分毎に処理し結果を LW push。
#   空キューなら ~no-op、多重起動は dispatch_run の mkdir lock で防止。
"*/2 * * * * $REPO_ROOT/scripts/analyst/dispatch_cron.sh >> $REPO_ROOT/data/brain/analyst_dispatch.log 2>&1"
# ★2026-06-20 戦略アナリスト(consultant): /戦略・戦略的問いを処理し意思決定メモを LW push。
#   奇数分(1-59/2)= analyst(偶数分)とずらし、Docker 同時 spawn を回避(DA の飽和懸念対策)。別 lock。
"1-59/2 * * * * $REPO_ROOT/scripts/consultant/dispatch_cron.sh >> $REPO_ROOT/data/brain/consultant_dispatch.log 2>&1"
# ★2026-07-11 出店候補 (tenpo): /出店 依頼を処理 (recommend 決定論抽出 + LLM 整形 1 call のみ=軽量)し
#   2 タイプ候補を LW push。別 root/別 lock、奇数分 (consultant 側)。
"1-59/2 * * * * $REPO_ROOT/scripts/tenpo/dispatch_cron.sh >> $REPO_ROOT/data/brain/tenpo_dispatch.log 2>&1"
# ★2026-06-08 評価#4: 月初(1日 09:00)に第三者 blind 採点 form 生成 + 海山へ配布通知 →
# judge κ (LLM-human agreement) の human data を起動 (κ が永久 no_data だったのを解消)。
"0 9 1 * * $REPO_ROOT/scripts/clone_cron.sh external-eval >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-08 システム評価 1-3: スクレイプ系/ヘルスチェック系の自己完結 wrapper も登録対象に。
# 従来 Mac Studio で手動 crontab 登録のみ → 再起動/crontab 事故で sales 取得が無音停止する
# resilience gap を解消。schedule/redirect は各 wrapper header (= runbook) と厳密一致させ、
# 既存手動エントリと dedup (pattern = wrapper path) して二重実行を避ける。
"0 9,11,13,15,17,19,21,23 * * * $REPO_ROOT/scrape_cron.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1"
"30 5 * * * $REPO_ROOT/scripts/health_cron.sh >> $REPO_ROOT/data/brain/health.log 2>&1"
"0 8 * * * $REPO_ROOT/scripts/plaud_daily_sync.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1"
# ★2026-07-11 採用レビュー #2: 社内規程 共有ドライブの月次取込 + 監視 (制度 FAQ の unmet 66% 解消)。
# 既存 gdrive cron は plaud/monday-dash のみ (--all 無し) で規程を回さなかった。DL 権限付与後は
# 次回実行で自動全件取込、未付与の間は loud_fail で通知 (7/6 の silent 死の再発防止)。月次 (規程は低頻度)。
"0 6 1 * * $REPO_ROOT/scripts/regulations_sync.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1"
# ★2026-07-03 海山指示: umiyama の web 会議へ Recall bot 自動参加 → 議事録 → wiki (Plaud と同経路)。
# 10分毎 (7-22時)。MEETING_AUTOJOIN_ENABLED=1 gate、denylist/社外除外は meeting_autojoin.py。
"*/10 7-22 * * * $REPO_ROOT/scripts/meeting_autojoin_cron.sh >> $REPO_ROOT/data/brain/meeting_autojoin.log 2>&1"
# ★2026-06-08 評価 SRE/Alignment: wiki/会話/alignment の hourly git snapshot。自律編集
# (clone_auto_improve/synthetic) の rollback 網 + 一次データの最終保全。これまで「想定」のみで
# cron 未登録 = 稼働保証が無かった。※ offsite は backup_wiki 内 git の origin remote 設定が前提 (海山確認要)。
"0 * * * * $REPO_ROOT/backup_wiki.sh >> $REPO_ROOT/data/brain/backup.log 2>&1"
# ★2026-06-08 評価 Tier0 #1: 再生成不能な一次データ (clone_history/memory/alignment/会話) を
# restic で暗号化 offsite (B2/S3) へ 6h おき。restic/creds 未設定なら loud-skip (= 設定まで無害)。
"0 */6 * * * $REPO_ROOT/scripts/backup_offsite.sh >> $REPO_ROOT/data/brain/backup_offsite.log 2>&1"
# ★2026-06-08 評価#2: restore drill = backup から実際に復元できるか週次検証 (日曜 05:00)。
# 「テストしてない backup は backup でない」を塞ぐ。未設定なら loud-skip。
"0 5 * * 0 $REPO_ROOT/scripts/backup_restore_drill.sh >> $REPO_ROOT/data/brain/backup_restore_drill.log 2>&1"
# ★2026-06-08 海山指示: Codex (別系列 GPT-5) で Claude のコードを独立レビュー。
# 週次 (日曜 07:00) = 直近 1 週間 diff、月次 (1日 07:30) = 全体 sweep。指摘ありの時だけ LINE。
# CODEX_API_KEY 未設定なら loud-skip。
"0 7 * * 0 $REPO_ROOT/scripts/codex_review.sh weekly >> $REPO_ROOT/data/brain/codex_review.log 2>&1"
"30 7 1 * * $REPO_ROOT/scripts/codex_review.sh monthly >> $REPO_ROOT/data/brain/codex_review.log 2>&1"
# ★2026-06-10 運用堅牢化: 取り込み済みファイル/ログの retention (無限増殖の掃除)。日次 04:15。
# import/processed の delete 系は file watcher / scrape と被らない早朝帯に置く。
"15 4 * * * $REPO_ROOT/scripts/housekeeping_cron.sh >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-10 運用堅牢化: STAPA scraper を wrapper 経由に (.env source して creds 欠落の
# 無音ログイン失敗を解消)。schedule は従来手動 crontab と同じ 22:30 / 14 日おきを踏襲。
# 旧 bare-python entry (python3 .../stapa_scraper.py) は下の legacy 除去ステップで掃除。
"30 22 */14 * * $REPO_ROOT/scripts/stapa_cron.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1"
# ★2026-06-28 Step 2 還流: 各PJ→Core 判断軸蒸留 (propose-only、Core 書込は海山承認時のみ)。日次 02:10。
"10 2 * * * $REPO_ROOT/scripts/clone_cron.sh core-reflux >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-05 Phase 1 孤島接続: graph エッジ候補の提案生成 (propose-only、採用は /bridge)。日次 02:20。
"20 2 * * * $REPO_ROOT/scripts/clone_cron.sh bridge >> $LOG_DIR/cron.log 2>&1"
# ★2026-06-28 personal 保管: wiki/personal/ の版管理 snapshot (gitignore=本体git履歴無の補完)。日次 04:25。
"25 4 * * * $REPO_ROOT/scripts/clone_cron.sh personal-snapshot >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-02 海山指示: 月額サービス領収書を月1収集 → Google Drive (毎月1日 10:00、要 --auth 初回)。
"0 10 1 * * $REPO_ROOT/scripts/receipt_cron.sh >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-05 海山指示「トークを wikiに」: git 追跡 inbox の chat export 配送 (15分毎)。
# owndays→IMPORT_DIR (既存 pipeline) / personal→wiki/personal 直書き (§1.17)。冪等 (sha256 state)。
"*/15 * * * * $REPO_ROOT/scripts/clone_cron.sh import-inbox >> $LOG_DIR/cron.log 2>&1"
# ★2026-07-06 海山指示「2も進めて」: もぐもぐ過去号 backfill orchestrator (毎時 8-22)。
# git 配送の request 無し/完了済みなら即無音 exit。scrape --all は request 毎に 1 回、
# 蒸留は 15 号ずつ + レビュー滞留ゲート (pending ≥ 10 で待機)。lock で重複起動防止。
"15 8-22 * * * $REPO_ROOT/scripts/magazine_backfill_cron.sh >> $REPO_ROOT/data/brain/scrape.log 2>&1"
# ★2026-07-02 監査 P2 (unmanaged-crontab-entries): 手動登録のみだった 6 本を REQUIRED 化。
# crontab 消失/再構築時に自動復元されなかった gap を解消 (特に auto_deploy = デプロイの生命線)。
# 既存 crontab 行と同一コマンド (schedule/挙動の変更なし)、dedup は下 PATTERNS で効く。
"*/5 * * * * $REPO_ROOT/scripts/auto_deploy.sh >> $REPO_ROOT/data/brain/auto_deploy.log 2>&1"
"30 23 * * * python3 $REPO_ROOT/apple_notes_sync.py >> $REPO_ROOT/data/brain/scrape.log 2>&1"
"0 8,12,18 * * * $REPO_ROOT/.venv/bin/python3 $REPO_ROOT/sync_to_claude_project.py >> $REPO_ROOT/data/brain/scrape.log 2>&1"
"0 4 * * 1 bash $REPO_ROOT/scripts/extractors/weekly_batch.sh >> $REPO_ROOT/data/brain/extractor_state/weekly_batch.log 2>&1"
"30 4 1 * * bash $REPO_ROOT/scripts/extractors/alignment_cron.sh >> $REPO_ROOT/data/brain/extractor_state/alignment_snapshot.log 2>&1"
"0 6 * * * cd $REPO_ROOT && set -a && . ./.env && set +a && [[ \"\$LITELLM_URL\" == *\"litellm:\"* ]] && export LITELLM_URL=\"http://localhost:4000\"; /usr/bin/python3 scripts/sales_accuracy_check.py >> $REPO_ROOT/data/brain/extractor_state/sales_accuracy.log 2>&1"
# ★2026-07-01 海山指示「月一回 OWNDAYS と主要競合の出店状況をアップデート」: 眼鏡チェーン空白地ダッシュボード
# (SG/TH/TW)の店舗+施設データ月次更新。毎月1日 04:10。境界/人口/所得は committed base 固定、店舗+モールのみ再取得
# → matrix → HTML 再生成 → data/brain/web/ へ cp → commit/push → auto_deploy 反映。国別 sanity gate + §1.18 loud_fail。
"10 4 1 * * $REPO_ROOT/scripts/whitespace_refresh.sh >> $LOG_DIR/cron.log 2>&1"
)

# 重複検出用パターン (script + mode で同定、schedule や redirect 部分は無視)
PATTERNS=(
"$REPO_ROOT/scripts/clone_cron.sh metrics"
"$REPO_ROOT/scripts/clone_cron.sh improve"
"$REPO_ROOT/scripts/clone_cron.sh regression"
"$REPO_ROOT/scripts/clone_cron.sh hallucination"
"$REPO_ROOT/scripts/clone_cron.sh monitor-daily"
"$REPO_ROOT/scripts/clone_cron.sh ingest-audit"
"$REPO_ROOT/scripts/clone_cron.sh uptime-monitor"
"$REPO_ROOT/scripts/clone_cron.sh eval-baseline"
"$REPO_ROOT/scripts/clone_cron.sh privacy-review"
"$REPO_ROOT/scripts/clone_cron.sh policy-check"
"$REPO_ROOT/scripts/clone_cron.sh ci-check"
"$REPO_ROOT/scripts/clone_cron.sh golden-eval"
"$REPO_ROOT/scripts/clone_cron.sh response-quality"
"$REPO_ROOT/scripts/clone_cron.sh cost-daily"
"$REPO_ROOT/scripts/clone_cron.sh notify-digest"
"$REPO_ROOT/scripts/clone_cron.sh credit-check"
"$REPO_ROOT/scripts/clone_cron.sh weekly"
"$REPO_ROOT/scripts/clone_cron.sh ai-research"
"$REPO_ROOT/scripts/clone_cron.sh ai-advisor"
"$REPO_ROOT/scripts/clone_cron.sh persona-gap"
"$REPO_ROOT/scripts/clone_cron.sh export-watch"
"$REPO_ROOT/scripts/clone_cron.sh gap-detect"
"$REPO_ROOT/scripts/clone_cron.sh dev-journal"
"$REPO_ROOT/scripts/kpi_dash_cron.sh"
"$REPO_ROOT/scripts/monday_dash_latest_cron.sh"
"$REPO_ROOT/scripts/stores_by_customer_range_cron.sh"
"$REPO_ROOT/scripts/quality_metrics_cron.sh"
"$REPO_ROOT/scripts/style_reflux_cron.sh"
"$REPO_ROOT/scripts/clone_cron.sh synthetic"
"$REPO_ROOT/scripts/analyst/dispatch_cron.sh"
"$REPO_ROOT/scripts/consultant/dispatch_cron.sh"
"$REPO_ROOT/scripts/tenpo/dispatch_cron.sh"
"$REPO_ROOT/scripts/clone_cron.sh external-eval"
# ★2026-06-08 評価 1-3 (上 REQUIRED_CRONS と同順): scrape/health/plaud。pattern = wrapper path。
"$REPO_ROOT/scrape_cron.sh"
"$REPO_ROOT/scripts/health_cron.sh"
"$REPO_ROOT/scripts/plaud_daily_sync.sh"
"$REPO_ROOT/scripts/regulations_sync.sh"
"$REPO_ROOT/scripts/meeting_autojoin_cron.sh"
"$REPO_ROOT/backup_wiki.sh"
"$REPO_ROOT/scripts/backup_offsite.sh"
"$REPO_ROOT/scripts/backup_restore_drill.sh"
"$REPO_ROOT/scripts/codex_review.sh weekly"
"$REPO_ROOT/scripts/codex_review.sh monthly"
# ★2026-06-10 運用堅牢化 (上 REQUIRED_CRONS と同順): housekeeping → stapa wrapper。
"$REPO_ROOT/scripts/housekeeping_cron.sh"
"$REPO_ROOT/scripts/stapa_cron.sh"
"$REPO_ROOT/scripts/clone_cron.sh core-reflux"
"$REPO_ROOT/scripts/clone_cron.sh bridge"
"$REPO_ROOT/scripts/clone_cron.sh personal-snapshot"
"$REPO_ROOT/scripts/receipt_cron.sh"
# ★2026-07-06 workflow レビュー BLOCKER 修正: import-inbox の PATTERN が 2026-07-05 commit で
# 欠落し 49 vs 50 の件数 guard が exit 1 = **全 cron 登録が丸一日停止していた** (import-inbox
# 自体も未登録)。REQUIRED_CRONS と同順で追加 + magazine_backfill も同位置に。
"$REPO_ROOT/scripts/clone_cron.sh import-inbox"
"$REPO_ROOT/scripts/magazine_backfill_cron.sh"
# ★2026-07-02 監査 P2 (上 REQUIRED_CRONS と同順): 手動登録 6 本の REQUIRED 化。
"$REPO_ROOT/scripts/auto_deploy.sh"
"$REPO_ROOT/apple_notes_sync.py"
"$REPO_ROOT/sync_to_claude_project.py"
"$REPO_ROOT/scripts/extractors/weekly_batch.sh"
"$REPO_ROOT/scripts/extractors/alignment_cron.sh"
"scripts/sales_accuracy_check.py"
"$REPO_ROOT/scripts/whitespace_refresh.sh"
)

# 現在の crontab を取得 (空でも OK)
CURRENT="$(crontab -l 2>/dev/null || true)"

# ★2026-06-10 legacy 除去: 旧 bare-python の STAPA entry
#   (例: "30 22 */14 * * python3 .../stapa_scraper.py >> ...") を撤去する。
# これは .env を source せず STAPA_USER/PASS 欠落で無音ログイン失敗していた行。
# 新 wrapper の PATTERN は "scripts/stapa_cron.sh" (= crontab 行に文字列 stapa_scraper.py を
# 含まない) ので、放置すると dedup されず二重実行になる。crontab 行で "stapa_scraper.py" を
# 含むのは legacy 行のみ → その行だけ安全に落とす (wrapper 行は影響を受けない)。
if printf '%s\n' "$CURRENT" | grep -q 'stapa_scraper\.py'; then
    CURRENT="$(printf '%s\n' "$CURRENT" | grep -v 'stapa_scraper\.py')"
    echo "[cron_install] MIGRATE: legacy bare-python STAPA entry を撤去 (wrapper stapa_cron.sh へ移行)"
    CHANGED_MIGRATE=1
fi

# 変更検知用
NEW="$CURRENT"
CHANGED=0
# legacy 除去だけでも crontab を書き戻すため、migrate フラグを CHANGED に反映
[ "${CHANGED_MIGRATE:-0}" = "1" ] && CHANGED=1

# ★2026-06-09 ガード: REQUIRED_CRONS と PATTERNS は index 対応の parallel 配列。
# 数がズレると PATTERNS[$i] が unbound (set -u) で落ち crontab 更新が止まる (実際に起きた)。
# 早期に loud-fail させて原因を明示する。
if [ "${#REQUIRED_CRONS[@]}" != "${#PATTERNS[@]}" ]; then
    echo "[cron_install] ❌ REQUIRED_CRONS(${#REQUIRED_CRONS[@]}) と PATTERNS(${#PATTERNS[@]}) の件数不一致。両配列を同数・同順に揃えること。" >&2
    # ★2026-07-06 workflow レビュー: この経路は「新 cron が永遠に未登録」の silent 死 (実際に
    # 2026-07-05 の import-inbox PATTERN 欠落で丸一日発生) → loud_fail 配線 (cron_env source 済)
    [ "${CRON_INSTALL_QUIET:-0}" = "1" ] || /usr/bin/python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT/scripts'); from clone_improve_lib import loud_fail; loud_fail('cron_install', False, 'REQUIRED_CRONS と PATTERNS の件数不一致 = 全 cron 登録停止中 (cron_install.sh の両配列を同数・同順に)', threshold=2, cooldown_h=6)" 2>&1 || true
    exit 1
fi

MISSING_CRONS=""
for i in "${!REQUIRED_CRONS[@]}"; do
    LINE="${REQUIRED_CRONS[$i]}"
    PAT="${PATTERNS[$i]}"
    # 同パターン (script + mode) が既存 crontab にあるかチェック
    if echo "$CURRENT" | grep -qF "$PAT"; then
        # 既存行と完全一致なら何もしない、完全一致でなければ置換
        if ! echo "$CURRENT" | grep -qF "$LINE"; then
            # 既存パターン行を新 LINE で置換
            NEW="$(echo "$NEW" | grep -vF "$PAT")"
            NEW="$(printf '%s\n%s' "$NEW" "$LINE")"
            echo "[cron_install] UPDATE: $PAT"
            CHANGED=1
        fi
    else
        # 新規追加
        NEW="$(printf '%s\n%s' "$NEW" "$LINE")"
        echo "[cron_install] ADD: $PAT"
        MISSING_CRONS="$MISSING_CRONS $(basename "$PAT" 2>/dev/null || echo x)"
        CHANGED=1
    fi
done

if [ "$CHANGED" = "1" ]; then
    # 先頭の空行を削る
    NEW="$(echo "$NEW" | sed '/./,$!d')"
    if echo "$NEW" | crontab -; then
        echo "[cron_install] $(date): crontab updated successfully"
        [ "${CRON_INSTALL_QUIET:-0}" = "1" ] || /usr/bin/python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT/scripts'); from clone_improve_lib import loud_fail; loud_fail('cron_install', True)" 2>&1 || true
    else
        echo "[cron_install] $(date): ❌ crontab update failed" >&2
        # ★2026-07-02 監査 P1 (cron-install-launchagent-tcc-denied): LaunchAgent の TCC 拒否で
        # 37 連敗しても無音だった → loud-fail 標準 (§1.18)。3 回連続 (LaunchAgent 30分毎 ≒ 90分) +
        # cooldown 24h で LINE/LW 通知。根治は海山の Full Disk Access 付与。
        # CRON_INSTALL_QUIET=1 (auto_deploy の cron 文脈 = 書込失敗が想定内) は streak に数えない
        # (cross-check S3: 誤った TCC 警報の防止)。stderr は cron.log に残す (§1.18、N4)。
        # ★2026-07-11: 7/1〜289連敗の実害 (policy-check/ci-check/golden-eval/tenpo 等 8 cron 未登録) が
        # alert に「何が止まっているか」が無く 10 日放置された → 欠落 cron 名を detail に含める。
        # 応急復旧: ssh mac-studio 'bash ~/brain-agent/scripts/cron_install.sh' (ssh 文脈は TCC 通過を実証済)
        _MISS="$(echo "$MISSING_CRONS" | cut -c1-160)"
        [ "${CRON_INSTALL_QUIET:-0}" = "1" ] || /usr/bin/python3 -c "import sys; sys.path.insert(0,'$REPO_ROOT/scripts'); from clone_improve_lib import loud_fail; loud_fail('cron_install', False, 'crontab write failed (TCC)。未登録cron:$_MISS。応急: ssh から cron_install.sh 実行 / 根治: Full Disk Access 付与')" 2>&1 || true
        exit 1
    fi
else
    # 何もしない (idempotent)、auto_deploy.log を汚さないため verbose flag 時のみ出力
    if [ "${VERBOSE:-}" = "1" ]; then
        echo "[cron_install] $(date): no change (already up to date)"
    fi
fi
