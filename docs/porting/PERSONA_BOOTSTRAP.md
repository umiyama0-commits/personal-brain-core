# PERSONA_BOOTSTRAP — ゼロデータから新しい人の AI クローンを立ち上げる方法論

> このドキュメントは Personal Brain (= OWNDAYS 海山丈司の「うみやまAI」) の中核メソドロジーを、**別の人物 (= あなたの経営者 / 上司 / 対象者) を、ゼロデータから AI クローン化する**ための tutorial として再構成したもの。
>
> 各 ADR に散在していた方法論をここに集約する。原典:
> - `docs/decisions/2026-04-28-self-replication-foundation.md` (4 層モデル + 二重ゲート)
> - `docs/decisions/2026-05-22-karpathy-development-principles.md` + `docs/development_principles.md` (Karpathy raw→compile→wiki 思想)
> - `docs/decisions/2026-05-14-clone-memory.md` (個別メモリー)
> - `docs/decisions/2026-05-16-hobbies-ingestion-flow.md` (嗜好取り込み)
> - `docs/decisions/2026-05-21-clone-quality-loops.md` (多層 self-eval loop)
> - `docs/decisions/2026-05-07-clone-public-upgrade-to-opus.md` (smart Opus 化 + 賢さ強化)
> - `brain_wiki.py` (compile ロジック / `CLONE_PUBLIC_PROMPT` / cache split)
> - `scripts/extractors/` (style / judgment / reflex / embodiment / drift_detector / alignment_snapshot)
> - `scripts/clone_alignment_trial.py` + `docs/alignment_trial/questions.md` (アラインメント sprint)
>
> 対象読者: 「自分の経営者のクローンを作りたい」開発者。本人 (= 対象者) の協力が前提。
>
> 本ドキュメントでの用語: **対象者** = クローン化したい本人 (= 海山に相当する人)。**クローン** = 立ち上げる AI 分身 (= うみやまAI に相当)。

---

## 0. このシステムが「普通の RAG bot」と違う 1 点

普通の「社内ナレッジ bot」は **知識 (= 何を知っているか)** を retrieval する。
このシステムが再現するのは **人格 (= どう考え、どう喋り、何を選ぶか)** で、知識はその *材料* にすぎない。

`CLONE_PUBLIC_PROMPT` (`brain_wiki.py:388`) の冒頭が全てを物語る:

> ★★★ 基本原則 (= 最上位、他全ルールに優先): 人間らしいコメントバック ★★★
> bot の **第一の役割は 人間らしい応答**。データ retrieval は **2 の手**。

つまりこの方法論の成否は **「対象者の声・判断・温度をどれだけ忠実に固定化できるか」** で決まる。知識ベースの網羅性ではない。最大の失敗モードは hallucination ではなく **「素の親切な AI アシスタントの声に戻ってしまう」** こと (詳細は Step 3)。これを念頭に読み進めること。

---

## 1. 全体像 — 4 層 self-replication モデル + パイプライン

### 1.1 パイプライン (= データの流れ)

```
[Step 1] raw 取込        対象者の生データ (chat / メモ / 会議録 / 嗜好 / 判断例)
   │                     → data/brain/raw/notes/, raw/conversations/
   ▼
[Step 2] Karpathy compile  raw → LLM compile → 集約 wiki (identity / style / thinking)
   │                     + 抽出器が個別パターン化 (style/* judgment/* reflex/* embodiment/*)
   ▼
[Step 3] alignment       本人が AI 応答を実際に書き直す → response-bank / few-shot で「声」を固定
   │
   ▼
[retrieval]              query が来たら 静的人格 (core 常駐) + 動的 retrieval (vector) を組み立て
   │
   ▼
[clone respond]          CLONE_PUBLIC_PROMPT に流して応答 (clone_respond_public)
   │
   ▼
[Step 4] 品質ループ       夜間 regression / hallucination check / synthetic-employee / drift
[Step 5] メモリー         per-user memory + sleep-time agent + auto-improve (継続学習)
```

この「**raw → LLM compile → wiki → retrieval → clone respond**」が背骨。Karpathy 思想 (`docs/development_principles.md`) に由来する: **生データを人が手で構造化するのではなく、LLM に compile させて wiki にし、それを retrieval で使う**。

### 1.2 4 層 self-replication モデル

`docs/decisions/2026-04-28-self-replication-foundation.md` の中核。**「知識ベース」から「文字 / 判断 / 反射 / 身体性まで本人像を再現する基盤」へ拡張する**ための 4 層構造。

