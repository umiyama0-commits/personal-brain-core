# Personal Brain — ゼロから別企業 / 別エグゼクティブ向けに立ち上げる手順書

> このリポジトリは OWNDAYS CEO 海山丈司の Personal Brain (LINE Works Bot「うみやまAI」+ 個人 LINE Push + 売上 retrieval + 自己複製基盤) です。
> **別のエグゼクティブ / 別の会社向けに、新しいマシンで一から立ち上げる** ための実務手順をまとめます。
> 「うみやま」「OWNDAYS」固有の値は **全て自分の対象に置き換える** 前提で読んでください (置換ポイントは都度明記)。
>
> このドキュメント単体 + `.env.example` だけで本番起動まで到達できることを目標にしています。
> 既存ホスト構築メモ (`docs/mac_studio_setup_2026-05-25.md` / `docs/mac_studio_setup_2026-06-08-eval-batch.md`) はこの上位 wrapper です。
>
> Ground 元: `.env.example` / `docker-compose.yml` / `Dockerfile` / `requirements.txt` / `litellm_config.yaml` / `lineworks_bot.py` / `main.py` / `scripts/cron_install.sh` / `scripts/cron_env.sh` / `docs/failure-log.md`。

---

## 0. これは何を立てるのか (5 分で全体像)

3 つの常駐 Docker コンテナ (`docker-compose.yml`) + ホスト側 cron + Cloudflare Tunnel で構成されます。

| コンテナ | image | port | 役割 |
|---|---|---|---|
| `line-bot` | `build: .` (= `Dockerfile`、Python 3.12) | 8000 | FastAPI webhook server。Bot 本体 + retrieval + file watcher + 自己改善ループ |
| `litellm` | `ghcr.io/berriai/litellm:main-latest` | 4000 | 全 LLM 呼び出しの統一プロキシ (モデル別名・fallback・コスト上限) |
| `redis` | `redis:7-alpine` | 6379 | 会話状態・キャッシュ (AOF 永続) |

- **本番公開経路**: `cloudflared` (ホスト常駐、コンテナ外) が `http://localhost:8000` を公開 URL (例: `brain.example.com`) に出す。LINE Works / Vapi / HeyGen の webhook はこの公開 URL に届く。
- **データの正本**: `./data` を `line-bot:/app/data` に bind mount。ベクトル DB は named volume `chroma_data`。**Python ソースは image に焼き込み** (= `COPY . .`)、`./data` だけが外出し。これが後述の「rebuild 必須」の根拠。
- `main.py` の `lifespan` が起動時に `_initial_reindex` / 各種 watcher / 自己改善ループを `asyncio.create_task` で立ち上げる (`main.py:97-106`)。

---

## 1. 前提 (Prerequisites)

### 1.1 ホスト

- **OS**: macOS (現行本番は Mac Studio、Apple Silicon)。Linux でも原理上動くが、本書のホスト手順 (LaunchAgent / `cron_env.sh` の Docker.app パス / `open -a Docker`) は macOS 前提。
- **CPU/RAM**: ホスト総 RAM **最低 16GB、推奨 32GB 以上** (本番 Mac Studio は 36GB)。
- **ディスク**: 空き **30GB 以上**を推奨。内訳の目安 = Docker image (~1.5GB) + chromadb index + `./data/brain` の wiki/raw/会話履歴 (運用で増える) + restic ローカルキャッシュ。

### 1.2 ★Docker Desktop の VM RAM は必ず手で上げる (最重要の初手)

`docs/failure-log.md` 2026-06-15 の実障害: Docker Desktop の Linux VM が **7.75GiB 割当のまま** 19 日連続稼働で `line-bot` が 6.93GiB (89%) まで肥大 → VM がスラッシュ → Apple Virtualization が 709% CPU 暴走 → Docker デーモン無応答 → 公開サイト全断。

- **対策**: Docker Desktop → Settings → Resources → Memory を **16GiB 以上**に。
- 設定ファイル直書きでも可: `~/Library/Group Containers/group.com.docker/settings-store.json` の `"MemoryMiB": 16384`。
- 教訓: 常駐コンテナの実フットプリント (chromadb index 込み) に対し **2 倍以上の天井**を確保。デフォルト放置厳禁。

### 1.3 ソフトウェア

```bash
# Docker Desktop (要 GUI 起動、VM RAM は §1.2 で 16GiB+)
docker --version          # 期待: 24.x 以上
docker compose version    # 期待: v2.x (compose v2 サブコマンド)

# Cloudflare Tunnel
brew install cloudflared

# Python (ホスト側 cron / OAuth 取得スクリプトを直接叩く用。コンテナ内は 3.12 固定)
python3 --version         # 期待: 3.11 以上

# 任意 (機能を使うなら): restic = offsite backup, git-filter-repo = 履歴 purge, ffmpeg = 動画
brew install restic git-filter-repo ffmpeg
```

