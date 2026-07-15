#!/bin/bash
# clone_cron.sh — うみやまAI 自動改善・利用トラッキング cron wrapper
#
# crontab -e で以下 4 行を追加 (Mac Studio で、cron_install.sh が自動登録):
#   30 2 * * * /Users/brain/brain-agent/scripts/clone_cron.sh metrics    >> .../cron.log 2>&1
#   0  3 * * * /Users/brain/brain-agent/scripts/clone_cron.sh improve    >> .../cron.log 2>&1
#   30 3 * * * /Users/brain/brain-agent/scripts/clone_cron.sh regression >> .../cron.log 2>&1
#   0  9 * * 1 /Users/brain/brain-agent/scripts/clone_cron.sh weekly     >> .../cron.log 2>&1
#
# モード:
#   metrics    - 日次利用 metrics (02:30 JST)
#   improve    - 日次自動改善 (03:00 JST、metrics の後)
#   regression - 応答スタイル夜間 regression test (03:30 JST、improve の後)
#   weekly     - 週次レポート (月曜 09:00 JST)
#   external-eval - 月初(1日 09:00) 第三者 blind 採点 form 生成 + 配布通知 (judge κ の human data 起動)
#
# 手動実行:
#   bash scripts/clone_cron.sh metrics
#   bash scripts/clone_cron.sh improve
#   bash scripts/clone_cron.sh weekly

set -uo pipefail

# 共通環境セットアップ (PATH / .env / LITELLM_URL)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/cron_env.sh"

cd "$(cd "$SCRIPT_DIR/.." && pwd)" || exit 1

MODE="${1:-help}"
TS="$(date +'%Y-%m-%d %H:%M:%S')"

