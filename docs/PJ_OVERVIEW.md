# Personal Brain Project — Overview & Capability Snapshot

> **Purpose**: 他 PJ との合併 / 統合 brainstorming 用の自己説明 document。
> 海山 PJ の現状 capability、強み弱み、合併で発生する価値 / 必要 piece を honest に提示。
>
> **Last Updated**: 2026-05-24
> **Author**: 海山丈司 + Claude (= 主開発 pair)
> **Status**: Phase 0 pre-launch (= 6 Feature build 完了、内部 limited test 直前)
> **★2026-07-03 追記**: 本 document は 2026-05-24 時点のスナップショット。v1 は 2026-05-21 に社員へ正式公開済・以後本番稼働中。現状の真実源は CLAUDE.md + `docs/review/ARCHITECTURE.md` (本文の「deploy 直前」「明日反映予定」等は当時の記述)。

---

# 📌 TL;DR (= 1 ページ要約)

**What**: OWNDAYS CEO 海山丈司の **Personal AI (= LINE Bot + 自己複製基盤)**

**Why**:
- 経営者業務補完 (= 社員からの Q&A 自動応答)
- 集団意思決定支援 (= LINE WORKS group 経由)
- 「不在時の経営者判断代行」 long-term

**Current state**: 6 Feature build 完了、production deploy + Phase 0 限定 test 直前
- Code: ~50 Python modules、+5,671 行 (= 直近 1 セッション、§10.2 commit 合計と一致)
- Test: 546 smoke tests passing
- Documentation: 14 ADR + failure-log + runbook
- Cost: 月 $200-500 想定

**Phase Plan**:
| Phase | 内容 | timeline |
|---|---|---|
| 0 | 海山 + 信頼役員 3-5 名 限定 test | 来週開始 (= 1 週間) |
| 1a | test group 1-2 個 拡大 | 1 ヶ月 |
| 1b | OWNDAYS 本部 200 名 | 2-3 ヶ月 |
| 1c | 店舗 300 + 海外 | 3-6 ヶ月 |
| 2 | Web meeting 参加 (= 音声 + 介入判断) | 6-12 ヶ月 |
| 3 | 意思決定モード (= 不在時代行) | 12-18 ヶ月 |
| 横展開 | 自分の別会社 + 知り合い経営者 限定配布 | trigger 依存 |

**ROI target (Phase 1)**: 月間 1,000 → 10,000 query / $1k → $10k 価値

**Differentiator** (= Reid AI / Delphi.ai 比較で):
- **集団対話** (= LINE WORKS group + per-group memory)
- **業務 KPI 直結** (= 売上 data + retrieval fallback で fact 厳格)
- **海山 1-click audit closed loop** (= 経営者本人を ground truth signal の center)
- **失敗 log + ADR + cross-check subagent rule** (= 開発 process 自体の差別化)
- **統合 monitoring + dashboard** (= production reliability)

**Honest gaps**:
- Voice production deploy 未 (= 「準備済」止まり)
- Video avatar 0
- 多言語 0 (= 日本語のみ)
- Reasoning pattern capture 弱 (= retrieval + LLM glue 止まり)
- Single host Mac Studio = SPOF
- Bus factor = 1 (= 海山 + Claude)

---

# 1. Project Identity

## 1.1 Mission

OWNDAYS CEO 海山丈司の **思考体系・判断軸・話法** を AI に外部化し、社内外との対話を補完する。
最終的に「**不在時に経営者として一部意思決定を代行**」可能なレベルを目指す。

3 段階の貢献を想定:
- **Phase 1**: 社員からの問い合わせ自動応答 (= 海山時間節約 + 社員待ち時間削減)
- **Phase 2**: Web meeting 参加 / 同席 (= 海山発言の補完 + 即答提供)
- **Phase 3**: 意思決定モード (= 一定範囲の決裁を代行)
- **横展開**: 同 architecture を関連経営者に限定配布 (= SaaS 化ではない、per-instance)

## 1.2 Why now (= LLM 時代の必然性)

- LLM が「個人の思考体系を外部化可能」 level に到達 (= 2024-2026)
- OWNDAYS 海外含む拡大で**経営者の bandwidth が constraint**化
- 「次世代経営者育成」と「経営判断品質維持」両立に AI 補完が現実解

