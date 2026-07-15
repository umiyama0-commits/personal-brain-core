# Mac Studio Setup Playbook — 2026-05-25

> 海山 1 回作業 (= ~30-40 分)、明日順序通り実行。
> 完了 後は MacBook 完結化 + 全 Feature 反映 + Phase 0 launch ready 状態。

---

# 順序

| # | Task | 時間 | 危険度 |
|---|---|---|---|
| **A** | ⚠️ password (★旧 ow****70) revoke | 5 分 | **security critical** |
| **B** | git pull + .env 追加 + force-recreate | 10 分 | 中 |
| **C** | auto_deploy 沈黙 真因 診断 | 5 分 | 低 |
| **D** | SSH access ON 設定 (推奨) | 10 分 | 低 |
| **E** | LINE WORKS Console 設定 | 5 分 | 低 |
| **F** | Google OAuth 取得 | 5 分 | 低 |

合計 ~30-40 分。Task A 最優先、それ以外は順序自由。

---

# Task A: ⚠️ password (★旧 ow****70) revoke (= security critical)

## なぜ

- chat / commit log に平文露出済 (= chat 履歴 + Anthropic 機械処理 log)
- 海山自身が以前 OWNDAYS Net Mobile で同 password 使用、既に scheduled remind あり
- 同 password 流用 service あれば連鎖 risk

## 操作

```
1. https://admin.google.com を 海山 OWNDAYS Workspace 管理者 account で開く
2. メニュー → ユーザー → bot-account@example.co.jp
3. 「パスワードをリセット」
4. 新 password 生成 (= 22 文字以上、英数記号混在)
5. ⚠️ 新 password は chat / .env / .py / .md にも書かない
   → password manager (1Password / Bitwarden / Keychain) のみ保存
6. 「次回ログイン時にパスワードを変更する」OFF (= bot 動作のため自動 login)
```

確認:
```bash
# 何処にも貼り付けないこと、これは security best practice
```

---

# Task B: deploy 反映 (= 10 分、最重要)

## 操作

```bash
# 1. brain-agent ディレクトリへ
cd ~/brain-agent

# 2. 最新 code pull (= MacBook 側で a1a11f9 まで push 済)
git pull origin main
# 期待: "Updating ... a1a11f9" 表示、Feature 1-6 + Dashboard v4 + 周知資料 反映

# 3. 現状 .env back up
cp .env .env.backup.$(date +%Y%m%d_%H%M%S)

# 4. .env 追加項目 (= 既知設定 3 件)
cat >> .env <<'EOF'

# ★2026-05-25 deploy 反映 - Plan C v2 Step 2 + Google Workspace + LINE WORKS group
COHERE_API_KEY=cohere_<REDACTED_ROTATE_AT_DASHBOARD>
BOT_GDRIVE_SHARE_ADDRESS=bot-account@example.co.jp
# LW_BOT_USER_ID は Task E 完了後に追記 (= LINE WORKS Console で取得)
EOF

# 5. force-recreate (= 重要、docker compose restart だけだと code 反映されない)
docker compose down line-bot
docker compose up -d --force-recreate line-bot

# 6. 起動完了待ち
sleep 20

# 7. 反映確認 (= 新 endpoint 応答するか)
curl -s "http://localhost:8000/api/admin/deploy-status?token=${ALIGNMENT_TRIAL_TOKEN}" \
  | python3 -m json.tool | head -10

# 期待:
# {
#   "git_head_commit": "a1a11f9",
#   "container_uptime_hours": 0.0,
#   ...
# }
```

## 失敗時 (= rollback)

```bash
# docker compose 起動失敗時
docker logs line-bot --tail 100 | grep -iE "error|exception"

# 復元
docker compose down line-bot
git reset --hard <前 deploy commit>  # ★ 海山確認後のみ
mv .env.backup.<timestamp> .env  # back up 復元
docker compose up -d --force-recreate line-bot
```

---

# Task C: auto_deploy 沈黙 真因 診断 (= 5 分)

## なぜ