| 層 | 役割 | 粒度 | 生成元 |
|---|---|---|---|
| **集約** (`identity.md` / `style.md` / `thinking.md`) | 腐りにくい本質サマリ。クローンの core retrieval に**常時投入** | 1 ファイル | LLM compile (Step 2) |
| **個別** (`wiki/style/*` / `judgment/*` / `reflex/*` / `embodiment/*`) | 観察可能な単位パターン。集約サマリの根拠 | 1 ファイル = 1 パターン | `scripts/extractors/{style,judgment,reflex,embodiment_indexer}.py` |
| **楔** (`meta/alignment_state.md`) | 月次の本人像スナップショット。翌月との差分検出の基準点 | 1 月 = 1 ブロック | `scripts/extractors/alignment_snapshot.py` (月初 04:30 cron) |
| **監視** (`meta/drift_log.md` + `audit/pending_questions.md`) | 個別記事の差分時系列 + 確認すべき問い | エントリ追記 | `scripts/extractors/{drift_detector,audit_generator}.py` |

4 つの層は別の役割を持つ:
- **集約層** = クローンが毎ターン読む「人格の core」。サイズが小さく、常にプロンプトに入る。
- **個別層** = 「なぜそう判断するか」の根拠。vector search で必要時に引かれる。
- **楔層** = 「2026-04 の対象者像」を固定しないための時間軸の基準点。
- **監視層** = パターンが古びたら本人に再確認を促す仕組み。

#### なぜ「集約」と「個別」を分けるのか

集約 (identity/style/thinking) だけだと根拠が消え、個別だけだと毎ターンの core に乗らない。**集約は常時 core 常駐 → 人格の一貫性を保証、個別は vector で必要時 → 深さを供給**、という二段構え。`brain_wiki.py:_read_wiki_state_public_compact` (`brain_wiki.py:3477`) で集約層が core_files として固定列挙され、個別層は vector hits として動的に足される。

### 1.3 二重ゲート (プライバシー)

人格データは PII の塊なので、2 段のゲートで守る (`docs/decisions/2026-04-28-self-replication-foundation.md`):

- **入口ゲート (PrivacyGate)** = raw → wiki に入る時の 5 カテゴリ排除 (`privacy_gate.py`、ルール → LLM 分類 → PII 除去の 3 段)。
- **出口ゲート (clone_visibility: public | private)** = クローンが retrieval で踏んでよいかの最終フィルタ。各 wiki ファイルの frontmatter に `clone_visibility` を書く。`private` はクローンの context から除外される。
- (将来枠) **exit_visibility: public | internal | private** = メタ枠だけ確保済み。現状 retrieval は clone_visibility ベース。

> **新規プロジェクトでの最重要設計判断**: 対象者の「公開してよい人格」と「公開してはいけない私的領域」の線をどこに引くか。`clone_visibility` を最初に設計しておかないと、後から PII がクローンの口から漏れる。

### 1.4 「複製は固定化を意味しない」(安全装置)

各個別パターン記事に **`last_updated` / `last_validated` / `last_observed` / `last_reviewed`** を必ず記録する。`drift_detector` が閾値 (90 / 90 / 60 / 180 日) を超えた記事を `pending_questions` に起票 → 本人確認 → 記事更新 → `drift_log` に履歴。さらに判断は「腐る」だけでなく「**置き換わる**」ので、`superseded_by` / `valid_until` を立てて旧版を retired 扱いにする (記事は削除せず時系列で残す)。

これで「2026-04 時点の対象者像」が永続化されてしまうのを構造的に防ぐ。**人格は更新され続けるもの**として設計する。

---

## 2. Step 1 — データ取込 (raw)

### 2.1 何を集めるか

クローンの 4 層は全て raw から作られる。集めるべきは以下 5 種:

| 種別 | 何を捉えるか | 置き場所 | 取込経路 |
|---|---|---|---|
| **chat 履歴** | 口語・語尾・温度・対人スタンス (最重要、口語の宝庫) | `data/brain/raw/conversations/*.md` | `chat_import.py` (LINE エクスポート .txt パーサー) |
| **メモ / ノート** | 思考の断片・価値観の言語化 | `data/brain/raw/notes/*.md` | `apple_notes_sync.py` (差分同期) |
| **会議録 / 議事録** | 実際の判断の現場・発言 | meeting compile 経由 → `wiki/knowledge/meetings-*.md` | `import/{plaud,recall,owl}/` に置く → `compile_meeting_note` |
| **嗜好 (音楽 / 本 / 映画 / 漫画)** | 美意識・価値観の核軸 (Step 2.3 参照) | `data/brain/wiki/hobbies/{music,books,movies,manga}/` | `docs/decisions/2026-05-16-hobbies-ingestion-flow.md` の標準フロー |
| **判断例 / 価値観** | 意思決定パターン・判断軸 (★最重要) | `data/brain/raw/notes/alignment_*.md` + `wiki/decisions/*.md` | **アラインメント survey** (下記) |

### 2.2 取込フロー (import dir watcher)

中心は `main.py:_watch_import_dir` (`main.py:5888`)。`data/brain/import/` を 30 秒間隔でポーリングし、自動取込する:

1. **`import/{plaud,recall,owl}/`** に会議 transcript (`.txt` / `.md`) → `compile_meeting_note` で処理 (ファイル名規約 `YYYY-MM-DD_title.txt`)。
2. **`import/` 直下の `.txt` + バイナリ (`.pdf .docx .xlsx .pptx .csv .tsv .md`)** → PrivacyGate 通過 → `ingest_note` で LLM compile。バイナリは `content_extractor.py` (pypdf + pdfminer.six) でテキスト抽出してから compile。
3. 取込済みは `import/processed/` へ退避。

#### ★ deterministic-scraper-prefix ルール (踏むと wiki が壊れる罠)

`docs/decisions/2026-04-27-scraper-output-no-llm-compile.md` の重要ルール。**決定論的に wiki を書くスクレイパーの出力を LLM compile に流してはいけない** — LLM が要約・上書きして wiki が破壊される (312 店舗テーブル → 数行に削られた実例)。

`main.py:_watch_import_dir` の `DETERMINISTIC_SCRAPER_PREFIXES` (`main.py:5902`) で prefix 一致のファイルは compile をスキップし archive のみ:
```python
DETERMINISTIC_SCRAPER_PREFIXES = (
    "owndays_mobile_sales_",
    "owndays_history_",
)
```
**新プロジェクトの教訓**: テーブルや構造化データを「決定論的に直接 wiki に書く」経路 (= scraper) と、「自由文を LLM に compile させる」経路 (= notes/会議録) を**最初から分離**し、前者の prefix をこのリストに登録する。混ぜると人格データもデータ wiki も両方壊れる。

### 2.3 嗜好取り込みの標準フロー

`docs/decisions/2026-05-16-hobbies-ingestion-flow.md`。嗜好は単なる雑学ではなく **対象者の価値観の核軸を捉える材料**。OWNDAYS では過去 170 作品 (漫画 35 / 本 72 / 映画 14 / 音楽 19) を投入し、**横断テーマ A〜F** (「青春の終わりと出発」「凡人主人公の覚醒」「血縁でない家族・居場所」等) として価値観に紐付けた。

フローの要点:
1. タイトル受領 → ジャンル判定 → (表記揺れは web 一次ソースで確定。例「建国日記」→「違国日記」)。
2. `data/brain/wiki/hobbies/{manga,books,movies,music}/<slug>.md` を作成 (slug は kebab-case 英語転字)。同著者 2 冊以上は著者集約 wiki に。
3. frontmatter 必須 (`clone_visibility: public`、`confidence: high|medium|low`)。
4. 本文 § 3「対象者の価値観との接続」が**最重要** (= identity.md の軸に紐付ける)。
5. **それっぽいけど怪しい内容を書かない** — 推測なら「要本人確認」と明示。Personal Brain の根幹は信頼性。

### 2.4 最小構成データセット (MINIMUM viable)

ゼロから始める時、**最初に必要なのは「アラインメント survey」1 本**。OWNDAYS では `data/brain/raw/notes/alignment_*.md` (alignment_100 / alignment_50_personal / 部門別 align_*) がこれにあたる。これは:

- **対象者が想定質問に自分の言葉で回答したもの** = 価値観 + 文体を同時に捉えた、最も整った素材。
- 全抽出器 (`style_extractor.py` 等) が第一入力としてこれを読む (`入力: data/brain/raw/notes/alignment_*.md (... 最も整った素材)`)。

#### 立ち上げ最小セット (これだけで「動くクローン」になる)

1. **アラインメント survey 100 問**: 対象者に「部下からこう聞かれたらどう答えるか」を 100 問書いてもらう (= `docs/alignment_trial/questions.md` を雛形に、対象者の業種に合わせて改変)。
2. **chat 履歴を数百ターン**: 口語・語尾を捉える (`raw/conversations/`)。
3. **嗜好リスト数十件**: 好きな本/映画/音楽 (価値観の核)。
4. (任意) **会議録 数本 + メモ**: 判断の現場と思考断片。

この最小セットを raw に置けば、Step 2 で identity/style/thinking が compile され、Step 3 で声を立てれば v0 クローンが動く。**完璧なデータを待たず、survey + chat の最小セットで一周回すのが正解** (Karpathy「Keep It Simple」)。初期 backfill 実績では会話 raw 10 件では reflex 0 件しか抽出できなかった (= 反射パターンは raw 量が要る) ので、chat は多めに。

---

## 3. Step 2 — Karpathy 式 compile

### 3.1 compile とは

Karpathy 思想 (`docs/development_principles.md`) の核: **生データを人が手で整形せず、LLM に「compile」させて構造化 wiki にする**。`brain_wiki.py:compile` (`brain_wiki.py:2129`) が実体:

```
raw_file を読む
  → COMPILE_PROMPT.format(schema, current_wiki, raw_data) を組む
  → LLM (default 'fast'、高価値操作は 'smart') に投げる
  → JSON {updates: [{file, ...}]} を受け取る
  → 各 update を wiki に apply (_apply_update)
  → index.md 再生成 + vector index 更新
```