## 1.3 Phase Position (= 今ここ)

```
[Phase 0] ← 今ここ、deploy 直前
   ↓
[Phase 1a-c] = 社内 Q&A rollout (= 3-6 ヶ月)
   ↓
[Phase 2] = Web meeting 参加 (= 6-12 ヶ月)
   ↓
[Phase 3] = 意思決定モード (= 12-18 ヶ月)

[横展開] = trigger 依存、Phase 1b 以降の任意時点
```

---

# 2. Capability Snapshot (= 何ができる)

## 2.1 Channels (= bot がどこで動くか)

| Channel | 状態 | 用途 |
|---|---|---|
| 海山個人 LINE Bot | ✅ Production | 海山自身の test + admin / review コマンド |
| LINE WORKS 「うみやまAI」 1:1 DM | ✅ Production | 社員個別相談 |
| LINE WORKS group + @mention | ✅ Tier 0 build 済 (= deploy 待ち) | 集団対話、silent listen + mention 反応 |
| Voice (= 音声 出力) | △ 準備済 (= production 未) | Phase 2 Meet 参加 |
| Video avatar | ❌ 未 build | Phase 3 候補 |
| Multi-language | ❌ 日本語のみ | Phase 2 海外店舗 |

## 2.2 Memory Architecture (= 「あの件」「先週の話」が通る design)

```
per-user core memory (clone_memory.py)
  ├─ Profile (= 役職 / 所属 / 関係)
  ├─ Ongoing Topics (= 進行中、date metadata 付き)
  ├─ Key Facts (= 不変事実、date metadata 付き)
  └─ Preferences (= 応答 style 好み)

per-group context (clone_group_context.py、Tier 0 新)
  ├─ Group Profile
  ├─ Ongoing Topics
  ├─ Recent Events (= 日付降順)
  └─ Group Culture

Tier 1: 暗黙参照解決 (= CLONE_PUBLIC_PROMPT)
  ├─ 「あの件」「先週の話」silent resolve
  ├─ Calendar 基準時間照合
  ├─ 拮抗時 機械的に確認
  └─ Topic 名冒頭明示 必須

Sleep-time agent (= Letta pattern)
  └─ Idle 30s で memory 再整理 (smart model)

Privacy 境界
  └─ 1:1 DM ↔ group cross-leak 不可
```

## 2.3 Retrieval / RAG

- **Vector store**: chromadb
- **Embedding**: chroma 内蔵 (= sentence-transformers)
- **Contextual Retrieval**: Anthropic 手法 (= chunk 前に文脈 prepend)
- **Reranking**: Cohere Rerank v3.5 (= cross-encoder、$2/1000 searches)
- **Fallback**: 高信頼 hit 0 + keyword 含時に「データ無い」 honest response (= Plan C v2 Step 5)
- **Recency bias**: 売上系 query で当日 file を up-weight

## 2.4 Self-improvement Loops (= 自動進化機構)

| Component | 頻度 | 役割 |
|---|---|---|
| `clone_auto_improve` | 03:00 daily | 7 種 signal 検知 + 自動 wiki 編集 |
| `clone_style_regression` | 03:30 daily | 30 件 gold で response 品質 regression test |
| `clone_hallucination_check` | 03:45 daily | post-hoc fact verifier (= smart-gpt で別系列) |
| `clone_external_eval` | 月初 1 日 | 5 軸 5 段階 blind 採点 |
| `clone_memory_privacy_review` | 04:00 daily | memory private 行 archive |
| `clone_sleep_time_agent` | idle 30s | memory 再整理 |
| `eval_baseline` | 04:00 daily | eval_set_v1 (30 件) で baseline 計測 |
| **`ai_research_agent`** | **月曜 09:30** | **世界 AI 進化 → 反映提案** (= Feature 5 新規) |

## 2.5 Quality Assurance (= closed loop)