- **コンテナ内 Python は 3.12 固定** (`Dockerfile: FROM python:3.12-slim`)。`requirements.txt` の `chromadb==1.5.8` は cp39-abi3 wheel が 3.12 と整合する版に**意図的に pin** されている (1.5.9 で wheel 不在 → source build 失敗事故あり、`requirements.txt` 冒頭コメント参照)。**勝手に上げない**。
- `Dockerfile` は `build-essential` (wheel 不在パッケージの source build 救済) と `ffmpeg` (動画フレーム抽出) を入れている。

---

## 2. 外部アカウント取得チェックリスト

各サービスを取得し、対応する `.env` 変数に入れます。**REQUIRED = これが無いと bot が起動/応答しない**。OPTIONAL = その機能だけ無効 (graceful degradation)。

> ★凡例: 「`.env.example` に有」= テンプレに行がある / 「★テンプレ漏れ」= コードは読むが `.env.example` に行が無い (§3.9 で手追加必須)。

### 2.1 LLM / インフラ系 (ここが欠けると起動不能)

| サービス | 要否 | 何のため | 取得手順の要点 | env 変数 |
|---|---|---|---|---|
| **Anthropic API** | **REQUIRED** | `smart` (Opus 4.8) = Bot 応答・wiki compile・lint の主力。`contextualize` (Haiku) = retrieval 用 | console.anthropic.com → API Keys → Create。残高/上限も設定 | `ANTHROPIC_API_KEY` |
| **OpenAI API** | **REQUIRED** | **embedding (`text-embedding-3-small`) が retrieval の心臓** → これが無いと初回 reindex もベクトル検索も死ぬ。加えて `fast`/`default` (GPT-4o)、`smart-gpt` (GPT-5.4)、`whisper` (音声入力)、各 fallback | platform.openai.com → API keys。**Anthropic だけにしても OpenAI key は外せない** (embedding 依存) | `OPENAI_API_KEY` |
| **LiteLLM master key** | **REQUIRED** | litellm プロキシの管理キー (`general_settings.master_key`)。bot ↔ litellm 間の認証 | **自前生成**するだけ (外部取得不要)。例: `printf 'sk-litellm-%s' "$(openssl rand -hex 16)"` | `LITELLM_MASTER_KEY` |
| **Redis URL** | **REQUIRED** | 会話状態・キャッシュ。compose 内の `redis` を指す | 取得不要。compose 既定 `redis://redis:6379` のまま | `REDIS_URL` |
| **Cloudflare Tunnel** | **REQUIRED (本番公開)** | `localhost:8000` を HTTPS 公開 URL に。LINE Works/Vapi webhook の到達先 | §6 参照。`cloudflared tunnel login` → named tunnel 作成 → DNS 紐付け。**env 変数ではなくホスト設定** | (env 無し、`~/.cloudflared/` + tunnel config) |
| **Gemini API** | OPTIONAL | Drive AI 検索 (`/drive ai`) の query 拡張 + re-rank | aistudio.google.com で 5 分・無料枠あり (billing 必須)。`.env.example:44-51` 参照 | `GEMINI_API_KEY` (`GEMINI_MODEL` 省略時 `gemini-2.5-flash-lite`) |

### 2.2 メッセージング (Bot の入出力)

| サービス | 要否 | 何のため | 取得手順の要点 | env 変数 |
|---|---|---|---|---|
| **LINE Works Bot** | **REQUIRED (これが本体)** | 「うみやまAI」= 社内向け Bot Clone の配信チャネル。認証は **Service Account JWT (RFC 7523)** | developers.worksmobile.com で ① Developer Console から **Bot 登録** → ② **App (OAuth Scope `bot`)** を作り `client_id`/`client_secret` 取得 → ③ **Service Account** 発行 → ④ その Service Account の **RSA 秘密鍵 (.pem)** を DL → ⑤ Bot 詳細から `Bot ID` 取得 → ⑥ webhook 署名検証用 `Bot Secret` 取得 | `LW_CLIENT_ID` / `LW_CLIENT_SECRET` / `LW_SERVICE_ACCOUNT` / `LW_PRIVATE_KEY_PATH` (or `LW_PRIVATE_KEY`) / `LW_BOT_ID` / `LW_BOT_SECRET` |
| ↑ Bot User ID | RECOMMENDED | グループ内 @mention 判定 (`lineworks_bot.py:is_mentioned`)。未設定だと plain-text `@名前` のみで検出 | Bot プロフィール画面で取得 (多くは UUID)。グループ運用しないなら後回し可 | `LW_BOT_USER_ID` (★テンプレ漏れ) / `LW_BOT_MENTION_NAMES` (★テンプレ漏れ、対象者の呼称をカンマ区切り) |
| **LINE 公式アカウント** | OPTIONAL (推奨) | エグゼクティブ**個人**への Push 通知 (コスト警告・障害アラート・売上検証結果)。Bot Clone とは別物 | developers.line.biz → Messaging API チャネル作成 → Channel access token (long-lived) + Channel secret | `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` |

