# Personal Brain — Claude Code Instructions

> **Note for readers of the public export**: this file is the working rulebook of the
> private repository, included here because it is the most honest description of how the
> system is actually operated. It references documents that are **not** part of this
> export — `docs/review/ARCHITECTURE.md`, `docs/runbook.md`, `docs/failure-log.md`, and
> `docs/decisions/*` — because they contain incident detail and business specifics.
> Those links will not resolve here. The porting guides under `docs/porting/` are the
> public substitute, and the README summarises the lessons the failure log carries.

> OWNDAYS CEO 海山丈司の Personal Brain (LINE Bot + 自己複製基盤 + 売上 retrieval) 開発時のルール。
> 詳細設計は `docs/review/ARCHITECTURE.md`、運用手順は `docs/runbook.md`、過去事故は `docs/failure-log.md`。
> 本ファイルは Claude が守るルールのみ (200-300 行)。
>
> Last Updated: 2026-07-03 (2026-05-23 に旧 1203 行版から refactor)

## 1. Discipline (必ず守る)

1.1 **Secret は os.getenv() 経由のみ**。`.py` / `.md` / `.yaml` への平文直書き禁止 (gitleaks で block)。`.env` を介して読む。
gitleaks は pre-commit hook で fail-closed 実行 (★2026-07-02 P1f 実効化: それまで config のみで hook 未設置 = inert)。hook 実体 `scripts/git_hooks/pre-commit` (版管理)、設置 `scripts/install_git_hooks.sh` (cron_install.sh が毎 cycle 冪等再設置 = 再 clone でも復活)。binary 不在も commit 停止 (緊急 bypass: `GITLEAKS_BYPASS=1`)。
1.1b **秘密は「値を1文字も書かない」— 部分マスクも禁止** (★2026-08-18 実害)。
先頭と末尾だけ残す伏せ方は安全に見えて **長さ・先頭末尾・生成の癖が残る**。この形で書いた作業メモが
公開 repo に 1 ヶ月出ていた (gitleaks の allowlist が `^docs/.*\.md$` で **docs を丸ごと除外**していたため
無検出。当該 allowlist は撤去済)。秘密に触れる時は **env 変数名だけで指す** (「`OWNDAYS_MOBILE_PASS` を rotate」)。
docs / コメント / commit message / リマインダー文面 / チャットのいずれにも値・断片を書かない。
機械的な防御は 3 層: ① gitleaks の `masked-credential-fragment` / `password-label-with-value`、
② 取り込み最上流の `privacy_gate._CRED_RE` (gate1_credential で BLOCK、理由に値を載せない)、
③ 公開 export の `scripts/public_export.py` (検知したら書き出さず異常終了)。
※②は導入時の実データ走査 (38,310 行) で **既存 raw notes の平文パスワード 4 件**を検出した =
過去分は別途スクラブが要る (取り込み済みのものは遡って消えない)。

1.2 **新規モジュール追加時**: §2 Key Files 表 + `tests/` + (cron なら `scripts/cron_install.sh`) を同時更新。
1.3 **destructive command** (`rm -rf` / `git push --force` / `drop table` / `chroma_data` wipe) は海山の Push 承認後にのみ実行。

ただし **auto-remediation の例外** (★2026-05-25 海山指示「自動で修正するように」): `bot_uptime_monitor.py` が
 `bot_dead` (= /health 接続失敗) / `webhook_silent` (= 30 分 turn 0 件) を検知した時に限り、
**`docker compose restart line-bot`** を自動試行可能 (= rate limit 60 分 max 3 回、`AUTO_REMEDIATE_ENABLED=0` で opt-out)。
他 container (cloudflared / litellm / redis) は触らない、destructive op は依然 海山承認必須。

★追加例外 (★2026-06-15 海山指示、failure-log 2026-06-15 の Docker VM wedge 全断を受けて): `scripts/host_docker_watchdog.sh` が
**Docker デーモン無応答/停止** (= `docker info` が 25s×2 連続 無応答、上記 `docker compose restart` 自体がハングし復旧不可な VM wedge) を
検知した時に限り、**Docker Desktop 自体の再起動** (quit → open → daemon 復活待ち → `compose up -d`) を自動試行可能
(= rate limit **2h max 1 回**、`HOST_WATCHDOG_AUTORESTART=0` で opt-out、auto_deploy ロック中は skip、open 失敗時は手動エスカレーション通知)。
これは「他 container を触らない」原則の明示的な例外 (daemon 復旧には全 container 巻き込み再起動が不可避なため)。
+ 外部 (Cloudflare の先) からの `/health` 死活も同 watchdog が監視。サイト down 時は **local `/health` で真因を切り分け** (★2026-06-30): local=200 = bot 健全 = Cloudflare tunnel 問題 → **`cloudflared` (LaunchAgent `com.umiyama.cloudflared`) を `launchctl kickstart -k`** (20分 max 1 回、`HOST_WATCHDOG_CFD_LABEL` で上書き可)。local も down = bot 問題 → 従来どおり `compose up -d line-bot`。これで「cloudflared が生存だが connection 劣化 → 全断」が自動復旧 (KeepAlive はプロセス死のみカバー)。それ以外の destructive op は依然 海山承認必須。
1.4 **docker-compose 配下の編集後は build + force-recreate** (`docker compose build line-bot && docker compose up -d --force-recreate line-bot`)。`docker compose restart` だけでは古い code のまま (Python source は image に baked-in)。
1.5 **chromadb 並行アクセス禁止** — line-bot 稼働中に `docker exec line-bot python3 reindex_history.py` を回さない (SIGSEGV crash loop 発生)。詳細: `docs/decisions/2026-04-27-chromadb-concurrent-access-ban.md`。
1.6 **main 直接 push、PR 不要、新ブランチ切らない** — Mac mini の `auto_deploy` で 5 分以内に本番反映。
1.7 **スクレイパー出力は LLM compile に流さない** — 決定論的に書く wiki が破壊される。新 scraper を足す時は `main.py:_watch_import_dir` の `DETERMINISTIC_SCRAPER_PREFIXES` を更新。詳細: `docs/decisions/2026-04-27-scraper-output-no-llm-compile.md`。
1.8 **新規 cron スクリプトの crontab 登録前に `env PATH="/usr/bin:/bin"` で dry-run** — cron 最小 PATH で外部バイナリ (docker/ffmpeg/git 等) が解決できるか実証する。`scripts/cron_env.sh` を冒頭で source も併用。
1.9 **個人情報** (人事評価 / 給与 / 考課) を含む Drive folder は `exclude_pattern` 必須 — `gdrive_sync.py` の DEFAULT_EXCLUDE_PATTERN に常時 block。詳細: `docs/integrations/gdrive-sync.md`。

★例外 (★2026-05-26 海山指示): 「個人と紐付かない、公開されてる」 集計給与情報 (= 給与レンジ / 給与体系 / 給与テーブル / 報酬体系 / 店長給与 / SV給与 / AM給与 / リーグ別 / 職位別 / 役職別) は `SALARY_PUBLIC_PATTERN` で override されて通る = うみやま AI / Drive 検索 両方で回答可。判定: 集計 marker hit + 個別 marker (個人別 / 個別 / 社員別 / 氏名 / 名簿 / per_employee 等) 無し → OK。引き続き block: 「給与一覧 個人別.xlsx」「人事評価 2026.xlsx」「健康診断 結果.xlsx」 等の (b)-(f) PII / 評価 / 健康 系 + 給与でも個人特定可能な file。