```
production query
   ↓
bot 応答
   ↓ (parallel)
   ├─ clone_history append
   ├─ update_clone_memory (fast-gpt 即時)
   ├─ update_group_context (channel あれば)
   └─ schedule_sleep_time_agent (30s idle)

事後 review (= 海山 daily 10 分):
   ├─ /audit-recent → 1-click ○/×/!
   ├─ /clone-feedback → 社員修正希望 review
   ├─ /clone-learning → 会話発見ダイジェスト
   └─ /research → AI Research 提案 review

統合 UI: /admin/review (= browser 経由 1 click、Feature 6 新規)
```

## 2.6 Monitoring / Observability

```
bot_events.jsonl (= 構造化 turn log)
   ↓ 5 分おき
bot_uptime_monitor (= 442 lines)
   ├─ /health 死活
   ├─ webhook silent (= 30 min 反応無)
   ├─ turn_failed burst (= 1h で 5+ 件)
   ├─ context_prefix_leak (= critical)
   ├─ deploy stale (= container uptime > 24h)
   ├─ component_streak (= Feature 1 新規、Cohere/Drive/group 個別 detect)
   └─ silent_skip burst

→ 異常検知 → LINE Push (= 海山 即知る) + remediation hint
→ 30 min cooldown で flood 防止
```

## 2.7 Admin UI (= 海山 review 用)

| URL | 機能 |
|---|---|
| `/admin/review` | 全 queue pending KPI summary (= Top page) |
| `/admin/review/research` | AI Research 提案 (accept/reject) |
| `/admin/review/audit` | audit stats + needs_attention + 未 audit list |
| `/admin/review/learning` | 会話発見 (accept/reject/noted) |
| `/admin/review/feedback` | 社員修正希望 (accept/reject/noted) |
| `/admin/review/memory` | 個別 memory user list |
| `/admin/review/group` | group context channel list |
| `/admin/usage` | ROI progress + heavy user + channel split + daily trend |
| `/api/admin/deploy-status` | git HEAD + container uptime + build failures |
| `/api/admin/redeploy` | MacBook curl で deploy trigger |

Mobile-friendly、token-gated。

## 2.8 LINE / LINE WORKS 統合

- LINE Messaging API (= 海山個人 LINE Bot)
- LINE WORKS Bot API (= 「うみやまAI」、1:1 + group 両対応)
- `<m userId="...">` 公式 mention 記法対応
- 添付 file (= PDF/DOCX/XLSX/PPTX/images) → 自動 text 抽出
- URL on-demand fetch (= Google Drive / Slides / Sheets / Docs)

## 2.9 Google Workspace 統合

- Google Drive selective sync (= cron 化された Monday Dash / Focus10 / WBR / 営業部)
- On-demand URL fetch (= 共有された Drive URL の中身を即取込)
- 権限不足時: 「bot-account@example.co.jp に閲覧権限を付与して」自動返答
- OAuth2 user credentials (= Service Account 化 候補)

## 2.10 業務 data 統合

| Data source | 用途 | 同期頻度 |
|---|---|---|
| OWNDAYS 売上 (Net Mobile) | 「今日の売上どう?」「先月比?」 | 2h おき |
| OWNDAYS 売上 過去 3 年 | 「2024-04 の数字?」 | 週次 full + 日次 incremental |
| 店舗マスター | 店舗別集計 | 更新時 |
| LINE Works スクレイパー | 議事録 / 内部告知 | 22:00 daily |
| Apple Notes | 海山メモ | 23:00 daily |
| STAPA メルマガ | 社内発信 | 取込時 |
| Plaud 音声 | 会議 録音 | 08:00 daily |
| Recall.ai | Web meeting 録音 | webhook |

---

# 3. Tech Stack

## 3.1 Language / Framework
- Python 3.9+
- FastAPI (= webhook server)
- asyncio (= concurrent I/O)
- httpx (= HTTP client)
- pytest (= testing)

## 3.2 LLM 階層 (= LiteLLM proxy 経由)
| alias | model | 用途 |
|---|---|---|
| `smart` | Claude Opus 4.8 | wiki compile / clone respond / sleep_time |
| `smart-gpt` | GPT-5.4 | 比較 / self-eval loop 遮断 |
| `smart-gpt-pro` | GPT-5.4-pro (真実源 = litellm_config.yaml) | 高難度 reasoning |
| `fast` | GPT-4o | privacy 分類 / chat |
| `fast-gpt` | GPT-5.4-mini | clone_memory update / 軽量 task |
| `code-max` | GPT-5-pro | internal code |

