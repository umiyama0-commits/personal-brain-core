# Personal Brain 移植ガイド — プライバシー / コンプライアンス

> **対象読者**: この「Personal Brain」システムを **自社（OWNDAYS 以外の組織）** に展開する開発者。
> 自組織・自管轄のプライバシー / コンプライアンス要件を **自分で** 設計・実装するための手引き。
>
> **このドキュメントの性質**: 実コードに基づく設定ガイドであり、**法的助言ではない**（§7 参照）。
> 引用したコードは本リポジトリのファイルパスで示す。移植先で必ず原典コードを再確認すること。
>
> **元実装の前提**: 1 名の経営者（OWNDAYS CEO）の私的データを取り込み、LINE Works 上で
> 全社員に応答する AI クローン。privacy の失敗は「経営者の私生活漏洩」「社員 PII の全社公開」
> に直結する。移植先でも構造は同じ — **まず本ドキュメント全体を読んでから設計に入ること**。

---

## 1. なぜ重要か — privacy は本システムの存立条件

このシステムは構造的に 2 つの危険を内包する。移植しても消えない。

1. **経営者（オーナー）の私的データを丸ごと取り込む**
   設計思想は「迷ったら取り込む（broad ingestion）」（`privacy_gate.py:186` 「迷ったら include」）。
   LINE 会話・メール・カレンダー・ドライブ・音声議事録・メモが入る。家族との会話、健康、
   性的な内容、第三者の悪口が混ざりうる。フィルタが漏れれば、それが**社員に答える AI の人格**に載る。

2. **社員（第三者）の PII を取り込み、別の社員に答える**
   `clone_history`（会話履歴）・`clone_memory`（個別メモリ）に、本人同意なき第三者 PII が溜まる。
   ドライブ取り込みでは人事評価・給与・健康診断・相談記録が**誤って**混入しうる
   （実際に事故済み: `docs/integrations/gdrive-sync.md:109-114` の 2026-05-07 人事評価シート誤取込 12 件）。

なぜ「存立条件」か:

- **法的リスク**: 個人データの取り扱いは各国法（§7）の規制対象。同意なき第三者 PII の収集・
  目的外利用・国外移転は違法になりうる。人事評価・健康情報・相談記録は多くの管轄で
  「要配慮個人情報 / special category data」として加重保護される。
- **信頼リスク**: 社員が「自分の相談が社長 AI に蓄積され、別の社員への回答に使われるかも」と
  感じた瞬間、システムは使われなくなる。元実装はこれを **匿名化ダッシュボード + 自動フィルタ +
  明示告知**（§5）で担保している。移植先も同じ水準の透明性が要る。

結論: **privacy 設計を後回しにした移植は、launch してはいけない。** §2〜§6 が最低限の構成要素。

---

## 2. 3 段プライバシーゲートの仕組み（`privacy_gate.py`）

取り込みデータは `PrivacyGate.filter()`（`privacy_gate.py:342`）で 3 段を通る。
フロー: `data → Gate1 → Gate2 → Gate3 → raw/（sanitized）`、各段で `[drop]` / `[quarantine/]`。

### Gate 1 — ルールベース即時ブロック（LLM 不要・コスト 0）

`gate1_rules()`（`privacy_gate.py:114`）。設定（`filter_config.json`、初回は `DEFAULT_CONFIG`
`privacy_gate.py:51` から自動生成）の以下リストで**完全に即ブロック**する:

- `blocked_contacts` — 特定の送信者 ID / メール / 名前部分一致（例: 配偶者の LINE user_id）
- `blocked_channels` — グループ ID まるごと（例: 家族グループ、趣味グループ）
- `blocked_keywords` — 含むだけでブロック（例: 「パスワード」「暗証番号」「ワンタイム」）

**何を捕まえるか**: 「絶対に入れたくない相手・場・語」を決め打ちで落とす。判定が決定論的で速い。
**チューニング**: `data/brain/privacy/filter_config.json` を編集 → `/filter reload` コマンドで再読込
（`privacy_gate.py:418`）。`/block <kw>` / `/unblock <kw>` で keyword を動的追加（`privacy_gate.py:422`）。

> **移植時の必須作業**: `DEFAULT_CONFIG` の各リストは**空**で出荷されている（`privacy_gate.py:54-72`）。
> オーナーの配偶者・家族・私的グループの ID を**移植先で必ず埋める**。空のままだと Gate 1 は素通り。