> **LINE Works 認証フロー (`lineworks_bot.py` で確認した実装)**: `client_id`(iss)/`service_account`(sub) を claim にした **RS256 JWT** を秘密鍵で署名 → `https://auth.worksmobile.com/oauth2/v2.0/token` に `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer` + `scope=bot` で POST → access_token を 24h キャッシュ。送信は `https://www.worksapis.com/v1.0/bots/{LW_BOT_ID}/users/{userId}/messages`。webhook は `X-WORKS-Signature` を `base64(HMAC-SHA256(body, LW_BOT_SECRET))` で照合。

### 2.3 検索精度 / データ取り込み

| サービス | 要否 | 何のため | 取得手順の要点 | env 変数 |
|---|---|---|---|---|
| **Cohere** | OPTIONAL (精度大) | Rerank 3.5 (`rerank-v3.5`) で chromadb candidate を関連度順に並べ替え (`brain_wiki_helpers/rerank.py`)。**未設定なら旧 retrieval flow にフォールバック** (壊れない) | dashboard.cohere.com → API Keys。$2/1,000 searches | `COHERE_API_KEY` (★テンプレ漏れ) |
| **Google API (Drive/Docs/Sheets)** | OPTIONAL | Drive selective sync (`gdrive_sync.py`)。資料取り込み | **実装は Service Account ではなく OAuth (refresh token 方式)**。手順は §5。Google Cloud Console で OAuth Client (Desktop) 作成 → `data/brain/credentials.json` 配置 → 同意フローで `data/brain/.google_token.json` 生成 | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `GOOGLE_REFRESH_TOKEN` (実際の読込は `.google_token.json`) |
| Drive 共有依頼アドレス | OPTIONAL | Bot が閲覧権限の無い Drive を共有された時に案内する宛先 | Bot 専用 Workspace アカウント発行を推奨 (`.env.example:53-59`) | `BOT_GDRIVE_SHARE_ADDRESS` |
| **対象企業の業務システム scraper** | 案件依存 | OWNDAYS では売上 (OWNDAYS Net Mobile / kpi-dash.com) や社内チャット (LINE Works scraper) / メルマガ (STAPA) を取り込む。**別企業では該当する社内システムに置換 or 丸ごと削除** | 各システムのログイン情報。Playwright login が多い | `OWNDAYS_MOBILE_*` / `KPIDASH_*` / `LINEWORKS_*` / `STAPA_*` (→ **自分の対象に読み替え。不要なら空のままで該当 cron/scraper を外す**) |

### 2.4 音声 / アバター (高度機能、最初は全部スキップ可)

| サービス | 要否 | 何のため | 取得手順の要点 | env 変数 |
|---|---|---|---|---|
| **Vapi** | OPTIONAL | 電話/Web 経由の音声アラインメント (本人の話し方を声で収集) | dashboard.vapi.ai → API Keys。Private (`sk_`) + Public (`pk_`) + 共有 Secret | `VAPI_SECRET` / `VAPI_PRIVATE_API_KEY` / `VAPI_PUBLIC_KEY` / `VAPI_SERVER_URL` (+ `VAPI_VOICE_*` 多数、`.env.example:152-181`) |
| **ElevenLabs** | OPTIONAL | 本人の Voice Clone TTS。Vapi 連携には `user.read`+`voices.read`+`text_to_speech` の 3 権限 (`.env.example:140-150`) | elevenlabs.io → Voice Lab で Pro Voice Clone → Voice ID。Profile → API Keys | `ELEVENLABS_VOICE_ID` / `ELEVENLABS_API_KEY` |
| **HeyGen** | OPTIONAL | 本人モーションアバターでの Live video 通話 | heygen.com → Account → API Token。Custom Avatar 作成 | `HEYGEN_API_KEY` / `HEYGEN_AVATAR_ID` / `HEYGEN_VOICE_ID` |
| **Recall.ai** | OPTIONAL | 会議 bot (議事録取り込み) | recall.ai → API key + webhook secret | `RECALL_API_KEY` / `RECALL_WEBHOOK_SECRET` / `RECALL_API_BASE` |

### 2.5 運用補助 (任意)

| サービス | 要否 | 何のため | env 変数 |
|---|---|---|---|
| **Backblaze B2 / AWS S3** | OPTIONAL | restic offsite backup (一次データ暗号化退避)。未設定なら `backup_offsite.sh` は loud-skip | `RESTIC_REPOSITORY` / `RESTIC_PASSWORD` / `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY` (or `AWS_*`) |
| **Codex CLI (別系列 GPT-5)** | OPTIONAL | Claude のコードを独立レビュー。未設定なら loud-skip | `CODEX_API_KEY` (or `codex login`) / `CODEX_REVIEW_MODEL` |

