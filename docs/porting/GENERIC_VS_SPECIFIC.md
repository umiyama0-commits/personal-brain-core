# Personal Brain 移植ガイド — 汎用 vs 差し替え マップ

> **対象読者**: この Personal Brain システムを **別の経営者 / 別の会社** 向けに移植する開発者。
> **目的**: 「何をそのまま使い回すか」「何を自分の人・会社のものに差し替えるか」を 1 枚で判断できる地図。
> これが「うみやまAI / OWNDAYS のもの」を「あなたのもの」に変える鍵。
>
> Last Updated: 2026-06-16 / 作成: 移植 onboarding 用
> 参照元: `CLAUDE.md` §2、`docs/review/ARCHITECTURE.md` §3-4、各モジュール実体

---

## 0. 3 行サマリ

1. **エンジン層は丸ごと汎用** — RAG (chromadb)、LiteLLM ルーティング、PrivacyGate、cron 基盤、clone respond、品質ループ、FastAPI/webhook、content_extractor は人物・会社に非依存。`os.getenv()` と wiki の中身を入れ替えるだけで動く。
2. **「人格」と「業務データ源」は全差し替え** — `wiki/identity|style|thinking`、`hobbies/*`、CLONE_PUBLIC_PROMPT、業務スクレイパー (OWNDAYS Net Mobile / LINE Works / kpi-dash / STAPA)、店舗マスター・組織図・売上パイプライン・「日本」default scope は **すべて 海山/OWNDAYS 固有**。あなたの人・会社の等価物に置換する。
3. **枠は使うが中身を直す = 要調整** — model alias (smart/fast)、few-shot 例、alignment 質問集、response-bank。構造は再利用、内容は本人のものに作り直す。

---

## 1. 全体の考え方 (3 層モデル)

```
┌─────────────────────────────────────────────────────────┐
│ [汎用] エンジン層   — 知識を compile・検索・応答・監視する仕組み │
│   brain_index / litellm / privacy_gate / cron / quality loop │
│   ↑ そのまま流用。触るのは env と閾値だけ                      │
├─────────────────────────────────────────────────────────┤
│ [要調整] 設定層     — 枠は同じ、中身は本人で作り直す            │
│   model alias / few-shot / alignment 質問 / response-bank     │
├─────────────────────────────────────────────────────────┤
│ [差替必須] 内容層   — 「誰の」「どの会社の」を決める全データ      │
│   persona wiki / hobbies / 業務 scraper / 組織図 / scope rule  │
│   ↓ ここを入れ替えないと「あなた」にならない (＝海山のまま)      │
└─────────────────────────────────────────────────────────┘
```

エンジンは「空の器」。**器に何を注ぐか (= persona wiki と業務データ) が移植作業の本体**。
最大の落とし穴は、`brain_wiki.py` の **プロンプト定数 (CLONE_PUBLIC_PROMPT 等) に 海山/OWNDAYS が直書きされている** こと。ここは「設定」ではなく「コード」なので、env だけ替えても消えない。§5 の grep で全部洗い出すこと。

---

## 2. 分類テーブル — コア / エンジン