## 3.3 Vector / Retrieval
- chromadb (= local persistent)
- sentence-transformers embedding
- Cohere Rerank 3.5 (= cross-encoder)
- Contextual chunks (= Anthropic 手法)

## 3.4 Infrastructure
- Docker + docker-compose
- Mac Studio (= bot host)
- Mac mini (= auto_deploy + scrape host)
- Cloudflare Tunnel (= public endpoint)
- Redis (= cache、session 用、ただし軽い使用)

## 3.5 External services
- LiteLLM (= LLM proxy)
- Cohere (= rerank)
- Google Workspace (= Drive / Docs / Sheets / Calendar / Gmail)
- LINE Messaging API
- LINE WORKS Bot API
- Recall.ai (= meeting bot)
- Vapi (= voice alignment)
- HeyGen / D-ID (= 候補、video avatar)

## 3.6 Repository
- GitHub private repo (= main 直接 push、auto_deploy)
- pre-commit hooks (= gitleaks / 構文 check)
- 1 main branch、PR 不要 (= velocity 優先)

---

# 4. Build History & Engineering Discipline

## 4.1 Timeline
- **2024 Q4**: initial concept (= LINE bot + 個人 memo)
- **2025 H1**: 自己複製基盤 4 層モデル + ADR 文化 confirm
- **2025 H2**: OWNDAYS 売上統合 + privacy_gate + 「うみやまAI」 LINE Works deploy
- **2026 Q1**: 多層 self-evaluation loop (= regression / hallucination / external_eval)
- **2026 Q2**:
  - Plan C v2: Contextual Retrieval + Cohere Rerank
  - Tier 0: LINE WORKS group 対応
  - **Feature 1-6 build** (= 直近 1 セッション、+3,872 行)

## 4.2 Engineering 規律 (= CLAUDE.md 1.x)

| 規律 | 内容 |
|---|---|
| 1.1 Secret hygiene | os.getenv() のみ、.py / .md / .yaml 平文禁止 |
| 1.4 Docker rebuild | force-recreate 必須 (= restart だけでは古い code) |
| 1.5 chromadb 並行 access 禁止 | SIGSEGV crash 防止 |
| 1.7 scraper 出力 ≠ LLM compile | 決定論的 wiki 保護 |
| 1.8 cron dry-run with minimal PATH | 外部 binary 解決 verify |
| 1.13 wiki scrub gotcha | 個人名を wikilink display text に入れない |
| 1.15 **cross-check subagent rule** | system prompt / retrieval / destructive / decisions 改訂時に並行 verify subagent spawn 必須 |

## 4.3 失敗 log 文化

- `docs/failure-log.md`: ★YYYY-MM-DD で学びを時系列集約
- `docs/decisions/`: 14 件 ADR (= 重要決定の独立 .md)
- 「同じ class の事故を 2 度起こさない」 invariant

## 4.4 Test discipline

- 546 smoke tests passing
- 各 feature build と独立 commit
- TDD-ish: test 先書 → implementation → commit
- Karpathy 5 原則: Surgical Edits / Keep Simple / Plan Mode / Parallelize / Slopacalypse 対策

---

# 5. Production Readiness

## 5.1 What's live (= 既 production 稼働)

- 海山個人 LINE Bot (= 1 年以上稼働)
- 「うみやまAI」 LINE WORKS Bot 1:1 (= ~6 ヶ月稼働)
- 売上 data 同期 + retrieval
- daily / weekly cron 群 (= 自動 quality assurance)
- bot_uptime_monitor (= 5 min cycle)

## 5.2 What's pending (= deploy 待ち、Feature 1-6 + Tier 0)

- LINE WORKS group 対応
- Google Workspace 連携 (= 認証 reset 後)
- Cohere Rerank 統合
- 6 Feature (= notifier / dashboard / audit / Tier 1 memory / research / review UI)
- AI Research Agent (= 月曜 自動)