★追加 block (★2026-05-27 海山指示「相談対応ログは個人情報の可能性が高い」): カテゴリ (k) として 「相談 / 面談 / 個別 communication」 系を追加 (= 相談対応 / 相談記録 / 相談ログ / 相談履歴 / 相談窓口 / ハラスメント相談 / メンタル相談 / キャリア相談 / 個別相談 / 面談記録 / 面談ログ / 1on1 / 通報 / 内部通報 / counseling / consultation log / grievance / harassment report / whistleblow 等)。社員相談 / ハラスメント / メンタル / 個別面談 記録は personal communication として PII 高 risk → 集計値 override 対象外。
1.10 **LINE Bot 管理コマンドは admin user_id 検証必須** (`services/auth.py:is_admin`)。
1.11 **Karpathy 5 原則** を意識: Surgical Edits Only / Keep It Simple / Plan Mode First / Parallelize with Subagents / Slopacalypse 対策。詳細: `docs/development_principles.md` + `docs/decisions/2026-05-22-karpathy-development-principles.md`。
1.12 **既存ファイルに追記前に「これ 50 行に縮められないか」を 1 度問う** (特に `main.py` / `brain_wiki.py`)。
1.12b **★main.py に新規 endpoint / handler を足さない** (★2026-07-10 世界基準評価 S4a、god object 逆進行の停止)。main.py は既に 7,825 行・49 endpoint・fan-out 最大の god object。strangler (#28) は「新規コードを旧構造に足さない」が成立条件 — 計画だけで流入が続く strangler は太り続ける (Fowler 原典の失敗型)。**新規 API endpoint は `routes/` の APIRouter へ、新規ロジックは `services/` or `brain_wiki_helpers/` の pure function へ**。main.py は app assembly (wiring) に留める。既存 endpoint の bug fix は可、endpoint 追加は原則不可 (どうしても必要なら ADR で理由を残す)。`import-linter` を CI に配線し中枢 import 循環 (brain_commands⇄main⇄routes) の再増殖を機械防止。
1.13 **Wiki scrub gotcha — 名前を wikilink display text に入れない** — `[[people/xxx|名前]]` は private link を含む**行全体が削除される**。名前は plain text で、wikilink は別 section に。詳細: `docs/runbook.md` の「Wiki scrub gotcha」節。
1.14 **data/ の git 管理は外科的分離** — Mac mini 自動生成と MacBook 手編集を `.gitignore` で選択同期 (作業消失事故の根治)。詳細: `docs/decisions/2026-05-19-data-surgical-separation.md`。compile 出力 (`identity/style/thinking.md` 等) を手編集して push しない。
1.15 **重要判断では必ず verification subagent を並行 spawn** — 主 agent 単独の「推測 → fixture → 本番 push」サイクルで複数バグ起こした (★2026-05-23 fine-tune 戦略の gpt-5.4 family fact 確認漏れ / response_bank regex 「」必須 / imported_drive 真の path 取り違え / plaud_speaker format 推測 / Contextual Retrieval prefix prompt 注入剥がし忘れ)。3 type subagent:
- **Fact-checker** (= WebSearch で公式 docs / 仕様確認、主 agent が推測してる claim を独立 verify)
- **Reviewer** (= 本物 file / 本物 data を独立調査、主 agent が見たと主張する内容を再確認)
- **Devil's Advocate** (= 主 agent の提案の失敗 scenario 強制立てる、確証バイアス対策)
3 つを同時並列 spawn (= background)、結果集約後に海山へ複眼報告。
**適用 trigger は 4 種のみに厳密化** (★2026-05-23 Strategy reviewer agent 指摘で全件 subagent → cost / 待ち時間トレードオフ顕著化、絞り込み):
- (a) **system prompt 設計** (= CLONE_PROMPT / few-shot 例 追加・差替・削除、bot 応答に直結する prompt の構造変更)
- (b) **retrieval pipeline 変更** (= brain_index / contextual / rerank の挙動変更、retrieval ↔ prompt 連結方式の変更)
- (c) **destructive op** (= rm -rf / chroma_data wipe / git push --force / drop table / 100 件以上の dataset 一括 re-process)
- (d) **CLAUDE.md / docs/decisions/ 改訂** (= 開発ルール / 方針変更、永続的影響)

上記以外 (= 既存 bug fix / typo / 軽微 refactor / コメント追加 / 単発 commit message 修正) は単独可。詳細: `docs/decisions/2026-05-23-cross-check-subagent-pattern.md`。

★2026-07-10 海山指示「開発方針の最終チェックも Fable 5 を通す」: trigger (d) (= CLAUDE.md / docs/decisions 改訂) の**最終チェックは Claude Fable 5 で行う** — session 側は検証 subagent / Workflow agent を `model: fable` で spawn。本番側バックストップ = `scripts/policy_diff_check.py` (07:10 daily) が policy 変更 commit (CLAUDE.md / docs/decisions/ / development_principles / REVIEW_CHECKLIST) を litellm `supervisor` (= Fable 5) で独立最終レビューし、懸念検知時のみ LINE 通知 (session 外の変更 = Mac Studio 直編集も漏れなく網に乗る。変更が無い日は LLM 0 call)。

1.16 **★macbook (= Claude) は着手前に 必ず `git fetch origin main && git log --oneline HEAD..origin/main` で remote 新 commit を確認** (★2026-05-27 海山指示):
海山が Mac Studio で並行作業する場合があるため、無 fetch で着手すると重複実装 / merge conflict が発生する (実例: `2026-05-27` `/talk` vs `/video-align` 重複)。
新 commit 検出時の手順:
- 内容を read してから着手範囲を再判断 (= 既存実装拡張 vs 別 path 並走)
- 着手前に `git pull --rebase origin main` で同期、その後で WIP 開始
- push 前にも 必ず再 fetch (= rebase fail 時は工数大)

1.17 **Personal Brain ドメイン構成** (★2026-06-28 海山指示「OWNDAYS 専用 → 海山個人の Personal Brain。
各PJの記憶は分けて管理、知見は基盤に還流、いつどこでも基盤を使える」)。単一真実源 = `brain_wiki_helpers/domain.py`:
- **Core 層**(project 非依存の基盤=人格・文体・判断軸・趣向): `style/` `judgment/` `hobbies/` + `identity.md`
  `style.md` `thinking.md`。**全ドメイン共有 = OWNDAYS-facing**(公開クローンも従来どおり使う)。`is_core_rel`。
- **`personal/<project>`**(非OWNDAYS の各PJ/投資、例 Example Garden): **OWNDAYS 向け retrieval/出力からは
  全経路で除外**、海山専用 `/personal` モードのみが「Core + その PJ」で参照(`_read_personal_state`)。`is_personal_rel`。
- **`owndays`**(knowledge/ analysis/ decisions/ 等)= 事業の1つ。OWNDAYS 出力に出る。
- 取り込み: gdrive_sync の folder entry に `"domain": "personal"` → `wiki/personal/` へ直接・常に private・
  IMPORT_DIR 非経由(OWNDAYS compile に渡さない)。**還流(PJ→Core の蒸留+承認)は Step 2 で実装済**
  (★2026-06-28、propose-only・`scripts/reflux.py`・02:10 daily、詳細は §自己改善ループ)。

**規律**: ① **wiki を rglob する新 reader / API / export を足す時は必ず深層 private を除外** =
`is_owndays_facing(rel)` (または `is_deep_private_rel`) を呼ぶ。**深層 private = `personal/` +
`interview/`**(★2026-07-03 v3 ADR DA R6: interview/ = 家族/弱さ/金/体の人格深層。frontmatter 一枚
防御 → path 防御に統合、旧規律②の `is_owndays_facing` 統一設計債も同時解消)。新しい非 facing dir は
`domain.py` の `DEEP_PRIVATE_DIRS` に 1 語足せば全出力 chokepoint に波及する。
② **意図的に deep_private 化しない海山経路 3 系統**(誤って「統一」しないこと、source-level test で固定):
chroma 索引(`brain_index.index_wiki_file` / `main._watch_wiki_changes`)と `/mcp/brain` スマホ connector
は personal のみ除外 = interview/ は海山専用 vector recall (P3b) と alignment 雑談継続性のため残す
(公開経路は chroma where + runtime visibility gate + path 強制 private の三重遮断)。海山 admin 消費者
(`/clone`・alignment 質問生成)は `_read_wiki_state(include_interview=True)` opt-in。
★2026-07-04 追加の意図的例外 (4 系統目): 音声アラインメントの話のタネ
(`alignment_interview.collect_wiki_topics`、海山専用 VAPI_SECRET 経路) は personal/ を意図的に含む。
転用防止に呼び出し箇所 1 箇所 pin (`tests/smoke/test_voice_align_first_message.py`)。
詳細: `docs/integrations/vapi-voice-alignment.md`。
★2026-07-11 追加の意図的例外 (5 系統目、海山指示「Brain Map は個人利用だから全部見れる様に」):
**Brain Map の admin tier のみ**(グラフ `/api/brain/graph` + 詳細ペイン `/api/brain/wiki?path=`)は
deep-private (personal/ + interview/) と `clone_visibility: private` を全開。ゲートは唯一 `brain_auth_tier(request) == "admin"`
(= `BRAIN_EXTENSION_KEY` を `compare_digest`。この鍵は `/brain` コマンド=admin gate 済でのみ海山に配信)。
**据え置き (全開しない)**: 弱い token tier (`?token=`)・兄弟 operator endpoint (`/api/brain/wiki/{path}`・
knowledge・dashboard・search)・`/mcp/brain`・社員クローン・公開 LINE は #2 のまま遮断。`build_graph_data`
の `admin` (旧 `include_deep_private` から改名) は**デフォルト False** (admin endpoint のみ True) = 非 admin
のグラフは deep-private path **と** `clone_visibility: private` ノードの両方を build 段階で除外
(★2026-07-11 b60f6b1 の §1.15 adversarial 検証で surfaced した「token tier でも `/api/brain/graph` が
private ノードの title/tags/path を JSON 露出」穴を封鎖 = graph も operator endpoint #2 と可視性一貫化。
判定は sibling endpoint と同じ `== "private"`、fail-safe = frontmatter 無し / clone_visibility 未設定も
private 扱いで build から除外)。source-level + build test で固定。
**残存リスク (受容済)**: `?key=` は URL 埋め込み secret = ブラウザ履歴 / access log / Cloudflare log / LINE 転送で
流出しうる。漏洩時は interview/ + personal/ 全文が web 経由で第三者可読になる → 疑わしい時は `BRAIN_EXTENSION_KEY`
を即 rotate (この個人利用トレードオフを海山が 2026-07-11 に受容)。smoke: `tests/smoke/test_deep_private_domain.py`。
★2026-07-12 追加の意図的例外 (6 系統目、音声 Phase 1): 通話中 PB retrieval
(`services/voice_tools.search_brain_for_voice` = 音声アラインメント通話の brain_search tool) は chroma wiki
collection をフィルタ無しで引く = **interview/ と clone_visibility:private を含む** (personal/ は索引時点で除外)。
ゲート: tool は trusted (VOICE_ALIGN_CALLER_ALLOWLIST) config のみ付与 + webhook tool-calls は X-Vapi-Secret
(phone=VAPI_SECRET / web=VAPI_WEB_SECRET) 必須。**tool 定義に server.secret は埋めない** (2025-08 に Vapi spec から
削除された legacy で届く保証無し + web-config はブラウザに config を返すため secret を埋めると電話用 secret が
漏れる — §1.15 cross-check で実証済の穴。省略すると assistant.server へ公式 fallback = 経路別 secret がそのまま効く)。
漏洩疑い時は VAPI_SECRET / VAPI_WEB_SECRET を rotate。smoke: `tests/smoke/test_voice_tools.py`。
詳細: `docs/decisions/2026-06-28-personal-domain-segregation.md` + `2026-06-28-personal-brain-core-and-registry.md`。
**運用 (admin)**: `/personal`(一覧)/ `/personal <q>`(全PJ+基盤)/ `/personal @<PJ> <q>`(PJ絞り込み)/ `/personal new <PJ>` / `/personal add <PJ> | <メモ>`(随時追加)。PJ名は `safe_project_slug` で `[a-z0-9-]` のみ=path injection 不能 + `relative_to` 二重チェック。**保管** = `scripts/personal_snapshot.py` が wiki/personal/ を**入れ子 git で版管理**(§1.14 gitignore=本体 git 履歴無しの補完、瞬時 rollback/diff、restic が .git ごと offsite 保全。日次 04:25 cron `personal-snapshot`)。還流の `/reflux` (§自己改善ループ) と合わせ、個人ドメインは「取り込み→版管理→還流」が揃う。

1.18 **loud-fail 標準 (★2026-07-02 Fable5 監査)** — **silent 死が実害になる系統 (通知・品質ゲート・デプロイ・
配信・取込) の新設/改修時**は、失敗・縮退を握りつぶさず成否確定点で
`clone_improve_lib.loud_fail(component, ok, detail, threshold=N, cooldown_h=H)` を呼ぶ
(= N回連続失敗 + cooldown で LINE/LW 通知、成功で streak リセット)。「log にだけ書いて終わり」の無音 skip と
「毎日同文 alert」(= alert 疲れ) はどちらも bug として扱う。全 cron への機械的な一括配線はしない (過剰配線は
依存とノイズを増やす)。**shell から呼ぶ時は cron_env.sh source が前提** (env 無しの `python3 -c` は通知不能 =
cross-check DA 実証)。記録は 1 実行 1 箇所 (多重呼びは streak 相殺 bug の元 = reviewer B1)。
背景: 監査で silent 死 5 系統が実害化 (consultant 配信 6.7日 / hallucination 33日 / cron-install 37連敗 /
prompt_diff 6/1〜 / sales_accuracy 通知 drop)。詳細: `docs/decisions/2026-07-02-fable5-system-recheck.md`。

1.19 **PB = コントロールセンター、開発 CLI は交換可能な道具 (★2026-07-12 海山方針)** — ①新機能の
状態・知識・人格データは repo の plain file (markdown/JSONL/yaml) に置く。開発 CLI の独自ストアを
基盤にしない。②CLI/製品固有 format に触るコードは adapter として分離し docstring に「adapter
(交換可能)」明記。③**推論呼び出し経路の**モデル ID hardcode 禁止 (新規・変更コードに適用。litellm
alias or env 注入。観測用対照表・fine-tune base 指定は対象外)。④session memory に恒久知見 (運用手順/
gotcha/設計判断) を書いたら**同 session 内で repo docs へも commit** (memory は cache + 作業状態、
恒久知見の唯一の置き場にしない)。CLI 切替の実コスト checklist・既知違反 2 件は ADR
`docs/decisions/2026-07-12-pb-control-center-portability.md` 参照。

## 2. Key Files & Modules (索引、詳細は `docs/review/ARCHITECTURE.md`)

### コア
- `main.py` — FastAPI webhook server + cron 起動 + file watcher (30s poll)
- `brain_wiki.py` — Karpathy 式ナレッジベース (raw → LLM compile → wiki)、retrieval + clone respond
- `brain_wiki_helpers/` — pure function 集 (visibility / recency_bias / store_keyword / llm_retry / frontmatter / **domain** = personal 非OWNDAYS ドメイン判定 §1.17 / **ontology** = 記憶層の path 決定論導出 / **edge_store** = 型付きエッジ sidecar (frontmatter 不使用、ADR 2026-07-05 §4) ★2026-07-05 / **daily_history_inject** = 日次売上の決定論注入 (国/エリア/業態/リーグ、捏造ゼロ) / **yoy_inject** = 昨年対比の決定論注入 ★2026-07-20 (既存店=Monday Dash 公式・全店=完了月 monthly.json のみ、日次自前YoYは作らない=捏造防止、本文日付で鮮度判定) / **business_intent** = 業務データ質問判定 + 売上follow-up検出 (個人Agent pre-route 用))
- `privacy_gate.py` — 3 段プライバシーフィルタ (ルール → LLM 分類 → PII 除去)
- `brain_commands.py` — LINE Bot コマンド (`/brain`, `/teach`, `/clone`, `/lint`, `/wiki`, `/forward` 等)
- `routes/` — FastAPI APIRouter (`alignment_trial` / `brain_api`)
- `tasks/` — background task (`self_improve`)
- `services/auth.py` — admin user_id 権限ゲート
- `services/agent_core.py` — **Umiyama AI Agent** (★2026-07-20 海山指示で個人 LINE bot を正式名称化 = 本人向け秘書、社員向け「うみやまAI」とは別物。通常テキストは目的メニュー無しで run_agent 直行、/help で全機能一覧、info 通知は 1日2回 digest 集約 = ADR `2026-07-20-umiyama-ai-agent-formalization.md`) の agentic コア (個人エージェント評価 #1): persona digest (identity/thinking/style) 常時注入 + bounded tool-loop (max 3 round / 55s budget = round 境界+各 tool 前判定 / **final round は tools 維持 + tool_choice none** = Anthropic は tool 履歴に tools 定義必須 / round0 失敗は従来単発 completion へ自動 fallback)。書込 tools は owner_memory の file 書込のみ = 外部送信ゼロ、Google scope readonly 不変。ADR `docs/decisions/2026-07-20-run-agent-agentic-upgrade.md`
- `services/owner_memory.py` — 海山自身の恒久 owner-memory (事実/嗜好/進行中 + タスク + リマインダー書込 = clone_reminder_check 互換、bot 生成分は `reminders/auto/` 非追跡)。会話から fire-and-forget 自動抽出 (話者帰属 = 海山発話のみ根拠、揮発情報除外、auto tag + 保存都度 LINE 通知、§1.18 loud_fail)。LINE `/memory` で一望/編集 (admin gate 済)。`OWNER_MEMORY_ENABLED=0` / `OWNER_MEMORY_NOTIFY=0` opt-out。Redis 7日消失の根治
- `content_extractor.py` — Google Docs/Excel/PDF/画像からテキスト抽出 (pypdf + pdfminer.six 両搭載)

### スクレイパー / 取り込み
- `lineworks_scraper.py` — LINE Works Web スクレイパー (Playwright + Chrome)
- `apple_notes_sync.py` — Apple Notes 差分同期 (AppleScript、`--since 14` default で timeout 回避)
- `stapa_scraper.py` — STAPA 社内メルマガ + OWNDAYS MAGAZINE (海山「もぐもぐダイアリー」= 本人の一人称連載コラムを isolate)
- `scripts/magazine_persona_ingest.py` — **もぐもぐダイアリー → 人格取込** (★2026-07-05 海山指示)。magazine 内で唯一の海山本人パート (本人執筆コラム) を号単位で isolate → **専用 prompt (`MAGAZINE_EXTRACT_PROMPT`)** で蒸留 → `interview_extracted/` レビュー待ち。★海山確認「本人執筆・大半本音・一部だけ士気鼓舞の他所行き」→ 文体/ユーモア/内省も取り込むが建前・レトリック反転は割り引き、書き言葉 register は明記。`credit_coverage=False` で音声 coverage 非汚染・`--limit` 洪水防止 (§1.15 cross-check DA 反映)。詳細: script docstring
- `scripts/magazine_backfill.py` + `magazine_backfill_cron.sh` — **過去号 backfill の git 配送実行** (★2026-07-06 海山「2も進めて」、リモート session は stapa 非到達のため)。git 追跡の `data/brain/magazine_backfill_request.json` を毎時 cron (8-22時) が拾い、request 毎に 1 回 `stapa_scraper --all` → `magazine_persona_ingest --limit 15` を **レビュー滞留ゲート付き trickle** (pending magazine 抽出 ≥ 10 で蒸留待機 = /align-voice のレビュー速度に自動追従、一括承認ゲート無力化の DA 対策を自動運転でも維持)。request 無し/完了済みは無音 exit、lock で重複起動防止、§1.18 loud_fail 配線。再実行は requested_at を変えて push
- `mobile_owndays_scraper.py` — OWNDAYS Net Mobile 売上 (6 sections、2h おき本番稼働)
- `mobile_owndays_historical.py` — OWNDAYS 過去 3 年履歴 (API 直叩き、週次フルリフレッシュ + 日次 incremental)
- `gdrive_sync.py` — Google Drive selective sync (Monday Dash / Focus10 / WBR / 営業部)。★2026-07-11 共有ドライブ (driveId 先頭 0A) は `_list_shared_drive_files` で drive 全体 flat 取得 (通常フォルダ query だとサブフォルダ配下が silent 欠落する bug 修正)
- `scripts/regulations_sync.py` + `scripts/regulations_sync.sh` — **社内規程 共有ドライブの月次取込 + 監視** (★2026-07-11 採用レビュー #2)。制度質問 (公休/産休/副業/就業規則) の unmet 66% 解消。既存 gdrive cron は plaud/monday-dash のみ (--all 無し) で規程を回さなかった。月次 (1日 06:00)、`--push` で loud_fail (候補ありなのに 0 取込 = 共有ドライブ DL 権限未付与を LINE 通知、7/6 の 5 日 silent 死の再発防止)。**要 海山/IT: sync アカウント bot-account@example.co.jp を当該共有ドライブの Contributor 以上に昇格** (現在 canDownload=False で 403、権限付与後は次回実行で 54 PDF 自動全取込)
- `scripts/receipt_harvester.py` + `scripts/receipt_cron.sh` — **月額サービス領収書の月次収集 → 共有ドライブ**(★2026-07-02 海山指示、毎月1日 10:00)。**直読み方式**(当初の転送方式は `bot-account@example.co.jp` が外部メール受信不可=550 User unknown で不成立 → 廃止)。領収書が届く個人 Gmail `you@example.com` を harvester が直読み → PDF/画像(スクショ)/リンクPDF/本文 → 経費精算メンバーが見る**共有ドライブ** `RECEIPT_DRIVE_PARENT_ID/YYYY-MM/`(受信月・JST)へ upload。**2トークン**(Gmail読取=umiyama0 `gmail.readonly` / Drive書込=**ceo@owndays.co.jp** `drive`フル=既存folderに書くため必須。★2026-07-03 実運用で当初設計の umiyama-ai から変更=GCPオーナー/共有ドライブ権限の実態に合わせた)。精度: **既知ベンダー or 自己送信のみ取込**(件名一致の第三者はskip)、リンクfetchは許可ドメイン限定(SSRF防止)、冪等はstate増分保存+Drive appProperties重複チェック、失敗は`--push`でLINE loud化。セットアップ済(2トークン発行・`RECEIPT_DRIVE_PARENT_ID` 設定・94ファイル push 実績)。詳細: `docs/integrations/receipt-harvester.md`
- `kpi_dash_scraper.py` — kpi-dash.com Dashboard 週次 scrape (= Playwright login + BFS 巡回、月曜 18:00 JST、★2026-05-25)
- `scripts/meeting_autojoin.py` + `meeting_autojoin_cron.sh` — **umiyama の web 会議に Recall bot 自動参加 → 議事録 → wiki** (★2026-07-03 海山指示、10分毎 7-22時)。カレンダー監視 (umiyama-ai token、共有済) → 参加判定 (録音OFF denylist 日英 = 面接/面談/1on1/評価/退職/昇進/給与/報酬/M&A/弁護士/医療/極秘 等 + **2人会議=実質1on1 skip** + **社外同席 default skip**、`[no-ai]`/`[ai-ok]` marker=社内 organizer のみ有効) → Recall (ap-northeast-1) に join_at 予約 → 終了後 poll で transcript → 既存 `/api/meeting/ingest` (= Plaud と同経路、PrivacyGate → compile_meeting_note → wiki/meetings/) + LINE 通知。冪等 state、gate `MEETING_AUTOJOIN_ENABLED=1`、loud_fail 配線。詳細: `docs/integrations/meeting-pipeline.md`
- `chat_import.py` — LINE / WhatsApp エクスポート .txt パーサー (★2026-07-05 空白区切り LINE variant + 複数行 + ドット日付 + WhatsApp 日本語ロケール 午前/午後 対応)
- `scripts/import_inbox_sweep.py` — **chat export 配送** (★2026-07-05 海山指示「トークを wikiに」)。リモート session からは IMPORT_DIR (git 非管理) に置けないため、git 追跡の `data/brain/import_inbox/` + manifest.json をドメイン宣言付き inbox とし、cron (15分毎 `clone_cron.sh import-inbox`) が routing: owndays → IMPORT_DIR (既存 PrivacyGate+compile)、personal/<pj> → `wiki/personal/<pj>/imports/` へ private transcript 直書き (§1.17 IMPORT_DIR 非経由、LLM 不使用)。冪等 (sha256 state)、loud_fail 配線
- `scripts/claude_personal_sync.py` / `scripts/claude_export_import.py` / `scripts/claude_export_watch.py` — **Example PJ 会話の自動取込**(★2026-06-29、両経路)。`personal/example-garden/` へ private(§1.17)。① scrape(日次 LaunchAgent `com.brain.personal-sync`、`PERSONAL_PROJECT_URL` 設定で **PJページ列挙=PJ内全会話**、未設定は title-mode fallback、セッション切れは LINE 通知=静かに止めない)② export 補完(`claude_export_watch` 日次 cron `export-watch` 06:30、監視フォルダ `~/Downloads`+`data/brain/import_exports/` の Claude.ai export zip → Example+アラインメント両 importer、export は projects.json で PJ単位)。二重取込は `write_personal_abstract` の conv_id dedup で防止。要 海山: claude.ai ログイン1回 + `PERSONAL_PROJECT_URL`。

### うみやまAI (Bot Clone)
- `lineworks_bot.py` — LINE Works Bot API (JWT 認証 + 送信 + 署名検証)
- `clone_history.py` — 1:1 会話履歴 (JSONL / ユーザごと)
- `clone_feedback.py` — 修正希望キュー (「違う」「正しくは」で記録)
- `clone_learning.py` — nightly scan で会話発見抽出
- `clone_memory.py` — 各 user の個別メモリー (Profile / Ongoing / Facts / Preferences、★2026-05-14)

### 自己複製基盤 (4 層モデル、★Step 1-8 完了 2026-04-28)
- `scripts/extractors/` — style / judgment / reflex / embodiment / drift_detector / audit_generator / alignment_snapshot
- `tests/extractors/` — pytest 64 件 (隔離 tmp_path)

### 品質保証 / 改善ループ (★2026-05-21 多層化)
- `scripts/bot_events.py` + `scripts/bot_metrics.py` — 構造化ログ + 集計 CLI
- `scripts/clone_style_regression.py` — 03:30 daily 夜間 regression (cosine + judge + style 違反 regex)
- `scripts/clone_hallucination_check.py` — 03:45 daily post-hoc fact verifier (smart-gpt = 別系列で self-eval loop 回避)
- `scripts/clone_external_eval.py` — 月初 1 日 第三者 blind 採点 (5 軸 5 段階、LLM judge agreement)
- `scripts/clone_memory_privacy_review.py` — 04:05 daily memory の private 行 archive
- `scripts/clone_sleep_time_agent.py` — 会話 idle 30 秒で smart memory 再整理
- `scripts/clone_prompt_diff_check.py` — deploy 時 pre/post 比較
- `scripts/policy_diff_check.py` — 07:10 daily 開発方針 (CLAUDE.md / docs/decisions / development_principles) 変更 commit の **Fable 5 (supervisor) 最終チェック** (★2026-07-10 海山指示。変更無い日は LLM 0 call、懸念のみ LINE。§1.15(d) の本番バックストップ)
- `scripts/clone_alignment_trial.py` — v1 公開前の 135 件集中アラインメント sprint
- `scripts/clone_ab_test.py` — online A/B test (bucket 振分 + analyze)
- `scripts/tracing.py` — OpenTelemetry subspan (soft import)
- `scripts/knowledge_graph.py` — 軽量 in-process knowledge graph (Phase 1)
- `scripts/multimodal_indexer.py` — Multi-modal RAG skeleton (Phase 1)
- `scripts/wiki_access_aggregate.py` — retrieval access 計測の集計 (★2026-06-01 階層メモリ ADR Phase 0、warm slot 是非を事実で判断 + CORE_BUDGET baseline)
- `scripts/connectome.py` + `scripts/connectome_build.py` — 連想想起(spreading activation)+ 可塑性(Hebbian)の **offline 研究 scaffolding** (★2026-06-20、本番 retrieval 非接続。純粋関数 + 既存 recall ログからの共起グラフ構築)。採否は事前登録 kill 基準で判断、ADR `docs/decisions/2026-06-20-connectome-plasticity-memory.md`。**可塑性の本番自己書換は §1.5 chroma 並行書込 scar と衝突するため不採用**
- `scripts/analyst/` — データアナリスト・エージェント (★2026-06-20、Phase 1 完成)。`registry.py`(data カタログ+visibility/access)/ `playbook.py`(世界最高水準の思考=必須検証ループ+捏造禁止+OWNDAYS ドメイン)/ `sandbox.py`(locked-down Docker: network none/read-only/cap-drop ALL/非root/秘密非注入/時間上限)/ `agent.py`(計画→探索→計算→検証→統合ループ、LiteLLM tool-use)。**admin 限定・read-only**。prod 実行は sandbox security review(image build+smoke)、clone 統合(Phase2)は **1.15** の後。ADR `docs/decisions/2026-06-20-data-analyst-agent.md`
- `scripts/tenpo/` — **出店候補レコメンド lane** (★2026-07-11 海山指示「エリアで当たり→AIがマップデータ操作→2タイプ候補」)。`/出店 <エリア>` (LW admin DM 限定) → `recommend.py` が whitespace 実データ (scatter 934自治体/stations 306駅/attack 3,197施設/kNN予測) を**決定論 filter+ランクのみ** (捏造ゼロ=候補は入力の部分集合、ランクは海山合意: S格→館売上→強競合→種別格、ROI 非算出) → タイプA「強い箱」+タイプB「空白地」各2-3件 → LLM は文章整形のみ (`playbook.py` 出力契約、fallback 決定論整形)。queue/dispatch は analyst 再利用 (`dispatch_run.py` 奇数分 cron、★sync main = asyncio.run 二重 loop 回避、loud_fail 配線)。`build_digest_wiki.py` = 県別 digest の public wiki 化 (数値ゼロ、MacBook 手動再生成 = §1.14)。main.py hook は 1 呼び出し (§1.12b)。★2026-07-11 **施設/商圏 個別照会** (海山「特定の商業施設や商業地を尋ねると空白地DBから回答」+「全員に公開」): `build_lookup_index.py` が 4,390 件を `data/brain/tenpo_lookup.json` に bake (**git 配送** = .gitignore whitelist 必須、MacBook 再生成→commit、mtime auto reload)、`lookup_service.py` (container-safe・stdlib のみ) が自然文から施設名検出 (bigram 前段 + 汎用語識別部ゲート = 別施設 latch 防止) → `clone_respond_public` 内で自前注入 + **桁事故ガード** (応答の 億/万 token が DB に無ければ正値行を決定論追記)。社員へ売上予想を伏せる switch = `SHOW_PROJECTION_TO_PUBLIC=0` (default 公開 = 海山判断 2026-07-11)
- `scripts/consultant/` — 戦略アナリスト=**中立声のコンサル**・エージェント (★2026-06-20、A+B 実装)。アナリストの一段上=オーケストレーター。`playbook.py`(McKinsey 級の思考＝MECE/仮説/選択肢×トレードオフ、但し**権威で断定しない・中立構造・[事実]/[仮説]/[要外部確認]分離**=§11.6 維持)/ `brain_search.py`(**chromadb 非接触**の wiki markdown 直読=§1.5 回避、Reviewer 指摘)/ `agent.py`(ツール: `ask_analyst`(既存アナリストへ定量委譲・**≤2回 hard cap**)/`search_brain`/`web_research`(**env-gated**: `WEB_SEARCH_API_KEY`/`TAVILY_API_KEY` 有時のみ有効・**URL 出所必須**・≤3回、Fact-checker 処方=キー無は従来どおり外部『要・外部確認』止まり)/`final_recommendation`)/ `routing.py`(単一判定点 `classify()`=analyst と二重発火しない)/ `deck.py`(戦略メモ→**プレゼン資料 HTML+PPTX** 生成、低彩度・[事実]/[仮説]/[要確認]色分け)/ `dispatch.py`+`dispatch_run.py`(deck 生成込み・別 root/lock/output、奇数分 cron)。配信: `/api/consultant/deck/{rid}`(HTML)+`/pptx`+`/chart`。**admin 限定**。§1.15 cross-check 3種実施。ADR `docs/decisions/2026-06-20-strategy-analyst-agent.md`
- `scripts/codex_review.sh` + `scripts/codex_review_schema.json` — Codex CLI (別系列 OpenAI、`codex exec` read-only) で Claude のコードを独立レビュー。**model 既定 = gpt-5.6-sol** (★2026-07-12 海山指示、host CLI 0.144.1 へ upgrade + MODEL_OK 実証。env CODEX_REVIEW_MODEL で上書き可)。週次 diff (日曜 07:00) + 月次 全体 sweep (1日 07:30)、指摘ありの時だけ LINE。認証は codex login (~/.codex/auth.json、設定済) or CODEX_API_KEY のどちらか、両方無ければ loud-skip (★2026-06-08 海山指示、cross-check の automation gap を埋める。★2026-06-10 廃止 flag 除去 + login 認証対応で実稼働化)

### ヘルスチェック
- `scripts/sales_data_health.py` — 05:30 daily L1-L5 健全性 (scraper / wiki / bot / deploy / **chroma ブロート** ★2026-08-14 = サイズ+メモリ+増加率+ETA、warn 8GB は週次 nag / crit 11GB で日次 🚨、sqlite と HNSW の内訳も測る = 打ち手が分かれるため。ADR `2026-08-14-chroma-bloat-remediation.md`)
- `scripts/sales_accuracy_check.py` — 06:00 daily 売上数字 正確性検証 (verdict: PASS/DECLINED/FAIL/**BOT_UNAVAILABLE**)
- `scripts/health_cron.sh` — wrapper (cron_env.sh source 込み)
- `scripts/cron_env.sh` — cron 3 点セット (PATH + .env + LITELLM_URL)
- `scripts/cron_install.sh` — crontab 自動登録 (auto_deploy が毎 cycle 呼出)
- `scripts/bot_uptime_monitor.py` — 5分毎 bot 稼働監視 (/health + bot_events、bot_dead/webhook_silent で `docker compose restart line-bot` 自動)
- `scripts/host_docker_watchdog.sh` — 5分毎 host watchdog (★2026-06-15)。Docker デーモン無応答 (VM wedge) と外部 /health 死活を監視し、wedge 時は Docker Desktop 再起動へエスカレーション (bot_uptime が拾えない層を補完、§1.3 例外)。**crontab でなく LaunchAgent `com.brain.docker-watchdog` で登録** (cron 文脈は crontab 書込み(TCC)も open -a Docker(GUI)も不可なため)。`deploy/launchagents/` + `scripts/install_launchagents.sh` で設置 (SSH から 1 回 → 以後ログイン時自動ロード=再起動も生存)
- **LaunchAgent `com.brain.cron-install`** (★2026-06-28) — `cron_install.sh` を 30 分毎 + RunAtLoad で実行し新規 cron を**自動登録**。auto_deploy の cron 起動文脈は TCC で crontab 書込不可 (`crontab update failed`) のため、権限のある GUI セッションで走る LaunchAgent が登録を担う。これで新 cron 追加時の SSH 手動 `cron_install.sh` が不要。設置は `install_launchagents.sh` (watchdog と同梱)

### 自己改善ループ
- `scripts/claude_export_alignment.py` — **人格 継続取込** (★2026-06-29)。Claude.ai export の「アラインメント雑談」(project名/title 合致のみ、本文不参照)を **音声と同じ蒸留**(`alignment_interview.record_session`+`extract_session`)へ流す → `interview_extracted/` にレビュー待ち(採用は海山)。音声 alignment(Vapi 電話 → `/webhook/voice-alignment`)は既に自動取込済、本 script はテキスト雑談の gap を埋める。state で再取込防止、alignment_interview は遅延 import(CI 軽量)。
- `scripts/persona_gap_questions.py` — **人格 ギャップ狙い撃ち質問** (★2026-06-29 海山指示「定期的に」)。`alignment_interview.coverage_report` で薄い次元を算出 → LLM で具体的な問いを生成 → 海山へ**隔週 push**(`clone_cron.sh persona-gap`=月曜 08:00 起動 + `--cadence biweekly` で偶数 ISO 週のみ実 push、★海山指示で週次→隔週=ピルアップ回避。monthly に変える時は flag 切替=第1月曜)。答えは音声雑談(同じ薄い次元を突く自動取込)or 返信で吸収 → 薄い所から優先的に深化。捏造なし(問いを出すだけ、wiki 反映は既存ゲート)。LITELLM 断は soft-fail。
- `scripts/bridge_proposer.py` — **孤島接続の提案生成** (★2026-07-05 Phase 1、ADR `2026-07-05-wiki-ontology-multilayer.md`)。graph 星型の主因 = judgment/analysis/decisions の orphan 88-100% に、共起 (実 recall ログ) + embedding 類似で関係候補を算出 → **propose-only** queue (`data/brain/graph/bridge_queue.jsonl`)、採用は海山の LINE `/bridge ok` のみ → sidecar `edges.jsonl` → /graph 描画にだけ効く (wiki/retrieval 不変)。compile の `new_connections` (従来破棄) も同 queue へ配線。日次 02:20 `clone_cron.sh bridge`、`BRIDGE_PROPOSER_ENABLED=0` で opt-out。
- `scripts/reflux.py` — **還流** (★2026-06-28 Step 2、ADR `2026-06-28-reflux-pipeline.md`)。各PJ (owndays / personal/<pj>) の記憶から **project 非依存の判断軸** を蒸留し海山の **Core (基盤)** へ積む。**propose-only**: 02:10 daily に `reflux_queue.jsonl` へ pending 提案、Core 書込は海山の `/reflux ok <id>` 承認時のみ (絶対不変)。承認時に出所引用を検証 (捏造 block)、機密 decisions skip、還先 `judgment/reflux-distilled.md` は private (社員クローン非露出)。LINE `/reflux` (admin)。既存 `style_reflux`(文体) とは別物 (判断軸)。
- `scripts/dev_journal_sync.py` — **開発判断ログ取込** (★2026-07-01 海山指示「Claude Code 上の指示・やり取り・改善判断も履歴/癖として残す」、ADR `2026-07-01-dev-journal-capture.md`)。Claude Code の開発セッション (`~/.claude/projects/-Users-brain-brain-agent*/*.jsonl`) を**増分** (per-session byte offset、初見=前向き) で蒸留し、判断が起きた区間を `wiki/personal/dev/` に private 記録 (日次 06:40 `clone_cron.sh dev-journal`)。**人格非直結**: `reflux.list_domains()` の `REFLUX_EXCLUDE_PERSONAL={"dev",".git"}` で還流除外 = 開発の癖を CEO 人格 judgment へ自動注入しない (人格昇格は段階的な別 gate、未実装)。防御: 話者帰属 (癖は海山発話が根拠、`umiyama_evidence`)、`SENSITIVE_RE`→`confidential:true`、`SECRET_RE` redact、`DEV_SIGNAL_RE` で雑務除外、smart-gpt で self-eval ループ回避。§1.15 3体 cross-check で初版 (人格汚染を実証) を棄却し v2 に再設計。
- `scripts/clone_auto_improve.py` — 03:00 daily 7 種 signal 検知 + 自動編集
- `scripts/prompt_patches_compact.py` — **system_prompt_patches 統合 GC** (★2026-07-05 prompt 監査 #11/#40/#41)。self_improve が append する additions (196 件 ≈13K 字が毎 turn 注入まで肥大) を **propose-only** で canonical 統合 (書き戻しは海山 `--approve` のみ、提案後の夜間 append と競合したら sha 検知で abort)、dead bucket (intent_keywords / drive_search_patterns) は `--gc-dead` で物理削除。**注意: patches.json は CLONE_PUBLIC_PROMPT に一切合成されない** — 唯一の読み手は main.py run_agent (海山個人アシスタント経路、gate 無しの live 経路)。clone 改善は clone_auto_improve 側。再発防止は self_improve 側に実装済 (正規化 dedup + 上限 60 最古drop + 捏造招待 deny-filter)。cron 無し = Mac mini で手動 (03:00-04:00 帯を避ける、§1.14)
- `scripts/synthetic_employee_agent.py` — 04:20 月曜 (週次、★2026-06-30 cost 対策で日次→週次化) 社員に扮した synthetic user が仮想環境(非永続)で bot を使い倒し改善点を検知 → 既定 propose-only で全カテゴリ queue + dedup。唯一の自律 arm = `keyword_miss` の確実な別表記を `drive_search_aliases.json` に **enabled=False(未承認)で記録** (gemini_query は **enabled=True のみ**検索 fan-out に使う = verify-before-activate、海山 `--approve` で有効化。findability 限定・事実不介入。`SYNTHETIC_AGENT_AUTOFIX=1` gate。rerank は bypass 経路があり安全網にならないため承認 gate が防御線) (★2026-06-07 cross-check 3種反映、gated: `SYNTHETIC_AGENT_CRON=1`、ADR `2026-06-07-synthetic-employee-auto-remediation.md`)
- `scripts/clone_usage_metrics.py` — 02:30 daily 集計
- `scripts/clone_weekly_report.py` — 09:00 月曜 週次レポート
- `scripts/ai_research_agent.py` — 09:30 月曜 世界 AI 進化を集約→**PB 自身の改善提案**(proposals.jsonl + 海山 push)
- `scripts/ai_advisor.py` — 09:35 月曜 **OWNDAYS 事業向け AI 活用トレンド & 提言**(ai_research の business 版、★2026-06-20 海山指示)。fetchers 再利用→OWNDAYS 文脈(店舗/検眼/EC/接客/BI)で統合→`build_analysis_wiki` で **public・static-factual wiki**(`analysis/ai-trends-owndays.md`、うみやまAI が AI 関連クエリで retrieval-gated に引用可)+ 海山へ週次 push。**数値捏造禁止ハードニング**(ROI/% は出所URL付き引用のみ、活用案は定性 option、`valid_days=14` 失効)= cross-check DA「捏造が public wiki 経由で CEO 公認に化ける」対策。clone への先回り提言(prompt 編集)は §1.15(a)+§11.6 逆転につき**海山 sign-off 保留**

### 索引 / 再構築
- `scripts/reindex_history.py` — docker 内実行 (line-bot stop 後のみ)
- `scripts/build_breakdown_history.py` — 国/エリア/業態/リーグ 日次履歴
- `scripts/build_store_daily_history.py` — 店舗 × 日次履歴 90 日
- `scripts/build_grouped_monthly.py` — 店舗マスター更新後の遡及再集計
- `scripts/build_monday_dash_latest.py` — Monday Dash 関連 source (LINE Works / kpi-dash / gdrive_monday-dash) を 1 つの core wiki に集約 (★2026-05-25 海山指示、最新鮮度 high)
- `scripts/build_dep_graph.py` — **モジュール依存グラフを AST から決定論的に生成** → `docs/review/dep-graph.md` (★2026-07-03 オントロジー導入)。LLM 不使用=幻覚ゼロ。pre-commit (framework + `scripts/git_hooks/pre-commit` 両方) が .py 変更 commit 時に自動再生成=腐らない。**「どこから呼ばれるか/影響範囲/import 循環」はこの doc を読む** (推測しない)。fan-out 表は god object 分割 (#28) の指標

## 3. Operational Notes (今動かす上で必須の事実)

3.1 **chromadb 並行アクセス禁止** — 詳細は `docs/runbook.md` の「chromadb 復旧」節。
3.2 **Docker image rebuild が必要** — Python source は image に baked-in、`docker compose restart` だけでは古い code。
3.3 **Wiki scrub gotcha** — 個人名を wikilink の display text に入れない (行ごと scrub される)。詳細は `docs/failure-log.md` の 2026-04-28 事案。
3.4 **売上検証で「全 FAIL かつ closest=None」が出たら bot 死亡疑い** — `docker ps | grep line-bot` → 落ちてたら `docker compose up -d line-bot`。詳細は `docs/runbook.md` の「bot 死亡疑い」節 + `docs/failure-log.md` の 2026-05-17 事案。
3.5 **新規 cron は登録前に dry-run** — `env PATH="/usr/bin:/bin" bash scripts/<wrapper>.sh --dry-run` で外部バイナリ解決を実証。詳細は `docs/failure-log.md` の 2026-05-18 / 2026-05-19 事案。
3.6 **scrape_cron.sh は冒頭で `set -a; . ./.env; set +a` で `.env` source** — cron は親 shell env を継承しない、忘れると OWNDAYS スクレイプが silent skip。
3.7 **海外含む日次集計は毎 scrape サイクル再構築** — 1 日 1 回だと時差で stale。`raw → wiki compile` は毎サイクル原則。

## 4. Models (via LiteLLM、詳細は `data/brain/wiki/decisions/llm-switching-policy.md`)

- `smart` — Claude Opus 4.8 (Lint / 会議録 compile / reflux・人格蒸留 / analyst・consultant / 海山個人アシスタント run_agent / memory-privacy 等の判断系。fallback: smart-fallback→smart-gpt)
  - ★**本番の実モデルは .env override で分岐** (★2026-07-12 モデル総点検で公称との乖離を是正): **うみやまAI クローン応答 = `CLONE_PUBLIC_PROD_MODEL=smart-gpt` (GPT-5.4)**、**Wiki compile = `BRAIN_COMPILE_MODEL=fast-gpt` (GPT-5.4-mini)**、抽出/OCR = `EXTRACT_MODEL=smart-sonnet` (Sonnet 5)。「smart=全部 Opus」ではない。実測 (litellm Postgres 7日): Opus 成功課金は smart-tier 経路の低トラフィックで僅少だが機能は正常 (probe OK)、gpt-4o の大量 req は `fast`/`default` (privacy 分類・チャット) が実モデル。
- `supervisor` — Claude Fable 5 (★2026-07-10 海山指示: 監督者層 = auto_improve 日次判断 / weekly_report / ai_research 提案のみ ≈ 月40回・トークン微小。fallback: smart→smart-fallback。**judge/verifier 層は対象外** = `pick_cross_family_judge` の別系列原則を維持 — 同系列 Fable 5 を judge にすると self-eval 防壁が消える。env `SUPERVISOR_MODEL` で即 revert 可)
- `smart-gpt` — GPT-5.4 (うみやまAI クローン本番応答の実モデル / 比較・代替 / self-eval loop 遮断にも)
- `smart-gpt-pro` / `smart-fallback` (gpt-4o) — 文脈で適宜。`smart-legacy` (opus-4-20250514) は **2026-06-15 retire 済 = 呼ぶと 404** (価格マップ参照のみ、推論非使用。要 cleanup)
- `fast` / `default` — GPT-4o (チャット応答 / Privacy 分類 = 全 traffic の最頻)
- `fast-gpt` — GPT-5.4-mini (Wiki compile 本番 / 軽量代替)
- `code` / `code-max` — GPT-5.4-pro / GPT-5-pro (内部コードタスク)。コードレビューは Codex CLI の gpt-5.6-sol (§2)
- `local-qwen` — **Qwen3.8-27B (Mac Studio ローカル、Ollama MLX)** ★2026-08-03 海山指示。
  API コスト 0 / 外部送信 0 / 実測 29-40 tok/s / 19GB GPU 常駐。thinking 既定 OFF (思考が
  max_tokens を食い content 空になるため)。未使用 5 分で自動アンロード = 本番にメモリを返す。
  fallback: fast-gpt→smart-gpt (ローカル障害で bot を止めない)。用途 (privacy 分類・OCR 等)
  への適用は品質 A/B 後。詳細: `docs/integrations/local-llm-qwen.md`

GPT-5 Codex 系は `/v1/responses` 専用で litellm 非対応 → Codex CLI 直接使用 (レビューは gpt-5.6-sol、★2026-07-12)。

## 5. Cron 概要 (詳細は `docs/runbook.md`)

主要 cron だけ抜粋:
- 02:00 nightly scan / 02:30 metrics / 03:00 auto_improve / 03:30 regression / 03:45 hallucination / 04:05 privacy-review / 04:00 月曜 weekly_batch / 04:30 月初 alignment_snapshot
- 05:30 sales_data_health (L1-L4) / 06:00 sales_accuracy_check / 07:10 policy_diff_check (★2026-07-10)
- 10:00・19:00 notify-digest (★2026-07-20 info 系通知の 1日2回まとめ配信、空なら無音。critical は従来即時)
- scrape_cron.sh 2h おき 9-23 時 (OWNDAYS 売上 + 履歴 + 店舗日次)
- 21:00 月曜 gdrive_sync / **18:00 月曜 kpi_dash_scraper (★2026-05-25)** / 08:00 daily Plaud / LINE Works は scrape_cron.sh 内で 2h おき (9-23時) / 23:30 daily Apple Notes
- **03:00 月/火/水 build_monday_dash_latest (★2026-05-25、★2026-06-08 月曜追加=「先週売上」stale bug fix)** — Monday Dash 最新を core wiki に集約 (= 海山-critical、月曜は Section 1 週次合計の鮮度用、火曜失敗時の保険で水曜も run)

## 6. References

- **Architecture**: `docs/review/ARCHITECTURE.md` (システム概観、30 分で全体把握)
- **Review checklist**: `docs/review/REVIEW_CHECKLIST.md` (社内エンジニアレビュー用)
- **Glossary**: `docs/glossary.md` (用語集)
- **Decisions (ADR)**: `docs/decisions/` (重要決定の独立 .md)
  - `2026-04-24-sales-data-pipeline.md` — 売上データ取得パイプライン
  - `2026-04-27-chromadb-concurrent-access-ban.md` — chromadb 並行アクセス禁止
  - `2026-04-27-scraper-output-no-llm-compile.md` — scraper 出力を LLM compile に流さない
  - `2026-04-28-self-replication-foundation.md` — 自己複製基盤 4 層 + 二重ゲート
  - `2026-05-07-clone-public-upgrade-to-opus.md` — Bot Clone を smart Opus 化 + 賢さ強化 6 点
  - `2026-05-14-clone-memory.md` — 個別メモリー機能
  - `2026-05-16-hobbies-ingestion-flow.md` — 嗜好データ取り込み標準フロー
  - `2026-05-18-vapi-switch-from-claude-mcp.md` — 音声アラインメント主経路を Vapi に
  - `2026-05-19-data-surgical-separation.md` — data/ の外科的分離 (根本ルール)
  - `2026-05-19-store-master-flow.md` — 店舗マスター更新フロー
  - `2026-05-21-clone-quality-loops.md` — 多層 self-evaluation loop
  - `2026-05-22-karpathy-development-principles.md` — Karpathy 5 原則導入
  - `2026-07-05-wiki-ontology-multilayer.md` — wiki 多層オントロジー化 4 フェーズ (0 導出層 / 1 孤島接続 / 2 語彙統制 / 3 retrieval 封印) + frontmatter 関係キー持続化の不採用 + 実装地雷登録
  - `2026-07-12-semantic-layer-direction.md` — 社内 AI 向けセマンティックレイヤーの方向づけ (★海山指示、direction-only = 実装未起動。発火条件 3 + 再評価 2026-10。新規 AI プロダクトは hub-pattern チェックリスト 7 で「指標の問い」を必ず通す)
  - `2026-07-12-pb-control-center-portability.md` — PB = コントロールセンター / 開発 CLI 非依存の原則 (★海山方針の再確認。substrate=plain file・adapter 分離・CLI 切替 checklist 5 項・§1.19 に圧縮規律)
  - `2026-07-20-run-agent-agentic-upgrade.md` — run_agent の agentic 化 (tool-loop + owner-memory + persona 注入、個人エージェント評価 #1 の実装)
  - `2026-07-20-umiyama-ai-agent-formalization.md` — 個人 LINE bot = Umiyama AI Agent 正式化 (目的メニュー廃止 + /help + info 通知の 1日2回 digest 集約。★海山「無用な通知等は極力なくす」)
  - `2026-08-14-chroma-bloat-remediation.md` — chroma HNSW ブロート再発 (5.2GB / ~285MB日) の監視・計画 rebuild・恒久策 (churn 削減 vs pgvector)。★上流は設計上 unbounded (chroma#2594 が 2 年 open) で回収手段が無いことを一次情報で確認。距離関数が既定 l2 なのにコメントは cosine = 要実測 (未解決)
- **Failure log**: `docs/failure-log.md` (★YYYY-MM-DD 学びを時系列集約、再発防止参照用)
- **Runbook**: `docs/runbook.md` (cron / restart / 売上検証 / docker rebuild / chromadb 復旧 等の運用手順)
- **Integrations**: `docs/integrations/`
  - `gdrive-sync.md` — Google Drive 取り込み
  - `vapi-voice-alignment.md` — 音声アラインメント (Vapi)
  - `meeting-pipeline.md` — 会議音声 / 議事録 (Recall + Plaud)
  - `system-hub-pattern.md` — 母屋(ハブ)連携: 新システムを PB に繋ぐ標準契約 (①同居=route / ②知識化=build_analysis_wiki / ③公開=MCP)。雛形 `whitespace_analysis_tw/build_wiki_tw.py` (★2026-06-20)
- **Development Principles**: `docs/development_principles.md` (Karpathy 由来、コード作業時の判断軸)