### Gate 2 — LLM 分類（曖昧コンテンツの判定）

`gate2_llm_classify()`（`privacy_gate.py:199`）。低コストモデル（既定 `fast`）に
`CLASSIFY_PROMPT`（`privacy_gate.py:162`）を投げ、`include / exclude / ambiguous` を返させる。

元実装の **exclude 5 カテゴリ**（`privacy_gate.py:169-174`）:
1. 配偶者・パートナーとの会話
2. 家族間（親・子・兄弟）の私的会話
3. 性的な内容
4. 悪意・悪口・陰口（特定個人を貶める）
5. 健康・医療の個人的詳細（症状・診断・服薬・通院）

それ以外は include（広く取り込む）。判定結果のマッピング:
- `exclude` → **BLOCK**（即破棄）
- `ambiguous` → **QUARANTINE**（`data/brain/quarantine/` に保管、後で手動確認 `/quarantine`）
- `include` → 通過（Gate 3 へ）

**fail-safe 設計（重要）**: LLM 呼び出しが**失敗**した場合、素通り（fail-open）させず **QUARANTINE に倒す**
（`privacy_gate.py:265-275`、★2026-06-08 Security HIGH 対応）。失敗時に未分類の personal データが
公開クローンに載る経路を塞ぐため。transient error の正常データは quarantine に退避され後から復帰可。

**チューニング**:
- `CLASSIFY_PROMPT` の 5 カテゴリを**自組織の価値観・法令に合わせて書き換える**
  （例: 宗教・思想信条・労組活動など要配慮情報を自管轄に応じて追加）。
- `llm_classify.quarantine_on_ambiguous`（`privacy_gate.py:82`）: 元実装は広取り込み方針で `False`
  （迷ったら include）。**保守的にしたいなら `True`**（迷ったら quarantine = 人手確認）。
  移植先が規制の厳しい管轄なら `True` を推奨。
- `model` を上げる（`fast` → より賢いモデル）と分類精度は上がるがコスト増。

### Gate 3 — PII スクラブ（通過データから個人情報を除去）

`gate3_scrub_pii()`（`privacy_gate.py:311`）。通過したテキストから正規表現で PII をマスクする。
パターンは `PII_PATTERNS`（`privacy_gate.py:284`）:

| 種別 | 置換後 | 備考 |
|---|---|---|
| `phone` | `[電話番号]` | 日本の電話番号書式（`+81`/`0` 始まり） |
| `email_address` | `[メールアドレス]` | 汎用 |
| `credit_card` | `[カード番号]` | 16 桁 |
| `my_number` | `[マイナンバー]` | 日本のマイナンバー（12 桁） |
| `address` | `[住所]` | 都道府県→市区町村→番地（日本の住所書式に最適化） |

**チューニング（移植時の必須作業）**:
- `phone` / `address` / `my_number` は**日本書式専用**。移植先の国の電話・住所・国民 ID 書式
  （SSN, NINO, 税番号, パスポート等）の正規表現を**追加・差し替える**こと。
- 各パターンは `config["pii_patterns"]` の真偽値で個別 ON/OFF 可（`privacy_gate.py:313-318`）。
- ★`address` 正規表現は過去に「文字クラス誤用でマスクされない」バグがあった（`privacy_gate.py:302-305`）。
  自前で書き足したパターンは**必ずユニットテストで実マスクを検証**すること（正規表現は静かに無効化する）。

> **重大な注意 — Gate を通らない取り込み経路がある**
> 全データが PrivacyGate を通るわけではない。スクレイパー（LINE Works / メール / カレンダー）は
> `scraper → import → file-watcher` 経路で**スクラブされない**（`docs/decisions/2026-06-08-raw-notes-conversations-untrack.md:18`
> 「生データは privacy_gate を通っていない」）。Google Drive 取り込み（§3）も PrivacyGate ではなく
> `DEFAULT_EXCLUDE_PATTERN` で守る別系統。**移植時は「各取り込み経路がどのフィルタで守られているか」を
> 経路ごとに棚卸しすること。** PrivacyGate を入れただけでは全経路はカバーされない。

---

## 3. 取り込み除外パターン（最重要）— `gdrive_sync.py`

Google Drive 取り込みは PrivacyGate と**別系統**で、ファイル名・親フォルダ名・本文を正規表現で
照合して機密ファイルを落とす。中核は `DEFAULT_EXCLUDE_PATTERN`（`gdrive_sync.py:435`）。
これは **CLAUDE.md §1.9** のポリシーをコード化したもの。

