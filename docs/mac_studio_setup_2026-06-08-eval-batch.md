# Mac Studio 設定手順 — 世界基準評価 #1〜#4 の有効化 (2026-06-08)

> MacBook 側 (Claude) は実装・push 済。本ファイルは **Mac Studio で海山が打つコマンド**。
> 順序: #1 (即・5分) → #2 (restic、#3 の前提) → #3 (purge、#2 成功後) → #4 (月初自動・独立)。
> secret は全て `.env` に入れる (gitignore 済 = commit されない、CLAUDE.md 1.1)。本ファイルに実値は書かない。

```bash
cd ~/brain-agent
git pull --rebase origin main     # 最新 (#1-#4 の実装) を取り込む
```

---

## #1. eval gate を warn 有効化 (deploy 後 eval で品質 regression を観測) — 5 分

deploy を止めずに、応答品質が落ちた deploy を LINE 通知 + 記録する観測モード。

```bash
# 現状確認
grep -E 'DEPLOY_EVAL_GATE|ALIGNMENT_TRIAL_TOKEN' .env

# (a) DEPLOY_EVAL_GATE が "off" なら warn に変更 (or 行ごと削除 → 既定 warn)。未設定なら既に warn。
# (b) ALIGNMENT_TRIAL_TOKEN が空なら任意の長い文字列を設定 (gate の eval 実行に必須。未設定だと
#     fail-open で skip = 無害だが計測されない)。
vim .env
#   DEPLOY_EVAL_GATE=warn
#   ALIGNMENT_TRIAL_TOKEN=<32文字以上のランダム文字列、未設定なら>   例: openssl rand -hex 24

# 反映: auto_deploy が毎 cycle .env を読む → restart 不要。次の commit deploy から warn。
# 確認 (次に commit が流れた時):
tail -50 data/brain/cron.log | grep -i 'eval gate'        # "deploy eval gate (warn)" が出る
cat data/brain/alignment/eval_gate_verdicts.jsonl | tail  # 判定が 1 行ずつ記録される
```

**block への昇格 (数週間後)**: `eval_gate_verdicts.jsonl` の `verdict=="regression"` が、実際は正常
だった deploy で何回出たか (= 誤検知率) を見る。**誤検知ゼロ**を確認できたら `.env` を
`DEPLOY_EVAL_GATE=block` に → 以降は regression deploy を前 image へ自動 rollback。

---

## #2. restic offsite backup + restore drill (RPO∞ → ~6h、戻せる証拠) — 20 分

一次データ (clone_history / memory / alignment / 会話) を暗号化して B2/S3 へ。**#3 の前提**。

```bash
# 1. restic install
brew install restic git-filter-repo     # filter-repo は #3 で使う

# 2. Backblaze B2 を用意 (推奨・S3 より安い):
#    https://www.backblaze.com → アカウント作成
#    - B2 Cloud Storage → Buckets → Create a Bucket (例: umiyama-brain-backup、Private)
#    - App Keys → Add a New Application Key (上記 bucket に限定) → keyID と applicationKey を控える
#    ※ S3 を使うなら .env の AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY 側を埋める

# 3. .env に追記 (★RESTIC_PASSWORD を失うと永久に復元不能 → 1Password 等に別途保管)
vim .env
#   RESTIC_REPOSITORY=b2:umiyama-brain-backup:restic
#   RESTIC_PASSWORD=<強いパスフレーズ>           例: openssl rand -base64 32
#   B2_ACCOUNT_ID=<keyID>
#   B2_ACCOUNT_KEY=<applicationKey>

# 4. 初回 backup を手動実行 (6h cron を待たない。repo は自動 init される)
set -a; . ./.env; set +a
bash scripts/backup_offsite.sh
#   → LINE に「offsite backup 初回成功」が来れば OK (data/brain/.backup_offsite_first_success 生成)

# 5. restore drill = 「実際に戻せる」を実証 (これが通って初めて backup は backup)
bash scripts/backup_restore_drill.sh
#   → LINE に「restore drill 初回成功」= RPO∞ wall 越え。以降 週次 cron で自動検証。
#   dry-run だけ: bash scripts/backup_restore_drill.sh --dry-run
```

---

## #3. 第三者PII を git から完全除去 (untrack + 履歴 purge) — ★destructive

社員/取引先の会話 646 件 (lineworks_/line_/gmail_/gcal_) を git 管理外 + 過去履歴からも除去。
**#2 の restic 成功 + restore drill 通過が前提** (disk から消えても復元できる状態にしてから)。

```bash
# (a) untrack: HEAD の index から 646 件を外す (disk には残る = bot は読み続ける)
bash scripts/untrack_conversations.sh      # safety copy + commit のみ (push は確認後)
git log --stat -1 | head -20               # 削除された (index から) を確認
git push origin main

# (b) 履歴 purge: 過去 commit からも除去 (★全 commit hash 書換 = irreversible)
bash scripts/purge_conversation_history.sh --dry-run       # 646件 + 前提チェックリスト
#   全部 OK なら実行:
bash scripts/purge_conversation_history.sh --execute --i-have-backup-and-approval

# (c) force-push (GitHub の履歴からも消す)
git remote add origin git@github.com-brain-agent:umiyama0-commits/brain-agent.git
git log --all --name-only --pretty=format: -- 'data/brain/raw/notes/lineworks_*' | head  # 空を確認
git push origin --force --all && git push origin --force --tags

# (d) ★MacBook 側は re-clone 必須 (履歴が乖離 → pull 不可)。MacBook で:
#     mv ~/brain-agent ~/brain-agent.old && git clone <URL> ~/brain-agent
#     (~/brain-agent.old は .env / data の手元コピーを移してから削除)
```

---

## #4. judge κ の human-data ループ (月初自動・初回だけ手動でも可) — 独立

LLM judge が「正しい方向に偏っているか」を人間 5 名の blind 採点で検算 (κ)。月初 09:00 に form 自動生成
+ LINE 通知が走る (cron 登録済)。**今すぐ初回を回すなら**:

```bash
bash scripts/clone_cron.sh external-eval
#   → LINE に form path → 5 名程度に配布 (blind = 発信者匿名で内容のみ採点)
# 採点 json が集まったら:
python3 scripts/clone_external_eval.py --import-file <評価者からの eval_results__*.json>   # 人数分
python3 scripts/clone_external_eval.py --agreement     # LLM judge vs 人間の κ を算出
#   → κ<0.6 なら judge が系統的にズレ = 再較正の LINE alert
```

---

## 完了後の状態 (評価の「橋を渡る」)

| # | 越える wall | 完了条件 |
|---|---|---|
| #1 | hard eval gate | warn で verdict 記録開始 → 数週後 block |
| #2 | RPO∞ | restore drill 初回成功 (戻せる証拠) |
| #3 | 第三者PII on GitHub | 履歴 purge + force-push + re-clone 完了 |
| #4 | judge 無検算 | 初回 5 名採点 import → κ 算出 |

困ったら MacBook (Claude) に各コマンドの出力を貼れば原因を見ます。