→ **明日の Mac Studio 1 回 setup で全 deploy 反映予定**

## 5.3 ROI Targets (= Phase 1)

| metric | target | 現状 | gap |
|---|---|---|---|
| 月間 query 数 | 1,000 → 10,000 | unknown (= dashboard 後測定) | 不明 |
| 1 回答価値 | $1 | 想定 | 信頼性次第 |
| ROI ratio | $1k → $10k/月 | TBD | Phase 0 で目処 |
| heavy user 数 | 100 (× 20/月) | unknown | distribution 必要 |
| group adoption | 1-2 → 10+ | 0 (= Tier 0 deploy 待ち) | Phase 1a 着手 |
| 信頼性 (= 海山 audit good 率) | ≥ 80% | unknown | audit loop 稼働後測定 |

## 5.4 Cost structure (= 想定 月次)

| 項目 | cost / 月 |
|---|---|
| LLM API (= LiteLLM 経由 Claude / GPT) | $100-200 |
| Cohere Rerank ($2/1000 searches) | $5-15 |
| Google Workspace API | $0 (= business plan 内) |
| LINE WORKS Bot | $0 (= business plan 内) |
| Mac Studio 電気 | ~$15 |
| Cloudflare Tunnel | $0 |
| Misc (= Recall.ai / Vapi) | $20-50 |
| **合計** | **~$140-280** |

Phase 1b (= 200 名 active) 想定で 2-3x 増、Phase 1c (= 500 名) で 5-10x 増。

---

# 6. Differentiators (= 他 PJ との比較で勝てる軸)

## 6.1 vs Reid AI (= Reid Hoffman digital twin)

| 項目 | Reid AI | 海山 PJ |
|---|---|---|
| Target | 1 人 (= Reid 本人発信) | 海山 + 社員 500 名集団 |
| 機能 | text + voice + video + 74 言語 | text + (voice 準備済) |
| 業務組込 | 講演 / Q&A focus | **業務 KPI 直結 + 集団** |
| 失敗回復 | unknown | **ADR + failure-log + audit loop** |

→ Reid は output 多様性で勝、海山は **業務統合 + 集団対話 + 規律** で勝。

## 6.2 vs Delphi.ai (= Digital Mind SaaS)

| 項目 | Delphi | 海山 PJ |
|---|---|---|
| Target | 経営者 / 専門家 個別 | OWNDAYS 海山特化 → 限定配布 |
| 入力 source | docs/web/SNS/video | 売上 / Drive / LINE / 議事録 / メモ |
| Reasoning capture | knowledge graph (= claimed) | retrieval + LLM glue 止まり |
| Question driven 補完 | 強 | 弱 (= alignment_interview 単発) |
| 集団対話 | 1:1 のみ | **group + per-group context** |
| 業務 KPI | ✗ | **✓ 売上 fact 厳格性** |

→ Delphi は reasoning + question driven で勝、海山は **集団 + 業務 + 規律** で勝。

## 6.3 vs 一般 enterprise chat bot

| | 一般 | 海山 PJ |
|---|---|---|
| Personality | corporate generic | **経営者 1 人特化** |
| Memory depth | session 内 | **長期累積 + date metadata** |
| Quality loop | none / superficial | **多層 + 海山 audit center** |
| 失敗 recovery | manual | **自動 alert + remediation hint** |
| ROI tracking | none | **dashboard 化** |

## 6.4 vs Anthropic / OpenAI internal AI tools

| | top-tier | 海山 PJ |
|---|---|---|
| Team size | 10-50 専属 | 1 + LLM pair |
| Budget | $10M+ | $0-100K |
| Production years | 数年 battle-tested | pre-launch |
| Multi-region | ✓ | ✗ (= single Mac Studio) |
| 独自軸 | 多様 | **集団 + 業務 + 規律 + audit** |

→ top-tier は scale + reliability で勝、海山は **独自軸 + 速度** で勝。

---

# 7. Known Gaps (= 正直)