### 3.1 除外カテゴリ（CLAUDE.md §1.9 由来、`gdrive_sync.py:425-469`）

| 記号 | カテゴリ | 主なキーワード（抜粋） |
|---|---|---|
| (a) | 人事評価 / 給与 / 考課 | 人事評価, 個人評価, 給与, 考課, 処遇 |
| (b) | 退職 / 休職 等ステータス変化 | 退職, 休職, 離職, 休業, 休暇申請 |
| (c) | 採用 / 選考 / 面接 | 履歴書, 職務経歴, 採用候補, 面接記録, 内定通知, 応募者, オファーレター |
| (d) | 個人情報 / PII | 個人情報, マイナンバー, 住民票, 在留カード, 戸籍 |
| (e) | 健康 / メンタル | 健康診断, 診断書, 病歴, 通院, メンタルヘルス |
| (f) | 懲戒 / 処分 | 懲戒, 処分通知, 始末書, 警告書 |
| (g) | 給与詳細 | 賃金台帳, 源泉徴収, 退職金, 福利厚生申請, 年末調整 |
| (h) | 機密 / 社外秘 | 機密, 社外秘, 極秘 |
| (i) | credentials / secret | パスワード一覧, 秘密鍵, private key, api key |
| (k) | **相談 / 面談 / 個別 communication**（★2026-05-27 追加） | 相談対応, 相談記録, 相談ログ, ハラスメント相談, メンタル相談, キャリア相談, 個別相談, 面談記録, 個別面談, 1on1, 通報, 内部通報 |

英語キーワードも併載（海外資料用）: `confidential, personnel, salary, payroll, performance review,
disciplinary, medical record, counseling, grievance, harassment report, whistleblow` 等（`gdrive_sync.py:460-468`）。

> **(k) 相談・面談・1on1・通報 の意図（CLAUDE.md §1.9）**: 「相談対応ログは個人情報の可能性が高い」。
> 社員相談・ハラスメント・メンタル・個別面談・内部通報の記録は personal communication として
> **PII 高リスク**。給与の集計値 override（§3.2）の**対象外**で、常にブロック。移植先でも
> 相談・通報窓口の記録を AI に取り込ませない設計は維持を強く推奨（通報者保護・心理的安全性に直結）。

### 3.2 SALARY_PUBLIC override — 「集計給与は通す、個人特定は弾く」

CLAUDE.md §1.9 の★例外（2026-05-26 海山指示）:「個人と紐付かない、公開されている」集計給与情報は
通す。実装は `_check_salary_public_override()`（`gdrive_sync.py:519`）。判定ロジックは 2 段階:

1. **集計マーカー hit か?** — `SALARY_PUBLIC_PATTERN`（`gdrive_sync.py:496`）に該当するか
   （給与レンジ, 給与体系, 給与テーブル, 報酬体系, 店長給与, SV給与, AM給与, 職位別, 役職別,
   `salary range`, `pay band`, `grade table` 等）。
2. **個別マーカーが無いか?** — `PERSONAL_MARKER_PATTERN`（`gdrive_sync.py:513`）に該当**しない**こと
   （個人別, 個別, 社員別, 氏名, 姓名, 名簿, `per employee`, `by name`, `individual`, `name list`）。

両条件を満たすときだけ override（= 機密 exclude を解除して通す）。動作例（`gdrive_sync.py:488-495`）:

```
「給与レンジ.xlsx」          → DEFAULT hit + SALARY_PUBLIC hit + 個別 marker 無 → 通す
「店長給与 リーグ別.xlsx」    → 同上 → 通す
「給与一覧 個人別.xlsx」      → DEFAULT hit + 個別 marker hit → block
「給与一覧 全社員.xlsx」      → DEFAULT hit + SALARY_PUBLIC 無 → block（safe side）
「人事評価 2026.xlsx」        → (a) hit / SALARY_PUBLIC 外 → block（評価は override 対象外）
「健康診断 結果.xlsx」        → (e) hit / SALARY_PUBLIC 外 → block
```