過去 6-7 commit が production 反映されてない状態が観測されたため。
Mac mini 上の auto_deploy.sh が動いてないか、失敗してる可能性。

## 操作

```bash
# 1. log 確認 (= 直近 50 行)
tail -50 ~/brain-agent/data/brain/auto_deploy.log

# パターン別判断:
# - "no change (up to date, pipeline healthy)" → 健全、最近 git push 無いだけ
# - "docker build failed" → build 失敗、原因 grep ERROR
# - "AUTO_DEPLOY_ENABLED=0 → skip" → .env で OFF
# - log file 自体無い → cron 走ってない

# 2. crontab に entry あるか
crontab -l | grep auto_deploy
# 期待: "*/5 * * * * /Users/brain/brain-agent/scripts/auto_deploy.sh ..."
# 無い場合 → cron_install.sh 実行
bash scripts/cron_install.sh

# 3. .env で AUTO_DEPLOY_ENABLED の値
grep AUTO_DEPLOY_ENABLED .env
# 期待: 行 無い (= default 1) または "AUTO_DEPLOY_ENABLED=1"

# 4. 手動実行で動くか確認
bash scripts/auto_deploy.sh 2>&1 | tail -20
# 期待: "no change" or "pull ok"
```

## 結果報告

私 (MacBook 側 Claude) に:
- log 末尾 30 行
- crontab grep 結果
- 手動実行結果

→ 真因分析 + 再発防止 patch 提案します。

---

# Task D: SSH access ON 設定 (= 10 分、永久 MacBook 完結化)

## なぜ

これ done すると以降 海山 Mac Studio access 不要、私 (Claude) と海山 MacBook から全 operation 可能:
- `ssh mac-studio` で remote shell
- code edit + deploy + debug 全部 MacBook から
- 「あれ どうなった?」 を即確認可

## 操作

### Step 1: Mac Studio で Remote Login ON

```
1. システム設定 を開く
2. 一般 → 共有
3. 「リモートログイン」 ON
4. 「アクセスを許可するユーザー」 = 「特定のユーザー」
5. + button → "brain" (= bot user) を追加、または「すべてのユーザー」
6. 「リモートログイン」 行の右側に表示される
   "管理者ユーザーは、'ssh brain@mac-studio.local' でこのコンピュータにアクセスできます"
   をメモ
```

### Step 2: Mac Studio の IP / hostname 確認

```bash
# Mac Studio terminal で:
echo "hostname: $(hostname)"
echo "IP (en0): $(ipconfig getifaddr en0 2>/dev/null)"
echo "IP (en1): $(ipconfig getifaddr en1 2>/dev/null)"
whoami  # = brain or umiyamatakeshi 等
```

これ私 (MacBook 側) に教えてもらえれば `~/.ssh/config` 更新します。

### Step 3: MacBook 側 public key を Mac Studio に登録

MacBook 側で**事前に生成済 public key**:
```
ssh-ed25519 AAAA...(your public key)... macbook-claude → mac-studio brain-agent host
```

Mac Studio で 1 行貼り付け:
```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
cat >> ~/.ssh/authorized_keys <<'EOF'
ssh-ed25519 AAAA...(your public key)... macbook-claude → mac-studio brain-agent host
EOF
chmod 600 ~/.ssh/authorized_keys
echo "OK"
```

### Step 4: MacBook 側から接続 test

私 (Claude) が MacBook 側で:
```bash
ssh mac-studio "uname -a && date"
# 期待: Mac Studio の OS info + 現在時刻
```

成功すれば 永久 MacBook 完結化 完了。

---

# Task E: LINE WORKS Developer Console 設定 (= 5 分)

## 操作

```
1. https://developers.worksmobile.com を 海山 OWNDAYS 管理者 account で開く

2. 「うみやまAI」 bot を選択

3. 「Bot 基本設定」:
   - 「グループに招待可能」 → ON
   - 「メッセージ受信 (Callback)」: グループ 含めて ON

4. 「Bot プロフィール」画面 で Bot User ID を取得
   (= 多くの場合 UUID 形式、xxx-xxx-xxx-xxx)
   コピー

5. Mac Studio terminal で:
   echo 'LW_BOT_USER_ID=<コピーした値>' >> ~/brain-agent/.env

6. bot 再起動 (= .env 反映)
   docker compose restart line-bot
```