case "$MODE" in
    metrics)
        echo "[$TS] clone_cron metrics start"
        python3 scripts/clone_usage_metrics.py
        echo "[$TS] clone_cron metrics done"
        ;;
    policy-check)
        # ★2026-07-10 海山指示「開発方針の最終チェックも Fable5」: CLAUDE.md / docs/decisions /
        # development_principles 変更 commit を supervisor (Fable 5) が独立最終レビュー。
        # 変更が無い日は LLM 呼び出しゼロ、懸念検知時のみ LINE (personal・非critical)
        echo "[$TS] clone_cron policy-check start"
        python3 scripts/policy_diff_check.py
        echo "[$TS] clone_cron policy-check done"
        ;;
    ci-check)
        # ★2026-07-10 世界基準評価 #3: GitHub Actions (main) の赤を毎日 poll し、赤に転じた時だけ通知。
        # 7/3 から CI 恒常赤が無通知で 40+ commit 無テスト反映されていた「signal 層の死」を塞ぐ。
        echo "[$TS] clone_cron ci-check start"
        python3 scripts/ci_status_check.py
        echo "[$TS] clone_cron ci-check done"
        ;;
    golden-eval)
        # ★2026-07-10 世界基準評価 S4b: retrieval golden eval を実運用 (2026-06-08 以来 0 run だった穴)。
        # BM25-only = chromadb 非依存 = bot 停止不要で週次。full dense+rerank pipeline は §1.5 で bot
        # 停止窓が要る (watchdog/auto_deploy と競合、reindex_history と同じ) ため手動運用 (docs 参照)。
        echo "[$TS] clone_cron golden-eval start"
        python3 scripts/golden_eval_log.py
        echo "[$TS] clone_cron golden-eval done"
        ;;
    core-reflux)
        # ★2026-06-28 Step 2 還流: 各PJ→Core 判断軸蒸留 (propose-only)。style_reflux とは別物 (判断軸 vs 文体)。
        echo "[$TS] clone_cron core-reflux start"
        python3 scripts/reflux.py
        echo "[$TS] clone_cron core-reflux done"
        ;;
    bridge)
        # ★2026-07-05 Phase 1 孤島接続: 共起+embedding から graph エッジ候補を propose-only で queue。
        # 採用は海山の /bridge のみ。BRIDGE_PROPOSER_ENABLED=0 で opt-out (default on = 提案のみで無害)。
        echo "[$TS] clone_cron bridge start"
        if [ "${BRIDGE_PROPOSER_ENABLED:-1}" = "1" ]; then
            python3 scripts/bridge_proposer.py --push --max "${BRIDGE_MAX_PROPOSALS:-30}"
        else
            echo "[$TS] bridge_proposer disabled (BRIDGE_PROPOSER_ENABLED != 1)"
        fi
        echo "[$TS] clone_cron bridge done"
        ;;
    dev-journal)
        # ★2026-07-01 Claude Code 開発セッション → personal/dev (増分・人格非直結)。dev_journal_sync 参照。
        echo "[$TS] clone_cron dev-journal start"
        python3 scripts/dev_journal_sync.py
        echo "[$TS] clone_cron dev-journal done"
        ;;
    personal-snapshot)
        # ★2026-06-28 personal 保管: wiki/personal/ の版管理 snapshot (入れ子 git、restic が offsite 保全)。
        echo "[$TS] clone_cron personal-snapshot start"
        python3 scripts/personal_snapshot.py
        echo "[$TS] clone_cron personal-snapshot done"
        ;;
    improve)
        echo "[$TS] clone_cron improve start"
        python3 scripts/clone_auto_improve.py
        echo "[$TS] clone_cron improve done"
        ;;
    regression)
        echo "[$TS] clone_cron regression start"
        python3 scripts/clone_style_regression.py
        rc=$?
        echo "[$TS] clone_cron regression done (rc=$rc)"
        exit $rc
        ;;
    privacy-review)
        echo "[$TS] clone_cron privacy-review start"
        python3 scripts/clone_memory_privacy_review.py
        echo "[$TS] clone_cron privacy-review done"
        ;;
    hallucination)
        echo "[$TS] clone_cron hallucination start"
        python3 scripts/clone_hallucination_check.py
        rc=$?
        echo "[$TS] clone_cron hallucination done (rc=$rc)"
        # rc=1 (contradicted あり) でも exit 0 で続行 (LINE Push が通知済)
        ;;
    eval-baseline)
        # ★2026-05-24 Plan C v2 Step 6 Tier 2 E: eval_set_v1 baseline daily 計測
        # Strategy reviewer 指摘「Plan C v2 で何 % 行ったか の唯一の答え」絶対基準。
        # bot 応答 (= smart Opus 4.7) vs eval ideal の cosine + LLM judge (= smart-gpt 系列分離)。
        echo "[$TS] clone_cron eval-baseline start"
        python3 scripts/eval_runner.py --version v1
        rc=$?
        echo "[$TS] clone_cron eval-baseline done (rc=$rc)"
        ;;
    monitor-daily)
        # ★2026-05-24 Plan C v2 Step 6 Tier 2 D: bot 応答 monitor daily 集計 + alert
        # category 分布 / length 分布 / fallback 率 / few-shot leak / context_prefix_leak
        echo "[$TS] clone_cron monitor-daily start"
        python3 scripts/bot_monitor_daily.py --since 24h --alert --json > "data/brain/alignment/monitor_daily_$(date +%Y%m%d).json"
        rc=$?
        echo "[$TS] clone_cron monitor-daily done (rc=$rc)"
        ;;
    uptime-monitor)
        # ★2026-05-24 海山指示「bot 作動してない、常時確認するエージェント」: 5 分おき bot 稼働 check
        # 検知: bot 死亡 / webhook 受信停止 / turn_failed 急増 / context_prefix_leak
        # 異常時 LINE Push (= 30 分 cooldown で flood 防止)
        echo "[$TS] clone_cron uptime-monitor start"
        python3 scripts/bot_uptime_monitor.py
        rc=$?
        echo "[$TS] clone_cron uptime-monitor done (rc=$rc)"
        # rc=0 healthy / rc=1 warning / rc=2 critical
        ;;
    response-quality)
        # ★2026-05-23: 応答品質の deploy 即時 feedback (打ち手 B)
        # ミラーリング失敗 / AI 臭さ / 過剰長文 を別系列 LLM で 3 軸採点、degraded ≥ 3 で LINE Push
        # ★2026-05-29 cost: cron は */30 なのに window が 1h で、各 turn が連続 2 run に入り
        #   二重採点 = judge LLM (smart-gpt) call が ~2x 無駄だった (dedup 無し)。window を cadence
        #   (30 分) にほぼ合わせ、cron jitter 用の ~6 分 overlap のみ残す (--hours 0.6) → judge cost
        #   ~40% 減。採点品質・検知 latency は不変 (各 turn は依然 1 回採点、30 分以内に検知)。
        echo "[$TS] clone_cron response-quality start"
        python3 scripts/clone_response_quality_judge.py --hours 0.6
        rc=$?
        echo "[$TS] clone_cron response-quality done (rc=$rc)"
        # rc=1 (degraded あり) でも exit 0 で続行 (LINE Push が通知済)
        ;;
    cost-daily)
        # ★2026-05-23 LEE §4.2: LiteLLM 日次 cost 集計 + LINE Push
        # litellm max_budget=50 USD/日 と並走する observability layer
        echo "[$TS] clone_cron cost-daily start"
        # 同時に 1 回限り reminder の配信 check も走らせる
        # (data/brain/reminders/<today>.md があれば LINE Push、9 時 daily で 1 度だけ)
        python3 scripts/clone_reminder_check.py
        python3 scripts/clone_cost_summary.py
        rc=$?
        echo "[$TS] clone_cron cost-daily done (rc=$rc)"
        ;;
    credit-check)
        # ★2026-05-23 海山指示: 外部 service 残高切れ silent fail 防止
        # Vapi / LiteLLM の残高 / 使用率を監視、閾値超で LINE Push
        echo "[$TS] clone_cron credit-check start"
        python3 scripts/external_credit_watchdog.py
        rc=$?
        echo "[$TS] clone_cron credit-check done (rc=$rc)"
        # rc=1 (= 警告あり) でも exit 0 で続行
        ;;
    weekly)
        echo "[$TS] clone_cron weekly start"
        python3 scripts/clone_weekly_report.py
        echo "[$TS] clone_cron weekly done"
        ;;
    external-eval)
        # ★2026-06-08 評価#4: 月初に第三者 blind 採点 form を生成し、海山に配布を促す。
        # judge κ (LLM-human agreement) の human data を起動する唯一の入口 (κ が永久 no_data だった)。
        # 採点回収後: --import-file で取り込み → --agreement で κ 算出 (κ<0.6 で judge 再較正 alert)。
        echo "[$TS] clone_cron external-eval start"
        FORM_PATH=$(python3 scripts/clone_external_eval.py --generate 2>&1 | grep '^form:' | sed 's/^form: //')
        echo "[$TS] external-eval form=$FORM_PATH"
        FORM_PATH="$FORM_PATH" python3 - <<'PY' 2>/dev/null || echo "[$TS] external-eval notify skipped"