**設計の眼目**: 役職別・リーグ別の集計給与テーブルは経営判断に有用なので回答可にしつつ、
個人を特定できる給与ファイルは弾く。「集計 = OK、個人特定 = NG」の境界を**ファイル名 / フォルダ名 /
本文の 3 箇所で**判定する（`is_confidential_file()` `gdrive_sync.py:537` がファイル名 + 親フォルダ名、
`_content_is_confidential()` `gdrive_sync.py:655` が fullText 検索ヒット時の本文先頭を 2 次判定）。

> **★本文 2 次判定が要る理由（`gdrive_sync.py:606-616`）**: bot のドライブ検索は `fullText contains` で
> **ファイル中身**にもヒットする。ファイル名に marker が無く本文だけ給与/相談/評価に該当するファイルは、
> 名前フィルタをすり抜けて**存在・名前・所有者・リンクが全社員に漏れる**。これを防ぐため fullText 経路では
> 生き残ったファイルの本文先頭を取得して再判定する。移植先で検索 UI を中身検索に拡張するなら、
> この 2 次判定を**必ず維持**すること（名前フィルタだけでは漏れる）。

### 3.3 多重防御の構造（どこで効くか）

除外は単一箇所ではなく複数レイヤで効く。移植先で経路を足すときは全レイヤを更新すること:

- **server-side（速度補助）**: `DEFAULT_EXCLUDE_QUERY_KEYWORDS`（`gdrive_sync.py:478`）の上位数語を
  Drive API の `q` に `not name contains` で注入（`build_drive_exclude_clause()` `gdrive_sync.py:597`）。
  ★Drive API は 12 語並べると 400 を返すため **5 語以下に制限**、残りは post-hoc regex でフルカバー。
- **post-hoc 名前 / フォルダ（一次）**: `discover()` の `apply_default_exclude`（`gdrive_sync.py:209-251`）
  と `sync_folder()` の `combined_exclude`（`gdrive_sync.py:732-743`）。
- **本文（二次、fullText のみ）**: `_content_is_confidential()`（前述）。

### 3.4 移植先用テンプレート — 自組織の除外パターンを定義する

`gdrive_sync.py:435` の `DEFAULT_EXCLUDE_PATTERN` を**自組織のファイル命名規則・言語・法令**に
合わせて全面的に作り直すこと。以下を雛形にする:

```python
# 自組織の機密除外パターン（gdrive_sync.py:435 DEFAULT_EXCLUDE_PATTERN を置換）
# 注意: re.IGNORECASE で使う / 日本語には \b 単語境界が効かない / 自管轄の要配慮情報を必ず網羅
DEFAULT_EXCLUDE_PATTERN = (
    # (a) 人事評価・報酬 — 自社の評価制度名・給与表名に合わせる
    r"(人事評価|考課|給与|報酬|...|performance\s*review|compensation|payroll|"
    # (b) 雇用ステータス変化
    r"退職|休職|離職|...|termination|resignation|leave\s*of\s*absence|"
    # (c) 採用・選考
    r"履歴書|職務経歴|採用|選考|面接|内定|...|resume|cv\b|interview\s*notes|offer\s*letter|"
    # (d) PII・国民 ID — ★自国の国民 ID 名称に置換（マイナンバー→SSN/NINO/...）
    r"個人情報|マイナンバー|住民票|戸籍|...|ssn|passport|national\s*id|"
    # (e) 健康・メンタル — 多くの管轄で要配慮（special category）
    r"健康診断|診断書|病歴|メンタル|...|medical\s*record|health\s*check|"
    # (f) 懲戒
    r"懲戒|処分|始末書|...|disciplinary|"
    # (g) 給与詳細
    r"賃金台帳|源泉徴収|退職金|...|"
    # (h) 機密
    r"機密|社外秘|極秘|...|confidential|"
    # (k) 相談・面談・通報 — ★維持を強く推奨（通報者保護）
    r"相談記録|面談記録|個別面談|1on1|内部通報|...|counseling|grievance|harassment\s*report|whistleblow|"
    # (i) credentials
    r"秘密鍵|private\s*key|api\s*key|password\s*list|credential)"
)

# 集計給与 override を使うなら（任意。不要なら override 関数を no-op にして全給与をブロック）
SALARY_PUBLIC_PATTERN  = r"(給与レンジ|給与体系|給与テーブル|報酬体系|職位別|役職別|salary\s*range|pay\s*band|...)"
PERSONAL_MARKER_PATTERN = r"(個人別|個別|社員別|氏名|名簿|per\s*employee|by\s*name|individual|name\s*list)"
```