| コンポーネント / ファイル | 区分 | 役割 | 移植時にやること |
|---|---|---|---|
| `main.py` (FastAPI skeleton, lifespan, `/webhook`, `/webhook/lineworks`, `/health`, `/ready`) | **汎用** | webhook server + cron 起動 + file watcher (30s poll) + 各 endpoint | skeleton はそのまま。ただし `/webhook/recall` `/webhook/voice-alignment` `/video-align` 等 OWNDAYS の連携 endpoint と、`prompt = "あなたはOWNDAYS CEO 海山丈司の…"` (L1247) 等の **埋め込みプロンプトは差替**。intent 分類プロンプト (L869〜 "CEOのメッセージを…") の data source 列挙も自社源に。 |
| `brain_index.py` (ChromaDB ベクトル索引) | **汎用** | wiki/raw → chunk → 埋め込み → top-k 検索 (RAG エンジン) | 無改修で動く。`text-embedding-3-small` を使う設定だけ確認。**これが移植の最大の資産**。 |
| `brain_wiki.py` の **retrieval ロジック** (`_read_wiki_state_public_compact` / `CORE_WIKI_REGISTRY` / `CATEGORY_BOOST_BY_INTENT` / `_extract_historical_sections`) | **汎用 (枠) + 要調整 (登録内容)** | core wiki を intent×category boost で動的サイズ決定 + 履歴セクション抽出 | アルゴリズムは流用。ただし **`CORE_WIKI_REGISTRY` に登録する core wiki 群 (現在 ~29、真実源 = brain_wiki.py) のファイル名**と、`CATEGORY_BOOST_BY_INTENT` の intent カテゴリ (sales/hobbies/consultation/business…) は自社の wiki 構成に合わせて再登録。`_extract_historical_sections` の店舗名・エリア名リスト (L2699〜 「日本/シンガポール/関東A…」) は **差替必須** (§5)。 |
| `brain_wiki.py` の **compile 系** (`compile_*`, `COMPILE_SCHEMA`) | **汎用 (枠) + 要調整 (タグ体系)** | raw → wiki に LLM で変換 | コンパイル機構は流用。`COMPILE_SCHEMA` (L86〜) の **タグ体系** (`OWNDAYS` / `Example_Garden` / `エンジェル投資` 等 L109〜) と `MEETING_NOTE_PROMPT` (L254、「OWNDAYS CEO 海山丈司の会議議事録」「海山の発話特徴…」) は本人用に書き直す。 |
| `brain_wiki.py` の **CLONE_PROMPT / CLONE_PUBLIC_PROMPT** (L374 / L388〜) | **差替必須** | clone 応答の system prompt (人格・口調・scope・出典ルール) | **ここが人格の心臓部**。「あなたは OWNDAYS 社長・海山丈司の AI 分身『うみやまAI』」「海山らしく」「CEO の声で」、scope=日本前提、売上出典 (`knowledge/owndays-history-monthly.md`) 等すべて自社・本人に置換。clone respond の**エンジン (retrieval→LLM→text の流れ) は汎用**だが、**注入するプロンプト文字列は全差替**。 |
| `privacy_gate.py` (3 段フィルタ: ルール→LLM分類→PII除去) | **汎用** | 取り込み前のプライバシーフィルタ。`Verdict` / `FilterResult` / `gate1_rules` / PII regex | フレームワークはそのまま。`DEFAULT_CONFIG` の `blocked_contacts` / `blocked_channels` / `blocked_keywords` (家族の LINE ID 等) は **本人の私的連絡先に差替**。PII regex (電話/メール/カード/マイナンバー) は日本向けなら流用可。 |
| `services/auth.py` (`is_admin` / `is_lw_admin`、fail-closed) | **汎用 (ロジック) + 差替 (ID)** | admin user_id 検証ゲート | コードは流用。`ALIGNMENT_TARGET_USER` (海山の LINE id) と `ADMIN_LW_USER_ID` (海山の LW id) は **env で自分の id に差替**。fail-closed 設計は維持。 |
| `content_extractor.py` (PDF/Docs/Sheets/Excel/画像→text) | **汎用** | 添付・Drive ファイルからテキスト抽出 (pypdf + pdfminer + openpyxl + Vision OCR) | 無改修。SSRF 対策 (`ipaddress`/`socket` で private IP 弾き) も込みで安全。 |
| `litellm_config.yaml` (model_list / fallbacks / cost cap) | **要調整** | LLM 統一 endpoint + 多段 fallback + 日次予算上限 | 構造は流用。`smart`/`fast` 等の alias を割り当てるモデルは好みで変更可。`max_budget` (50 USD/日) は規模に合わせ調整。API key は env 経由のまま。詳細は §6。 |
| `lineworks_bot.py` (LW Bot API: JWT 認証 + send + 署名検証) | **差替 or 撤去** | うみやまAI の配信 channel (LINE Works) | **OWNDAYS の社内 chat = LINE Works 前提**。別社が Slack/Teams/LINE 公式なら **このモジュール自体を該当 SDK に置換**。`LW_BOT_ID` / `LW_CLIENT_ID` / `LW_SERVICE_ACCOUNT` / `LW_PRIVATE_KEY` 等は全部その会社のワークスペース資格情報。 |
| `clone_history.py` / `clone_feedback.py` / `clone_learning.py` / `clone_memory.py` | **汎用** | 1:1 履歴 / 修正キュー / nightly 発見抽出 / ユーザ個別メモリ | 機構は人物非依存。`clone_learning.py` の抽出プロンプト (「海山社長のナレッジマネージャ」L131、「海山丈司の AI 分身 応答品質審査官」L606) の **人名だけ差替**。 |
| `brain_commands.py` (`/brain` `/teach` `/clone` `/lint` `/wiki` `/forward` 等) | **汎用** | LINE Bot 管理コマンド | コマンド体系は流用。is_admin ゲート維持。文言中の人名・社名があれば差替。 |
| `routes/` (`alignment_trial` / `brain_api`) | **汎用** | FastAPI APIRouter | 無改修。 |
| `tasks/self_improve.py` | **汎用 (枠) + 要調整 (プロンプト)** | background 自己改善 | eval プロンプト (L76「CEOとAIアシスタントの会話ログ」) の役職表現を調整。 |