---

## 3. `.env` の埋め方 (`.env.example` をサービス別に walk-through)

```bash
cp .env.example .env
chmod 600 .env          # secret なので権限を絞る
```

`.env` は `.gitignore` 済 (gitleaks で平文 commit を block)。**実値は `.env` だけに**置く (CLAUDE.md §1.1)。
**置換の大原則**: `umiyama` / `owndays` / `brain.example.com` を**自分の対象者・会社・ドメインに全置換**。

以下、グループ別に「何を入れるか」と **デフォルト無し = 必ず provision** を明示。

### 3.1 LiteLLM / Redis (`.env.example:4-7`)
```
LITELLM_URL=http://litellm:4000          # ★そのまま (compose 内 hostname)
LITELLM_MASTER_KEY=sk-litellm-brain-XXXX # ★要 provision (自前生成、§2.1)
REDIS_URL=redis://redis:6379             # ★そのまま
```

### 3.2 LLM Providers (`.env.example:9-12`)
```
ANTHROPIC_API_KEY=sk-ant-...   # ★要 provision (REQUIRED)
OPENAI_API_KEY=sk-...          # ★要 provision (REQUIRED — embedding 依存)
GEMINI_API_KEY=...             # 任意 (Drive AI 検索)
```

### 3.3 LINE 公式 (個人 Push) (`.env.example:14-16`)
```
LINE_CHANNEL_ACCESS_TOKEN=...  # 任意だが推奨 (障害/コスト通知)
LINE_CHANNEL_SECRET=...
```

### 3.4 LINE Works Bot (`.env.example:18-24`) — **ここが本体、最重要**
```
LW_CLIENT_ID=xxx                                  # ★要 provision
LW_CLIENT_SECRET=xxx                              # ★要 provision
LW_SERVICE_ACCOUNT=xxxxxx.serviceaccount@<company># ★要 provision (ドメイン置換)
LW_PRIVATE_KEY_PATH=/app/data/brain/.lw_private_key.pem  # ★pem を §4.3 で配置
LW_BOT_ID=xxx                                     # ★要 provision
LW_BOT_SECRET=xxx                                 # ★要 provision (webhook 署名)
```
> 秘密鍵は **ホストの `./data/brain/.lw_private_key.pem` に置く** → コンテナからは `/app/data/brain/.lw_private_key.pem` で見える (bind mount)。`.pem` の中身を `LW_PRIVATE_KEY` に直書きしてもよい (`lineworks_bot.py:_get_private_key` は PATH 優先)。

### 3.5 業務システム scraper (`.env.example:26-42`) — **別企業では総入れ替え**
OWNDAYS 固有 (`OWNDAYS_MOBILE_*` / `LINEWORKS_*` / `STAPA_*` / `KPIDASH_*`)。
別企業では **(a) 自社システム用 scraper に置換** するか **(b) 空のままにして該当 cron/scraper を §7 で外す**。空でも起動はする (該当機能が無効化されるだけ)。

### 3.6 動作モード / フラグ (`.env.example:86-118`)
```
CLONE_PUBLIC_PROD_MODEL=smart   # Bot 応答モデル。最初は smart(Opus) 推奨
CLONE_MEMORY_ENABLED=1          # 個別メモリー
AUTO_DEPLOY_ENABLED=1           # git push → 自動 pull/rebuild (§7)
AUTO_DEPLOY_BRANCH=main
BRAIN_MCP_TOKEN=...             # ★要 provision (MCP/拡張アクセス用、自前生成)
DEPLOY_ADMIN_TOKEN=            # 任意 (deploy/rebuild を閲覧 token と分離。未設定で従来動作)
DEPLOY_EVAL_GATE=warn          # deploy 後 eval gate。warn 推奨スタート
```

### 3.7 音声/アバター/会議 (`.env.example:61-193`)
最初は **全部空で OK**。使う機能だけ後から埋める (§2.4)。

### 3.8 backup / Codex (`.env.example:120-138`)
最初は空で OK (loud-skip)。本番運用に入ってから §2.5 を埋める。

### 3.9 ★`.env.example` に行が無いが **コードが読む** 変数 (手で追記必須/推奨)

`.env.example` をコピーしただけでは足りません。以下を `.env` に**追記**してください。