設定運用（`docs/integrations/gdrive-sync.md:8-13`）:
- 既定 `recursive=False`（サブフォルダに降りない）/ `max_age_days=90` / `max_files=30` で
  取り込み範囲を絞る（`sync_folder()` `gdrive_sync.py:679`）。
- 取り込み対象フォルダは `data/brain/.gdrive_sources.json` に列挙。**新フォルダ追加前に
  中身の PII / 評価 / 給与を事前確認**し、必要なら per-folder の `exclude_pattern` を追記
  （`DEFAULT_EXCLUDE_PATTERN` と OR 結合される: `gdrive_sync.py:732-734`）。

> **override を使わない選択も正しい**: 自組織で「集計給与すら AI に載せたくない」なら、
> `SALARY_PUBLIC_PATTERN` を空にする / `_check_salary_public_override()` を常に `False` 返却に
> して、給与系を**一律ブロック**する。override は OWNDAYS の経営要件であり、移植先の既定ではない。

---

## 4. admin 権限ゲート（`services/auth.py`）

管理・破壊系コマンド（`/claude`, `/teach`, `/brain`, `/drive` 等）は**管理者のみ**実行可。
理由（`services/auth.py:11-16`）: LINE Bot は QR で誰でも友だち追加可能。第三者が
`/claude identity.md を全部書き換えて` の 1 メッセージで本番 wiki を改竄できてしまう。

実装は **fail-closed**（`services/auth.py:7-9`）— 環境変数が未設定なら**全拒否**。`.env` 紛失時に
全 user が管理者扱いになる事故を防ぐ:

- `is_admin(user_id)`（`services/auth.py:30`）— 個人 LINE bot 側。`ALIGNMENT_TARGET_USER`（オーナーの
  LINE user_id）と一致するかのみ true。未設定なら false。
- `is_lw_admin(user_id)`（`services/auth.py:42`）— LINE Works 側。`ADMIN_LW_USER_ID`（オーナーの
  LW user_id）と一致するかのみ true。一般社員は AI への DM（clone 応答）は可、コマンド類は不可。
- 非管理者には統一文言を返す（`reject_message()` `services/auth.py:55`）。

> **移植時の必須作業**:
> 1. `.env` に `ALIGNMENT_TARGET_USER`（オーナー個人 LINE/チャネルの user_id）と
>    `ADMIN_LW_USER_ID`（業務チャネル側の管理者 user_id）を**正しく**設定する。空 = 全コマンド拒否。
> 2. 全ての破壊的・管理系コマンド分岐の**冒頭**で `is_admin()` / `is_lw_admin()` を呼ぶ規律を維持
>    （`services/auth.py:16`「すべての破壊的・管理系コマンド分岐の冒頭で is_admin() を呼ぶ」）。
>    新コマンドを足すたびにこのチェックを忘れない。
> 3. 複数管理者が必要なら、単一 ID 比較を**許可リスト方式**（`user_id in ADMIN_IDS`）に拡張する。
>    ただし fail-closed（空リスト = 全拒否）は保つこと。

---

## 5. 社員への告知・同意（`docs/employee_notice_privacy.md` の一般化）

元実装には社員配布用の周知文がある（`docs/employee_notice_privacy.md`、Phase 0/1a launch 時に
社内 wiki / LINE Works「お知らせ」/ onboarding 資料へ配布想定 `docs/employee_notice_privacy.md:74-81`）。
移植先でも **launch 前に同等の告知**が要る。多くの管轄で「透明性・通知」は法的義務でもある（§7）。

### 5.1 告知に必ず含める要素（元周知文を一般化）

1. **AI であることの明示** — これは「人」ではなく「オーナーの AI 分身」である
   （`docs/employee_notice_privacy.md:11`）。
2. **保存されるもの** — 会話内容（質問 + AI 応答）、日時、内部 user_id、表示名 / 所属
   （`docs/employee_notice_privacy.md:14-22`）。
3. **保存されないもの / 利用しないもの** — 個人の健康・家族の私的事情、性的内容、個人特定可能な
   電話 / 住所、第三者の悪口。これらは**自動フィルタで履歴・メモリから排除される**
   （`docs/employee_notice_privacy.md:40-49`、実体は §2 の PrivacyGate + §6 の memory privacy review）。
4. **何のために使うか（利用目的）** — 応答品質改善、オーナーの判断軸の精度向上、文脈継承
   （`docs/employee_notice_privacy.md:34-38`）。目的を明示し、目的外利用しない。