---

## 3. 分類テーブル — スクレイパー / 取り込み

> 大原則: **「取り込み機構 (file watcher / PrivacyGate / compile)」は汎用、「どこから取ってくるか = 各 scraper」は会社固有**。
> あなたの会社の業務データ源 (売上 BI、社内 wiki、メール、チャット…) に対して **新しい scraper を書く**のが移植の主作業。出力先は同じ `data/brain/import/` に流せば、以降のパイプラインは無改修で動く。

| コンポーネント / ファイル | 区分 | 役割 | 移植時にやること |
|---|---|---|---|
| File Watcher (`main.py:_watch_import_dir`, 30s poll) + `DETERMINISTIC_SCRAPER_PREFIXES` | **汎用** | `import/` 投入物を検知 → PrivacyGate → compile | 機構は流用。新 scraper の出力 prefix を `DETERMINISTIC_SCRAPER_PREFIXES` に登録 (CLAUDE.md 1.7)。 |
| `mobile_owndays_scraper.py` (OWNDAYS Net Mobile 売上、2h おき) | **差替必須** | 店舗売上・昨対・客数を社内ポータルから取得 | **あなたの業務データ源 scraper に丸ごと置換**。`OWNDAYS_MOBILE_USER/PASS/START_URL` は不要に。これが「会社の数字」の入口なので、自社の売上/KPI 源に対し新規作成。Playwright login + cookie 永続化の**雛形としては優秀**。 |
| `mobile_owndays_historical.py` (過去 3 年履歴、API 直叩き) | **差替必須** | 売上履歴の週次フル + 日次 incremental | 自社履歴源へ。後述の売上パイプライン (§4) とセットで再設計。 |
| `lineworks_scraper.py` (LINE Works Web スクレイプ、Playwright) | **差替 or 撤去** | 社内 chat の会話取り込み | 社内 chat が違うなら置換。`LINEWORKS_USER/PASS` は該当ツールの資格情報に。 |
| `kpi_dash_scraper.py` (kpi-dash.com 週次、月曜 18:00) | **差替必須** | 外部 KPI Dashboard を BFS 巡回取得 | kpi-dash.com は OWNDAYS の BI。自社 BI へ置換。`KPIDASH_*` env 差替。BFS scraper の雛形として流用可。 |
| `stapa_scraper.py` (STAPA 社内メルマガ、`stapa.owndays.net`) | **差替 or 撤去** | 「海山社長のもぐもぐダイアリー」等オウンドメディア取得 | 完全に OWNDAYS 固有。自社オウンドメディアが無ければ撤去。`STAPA_*` env も削除。 |
| `gdrive_sync.py` (Drive selective sync + PII exclude) | **汎用 (機構) + 差替 (フォルダ/パターン)** | 指定フォルダの選択同期 + 人事/給与の fail-safe 除外 | 同期機構・除外フレームは流用。`data/brain/.gdrive_sources.json` の **folder_id (Monday Dash / WBR / 営業部) は自社フォルダに差替**。`DEFAULT_EXCLUDE_PATTERN` / `SALARY_PUBLIC_PATTERN` (L435/L496) は自社のプライバシー方針 (CLAUDE.md 1.9 の override ルール) に合わせて見直し。 |
| `apple_notes_sync.py` (Apple Notes 差分同期、AppleScript) | **汎用** | 本人のメモ取り込み | 機構は本人非依存。本人が Apple Notes 使うなら無改修、使わなければ撤去。 |
| `chat_import.py` (LINE export .txt パーサー) | **汎用** | LINE トーク履歴の取り込み | 本人非依存。`sender = "海山丈司" if … else …` 系 (chatgpt_scraper / claude_scraper 同様) の **本人名ラベルだけ差替**。 |
| `vapi` 音声アラインメント / `heygen` 動画アバター (main.py + env 群) | **要調整 / 差替** | 電話雑談蒸留 + 動画アバター応答 | 機構は汎用だが `VAPI_VOICE_*` (ElevenLabs voice clone パラメータ) / `HEYGEN_AVATAR_ID/VOICE_ID` は **本人の声・顔に差替**。本人の音声/動画 clone を作り直す。 |