import os, sys
sys.path.insert(0, "scripts")  # clone_improve_lib は scripts/ 配下
try:
    from clone_improve_lib import line_push
    fp = os.environ.get("FORM_PATH") or "(生成失敗 — 手動確認)"
    line_push(
        "📋 今月の第三者 blind 採点 form を生成しました (judge の信頼性を人間基準で検算)。\n\n"
        f"form: {fp}\n\n"
        "手順: 5 名程度に配布 → 各自 5 軸 5 段階で blind 採点 → json を download → 海山に集約\n"
        "→ python3 scripts/clone_external_eval.py --import-file <json>\n"
        "→ python3 scripts/clone_external_eval.py --agreement  (LLM judge vs 人間の κ を算出)\n"
        "κ<0.6 なら judge が系統的にズレている = 再較正。これで self-eval loop の盲点が塞がる。"
    )
except Exception as e:
    print(f"notify failed: {e}")
PY
        echo "[$TS] clone_cron external-eval done"
        ;;
    ai-research)
        echo "[$TS] clone_cron ai-research start"
        python3 scripts/ai_research_agent.py
        echo "[$TS] clone_cron ai-research done"
        ;;
    ai-advisor)
        # ★2026-06-20 OWNDAYS 事業向け AI 活用トレンド & 提言(ai_research の business 版)。
        # public wiki に書き(うみやまAI が引用可)+ 海山へ週次 push。数値捏造禁止ハードニング済。
        echo "[$TS] clone_cron ai-advisor start"
        python3 scripts/ai_advisor.py
        echo "[$TS] clone_cron ai-advisor done"
        ;;
    synthetic)
        # ★2026-06-07 海山指示: 社員に扮した synthetic user が仮想環境で bot を使い倒し、
        # 悪い応答を検知し改善提案を queue (propose-only、cross-check で自律直接編集は不採用)。
        # ADR: docs/decisions/2026-06-07-synthetic-employee-auto-remediation.md
        # ★安全 gate (§1.8 手動 dry-run 後に有効化): SYNTHETIC_AGENT_CRON=1 が .env に
        #   無ければ何もせず exit (= push しても自動起動せず、海山が Mac Studio で
        #   `python3 scripts/synthetic_employee_agent.py --dry-run` 検証後に flag を立てる)。
        if [ "${SYNTHETIC_AGENT_CRON:-}" != "1" ]; then
            echo "[$TS] clone_cron synthetic skipped (SYNTHETIC_AGENT_CRON!=1, 手動有効化待ち)"
            exit 0
        fi
        echo "[$TS] clone_cron synthetic start"
        python3 scripts/synthetic_employee_agent.py
        rc=$?
        echo "[$TS] clone_cron synthetic done (rc=$rc)"
        ;;
    persona-gap)
        # ★2026-06-29 海山指示「ギャップ狙い撃ち質問を定期的に」: 人格 wiki の薄い次元を
        # alignment_interview の coverage から算出 → 質問を LLM 生成 → 海山へ push。
        # 答えは音声雑談(同じ薄い次元を突く自動取込)or 返信で吸収 → 薄い所から優先的に深まる。
        # cron は毎週月曜だが --cadence biweekly で隔週に間引き(★海山指示 週次→隔週、ピルアップ回避)。
        echo "[$TS] clone_cron persona-gap start"
        python3 scripts/persona_gap_questions.py --cadence biweekly
        rc=$?
        echo "[$TS] clone_cron persona-gap done (rc=$rc)"
        ;;
    gap-detect)
        # ★2026-07-01 海山指示: クローンが「データ不足」で答えられなかった data/財務 質問を clone_history から
        # 検知し、未処理の新規分を海山へ push(A=データ提供要 / B=既存データで retrieval 改善で埋める backlog)。
        echo "[$TS] clone_cron gap-detect start"
        python3 scripts/clone_gap_detector.py --push
        rc=$?
        echo "[$TS] clone_cron gap-detect done (rc=$rc)"
        ;;
    export-watch)
        # ★2026-06-29 海山指示「Example PJ 全会話を自動取込(両方)」: 監視フォルダの Claude.ai export zip を
        # ① Example→personal ② アラインメント→人格 の両 importer へ流す backstop。日次スクレイプの取りこぼし補完。
        echo "[$TS] clone_cron export-watch start"
        python3 scripts/claude_export_watch.py
        rc=$?
        echo "[$TS] clone_cron export-watch done (rc=$rc)"
        ;;
    import-inbox)
        # ★2026-07-05 海山指示「(LINE/WhatsApp トークを) wikiに」: git 追跡 inbox
        # (data/brain/import_inbox/) の chat export を manifest のドメイン宣言に従い配送。
        # owndays → IMPORT_DIR (既存 PrivacyGate+compile)、personal/<pj> → wiki/personal 直書き (§1.17)。
        echo "[$TS] clone_cron import-inbox start"
        python3 scripts/import_inbox_sweep.py
        rc=$?
        echo "[$TS] clone_cron import-inbox done (rc=$rc)"
        ;;
    help|*)
        echo "Usage: $0 {metrics|improve|regression|privacy-review|hallucination|response-quality|cost-daily|weekly|ai-research|synthetic|persona-gap|export-watch|import-inbox}"
        echo ""
        echo "  metrics          - daily usage tracking (recommended: 02:30 JST)"
        echo "  improve          - daily auto-improve agent (recommended: 03:00 JST)"
        echo "  regression       - response style regression test (recommended: 03:30 JST)"
        echo "  hallucination    - daily post-hoc fact verifier (recommended: 03:45 JST)"
        echo "  response-quality - response quality 3-axis judge (recommended: every 30 min)"
        echo "  cost-daily       - LiteLLM 日次 cost summary + LINE Push (recommended: 09:00 JST)"
        echo "  credit-check     - 外部 service 残高切れ silent fail 防止 (recommended: 09:00 + 21:00 JST)"
        echo "  privacy-review   - clone_memory privacy re-evaluation (recommended: 04:00 JST)"
        echo "  weekly           - weekly report (recommended: Mon 09:00 JST)"
        echo "  ai-research      - AI 進化キャッチアップ + 反映提案 (recommended: Mon 09:30 JST)"
        echo "  synthetic        - 社員に扮した synthetic QA + self-heal (04:20 JST, gated: SYNTHETIC_AGENT_CRON=1)"
        exit 1
        ;;
esac