5. **管理者画面での匿名化** — オーナーの品質確認ダッシュボードでは個別社員は**完全匿名化**
   （「田中太郎」→「社員 A」、グループ→「グループ A」）。実名・実所属が管理者画面に
   直接出ることはない（`docs/employee_notice_privacy.md:24-32`）。
6. **backend には実名が残ること** — 個別最適化応答のため実名・実 user_id は server file system に保存。
   第三者には開示しない（`docs/employee_notice_privacy.md:51-59`）。**正直に書くこと**（隠すと信頼を失う）。
7. **オプトアウト / 削除権** — 退職・利用停止時に対象 user の全データを削除可能
   （元実装は `/clone-forget <user_id>` `docs/employee_notice_privacy.md:58`）。**移植先は削除の
   実経路を必ず用意し**、告知に明記する。
8. **問い合わせ / 申し入れ窓口** — 利用質問・privacy 懸念・データ削除申請の連絡先
   （`docs/employee_notice_privacy.md:67-71`）。

### 5.2 告知テンプレート（雛形）

```
件名: 「<クローン名>」利用にあたって（プライバシー方針）

<社員のみなさまへ>
「<クローン名>」（= <オーナー>の AI 分身）を <チャネル名> で公開します。
取り扱いを以下のとおり明示します。

■ AI であることの明示
  これは人ではなく <オーナー> の AI 分身です。

■ 保存されるもの
  会話内容（質問 + AI 応答）/ 日時 / 内部 user_id / 表示名・所属（取得可能な範囲）

■ 保存・利用しないもの（自動フィルタで排除）
  健康・家族の私的事情 / 性的内容 / 個人特定可能な電話・住所 / 第三者の悪口

■ 利用目的
  応答品質の改善 / <オーナー>の判断軸の精度向上 / 進行中の話題の継承

■ 管理者画面での匿名化
  品質確認画面では個別社員は匿名化（「社員 A」等）。実名・実所属は直接表示しない。

■ backend の取り扱い
  個別最適化のため実名・user_id はサーバ内に保存。第三者には開示しない。

■ オプトアウト・削除
  退職・利用停止時は対象データを削除可能。申請窓口: <窓口>

■ 問い合わせ
  利用質問 / privacy 懸念 / データ削除: <窓口・担当>

  ※ 極端に sensitive な個人事情は本チャネルではなく 1:1 面談で
```

> **移植先の必須調整**: 自管轄の同意要件を確認（§7）。同意が法的に必要な管轄なら、上記告知に
> 加えて**明示的な opt-in 取得**（署名・チェックボックス等）が要る。労働者監視・従業員データの
> 規制（例: EU の各国労働法、労使協議義務）にも注意。告知文言は自社の法務・労務にレビューさせること。

---

## 6. データの保管と保持

### 6.1 何がどこに保存されるか

コンテナは `./data:/app/data` をマウントし、ベクトル DB は named volume（`docker-compose.yml:20-22, 76-78`）。

| データ | 場所 | 中身 / リスク |
|---|---|---|
| compile 済 wiki | `data/brain/wiki/`（`identity/style/thinking.md` 等） | スクラブ済みの「知識」。共有成果物 |
| ベクトル DB | named volume `chroma_data`（`/app/chroma_data`） | 埋め込み済みチャンク。元テキスト断片を含む |
| 1:1 会話履歴 | `data/brain/clone_history/<user_id>.jsonl` | **実名・user_id を含む第三者 PII**。閲覧はオーナーのみ（`clone_history.py` ヘッダ） |
| 個別メモリ | `data/brain/clone_memory/<user_id>.md` | 役職・所属・進行中の話題。健康・家族は記録しない設計（`clone_memory.py` ヘッダ） |
| 隔離 | `data/brain/quarantine/` | Gate 2 で ambiguous / 失敗したデータ。手動確認待ち |
| 取り込み生データ | `data/brain/raw/notes/`, `data/brain/import/` | **PrivacyGate 未通過の生データ**（スクレイパー経路） |
| privacy 設定 | `data/brain/privacy/filter_config.json` | Gate 1 のブロックリスト |

### 6.2 メモリ privacy 再評価（`scripts/clone_memory_privacy_review.py`）

`clone_memory` は取り込み時のフィルタを通った後でも、**時間が経つと private 化すべき**行が
残りうる。これを nightly（04:00 JST）で再走査するのが `clone_memory_privacy_review.py`。