---

## 4. 分類テーブル — Wiki レイヤー (data/brain/wiki/)

> ここが **「誰の brain か」を決める実体**。エンジンは器、wiki は中身。
> **重要**: `data/` の大半は `.gitignore` 管理 (CLAUDE.md 1.14、Mac mini 自動生成と手編集を外科的分離) のため、MacBook チェックアウトには `style.md` 等一部しか無い。移植時は **空の wiki ツリーから本人のデータで再生成**するのが正道 (既存ファイルを編集するのではなく)。

| Wiki レイヤー | 区分 | 役割 | 移植時にやること |
|---|---|---|---|
| `identity.md` / `style.md` / `thinking.md` (persona core) | **差替必須** | 本人の価値観・文体・判断パターン (clone の人格 core、常時 retrieval 投入) | **全消し → 本人のデータで再 compile**。海山の「効率より品質」「面・線・点」「知らんけど」等は全部本人のものに。raw (本人の会話/発言) を集めて `compile_*` で生成し直す。 |
| `hobbies/{manga,books,movies,music}/*` + `hobbies/index.md` | **差替必須** | 本人の文化的嗜好 (Nujabes / 漫画 / 映画…)。雑談・人生相談で引用 | 完全に海山個人。本人の嗜好で作り直す。`importance:1-5` の引用深度フレームは流用可。 |
| `style/*` (40+ ファイル: `style-aizuchi` / `style-naruhodo-overuse` / `response-bank.md` / `few-shot-examples-v1.json` 等) | **差替必須 (内容) / 汎用 (枠組み)** | 観察された文体パターンの個別記事 + 参考回答 + few-shot | ファイル構造 (個別 style 記事 + response-bank + few-shot json) は **優れた枠なので流用**。中身 (海山の口癖・相槌・語尾・30 問回答例) は本人のものに総入れ替え。詳細は §6 「要調整」。 |
| `knowledge/owndays-*` (`owndays-store-master` / `owndays-organization` / `owndays-vmv` / `owndays-japan-strategy` / `owndays-fy26-results` / `owndays-tsa` / `owndays-monday-dash-latest` / `owndays-sales-metrics-stance` 等) | **差替必須** | OWNDAYS の経営知識・組織・店舗・業績・文化 | **全部 OWNDAYS 固有データ**。自社の組織図・拠点マスター・MVV・業績・戦略に置換。`owndays-store-master.md` の「304 店 / 6 AM / 27 SV / 38 都道府県 / 担当店舗表」は自社の拠点・組織構造へ。 |
| `knowledge/history/*` (`org-snapshots/` 等) + 売上履歴 wiki (`owndays-history-monthly` 等、prompt から参照) | **差替必須** | 国別/エリア別/業態別/リーグ別の日次・月次売上履歴、組織スナップショット | 自社の履歴データ構造へ。`### 日本` 等の sub-section 構造 (scope rule と連結、§5) を自社の地理/事業区分に作り直す。`build_breakdown_history.py` / `build_store_daily_history.py` / `build_grouped_monthly.py` の集計軸も自社次元に。 |
| `decisions/*` (経営判断ログ: 閉店判断 / 給与交渉 / 法務 / 人事…) | **差替必須** | 本人の過去の意思決定ログ (judgment 学習源) | 海山個人の判断履歴。本人の decision で置換 (機微情報含むので慎重に)。`llm-switching-policy.md` だけは汎用 (モデル方針)。 |
| `judgment/*` / `analysis/*` | **差替必須** | 判断軸ダイジェスト・分析 | 本人のものに。 |
| `clone-disclosure-policy.md` | **要調整** | clone が何を開示してよいかの方針 | 枠は流用、開示範囲は本人の意向で再設定。 |
| 自己複製 4 層 (`scripts/extractors/` が生成する `style/`, `judgment/`, `reflex/`, `embodiment/` の個別記事 + `meta/alignment_state.md` + `meta/drift_log.md` + `audit/pending_questions.md`) | **汎用 (抽出器) + 差替 (生成物)** | 「本人像が腐らない」ための bi-temporal 4 層構造 | **抽出器 (`scripts/extractors/style|judgment|reflex|embodiment|drift_detector|alignment_snapshot`) は完全汎用** = そのまま流用し、本人の raw に対して回せば本人版の 4 層が生える。生成済みの記事 (海山版) は破棄。`superseded_by`/`valid_until` の bi-temporal 機構も汎用。 |