```bash
# ── 認証トークン (web/API ゲート。未設定だと該当 endpoint が 503/401) ──
BRAIN_EXTENSION_KEY=<自前生成>     # /api/* の API key (claude_dispatcher.py:65, main.py:require_api_key)
ALIGNMENT_TRIAL_TOKEN=<自前生成>   # /admin/review 等の閲覧 token (routes/alignment_trial.py:40)
                                  #   ★DEPLOY_ADMIN_TOKEN 未設定時は deploy もこの token に fallback
WHITESPACE_TOKEN=<自前生成 or 空>  # /whitespace ダッシュボード保護 (main.py:124、空なら公開)
# (VOICE_ALIGN_TOKEN は .env.example に記載済のため手動追記リストから除外 ★2026-07-03)

# ── Cohere rerank (精度。未設定なら旧 flow にフォールバック) ──
COHERE_API_KEY=<取得値 or 空>

# ── LINE Works group 運用するなら ──
LW_BOT_USER_ID=<UUID>             # @mention 判定 (lineworks_bot.py)
LW_BOT_MENTION_NAMES=<呼称1,呼称2> # 例: 対象者の通称・Bot 名をカンマ区切り

# ── コスト計測を使うなら (★注意: §8 の 6/12 事故あり、最初は 0 のまま推奨) ──
# COST_TRACKING_ENABLED=1
```

> 自前生成は `openssl rand -hex 24` 等で十分長いランダム文字列を。`ALIGNMENT_TRIAL_TOKEN` は eval gate の実行にも使われるので**空にしない**。

---

## 4. 初回起動の通し手順

> 前提: §1 完了 (Docker Desktop 起動済 + VM RAM 16GiB+)、§3 で `.env` 記入済。
> 以下は `~/brain-agent` に clone した想定。本番の `REPO_ROOT` は `/Users/brain/brain-agent` (`cron_install.sh:21`) だが、開発機では任意で可。

### Step 1. clone

```bash
git clone <このリポジトリの URL> ~/brain-agent
cd ~/brain-agent
git log --oneline -1        # 確認: HEAD が取れる
```

### Step 2. `.env` 配置 + 秘密鍵 + データ雛形

```bash
# .env (§3 で作成済を配置)
ls -l .env && stat -f '%Sp' .env     # 確認: 存在 & 権限 600 (-rw-------)

# LINE Works 秘密鍵を bind mount 配下へ
cp /path/to/your_lw_private_key.pem data/brain/.lw_private_key.pem
chmod 600 data/brain/.lw_private_key.pem

# BRAIN_ROOT が要求する wiki/ raw/ を用意 (brain_wiki.py:60-62、無いと reindex が空振り)
mkdir -p data/brain/wiki data/brain/raw
ls -d data/brain/wiki data/brain/raw   # 確認
```
> **コンテンツ移植**: この時点で `data/brain/wiki/**` と `data/brain/raw/**` は**空 or 最小**。OWNDAYS の wiki/個人データは持ち込まない (別人格)。新エグゼクティブの素材を後から `raw/` に投入 → file watcher / compile が wiki 化していく。最初は空でも bot は起動する (retrieval が空を返すだけ)。

### Step 3. build (Python source を image に焼く)

```bash
docker compose build line-bot
# 確認: 最終行付近に "naming to ... line-bot" / "FINISHED"。
#       chromadb 1.5.8 の wheel が引けず build-essential 経由 source build に落ちても OK。
```

### Step 4. up (3 コンテナ起動)

```bash
docker compose up -d
docker compose ps
# 確認: redis(healthy) / litellm(running) / line-bot(running→healthy)。
#       line-bot は healthcheck start_period 30s。
```

`depends_on` で `redis` healthy + `litellm` start を待ってから `line-bot` が立つ (`docker-compose.yml:15-19`)。

### Step 5. 初回 reindex の完了待ち (~数分、自動)

初回 reindex は**手動コマンド不要**。`main.py:_initial_reindex` が起動 **10 秒後** (litellm の embedding API が立つのを待つ) に走り、**chunks が 0 の時だけ** `WIKI_DIR` と `RAW_DIR` を全件ベクトル化します (`main.py:5798-5817`)。

```bash
# litellm が応答するか (embedding 経路の生死)
curl -s http://localhost:4000/health -H "Authorization: Bearer $LITELLM_MASTER_KEY" | head

# reindex の進行/完了をログで追う
docker logs -f line-bot 2>&1 | grep -iE "reindex|index build|chunks"
# 期待ログ:
#   "Initial vector index build starting..."
#   "Initial reindex complete: <N> chunks"          # ← 完了
# 既にデータがある状態で再起動した場合:
#   "Vector index already has <N> chunks — skipping reindex"
```
- 所要は wiki/raw の量次第。**空スタートなら即完了**。大量素材を先に入れた場合は OpenAI embedding API の batch で数分〜十数分。
- ★`OPENAI_API_KEY` が無効だとここで `Initial reindex error` が出て検索が死ぬ。最優先で疎通確認。
- ★`chromadb 並行アクセス禁止`: この最中に `docker exec line-bot python3 scripts/reindex_history.py` 等を**別途回さない** (SIGSEGV crash loop)。reindex は line-bot 停止時のみ (CLAUDE.md §1.5)。