## 7.1 Technical gaps
- **Single host Mac Studio = SPOF** (HA / backup 未整備)
- **Voice production deploy 未** (= 「準備済」止まり)
- **Video avatar 0**
- **多言語 0** (= 日本語のみ、海外店舗用途で必要)
- **Reasoning pattern capture 弱** (= retrieval + LLM glue 止まり、Delphi 主張の knowledge graph reasoning 未)
- **Question driven 補完 弱** (= alignment_interview 単発、loop 不十分)
- **A/B test framework 未稼働** (= build 済だが measurement 0)
- **Load test 未実施** (= 500 user 同時 access 挙動 unknown)
- **Service Account 化未** (= Google OAuth user credentials dependency)

## 7.2 Organizational gaps
- **Bus factor = 1** (= 海山 + Claude のみ、引継ぎ untested)
- **No formal compliance** (= GDPR / SOC 2 未取得)
- **No team scaling plan** (= 横展開時の operations 人員)
- **No monetization model** (= 商業化 vs 限定配布の意思決定未確定)

## 7.3 Strategic gaps
- **Phase 3 意思決定モード architecture 未着手**
  - 現状 retrieval + style mimicry、新規 situation で degrade
  - judgment generation model が别 layer 必要
- **Distribution strategy 未** (= 200 名 active 化の adoption push 計画 0)
- **「LLM 一般進化 vs 海山特化」 moat 不明** (= LLM 性能向上で commodity 化 risk)

---

# 8. Merger / Collaboration Angles

## 8.1 海山 PJ が「持ち込める」 もの

| 持ち込み | 価値 |
|---|---|
| OWNDAYS 業務 KPI 統合 framework | 他 SaaS で売上 / 業務 DB 直結する template |
| 集団対話 (= per-group memory + silent listen) | LINE WORKS 経由 group bot のリファレンス実装 |
| 1-click audit closed loop UI | LLM bot 品質保証の center pattern |
| 失敗 log + ADR + cross-check subagent rule | LLM agent 開発 process discipline の framework |
| 多層 self-evaluation loop | bot 品質 監視 framework |
| 統合 Review Dashboard | LLM 系 review UX の reference |
| Phase 0 → 3 段階 plan | 経営者 AI clone の rollout playbook |

## 8.2 海山 PJ が「補完されたい」 もの

| 補完候補 | 提供元候補 | 価値 |
|---|---|---|
| Reasoning pattern capture (= knowledge graph) | Delphi.ai / 連想 graph DB SaaS | Phase 3 意思決定モード前提、自前 build 重い |
| Voice clone production | ElevenLabs / Resemble.ai / Vapi 深統合 | Phase 2 Meet 参加で必須 |
| Video avatar | HeyGen / Synthesia / D-ID | Phase 2-3 候補 |
| 多言語 LLM + TTS | OpenAI / Anthropic 多言語 fine-tune | 海外店舗展開で必須 |
| Knowledge ingest 自動化 (= Slack / Gmail / 評価制度 etc) | Glean / Hebbia / Notion AI 系 | ingest 加速 |
| Question driven 補完 UX | Delphi 風 onboarding | gap fill loop 強化 |
| HA / multi-region infra | AWS / GCP / Cloudflare Workers | Phase 1c+ scale |
| Compliance (= GDPR / SOC 2) | external audit firm | enterprise 顧客向け |

## 8.3 合併で価値が生まれそうな PJ type

### Type A: 経営者特化 AI clone 系
- 例: Delphi.ai / Reid AI 同系統 SaaS
- 価値: **海山特化 implementation を Delphi-like flexibility で**
- risk: 海山個別 contextualization が薄まる
- 判断: **借りる方向**で、合併は不要 (= per-instance 配布なら自前で十分)

### Type B: Enterprise AI search / RAG 系
- 例: Glean / Hebbia / Sana Labs
- 価値: **業務 doc ingest 加速** (= Slack / Gmail / Notion 統合)
- risk: 一般化で OWNDAYS 文脈が薄まる
- 判断: **partner として ingest layer 借りる**

### Type C: Voice / Video / Avatar 系
- 例: HeyGen / ElevenLabs / Vapi / Synthesia
- 価値: **Phase 2-3 channel 即拡張**
- risk: lock-in
- 判断: **API 統合**で十分、合併不要

