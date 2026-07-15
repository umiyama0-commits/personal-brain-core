# brain-agent マルチ PC 開発セットアップ

別の PC の Claude Code から、同じプロジェクトに開発指示できるようにする手順。

## 構成

```
┌─────────────────────┐         ┌────────────────────┐
│  メイン Mac          │ ◄────── │  別の PC           │
│  - Docker line-bot  │  pull   │  - Claude Code     │
│  - cron jobs        │         │  - 編集 + commit   │
│  - cloudflared      │ ──────► │  - push            │
│  - 全データ live    │  push   │                    │
└─────────────────────┘         └────────────────────┘
       ▲                                  ▲
       │       ┌──────────────────┐       │
       └───────│  GitHub (private) │◄──────┘
               │  brain-agent.git  │
               └──────────────────┘
```

**設計原則**:
- **コードは Git で同期** (Python / shell / md / wiki 一部)
- **データは同期しない** (raw / chroma / cookies / 個人履歴は機密 + 重い)
- **bot/services はメイン Mac だけで動く** (Docker / cron / tunnel)
- **別 PC は編集 + push 専用** (Claude Code で指示 → push → メイン Mac が auto-pull)

## 同期する / しない

| 区分 | パス | 同期 | 理由 |
|---|---|---|---|
| コード | `*.py`, `scripts/`, `docker-compose.yml` | ✅ | 開発対象 |
| ドキュメント | `CLAUDE.md`, `*.md` | ✅ | コンテキスト |
| Wiki コア | `data/brain/wiki/{identity,style,thinking,knowledge,...}` | ✅ | Claude Code の context |
| アラインメント | `data/brain/alignment/` | ✅ | 履歴用 |
| 議事録 | `data/brain/wiki/meetings/` | ❌ | 機密、暫定除外 |
| 生 scrape | `data/brain/import/`, `data/brain/raw/` | ❌ | 43MB、PII |
| 個人 1:1 履歴 | `data/brain/clone_history/` | ❌ | プライバシー |
| 個別メモリー | `data/brain/clone_memory/` | ❌ | プライバシー |
| ベクター DB | `chroma_data/` | ❌ | rebuild 可能、大 |
| Secrets | `.env`, `*.pem`, `*_cookies.json` | ❌ | 絶対 NG |

## セットアップ手順

### Phase 1: GitHub プライベートリポジトリ作成 (メイン Mac)

**【海山が手動で】**

1. **GitHub アカウントで private repo 作成**
   - https://github.com/new
   - Repository name: `brain-agent`
   - **Private** を選択
   - "Add README" / "Add .gitignore" / "Add license" は **全部チェック外す** (空で作る)
   - "Create repository"

2. **SSH key (まだなら) 作成 + GitHub 登録**
   ```bash
   # SSH key を作成 (既存があれば skip)
   ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519 -C "you@example.com"

   # 公開鍵を表示してコピー
   cat ~/.ssh/id_ed25519.pub
   ```
   → https://github.com/settings/keys で "New SSH key" → 貼り付け → 保存

3. **メイン Mac で初期 commit + push**
   ```bash
   cd /Users/brain/brain-agent

   # git init (.gitignore は既に作成済み)
   git init -b main
   git config user.email "you@example.com"
   git config user.name "Take Umiyama"

   # 初回 add (.gitignore で多くを除外、確認するなら git status で見る)
   git add .
   git status         # ★ここで確認、巨大ファイルや secrets が混ざってないか
   git commit -m "initial commit: brain-agent codebase"

   # GitHub のリポジトリ URL を設定 (上で作った repo の URL)
   git remote add origin git@github.com:YOUR_USERNAME/brain-agent.git
   git push -u origin main
   ```

### Phase 2: 自動デプロイ cron セットアップ (メイン Mac)

**【自動で実行可能、海山が一回 crontab を編集】**