### Step 6. bot 疎通テスト (公開前のローカル確認)

```bash
# health
curl -s http://localhost:8000/health      # 期待: 200 + JSON

# LINE Works 設定が揃っているかをコンテナ内から
docker exec line-bot python3 -c "import lineworks_bot as lw; print('LW configured:', lw.is_configured())"
# 期待: LW configured: True  (False なら §3.4 のどれかが欠落)
```

### Step 7. 公開経路 (Cloudflare Tunnel) を上げて webhook を通す

§6 を実施し、LINE Works Developer Console の **Callback (webhook) URL** を
`https://<your-domain>/webhook/lineworks` に設定 (実装の受け口は `main.py:1728` の `@app.post("/webhook/lineworks")`)。
Console の「Bot 監視」で webhook が届いているか確認。なお `main.py` には個人 LINE 用 `@app.post("/webhook")` (1307)、Recall 用 `/webhook/recall`、音声用 `/webhook/voice-alignment` も別途あるので混同しない。

### Step 8. cron install (ホスト側の定期ジョブ)

```bash
# ★本番の cron は REPO_ROOT を /Users/brain/brain-agent 固定で参照する。
#   別パスに置いたなら REPO_ROOT を上書きして実行 (cron_install.sh:21)。
REPO_ROOT=~/brain-agent bash scripts/cron_install.sh
crontab -l | head -40        # 確認: clone_cron.sh / scrape_cron.sh / health_cron.sh 等が並ぶ
```
- `cron_install.sh` は**冪等** (既存行と pattern 一致で重複追加しない)。`auto_deploy` が毎サイクル呼ぶ前提。
- **★新規 cron を足す/有効化する前に最小 PATH で dry-run** (§8 の鉄則):
  ```bash
  env PATH="/usr/bin:/bin" bash scripts/health_cron.sh --dry-run   # docker 等が解決できるか
  ```
- macOS では一部は crontab でなく **LaunchAgent** で登録 (例: `com.brain.docker-watchdog`、`cron_install.sh:34` のコメント参照)。`scripts/install_launchagents.sh` を併用。

---

## 5. Google OAuth (Drive 連携を使う場合のみ)

実装は **Service Account ではなく OAuth refresh token 方式** (`gdrive_sync.py:94` = `Credentials.from_authorized_user_file`、bootstrap は `google_sync.py` の `InstalledAppFlow`)。SCOPES は Drive/Gmail/Calendar の **readonly** 3 種 (`gdrive_sync.py:62`)。
> ★注意: 既存メモ `docs/mac_studio_setup_2026-05-25.md` Task F は `scripts/google_sync.py` / `data/brain/credentials.json` と書いているが、**実際の実装はリポジトリ直下の `google_sync.py` と直下 `credentials.json`** (`google_sync.py:44` = `BASE_DIR / "credentials.json"`)。以下が正。

```bash
cd ~/brain-agent

# 1. Google Cloud Console で OAuth Client (種別: デスクトップ) を作成 → JSON を DL
#    → リポジトリ直下に credentials.json として配置 (★data/brain/ ではない)
ls credentials.json     # 確認

# 2. 同意フロー (ブラウザ起動。Bot 用 Workspace アカウントでログイン推奨)
python3 google_sync.py
#    → 同意画面で「許可」→ data/brain/.google_token.json が生成される (TOKEN_FILE)

# 3. refresh_token を確認
python3 -c "
import gdrive_sync
c = gdrive_sync.get_credentials()
print('refresh_token:', bool(getattr(c,'refresh_token',None)))
print('scopes:', getattr(c,'scopes','unknown'))
"
# 期待: refresh_token: True
```
- `.google_token.json` は `data/brain/` 配下 = bind mount でコンテナからも見える。`get_credentials()` が期限切れ時に自動 refresh して書き戻す。
- ★人事評価/給与/相談記録など PII を含む Drive は `gdrive_sync.py` の `DEFAULT_EXCLUDE_PATTERN` で block される (CLAUDE.md §1.9)。別企業でも除外方針を踏襲。

---

## 6. Cloudflare Tunnel (本番公開)

`docker compose up` はローカル `localhost:8000` まで。外部 webhook を受けるには公開 URL が要る。