---

## 5.「ハードコードされた前提」の探し方 (grep ターゲット)

> 移植で最も漏れやすいのが、**env でなくコード/プロンプト/wiki に直書きされた 海山/OWNDAYS 固有値**。
> 以下を順に潰す。`data/` 配下は gitignore 込みなので `git grep` でなく通常 `grep -r` を使うこと。

### 5.1 人名・社名リテラル (プロンプトに直書き = env で消えない)

```bash
# 海山 / OWNDAYS / うみやまAI の直書きを全部洗う (.py 全体で 130+ 箇所 in brain_wiki.py)
grep -rn "海山\|OWNDAYS\|owndays\|うみやまAI" --include="*.py" .
grep -rn "海山丈司\|社長\|CEO" --include="*.py" . | grep -v test
```
**主な hit と対応**:
- `brain_wiki.py` L388 `CLONE_PUBLIC_PROMPT` = "あなたは OWNDAYS 社長・海山丈司の AI 分身「うみやまAI」"
- `brain_wiki.py` L254 `MEETING_NOTE_PROMPT` = "OWNDAYS CEO 海山丈司の会議議事録" / L289 "海山の発話特徴…"
- `main.py` L1247 "あなたはOWNDAYS CEO 海山丈司のパーソナルAIアシスタント"、L878〜 intent 分類プロンプト
- `clone_learning.py` L131 / L606、`claude_dispatcher.py` L68/L117、`alignment_interview.py` L508、`self_improve.py` L76
- `sync_to_claude_project.py` L117/L121
- scraper 内の `sender = "海山丈司" if … else …` (`chat_import` / `chatgpt_scraper` / `claude_scraper`)
→ **すべて本人名・社名に置換** (定数化して 1 箇所参照にするのが理想)。

### 5.2 「日本」default scope (brain_wiki.py)

```bash
grep -n "日本\|シンガポール\|関東A\|西日本\|九州" brain_wiki.py
```
- L435〜 `CLONE_PUBLIC_PROMPT` の「scope 暗黙前提: 日本語 query では default 日本」ブロック全体 (L436-455)
- L2699〜 `_extract_historical_sections` の国名リスト (`"日本","シンガポール","タイ","台湾","香港","フィリピン"`) と エリアリスト (`"関東A","関東B","西日本A"…`)
- 出典指定 `knowledge/owndays-history-monthly.md` の `### 日本` sub-section 参照
→ **自社の事業地理 / 区分に作り直す** (例: 国展開が無いなら scope rule ごと撤去、事業部別なら事業部名に)。scope を返答冒頭に明示する設計思想自体は流用価値あり。

### 5.3 admin / 認証 ID (env、`.env` で差替)

