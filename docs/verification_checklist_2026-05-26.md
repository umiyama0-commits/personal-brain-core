# 徹底動作確認 Checklist — 2026-05-26 (= deploy 翌日)

> Mac Studio Task A-F 完了後、Phase 0 限定 launch 前に **35 項目** verify。
> 海山 + 私 (= MacBook 側 Claude) で並行作業、~95 分目安。

---

# 進行 framework

| Phase | 項目数 | 時間 | 主担当 |
|---|---|---|---|
| P1. Pre-flight | 4 | 5 分 | 私 + 海山 |
| P2. Regression (= 既存壊れてない) | 6 | 15 分 | 海山 |
| P3. Tier 0 group flow | 5 | 20 分 | 海山 (test group) |
| P4. Feature 1-6 + Dashboard | 12 | 30 分 | 海山 + 私 (log) |
| P5. Google Workspace | 3 | 10 分 | 海山 |
| P6. Privacy boundary | 3 | 10 分 | 海山 + 私 |
| P7. Performance | 2 | 5 分 | 私 (log 解析) |
| P8. Sign-off | 1 | - | 海山 |

各 phase 終わるごと → 私に「P1 完了、P2 へ」と一言。

---

# P1. Pre-flight (= 5 分)

```
[ ] 1.1 /health 応答 ok       (curl https://brain.example.com/health)
[ ] 1.2 git HEAD = a1a11f9    (curl /api/admin/deploy-status?token=...)
[ ] 1.3 container uptime < 24h
[ ] 1.4 .env 4 件設定済 (COHERE_API_KEY + BOT_GDRIVE_SHARE_ADDRESS + LW_BOT_USER_ID + 他既存)
```

---

# P2. Regression (= 既存 1:1 DM、15 分、海山個人 LINE で test)

```
[ ] 2.1 通常応答          /clone-public 今日の売上どう?     → 自然応答 + 数字
[ ] 2.2 PDF 添付応答       適当 PDF 投下 + 質問              → 中身踏まえて応答
[ ] 2.3 clone_memory 注入   過去 続き「あの龍仁の件」         → natural resolve
[ ] 2.4 /brain stats        /brain                            → 蓄積件数表示
[ ] 2.5 /audit-stats        /audit-stats                      → 過去 30 日統計
[ ] 2.6 Drive URL fetch     Drive URL 投下                    → fetch or 「権限付与して」
```

→ **1 つでも fail なら deploy 不全、再調査必要**

---

# P3. Tier 0 group flow (= 20 分、test group 1 作成)

事前: 海山が test group 作成 → 「うみやまAI」追加 → 信頼テスター 1 名招待 (= 役員)

```
[ ] 3.1 bot join 受信       group に bot 追加 → log で join event
[ ] 3.2 silent listen       一般 「今日寒いね」 → bot 反応無 + log silent listen
[ ] 3.3 @mention 反応       @うみやまAI テスト  → 応答 + log mention detected
[ ] 3.4 <m> tag format       LINE WORKS UI 経由 mention      → debug log で raw format 確認
[ ] 3.5 group context 蓄積  3-4 turn 後 group_context md     → date metadata 入ってる
```

→ **3.4 で raw format `<m userId="...">` でないなら fine-tune 必要、私に payload 共有**

---

# P4. Feature 1-6 + Dashboard (= 30 分)

## Feature 1: Failure notifier
```
[ ] 4.1 (option) Cohere key 一時 invalid → 3 query 投げ → 60 分以内に LINE Push 来る
        (skip 可、Phase 0 中に自然 trigger 想定)
```

## Feature 2: Usage dashboard
```
[ ] 4.2 browser で /admin/usage 開く → HTML 表示
[ ] 4.3 JSON /api/admin/usage → 構造化 dict
```

## Feature 3: Daily audit UI
```
[ ] 4.4 個人 LINE で /clone-public test → 直後 ○ 送信 → audit 記録
[ ] 4.5 /audit-recent → 直近 list 表示
[ ] 4.6 × 1 数字古い → #1 audit verdict=bad note 付き 記録
```

## Feature 4: Tier 1 memory
```
[ ] 4.7 個人 LINE で 1-2 turn 後 clone_memory/<user_id>.md 確認 → date metadata あり
[ ] 4.8 「龍仁出店」話題 → 翌日 (= 同 session 内) 「あの件」 → 龍仁で resolve
```

## Feature 5: AI Research Agent
```
[ ] 4.9 /research-run → 「research 開始」reply + 2-3 分後 LINE Push digest
[ ] 4.10 /research → 最新 digest 表示
```

## Feature 6 + Dashboard v4
```
[ ] 4.11 browser で /admin/review 開く → KPI summary + nav
[ ] 4.12 /admin/review/research の提案 accept → proposals.jsonl で status=accepted
[ ] 4.13 /admin/review/memory → 「社員 A」「社員 B」 匿名化表示 確認
[ ] 4.14 /admin/review/memory/<user_id> → 会話 bubble + プライバシー注記
```

---

# P5. Google Workspace (= 10 分、海山が Drive で test sheet 準備)

```
[ ] 5.1 共有 sheet 読込    bot-account@example.co.jp に共有された URL → 中身応答
[ ] 5.2 未共有 sheet       未共有 URL → 「bot-account@example.co.jp に閲覧権限を付与して」
[ ] 5.3 PDF binary 取込    Drive PDF URL → PDF text 抽出して応答
```

---

# P6. Privacy boundary (= 10 分、★critical)

```
[ ] 6.1 DM → group leak 無し    DM で「health 不安」 → group context に 漏れない
[ ] 6.2 user A → B leak 無し   group で user A 発言 → user A core memory にのみ
[ ] 6.3 cross-group leak 無し   group X の話題 → group Y context に出ない
```

→ **6.1-6.3 いずれか fail なら public launch 中止、即 fix sprint**

---

# P7. Performance (= 5 分、私が log 解析)

```
[ ] 7.1 1:1 latency p95        bot_events.jsonl から過去 1h、< 5,000 ms
[ ] 7.2 group latency p95     channel_id 付き events、< 6,000 ms
```

---

# P8. Sign-off

```
[ ] P1-P7 全 35 項目 PASS
[ ] 不明 / 違和感 ある項目 ゼロ
[ ] heavy user (= 役員 3-5 名) に投入する自信あり
[ ] Phase 0 限定 launch GO

海山 sign-off → Phase 0 始動
```

---

# Phase 0 始動 後の monitoring (= 1 週間)

毎日 海山 check:
- `/admin/review` で pending件数 確認 (= 5 分)
- 海山個人 LINE に来る notification 即対応
- 異常 (= 役員から困惑、bot 死亡 etc) で 即 私連絡

私 (= MacBook 側 Claude):
- log 監視 + 異常検知 patch 提案
- 海山連絡 受領 即対応

1 週間後 review:
- usage analytics で 数字確認
- audit 内容 review
- GO / NO-GO Phase 1a (= 限定 group 拡大)
