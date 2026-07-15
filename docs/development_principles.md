# Personal Brain 開発原則 (詳細版)

★ 2026-05-22 作成。Andrej Karpathy の CLAUDE.md File (2026-05 公開) を元に、
Personal Brain の文脈で適用した原則。

CLAUDE.md 冒頭にも短縮版あり。これは詳細版 (= 社員エンジニアレビュー / 新規参入者の on-boarding 用)。

---

## 全体俯瞰

Karpathy が 2026-05 に X で共有した「CLAUDE.md File」は、Claude Code を使う
エンジニア向けの **6 つの workflow 原則 + 3 つの core 原則 + 6 つの mindset**。

これは LLM agent capabilities が **2025-12 で coherence threshold を超えた** phase
shift を受けての提唱。要は「**AI が書くコードの質をどう保つか / どう協業するか**」の
原則。

Personal Brain も Karpathy 思想がベースなので親和性高い。導入する 5 原則を以下に整理。
(導入しない原則は最後の付録に)。

---

## 原則 1: Surgical Edits Only (= 最重要)

> Change only what's necessary
> Don't touch unrelated code or comments
> Don't "improve" things that aren't broken
> Minimize side effects and churn

### Personal Brain 文脈での重要性

私 (Claude) は無意識に「ついでにこれも」「これも合わせて改善」をやりがち。結果として:
- `main.py` が **4700+ 行** に肥大化 (= 既に分割の必要を感じる規模)
- `brain_wiki.py` が **4000+ 行** (同上)
- 1 commit で複数領域が変わる → 副作用リスク
- diff レビューが追えなくなる

### 適用ルール

1. **「修正範囲外には touch しない」を厳守**
   - typo 修正したい誘惑も、別 commit にする
   - import の整理、format 修正は別 PR
2. **「壊れてないもの」は改善しない**
   - 動いてるコードを「もっと良く」しないで放置する勇気
   - リファクタは別 sprint
3. **diff 量で自分を律する**
   - 1 commit の diff が 500+ 行になりそうなら **plan mode に戻る**
4. **副作用テスト**
   - 修正後、無関係 module の smoke test も走らせて regression 確認

### 違反パターンの実例 (= 私が過去に踏んだ罠)

- alignment_trial 実装で、CSS 改善 + JS 改善 + Python ロジック改善を 1 commit で全部やった (= rollback 困難)
- 売上 wiki 修正で「ついでに」関連 metadata も更新 → 別件の bug を呼んだ
- これらは違反、今後避ける

---

## 原則 2: Keep It Simple

> Avoid overengineering and bloated abstractions
> Prefer 100 lines over 1000
> Clean up dead code and cruft
> Ask: "Is there a simpler way?"

### Personal Brain 文脈での重要性

Personal Brain は機能数 が多い (= cron 12 種、endpoint 30+ 種、extractor 6 種、style パターン 50+ 種、etc.)。**機能の積み上げで複雑度が指数的に増えてる**。

例:
- alignment_trial だけで endpoint 4 個 (GET /id, GET /, POST /run, POST /review, GET /status)
- もっとシンプルにできた? → 「POST /review で run も triggered 可」型でまとめれば 2 個で済んだかも

### 適用ルール

1. **新機能の前に「必要?」を 3 回自問**
   - 「これ追加して海山にとって価値ある?」
   - 「既存機能を組み合わせるだけで実現できない?」
   - 「3 ヶ月後の自分が見て、削除候補にならない?」
2. **100 lines > 1000 lines**
   - 同じ機能を 100 lines で実装できるなら、1000 lines の解は捨てる
   - 抽象化レイヤーは「2 回使うか必要になってから」追加
3. **Dead code を見つけたら delete**
   - 別 commit で
   - 「いつか使うかも」は捨てる、git history に残ってる
4. **Bloated abstractions の警告サイン**
   - Class が 5 レベル以上継承
   - 関数が 100 行超え
   - Module の責務が複数領域にまたがる

### 違反パターンの実例

- CORE_WIKI_REGISTRY + CATEGORY_BOOST_BY_INTENT で「動的 target_chars 配分」を実装
  → 効果はある、ただし「複雑さに見合うか」は再評価必要 (= 7 category × 6 intent = 42 通り、cache miss も起きやすい)
- bot_events / tracing / metrics CLI を 1 sprint で 3 個実装
  → どれか 1 つで十分だった可能性