```bash
grep -rn "ALIGNMENT_TARGET_USER\|ADMIN_LW_USER_ID\|DEPLOY_ADMIN_TOKEN" --include="*.py" .
```
- `services/auth.py` L24 `ALIGNMENT_TARGET_USER` (海山の LINE user_id)、L27 `ADMIN_LW_USER_ID` (海山の LW user_id)
→ `.env` で自分の id に。**コード改修不要、env のみ**。

### 5.4 LINE Works ワークスペース ID / 配信 channel 資格情報 (env)

```bash
grep -rn "os.getenv" --include="*.py" . | grep -E "LW_|LINEWORKS_"
```
- `lineworks_bot.py`: `LW_BOT_ID` / `LW_CLIENT_ID` / `LW_CLIENT_SECRET` / `LW_SERVICE_ACCOUNT` / `LW_PRIVATE_KEY(_PATH)` / `LW_BOT_SECRET` / `LW_BOT_USER_ID` / `LW_BOT_MENTION_NAMES`
- `lineworks_scraper.py`: `LINEWORKS_USER` / `LINEWORKS_PASS`
→ 別 chat 基盤なら **`lineworks_bot.py` ごと置換** (§2)。LINE Works 継続なら env のみ。

### 5.5 業務データ源の資格情報・URL・ファイルパス (env + 設定ファイル)

```bash
grep -rn "os.getenv" --include="*.py" . | grep -E "OWNDAYS_MOBILE|KPIDASH|STAPA"
grep -rn "owndays.net\|kpi-dash.com\|stapa" --include="*.py" .
```
- `OWNDAYS_MOBILE_USER/PASS/START_URL`、`KPIDASH_USER/PASS/LOGIN_URL`、`STAPA_USER/PASS`
- URL リテラル: `stapa.owndays.net` (stapa_scraper)、`https://kpi-dash.com/login`、`mobile.owndays.net`
- 音声/動画: `VAPI_VOICE_*` (ElevenLabs の海山 voice clone)、`HEYGEN_AVATAR_ID/VOICE_ID` (海山アバター)
- `main.py` L4061 `https://brain.example.com/webhook/voice-alignment` (本番ドメイン)
→ **scraper 差替に伴い env・URL を自社源に**。Drive フォルダは `data/brain/.gdrive_sources.json` の `folder_id`。

### 5.6 店舗マスター・組織図・売上次元 (wiki + 集計スクリプト)

```bash
grep -rn "AM\|SV\|エリア\|リーグ\|店舗マスター\|担当店舗" data/brain/wiki/knowledge/
grep -rln "owndays" data/brain/wiki/
```
- `knowledge/owndays-store-master.md` (304 店 / AM 6 / SV 27 / 9 エリア)、`owndays-organization`、`history/org-snapshots/`
- 集計軸: `scripts/build_breakdown_history.py` (国/エリア/業態/リーグ)、`build_store_daily_history.py`、`build_grouped_monthly.py`、`build_monday_dash_latest.py`
→ 自社の **拠点マスター / 組織階層 / 集計次元** に作り直す。AM/SV/エリア/リーグ/業態という OWNDAYS 固有の組織語彙を自社語彙に。

### 5.7 タグ体系 (compile schema)

```bash
grep -n "OWNDAYS\|Example_Garden\|エンジェル投資" brain_wiki.py
```
- `COMPILE_SCHEMA` (L109〜) の仕事サブタグ `OWNDAYS` / `Example_Garden` (海山の飲食事業) / `エンジェル投資`
→ 本人の事業ポートフォリオに合わせたタグに。

### 5.8 PrivacyGate の私的ブロックリスト (設定ファイル)

- `data/brain/privacy/filter_config.json` の `blocked_contacts` / `blocked_channels` (家族・パートナーの LINE ID / グループ)
→ **本人の私的連絡先に差替** (空のまま運用すると私生活が漏れる)。

---

## 6.「要調整」項目の詳細 (枠は使う・中身は作り直す)

### 6.1 model alias (`litellm_config.yaml` + CLAUDE.md §4)
`smart`=Opus 4.8 / `smart-gpt`=GPT-5.4 / `fast`=GPT-4o … の **役割割当の思想** (応答 LLM と検証 LLM を別系列にして self-eval loop を断つ等) は流用。実際にどのモデルを当てるかは予算・好みで調整。fallback chain と `max_budget` も規模に合わせる。

