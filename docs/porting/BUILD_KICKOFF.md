# BUILD_KICKOFF — AI コーディングエージェント向け ビルド指示書

> **このファイルの使い方**: このリポジトリ(または複製)を Claude Code / Codex 等で開き、エージェントに本書を読ませれば着工できる。エージェントはこれを「ミッション仕様」として扱い、フェーズ順に進め、人間の入力が要る所で止まって聞く。
> **人間(導入する開発者)へ**: 本書の内容をあなたの新プロジェクトの `CLAUDE.md` / `AGENTS.md` にコピーすれば、エージェントが自動で拾う。

---

## 0. あなた(エージェント)のミッション

このリポジトリは **「Personal Brain」= 経営者の AI 分身 + 業務 retrieval + 自己複製基盤** の**動く参照実装**である。あなたのタスクは、これを土台に **別の経営者(以下 TARGET)・別の会社向けに作り替えて稼働させる**こと。

- ゼロから書くのではなく、**この実装を理解 → 汎用部分は再利用 → 固有部分(人格・業務データ)を TARGET のものに差し替え**る。
- 設計の正本: `docs/porting/PORTING_GUIDE.md`(全体索引)+ 同フォルダの 4 本。
- システム概念(非技術者向け): `docs/porting/00_CONCEPT_DECK_FOR_CEO.md`。

**最初にやること**: `docs/porting/PORTING_GUIDE.md` と `docs/porting/GENERIC_VS_SPECIFIC.md` を読み、その後 §1 Phase 0 の質問を人間にする。

---

## 1. ビルド計画(フェーズ順 / 各フェーズに完了条件 DoD)

### Phase 0 — 入力をそろえる(人間に聞く)
着工前に人間へ確認(揃うまで先に進まない):
- **TARGET は誰か**(クローン対象の経営者)。その人の**データ提供と協力の同意**は取れているか
- **TARGET の人格データ**の在りか(メモ/会議録/チャット/過去の判断例/嗜好)
- **会社の業務データ源**(売上等を何から取るか)
- **アカウント**(`SETUP_FROM_ZERO.md` §2 のチェックリストを提示し、誰が用意するか確認)
- **言語 / 規模 / デプロイ環境 / プライバシー方針(管轄法)**
**DoD**: 上記が文書で確定。

### Phase 1 — 素のシステムを起動
`docs/porting/SETUP_FROM_ZERO.md` に従う。
**DoD**: `docker compose up` 後、`/health` が ok、bot が空 wiki で 1:1 疎通する。

### Phase 2 — 汎用と固有を切り分け、固有を差し替え
`docs/porting/GENERIC_VS_SPECIFIC.md` の分類表 + §5 の grep ターゲットを実行。
**DoD**: 「海山 / OWNDAYS / うみやまAI」リテラル・「日本」default scope・admin id・業務 scraper・組織図・店舗マスターが TARGET のものに置換され、grep で固有リテラルが残っていない。

### Phase 3 — 人格・知識データを取り込み compile
`docs/porting/PERSONA_BOOTSTRAP.md` の Step 1〜2。raw 取込 → Karpathy 式 LLM compile → `identity / style / thinking`(人格)+ `knowledge`(会社)wiki を生成。
**DoD**: wiki が compile され、retrieval が TARGET の事実を返す(`brain_search` 相当でヒット)。

### Phase 4 — 「声」を固める(最重要・手を抜かない)
`PERSONA_BOOTSTRAP.md` の Step 3。alignment(想定質問への TARGET 本人回答)+ response-bank の本人手書き + few-shot を整備。
**DoD**: 代表質問数本で、**汎用 AI の喋りでなく TARGET の声**で返る(人間が確認)。**ここを省略しない**(省くと“素の親切な AI アシスタント”に戻る、`brain_wiki.py:426` の教訓)。

### Phase 5 — 品質ループ + プライバシー
`PRIVACY_COMPLIANCE.md` で除外パターン・同意・admin ゲートを TARGET の組織/管轄に設計。品質ループ(夜間 regression / hallucination / synthetic)を稼働。
**DoD**: 夜間ループが回り、**TARGET の業務トピックの gold 質問が regression に入っている**(劣化検知の生命線)。除外パターンが管轄法に適合。

### Phase 6 — 運用化
`docs/runbook.md` で cron / 監視 / 復旧を整備。
**DoD**: cron 登録、外部死活監視(系外から)、復旧手順が機能。

---

## 2. 守るべきガードレール(過去の高くついた事故。`docs/failure-log.md` 参照)

- **Docker source は image に baked-in** → コード変更後は `build + force-recreate`(restart だけでは古いまま)
- **chromadb 並行アクセス禁止** — bot 稼働中に reindex を回さない(SIGSEGV / index 破損)
- **Docker VM RAM はデフォルト放置しない**(≥16GiB。常駐肥大で VM スラッシュ → デーモン wedge → 全断)
- **compile 出力(identity/style/thinking)を手編集して push しない**(data 外科的分離)
- **取込経路は必ず PrivacyGate を通す**(scraper→import の素通りは PII 漏れリスク)
- **gold 質問の無い人格パスは劣化に気づけない**(必ず追加)
- **秘密情報は `os.getenv()` 経由のみ**、平文直書き禁止
- **重要判断(人格 prompt / retrieval / 破壊的操作)は cross-check してから本番**

---

## 3. 人間に聞くべきタイミング(勝手に進めない)
- TARGET の**実データ**(人格・声の素)が必要なとき
- **秘密情報 / アカウント**の発行が必要なとき
- TARGET の**判断・声のニュアンス**が分からないとき(でっち上げない)
- **プライバシー / 管轄法**の設計判断
- **破壊的操作**(データ wipe 等)

---

## 4. 全体の Definition of Done
- 素のシステムが TARGET の環境で稼働(Phase1)
- 固有リテラルゼロ(Phase2)
- TARGET の人格・知識で retrieval が効く(Phase3)
- 代表質問が **TARGET の声**で返る(Phase4)
- 夜間品質ループ + gold 質問 + 除外パターンが稼働(Phase5)
- cron / 監視 / 復旧が運用化(Phase6)

---

## 5. 参照(読む順)
1. `docs/porting/PORTING_GUIDE.md`(全体索引・アーキ図・最初に決めること)
2. `docs/porting/GENERIC_VS_SPECIFIC.md`(何を残し何を差し替えるか)
3. `docs/porting/SETUP_FROM_ZERO.md`(ゼロ構築)
4. `docs/porting/PERSONA_BOOTSTRAP.md`(人格をゼロデータから立てる)
5. `docs/porting/PRIVACY_COMPLIANCE.md`(プライバシー設計)
6. `docs/review/ARCHITECTURE.md` / `docs/decisions/`(深い設計理由)/ `docs/failure-log.md`(事故と学び)

---

**着工**: まず §5 の 1, 2 を読み、§1 Phase 0 の質問を人間に投げよ。