---

## 原則 3: Plan Mode First

> Use plan mode for any non-trivial task
> Write detailed specs up front
> Reduce ambiguity before writing code
> Lightweight inline plan for smaller tasks

### Personal Brain 文脈での重要性

私の働き方は **「海山指示 → 即実装 → commit」**。Karpathy 流は **「指示 → plan 提示 → 海山 OK → 実装」**。

問題点:
- 私が独走して海山が「方向違うかも」と思っても止められない
- 大型機能 (= alignment_trial、bot_events 統合 等) は実装途中で気付くと rollback コスト大
- 仕様の曖昧さが残ったまま実装 → 後で「これじゃない」になる

### 適用ルール

1. **大型変更 (= 50+ 行 / 複数ファイル / 新 module) は plan を先に**
   - plan = どのファイルに何を追加 / 削除 / 修正、なぜ、どう test するか
   - 5-10 行で書ける、長すぎ NG
2. **海山の OK を取ってから実装**
   - 「これでいい?」を 1 行で問う
   - 「進めて」と言われたら GO
3. **小さい変更 (= 1 ファイル / 20 行以内) は inline plan**
   - 「これを修正、理由は X、影響は Y」を tool 呼ぶ前に明示
4. **曖昧さは plan 段階で潰す**
   - 仕様の解釈が複数あるなら、plan で「A or B どっち?」を確認

### 違反パターンの実例

- alignment_trial の 100 件 → 105 件 → 135 件 の追加で、海山指示を「広げる」方向に解釈し続けた
  → plan を最初に提示してれば「100 件で先に試して、必要なら拡張」型の path が見えた
- HTML レビュー UI の改善 (コメント欄拡大 + 応答短く + 敬語回避) を 1 commit でやった
  → 3 つの修正は本来 3 つの sprint だった可能性

---

## 原則 4: Parallelize with Subagents

> Offload research, exploration, analysis
> Use subagents to keep context clean
> One task per subagent
> Merge results back with judgment

### Personal Brain 文脈での重要性

私には `Agent` tool がある (= general-purpose / Explore / Plan 等のサブエージェント)。
**ほぼ使ってない**。これは context bloat の原因:
- コードベース探索を直接やる → context に大量の grep / read 結果が乗る
- リサーチ系 (= 「他の Personal AI PJ 比較」) も直接 → web 検索結果が context に
- 結果として **設計判断する時に context が汚れてる**

### 適用ルール

1. **以下のタスクは subagent に offload**
   - **コードベース探索**: 「○○ どこで使われてる?」「○○ の影響範囲」
   - **リサーチ**: 「世界の Personal AI 比較」「Anthropic の最新仕様」型
   - **大量 log 解析**: `docker logs` 1000 行から原因抽出
   - **ファイル発見**: 「○○ という機能ありそうな .py を探す」
2. **1 subagent = 1 タスク**
   - 「○○ 調べて + 結果から △△ も調べて」は分ける
3. **結果は merge with judgment**
   - subagent の出力を盲信しない、自分で再評価
4. **メイン context は設計判断 / コード生成に集中**

### 違反パターンの実例

- 「sales_data_health の問題究明」を私が直接やった → context に大量の log 残り、その後の作業効率落ちた
- 「世界の Personal AI 比較」も直接 → web 検索結果が今も context に残ってる

---

## 原則 5: Slopacalypse 対策

> Brace for AI slop in 2026
> Hype will be loud
> Signal requires judgment

### Personal Brain 文脈での重要性

これは Personal Brain にとって **構造的に最も critical**:
- AI 生成 wiki / response / コード が日々増えてる
- self-evaluation loop に偏ると **自分で書いた slop を自分で OK 出す**
- hype に流されて「新 cron 追加」「新 endpoint 追加」を続けると、機能の slop で本質を見失う

### 既存の対策 (= 既に実装済)

| 対策 | 機能 | 状況 |
|---|---|---|
| `clone_hallucination_check.py` | post-hoc fact verifier、verifier は別 LLM 系列 | 03:45 daily |
| `clone_external_eval.py` | 月次 第三者 blind 採点 | 月初 1 日 |
| `clone_style_regression.py` | 夜間 regression、3 軸採点 | 03:30 daily |
| `clone_ab_test.py` | online A/B (model 別) | ad-hoc |
| `docs/review/` | 社員エンジニア向けレビューパッケージ | 公開準備済 |