### 6.2 few-shot 例 (`data/brain/wiki/style/few-shot-examples-v1.json`)
**仕組み = 「RAG だけだと base LLM が標準語に回帰するので、本人テイストの数例を system prompt に直接焼く」** は普遍的に有効。`category_balance` (挨拶/雑談/経営判断/キャリア/業務/反論) という設計も流用可。ただし 20 例の中身 (「いつもお疲れさま」「グミが好き」「面・線・点」「知らんけど」…) は **全部本人の実発話に差替**。差替は本人の response-bank から cherry-pick する運用 (CLAUDE.md 1.15 の subagent 経由) を踏襲。

### 6.3 alignment 質問集 (`scripts/generate_alignment_q67.py` / `clone_alignment_trial.py` / `build_alignment_form.py` ほか)
**「本人に質問をぶつけて回答を収集 → wiki に反映」というアラインメント sprint の仕組み**は汎用。質問セット自体 (経営観・人生観を引き出す設問) は流用ベースにしつつ、本人の事業・関心に合わせて調整。`data/brain/alignment/` の収集物は本人のものに。

### 6.4 response-bank (`data/brain/wiki/style/response-bank.md`)
**「本人記入の想定 30 問参考回答 → bot は逐語コピーせずトーン/語尾/軸/思考の運びを抽出」というルール設計**は流用。回答内容は本人記入で総入れ替え。`response_bank` の regex で「」が必須等の gotcha (CLAUDE.md 1.15) は枠ごと継承。

---

## 7. 品質保証 / 自己改善ループ (ほぼ全部 汎用)

`scripts/` 配下の品質・改善・監視ループは **人物非依存のフレームワーク** = そのまま流用できる最大の資産。中の eval プロンプトに人名があれば差替する程度。

| モジュール | 区分 | 備考 |
|---|---|---|
| `clone_style_regression.py` (cosine + judge + violation regex) | **汎用** | 採点軸の violation regex は本人の style 違反パターンに調整 |
| `clone_hallucination_check.py` (post-hoc fact verifier、別系列 LLM) | **汎用** | verifier を応答側と別系列にする設計込みで流用 |
| `clone_external_eval.py` (月次 第三者 blind 採点) | **汎用** | 5 軸 5 段階のルーブリックは流用、観点は調整可 |
| `clone_auto_improve.py` (7 signal 検知 + 自動編集) | **汎用** | signal 検知ロジックは普遍 |
| `synthetic_employee_agent.py` (社員役 synthetic user が bot を使い倒す) | **要調整** | 「社員」ペルソナは自社のユーザ像に。承認 gate (`SYNTHETIC_AGENT_AUTOFIX`) 思想は維持 |
| `clone_memory_privacy_review.py` (memory の private 行 archive) | **汎用** | プライバシー観点は本人方針に |
| `clone_sleep_time_agent.py` (idle 30s で memory 再整理) | **汎用** | 無改修 |
| `bot_events.py` / `bot_metrics.py` / `tracing.py` (構造化ログ + OTel) | **汎用** | 無改修 |
| `codex_review.sh` (別系列 GPT で Claude コードを独立レビュー) | **汎用** | 無改修 |
| `knowledge_graph.py` / `multimodal_indexer.py` (Phase1 skeleton) | **汎用** | 無改修 |

---

## 8. cron / 運用基盤 (汎用、登録内容のみ調整)

| コンポーネント | 区分 | 移植時にやること |
|---|---|---|
| `scripts/cron_env.sh` (PATH + .env + LITELLM_URL の 3 点セット) | **汎用** | 無改修 (パス前提だけ確認) |
| `scripts/cron_install.sh` (crontab 自動登録、auto_deploy が毎 cycle 呼出) | **汎用 (枠) + 調整 (job 一覧)** | 登録する cron job のリストを自社 scraper のスケジュールに差替 |
| `scripts/health_cron.sh` / `scrape_cron.sh` 等 wrapper | **汎用** | `set -a; . ./.env; set +a` の env source 慣習 (CLAUDE.md 3.6) は維持 |
| `sales_data_health.py` / `sales_accuracy_check.py` (L1-L4 健全性 + 売上正確性 15 query) | **要調整** | 健全性チェックの**枠は流用**、検証する「正解値 15 query」(武蔵小山店の売上 等) は自社データの既知値に差替 |
| Docker / `docker-compose` (build + force-recreate 慣習、CLAUDE.md 1.4) | **汎用** | 無改修。auto_deploy / FileVault 復帰等の運用は環境依存 |