### Type D: 知識 graph / Reasoning capture 系
- 例: Causaly / Komodo / SciKnow (research 寄り)
- 価値: **Phase 3 意思決定 model の core component**
- risk: 実用品が少ない (= 研究フェーズの製品多数)
- 判断: **2-3 年後の再評価** (= 今は時期早い)

### Type E: 他経営者 AI PJ (= 横展開先)
- 例: 知り合い経営者の自社 AI 構想
- 価値: **海山型 framework を pilot deploy + 改善 feedback**
- risk: support burden
- 判断: **限定配布の 1 件目候補**、合併じゃなく**ライセンス供与 + co-development**

### Type F: 業務 SaaS (= Notion / Slack / Microsoft 365)
- 例: Notion AI / Slack AI / Microsoft Copilot
- 価値: **distribution + 既存 user base への組込**
- risk: 「経営者特化」 angle 失う
- 判断: **partnership で integration 提供**、合併じゃなく API plug-in

### Type G: LLM infra 提供元 (= Anthropic / OpenAI / Google)
- 価値: 優遇 access + 共同開発 + reference customer 化
- risk: dependency 強化
- 判断: **partnership 強化**、合併じゃなく case study 提供 + early access

## 8.4 合併 / 統合検討の判断 framework

```
合併 / 統合 価値 = Σ {
  (相手 capability × 海山 PJ 不足度) +    # 補完性
  (海山 capability × 相手不足度) +         # 提供性
  (distribution synergy) +                  # adoption push
  (cost saving) -                          # 共通 infra
  (integration cost) -                     # 統合工数
  (lock-in risk) -                         # 依存性
  (cultural mismatch)                      # 経営者個人化 dilution
}
```

優先 score (= 私の判断):
1. **B (= enterprise ingest) - partnership** (= score 高)
2. **C (= voice/video) - API 統合** (= score 中)
3. **E (= 他経営者横展開) - co-development** (= score 中)
4. **G (= LLM infra) - early access** (= score 中)
5. F (= 業務 SaaS) - 慎重に partnership
6. A (= 競合経営者 AI SaaS) - 借りるが合併不要
7. D (= 知識 graph) - 時期早

---

# 9. Stewardship & Contact

## 9.1 Project lead
- **海山丈司** (= OWNDAYS CEO)
- 主用途 / vision owner

## 9.2 Dev pair
- **Claude (= Anthropic Opus 4.8)** + **海山** (= LLM pair coding)
- 開発 cycle: 海山指示 → Claude design + build + test + commit
- 規律: CLAUDE.md 1.15 cross-check subagent rule

## 9.3 Infrastructure
- Mac Studio (= bot host, 海山所有)
- Mac mini (= auto_deploy + scrape)
- Cloudflare Tunnel (= public endpoint)
- GitHub private repo

## 9.4 合併ブレスト 検討依頼時の連絡経路
- 海山 LINE (= 個人)
- email (= 別途)

---

# 10. Appendix

## 10.1 主要 file 構造