## 確認

```bash
docker exec line-bot env | grep LW_BOT_USER_ID
# 期待: LW_BOT_USER_ID=<取得した値>
```

---

# Task F: Google OAuth `bot-account@example.co.jp` で token 取得 (= 5 分)

## なぜ

bot が Drive / Docs / Sheets / Slides を読むため、umiyama-ai 専用 account で OAuth 必要。
既存 token が個人 account のものなら、共有された資料を読めない。

## 操作

```bash
cd ~/brain-agent

# 1. 既存 token back up (= 別 account のものかもしれない)
if [ -f data/brain/.google_token.json ]; then
  mv data/brain/.google_token.json data/brain/.google_token.json.bak.$(date +%Y%m%d_%H%M%S)
  echo "old token backed up"
fi

# 2. OAuth flow 実行 (= browser 起動、bot-account@example.co.jp で login)
python3 scripts/google_sync.py
# → browser に同意画面 表示
# → 「許可」 click
# → ターミナルに success 表示

# 3. 反映確認
python3 -c "
import gdrive_sync
creds = gdrive_sync.get_credentials()
print('refresh_token 有:', bool(getattr(creds, 'refresh_token', None)))
print('scopes:', creds.scopes if hasattr(creds, 'scopes') else 'unknown')
"
# 期待: refresh_token 有: True

# 4. test fetch (= 1 件 query で動作 verify)
TOKEN="${ALIGNMENT_TRIAL_TOKEN}"
# 共有された任意 Google Doc URL を 1 つ準備、ここでは sample
# (海山が bot-account@example.co.jp に共有してる Doc URL に置き換え)
```

## 失敗時

```bash
# OAuth 失敗 / 同意画面出ない場合
# 1. Google Cloud Console で OAuth Client ID 確認
#    https://console.cloud.google.com → APIs & Services → Credentials
# 2. credentials.json (= OAuth client secret) が data/brain/ に存在するか
ls data/brain/credentials.json
# 3. 無ければ 設定 redo 必要 (= 別途私に連絡)
```

---

# 完了後 final check

全 task done 後、MacBook 側 (= 私が確認):

```bash
TOKEN="${ALIGNMENT_TRIAL_TOKEN}"

# 1. deploy 反映確認
curl -s "https://brain.example.com/api/admin/deploy-status?token=${TOKEN}" \
  | python3 -m json.tool

# 2. 新 endpoint test
curl -s -I "https://brain.example.com/admin/review?token=${TOKEN}" | head -3
# 期待: HTTP/2 200

# 3. browser で Dashboard 確認
open "https://brain.example.com/admin/review?token=${TOKEN}"
```

→ これら全部 OK で**明後日の徹底動作確認 phase へ進む準備完了**。

---

# 緊急時 escalation

| 状況 | 連絡先 / アクション |
|---|---|
| bot 停止 / restart loop | docker logs line-bot --tail 100 → MacBook 側 私に共有 |
| OAuth 取得不可 | Google Cloud Console 状態確認 / 別途 setup support |
| LINE WORKS webhook 来ない | Console 「Bot 監視」画面で webhook log 確認 |
| Mac Studio 自体 起動しない | 海山個人 LINE で連絡 (= 私から直接 access 不可) |

---

# 完了報告 template (= 私 への report 用)

```
□ Task A: password revoke 済
□ Task B: git pull → a1a11f9、force-recreate OK、deploy-status 反映
□ Task C: auto_deploy log 結果 (← 内容貼り付け)
□ Task D: SSH ON、IP は <xxx.xxx.xxx.xxx>、authorized_keys に key 追加済
□ Task E: LINE WORKS Console 設定 OK、LW_BOT_USER_ID=<UUID>
□ Task F: Google OAuth umiyama-ai 取得済、refresh_token 確認 OK
```

→ これ送ってもらえれば 明後日 verification phase 即着手。