---

## 9. 売上データパイプライン (差替必須の代表例、ADR あり)

`docs/decisions/2026-04-24-sales-data-pipeline.md` に設計が残る OWNDAYS 売上パイプラインは、移植の**最大の差替対象**:

```
[差替] mobile_owndays_scraper (Net Mobile) ┐
[差替] mobile_owndays_historical (API)     ┼→ import/ →[汎用] watcher → PrivacyGate
[差替] kpi_dash_scraper (BI)               ┘        → compile →[差替] knowledge/owndays-history-*
                                                    →[汎用] brain_index (Chroma)
                                                    →[差替/枠流用] CLONE_PUBLIC_PROMPT の出典ルール + scope
                                                    →[要調整] sales_accuracy_check の 15 query
```

→ **取り込み〜検索〜応答のパイプライン (中央) は汎用、両端 (データ源 scraper と出典/scope/検証の固有値) が差替**。あなたの会社の「数字の源」と「数字の次元 (地理/事業/期間)」をここに流し込めば、うみやまAI が自社の数字で答えるようになる。

---

## 10. 移植チェックリスト (順序)

1. **env を全部自分のものに** (§5.3-5.5 の getenv 一覧 = `.env` 再作成)。`services/auth.py` の admin id から。
2. **配信 channel を決める** — LINE Works 継続か、別 chat か。後者なら `lineworks_bot.py` を置換。
3. **プロンプト定数の人名・社名を全置換** (§5.1) — `CLONE_PUBLIC_PROMPT` / `MEETING_NOTE_PROMPT` / `main.py` 各 prompt。
4. **scope rule を作り直す** (§5.2) — 「日本」default と国名/エリアリスト。事業構造に合わせて。
5. **業務 scraper を自社源で新規作成** (§3) — `mobile_owndays_scraper` を雛形に。出力は `import/` へ。`DETERMINISTIC_SCRAPER_PREFIXES` 登録。
6. **wiki を空から再生成** (§4) — persona core (identity/style/thinking)、hobbies、knowledge/組織・売上履歴。raw を集めて compile。自己複製抽出器 (`scripts/extractors/`) を本人 raw に対し回す。
7. **要調整 4 点を本人化** (§6) — model alias / few-shot / alignment 質問 / response-bank。
8. **検証の正解値を差替** (§8) — `sales_accuracy_check` の 15 query を自社既知値に。
9. **PrivacyGate の私的ブロックリスト記入** (§5.8) — 家族・パートナー連絡先。
10. **品質ループ・cron・Docker はそのまま起動** (§7-8) — eval プロンプトの人名だけ確認。

---

## 付録: ひと目で「触る必要があるか」

- **触らない (汎用そのまま)**: `brain_index.py` / `content_extractor.py` / `privacy_gate.py` の枠 / cron 基盤 / 品質ループ群 / 自己複製抽出器 / FastAPI skeleton / clone history・memory・feedback の機構 / OpenTelemetry。
- **env だけ触る**: `services/auth.py` / LINE Works 資格情報 / 各 scraper の URL・creds / Drive folder_id。
- **コード/プロンプトを直書き編集**: `brain_wiki.py` の各 PROMPT 定数 + scope/履歴リスト / `main.py` の埋め込みプロンプト / `clone_learning.py` 他の人名。
- **データを全入れ替え**: `data/brain/wiki/` の persona・hobbies・knowledge・decisions / response-bank / few-shot / alignment 収集物 / PrivacyGate 設定。
- **モジュールごと置換 or 撤去**: 業務 scraper (Net Mobile / historical / kpi-dash / stapa) / 必要なら `lineworks_bot.py` / 音声・動画 clone。