```bash
# A. ログイン (ブラウザでドメイン認可)
cloudflared tunnel login

# B. named tunnel 作成 (credentials json が ~/.cloudflared/<UUID>.json に出る)
cloudflared tunnel create brain-<company>

# C. DNS 紐付け (例: brain.example.com → このトンネル)
cloudflared tunnel route dns brain-<company> brain.example.com

# D. config (~/.cloudflared/config.yml) — service を localhost:8000 に向ける
#    tunnel: <UUID>
#    credentials-file: /Users/<you>/.cloudflared/<UUID>.json
#    ingress:
#      - hostname: brain.example.com
#        service: http://localhost:8000
#      - service: http_status:404

# E. 常駐起動 (macOS は LaunchAgent or `brew services` 推奨。最小確認は前景起動)
cloudflared tunnel run brain-<company>

# 確認: 外部から
curl -s https://brain.example.com/health     # 期待: 200
```
- ★`cloudflared` は **コンテナ外のホストプロセス**。`docker compose` には含まれない (failure-log でも別系統として扱われる)。
- ARCHITECTURE §10 の `cloudflared tunnel --url http://localhost:8000` は **使い捨て URL** (quick tunnel)。本番は上記 named tunnel + 固定ドメインにする。

---

## 7. auto_deploy (任意だが本番は推奨)

`AUTO_DEPLOY_ENABLED=1` だと、別マシンから `git push origin main` するとホストの `scripts/auto_deploy.sh` (cron 5 分間隔) が pull → build → force-recreate を自動実行 (CLAUDE.md §1.6)。本番マシンを直接触らず MacBook から deploy できる。最初の 1 台だけ立てるなら無効でも可。

> **別企業 scraper の取り回し**: §3.5 で空にした OWNDAYS 系 scraper の cron 行は、`cron_install.sh` の `REQUIRED_CRONS`/`PATTERNS` から該当行を削除 (両配列は**同数・同順**必須、ズレると `set -u` で落ちる — `cron_install.sh:151`)。

---

## 8. 初回によくハマる所 (`docs/failure-log.md` 由来)

### 8.1 ★Docker image rebuild 必須 — source は焼き込み
`docker-compose.yml` の `line-bot` は `./data:/app/data` だけ mount。`main.py` 等の **Python source は image に baked-in** (`Dockerfile: COPY . .`)。
- **コード編集後は必ず**: `docker compose build line-bot && docker compose up -d --force-recreate line-bot`
- **`docker compose restart line-bot` だけでは古いコードのまま動く** (failure-log「Docker image rebuild 漏れ」、CLAUDE.md §1.4)。
- 例外: `.env` 変更は restart で反映 (env は image 外)。`./data` 配下の変更も即反映。

### 8.2 ★cron は最小 PATH (`/usr/bin:/bin`) で起動する
host cron は Docker Desktop / Homebrew の bin が PATH 外 → `docker`/`ffmpeg`/`git` が `command not found`。
- **新規 cron wrapper は冒頭で `source scripts/cron_env.sh`** (PATH 補正 + `.env` source + `LITELLM_URL` を `localhost` に書換 + `BRAIN_APP_ROOT`/`BRAIN_ROOT` export を一括、`cron_env.sh` 全体)。
- **登録前に必ず** `env PATH="/usr/bin:/bin" bash scripts/<wrapper>.sh --dry-run` で外部バイナリ解決を実証 (CLAUDE.md §1.8。これを怠ると毎朝の health チェックが誤発火してオオカミ少年化)。
- scraper 系 wrapper は冒頭で `set -a; . ./.env; set +a` を忘れない (cron は親 shell env を継承しない → silent skip)。

### 8.3 ★chromadb 並行アクセス禁止
`line-bot` 稼働中に `docker exec line-bot python3 scripts/reindex_history.py` 等の**別 chromadb 書込みを回さない** → SIGSEGV crash loop。reindex は line-bot 停止時のみ (CLAUDE.md §1.5、`docs/decisions/2026-04-27-chromadb-concurrent-access-ban.md`)。同一プロセス内の asyncio 直列 watcher は安全 (`main.py:_watch_wiki_changes` のコメント)。

### 8.4 ★Docker VM RAM 枯渇 (§1.2 の再掲・最重要)
VM RAM を放置すると長期稼働でじわ伸び → VM スラッシュ → デーモン wedge → 全断 (2026-06-15)。**16GiB+ に上げる**。`docker compose restart` はデーモン無応答下では**コマンド自体がハングする**ので、外部からの死活監視 (公開 URL を叩く) を別途持つ。

### 8.5 起動時 reindex が「空」になる / 検索が無 hit
- `data/brain/wiki` `data/brain/raw` を作り忘れ (§4 Step 2) → reindex 対象 0。
- `OPENAI_API_KEY` 無効 → `Initial reindex error` (embedding 失敗)。`docker logs line-bot | grep -i reindex` で確認。
- 既存 chunks があると skip される (`chunks > 0`)。最初からやり直すなら `chroma_data` volume を消す (= ★destructive、承認の上で `docker compose down && docker volume rm brain-agent_chroma_data`)。

### 8.6 webhook が届かない / 401
- LINE Works Console の Callback URL が公開ドメインを指しているか。`LW_BOT_SECRET` 不一致だと署名検証 false (`lineworks_bot.py:verify_signature` は未設定時 warning 出して false)。
- グループで反応しない → `LW_BOT_USER_ID` 未設定で @mention 検出が plain-text 限定 (§3.9)。