```
brain-agent/
├── CLAUDE.md                       # 開発規律 (= 250 行)
├── main.py                         # FastAPI webhook server (~2,400 行)
├── brain_wiki.py                   # core retrieval + clone respond (~4,400 行)
├── brain_wiki_helpers/             # pure functions
│   ├── rerank.py                   # Cohere Rerank
│   ├── contextual.py               # Contextual chunks
│   └── ...
├── clone_history.py                # 1:1 履歴 (channel_id 対応、~200 行)
├── clone_memory.py                 # per-user memory (~210 行)
├── clone_group_context.py          # per-group context (Tier 0、~210 行)
├── clone_audit.py                  # 海山 audit (Feature 3、~354 行)
├── clone_feedback.py               # 社員修正希望 (~400 行)
├── clone_learning.py               # 会話発見 (~600 行)
├── privacy_gate.py                 # 3 段 filter
├── lineworks_bot.py                # LINE WORKS API (~480 行)
├── gdrive_sync.py                  # Google Drive 取込 (~590 行)
├── content_extractor.py            # PDF/Office/image text 抽出 (~900 行)
├── routes/
│   ├── brain_api.py                # 30+ admin endpoint (~1,550 行)
│   └── alignment_trial.py
├── services/
│   ├── auth.py                     # admin user_id 検証
│   ├── drive_ingest.py             # Drive URL on-demand fetch
│   ├── usage_analytics.py          # Feature 2 dashboard
│   └── review_dashboard.py         # Feature 6 統合 review (~658 行)
├── scripts/
│   ├── ai_research_agent.py        # Feature 5 (= 544 行)
│   ├── bot_uptime_monitor.py       # 5 min health check (~500 行)
│   ├── bot_events.py + bot_metrics.py
│   ├── clone_sleep_time_agent.py
│   ├── eval_runner.py
│   ├── clone_*.py                  # 多数の self-improvement script
│   ├── auto_deploy.sh              # Mac mini cron 5 min
│   └── cron_install.sh             # 冪等 crontab 自動登録
├── tests/smoke/                    # 546 test passing
├── data/brain/                     # 永続 data (= git ignored)
│   ├── wiki/                       # compile 出力
│   ├── raw/                        # 取込 raw
│   ├── clone_history/              # 会話ログ
│   ├── clone_memory/               # per-user .md
│   ├── clone_group_context/        # per-group .md (新)
│   ├── clone_audit/                # 海山 audit log (新)
│   ├── ai_research/                # research digests (新)
│   ├── bot_events/events.jsonl
│   └── alignment/eval_set_v1.json
└── docs/
    ├── ARCHITECTURE.md
    ├── REVIEW_CHECKLIST.md
    ├── failure-log.md
    ├── runbook.md
    ├── decisions/                  # 14 ADR
    ├── integrations/
    └── PJ_OVERVIEW.md              # 本 document
```

## 10.2 直近 build 履歴 (= 直近 1 セッション)

| commit | content | 行数 |
|---|---|---|
| b0f3c5c | Plan C v2 Step 2 — Cohere Rerank v3.5 | +280 |
| bb24745 | /api/admin/redeploy + docker.sock mount | +250 |
| 4054e17 | eval in-process bot fix | +30 |
| 269b280 | Tier 0 LINE WORKS group 対応 | +1,034 |
| f6c6148 | LINE WORKS `<m userId="...">` mention 対応 | +193 |
| 454d4e0 | Google Workspace 文言厳密化 | +12 |
| 075d4c7 | Feature 1: Failure notifier 拡張 | +390 |
| be01573 | Feature 2: Usage analytics dashboard | +681 |
| eccc168 | Feature 3: 海山 daily audit UI | +755 |
| 71b5e85 | Feature 4: Tier 1 memory | +102 |
| d76d71a | Feature 5: AI Research Agent | +960 |
| 7226dd9 | Feature 6: 統合 Review Dashboard | +984 |
| **合計** | | **+5,671 行** |

## 10.3 比較対象 PJ summary (= Reid AI / Delphi.ai)

参考までに比較した既存 PJ:

| PJ | 1 行説明 | 公開情報 |
|---|---|---|
| Reid AI | LinkedIn 創業者 Reid Hoffman の AI 分身、講演 + 多言語発信 | WSJ 報道 75+ 講演 / 74 言語 / 本人時間 50% 削減 |
| Delphi.ai | Digital Mind SaaS、経営者 / 専門家 / コーチ向け | Sequoia 系出資、knowledge graph reasoning 主張 |

---

# Closing

この document は合併 / 統合 brainstorming の base material として作成されました。

**海山 PJ の本質**:
- 「経営者 1 人の AI clone」 という同 peer set にいる Reid AI / Delphi.ai と比較すると
- 海山 PJ は「**集団対話 + 業務 KPI 統合 + 規律 + audit loop**」 という独自軸で **第 3 の positioning**
- voice / video / 多言語 / reasoning depth で劣後、ただし catch up 可能
- bus factor / distribution / 商業性は未確定

合併検討時には:
- 「補完性」 (= 我々の不足を埋める) を優先
- 「distribution synergy」 (= 他社 user base 統合)
- 「lock-in / cultural mismatch」を慎重に避ける
- **per-instance 配布 strategy を keep する** (= SaaS multi-tenant 化に流されない)

ご質問 / 詳細 / 個別議論は 海山経由でお声がけください。