```bash
# crontab に追加 (crontab -e)
*/5 * * * * /Users/brain/brain-agent/scripts/auto_deploy.sh >> /Users/brain/brain-agent/data/brain/auto_deploy.log 2>&1
```

これで 5 分おきに `git fetch + 変更あれば pull + (py 変更なら docker rebuild)` が走る。

### Phase 3: 別の PC のセットアップ

**【新しい PC で】**

1. **SSH key 作成 + GitHub 登録**
   - 上の Phase 1 step 2 と同じ手順、ただし新 PC 上で

2. **リポジトリを clone**
   ```bash
   # 任意の場所に clone (例: ~/brain-agent)
   git clone git@github.com:YOUR_USERNAME/brain-agent.git
   cd brain-agent
   ```

3. **`.env` を別途準備**
   - メイン Mac の `.env` を **手動でコピー** (USB / 1Password 経由など)
   - もしくは `.env.example` を見ながら必要な API キーを再投入
   - `*.pem` (LW_PRIVATE_KEY_PATH) もコピーが必要なら

4. **Claude Code をインストール + ログイン**
   - https://claude.ai/code で同じ Anthropic アカウントでログイン
   - 自動的に同じ subscription が使える

5. **(任意) Docker は別 PC では起動しない**
   - bot は **メイン Mac だけ** で稼働させる前提
   - 別 PC は編集 + push 専用

### Phase 4: 動作確認

別 PC で:
```bash
# 何か小さい変更 (例: コメント追加)
echo "# test from second PC" >> CLAUDE.md
git add CLAUDE.md
git commit -m "test: from second PC"
git push
```

5 分以内にメイン Mac で:
```bash
tail -f /Users/brain/brain-agent/data/brain/auto_deploy.log
# pulling... / no rebuild needed (md changes only) と出れば成功
```

## 開発ワークフロー (別 PC から)

```bash
# 最新 pull
git pull --rebase

# Claude Code で開発指示
claude "/path/to/brain-agent"
# → 自然言語で「○○を実装して」と指示
# → Claude が編集 → 動作確認は限定的 (Docker 無いため)

# commit + push
git add -A && git commit -m "feat: ..." && git push
# → 5 分後にメイン Mac で自動 deploy
```

## 別 PC でできない / 制限

- **Docker 経由の実行テスト**: メイン Mac じゃないと動かない
- **live データへのアクセス**: clone_history / chroma / cookies は同期されてない
  - → 動作確認は本番 (メイン Mac) でやるか、別 PC で `--dry-run` 系でテスト

→ **別 PC = 「読んで考えて書く」専用、「動かす」のはメイン Mac** という割り切り

## トラブルシュート

### 別 PC で push できない
- SSH key が GitHub に登録されてない → Phase 3 step 1
- branch が古い → `git pull --rebase origin main` 先にやる

### auto_deploy が動いてない
- log 確認: `tail -100 /Users/brain/brain-agent/data/brain/auto_deploy.log`
- 手動実行: `/Users/brain/brain-agent/scripts/auto_deploy.sh`
- cron 確認: `crontab -l | grep auto_deploy`

### rebase conflict 出た
- auto_deploy が `--abort` で自動回避 → メイン Mac で手動解決
  ```bash
  cd /Users/brain/brain-agent
  git pull --rebase
  # conflict 解決 → git add → git rebase --continue
  ```

### secret を間違えて commit した
1. **即座に rotate**: 該当 API キーを発行元 (Anthropic / OpenAI / GitHub 等) で revoke
2. `git rm --cached <file>` で履歴から外す + .gitignore に追加
3. `git filter-repo` で履歴掃除 (or 軽量なら新規 repo)

## 構成図 (リアルタイム)

```
編集 (別 PC, Claude Code)
    ↓ git push
GitHub (private)
    ↓ git fetch (5min cron)
メイン Mac
    ↓ git pull --rebase
    ↓ (py 変更なら) docker compose build line-bot
    ↓ docker compose up -d --force-recreate
本番 (LINE Works / LINE Bot / cloudflared)
```