### 8.7 コスト計測フラグの罠
`COST_TRACKING_ENABLED=1` は過去に潜伏 TypeError を起動し **19 時間 全応答が「お休み」化**した事故あり (failure-log 2026-06-12)。**最初は未設定 (0) のまま**にし、`tests/test_bot_run_context_key_collision.py` 等が通ることを確認してから有効化。

---

## 9. 動作確認チェックリスト (公開後)

```bash
TOKEN="$ALIGNMENT_TRIAL_TOKEN"

# 1. health (ローカル & 公開)
curl -s http://localhost:8000/health
curl -s https://brain.example.com/health

# 2. ベクトル検索が生きているか (chunks > 0)
docker exec line-bot python3 -c "
import asyncio, httpx, os
from brain_index import BrainIndex
async def m():
    async with httpx.AsyncClient() as h:
        idx = BrainIndex(h, os.getenv('LITELLM_URL'), os.getenv('LITELLM_MASTER_KEY'))
        print('chunks:', idx.get_stats()['total_chunks'])
asyncio.run(m())
"
# 期待: chunks: <N>  (0 なら §8.5)

# 3. deploy 状態 (auto_deploy 健全性)
curl -s "http://localhost:8000/api/admin/deploy-status?token=$TOKEN" | python3 -m json.tool | head

# 4. テスト (pytest)
docker exec line-bot python3 -m pytest tests/ -q     # or ホストで python3 -m pytest tests/ -q

# 5. 実 bot クエリ
#    LINE Works で対象 Bot に 1:1 で挨拶 → 応答が返る。
#    グループでは @mention して反応を確認。
#    LINE 公式を設定したなら、コスト/障害アラートが個人 LINE に届くか。
```

---

## 10. 移植時の置換ポイント早見表

| 置換対象 | 現状値 (OWNDAYS) | どこに出るか |
|---|---|---|
| エグゼクティブ名/呼称 | 海山 / うみやま / うみやまAI | `LW_BOT_MENTION_NAMES`、`data/brain/wiki/**` 人格データ、`CLONE_PROMPT` 系 |
| 会社/ドメイン | OWNDAYS / owndays / brain.example.com | `LW_SERVICE_ACCOUNT`、`BOT_GDRIVE_SHARE_ADDRESS`、`VAPI_SERVER_URL`、Cloudflare DNS |
| LINE Works テナント | OWNDAYS Workspace | `LW_*` 一式 (新テナントで Bot/App/ServiceAccount 再発行) |
| 業務 scraper | OWNDAYS Net Mobile / kpi-dash / STAPA / LINE Works | §3.5 で総入れ替え or 削除 + cron 行除去 |
| 人格/知識データ | `data/brain/wiki/**` `raw/**` | **持ち込まず空スタート**、新素材を投入 |
| 各種 token | 既存値 | 全て新規生成 (`BRAIN_EXTENSION_KEY`/`ALIGNMENT_TRIAL_TOKEN`/`VOICE_ALIGN_TOKEN`/`WHITESPACE_TOKEN`/`BRAIN_MCP_TOKEN`/`LITELLM_MASTER_KEY`) |

---

## 付録: 最短ハッピーパス (コア機能だけ・音声/scraper 無し)

```bash
# 0) Docker Desktop VM RAM を 16GiB+ に (§1.2)
# 1) clone
git clone <URL> ~/brain-agent && cd ~/brain-agent
# 2) .env (最小)
cp .env.example .env && chmod 600 .env
#    必須: LITELLM_MASTER_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY /
#          LW_CLIENT_ID / LW_CLIENT_SECRET / LW_SERVICE_ACCOUNT / LW_PRIVATE_KEY_PATH / LW_BOT_ID / LW_BOT_SECRET
#    追記 (§3.9): BRAIN_EXTENSION_KEY / ALIGNMENT_TRIAL_TOKEN
cp /path/lw.pem data/brain/.lw_private_key.pem && chmod 600 data/brain/.lw_private_key.pem
mkdir -p data/brain/wiki data/brain/raw
# 3) build & up
docker compose build line-bot && docker compose up -d
docker compose ps
# 4) 初回 reindex 完了待ち
docker logs -f line-bot 2>&1 | grep -iE "reindex|chunks"
# 5) 疎通
curl -s http://localhost:8000/health
docker exec line-bot python3 -c "import lineworks_bot as lw; print(lw.is_configured())"
# 6) 公開 (§6) → LINE Works Console で Callback URL 設定
# 7) cron
REPO_ROOT=~/brain-agent bash scripts/cron_install.sh
```

これで「最小うみやまAI 相当」が別エグゼクティブ向けに 1 台で立ち上がる。音声/アバター/scraper/backup は §2.4-2.5 を後乗せ。