ポイント:
- **current_wiki を一緒に渡す** = LLM は「既存 wiki に対する差分 update」を返す (全消し再生成ではない)。
- compile 先は LLM が判断する: 人格に関わる内容なら `identity.md` / `style.md` / `thinking.md`、知識なら `knowledge/` 等。
- raw が大きすぎる場合は `MAX_RAW_CHARS` で頭+尾を残して切り詰め。
- `_apply_update` には path traversal 防御あり (`..` を含むパスは拒否、`brain_wiki.py:2234`)。LLM 出力は信用しない。

### 3.2 集約 3 ファイルに何を抽出するか

compile が育てる集約層 (4 層モデルの最上段):

| ファイル | 捉えるもの | 例 (OWNDAYS) |
|---|---|---|
| **identity.md** | 価値観・信念・性格・人生観の軸 | 美意識 / 顧客至上 / リスク観 / 経歴の OS |
| **style.md** | 文体・口癖・相槌・コミュニケーション特性 | 「主語省略がデフォルト」「『うん』単独は使わない」「敬語の相手にも砕けたトーン」(`data/brain/wiki/style.md`) |
| **thinking.md** | 判断パターン・意思決定の傾向 | 「面・線・点」フレーム / ROI 2 年 / 単純化を拒否する |

`style.md` の実例を見ると、これが**いかに具体的か**が分かる (`data/brain/wiki/style.md`):
- 相槌語彙: ✅多用「なるほど / ほう / オッケー / そうね」 ❌「うん」単独 (子供っぽい) ❌「へえ」(距離感)
- 「徐々に掘り下げる」: 1st turn はミニマル、相手が掘ってきたら段階的に
- 「深層は伏流水、直接引用しない」: 経歴は語感に滲ませるだけ、表層に出さない

**抽象的な人格記述ではなく、観察可能な単位ルールまで落とす**のがコツ。

### 3.3 個別パターン抽出器

集約だけでは根拠が薄い。`scripts/extractors/` の抽出器が raw を読んで**個別パターン**を 1 ファイル = 1 パターンで量産する (4 層モデルの「個別」層)。

| 抽出器 | 出力 | 何を抽出 | 入力 |
|---|---|---|---|
| `style_extractor.py` | `wiki/style/style-<cat>-NNN.md` | 語彙 / 語尾 / 文体パターン | alignment + conversations + notes |
| `judgment_extractor.py` | `wiki/judgment/judgment-<domain>-NNN.md` | (situation, choice_made, underlying_value) の判断パターン | decisions + alignment + conversations |
| `reflex_extractor.py` | `wiki/reflex/*` | 即応反射 (「これを言われたらこう返す」) | conversations 中心 |
| `embodiment_indexer.py` | `wiki/embodiment/*` | 身体性 (話速 / ピッチ等の特徴。★バイナリは置かず external_path で参照) | 音声 manifest |

全抽出器の共通設計 (`scripts/extractors/_common.py`):
- **増分処理**: `extractor_state/<name>.json` に処理済みファイルの sha256 を記録。
- **dedup**: 既存 wiki パターンのサマリを LLM に渡し「意味的に重複するものは出すな」と指示。
- **堅牢化**: LLM 呼び出しに retry (5xx/429/timeout に exponential backoff)、JSON contract 検証、`run_context()` で構造化ログ。
- **テスト**: `tests/extractors/` pytest 64 件 (隔離 tmp_path、本物 data には触れない)。

新 raw が増えるたびに走らせる (週次 cron 推奨)。`--backfill` で初回一括、以降は差分 default。

### 3.4 ★ compile 出力を手編集しないルール (data 外科的分離)

最重要の運用規律。`docs/decisions/2026-05-19-data-surgical-separation.md`。

**`identity.md` / `style.md` / `thinking.md` は compile 出力なので、手編集して push してはいけない。** これらは git 管理外 (`.gitignore`)。Mac mini の compile が上書きするので、手編集は次の compile で消える (= 作業消失事故)。

`.gitignore` の構造 (実際の設定):
- **Mac mini が自動生成するもの** (compile 出力 identity/thinking/style.md、history-*.md 等) = ignore。
- **人が手で deliberate に書くもの** (`wiki/hobbies/`, `wiki/decisions/`, `wiki/style/`, `wiki/analysis/`, 一部 `wiki/knowledge/` の明示リスト) = track して git 同期。

> 実際このプロジェクトの本番運用では `data/brain/wiki/identity.md` / `thinking.md` は git 上に**存在しない** (= Mac mini ローカルの compile 生成物)。`style.md` は手編集対象として track されている。新プロジェクトでは「どのファイルが compile 生成 (ignore) で、どれが手編集 (track) か」を最初に `.gitignore` で線引きすること。

人格を直したい時は **raw を足して再 compile** か、**個別 wiki (track 対象) を手で書く**。集約ファイルを直接いじらない。

---

