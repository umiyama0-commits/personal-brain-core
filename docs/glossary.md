# 用語集 (Glossary)

> ★ CLAUDE.md (旧 1203 行版) から 2026-05-23 切り出し。
> Personal Brain プロジェクトに頻出する用語を 1 行ずつ。

## Personal Brain コア概念

- **Personal Brain** — OWNDAYS CEO 海山丈司の AI 分身 / ナレッジベース統合システム
- **BrainWiki** — Karpathy 式ナレッジベース。raw 蓄積 → LLM compile → wiki 生成
- **PrivacyGate** — 3 段プライバシーフィルタ (ルール → LLM 分類 → PII 除去)、5 カテゴリ排除 (家族会話 / 性的内容 / 悪口 / 医療詳細 / パートナー会話)
- **clone_visibility** — wiki ファイルの公開可否を public / private で示す frontmatter (うみやまAI が retrieval で踏むかの最終フィルタ)
- **exit_visibility** — public / internal / private (将来の出口ゲート用メタ枠、現在は記録のみ、retrieval ロジックは clone_visibility ベース)

## うみやまAI / Clone 関連

- **うみやまAI / 分身 / clone** — LINE Works Bot 経由で全社員 (~200 人) と 1:1 DM できる AI 分身
- **clone_respond_public** — `brain_wiki.py` の応答生成関数 (clone_visibility=public の wiki のみ参照)
- **clone_memory** — 各 user の会話に対する増分メモリー (`clone_memory/<user_id>.md`)、Profile / Ongoing / Facts / Preferences の 4 セクション
- **clone_feedback** — 社員からの修正希望キュー (「違う」「正しくは」等で記録)
- **clone_learning** — nightly scan で会話発見を抽出 (fact / correction / decision / style / response_quality 等のカテゴリ)
- **M1 / M2 / M3** — 応答モード: M1 情報モード (即答) / M2 相談モード (デフォルト、考える余白) / M3 判断モード (明確に決めて欲しい時)

## 自己複製基盤 (Self-Replication Foundation) 4 層

- **集約層** — identity.md / style.md / thinking.md (腐りにくい本質サマリ、AI core retrieval に常時投入)
- **個別層** — wiki/style/ / wiki/judgment/ / wiki/reflex/ / wiki/embodiment/ (観察可能な単位パターン、1 ファイル = 1 パターン)
- **楔層** — meta/alignment_state.md (月次「本人像スナップショット」、月初 1 日 04:30 自動追記)
- **監視層** — meta/drift_log.md + audit/pending_questions.md (差分時系列 + 確認すべき問い)
- **style** — 言語パターン (alignment / conversations / notes から抽出)
- **judgment** — 判断パターン (decisions / alignment / conversations から抽出)
- **reflex** — 反射的反応 (conversations から抽出)
- **embodiment** — 身体性メタデータ (manifest JSON → wiki/embodiment/、★バイナリ禁止、external_path のみ)
- **bi-temporal** — 判断パターンが「腐る」だけでなく「置き換わる」事も認識。`superseded_by` / `valid_until` frontmatter で retired マーク

## OWNDAYS 組織用語

- **VMV** — OWNDAYS の Vision / Mission / Values (Values は 10 個。VMV 2026 が現行版)
- **MIS** — (社内用語: 文脈で適宜)
- **AM** — Area Manager (エリアマネージャ、現行 6 名)
- **SV** — Supervisor (現行 27 名、SV 選挙で選出)
- **J1〜J5** — リーグ別店舗評価ランク
- **DK1〜DK5** — (社内用語: 文脈で適宜)
- **本部 / 営業部 / 営業本部** — OWNDAYS 内部組織区分

## データ取得経路

- **mobile.owndays.net** — OWNDAYS Net Mobile (社内売上ダッシュボード、scraper 経由で取得)
- **Recall.ai** — Meet/Zoom/Teams 横断の bot ベース transcript 取得サービス
- **Plaud Note** — カード型ボイスレコーダ (iPhone 背面装着、対面会議の transcript 用)
- **Vapi** — 電話ベース音声 AI プラットフォーム (Deepgram nova-2 STT + OpenAI TTS、車内日本語音声アラインメント用、番号 `+1 572 726 9551`)
- **STAPA** — OWNDAYS 社内メルマガサービス
- **LINE Works** — OWNDAYS 全社で使う業務チャット (うみやまAI Bot の宿主)

## モデル役割

- **smart** — Claude Opus 4.8 (Wiki compile / Lint / うみやまAI 応答)
- **smart-gpt** — GPT-5.4 (比較・代替、smart の self-evaluation loop 遮断にも)
- **smart-gpt-pro** — GPT-5.4-pro (高 effort 推論)
- **smart-fallback** — GPT-4o (Anthropic 障害時 自動切替)
- **smart-legacy** — Claude Opus 4 (2025-05) 比較用
- **fast / default** — GPT-4o (チャット応答、Privacy 分類)
- **fast-gpt** — GPT-5.4-mini (軽量代替)
- **code / code-max** — GPT-5.4-pro / GPT-5-pro (内部コードタスク)

## Cron / Operations 用語

- **scrape_cron.sh** — 2 時間おき (9-23 時) で OWNDAYS 売上スクレイプを実行する wrapper
- **cron_env.sh** — cron 3 点セット (PATH + .env source + LITELLM_URL 書換)、新 cron は冒頭で必ず source
- **auto_deploy** — Mac Studio (旧 Mac mini、~2026-05-25 移行) で git pull + docker rebuild + force-recreate を 5 分おきに行う仕組み
- **L1/L2/L3/L4 health check** — sales_data_health.py の検査層 (scraper / wiki / bot / deploy)
- **DETERMINISTIC_SCRAPER_PREFIXES** — main.py 内のリスト、スクレイパー出力を LLM compile に流さないための skip prefix 集合
- **BOT_UNAVAILABLE** — sales_accuracy_check の verdict (= bot 死亡、kind: container_not_running / docker_daemon_down / import_error / timeout / empty_reply)