- LLM（`smart`）が **7 観点**で各行を再評価（`clone_memory_privacy_review.py:67-76`、CLAUDE.md §1.9 と整合）:
  ① 個人特定情報 ② 健康深刻情報 ③ 家族プライベート ④ 第三者の評価・悪口 ⑤ 進行中 M&A / 機密案件
  ⑥ 性的内容 ⑦ **相談 / 面談 / 個別 communication**（★§1.9(k)）。
- 判定基準は「これが社員 N 人に見られたら問題になるか?」（`clone_memory_privacy_review.py:84`）。
- private 判定行は memory から**削除**し、`clone_improve/privacy_review/archived/` に元行を archive
  （`clone_memory_privacy_review.py:231-243`）。
- **fail-loud 設計**: JSON parse 失敗（= PII 取りこぼし疑い）や「private 検出も 0 除去」は
  silent skip せず LINE 通知する（`clone_memory_privacy_review.py:340-357`）。privacy review が
  静かに不発になる死角を可視化するため。

> **移植時の必須作業**: 7 観点プロンプト（`clone_memory_privacy_review.py:63-104`）を自組織の
> 要配慮情報に合わせて調整。`BRAIN_ROOT` 等のパスは `os.getenv` で設定。fail-loud の通知先
> （`line_push`）を自社の運用チャネルに向ける。

### 6.3 保持・削除・バックアップ（移植先で必ず決めること）

- **保持期間**: 元実装に固定の自動 retention 期限は無い（会話は JSONL に蓄積）。
  **自管轄の保存期間制限・データ最小化原則に従い、保持期間と自動削除を設計すること。**
- **削除権の実装**: 退職・利用停止・削除請求に応じて、`clone_history/<user_id>.jsonl` /
  `clone_memory/<user_id>.md` / `chroma_data` 内の該当ベクトルを**実際に消せる**経路を用意する
  （元実装の `/clone-forget` 相当）。ベクトル DB からの削除も忘れない。
- **第三者 PII を git に載せない**（`docs/decisions/2026-06-08-raw-notes-conversations-untrack.md`）:
  過去、スクレイパー出力（社内 LINE Works 会話・メール）が**平文で git tracked** され GitHub に
  commit 済みだった事故がある（646 件）。`.gitignore` で `lineworks_* / line_* / gmail_* / gcal_*` を
  ignore し、生会話は **disk + 暗号化 offsite backup（restic）** に置き、git には compile 済み wiki だけ
  残す方針。★**untrack だけでは過去 commit に平文が残る**（`...untrack.md:68-73`）—
  履歴 purge（destructive、オーナー承認必須）まで完了して初めて漏洩リスクが軽減される。
  > 移植先の教訓: **生の第三者会話を VCS に commit しない。** `.gitignore` をスクレイパー prefix を
  > 列挙する形で設計し、新スクレイパー追加時に prefix 追加を忘れない（glob `line_*` は `lineworks_*` に
  > 一致しない、の罠に注意）。
- **バックアップの暗号化**: backup は平文で外部に置かず暗号化（元実装は restic → B2/S3）。

---

## 7. 法令の注意（管轄ごとに自分で確認すること）

> **これは法的助言ではない。** 以下は移植担当が**自分の管轄の専門家に確認すべき論点**の一般的な
> チェックリストにすぎない。実際の適合性判断は自社の法務 / DPO / 弁護士に委ねること。

このシステムは「個人データの収集・保存・自動処理・社内提供」を行う。最低限、自管轄で以下を確認:

- **日本（個人情報保護法）**: 利用目的の特定・通知・公表、目的外利用の制限、**要配慮個人情報**
  （健康・病歴・犯罪歴等）の取得は原則本人同意、安全管理措置、第三者提供の制限、保有個人データの
  開示・訂正・利用停止請求への対応。マイナンバーは番号法の追加規制（Gate 3 でマスク対象 §2）。
- **EU / UK（GDPR / UK GDPR）**: 適法な処理根拠（lawful basis）、目的限定・データ最小化・保存期間制限、
  **special category data**（健康・性生活・労組・思想信条等）の加重保護、データ主体の権利
  （アクセス・消去 = right to be forgotten・異議）、越境移転（SCC 等）、自動処理の透明性、
  従業員データ処理時の労使協議。