## 4. Step 3 — 人格の「声」を立てる (alignment)

ここがこの方法論の**心臓部**。Step 2 までで「知識と判断軸」は入るが、**喋り方が generic な AI のまま**だと別人になる。これを潰すのが alignment。

### 4.1 なぜこれが critical か (recent failure)

`CLONE_PUBLIC_PROMPT` の ★2026-06-15 ルール (`brain_wiki.py:426`) が、まさに最近の失敗を物語る。AI/技術トピックで bot が役立とうとした結果 **「素の親切な AI アシスタントの喋り方に戻る」** 事故が起きた:

> **「素の親切な AI アシスタント」の喋り方には戻らない** — ここを崩すと別人になる:
> - 対象者の言葉・温度・リーダーの視点で。主語省略・丸い語尾を保つ
> - 命令口調 (「〜しろ」) にはしない
> - 細かい技術トリビアに埋もれない (ただし要点になる具体策は 1 つは渡す)

base LLM は放っておくと**標準語・敬語・箇条書き・「それは興味深いですね」型の generic 応答に回帰する**。これが最大の敵。retrieval や compile では模倣しきれない。だから alignment で「声」を **system prompt に固定**する。

### 4.2 武器その 1 — 135 問アラインメント sprint

`scripts/clone_alignment_trial.py` + `docs/alignment_trial/questions.md`。v1 正式公開前に**本人が集中して 1-2 時間で精度を合わせる** sprint。

フロー (`clone_alignment_trial.py:16`):
```
1. parse_questions(questions.md)         # 135 問 (店舗 70 + 本部 65、対象者指定の比重)
2. run_trial(questions, model="smart")   # 各質問にクローンが応答
3. generate_html(...)                    # browser で開けるレビュー UI (コメント記入 + サーバ送信ボタン)
4. 本人がコメント記入 + 軸スコア(1-5) + JSON エクスポート
5. ingest_review(review.json)            # 結果を wiki/prompt に反映
6. rerun(base)                           # 改善後に再応答
7. diff(run1, run2)                      # 改善 trend を確認
```

各質問は `## <id> (役職 / カテゴリ)` + シナリオ本文 + `expected_axes` (期待する判断軸) で構成。配信は `main.py` の `/alignment-trial/*` endpoint (token 認証)。

**新プロジェクトでの作り方**: `questions.md` を雛形に、対象者の業種・組織のリアルな質問を 100 問前後用意する。「部下/同僚から実際に来そうな質問」を網羅し、各問に「対象者ならこう答えるべき軸」を書いておく。これを本人にレビューさせて反復する。

### 4.3 武器その 2 — response-bank (本人手書きの参考回答)

`data/brain/wiki/style/response-bank.md`。**対象者本人が想定質問に手書きした回答例**。これが「声」の原資。

冒頭の警告が設計思想を示す (`response-bank.md:21`):
> これは **参考回答例** (★本人記入)。bot 応答時に **逐語的に真似しない**、回答の長さ・構造も厳密に合わせない。抽出すべきは: **トーン / 温度 / 語尾 / 軸 / 思考の運び方 / コーティングの入れ方**。

中身の例 (alignment_trial で本人が AI 応答を**実際に書き直した**もの):
- トーン共通ルール: 相手が敬語でも砕けたトーン / 「店長さん」と呼ばない / 文末を「です」で揃えない / 主語省略
- 「★ 本人の実書き直し例」: AI が出した応答を本人が赤入れした before/after を掲載 → **「なるほど」「レバレッジ」「推測も入るけど」を一切使わない**入り方を「体で覚える」ための教材。

このファイルは **core 常駐** (`brain_wiki.py:3588` の core_files_truncated に `style/response-bank.md` が入っている) = 毎ターン必ずプロンプトに乗る。

### 4.4 武器その 3 — few-shot examples (system prompt にハードコード)

`data/brain/wiki/style/few-shot-examples-v1.json`。**base LLM の標準語回帰を遮断する最後の砦**。`CLONE_PUBLIC_PROMPT` 内に直接埋め込まれる (`brain_wiki.py:1318` の `{few_shot_examples}`)。

設計意図 (json の `purpose`):
> 海山テイスト few-shot を base LLM の system prompt にハードコード、RAG で文体模倣しきれない部分 (= base LLM の標準語回帰) を遮断する。

構造: `category_balance` (挨拶 2 / 雑談 5 / 経営判断 5 / キャリア 3 / 業務 3 / 反論 2) で網羅し、各 example は `{user, assistant, why}`。`why` が秀逸:
- ex-04 雑談「ストレス溜まった時どうしてる?」→「食べる。気がついたら太ってる。」 / why:「ニヒル自虐 + 余韻」
- ex-17 業務「CVR が低い」→「…これを『献身性の罠』といま名付けた。サッカーのフォワードが守りに参加してたら点取れないのと同じ。」 / why:「その場で名付け+サッカー比喩」