### 追加で意識すべきこと

1. **「これ slop じゃないか?」を自問する習慣**
   - 新規生成 wiki / response を見て、内容が浅ければ flag
   - hype に流された「新機能だらけ」な状態を 月 1 で点検
2. **海山の judgment が最終 signal**
   - clone_feedback / clone_learning の海山レビューが最後の砦
   - これ放置すると AI loop が偏る
3. **第三者視点を意図的に入れる**
   - external_eval (月次 5 名)、社員エンジニアレビュー (= 6 月以降予定)
4. **「機能の追加」より「機能の削除 / 統合」を時々考える**
   - 半年経って使われてない機能は削除
   - 似た機能は統合

### Karpathy の TLDR より

> LLM agent capabilities (Claude & Codex especially) crossed a threshold of
> coherence around December 2025. This is a phase shift in software engineering.
> Intelligence is ahead—integrations, workflows, and diffusion must catch up.
> 2026 will be a high energy year as the industry metabolizes this new
> capability.

Personal Brain はこの phase shift の **早期実装例** だが、同時に slop リスクも高い。
2026 を「judgment を強化する年」として位置づける。

---

## 導入しない原則 (= 既に対応済 or 不要)

### Verify Relentlessly
- 既に smoke + integration test suite で対応 (2026-07 時点 smoke 834 件 + integration 19 件、真実源 = `pytest tests/smoke/ tests/integration/ --collect-only -q`)
- ただし「pass 後も logic verify」の姿勢は意識継続

### Goal-Driven Execution (= TDD)
- Personal Brain の規模では TDD はオーバー
- smoke test を後で追加する path で十分

### Tenacity / Leverage / Fun (Mindset 系)
- 個人 mindset、CLAUDE.md より海山個人の判断軸
- 必要なら identity.md / thinking.md で吸収

### Atrophy
- 「writing と reading は違う」、これは個人開発者向け
- Personal Brain は私 (Claude) が大半書くので適用度低い

### Speedups ≠ Just Faster
- 「速くなる ≠ もっとできる」、概念的に重要だが日常運用ルールにはしにくい

---

## 6 つの「Questions to Keep Asking」 (Karpathy 由来)

これは判断材料として常に頭に置く:

1. **10X engineer gap に何が起きるか?**
   - AI で全 engineer の生産性が上がる → 10X engineer の優位性は減る?
2. **Generalist が specialist を outperform するか LLM で?**
   - 海山が「経営者 (= generalist) で Personal AI 作れる」のはこの仮説の実証
3. **未来の LLM coding はどうなるか?** (StarCraft? Factorio? Music?)
   - = AI を「ゲームの上手いプレイヤー」として使う未来図
4. **Digital knowledge work のどれくらいが society の bottleneck?**
   - 知的労働の自動化が進む先の society 像
5. **Personal Brain 固有: 「海山の代理判断」はどこまで AI に任せるか?**
   - v1 = 質問応答、v2 = タスク実行、v3 = ?
6. **Personal Brain 固有: 200 人運用で何が起きるか?**
   - 利用率、満足度、トラブル、組織変化

---

## 実践チェックリスト (= 私の毎ターン self-question)

コード書く前に self-check:

```
□ Plan mode で spec 出した? (大型変更時)
□ 「もっとシンプルな方法ない?」自問した?
□ 修正範囲外に触らない覚悟?
□ subagent に offload できる調査タスクある?
□ 出力が slop じゃない自信ある?
```

5 つ全部 ✅ なら GO。1 つでも ✗ なら一旦停止。

---

## 監査 (= 海山が私を見張る視点)

海山が私のアウトプットを見て、以下を確認:

| sign | 対応 |
|---|---|
| diff が 500+ 行 | Surgical Edits 違反、止める |
| 新 endpoint / cron / wiki を 1 sprint で 3 個以上追加 | Keep It Simple 違反、整理 |
| plan なしで大型変更 | Plan Mode First 違反、戻る |
| context が log / 検索結果で溢れてる | Subagents 違反、subagent 化 |
| 応答や wiki が深さ不足 | Slopacalypse 警告、judgment 入れる |

これらを sign として、海山が「止めて」と言える基準。

---

## 改訂履歴

- 2026-05-22 初版作成 (Karpathy CLAUDE.md File 由来)