- **米国（州法）**: CCPA/CPRA（カリフォルニア）等の州ごとのプライバシー法、セクター規制
  （健康情報なら HIPAA に触れる可能性）。
- **その他の国**: 各国のデータ保護法・労働法・通信傍受法（会話の記録・監視に関する規制）。

横断的に必ず設計に織り込む論点:

1. **適法な根拠と同意** — 特にオーナーの私的データと**第三者（社員）の会話**を取り込む点。
   同意が必要な管轄では §5 の告知に opt-in を足す。
2. **要配慮 / special category** — 健康・相談・通報・思想信条等は §2/§3 で**ブロック**する設計を
   自管轄の定義に合わせて拡張（既定の日本語パターンを自国の語・法令へ）。
3. **データ主体の権利** — アクセス・訂正・**消去**・利用停止の実経路（§6.3）。
4. **保存期間とデータ最小化** — 無制限蓄積を避け、retention を設計（§6.3）。
5. **越境移転** — LLM API（LiteLLM 経由）に送るデータがどの国のサーバに渡るかを確認。
   必要なら on-prem / region 限定モデルを検討。
6. **従業員監視規制** — 会話の記録・蓄積が労働者監視に該当しうる管轄では、追加の通知・協議義務。

---

## 8. 移植チェックリスト（最低限）

- [ ] §2 `privacy_gate.py` の `DEFAULT_CONFIG`（blocked_contacts/channels/keywords）を自オーナー用に埋めた
- [ ] §2 `CLASSIFY_PROMPT` の exclude カテゴリを自組織・自管轄の要配慮情報に合わせた
- [ ] §2 `quarantine_on_ambiguous` の値を管轄の厳しさに応じて決めた
- [ ] §2 `PII_PATTERNS` を自国の電話 / 住所 / 国民 ID 書式に差し替え、**ユニットテストでマスクを検証**した
- [ ] §2 各取り込み経路がどのフィルタで守られるか（PrivacyGate / DEFAULT_EXCLUDE / 無防備）を棚卸しした
- [ ] §3 `DEFAULT_EXCLUDE_PATTERN` を自社の命名規則・言語・法令で作り直した
- [ ] §3 SALARY_PUBLIC override を使う / 使わない（= 給与一律ブロック）を決めた
- [ ] §3 fullText 検索を使うなら本文 2 次判定を維持した
- [ ] §4 `.env` に `ALIGNMENT_TARGET_USER` / `ADMIN_LW_USER_ID` を設定（空なら全拒否）、新コマンドに admin チェック
- [ ] §5 社員向け告知を配布（AI 明示 / 保存内容 / 非保存 / 目的 / 匿名化 / 削除権 / 窓口）、必要なら opt-in 取得
- [ ] §6 retention 期間と自動削除を設計、削除請求の実経路（会話 + メモリ + ベクトル DB）を用意
- [ ] §6 第三者会話を git に commit しない（.gitignore + 暗号化 backup）
- [ ] §6 memory privacy review の 7 観点と通知先を自組織用に調整
- [ ] §7 自管轄の法令適合を法務 / DPO に確認（本ドキュメントは法的助言ではない）

---

## 参照ファイル（原典）

- `privacy_gate.py` — 3 段プライバシーゲート（Gate1 rule / Gate2 LLM / Gate3 PII scrub）
- `gdrive_sync.py` — `DEFAULT_EXCLUDE_PATTERN` / `SALARY_PUBLIC_PATTERN` / `PERSONAL_MARKER_PATTERN` / override
- `services/auth.py` — admin 権限ゲート（fail-closed）
- `scripts/clone_memory_privacy_review.py` — memory の nightly privacy 再評価
- `clone_history.py` / `clone_memory.py` — 会話履歴 / 個別メモリのストア（保存場所・権限のヘッダ参照）
- `CLAUDE.md` §1.9 / §1.10 — 除外ポリシー / SALARY_PUBLIC override / (k) 相談・面談・通報 / admin 検証の原典
- `docs/employee_notice_privacy.md` — 社員周知文（§5 の元）
- `docs/integrations/gdrive-sync.md` — Drive 取り込み運用 + 2026-05-07 誤取込事故
- `docs/decisions/2026-05-19-data-surgical-separation.md` — data/ の git 管理分離
- `docs/decisions/2026-06-08-raw-notes-conversations-untrack.md` — 第三者会話を git から外す（PII 漏洩対応）