**作り方**: response-bank と alignment_trial の本人回答から、**トーン/温度/語尾/思考の運び方が際立つ 20 例**を cherry pick し、category をバランスさせる。逐語複写ではなく「型」を抽出。`{few_shot_examples}` は全 query で必ずプロンプトに流れるので、**個人情報級の content は入れない** (= ex-06 は「AI と会話」を抽象版に差し替えた漏出対策の実例)。

### 4.5 声を立てる順序 (recipe)

1. **survey を書かせる** (Step 1) → identity/style/thinking が compile される (Step 2)。
2. **alignment_trial を回す**: クローンに 100 問答えさせ、本人が HTML UI で全部レビュー・書き直し。
3. **書き直しを response-bank に蓄積** → core 常駐させる。
4. **書き直しから 20 例を few-shot に cherry pick** → system prompt にハードコード。
5. **個別 style パターンを抽出** (`style_extractor.py`) → 「なるほど多用しない」「店舗向けは横文字回避」等の単位ルールを wiki/style/* に。
6. **rerun + diff** で改善を確認 → 満足するまで 2〜5 を反復。

> この 3 点 (alignment sprint + response-bank + few-shot) が揃って初めて「本人の声」になる。1 つでも欠けると generic AI に戻る。

---

## 5. Step 4 — 品質ループ (authenticity を守り続ける)

`docs/decisions/2026-05-21-clone-quality-loops.md`。Karpathy「Slopacalypse 対策」(`docs/development_principles.md` 原則 5) の実装。思想は **「1 つの LLM が応答も採点もすると bias が固定化する → judge を別系列にする / 人間 judgment を最終 signal にする」**。

### 5.1 多層 self-eval loop

| Layer | スクリプト | 頻度 | 何をする |
|---|---|---|---|
| **L1 構造化ログ** | `bot_events.py` / `bot_metrics.py` | 常時 | 各ターンを `events.jsonl` に記録、component 別 p50/p95 latency / 失敗率 |
| **L2 夜間 regression** | `clone_style_regression.py` | 03:30 daily | 30 質問への応答を gold (response-bank) と比較。3 軸採点 (cosine / LLM-judge / style 違反 regex)。劣化 or FAIL≥5 で LINE Push |
| **L3 hallucination check** | `clone_hallucination_check.py` | 03:45 daily | 応答を atomic claim に分解し wiki と照合。supported / unsupported / **contradicted**。verify は **smart-gpt (別系列)** で self-eval loop 回避。contradicted≥3 で Push |
| **L4 第三者 blind 採点** | `clone_external_eval.py` | 月初 | 直近 30 日から 20 ターン sampling → HTML form を 5 名に配布 → 5 軸 5 段階 blind 採点 → LLM-judge との一致率を月次監視 |
| **L5 privacy 再評価** | `clone_memory_privacy_review.py` | 04:00 daily | 既存 memory を 6 観点で再採点、private 行を archive |
| **L6 prompt diff check** | (auto_deploy 経由) | deploy 時 | prompt/style 変更を検知 → pre/post diff、degraded なら即 Push |

### 5.2 synthetic-employee (自律的に弱点を炙る)

`scripts/synthetic_employee_agent.py` (04:20 daily)。**社員に扮した synthetic user が仮想環境 (非永続) でクローンを使い倒し、改善点を検知** → propose-only で queue。findability 限定 (keyword_miss の別表記を `drive_search_aliases.json` に未承認で記録、本人 `--approve` で有効化)。事実には介入しない verify-before-activate 設計。

### 5.3 drift detection (人格が古びるのを検知)

`scripts/extractors/drift_detector.py` (4 層モデルの「監視」層)。個別パターン記事の `last_*` タイムスタンプが閾値 (90/90/60/180 日) を超えたら `audit/pending_questions.md` に「これまだ合ってる?」を起票 → 本人確認 → 更新 or retire (`superseded_by` / `valid_until`)。これで Step 1 の「複製は固定化を意味しない」を運用に落とす。

### 5.4 ★ 最重要教訓 — gold question をトピックごとに足す

夜間 regression (L2) と alignment_trial は **gold question (= 採点基準となる想定質問)** で精度を測る。直近の学び:

> **クローンが扱う各トピックに対応する gold question を必ず追加せよ。**

カバーされていないトピックは「測られない」ので、そこで声が崩れても気づけない。AI/技術トピックで「素の AI に戻る」事故 (4.1) が起きたのも、当初その領域の gold question が薄かったため。新トピックをクローンに担当させたら、**同時に L2/alignment_trial にそのトピックの gold question を足す**のを習慣化する。response-bank・few-shot・questions.md は「カバー範囲の地図」でもある。

---

## 6. Step 5 — メモリー & 継続学習

### 6.1 per-user memory

`docs/decisions/2026-05-14-clone-memory.md`。`clone_memory.py`。各ユーザの会話ごとに background task で memory を増分更新 (`update_clone_memory`、fast-gpt、~$0.00003/turn)。

- 保存: `data/brain/clone_memory/<user_id>.md` (frontmatter + 4 セクション: **Profile / Ongoing Topics / Key Facts / Preferences**)。
- 次回応答時に `clone_respond_public` が memory を system prompt に注入 → 「前回の続きで言うと」「役職に合わせた粒度」が自然に出る。turn 0 (初回) は注入しない。
- プライバシー: 健康深刻 / 家族の極めて私的 / 第三者の悪口 / 性的 / PII は記録しない。L5 (5.1) が毎日再評価して private 行を archive。

### 6.2 sleep-time agent

`scripts/clone_sleep_time_agent.py` (Letta の sleep-time agent pattern)。会話が idle 30 秒になったら smart で memory を全体再整理 (debounce: 連続会話中は cancel され走らない)。応答直後の軽量 update (fast-gpt) と並行稼働 = **「会話中は軽く、一区切りで深く」**整理する二段構え。wiki 昇格候補は `clone_improve/sleep_time_drafts/` に出力。

### 6.3 auto-improve

`scripts/clone_auto_improve.py` (03:00 daily)。7 種の signal を検知して自動編集を propose。`clone_learning.py` の nightly scan が会話から発見を抽出し、応答品質 (misunderstanding / too_passive / wrong_data / too_questioning / tone_off) も自動評価 (`docs/decisions/2026-05-07-clone-public-upgrade-to-opus.md`)。翌日のダイジェストで本人が accept/reject。

> **設計原則**: 継続学習は全て **propose-only + 本人承認** が基本。クローンが自分で自分を勝手に書き換えない (Slopacalypse 対策)。本人の judgment が最終 signal。

---

## 7. 人格プロンプトの構造 (CLONE_PUBLIC_PROMPT)

### 7.1 全体構造 — 静的人格 + 動的 retrieval

`CLONE_PUBLIC_PROMPT` (`brain_wiki.py:388`〜1327) は巨大な layered prompt。組み立ては `clone_respond_public` (`brain_wiki.py:4575`)。上から:

```
┌─ 静的人格 (毎回ほぼ不変) ────────────────────────────
│ ★最上位原則: 人間らしいコメントバック (data は 2 の手)      [L391]
│ ★AI/技術トピックは本人として答える (素の AI に戻らない)     [L426] ← 2026-06-15
│ scope 暗黙前提 / データ出典ルール / 数値矛盾解消ルール        [L435-]
│ query 分類 (A 数字 / B 雑談 / C 抽象) と雑談応答 spec        [L501-]
│ ★絶対遵守: 知識ベース外の固有名詞を列挙しない (hallucination 禁止) [L551]
│ 応答モード M1/M2/M3 / セーフガード                          [L1305-]
│ 海山テイスト few-shot 20 例  {few_shot_examples}            [L1318] ← system prompt にハードコード
├─ 動的 context (毎ターン fresh) ──────────────────────
│ 今日の日付  {today}                                        [L1321]
│ 参照可能な情報 (Public Wiki + 個別メモリー)  {wiki_content} [L1326]
└──────────────────────────────────────────────────
```

`{wiki_content}` が `_read_wiki_state_public_compact` (`brain_wiki.py:3477`) の出力 = **集約 core 常駐ファイル + vector hits + clone_memory** を ~90K char に圧縮したもの (本番 caller `clone_respond_public` は `max_chars=90_000` を渡す。関数 signature default の 25K と混同しない)。つまり:
- **静的人格** = ルール + few-shot (= 4 層の「集約」を支えるプロンプト規範)。
- **動的 retrieval** = core 常駐 wiki + その query に必要な個別 wiki (vector)。

### 7.2 core 常駐ファイル (毎ターン必ず乗る人格層)

`brain_wiki.py:3553` の `core_files_truncated` が「集約層 + 声の土台」を毎ターン強制投入する。人格に直結するもの:
- `identity.md` / `style.md` / `thinking.md` (集約 3 ファイル、各 truncate 7K)
- `style/response-bank.md` (本人手書き 30 問回答)
- `style/style-response-examples.md` (フレーバー実例)
- `style/style-no-claude-proposals.md` (hallucination 根絶ルール)
- `style/style-depth-as-undercurrent.md` (深層は伏流水、直接引用 NG)
- `style/style-value-reference.md` (Value 引用ルール)
- `hobbies/index.md` 系 (嗜好マスター)

`CORE_WIKI_REGISTRY` (`brain_wiki.py:3667`) で各 core ファイルに (category, priority 1-5) を持たせ、query intent で truncate サイズを動的配分する (例: 売上 query → sales boost、嗜好 query → hobbies boost)。

> **新プロジェクトの判断**: 「毎ターン必ず読ませたい人格ファイル」を core_files として固定列挙し、「必要時だけ引きたい根拠」は vector に任せる。**声を決めるファイル (style 系 + response-bank + few-shot) は必ず core に常駐**させること。vector 任せにすると声が出ない query が生まれる。

### 7.3 cache split (静的人格を課金境界で切る)

長大な静的人格を毎ターン re-tokenize すると高コスト。`brain_wiki.py` は **3 block cache split** で対処 (`_split_prompt_for_caching_v3`、`brain_wiki.py:1367`)。Anthropic prompt caching は prefix match なので:

- **block1 (persona + few_shot)**: 不変 → 常時 cache hit。
- **block2 (今日の日付 + 安定 core wiki)**: f(query_intent 6 種, core compile ~2h 毎, 日付) → intent 別に最大 6 エントリ並存、同 intent 内は hit (~90% off)。
- **block3 (履歴 / vector / メモリー)**: 毎ターン fresh。

境界マーカー: `_CACHE_BOUNDARY_MARKER = "# 現在日時 (動的 context"` (`brain_wiki.py:1334`) と core 末尾の sentinel `_CORE_CACHE_SENTINEL` (`brain_wiki.py:1359`)。**モデルが見る連結テキストは従来と byte 完全一致** (品質不変、`tests/smoke/test_core_cache_split.py` で検証) で、変わるのは cache_control の置き場所だけ。TTL は 1h。

### 7.4 ペルソナルールはどこに書くか (= 設計の意思決定表)

新しい人格ルールを足す時、置き場所の判断:

| ルールの種類 | 置き場所 | 理由 |
|---|---|---|
| 絶対遵守の応答規範 (hallucination 禁止 / scope / 数値出典) | `CLONE_PUBLIC_PROMPT` に直書き | 最上位、全 query で効かせる |
| 「声」の型 (トーン/語尾の手本) | few-shot json + response-bank.md | base LLM の標準語回帰を遮断 |
| 観察可能な単位スタイル (「なるほど多用しない」等) | `wiki/style/style-*.md` (track 対象、手編集可) | drift 管理対象、根拠として vector でも引ける |
| 本質サマリ (価値観 / 判断傾向) | identity/thinking.md (compile 出力、**手編集禁止** → raw 経由) | 4 層の集約層、再 compile で育てる |
| 嗜好・知識 | `wiki/hobbies/`, `wiki/knowledge/` | 材料、vector retrieval |

> **黄金律**: 「声が崩れた」時は few-shot / response-bank / CLONE_PUBLIC_PROMPT の静的人格を疑う。「知識が間違った」時は core wiki / retrieval を疑う。「古い自己像で喋った」時は drift / superseded_by を疑う。

---

## 8. ゼロからの実行順序 (チェックリスト)

```
[ ] 0.  対象者の public / private 境界を設計 (clone_visibility 方針)
[ ] 1.  アラインメント survey 100 問を対象者の業種に合わせて作成 → 本人が回答
        → data/brain/raw/notes/alignment_*.md
[ ] 2.  chat 履歴を chat_import で取込 (口語多めに) → raw/conversations/
[ ] 3.  嗜好リスト (本/映画/音楽) を hobbies フローで投入
[ ] 4.  import dir watcher で会議録/メモを取込 (deterministic prefix を登録)
[ ] 5.  compile を回す → identity / style / thinking が生成される (手編集しない)
[ ] 6.  抽出器を --backfill で回す → wiki/style/* judgment/* を量産
[ ] 7.  alignment_trial を回す → 本人が HTML で全 100 問レビュー・書き直し
[ ] 8.  書き直しを response-bank に蓄積 (core 常駐) + 20 例を few-shot に cherry pick
[ ] 9.  rerun + diff で改善確認、満足するまで 7-8 を反復 → v1 公開
[ ] 10. 品質ループ (L2 regression / L3 hallucination / drift) を cron 稼働
        ★ 各トピックに gold question を足す
[ ] 11. per-user memory + sleep-time + auto-improve を稼働 (全て propose-only + 本人承認)
[ ] 12. .gitignore で compile 出力 (ignore) と手編集 wiki (track) を線引き
```

### 落とし穴トップ 5 (このプロジェクトが実際に踏んだもの)

1. **声が generic AI に戻る** — few-shot / response-bank を core 常駐させないと標準語回帰する (4.1)。
2. **compile 出力を手編集して push → 消える** — identity/style/thinking は再 compile で上書きされる (3.4)。
3. **scraper 出力を LLM compile に流して wiki 破壊** — deterministic prefix を登録し忘れた (2.2)。
4. **カバーされてないトピックで気づかず劣化** — gold question をトピックごとに足す (5.4)。
5. **古い自己像で喋る** — drift detection + superseded_by を運用しないと「2026-04 の本人像」が固定化 (1.4)。

---

> このドキュメントは方法論の地図。各 Step の実装詳細は冒頭の原典 ADR と引用したコード行を直接当たること。**最大の差別化要因は「声の固定化」(Step 3)** — ここに最も時間を使うのが、別人格クローンを成功させる鍵。
