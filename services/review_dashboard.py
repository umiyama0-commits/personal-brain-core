"""review_dashboard.py — 統合 Review Dashboard (★2026-05-24 v2 redesign).

# 役割

LINE で散らばっていた review 系コマンドを **Web UI 1 ヶ所に統合**:
- 会話発見ダイジェスト (clone_learning)
- 社員 修正希望 (clone_feedback)
- 海山 audit (clone_audit) — 3 actions (Accept / Reject / コメント付き修正)
- AI Research 提案 (ai_research_agent)
- 個別 memory / group context 閲覧

# v2 改修 (= 海山指示 2026-05-24)

- 全体 design 刷新 (= refined / minimal / sophisticated、Linear.app / Vercel 風)
- Top page に Usage summary + 日別利用数 SVG chart 追加
- Audit に Accept/Reject/コメント付き修正 3 actions
- Learning / Feedback に comment field 常時露出 (= status 変更なし comment back 可)
- action endpoint も comment param 対応

# URL 構造

- /admin/review                  = Top (= 全 queue summary + usage chart)
- /admin/review/learning         = 会話発見 + comment field
- /admin/review/feedback         = 社員修正希望 + comment field
- /admin/review/audit            = audit (Top + 3 actions)
- /admin/review/research         = AI research
- /admin/review/memory           = clone_memory 閲覧
- /admin/review/group            = group_context 閲覧
- /admin/review/<queue>/action   = POST endpoint (action + note 受付)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))


# ─── design system v3 (= Claude Design aesthetic、★2026-05-24 海山指示) ────
# 参考: claude.ai web interface、Anthropic ブランドガイド
# - cream / warm off-white background
# - terracotta accent (= warmth、専門性)
# - serif heading (= 文芸的、思考体系の表現)
# - quiet palette、generous whitespace
def _shared_css() -> str:
    return """
<style>
:root {
  --bg: #f5f1eb;
  --surface: #faf9f5;
  --surface-2: #efe9dc;
  --border: #e8e3d4;
  --border-soft: #efe9dc;
  --text: #191919;
  --text-2: #5c5c57;
  --text-3: #8a857a;
  --accent: #cc785c;
  --accent-hover: #b66a4f;
  --accent-soft: #f5e3d8;
  --success: #5d7553;
  --success-soft: #e1e8d8;
  --warning: #bc7d29;
  --warning-soft: #f2e1bf;
  --danger: #a8362a;
  --danger-soft: #f0d8d2;
  --shadow-sm: 0 1px 2px rgba(60, 40, 20, 0.04);
  --shadow: 0 1px 3px rgba(60, 40, 20, 0.06), 0 1px 2px rgba(60, 40, 20, 0.04);
  --radius: 8px;
  --radius-sm: 6px;
  --font-serif: ui-serif, "Iowan Old Style", "Apple Garamond", Georgia, Cambria, "Times New Roman", Times, serif;
  --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Helvetica Neue", sans-serif;
}

* { box-sizing: border-box; }
*:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

body {
  font-family: var(--font-sans);
  font-size: 14.5px;
  line-height: 1.6;
  color: var(--text);
  background: var(--bg);
  margin: 0;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.container {
  max-width: 920px;
  margin: 0 auto;
  padding: 32px 22px 80px;
}

/* ─── Navigation (= refined、quiet) ─── */
nav.top-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  margin-bottom: 36px;
  font-size: 13.5px;
  border-bottom: 1px solid var(--border);
  padding-bottom: 14px;
}
nav.top-nav a {
  color: var(--text-2);
  text-decoration: none;
  padding: 6px 14px 6px 0;
  margin-right: 6px;
  font-weight: 400;
  transition: color 0.15s;
  border-bottom: 2px solid transparent;
  margin-bottom: -16px;
  padding-bottom: 14px;
}
nav.top-nav a:hover { color: var(--text); }
nav.top-nav a.current {
  color: var(--accent);
  font-weight: 500;
  border-bottom-color: var(--accent);
}

/* ─── Headings (= serif で文芸的) ─── */
h1 {
  font-family: var(--font-serif);
  font-size: 34px;
  font-weight: 500;
  letter-spacing: -0.015em;
  margin: 4px 0 4px;
  color: var(--text);
  line-height: 1.2;
}
h1 + .subtitle {
  color: var(--text-2);
  font-size: 14px;
  margin-bottom: 36px;
  font-family: var(--font-sans);
}
h2 {
  font-family: var(--font-serif);
  font-size: 22px;
  font-weight: 500;
  letter-spacing: -0.01em;
  margin: 44px 0 18px;
  color: var(--text);
  display: flex;
  align-items: baseline;
  gap: 10px;
}
h2 .count-badge {
  font-family: var(--font-sans);
  font-size: 11px;
  font-weight: 500;
  background: var(--surface-2);
  color: var(--text-2);
  padding: 2px 9px;
  border-radius: 999px;
  letter-spacing: 0.02em;
}
h3 {
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 600;
  margin: 18px 0 8px;
  color: var(--text);
}

/* ─── KPI grid (= 柔らかい cream surface、quiet) ─── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 10px;
  margin: 14px 0 8px;
}
.kpi {
  background: var(--surface);
  padding: 16px 18px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  transition: border-color 0.15s, transform 0.15s;
}
.kpi:hover {
  border-color: var(--border);
  transform: translateY(-1px);
}
.kpi .label {
  font-size: 11.5px;
  color: var(--text-2);
  letter-spacing: 0.02em;
  font-weight: 400;
  margin-bottom: 6px;
  font-family: var(--font-sans);
}
.kpi .value {
  font-family: var(--font-serif);
  font-size: 28px;
  font-weight: 500;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: var(--text);
  line-height: 1.05;
}
.kpi .unit {
  font-size: 14px;
  color: var(--text-3);
  margin-left: 3px;
  font-weight: 400;
  font-family: var(--font-sans);
}
.kpi.accent .value { color: var(--accent); }
.kpi.success .value { color: var(--success); }
.kpi.warning .value { color: var(--warning); }
.kpi.danger .value { color: var(--danger); }

/* ─── Progress bar ─── */
.progress-card {
  background: var(--surface);
  padding: 20px 22px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  margin: 14px 0;
}
.progress-card .head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 14px;
}
.progress-card .label {
  font-size: 13.5px;
  color: var(--text-2);
  font-weight: 400;
}
.progress-card .pct {
  font-family: var(--font-serif);
  font-size: 26px;
  font-weight: 500;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: var(--accent);
}
.progress-bar {
  background: var(--surface-2);
  border-radius: 999px;
  height: 6px;
  overflow: hidden;
  position: relative;
}
.progress-bar .fill {
  height: 100%;
  background: linear-gradient(90deg, var(--accent) 0%, #d68a6c 100%);
  border-radius: 999px;
  transition: width 0.5s ease;
}
.progress-card .meta {
  color: var(--text-3);
  font-size: 11.5px;
  margin-top: 10px;
}

/* ─── Chart (= 暖色のひかえめ chart) ─── */
.chart-card {
  background: var(--surface);
  padding: 22px 24px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  margin: 14px 0;
}
.chart-card .chart-title {
  font-size: 12.5px;
  color: var(--text-2);
  font-weight: 400;
  margin-bottom: 14px;
  letter-spacing: 0.02em;
}
.chart-svg { width: 100%; height: 170px; display: block; }

/* ─── 調査メモ (cost 要因) ─── */
.memo-box {
  background: var(--surface);
  border: 1px solid var(--border-soft);
  border-left: 3px solid var(--accent);
  border-radius: var(--radius);
  padding: 18px 22px;
  margin: 14px 0;
}
.memo-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 12px;
  letter-spacing: -0.005em;
}
.memo-list {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-2);
}
.memo-list li { margin-bottom: 8px; }
.memo-list b { color: var(--text); font-weight: 600; }
.memo-fix {
  display: inline-block;
  margin-top: 2px;
  font-size: 12px;
  color: #16a34a;
}
.memo-foot {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border-soft);
  font-size: 11.5px;
  color: var(--text-3);
  line-height: 1.6;
}

/* ─── Item cards ─── */
.item {
  background: var(--surface);
  padding: 20px 22px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  margin: 12px 0;
  transition: border-color 0.15s;
}
.item:hover { border-color: var(--border); }
.item-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 12px;
}
.item-title {
  font-weight: 500;
  font-size: 14.5px;
  letter-spacing: -0.005em;
  color: var(--text);
  line-height: 1.5;
}
.item-meta {
  font-size: 11.5px;
  color: var(--text-3);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.item-body {
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--text-2);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--bg);
  padding: 12px 16px;
  border-radius: var(--radius-sm);
  margin: 10px 0;
  border-left: 2px solid var(--border);
}
.item-body strong { color: var(--text); font-weight: 600; }

/* ─── Badge / tag (= refined、quiet pill) ─── */
.tag {
  display: inline-block;
  font-size: 10.5px;
  font-weight: 500;
  padding: 2px 9px;
  border-radius: 999px;
  letter-spacing: 0.02em;
  vertical-align: middle;
  font-family: var(--font-sans);
}
.tag.good { background: var(--success-soft); color: var(--success); }
.tag.bad { background: var(--danger-soft); color: var(--danger); }
.tag.fix { background: var(--warning-soft); color: var(--warning); }
.tag.pending { background: var(--accent-soft); color: var(--accent); }
.tag.neutral { background: var(--surface-2); color: var(--text-2); }

/* ─── Action area ─── */
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
  align-items: flex-end;
}
.actions form {
  display: flex;
  gap: 6px;
  align-items: flex-end;
  margin: 0;
}
.comment-row {
  flex: 1 1 100%;
  display: flex;
  gap: 6px;
  margin-bottom: 4px;
}
.comment-row input {
  flex: 1;
  font: inherit;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  transition: border-color 0.12s;
}
.comment-row input:focus {
  outline: none;
  border-color: var(--accent);
}

button.btn, a.btn {
  padding: 8px 16px;
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--text);
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: all 0.15s;
  white-space: nowrap;
}
button.btn:hover, a.btn:hover {
  background: var(--surface-2);
  border-color: var(--text-3);
}
button.btn.accept {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
button.btn.accept:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
button.btn.reject {
  background: var(--surface);
  color: var(--danger);
  border-color: var(--danger-soft);
}
button.btn.reject:hover { background: var(--danger-soft); border-color: var(--danger); }
button.btn.fix {
  background: var(--warning-soft);
  color: var(--warning);
  border-color: var(--warning);
}
button.btn.fix:hover { background: var(--warning); color: #fff; }
button.btn.comment {
  background: var(--surface);
  color: var(--accent);
  border-color: var(--accent);
}
button.btn.comment:hover { background: var(--accent-soft); }
button.btn.ghost {
  background: transparent;
  border-color: var(--border);
}

/* ─── Tables (= refined、boundary 控えめ) ─── */
table {
  border-collapse: separate;
  border-spacing: 0;
  width: 100%;
  font-size: 13.5px;
  background: var(--surface);
  border-radius: var(--radius);
  overflow: hidden;
  border: 1px solid var(--border-soft);
  margin: 12px 0;
}
th, td {
  padding: 12px 16px;
  text-align: left;
  border-bottom: 1px solid var(--border-soft);
}
tr:last-child td { border-bottom: none; }
th {
  background: var(--bg);
  font-weight: 500;
  font-size: 11.5px;
  color: var(--text-2);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
tbody tr {
  cursor: default;
  transition: background 0.1s;
}
tbody tr.clickable {
  cursor: pointer;
}
tbody tr.clickable:hover { background: var(--surface-2); }
td code {
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 11.5px;
  background: var(--bg);
  padding: 2px 7px;
  border-radius: 4px;
  color: var(--accent);
}
td a {
  color: var(--accent);
  text-decoration: none;
  font-weight: 500;
}
td a:hover { text-decoration: underline; }

/* ─── Empty state ─── */
.empty {
  padding: 48px 24px;
  text-align: center;
  color: var(--text-3);
  background: var(--surface);
  border-radius: var(--radius);
  border: 1px dashed var(--border);
  font-size: 13.5px;
  line-height: 1.7;
}

/* ─── Flash ─── */
.flash {
  background: var(--accent-soft);
  color: var(--accent);
  padding: 12px 18px;
  border-radius: var(--radius-sm);
  margin-bottom: 20px;
  font-size: 13.5px;
  font-weight: 500;
  border-left: 3px solid var(--accent);
}

/* ─── Quick links ─── */
.quick-links {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 16px 0 8px;
}
.quick-link {
  background: var(--surface);
  color: var(--text);
  text-decoration: none;
  padding: 12px 18px;
  border-radius: var(--radius-sm);
  font-size: 13.5px;
  font-weight: 500;
  border: 1px solid var(--border-soft);
  display: inline-flex;
  align-items: center;
  gap: 8px;
  transition: all 0.15s;
}
.quick-link:hover {
  border-color: var(--accent);
  color: var(--accent);
}

/* ─── 会話 history bubbles (= per-user 詳細 page) ─── */
.chat {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-top: 20px;
}
.chat-turn {
  display: flex;
  flex-direction: column;
  max-width: 80%;
}
.chat-turn.user { align-self: flex-end; align-items: flex-end; }
.chat-turn.assistant { align-self: flex-start; align-items: flex-start; }
.bubble {
  padding: 12px 16px;
  border-radius: 14px;
  font-size: 13.5px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.chat-turn.user .bubble {
  background: var(--accent-soft);
  color: var(--text);
  border-bottom-right-radius: 4px;
}
.chat-turn.assistant .bubble {
  background: var(--surface);
  color: var(--text);
  border: 1px solid var(--border-soft);
  border-bottom-left-radius: 4px;
}
.chat-meta {
  font-size: 10.5px;
  color: var(--text-3);
  margin: 4px 6px;
  font-variant-numeric: tabular-nums;
}

/* ─── User card (= memory list clickable) ─── */
.user-card {
  background: var(--surface);
  padding: 16px 20px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  margin: 10px 0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  transition: all 0.15s;
  text-decoration: none;
  color: var(--text);
}
.user-card:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
}
.user-card .user-info { flex: 1; }
.user-card .user-name {
  font-weight: 500;
  font-size: 14.5px;
  color: var(--text);
}
.user-card .user-id-code {
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 11px;
  color: var(--text-3);
  margin-top: 2px;
}
.user-card .user-stats {
  display: flex;
  gap: 24px;
  align-items: center;
}
.user-card .stat {
  text-align: right;
}
.user-card .stat-value {
  font-family: var(--font-serif);
  font-size: 18px;
  font-weight: 500;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.user-card .stat-label {
  font-size: 10.5px;
  color: var(--text-3);
  letter-spacing: 0.02em;
}
.user-card .arrow {
  color: var(--text-3);
  font-size: 18px;
  margin-left: 16px;
  transition: transform 0.15s, color 0.15s;
}
.user-card:hover .arrow {
  color: var(--accent);
  transform: translateX(3px);
}

/* ─── Footer ─── */
.footer-meta {
  color: var(--text-3);
  font-size: 11.5px;
  text-align: center;
  margin-top: 64px;
  padding-top: 24px;
  border-top: 1px solid var(--border-soft);
  font-family: var(--font-serif);
  font-style: italic;
}
.footer-meta a { color: var(--text-3); text-decoration: none; }
.footer-meta a:hover { color: var(--accent); }

/* ─── Memory dump (= /-formatted) ─── */
.memory-dump {
  background: var(--surface);
  padding: 24px 28px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  margin: 16px 0;
  font-family: var(--font-sans);
  font-size: 13.5px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.memory-dump strong { color: var(--text); }
.memory-dump h3 {
  font-family: var(--font-serif);
  font-size: 16px;
  font-weight: 500;
  color: var(--accent);
  margin: 14px 0 6px;
}

/* ─── Mobile ─── */
@media (max-width: 640px) {
  .container { padding: 16px 14px 48px; }
  nav.top-nav { font-size: 12px; }
  nav.top-nav a { padding: 6px 10px; }
  h1 { font-size: 22px; }
  .kpi .value { font-size: 22px; }
  .item-head { flex-direction: column; gap: 4px; }
  .actions form { flex-wrap: wrap; }
}
</style>
"""


def _nav(current: str, token: str) -> str:
    pages = [
        ("/admin/review", "Top"),
        ("/admin/review/research", "Research"),
        ("/admin/review/audit", "Audit"),
        ("/admin/review/learning", "発見"),
        ("/admin/review/feedback", "修正希望"),
        ("/admin/review/system", "システム修正"),  # ★2026-05-25 海山指示
        ("/admin/review/voice-align", "音声align"),  # ★2026-05-26 海山指示
        ("/admin/review/quality", "品質"),  # ★2026-05-26 海山 C2+C3
        ("/admin/review/style-reflux", "Style逆流"),  # ★2026-05-26 海山 B1+B3
        ("/admin/review/web-clip", "Web 取込"),  # ★2026-05-26 海山指示
        ("/admin/review/data-gaps", "データ拡充"),  # ★2026-05-26 海山指示
        ("/admin/review/conversation-success", "成功事例"),  # ★2026-05-26 海山指示
        ("/admin/review/memory", "Memory"),
        ("/admin/review/group", "Group"),
        ("/admin/usage", "Usage"),
        ("/admin/review/cost", "API料金"),  # ★2026-05-29 海山指示「API料金トラック」
    ]
    links = []
    for url, label in pages:
        cls = "current" if url == current else ""
        sep = "&" if "?" in url else "?"
        links.append(f'<a href="{url}{sep}token={token}" class="{cls}">{label}</a>')
    return f'<nav class="top-nav">{"".join(links)}</nav>'


def _html_envelope(title: str, subtitle: str, body: str, current: str, token: str,
                   flash: Optional[str] = None) -> str:
    flash_html = f'<div class="flash">✓ {flash}</div>' if flash else ""
    sub_html = f'<div class="subtitle">{subtitle}</div>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · うみやまAI</title>
{_shared_css()}
</head>
<body>
<div class="container">
{_nav(current, token)}
<h1>{title}</h1>
{sub_html}
{flash_html}
{body}
<div class="footer-meta">Personal Brain Review Dashboard v2 · <a href="/admin/usage?token={token}" style="color: var(--text-3);">Usage detail →</a></div>
</div>
</body>
</html>"""


def _escape(text: Any) -> str:
    if text is None:
        return ""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ─── プライバシー匿名化 (★2026-05-24 海山指示) ────────
# 「Dashboard 側では完全匿名化、社員 A・B・C 等で表示」 を厳守
# - backend (clone_memory.md / clone_history.jsonl) には実名残す
# - 表示 layer (= 本 module) で全て alias に置換
# - 同じ user_id は常に同じ alias (= stable)

def _alphabet_label(idx: int) -> str:
    """0 → A, 1 → B, ..., 25 → Z, 26 → AA, 27 → AB, ..."""
    if idx < 0:
        return "?"
    chars = []
    n = idx
    while True:
        chars.append(chr(ord("A") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(chars))


def _user_alias(user_id: Optional[str]) -> str:
    """user_id を「社員 A」「社員 B」... へ匿名化.

    user_id sorted 順で 0-indexed enumerate、A-Z 後は AA, AB...
    list_users() ベース (= clone_memory.md 持ってる user のみ)、
    未知 user_id は hash で安定 fallback。
    """
    if not user_id:
        return "社員 ?"
    try:
        import clone_memory
        users = clone_memory.list_users()
        sorted_ids = sorted(u.get("user_id", "") for u in users)
        if user_id in sorted_ids:
            idx = sorted_ids.index(user_id)
            return f"社員 {_alphabet_label(idx)}"
    except Exception:
        pass
    # fallback: hash-based stable label (= clone_memory に無い場合)
    import hashlib
    h = int(hashlib.sha1(str(user_id).encode("utf-8")).hexdigest(), 16) % 26
    return f"社員 {chr(ord('A') + h)}*"  # * は fallback marker


def _channel_alias(channel_id: Optional[str]) -> str:
    """channel_id を「グループ A」「グループ B」へ匿名化."""
    if not channel_id:
        return "グループ ?"
    try:
        import clone_group_context
        channels = clone_group_context.list_channels()
        sorted_ids = sorted(c.get("channel_id", "") for c in channels)
        if channel_id in sorted_ids:
            idx = sorted_ids.index(channel_id)
            return f"グループ {_alphabet_label(idx)}"
    except Exception:
        pass
    import hashlib
    h = int(hashlib.sha1(str(channel_id).encode("utf-8")).hexdigest(), 16) % 26
    return f"グループ {chr(ord('A') + h)}*"


def _mask_user_id(uid: Optional[str], show_chars: int = 6) -> str:
    """user_id を表示用 short hash (= 完全匿名化補助、debug 用).

    短縮表示 (= 先頭 N 文字 + …)、デバッグ時のみ参照、user identify には使わない。
    """
    if not uid:
        return "—"
    uid = str(uid)
    if len(uid) <= show_chars:
        return uid
    return f"{uid[:show_chars]}…"


# ─── Daily trend SVG chart ────────────────────────────
def _render_daily_chart(daily_trend: list[dict], width: int = 600, height: int = 160) -> str:
    """日別 query 数の area chart (= SVG inline、no JS).

    daily_trend: [{"date": "YYYY-MM-DD", "queries": int, "failures": int}, ...]
    """
    if not daily_trend:
        return '<div class="empty">グラフ表示用 data なし</div>'

    # 過去 7 日に絞る (= 海山指示 2026-05-25)
    data = daily_trend[-7:] if len(daily_trend) > 7 else daily_trend
    if not data:
        return '<div class="empty">data なし</div>'

    # padding inside viewBox
    pad_l, pad_r, pad_t, pad_b = 36, 16, 18, 24
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    queries = [d.get("queries", 0) for d in data]
    max_q = max(queries) if queries else 1
    max_q = max(max_q, 1)

    n = len(data)
    step = plot_w / max(n - 1, 1) if n > 1 else 0

    # path points
    points = []
    for i, q in enumerate(queries):
        x = pad_l + i * step
        y = pad_t + plot_h - (q / max_q) * plot_h
        points.append((x, y))

    if n == 1:
        # 1 点だけ: 中央 plot
        points = [(pad_l + plot_w / 2, pad_t + plot_h - (queries[0] / max_q) * plot_h)]

    # line path + area fill
    line_d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
    area_d = (
        line_d
        + f" L {points[-1][0]:.1f} {pad_t + plot_h:.1f}"
        + f" L {points[0][0]:.1f} {pad_t + plot_h:.1f} Z"
    )

    # circles for points
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#cc785c" stroke="#faf9f5" stroke-width="2"/>'
        for x, y in points
    )

    # x labels: 7 日以下は全日付、それ超は 最初 / 中間 / 最後 の 3 点
    if n <= 7:
        label_indices = list(range(n))
    else:
        label_indices = [0, n // 2, n - 1] if n >= 3 else list(range(n))
    x_labels = "".join(
        f'<text x="{pad_l + i * step:.1f}" y="{height - 6}" '
        f'text-anchor="middle" font-size="10.5" fill="#8a857a" '
        f'font-family="ui-sans-serif, sans-serif">'
        f'{data[i].get("date", "")[5:]}</text>'  # MM-DD only
        for i in label_indices
    )

    # y axis labels (= 0, max)
    y_labels = (
        f'<text x="{pad_l - 6}" y="{pad_t + 4}" text-anchor="end" '
        f'font-size="10.5" fill="#8a857a" font-family="ui-sans-serif, sans-serif">{max_q}</text>'
        f'<text x="{pad_l - 6}" y="{pad_t + plot_h + 4}" text-anchor="end" '
        f'font-size="10.5" fill="#8a857a" font-family="ui-sans-serif, sans-serif">0</text>'
    )

    return f"""<svg class="chart-svg" viewBox="0 0 {width} {height}" preserveAspectRatio="none">
<defs>
<linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
<stop offset="0%" stop-color="#cc785c" stop-opacity="0.22"/>
<stop offset="100%" stop-color="#cc785c" stop-opacity="0"/>
</linearGradient>
</defs>
<path d="{area_d}" fill="url(#areaGrad)" stroke="none"/>
<path d="{line_d}" stroke="#cc785c" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
{circles}
{x_labels}
{y_labels}
</svg>"""


# ─── data aggregation ────────────────────────────────
def aggregate_review_queues() -> dict:
    out = {}
    try:
        import clone_learning
        out["learning_pending"] = len(clone_learning.list_pending(limit=999))
    except Exception as e:
        logger.warning(f"clone_learning: {e}")
        out["learning_pending"] = None
    try:
        import clone_feedback
        out["feedback_pending"] = len(clone_feedback.list_pending(limit=999))
    except Exception as e:
        logger.warning(f"clone_feedback: {e}")
        out["feedback_pending"] = None
    try:
        import clone_audit
        stats = clone_audit.audit_stats(days=30)
        out["audit_total_30d"] = stats.get("n_total_audits", 0)
        out["audit_good_rate"] = stats.get("good_rate_pct", 0)
        out["audit_needs_attention"] = len(stats.get("needs_attention", []))
    except Exception as e:
        logger.warning(f"audit: {e}")
        out["audit_total_30d"] = None

    try:
        app_root = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
        prop_file = app_root / "data" / "brain" / "ai_research" / "proposals.jsonl"
        if prop_file.exists():
            n = 0
            for ln in prop_file.read_text(encoding="utf-8").splitlines():
                if not ln.strip(): continue
                try:
                    if json.loads(ln).get("status") == "pending":
                        n += 1
                except Exception: continue
            out["research_pending"] = n
        else:
            out["research_pending"] = 0
    except Exception:
        out["research_pending"] = None

    # Usage data
    try:
        import sys as _sys
        scripts_path = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_path not in _sys.path:
            _sys.path.insert(0, scripts_path)
        from services.usage_analytics import aggregate_usage
        usage = aggregate_usage(since_sec=86400 * 30)
        out["usage"] = usage
    except Exception as e:
        logger.warning(f"usage aggregate: {e}")
        out["usage"] = None

    return out


# ─── Top page (= 全 queue summary + usage + chart) ──
def render_top_page(token: str, flash: Optional[str] = None) -> str:
    data = aggregate_review_queues()
    usage = data.get("usage") or {}
    summary = usage.get("summary", {})
    channel = usage.get("channel_split", {})
    roi = usage.get("roi_progress", {})
    daily_trend = usage.get("daily_trend", [])

    def kpi(label: str, value: Any, unit: str = "", cls: str = "") -> str:
        cls_str = f" {cls}" if cls else ""
        v = "—" if value is None else f"{value:,}" if isinstance(value, int) else value
        u = f'<span class="unit">{unit}</span>' if unit else ""
        return (f'<div class="kpi{cls_str}">'
                f'<div class="label">{label}</div>'
                f'<div class="value">{v}{u}</div>'
                f'</div>')

    progress_pct = roi.get("progress_pct", 0)
    pace = roi.get("current_pace_estimate_monthly", 0)

    # Pending KPIs
    audit_needs = data.get("audit_needs_attention") or 0
    learning_cls = "warning" if (data.get("learning_pending") or 0) > 0 else ""
    feedback_cls = "warning" if (data.get("feedback_pending") or 0) > 0 else ""
    research_cls = "accent" if (data.get("research_pending") or 0) > 0 else ""
    audit_cls = "danger" if audit_needs >= 5 else ("warning" if audit_needs > 0 else "")

    chart_html = _render_daily_chart(daily_trend) if daily_trend else '<div class="empty">data なし</div>'

    # ★2026-05-26 海山指示: 数字でも見えるよう 過去 7 日 numeric table を追加
    daily_numeric_rows = ""
    if daily_trend:
        recent7 = daily_trend[-7:]
        # 新しい順 (= 右から左) で並べる、最新が左
        for d in reversed(recent7):
            ds = d.get("date", "")
            qs = d.get("queries", 0)
            fs = d.get("failures", 0)
            aus = d.get("automated", 0)  # ★2026-06-09: batch/eval は実利用と別表示
            mmdd = ds[5:] if len(ds) >= 10 else ds
            wd = ""
            try:
                from datetime import date as _date
                _d = _date.fromisoformat(ds)
                wd = ["月", "火", "水", "木", "金", "土", "日"][_d.weekday()]
            except Exception:
                pass
            fail_part = f" <span style='color:var(--danger);font-size:12px;'>fail {fs}</span>" if fs else ""
            daily_numeric_rows += (
                f"<tr>"
                f"<td style='padding:6px 12px;font-variant-numeric:tabular-nums;'>{_escape(mmdd)} ({wd})</td>"
                f"<td style='padding:6px 12px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600;'>{qs}</td>"
                f"<td style='padding:6px 12px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-3);font-size:12px;'>{aus if aus else ''}</td>"
                f"<td style='padding:6px 12px;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-3);'>{fail_part}</td>"
                f"</tr>"
            )
    daily_numeric_table = (
        f'<table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:13px;">'
        f'<thead style="background:var(--bg);"><tr>'
        f'<th style="text-align:left;padding:6px 12px;color:var(--text-3);">日付 (曜)</th>'
        f'<th style="text-align:right;padding:6px 12px;color:var(--text-3);">回答数 (実)</th>'
        f'<th style="text-align:right;padding:6px 12px;color:var(--text-3);font-size:11px;">自動 (batch/eval)</th>'
        f'<th style="text-align:right;padding:6px 12px;color:var(--text-3);">fail</th>'
        f'</tr></thead><tbody>{daily_numeric_rows}</tbody></table>'
    ) if daily_numeric_rows else ""

    body = f"""
<h2>📋 Pending review <span class="count-badge">{(data.get('learning_pending') or 0) + (data.get('feedback_pending') or 0) + (data.get('research_pending') or 0) + audit_needs}</span></h2>
<div class="kpi-grid">
{kpi("発見", data.get("learning_pending"), "件", learning_cls)}
{kpi("修正希望", data.get("feedback_pending"), "件", feedback_cls)}
{kpi("Research 提案", data.get("research_pending"), "件", research_cls)}
{kpi("Audit attention", audit_needs, "件", audit_cls)}
</div>
<div class="quick-links">
  <a class="quick-link" href="/admin/review/research?token={token}">→ Research</a>
  <a class="quick-link" href="/admin/review/audit?token={token}">→ Audit</a>
  <a class="quick-link" href="/admin/review/learning?token={token}">→ 発見</a>
  <a class="quick-link" href="/admin/review/feedback?token={token}">→ 修正希望</a>
</div>

<h2>📈 Phase 1 ROI Progress</h2>
<div class="progress-card">
  <div class="head">
    <div class="label">月間 pace (= 過去 30 日換算)</div>
    <div class="pct">{progress_pct}%</div>
  </div>
  <div class="progress-bar"><div class="fill" style="width: {min(100, progress_pct)}%;"></div></div>
  <div class="meta">{pace:,} / 1,000 query (Phase 1 milestone) · ultimate 10,000/月</div>
</div>

<h2>📊 Usage (= 過去 30 日)</h2>
<div class="kpi-grid">
{kpi("total queries", summary.get("total_queries", 0))}
{kpi("failure rate", f"{summary.get('failure_rate_pct', 0)}%", cls="success" if summary.get('failure_rate_pct', 0) < 5 else "warning")}
{kpi("avg latency", summary.get("avg_latency_ms", 0), "ms")}
{kpi("p95 latency", summary.get("p95_latency_ms", 0), "ms")}
</div>

<h2>📉 日別利用数 (= 過去 7 日)</h2>
<div class="chart-card">
  <div class="chart-title">queries / day</div>
  {chart_html}
  {daily_numeric_table}
</div>

<h2>📡 Channel split</h2>
<div class="kpi-grid">
{kpi("1:1 DM", channel.get("dm_count", 0))}
{kpi("Group", channel.get("group_count", 0))}
{kpi("Group ratio", f"{channel.get('group_pct', 0)}%", cls="accent" if channel.get('group_pct', 0) > 30 else "")}
</div>

<h2>🔍 Audit 統計 (= 過去 30 日)</h2>
<div class="kpi-grid">
{kpi("total audits", data.get("audit_total_30d", 0))}
{kpi("good 率", f"{data.get('audit_good_rate', 0)}%", cls="success" if (data.get('audit_good_rate') or 0) >= 80 else "warning")}
</div>

<h2>🧠 Memory / Group context</h2>
<div class="quick-links">
  <a class="quick-link" href="/admin/review/memory?token={token}">→ 個別 memory</a>
  <a class="quick-link" href="/admin/review/group?token={token}">→ group context</a>
</div>
"""
    return _html_envelope(
        "Review Dashboard",
        f"統合 review + ROI tracking · pending {(data.get('learning_pending') or 0) + (data.get('feedback_pending') or 0) + (data.get('research_pending') or 0) + audit_needs} 件",
        body, "/admin/review", token, flash,
    )


# ─── API 料金 page (★2026-05-29 海山指示「各種 API 料金 + 課金状況の track 機能」) ──
def _cost_kpi(label: str, value: Any, unit: str = "", cls: str = "") -> str:
    cls_str = f" {cls}" if cls else ""
    u = f'<span class="unit">{unit}</span>' if unit else ""
    return (f'<div class="kpi{cls_str}"><div class="label">{_escape(label)}</div>'
            f'<div class="value">{value}{u}</div></div>')


def _cost_investigation_memo(cache_hit_pct: Optional[float]) -> str:
    """コスト調査結果メモ (★2026-05-29、code 分析ベース、海山がダッシュ上で「なぜ高い」を把握)."""
    cache_note = ""
    if cache_hit_pct is not None:
        if cache_hit_pct < 30:
            cache_note = (f"<b>現状 cache hit {cache_hit_pct}%</b> (低い)。"
                          f"90K の wiki が cache 境界の外 = 毎 Opus call で full 課金されている可能性大。")
        else:
            cache_note = f"現状 cache hit {cache_hit_pct}%。"
    return f"""
<div class="memo-box">
<div class="memo-title">🔎 コスト要因 調査メモ (★2026-05-29 code 分析)</div>
<ol class="memo-list">
<li><b>Opus (smart) の 90K wiki 文脈が大半 uncached</b> — clone_respond の prompt cache 境界マーカーが wiki retrieval の<u>前</u>にあり、~60K の固定 core + 履歴が毎 turn fresh 課金 (Opus は input $15/1M)。{cache_note} <span class="memo-fix">→ 対策案: cache 境界を固定 core の後ろへ移す (要承認・retrieval pipeline 変更)</span></li>
<li><b>1 会話で Opus が 2 回</b> — 応答 (clone_respond) に加え sleep_time_agent が会話 idle 毎に Opus (max_tokens 4000) を再実行。<span class="memo-fix">→ 対策案: sleep-time を軽量 model 化 or 発火条件を絞る</span></li>
<li><b>品質 judge が 30 分毎に GPT-5.4</b> — response-quality cron (*/30) が各 turn を GPT-5.4 ($10/$40) で採点。traffic 比例で常時加算。<span class="memo-fix">→ 対策案: judge を fast-gpt 化 or 頻度を日次に</span></li>
<li><b>夜間 Opus batch</b> — regression (~30) + eval-baseline (30) + auto_improve (8K tokens) が毎晩 Opus。<span class="memo-fix">→ 対策案: sample 数削減 or 隔日化</span></li>
<li><b>動画 = GPT-4o vision ~11-14 call/本</b> — frame 解析 + thumbnail prefetch。動画利用増で spike。<span class="memo-fix">→ 対策案: frame 上限・抽出間隔の調整</span></li>
</ol>
<div class="memo-foot">確定値は LiteLLM <code>/spend</code> (= 下記 課金状況)。本メモは per-turn 推定 + code 分析。対策はいずれも prompt/retrieval/cron への変更を伴うため、着手前に海山承認 (CLAUDE.md 1.15)。</div>
</div>
"""


def render_cost_page(token: str, flash: Optional[str] = None,
                     litellm_status: Optional[dict] = None) -> str:
    """API 料金 + 課金状況の track ページ.

    - 課金状況: LiteLLM /spend 実値 (litellm_status) を優先、なければ events 推定。
    - per-turn 推定: events.jsonl の usage から provider/model/component 別 USD。
    - 調査メモ: コスト要因 top 5 + 対策案 (= 海山「調査して」への回答)。
    """
    try:
        from services.usage_analytics import aggregate_cost
        cost = aggregate_cost(since_sec=86400 * 14)
    except Exception as e:
        logger.warning(f"aggregate_cost failed: {e}")
        cost = {"has_usage_data": False, "note": f"集計失敗: {e}"}

    has_data = cost.get("has_usage_data")
    cache_pct = (cost.get("cache") or {}).get("cache_hit_pct") if has_data else None

    # ─── 課金状況 (budget gauge) ───
    # LiteLLM /spend 実値 優先、無ければ events 推定の今日値
    ls = litellm_status or {}
    cap = float(ls.get("budget_usd") or (cost.get("budget") or {}).get("cap_usd") or 50)
    real_used = ls.get("used_usd")
    est_today = (cost.get("today") or {}).get("usd", 0.0) if has_data else None
    gauge_used = real_used if real_used is not None else (est_today or 0.0)
    gauge_src = "LiteLLM /spend 確定値" if real_used is not None else "events.jsonl 推定"
    gauge_pct = round(gauge_used / cap * 100, 1) if cap else 0.0
    if gauge_pct >= 90:
        gauge_cls, bar_color = "danger", "var(--danger)"
    elif gauge_pct >= 60:
        gauge_cls, bar_color = "warning", "#d97706"
    else:
        gauge_cls, bar_color = "success", "#16a34a"

    budget_alerts = ""
    if gauge_pct >= 90:
        budget_alerts = ('<div class="flash" style="background:var(--danger);color:#fff;">'
                         '⚠️ 今日の spend が日次 budget の 90% 超。超過で LiteLLM が 503 を返し、'
                         'bot は「お休みをいただいてます」fallback になります。</div>')

    ls_err = ls.get("error")
    real_line = (f'<div class="meta">LiteLLM 確定 spend: ${real_used:.2f} / cap ${cap:.0f} '
                 f'(= {ls.get("usage_pct", gauge_pct)}%)</div>'
                 if real_used is not None
                 else f'<div class="meta">LiteLLM /spend 未取得 ({_escape(ls_err) if ls_err else "未接続"}) — '
                      f'下記は events.jsonl からの推定値</div>')

    body = [budget_alerts]
    body.append(f"""
<h2>💰 課金状況 (今日 / 日次 budget)</h2>
<div class="progress-card">
  <div class="head">
    <div class="label">今日の spend ({gauge_src})</div>
    <div class="pct {gauge_cls}">{gauge_pct}%</div>
  </div>
  <div class="progress-bar"><div class="fill" style="width:{min(100, gauge_pct)}%;background:{bar_color};"></div></div>
  <div class="meta">${gauge_used:.2f} / 日次上限 ${cap:.0f} (LITELLM_MAX_BUDGET) · 超過時 503 → fallback</div>
  {real_line}
</div>
""")

    # ─── 調査メモ (always、static 分析) ───
    body.append(_cost_investigation_memo(cache_pct))

    if not has_data:
        body.append(f"""
<h2>📊 per-turn コスト内訳</h2>
<div class="empty">{_escape(cost.get('note') or 'usage data 蓄積中')}</div>
""")
        return _html_envelope(
            "API 料金 / 課金状況",
            "コスト track · 調査メモ + 課金状況",
            "\n".join(body), "/admin/review/cost", token, flash,
        )

    totals = cost.get("totals", {})
    today = cost.get("today", {})
    win = cost.get("window_label", "")

    # ─── Provider 別 (Claude vs OpenAI) ───
    prov_cards = ""
    for p in cost.get("by_provider", []):
        pcls = "accent" if p["provider"].startswith("Anthropic") else ""
        prov_cards += _cost_kpi(p["provider"], f"${p['usd']:.2f}", f" ({p['pct']}%)", pcls)
    body.append(f"""
<h2>🏢 Provider 別 (= 過去 {win})</h2>
<div class="kpi-grid">
{_cost_kpi("期間合計", f"${totals.get('usd', 0):.2f}")}
{_cost_kpi("1 日平均", f"${totals.get('avg_daily_usd', 0):.2f}")}
{_cost_kpi("月額換算 (見込)", f"${totals.get('monthly_projection_usd', 0):.0f}")}
{_cost_kpi("today (推定)", f"${today.get('usd', 0):.2f}")}
</div>
<div class="kpi-grid" style="margin-top:8px;">
{prov_cards or '<div class="empty">data なし</div>'}
</div>
""")

    # ─── 日別 trend (chart + numeric table) ───
    trend = cost.get("daily_trend", [])
    chart_src = [{"date": t["date"], "queries": int(round(t["usd"]))} for t in trend]
    chart_html = _render_daily_chart(chart_src) if chart_src else '<div class="empty">data なし</div>'
    trend_rows = ""
    for t in reversed(trend[-14:]):
        ds = t.get("date", "")
        mmdd = ds[5:] if len(ds) >= 10 else ds
        dp = t.get("delta_pct")
        if dp is None:
            delta_html = '<span style="color:var(--text-3);">—</span>'
        else:
            up = dp > 0
            mark = " ⚠️" if dp >= 30 else ""
            color = "var(--danger)" if up else "#16a34a"
            delta_html = f'<span style="color:{color};">{dp:+.0f}%{mark}</span>'
        provs = t.get("providers", {})
        anth = provs.get("Anthropic (Claude)", 0)
        oai = provs.get("OpenAI", 0)
        trend_rows += (
            f"<tr>"
            f"<td style='padding:6px 12px;font-variant-numeric:tabular-nums;'>{_escape(mmdd)}</td>"
            f"<td style='padding:6px 12px;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;'>${t['usd']:.2f}</td>"
            f"<td style='padding:6px 12px;text-align:right;font-variant-numeric:tabular-nums;'>{delta_html}</td>"
            f"<td style='padding:6px 12px;text-align:right;color:var(--text-3);font-variant-numeric:tabular-nums;'>${anth:.2f}</td>"
            f"<td style='padding:6px 12px;text-align:right;color:var(--text-3);font-variant-numeric:tabular-nums;'>${oai:.2f}</td>"
            f"</tr>"
        )
    body.append(f"""
<h2>📉 日別 cost trend (= 過去 {win}、推定)</h2>
<div class="chart-card">
  <div class="chart-title">USD / day (整数丸め)</div>
  {chart_html}
  <table style="width:100%;border-collapse:collapse;margin-top:12px;font-size:13px;">
  <thead style="background:var(--bg);"><tr>
  <th style="text-align:left;padding:6px 12px;color:var(--text-3);">日付</th>
  <th style="text-align:right;padding:6px 12px;color:var(--text-3);">USD</th>
  <th style="text-align:right;padding:6px 12px;color:var(--text-3);">前日比</th>
  <th style="text-align:right;padding:6px 12px;color:var(--text-3);">Claude</th>
  <th style="text-align:right;padding:6px 12px;color:var(--text-3);">OpenAI</th>
  </tr></thead><tbody>{trend_rows}</tbody></table>
</div>
""")

    # ─── Model 別 ───
    model_rows = ""
    for r in cost.get("by_model", [])[:15]:
        price_note = "" if r["known_price"] else " <span style='color:var(--text-3);'>(価格不明=推定)</span>"
        model_rows += (
            f"<tr>"
            f"<td style='padding:6px 10px;'><code>{_escape(r['model'])}</code>{price_note}</td>"
            f"<td style='padding:6px 10px;color:var(--text-3);'>{_escape(r['provider'])}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;'>${r['usd']:.2f}</td>"
            f"<td style='padding:6px 10px;text-align:right;font-variant-numeric:tabular-nums;'>{r['calls']:,}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:var(--text-3);font-variant-numeric:tabular-nums;'>{r['input']:,}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:var(--text-3);font-variant-numeric:tabular-nums;'>{r['output']:,}</td>"
            f"<td style='padding:6px 10px;text-align:right;color:#16a34a;font-variant-numeric:tabular-nums;'>{r['cache_read']:,}</td>"
            f"</tr>"
        )
    body.append(f"""
<h2>🤖 Model 別 (= 過去 {win}、USD 降順)</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<thead style="background:var(--bg);"><tr>
<th style="text-align:left;padding:6px 10px;color:var(--text-3);">model</th>
<th style="text-align:left;padding:6px 10px;color:var(--text-3);">provider</th>
<th style="text-align:right;padding:6px 10px;color:var(--text-3);">USD</th>
<th style="text-align:right;padding:6px 10px;color:var(--text-3);">calls</th>
<th style="text-align:right;padding:6px 10px;color:var(--text-3);">in tok</th>
<th style="text-align:right;padding:6px 10px;color:var(--text-3);">out tok</th>
<th style="text-align:right;padding:6px 10px;color:var(--text-3);">cache読</th>
</tr></thead><tbody>{model_rows or '<tr><td colspan="7" class="empty">data なし</td></tr>'}</tbody></table>
""")

    # ─── Component 別 ───
    comp_rows = ""
    for r in cost.get("by_component", [])[:15]:
        comp_rows += (
            f"<tr>"
            f"<td style='padding:6px 12px;'>{_escape(r['component'])}</td>"
            f"<td style='padding:6px 12px;text-align:right;font-weight:600;font-variant-numeric:tabular-nums;'>${r['usd']:.2f}</td>"
            f"<td style='padding:6px 12px;text-align:right;font-variant-numeric:tabular-nums;'>{r['calls']:,}</td>"
            f"</tr>"
        )
    cache = cost.get("cache", {})
    body.append(f"""
<h2>🧩 機能 (component) 別 — どこが高コストか</h2>
<table style="width:100%;border-collapse:collapse;font-size:13px;">
<thead style="background:var(--bg);"><tr>
<th style="text-align:left;padding:6px 12px;color:var(--text-3);">component</th>
<th style="text-align:right;padding:6px 12px;color:var(--text-3);">USD</th>
<th style="text-align:right;padding:6px 12px;color:var(--text-3);">calls</th>
</tr></thead><tbody>{comp_rows or '<tr><td colspan="3" class="empty">data なし</td></tr>'}</tbody></table>
<p class="meta" style="margin-top:6px;">※ usage は主に clone_respond turn に記録。裏 task (sleep-time/memory) や cron (judge/regression) は本表に出ない場合あり = 確定総額は LiteLLM /spend。</p>

<h2>🗄 Prompt cache 効率 (Anthropic)</h2>
<div class="kpi-grid">
{_cost_kpi("cache hit", f"{cache.get('cache_hit_pct', 0)}%", "", "success" if cache.get('cache_hit_pct', 0) >= 50 else "warning")}
{_cost_kpi("cache 読 token", f"{cache.get('anthropic_cache_read_tokens', 0):,}")}
{_cost_kpi("非cache input", f"{cache.get('anthropic_input_tokens', 0):,}")}
</div>
<p class="meta">cache hit が低いほど Opus の input を full 課金。90K wiki が cache 境界外なのが主因 (調査メモ #1)。</p>
""")

    return _html_envelope(
        "API 料金 / 課金状況",
        f"コスト track · 過去 {win} 推定 ${totals.get('usd', 0):.2f} · 月額見込 ${totals.get('monthly_projection_usd', 0):.0f}",
        "\n".join(body), "/admin/review/cost", token, flash,
    )


# ─── Learning page (= 発見ダイジェスト) ──────────────
def _render_action_form(token: str, default_mode: str = "quality") -> str:
    """★2026-05-25 海山指示: ダッシュボードの主目的 2 つを直接入力できる form widget.

    - mode=quality   → 回答品質向上 → clone_learning.add_manual_entry
    - mode=system    → システム修正依頼 → services/system_issues.add_entry

    learning + audit page の top に挿入。POST 先は /admin/review/action/submit。
    """
    q_checked = "checked" if default_mode == "quality" else ""
    s_checked = "checked" if default_mode == "system" else ""
    return f"""
<form method="POST" action="/admin/review/action/submit?token={token}" class="action-form">
  <h3 style="margin: 0 0 12px 0; font-size: 15px;">📝 直接入力 (= 改善 / 修正依頼を即登録)</h3>
  <div style="margin-bottom: 12px; display: flex; gap: 16px; flex-wrap: wrap;">
    <label style="display: inline-flex; align-items: center; gap: 6px; cursor: pointer;">
      <input type="radio" name="mode" value="quality" {q_checked}>
      <span><strong>回答品質向上</strong> <span style="color: var(--text-3); font-size: 12px;">(= bot wiki / style 改善)</span></span>
    </label>
    <label style="display: inline-flex; align-items: center; gap: 6px; cursor: pointer;">
      <input type="radio" name="mode" value="system" {s_checked}>
      <span><strong>システム修正依頼</strong> <span style="color: var(--text-3); font-size: 12px;">(= 不備 / バグ / 機能要望)</span></span>
    </label>
  </div>
  <label style="display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600;">内容</label>
  <textarea name="content" rows="2" required placeholder="何を改善 / 修正したいか" style="width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font: inherit; font-size: 13px; box-sizing: border-box; resize: vertical; min-height: 50px; margin-bottom: 10px;"></textarea>
  <label style="display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600;">補足 <span style="color: var(--text-3); font-weight: normal;">(任意 — 品質 = wiki patch 案 / システム = 期待動作)</span></label>
  <textarea name="detail" rows="2" placeholder="(任意)" style="width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font: inherit; font-size: 13px; box-sizing: border-box; resize: vertical; min-height: 50px; margin-bottom: 10px;"></textarea>
  <button type="submit" class="btn accept">✓ 登録</button>
</form>
<style>
.action-form {{
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 16px 18px;
  border-radius: var(--radius-md, 8px);
  margin-bottom: 20px;
}}
</style>
"""


def render_conversation_success_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-26 海山指示: bot 応答後に user が会話を続けた turn (= positive signal) 一覧.

    failure cycle (= clone_audit / data-gaps / style-reflux) の対 として、
    success pattern を蓄積。style/prompt 改善の正解 dataset。
    """
    try:
        from services import conversation_success as cs
        recent = cs.list_recent(limit=50, since_days=30)
        stats = cs.summary_stats()
    except Exception as e:
        return _html_envelope(
            "成功事例", "会話継続 = positive signal",
            f'<div class="empty">conversation_success 読込失敗: {_escape(str(e))}</div>',
            "/admin/review/conversation-success", token, flash,
        )

    parts = [
        '<div class="item-body" style="background:var(--bg);">',
        '<strong>用途</strong>: bot 応答 → user が 30 分以内に follow-up (= 修正でない) ',
        'した turn を「応答 OK だった」 推定として記録。',
        'failure cycle (= audit bad / data-gaps) の対、style 改善の正解 dataset。<br>',
        '<strong>action</strong>: 「style 反映」 (= 該当 turn を good example として参照可)、',
        '「skip」 (= 反映不要)、「Comment のみ」。',
        '</div>',
        '',
    ]

    # stats
    total = stats.get("total", 0)
    by_status = stats.get("by_status", {})
    by_day = stats.get("by_day", {})
    parts.append(f"""
<div class="kpi-grid" style="margin: 16px 0;">
  <div class="kpi"><div class="label">累計</div><div class="value">{total}</div></div>
  <div class="kpi"><div class="label">captured</div><div class="value">{by_status.get("captured", 0)}</div></div>
  <div class="kpi"><div class="label">applied</div><div class="value">{by_status.get("applied", 0)}</div></div>
  <div class="kpi"><div class="label">skipped</div><div class="value">{by_status.get("skipped", 0)}</div></div>
</div>
""")

    # 日別 trend (簡易 table)
    if by_day:
        parts.append('<h2 style="margin-top:24px;">📅 日別 capture (= 過去 14 日)</h2>')
        rows = "".join(
            f"<tr><td><code>{_escape(d)}</code></td><td style='text-align:right;font-weight:600;'>{c}</td></tr>"
            for d, c in by_day.items()
        )
        parts.append(
            '<table style="width:60%;border-collapse:collapse;font-size:13px;margin-bottom:24px;">'
            '<thead style="background:var(--bg);"><tr>'
            '<th style="text-align:left;padding:6px 12px;color:var(--text-3);">日付</th>'
            '<th style="text-align:right;padding:6px 12px;color:var(--text-3);">件数</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
        )

    parts.append(f'<h2>📈 直近 success <span class="count-badge">{len(recent)}</span></h2>')
    if not recent:
        parts.append('<div class="empty">📭 success 事例 無し — まだ 30 日以内に continuation 検出されてない</div>')
    else:
        for r in recent:
            rid = r.get("id", "?")
            ts = (r.get("timestamp") or "")[:16]
            uq = (r.get("user_query") or "")[:200]
            br = (r.get("bot_response") or "")[:400]
            cont = (r.get("continuation") or "")[:200]
            elapsed = r.get("elapsed_seconds")
            elapsed_disp = f"{int(elapsed)}s" if elapsed else "?"
            status = r.get("status", "captured")
            status_class = {"captured": "pending", "applied": "good", "skipped": "neutral", "reviewed": "fix"}.get(status, "neutral")
            user_alias = _user_alias(r.get("user_id", ""))
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title"><span class="tag {status_class}">{_escape(status)}</span> {_escape(uq[:60])}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · {_escape(user_alias)} · elapsed: {elapsed_disp} · <code>{_escape(rid)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>USER:</strong> {_escape(uq)}</div>
  <div class="item-body" style="background: var(--bg);"><strong>BOT 応答 ✓:</strong>
{_escape(br)}</div>
  <div class="item-body"><strong>USER (継続):</strong> {_escape(cont)}</div>
  <form method="POST" action="/admin/review/conversation-success/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="メモ (任意)">
      </div>
      <button type="submit" name="action" value="applied" class="btn accept">✓ style 反映済</button>
      <button type="submit" name="action" value="skipped" class="btn reject">— skip</button>
      <button type="submit" name="action" value="reviewed" class="btn">📝 reviewed</button>
    </div>
  </form>
</div>""")

    body = "\n".join(parts)
    return _html_envelope(
        "成功事例", "会話継続 = positive signal (= 失敗 cycle の対、style 改善 正解 dataset)",
        body, "/admin/review/conversation-success", token, flash,
    )


def handle_conversation_success_action(action: str, item_id: str, note: str = "") -> tuple[bool, str]:
    """conversation-success page action: applied / skipped / reviewed."""
    if action not in ("applied", "skipped", "reviewed"):
        return False, f"unknown action: {action}"
    if not item_id:
        return False, "id 必須"
    try:
        from services import conversation_success as cs
        ok = cs.update_status(item_id, action)
        if not ok:
            return False, f"status 更新失敗 (id={item_id})"
        # note は今 module で別途 add_comment 関数無いので skip (= 後で必要なら追加)
        return True, f"success #{item_id[:20]} → {action}" + (" (note 受領)" if note.strip() else "")
    except Exception as e:
        logger.exception(f"conversation_success action {action}/{item_id}: {e}")
        return False, str(e)


def render_data_gaps_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-26 海山指示: bot「データ無い」回答 = データ拡充候補 dashboard."""
    try:
        from services import data_gaps
        gaps = data_gaps.list_active(limit=80)
        summary = data_gaps.summary_by_category()
    except Exception as e:
        return _html_envelope(
            "データ拡充", "「データ無い」回答 → 拡充候補",
            f'<div class="empty">data_gaps module 読込失敗: {_escape(str(e))}</div>',
            "/admin/review/data-gaps", token, flash,
        )

    parts = [
        '<div class="item-body" style="background:var(--bg);">',
        '<strong>用途</strong>: bot が「データ無い」「分からない」等と答えた turn を ',
        '自動 capture (= scripts/data_gap_detector + clone_respond hook)、',
        '同 query 繰返しは <code>occurrence_count</code> で集計。',
        '<strong>action</strong>: 「実装予定」 = 整備計画化、「skip」 = edge case、',
        '「done」 = wiki 拡充完了。<br>',
        '<strong>関連 prompt rule</strong>: <code>brain_wiki.py</code> §2a — 「データ無い」 と',
        '引いて終わるのは NG、必ず「今後拡充予定」 tone で答える。',
        '</div>',
        '',
    ]

    # category summary
    if summary:
        parts.append('<h2>📊 category 別 集計</h2>')
        rows = []
        for cat, info in sorted(summary.items(), key=lambda x: -x[1].get("occurrences", 0)):
            rows.append(
                f"<tr>"
                f"<td><code>{_escape(cat)}</code></td>"
                f"<td style='text-align:right;'>{info.get('pending', 0)}</td>"
                f"<td style='text-align:right;'>{info.get('planned', 0)}</td>"
                f"<td style='text-align:right;color:var(--text-3);'>{info.get('done', 0)}</td>"
                f"<td style='text-align:right;color:var(--text-3);'>{info.get('skipped', 0)}</td>"
                f"<td style='text-align:right;font-weight:600;'>{info.get('occurrences', 0)}</td>"
                f"</tr>"
            )
        parts.append(
            '<table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px;">'
            '<thead style="background:var(--bg);">'
            '<tr>'
            '<th style="text-align:left;padding:6px 12px;color:var(--text-3);">category</th>'
            '<th style="text-align:right;padding:6px 12px;color:var(--text-3);">pending</th>'
            '<th style="text-align:right;padding:6px 12px;color:var(--text-3);">planned</th>'
            '<th style="text-align:right;padding:6px 12px;color:var(--text-3);">done</th>'
            '<th style="text-align:right;padding:6px 12px;color:var(--text-3);">skipped</th>'
            '<th style="text-align:right;padding:6px 12px;color:var(--text-3);">累計 occur</th>'
            '</tr></thead>'
            f'<tbody>{chr(10).join(rows)}</tbody></table>'
        )

    parts.append(f'<h2>📋 Active gap <span class="count-badge">{len(gaps)}</span> (= occurrence 多い順)</h2>')
    if not gaps:
        parts.append('<div class="empty">📭 active な gap なし</div>')
    else:
        for r in gaps:
            rid = r.get("id", "?")
            ts = r.get("timestamp", "")[:16]
            cat = r.get("matched_category", "?")
            occ = int(r.get("occurrence_count", 1))
            user_query = r.get("user_query", "")[:300]
            bot_response = r.get("bot_response", "")[:400]
            matched = r.get("matched_text", "")
            forward = r.get("forward_looking", False)
            status = r.get("status", "pending")

            occ_tag = f'<span class="tag {"fix" if occ >= 5 else "neutral"}">{occ}× occur</span>'
            cat_tag = f'<span class="tag pending">{_escape(cat)}</span>'
            forward_tag = '<span class="tag good">✓ 新 tone</span>' if forward else '<span class="tag fix">⚠ 旧 tone</span>'
            status_tag = f'<span class="tag {"good" if status == "planned" else "pending"}">{_escape(status)}</span>'

            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{occ_tag} {cat_tag} {status_tag} {forward_tag} {_escape(user_query[:80])}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · <code>{_escape(rid)}</code> · matched: <code>{_escape(matched)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>user query:</strong> {_escape(user_query)}</div>
  <div class="item-body" style="background: var(--bg); font-size: 12.5px;"><strong>bot 応答 head:</strong> {_escape(bot_response)}</div>
  <form method="POST" action="/admin/review/data-gaps/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="調査メモ / 整備計画">
      </div>
      <button type="submit" name="action" value="planned" class="btn accept">📅 実装予定</button>
      <button type="submit" name="action" value="done" class="btn">✓ 完了</button>
      <button type="submit" name="action" value="skipped" class="btn reject">— skip</button>
      <button type="submit" name="action" value="comment" class="btn comment">💬 Comment のみ</button>
    </div>
  </form>
</div>""")

    body = "\n".join(parts)
    return _html_envelope(
        "データ拡充候補", "bot「データ無い」回答 → 整備優先度",
        body, "/admin/review/data-gaps", token, flash,
    )


def handle_data_gap_action(action: str, item_id: str, note: str = "") -> tuple[bool, str]:
    """data-gaps page の action: planned / done / skipped / comment."""
    if action not in ("planned", "done", "skipped", "comment"):
        return False, f"unknown action: {action}"
    if not item_id:
        return False, "id 必須"
    try:
        from services import data_gaps
        if action != "comment":
            ok = data_gaps.update_status(item_id, action)
            if not ok:
                return False, f"status 更新失敗 (id={item_id})"
        if note.strip():
            data_gaps.add_comment(item_id, note.strip(), reviewer="umiyama")
        return True, f"gap #{item_id[:20]} → {action}" + (" (note 付き)" if note.strip() else "")
    except Exception as e:
        logger.exception(f"data_gap action {action}/{item_id}: {e}")
        return False, str(e)


def _render_web_clip_form(token: str) -> str:
    """★2026-05-26: web / 他媒体 → wiki に取り込む submit form widget."""
    from services import web_clips
    target_options = []
    for path, label, warn in web_clips.WIKI_TARGETS:
        warn_mark = " ⚠️" if warn else ""
        target_options.append(
            f'<option value="{_escape(path)}">{_escape(label)}{warn_mark}</option>'
        )
    options_html = "\n".join(target_options)
    return f"""
<form method="POST" action="/admin/review/web-clip/submit?token={token}" class="action-form">
  <h3 style="margin: 0 0 12px 0; font-size: 15px;">✂️ Web / 他媒体 から取り込み</h3>
  <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px;">
    <input type="text" name="title" placeholder="タイトル (例: Naval - 信仰について)"
           style="padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); font: inherit; font-size: 13px; box-sizing: border-box;">
    <input type="url" name="source_url" placeholder="source URL (任意)"
           style="padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); font: inherit; font-size: 13px; box-sizing: border-box;">
  </div>
  <label style="display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600;">引用本文 <span style="color: var(--danger);">*</span></label>
  <textarea name="quote" rows="4" required placeholder="拾った言葉・引用をそのまま貼る"
            style="width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font: inherit; font-size: 13px; box-sizing: border-box; resize: vertical; min-height: 80px; margin-bottom: 10px;"></textarea>
  <label style="display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600;">海山の感想 / 加筆 <span style="color: var(--text-3); font-weight: normal;">(任意)</span></label>
  <textarea name="reflection" rows="2" placeholder="なぜ共感したか / どう自分に響くか (任意)"
            style="width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font: inherit; font-size: 13px; box-sizing: border-box; resize: vertical; min-height: 60px; margin-bottom: 10px;"></textarea>
  <label style="display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600;">反映先 wiki</label>
  <select name="target_wiki" required style="width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--surface); color: var(--text); font: inherit; font-size: 13px; box-sizing: border-box; margin-bottom: 12px;">
    {options_html}
  </select>
  <div style="display: flex; gap: 10px; flex-wrap: wrap;">
    <label style="display: inline-flex; align-items: center; gap: 6px; cursor: pointer; font-size: 12.5px; color: var(--text-2);">
      <input type="checkbox" name="apply_now" value="1">
      <span>即時 wiki 反映 (= pending 経由しない、自信ある時のみ)</span>
    </label>
    <button type="submit" class="btn accept" style="margin-left: auto;">✓ 登録</button>
  </div>
</form>
<style>
.action-form {{
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 16px 18px;
  border-radius: var(--radius-md, 8px);
  margin-bottom: 20px;
}}
.action-form .danger {{ color: var(--danger); }}
</style>
"""


def render_web_clip_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-26 海山指示: web / 他媒体 → wiki 取込 dashboard."""
    try:
        from services import web_clips
        pending = web_clips.list_all(limit=50, include_resolved=False)
        all_items = web_clips.list_all(limit=100, include_resolved=True)
        applied_count = sum(1 for r in all_items if r.get("status") == "applied")
    except Exception as e:
        return _html_envelope(
            "Web 取込", "wiki 反映 queue",
            _render_web_clip_form(token) +
            f'<div class="empty">web_clips module 読込失敗: {_escape(str(e))}</div>',
            "/admin/review/web-clip", token, flash,
        )

    parts = [_render_web_clip_form(token)]

    parts.append(
        f'<h2>📋 Pending clip <span class="count-badge">{len(pending)}</span></h2>'
    )
    if not pending:
        parts.append(
            '<div class="empty">📭 pending な clip なし — 上の form で追加</div>'
        )
    else:
        for r in pending:
            rid = r.get("id", "?")
            ts = r.get("timestamp", "")[:16]
            title = r.get("title", "").strip() or "(無題)"
            quote = r.get("quote", "").strip()
            reflection = r.get("reflection", "").strip()
            src = r.get("source_url", "").strip()
            target = r.get("target_wiki", "")
            status = r.get("status", "pending")

            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{_escape(title)} <span class="tag neutral">→ {_escape(target)}</span></div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · <code>{_escape(rid)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>引用:</strong>
{_escape(quote)}</div>
  {f'<div class="item-body" style="background: var(--bg);"><strong>感想:</strong>{chr(10)}{_escape(reflection)}</div>' if reflection else ''}
  {f'<div class="item-body" style="background: var(--bg); font-size: 12.5px;"><strong>source:</strong> <a href="{_escape(src)}" target="_blank">{_escape(src)}</a></div>' if src else ''}
  <form method="POST" action="/admin/review/web-clip/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="コメント (任意)">
      </div>
      <button type="submit" name="action" value="apply" class="btn accept">✓ wiki に反映</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ 却下</button>
      <button type="submit" name="action" value="comment" class="btn comment">💬 Comment のみ</button>
    </div>
  </form>
</div>""")

    # 最近 applied の summary (= 何 wiki に何 件取り込まれたか)
    if applied_count > 0:
        applied_by_target: dict[str, int] = {}
        for r in all_items:
            if r.get("status") == "applied":
                applied_by_target[r.get("target_wiki", "?")] = applied_by_target.get(r.get("target_wiki", "?"), 0) + 1
        if applied_by_target:
            parts.append(f'<h2 style="margin-top:32px;">✓ 既反映 (累計 {applied_count} 件)</h2>')
            rows = "".join(
                f"<tr><td><code>{_escape(p)}</code></td><td style='text-align:right;'>{n}</td></tr>"
                for p, n in sorted(applied_by_target.items(), key=lambda x: -x[1])
            )
            parts.append(
                f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
                f'<thead style="background:var(--bg);"><tr>'
                f'<th style="text-align:left;padding:6px 12px;color:var(--text-3);">target wiki</th>'
                f'<th style="text-align:right;padding:6px 12px;color:var(--text-3);">件数</th>'
                f'</tr></thead><tbody>{rows}</tbody></table>'
            )

    body = "\n".join(parts)
    return _html_envelope(
        "Web 取込", "web / 他媒体 → wiki 反映 queue",
        body, "/admin/review/web-clip", token, flash,
    )


def handle_web_clip_action(action: str, item_id: str, note: str = "") -> tuple[bool, str]:
    """web-clip page の action handler.

    action:
      - 'apply'   → apply_clip (= wiki 追記、status=applied)
      - 'reject'  → status=rejected
      - 'comment' → note のみ追記
    """
    if action not in ("apply", "reject", "comment"):
        return False, f"unknown action: {action}"
    if not item_id:
        return False, "id 必須"

    try:
        from services import web_clips
        ok_action = True
        if action == "apply":
            result = web_clips.apply_clip(item_id)
            if not result.get("ok"):
                return False, f"apply 失敗: {result.get('error', '?')}"
            applied_path = result.get("applied_path", "")
            msg_action = f"→ wiki {applied_path} に反映"
        elif action == "reject":
            ok_action = web_clips.update_status(item_id, "rejected")
            if not ok_action:
                return False, f"reject 失敗 (id={item_id})"
            msg_action = "✕ rejected"
        else:  # comment
            msg_action = "comment 追記"

        if note.strip():
            web_clips.add_comment(item_id, note.strip(), reviewer="umiyama")

        return True, f"clip #{item_id[:20]} {msg_action}" + (" (note 付き)" if note.strip() else "")
    except Exception as e:
        logger.exception(f"web_clip action {action}/{item_id}: {e}")
        return False, str(e)


def render_style_reflux_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-26 海山 B1+B3: style 逆流 週次レポート 一覧 + 最新内容表示."""
    reflux_dir = Path(os.getenv("BRAIN_ROOT", "/app/data/brain")) / "clone_improve" / "style_reflux"
    reports: list[Path] = []
    if reflux_dir.exists():
        reports = sorted(reflux_dir.glob("*.md"), reverse=True)[:10]

    parts = [
        '<div class="item-body" style="background:var(--bg);">',
        '<strong>用途</strong>: 直近 30 日の audit fail / feedback / 発見 を pattern 別に集約、',
        '頻出 failure type と 改善 proposal を提示。海山が style wiki に反映する判断材料。<br>',
        '<strong>再生成</strong>: <code>python3 scripts/style_reflux.py</code> (= 月曜 04:10 cron)',
        '</div>',
        '',
        f'<h2>📑 過去レポート <span class="count-badge">{len(reports)}</span></h2>',
    ]
    if not reports:
        parts.append(
            '<div class="empty">📭 レポート無し — '
            '<code>python3 scripts/style_reflux.py</code> で初回生成可</div>'
        )
    else:
        for r in reports[:5]:
            d = r.stem  # YYYY-MM-DD
            try:
                size_kb = r.stat().st_size / 1024
            except Exception:
                size_kb = 0
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">📄 {_escape(d)}</div>
      <div class="item-meta" style="margin-top: 4px;">{size_kb:.1f}KB · <code>{_escape(r.name)}</code></div>
    </div>
  </div>
</div>""")

        # 最新 1 件は内容を inline で表示 (= 海山が click せず即見える)
        latest = reports[0]
        try:
            content = latest.read_text(encoding="utf-8")
            # markdown を簡易 HTML に (= h2, table, list)
            # 今は raw markdown を <pre> で出すだけ、十分実用
            parts.append(
                f'<h2 style="margin-top:32px;">📋 最新レポート (= {_escape(latest.stem)}) 内容</h2>'
            )
            parts.append(
                f'<div class="item-body" style="background:var(--bg);font-family:Monaco,monospace;'
                f'font-size:12.5px;white-space:pre-wrap;max-height:600px;overflow-y:auto;">'
                f'{_escape(content)}'
                f'</div>'
            )
        except Exception as e:
            parts.append(f'<div class="empty">読込失敗: {_escape(str(e))}</div>')

    body = "\n".join(parts)
    return _html_envelope(
        "Style 逆流", "audit/feedback/発見 → style 改善 proposal (= 週次)",
        body, "/admin/review/style-reflux", token, flash,
    )


def render_quality_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-26 海山 C2+C3: 品質 metric 14 日 trend + 直近 alert 一覧."""
    metrics_file = Path(os.getenv("BRAIN_ROOT", "/app/data/brain")) / "clone_improve" / "quality_metrics.jsonl"
    alert_log = Path(os.getenv("BRAIN_ROOT", "/app/data/brain")) / "quality_metrics_alerts.jsonl"

    # metrics 14 日
    metrics: list[dict] = []
    if metrics_file.exists():
        try:
            by_date: dict[str, dict] = {}
            for line in metrics_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                    if m.get("date"):
                        by_date[m["date"]] = m
                except Exception:
                    continue
            metrics = sorted(by_date.values(), key=lambda m: m["date"], reverse=True)[:14]
        except Exception:
            metrics = []

    # alerts 最近 10
    alerts: list[dict] = []
    if alert_log.exists():
        try:
            for line in alert_log.read_text(encoding="utf-8").splitlines()[-30:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    alerts.append(json.loads(line))
                except Exception:
                    continue
            alerts = list(reversed(alerts))[:10]
        except Exception:
            alerts = []

    parts = []

    # 直近 alert 表示
    parts.append(f'<h2>🚨 直近 trend alert <span class="count-badge">{len(alerts)}</span></h2>')
    if not alerts:
        parts.append('<div class="empty">📭 alert 履歴なし — 品質安定</div>')
    else:
        for a in alerts[:5]:
            ts = (a.get("ts") or "")[:16]
            sev = a.get("severity", "?")
            sev_tag = f'<span class="tag {"fix" if sev=="warning" else "neutral"}">{_escape(sev)}</span>'
            summary = a.get("summary", "")
            deg_items = a.get("degraded", [])
            deg_lines = "".join(
                f'<li><code>{_escape(d.get("axis",""))}</code>: '
                f'{d.get("today","?")} (基準 {d.get("baseline","?")}) '
                f'{d.get("delta_pct","?")}%</li>'
                for d in deg_items
            )
            parts.append(f'''
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{sev_tag} {_escape(summary)}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts}</div>
    </div>
  </div>
  <div class="item-body"><ul style="margin:0;padding-left:20px;">{deg_lines}</ul></div>
</div>''')

    # 14 日 trend table
    parts.append(f'<h2 style="margin-top:32px;">📊 14 日 trend <span class="count-badge">{len(metrics)}</span></h2>')
    if not metrics:
        parts.append('<div class="empty">📭 metric data 無し — `python3 scripts/quality_metrics.py --since 7` で backfill 可</div>')
    else:
        rows = []
        for m in metrics:
            d = m.get("date", "?")
            t = m.get("turn", {})
            q = m.get("quality_judge", {})
            ad = m.get("auto_discovery", {})
            rows.append(
                f"<tr>"
                f"<td><code>{_escape(d)}</code></td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{t.get('n_finished', 0)}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:{'var(--danger)' if t.get('fail_rate_pct', 0) > 3 else 'var(--text-2)'};'>{t.get('fail_rate_pct', 0)}%</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:{'var(--danger)' if t.get('fallback_rate_pct', 0) > 10 else 'var(--text-2)'};'>{t.get('fallback_rate_pct', 0)}%</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{q.get('n_judged', 0)}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{q.get('mean_ai_smell', '—')}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{q.get('mean_mirroring_fit', '—')}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{q.get('mean_length_appropriate', '—')}</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:{'var(--danger)' if q.get('degraded_rate_pct', 0) > 15 else 'var(--text-2)'};'>{q.get('degraded_rate_pct', 0)}%</td>"
                f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:{'var(--danger)' if ad.get('n_response_quality', 0) > 5 else 'var(--text-2)'};'>{ad.get('n_response_quality', 0)}</td>"
                f"</tr>"
            )
        parts.append(f"""
<table class="quality-table" style="width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;">
  <thead style="background:var(--bg);border-bottom:1px solid var(--border);">
    <tr>
      <th style="text-align:left;padding:8px 12px;font-size:12px;color:var(--text-3);">日付</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">finished</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">fail %</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">fallback %</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">judged</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">ai_smell ↑</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">mirror ↑</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">length ↑</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">degraded %</th>
      <th style="text-align:right;padding:8px 12px;font-size:12px;color:var(--text-3);">RQ 発見</th>
    </tr>
  </thead>
  <tbody>
    {chr(10).join(rows)}
  </tbody>
</table>
<p style="color:var(--text-3);font-size:12px;margin-top:8px;">
  指標 注: <b>fail %</b>/<b>fallback %</b>/<b>degraded %</b>/<b>RQ 発見</b> は低い方が良。
  <b>ai_smell</b>/<b>mirror</b>/<b>length</b> は <code>1-5</code> の score、高い方が良。
  re-build: <code>python3 scripts/quality_metrics.py --since 7</code>
</p>
""")

    body = "\n".join(parts)
    return _html_envelope(
        "品質 trend", "応答品質 metric 14 日推移 + 直近劣化 alert",
        body, "/admin/review/quality", token, flash,
    )


def _render_coverage_bar(coverage: list[dict]) -> str:
    """★2026-05-26: 8 次元 カバレッジを HTML table + ■□ bar で可視化.

    coverage: alignment_interview.coverage_report() の戻り (= 薄い順 sort 済).
    """
    if not coverage:
        return '<div class="empty">📊 coverage data 無し (= 一度も /voice-align 経路で会話してない)</div>'

    rows = []
    for r in coverage:
        depth = r.get("depth_score", 0)
        sessions = r.get("session_count", 0)
        last = (r.get("last_explored") or "")[:10] or "—"
        wb = r.get("wiki_bytes", 0)
        wb_disp = f"{wb / 1024:.1f}KB" if wb >= 1024 else f"{wb}B"
        # depth 0-8 を ■□ で可視化
        bar_filled = min(max(int(depth), 0), 8)
        bar = "■" * bar_filled + "□" * (8 - bar_filled)
        rows.append(
            f"<tr>"
            f"<td><code>{_escape(r.get('id', ''))}</code></td>"
            f"<td>{_escape(r.get('label', ''))}</td>"
            f"<td style='font-family:monospace;font-size:13px;color:var(--text-2);'>{bar}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{depth}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{sessions}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;'>{wb_disp}</td>"
            f"<td style='text-align:right;font-variant-numeric:tabular-nums;color:var(--text-3);'>{last}</td>"
            f"</tr>"
        )
    return f"""
<h2>📊 8 次元 カバレッジ (= 薄い順、雑談で優先的に突く)</h2>
<table class="coverage-table" style="width:100%;border-collapse:collapse;margin:12px 0;">
  <thead style="background:var(--bg);border-bottom:1px solid var(--border);">
    <tr>
      <th style="text-align:left;padding:8px 12px;font-size:12.5px;color:var(--text-3);">id</th>
      <th style="text-align:left;padding:8px 12px;font-size:12.5px;color:var(--text-3);">次元</th>
      <th style="text-align:left;padding:8px 12px;font-size:12.5px;color:var(--text-3);">depth (0-8)</th>
      <th style="text-align:right;padding:8px 12px;font-size:12.5px;color:var(--text-3);">depth</th>
      <th style="text-align:right;padding:8px 12px;font-size:12.5px;color:var(--text-3);">sessions</th>
      <th style="text-align:right;padding:8px 12px;font-size:12.5px;color:var(--text-3);">wiki 厚み</th>
      <th style="text-align:right;padding:8px 12px;font-size:12.5px;color:var(--text-3);">最終</th>
    </tr>
  </thead>
  <tbody>
    {chr(10).join(rows)}
  </tbody>
</table>
"""


def render_voice_align_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-26 海山指示: Vapi 雑談アラインメント の蒸留状況 dashboard.

    内容:
    - 8 次元 カバレッジ可視化 (= 薄い順、■□ bar)
    - Pending 蒸留案 list (= レビュー待ち、Accept all / 詳細 / Reject)
    - 各 extraction file の summary (= extracted_at / item_count / session_summary)
    """
    try:
        import alignment_interview as ai
        coverage = ai.coverage_report()
        pending = ai.list_pending_extractions()
    except Exception as e:
        return _html_envelope(
            "音声 align", "蒸留状況",
            f'<div class="empty">alignment_interview 読込失敗: {_escape(str(e))}</div>',
            "/admin/review/voice-align", token, flash,
        )

    parts = [_render_coverage_bar(coverage)]

    # Pending 蒸留案
    parts.append(
        f'<h2 style="margin-top:32px;">📭 Pending 蒸留案 '
        f'<span class="count-badge">{len(pending)}</span></h2>'
    )
    if not pending:
        parts.append(
            '<div class="empty">📭 pending な蒸留案なし — '
            '電話 +1 XXX XXX XXXX で雑談、または '
            '<code>/voice-align?token=...</code> (web SDK) で会話 → '
            '数分後にここに表示される</div>'
        )
    else:
        for p in pending:
            fname = p.get("file", "?")
            ts = (p.get("extracted_at") or "")[:16]
            n_items = p.get("item_count", 0)
            summary = p.get("summary", "")[:300] or "(session_summary 無し)"
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{_escape(summary[:80])}{'...' if len(summary) > 80 else ''} <span class="tag neutral">{n_items} items</span></div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · <code>{_escape(fname)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>session summary:</strong>
{_escape(summary)}</div>
  <form method="POST" action="/admin/review/voice-align/action?token={token}" style="margin-top:10px;">
    <input type="hidden" name="file" value="{_escape(fname)}">
    <div class="actions">
      <a class="btn" href="/admin/review/voice-align/detail?token={token}&file={_escape(fname)}">📄 詳細 (item 別 accept/reject)</a>
      <button type="submit" name="action" value="accept_all" class="btn accept">✓ 全 item 採用</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ 全 reject</button>
    </div>
  </form>
</div>""")

    body = "\n".join(parts)
    return _html_envelope(
        "音声 align", "Vapi 雑談 蒸留状況 (= 8 次元 カバレッジ + Pending 蒸留案)",
        body, "/admin/review/voice-align", token, flash,
    )


def render_voice_align_detail_page(token: str, filename: str,
                                   flash: Optional[str] = None) -> str:
    """★2026-05-26: 蒸留案 1 件の詳細 (= per-item Accept / Reject checkbox)."""
    try:
        import alignment_interview as ai
        d = ai.get_extraction(filename)
    except Exception as e:
        return _html_envelope(
            "音声 align 詳細", "読込失敗",
            f'<div class="empty">{_escape(str(e))}</div>',
            "/admin/review/voice-align", token, flash,
        )
    if not d:
        return _html_envelope(
            "音声 align 詳細", "ファイル無し",
            f'<div class="empty">file <code>{_escape(filename)}</code> 見つからず</div>',
            "/admin/review/voice-align", token, flash,
        )

    items = d.get("items", [])
    summary = d.get("session_summary", "") or "(summary 無し)"
    extracted_at = d.get("extracted_at", "")
    status = d.get("status", "")

    parts = [
        f'<div style="margin-bottom:16px;">'
        f'<a class="btn" href="/admin/review/voice-align?token={token}">← 戻る</a>'
        f'</div>',
        f'<div class="item-body" style="background:var(--bg);">'
        f'<strong>file</strong>: <code>{_escape(filename)}</code><br>'
        f'<strong>extracted</strong>: {_escape(extracted_at)}<br>'
        f'<strong>status</strong>: {_escape(status)}<br>'
        f'<strong>summary</strong>: {_escape(summary)}'
        f'</div>',
        f'<h2 style="margin-top:24px;">📋 抽出 item <span class="count-badge">{len(items)}</span></h2>',
    ]

    if status == "applied":
        parts.append('<div class="empty">✓ already applied — wiki/interview/ に反映済</div>')
    elif status == "rejected":
        parts.append('<div class="empty">✕ rejected — 反映せず保管</div>')
    elif not items:
        parts.append('<div class="empty">item 0 件</div>')
    else:
        parts.append(
            f'<form method="POST" action="/admin/review/voice-align/action?token={token}">'
            f'<input type="hidden" name="file" value="{_escape(filename)}">'
        )
        for i, it in enumerate(items):
            cat = it.get("category", "")
            insight = (it.get("insight") or "").strip()
            evidence = (it.get("evidence_quote") or "").strip()
            confidence = it.get("confidence", "medium")
            conf_class = {"high": "good", "medium": "pending", "low": "fix"}.get(confidence, "neutral")
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;">
        <input type="checkbox" name="indices" value="{i}" checked style="width:18px;height:18px;">
        <div>
          <div class="item-title">
            <span class="tag {conf_class}">{_escape(confidence)}</span>
            <span class="tag neutral">{_escape(cat)}</span>
            #{i+1}
          </div>
        </div>
      </label>
    </div>
  </div>
  <div class="item-body"><strong>insight:</strong>
{_escape(insight)}</div>
  {f'<div class="item-body" style="background:var(--bg);font-size:12.5px;"><strong>evidence:</strong> 「{_escape(evidence)}」</div>' if evidence else ''}
</div>""")
        parts.append("""
<div class="actions" style="margin-top: 16px;">
  <button type="submit" name="action" value="accept_selected" class="btn accept">✓ チェック済 item を採用</button>
  <button type="submit" name="action" value="accept_all" class="btn">✓ 全 item 採用</button>
  <button type="submit" name="action" value="reject" class="btn reject">✕ 全 reject</button>
</div>
</form>""")

    body = "\n".join(parts)
    return _html_envelope(
        f"音声 align 詳細: {filename[:32]}",
        "per-item accept / reject",
        body, "/admin/review/voice-align", token, flash,
    )


def handle_voice_align_action(filename: str, action: str,
                              accepted_indices: list[int] | None = None,
                              note: str = "") -> tuple[bool, str]:
    """★2026-05-26: voice-align page の action handler.

    action:
      - 'accept_all'      → apply_extraction(filename) で全 item 採用
      - 'accept_selected' → apply_extraction(filename, accepted_indices=[...])
      - 'reject'          → reject_extraction(filename)
    """
    if action not in ("accept_all", "accept_selected", "reject"):
        return False, f"unknown action: {action}"
    if not filename:
        return False, "file 必須"
    try:
        import alignment_interview as ai
        if action == "reject":
            ok = ai.reject_extraction(filename)
            if not ok:
                return False, f"reject 失敗 ({filename})"
            return True, f"✕ rejected: {filename[:30]}"
        # accept variants
        if action == "accept_all":
            result = ai.apply_extraction(filename)
        else:  # accept_selected
            result = ai.apply_extraction(filename, accepted_indices=accepted_indices)
        if result.get("error"):
            return False, f"apply 失敗: {result['error']}"
        applied = result.get("applied", 0)
        files = result.get("files", [])
        msg = f"✓ applied {applied} items → {', '.join(files[:3])}"
        if len(files) > 3:
            msg += f" (+{len(files) - 3})"
        return True, msg
    except Exception as e:
        logger.exception(f"voice_align action {action}/{filename}: {e}")
        return False, str(e)


def render_system_issues_page(token: str, flash: Optional[str] = None) -> str:
    """★2026-05-25: システム修正依頼 (= bug / 機能要望) 一覧 + 直接入力 form."""
    try:
        from services import system_issues
        items = system_issues.list_all(limit=50, include_resolved=False)
    except Exception as e:
        body = _render_action_form(token, default_mode="system") + \
               f'<div class="empty">System issues 読込失敗: {_escape(str(e))}</div>'
        return _html_envelope("システム修正依頼", "不備 / バグ / 機能要望 の backlog",
                              body, "/admin/review/system", token, flash)

    form_html = _render_action_form(token, default_mode="system")

    if not items:
        body = form_html + '<div class="empty">📭 pending / acknowledged な依頼なし</div>'
        return _html_envelope("システム修正依頼", "不備 / バグ / 機能要望 の backlog",
                              body, "/admin/review/system", token, flash)

    parts = [form_html, f'<h2>🐛 Pending システム修正依頼 <span class="count-badge">{len(items)}</span></h2>']
    for r in items:
        rid = r.get("id", "?")
        ts = r.get("timestamp", "")[:16]
        status = r.get("status", "pending")
        desc = r.get("description", "")
        expected = r.get("expected", "")
        comments = r.get("comments", [])
        status_class = {"pending": "pending", "acknowledged": "fix", "fixed": "good", "rejected": "bad"}.get(status, "neutral")
        status_tag = f'<span class="tag {status_class}">{_escape(status)}</span>'

        comments_html = ""
        if comments:
            comments_html = '<div class="item-body" style="background: var(--bg); font-size: 12.5px;"><strong>コメント履歴:</strong>'
            for c in comments[-5:]:
                c_ts = c.get("ts", "")[:16]
                c_who = _escape(c.get("reviewer", "?"))
                c_msg = _escape(c.get("comment", ""))
                comments_html += f'<div style="margin-top: 6px;">[{c_ts}] <code>{c_who}</code>: {c_msg}</div>'
            comments_html += '</div>'

        parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{status_tag} {_escape(desc[:120])}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · <code>{_escape(rid)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>内容:</strong>
{_escape(desc)}</div>
  {f'<div class="item-body"><strong>期待動作:</strong>{chr(10)}{_escape(expected)}</div>' if expected else ''}
  {comments_html}
  <form method="POST" action="/admin/review/system/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="コメント (任意)">
      </div>
      <button type="submit" name="action" value="acknowledged" class="btn">👀 Acknowledge</button>
      <button type="submit" name="action" value="fixed" class="btn accept">✓ Fixed</button>
      <button type="submit" name="action" value="rejected" class="btn reject">✕ Reject</button>
      <button type="submit" name="action" value="comment" class="btn comment">💬 Comment のみ</button>
    </div>
  </form>
</div>""")

    body = "\n".join(parts)
    return _html_envelope("システム修正依頼", "不備 / バグ / 機能要望 の backlog (= 海山直接入力)",
                          body, "/admin/review/system", token, flash)


def render_learning_page(token: str, flash: Optional[str] = None) -> str:
    try:
        import clone_learning
        items = clone_learning.list_pending(limit=30)
    except Exception as e:
        return _html_envelope("発見ダイジェスト", "Learning queue 読込失敗",
                              f'<div class="empty">{_escape(str(e))}</div>',
                              "/admin/review/learning", token, flash)

    # ★2026-05-25 海山指示: top に「直接入力」 form widget (品質 default)
    form_html = _render_action_form(token, default_mode="quality")

    if not items:
        body = form_html + '<div class="empty">📭 pending な発見なし — 全て review 済</div>'
        return _html_envelope("発見ダイジェスト", "回答品質向上 — 会話発見 + 海山直接入力",
                              body, "/admin/review/learning", token, flash)

    parts = [form_html, f'<h2>📭 Pending 発見 <span class="count-badge">{len(items)}</span></h2>']
    for r in items:
        rid = r.get("id", "?")
        ts = r.get("timestamp", "")[:16]
        cat = r.get("category", "")
        # ★完全匿名化: user_display 直接出さず 社員 alias 表示
        user = _user_alias(r.get("user_id", ""))
        insight = r.get("insight", "")
        snippet = r.get("source_snippet", "")[:300]
        patch = r.get("proposed_wiki_patch", "")
        is_manual = r.get("manual_entry", False)
        cat_label = "直接入力" if is_manual else (cat or "auto")
        tag_class = "good" if is_manual else "neutral"
        cat_tag = f'<span class="tag {tag_class}">{_escape(cat_label)}</span>'

        # ★2026-05-25: proposed_wiki_patch を直接編集可能化 (= textarea + name="patch")。
        # 旧値は hidden で保持し、変更検知して update_patch 呼出。
        patch_display = patch[:4000] if patch else ""
        parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{cat_tag} {_escape(insight)}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · {_escape(user)} · <code>{_escape(rid)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>抽出元 会話:</strong>
{_escape(snippet)}</div>
  <form method="POST" action="/admin/review/learning/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <input type="hidden" name="patch_original" value="{_escape(patch_display)}">
    <div class="item-body" style="padding-bottom: 8px;">
      <strong>提案 wiki patch (= 編集可能、保存は Accept/Noted/Comment 時):</strong>
      <textarea name="patch" rows="4" style="display:block;width:100%;margin-top:8px;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius-sm);background:var(--surface);color:var(--text);font:inherit;font-size:13px;line-height:1.55;box-sizing:border-box;resize:vertical;min-height:80px;" placeholder="(空 — patch 提案無し)">{_escape(patch_display)}</textarea>
    </div>
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="コメント (任意、status 変えずコメントだけも可)">
      </div>
      <button type="submit" name="action" value="accept" class="btn accept">✓ Accept</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ Reject</button>
      <button type="submit" name="action" value="noted" class="btn">📝 Noted</button>
      <button type="submit" name="action" value="comment" class="btn comment">💬 Comment のみ</button>
      <button type="submit" name="action" value="escalate" class="btn fix" title="この item を システム修正依頼として再分類 (= bot crash / error fallback / retrieval bug 等の真因対応)">🐛 システム修正に格上げ</button>
    </div>
  </form>
</div>""")

    body = "\n".join(parts)
    return _html_envelope("発見ダイジェスト", "回答品質向上 — 会話発見 + 海山直接入力 + システム不備に格上げ可能",
                          body, "/admin/review/learning", token, flash)


# ─── Feedback page (= 社員修正希望) ──────────────────
def render_feedback_page(token: str, flash: Optional[str] = None) -> str:
    try:
        import clone_feedback
        items = clone_feedback.list_pending(limit=30)
    except Exception as e:
        return _html_envelope("社員修正希望", "Feedback queue 読込失敗",
                              f'<div class="empty">{_escape(str(e))}</div>',
                              "/admin/review/feedback", token, flash)

    if not items:
        body = '<div class="empty">📭 pending な修正希望なし</div>'
        return _html_envelope("社員修正希望", "社員から届いた bot 応答 修正リクエスト",
                              body, "/admin/review/feedback", token, flash)

    parts = []
    for r in items:
        rid = r.get("id", "?")
        ts = r.get("timestamp", "")[:16]
        # ★完全匿名化: 社員 alias 表示
        user = _user_alias(r.get("user_id", ""))
        trigger = r.get("trigger_msg", "")[:200]
        response = r.get("response", "")[:300]
        feedback = r.get("feedback", "")
        backcheck = r.get("backcheck", {})
        verdict = backcheck.get("verdict", "") if backcheck else ""
        verdict_tag = f'<span class="tag pending">{_escape(verdict)}</span>' if verdict else ""

        parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{verdict_tag} {_escape(feedback[:120])}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · {_escape(user)} · <code>{_escape(rid)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>元 query:</strong> {_escape(trigger)}</div>
  <div class="item-body"><strong>bot 応答:</strong> {_escape(response)}</div>
  <div class="item-body"><strong>修正内容:</strong> {_escape(feedback)}</div>
  <form method="POST" action="/admin/review/feedback/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="コメント (任意、status 変えずコメントだけも可)">
      </div>
      <button type="submit" name="action" value="accept" class="btn accept">✓ Accept</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ Reject</button>
      <button type="submit" name="action" value="noted" class="btn">📝 Noted</button>
      <button type="submit" name="action" value="comment" class="btn comment">💬 Comment のみ</button>
    </div>
  </form>
</div>""")

    body = f'<h2>📭 Pending 修正希望 <span class="count-badge">{len(items)}</span></h2>' + "\n".join(parts)
    return _html_envelope("社員修正希望", "社員から届いた bot 応答 修正リクエスト",
                          body, "/admin/review/feedback", token, flash)


# ─── Audit page (= needs_attention + 未 audit、3 actions) ──
def render_audit_page(token: str, flash: Optional[str] = None) -> str:
    try:
        import clone_audit
        stats = clone_audit.audit_stats(days=30)
        recent_unrated = clone_audit.list_recent_unrated(limit=10)
    except Exception as e:
        # ★2026-05-25: audit module 失敗時でも直接入力 form は出す (= 海山が即報告できる)
        return _html_envelope("Audit", "audit module 読込失敗",
                              _render_action_form(token, default_mode="system") +
                              f'<div class="empty">{_escape(str(e))}</div>',
                              "/admin/review/audit", token, flash)

    n_total = stats.get("n_total_audits", 0)
    n_good = stats.get("n_good", 0)
    n_bad = stats.get("n_bad", 0)
    n_fix = stats.get("n_fix", 0)
    good_rate = stats.get("good_rate_pct", 0)
    needs = stats.get("needs_attention", [])

    # ★2026-05-25 海山指示: top に「直接入力」 form widget (audit page = システム寄り default)
    parts = [_render_action_form(token, default_mode="system"), f"""
<h2>📊 統計 (= 過去 30 日)</h2>
<div class="kpi-grid">
  <div class="kpi"><div class="label">total</div><div class="value">{n_total}</div></div>
  <div class="kpi success"><div class="label">good ○</div><div class="value">{n_good}</div></div>
  <div class="kpi danger"><div class="label">bad ×</div><div class="value">{n_bad}</div></div>
  <div class="kpi warning"><div class="label">fix !</div><div class="value">{n_fix}</div></div>
  <div class="kpi {"success" if good_rate >= 80 else ("warning" if good_rate >= 60 else "danger")}">
    <div class="label">good 率</div><div class="value">{good_rate}<span class="unit">%</span></div>
  </div>
</div>
"""]

    # needs_attention (= 過去 audit で bad/fix のもの、follow-up actions)
    parts.append(f'<h2>⚠️ 要 attention <span class="count-badge">{len(needs)}</span></h2>')
    if not needs:
        parts.append('<div class="empty">📭 要 attention なし — good rate 良好</div>')
    else:
        for r in needs[-20:]:
            v = r.get("verdict", "?")
            v_class = "bad" if v == "bad" else ("fix" if v == "fix" else "neutral")
            rid = r.get("id", "")
            ts = r.get("ts", "")[:16]
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title"><span class="tag {v_class}">{_escape(v)}</span> {_escape(r.get("user_query", "")[:120])}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · <code>{_escape(rid)}</code></div>
    </div>
  </div>
  <div class="item-body"><strong>bot 応答:</strong> {_escape(r.get("bot_response", "")[:200])}</div>
  {f'<div class="item-body"><strong>note:</strong> {_escape(r.get("note", ""))}</div>' if r.get("note") else ''}
  <form method="POST" action="/admin/review/audit/action?token={token}">
    <input type="hidden" name="id" value="{_escape(rid)}">
    <input type="hidden" name="source" value="needs_attention">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="follow-up コメント / 修正内容">
      </div>
      <button type="submit" name="action" value="accept" class="btn accept">✓ Accept (= 対応済)</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ Reject (= 問題なし)</button>
      <button type="submit" name="action" value="fix" class="btn fix">🔧 コメント付き修正</button>
      <button type="submit" name="action" value="escalate" class="btn fix" title="システム修正依頼に格上げ">🐛 システム修正に格上げ</button>
      <button type="submit" name="action" value="resolve" class="btn" title="この item を list から消す (= 対応済として閉じる)">✅ 解決済 (= 閉じる)</button>
    </div>
  </form>
</div>""")

    # 未 audit (= 直近 bot 応答、3 verdicts)
    parts.append(f'<h2>📥 未 audit <span class="count-badge">{len(recent_unrated)}</span></h2>')
    if not recent_unrated:
        parts.append('<div class="empty">未 audit ゼロ — 全 response review 済</div>')
    else:
        for it in recent_unrated:
            ch = it.get("channel_id")
            # ★完全匿名化: channel も グループ alias 表示
            ch_tag = (f'<span class="tag accent">{_channel_alias(ch)}</span>'
                      if ch else '<span class="tag neutral">DM</span>')
            user_str = _user_alias(it.get("user_id", ""))
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{ch_tag} #{it.get('index', '?')} · {_escape(user_str)}: {_escape(it.get('user_query', '')[:100])}</div>
      <div class="item-meta" style="margin-top: 4px;">{it.get('ts', '')[:16]}</div>
    </div>
  </div>
  <div class="item-body"><strong>bot 応答:</strong> {_escape(it.get('bot_response', '')[:300])}</div>
  <form method="POST" action="/admin/review/audit/action?token={token}">
    <input type="hidden" name="index" value="{it.get('index', '')}">
    <input type="hidden" name="source" value="unrated">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="修正内容 / コメント (fix の時必須)">
      </div>
      <button type="submit" name="action" value="accept" class="btn accept">✓ Accept (= good)</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ Reject (= bad)</button>
      <button type="submit" name="action" value="fix" class="btn fix">🔧 コメント付き修正</button>
      <button type="submit" name="action" value="escalate" class="btn fix" title="bot 応答の不備をシステム修正依頼に格上げ (= crash / fallback / retrieval bug 等の真因対応)">🐛 システム修正に格上げ</button>
    </div>
  </form>
</div>""")

    body = "\n".join(parts)
    return _html_envelope("Audit Dashboard", "海山評価 + 要 attention + 未 audit (= 一件ずつ 品質 / システム 分類可)",
                          body, "/admin/review/audit", token, flash)


# ─── Research page ──────────────────────────────────
def render_research_page(token: str, flash: Optional[str] = None) -> str:
    app_root = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
    research_dir = app_root / "data" / "brain" / "ai_research"
    proposals = []
    prop_file = research_dir / "proposals.jsonl"
    if prop_file.exists():
        try:
            for ln in prop_file.read_text(encoding="utf-8").splitlines():
                if not ln.strip(): continue
                try:
                    proposals.append(json.loads(ln))
                except Exception: continue
        except Exception: pass

    pending = [p for p in proposals if p.get("status") == "pending"]
    accepted = [p for p in proposals if p.get("status") == "accepted"]

    parts = [f'<h2>💡 Pending 提案 <span class="count-badge">{len(pending)}</span></h2>']
    if not pending:
        parts.append('<div class="empty">📭 pending 提案なし · /research-run で取得 or 月曜 09:30 自動実行待ち</div>')
    else:
        for p in pending[-20:]:
            pid = p.get("id", "?")
            title = p.get("title", "?")
            body_text = p.get("body", "")[:600]
            ts = p.get("ts", "")[:16]
            parts.append(f"""
<div class="item">
  <div class="item-head">
    <div>
      <div class="item-title">{_escape(title)}</div>
      <div class="item-meta" style="margin-top: 4px;">{ts} · <code>{_escape(pid)}</code></div>
    </div>
  </div>
  <div class="item-body">{_escape(body_text)}</div>
  <form method="POST" action="/admin/review/research/action?token={token}">
    <input type="hidden" name="id" value="{_escape(pid)}">
    <div class="actions">
      <div class="comment-row">
        <input type="text" name="note" placeholder="コメント (任意)">
      </div>
      <button type="submit" name="action" value="accept" class="btn accept">✓ Accept</button>
      <button type="submit" name="action" value="reject" class="btn reject">✕ Reject</button>
      <button type="submit" name="action" value="comment" class="btn comment">💬 Comment のみ</button>
    </div>
  </form>
</div>""")

    parts.append(f'<h2>📄 Digest 履歴 <span class="count-badge">{len(list(research_dir.glob("*-digest.md"))) if research_dir.exists() else 0}</span></h2>')
    digest_files = sorted(research_dir.glob("*-digest.md"), reverse=True)[:10] if research_dir.exists() else []
    if not digest_files:
        parts.append('<div class="empty">📭 digest 履歴なし</div>')
    else:
        parts.append('<table><tr><th>date</th><th>size</th><th>preview</th></tr>')
        for f in digest_files:
            try:
                content = f.read_text(encoding="utf-8")
                preview = ""
                for ln in content.splitlines()[:20]:
                    if ln.startswith("### "):
                        preview = ln[4:][:80]; break
                size_kb = f.stat().st_size / 1024
                parts.append(f'<tr><td><code>{f.stem}</code></td><td style="color: var(--text-3);">{size_kb:.1f}KB</td><td style="color: var(--text-2);">{_escape(preview)}</td></tr>')
            except Exception: continue
        parts.append('</table>')

    body = "\n".join(parts)
    return _html_envelope("AI Research", "週次 世界 AI 進化 + 当 PJ への反映提案",
                          body, "/admin/review/research", token, flash)


# ─── Memory page (= per-user list、clickable cards、プライバシー mask) ──
def render_memory_page(token: str, flash: Optional[str] = None) -> str:
    try:
        import clone_memory
        users = clone_memory.list_users()
    except Exception as e:
        return _html_envelope("Memory", "clone_memory 読込失敗",
                              f'<div class="empty">{_escape(str(e))}</div>',
                              "/admin/review/memory", token, flash)
    if not users:
        body = '<div class="empty">📭 memory 無し<br><span style="font-size: 12px;">社員が「うみやまAI」と会話 した後に蓄積されます</span></div>'
        return _html_envelope("Memory", "各社員の累積 memory",
                              body, "/admin/review/memory", token, flash)

    # ★2026-05-26 海山指示: ID 順 → 最新回答 (= last_updated) 降順 sort.
    # alias は idx でなく user_id 由来 (= _user_alias) を使って永続安定化、表示順だけ最新順。
    sorted_users = sorted(
        users,
        key=lambda u: (u.get("last_updated", "") or "", u.get("user_id", "")),
        reverse=True,
    )

    parts = [
        f'<h2>個別 memory <span class="count-badge">{len(users)}</span></h2>',
        '<p style="color: var(--text-2); font-size: 13.5px; margin-bottom: 16px;">'
        '社員からの会話を 蓄積した memory。<strong>プライバシー保護のため Dashboard 上は完全匿名化</strong> '
        '(= alias は user_id 永続マッピング)、backend には実 data そのまま保存。'
        '表示順は <strong>最新回答 (last_updated) 降順</strong>。クリックで詳細 + 会話履歴を確認。</p>',
    ]

    for u in sorted_users[:50]:
        uid = u.get("user_id", "")
        alias = _user_alias(uid)
        turn_count = u.get("turn_count", 0)
        size_kb = u.get("size", 0) / 1024
        updated = u.get("last_updated", "")[:16]

        parts.append(f"""
<a class="user-card" href="/admin/review/memory/{_escape(uid)}?token={token}">
  <div class="user-info">
    <div class="user-name">{_escape(alias)}</div>
    <div class="user-id-code">更新 {_escape(updated)}</div>
  </div>
  <div class="user-stats">
    <div class="stat">
      <div class="stat-value">{turn_count}</div>
      <div class="stat-label">turns</div>
    </div>
    <div class="stat">
      <div class="stat-value">{size_kb:.1f}<span style="font-size: 11px; color: var(--text-3);">KB</span></div>
      <div class="stat-label">size</div>
    </div>
    <div class="arrow">→</div>
  </div>
</a>""")

    body = "\n".join(parts)
    return _html_envelope("Memory", "各社員の累積 memory (= 匿名化表示)",
                          body, "/admin/review/memory", token, flash)


def render_memory_detail_page(user_id: str, token: str, flash: Optional[str] = None) -> str:
    """個別 user の memory 詳細 + 会話履歴 (= プライバシー mask 適用)."""
    try:
        import clone_memory
        import clone_history
    except Exception as e:
        return _html_envelope("Memory detail", "module 読込失敗",
                              f'<div class="empty">{_escape(str(e))}</div>',
                              "/admin/review/memory", token, flash)

    if not user_id:
        return _html_envelope("Memory detail", "user_id 未指定",
                              '<div class="empty">user_id が必要</div>',
                              "/admin/review/memory", token, flash)

    # memory load
    try:
        fm, mem_body = clone_memory.load_with_meta(user_id)
    except Exception:
        fm, mem_body = {}, ""

    alias = _user_alias(user_id)

    if not mem_body or mem_body == clone_memory.DEFAULT_BODY:
        body = (f'<div class="empty">📭 <strong>{_escape(alias)}</strong> の memory なし</div>')
        return _html_envelope("Memory detail", "user 個別 memory + 会話履歴",
                              body, "/admin/review/memory", token, flash)

    turn_count = int(fm.get("turn_count", "0") or "0")
    updated = fm.get("updated", "")[:16]

    parts = [f"""
<div style="background: var(--surface); padding: 24px 28px; border-radius: var(--radius); border: 1px solid var(--border-soft); margin-bottom: 24px;">
  <div style="font-family: var(--font-serif); font-size: 26px; font-weight: 500; letter-spacing: -0.01em;">{_escape(alias)}</div>
  <div style="color: var(--text-3); font-size: 12.5px; margin-top: 6px;">
    {turn_count} turns · 更新 {_escape(updated)}
  </div>
  <div style="margin-top: 14px;">
    <a class="quick-link" href="/admin/review/memory?token={token}" style="font-size: 12.5px;">← memory list に戻る</a>
  </div>
</div>
"""]

    # memory body 表示 (= md-like)
    # h2 → h3 として表示
    formatted = _escape(mem_body)
    # `## Section` を <h3> に
    import re as _re
    formatted = _re.sub(r'^## (.+)$', r'<h3>\1</h3>', formatted, flags=_re.MULTILINE)
    parts.append(f'<h2>memory 内容</h2><div class="memory-dump">{formatted}</div>')

    # 会話履歴 (= 直近 N 件、bubble 表示)
    parts.append(f'<h2>会話履歴 <span class="count-badge">直近 30 件</span></h2>')
    try:
        hist = clone_history.load_recent(user_id, n=30)
    except Exception:
        hist = []

    if not hist:
        parts.append('<div class="empty">📭 会話履歴 無し</div>')
    else:
        parts.append('<div class="chat">')
        # 古→新の順で表示 (= load_recent は古い順だが念のため)
        for r in hist:
            role = r.get("role", "")
            content = r.get("content", "")
            if not content:
                continue
            cls = "user" if role == "user" else "assistant"
            parts.append(f"""
<div class="chat-turn {cls}">
  <div class="bubble">{_escape(content)}</div>
  <div class="chat-meta">{cls}</div>
</div>""")
        parts.append('</div>')

    parts.append(
        '<div style="background: var(--surface); padding: 14px 18px; border-radius: var(--radius-sm);'
        ' border-left: 3px solid var(--accent); margin-top: 24px; font-size: 12.5px; color: var(--text-2);">'
        '<strong style="color: var(--text);">プライバシー注記</strong><br>'
        'Dashboard 上は完全匿名化 (= 社員 A, B, C...)。実名は backend (= clone_memory.md / '
        'clone_history.jsonl) にそのまま保存されますが、表示 layer で必ず alias に置換されます。'
        '社員への周知文に「Dashboard 表示は匿名化されます」 を明記推奨。</div>'
    )

    body = "\n".join(parts)
    return _html_envelope("Memory: " + alias, "個別 memory + 会話履歴 (= 完全匿名化)",
                          body, "/admin/review/memory", token, flash)


# ─── Group page ────────────────────────────────────
def render_group_page(token: str, flash: Optional[str] = None) -> str:
    try:
        import clone_group_context
        channels = clone_group_context.list_channels()
    except Exception as e:
        return _html_envelope("Group", "clone_group_context 読込失敗",
                              f'<div class="empty">{_escape(str(e))}</div>',
                              "/admin/review/group", token, flash)
    if not channels:
        body = '<div class="empty">📭 group context 無し<br><span style="font-size: 12px;">group にまだ追加 or mention 無し</span></div>'
        return _html_envelope("Group", "LINE WORKS group 集団文脈",
                              body, "/admin/review/group", token, flash)

    sorted_channels = sorted(channels, key=lambda c: c.get("channel_id", ""))

    parts = [
        f'<h2>group 一覧 <span class="count-badge">{len(channels)}</span></h2>',
        '<p style="color: var(--text-2); font-size: 13.5px; margin-bottom: 16px;">'
        'LINE WORKS group の集団文脈。<strong>Dashboard 上は完全匿名化</strong> '
        '(= グループ A・B・C …)、実 channel 名は backend のみ参照。</p>',
    ]

    for idx, c in enumerate(sorted_channels[:50]):
        alias = f"グループ {_alphabet_label(idx)}"
        turn_count = c.get("turn_count", 0)
        members = c.get("member_count", 0)
        size_kb = c.get("size", 0) / 1024
        updated = c.get("last_updated", "")[:16]

        parts.append(f"""
<div class="user-card" style="cursor: default;">
  <div class="user-info">
    <div class="user-name">{_escape(alias)}</div>
    <div class="user-id-code">更新 {_escape(updated)}</div>
  </div>
  <div class="user-stats">
    <div class="stat">
      <div class="stat-value">{turn_count}</div>
      <div class="stat-label">turns</div>
    </div>
    <div class="stat">
      <div class="stat-value">{members}</div>
      <div class="stat-label">members</div>
    </div>
    <div class="stat">
      <div class="stat-value">{size_kb:.1f}<span style="font-size: 11px; color: var(--text-3);">KB</span></div>
      <div class="stat-label">size</div>
    </div>
  </div>
</div>""")

    parts.append(
        '<div style="background: var(--surface); padding: 14px 18px; border-radius: var(--radius-sm);'
        ' border-left: 3px solid var(--accent); margin-top: 20px; font-size: 12.5px; color: var(--text-2);">'
        '<strong style="color: var(--text);">プライバシー注記</strong><br>'
        'Dashboard 上は完全匿名化 (= グループ A, B, C...)。実 channel 名 / メンバーは backend のみ。</div>'
    )
    body = "\n".join(parts)
    return _html_envelope("Group", "集団文脈 (= 完全匿名化)",
                          body, "/admin/review/group", token, flash)


# ─── Action handlers ────────────────────────────────
def handle_action(queue: str, action: str, item_id: str, note: str = "",
                  patch: str = "", patch_original: str = "") -> tuple[bool, str]:
    """learning / feedback / research の accept / reject / noted / comment action.

    action="comment" は status 変更なし、note のみ追記。
    ★2026-05-25: queue='learning' のみ patch (= proposed_wiki_patch の編集後値) +
    patch_original (= form 描画時の旧値) を受け取り、変更があれば update_patch を呼ぶ。
    patch / patch_original が一致すれば no-op (= 編集無し)。
    """
    # ★2026-05-25: system queue は別 action 名 (acknowledged / fixed / rejected / comment)
    if queue == "system":
        valid_system_actions = ("acknowledged", "fixed", "rejected", "comment")
        if action not in valid_system_actions:
            return False, f"unknown system action: {action} (valid: {valid_system_actions})"
        try:
            from services import system_issues
            ok_status = True
            if action != "comment":
                ok_status = system_issues.update_status(item_id, action)
            ok_comment = True
            if note.strip():
                ok_comment = system_issues.add_comment(item_id, note.strip(), reviewer="umiyama")
            ok = ok_status and ok_comment
        except Exception as e:
            logger.exception(f"system action {action}/{item_id}: {e}")
            return False, str(e)
        if not ok:
            return False, f"更新失敗 (id: {item_id})"
        msg = f"system_issue #{item_id[:20]} → {action}"
        if note.strip():
            msg += " (note 付き)"
        return True, msg

    # ★2026-05-25 海山指示: learning item を system_issue に格上げ分類
    if queue == "learning" and action == "escalate":
        try:
            import clone_learning
            sysi_id = clone_learning.escalate_to_system(item_id, note=note, reviewer="umiyama")
        except Exception as e:
            logger.exception(f"escalate {item_id}: {e}")
            return False, str(e)
        if not sysi_id:
            return False, f"escalate 失敗 (id: {item_id}、元 item 見つからず or system_issues エラー)"
        msg = f"learning #{item_id[:20]} → system_issue {sysi_id} に格上げ"
        return True, msg

    if action not in ("accept", "reject", "noted", "comment"):
        return False, f"unknown action: {action}"

    status_map = {"accept": "accepted", "reject": "rejected", "noted": "noted"}
    new_status = status_map.get(action)  # comment は None
    patch_changed = False  # ★2026-05-25: learning queue の patch 編集検知 flag

    try:
        if queue == "learning":
            import clone_learning
            ok_status = True
            if new_status:
                ok_status = clone_learning.update_status(item_id, new_status)
            ok_comment = True
            if note.strip():
                try:
                    ok_comment = clone_learning.add_comment(item_id, note.strip(), reviewer="umiyama")
                except AttributeError:
                    ok_comment = True  # add_comment 無くても warning だけ
            # ★2026-05-25: patch 変更検知 → update_patch 呼出 (= 編集が無ければ no-op)
            # textarea は browser が改行を \r\n に正規化することがあるため、比較前に LF 統一。
            ok_patch = True
            patch_norm = patch.replace("\r\n", "\n").replace("\r", "\n")
            patch_orig_norm = patch_original.replace("\r\n", "\n").replace("\r", "\n")
            if patch_norm != patch_orig_norm:
                try:
                    ok_patch = clone_learning.update_patch(item_id, patch_norm, reviewer="umiyama")
                    patch_changed = bool(ok_patch)
                except AttributeError:
                    ok_patch = True  # update_patch 無くても warning だけ
            ok = ok_status and ok_comment and ok_patch
        elif queue == "feedback":
            import clone_feedback
            ok_status = True
            if new_status:
                ok_status = clone_feedback.update_status(item_id, new_status)
            ok_comment = True
            if note.strip():
                try:
                    ok_comment = clone_feedback.add_comment(item_id, note.strip(), reviewer="umiyama")
                except AttributeError:
                    ok_comment = True
            ok = ok_status and ok_comment
        elif queue == "research":
            ok = _update_research_proposal(item_id, new_status or "noted", note=note)
        else:
            return False, f"unknown queue: {queue}"
    except Exception as e:
        logger.exception(f"action {queue}/{action}/{item_id}: {e}")
        return False, str(e)

    if not ok:
        return False, f"更新失敗 (id: {item_id})"

    msg = f"{queue} #{item_id[:20]} → {new_status or 'comment 追記'}"
    if note.strip():
        msg += f" (note 付き)"
    # ★2026-05-25: patch 編集も msg に反映 (= learning queue のみ判定可能)
    if patch_changed:
        msg += " (patch 更新)"
    return True, msg


def handle_audit_action(action: str, source: str, item_id: str = "",
                        index: str = "", note: str = "") -> tuple[bool, str]:
    """audit page 用 action.

    source:
      - 'needs_attention' (= 既存 audit record に follow-up note 付加 or 状態 update)
      - 'unrated' (= list_recent_unrated の index 指定で 新 audit record 作成)
    action:
      - 'accept' (= good)
      - 'reject' (= bad)
      - 'fix' (= fix + note 必須)
    """
    # ★2026-05-26 海山指示: needs_attention list から「対応済」として閉じる
    if action == "resolve":
        if source != "needs_attention":
            return False, "resolve は needs_attention のみ対応"
        if not item_id:
            return False, "id 必須"
        try:
            import clone_audit
            ok = clone_audit.mark_resolved(item_id, resolved_by="umiyama", note=note)
            if not ok:
                return False, f"resolve 失敗 (id={item_id})"
            return True, f"{item_id[:20]} → ✅ resolved (= list から外れる)"
        except Exception as e:
            logger.exception(f"audit resolve {item_id}: {e}")
            return False, str(e)

    # ★2026-05-25 海山指示: unrated / needs_attention の bot 応答を system_issue に格上げ
    if action == "escalate":
        try:
            import clone_audit
            from services import system_issues
            target_q, target_r = "", ""
            if source == "unrated":
                if not index or not index.isdigit():
                    return False, "escalate 時も index 必須 (unrated)"
                idx = int(index)
                candidates = clone_audit.list_recent_unrated(limit=20)
                if idx < 1 or idx > len(candidates):
                    return False, f"index {idx} は範囲外"
                it = candidates[idx - 1]
                target_q = it.get("user_query", "")
                target_r = it.get("bot_response", "")
            elif source == "needs_attention":
                # 既存 audit record から取得
                audit_dir = Path(os.getenv("BRAIN_ROOT", "/app/data/brain")) / "clone_audit"
                if audit_dir.exists():
                    for jf in sorted(audit_dir.glob("*.jsonl"), reverse=True):
                        found = False
                        for ln in jf.read_text(encoding="utf-8").splitlines():
                            if not ln.strip():
                                continue
                            try:
                                rec = json.loads(ln)
                                if rec.get("id") == item_id:
                                    target_q = rec.get("user_query", "")
                                    target_r = rec.get("bot_response", "")
                                    found = True
                                    break
                            except Exception:
                                continue
                        if found:
                            break
            else:
                return False, f"escalate 不可 source: {source}"

            description = "[bot 応答 不備 — audit から escalate]"
            if target_q:
                description += f"\n[USER 質問]\n{target_q}"
            if target_r:
                description += f"\n[BOT 応答]\n{target_r}"
            expected = (note or "").strip() or "(期待動作未記入)"
            sysi_id = system_issues.add_entry(description, expected=expected, reviewer="umiyama")
            # 元 audit にも fix verdict で record (= 履歴残し、note に reference)
            try:
                clone_audit.record_audit(
                    audited_by="umiyama_via_dashboard",
                    target_user_id="(escalated)",
                    user_query=target_q,
                    bot_response=target_r,
                    verdict="fix",
                    note=f"→ system_issue {sysi_id} に格上げ" + (f" / {note}" if note.strip() else ""),
                )
            except Exception as e:
                logger.warning(f"audit record (escalate ref) failed: {e}")
            return True, f"audit {source} → system_issue {sysi_id} に格上げ"
        except Exception as e:
            logger.exception(f"audit escalate {source}/{item_id or index}: {e}")
            return False, str(e)

    if action not in ("accept", "reject", "fix"):
        return False, f"unknown action: {action}"

    verdict_map = {"accept": "good", "reject": "bad", "fix": "fix"}
    verdict = verdict_map[action]

    try:
        import clone_audit
        if source == "unrated":
            # 未 audit から新 record 作成
            if not index or not index.isdigit():
                return False, "index 必須"
            idx = int(index)
            candidates = clone_audit.list_recent_unrated(limit=20)
            if idx < 1 or idx > len(candidates):
                return False, f"index {idx} は範囲外 (1-{len(candidates)})"
            it = candidates[idx - 1]
            if verdict == "fix" and not note.strip():
                return False, "fix 時は note 必須"
            rec = clone_audit.record_audit(
                audited_by="umiyama_via_dashboard",
                target_user_id=it["user_id"],
                user_query=it["user_query"],
                bot_response=it["bot_response"],
                verdict=verdict,
                note=note,
                target_channel_id=it.get("channel_id"),
                ts_target=it["ts"],
            )
            return True, f"#{idx} → {verdict}" + (f" (note 付き)" if note.strip() else "")
        elif source == "needs_attention":
            # 既存 audit record に follow-up note 追記 + 新 status record
            # 簡易: 同 target に対し新 audit record を 1 件追加 (= 履歴重層化)
            # 既存 record の探索 + body 取得は省略、新 record だけ作る
            # → 今回は 「追加 audit record」 として記録、note 必須でなくても良い
            if verdict == "fix" and not note.strip():
                return False, "fix 時は note 必須"
            # 既存 needs_attention list から item 引きたいが、id 指定だけだと
            # 関連 query/response 取得できない → minimal record だけ
            audit_dir = Path(os.getenv("BRAIN_ROOT", "/app/data/brain")) / "clone_audit"
            target_q, target_r, target_ch = "", "", None
            target_uid = "follow_up"
            # 履歴から探す (= 線形 search、件数少ない前提)
            if audit_dir.exists():
                for jf in sorted(audit_dir.glob("*.jsonl"), reverse=True):
                    found = False
                    for ln in jf.read_text(encoding="utf-8").splitlines():
                        if not ln.strip(): continue
                        try:
                            rec = json.loads(ln)
                            if rec.get("id") == item_id:
                                target_q = rec.get("user_query", "")
                                target_r = rec.get("bot_response", "")
                                target_uid = rec.get("target_user_id", "follow_up")
                                target_ch = rec.get("target_channel_id")
                                found = True
                                break
                        except Exception: continue
                    if found: break
            rec = clone_audit.record_audit(
                audited_by="umiyama_via_dashboard",
                target_user_id=target_uid,
                user_query=target_q,
                bot_response=target_r,
                verdict=verdict,
                note=note + f" (follow-up to {item_id})" if note else f"follow-up to {item_id}",
                target_channel_id=target_ch,
            )
            return True, f"{item_id[:20]} に follow-up: {verdict}"
        else:
            return False, f"unknown source: {source}"
    except Exception as e:
        logger.exception(f"audit action {action}/{source}/{item_id}: {e}")
        return False, str(e)


def _update_research_proposal(item_id: str, status: str, note: str = "") -> bool:
    app_root = Path(os.getenv("BRAIN_APP_ROOT", "/app"))
    prop_file = app_root / "data" / "brain" / "ai_research" / "proposals.jsonl"
    if not prop_file.exists():
        return False
    lines = prop_file.read_text(encoding="utf-8").splitlines()
    updated = []
    matched = False
    for ln in lines:
        ln = ln.strip()
        if not ln: continue
        try:
            p = json.loads(ln)
            if p.get("id") == item_id:
                # comment action なら status 維持、note 追記
                if status != "noted" or not note:
                    p["status"] = status
                p["reviewed_at"] = datetime.now(JST).isoformat()
                if note.strip():
                    notes = p.get("notes", [])
                    notes.append({"ts": datetime.now(JST).isoformat(), "note": note.strip()})
                    p["notes"] = notes
                matched = True
            updated.append(p)
        except Exception: continue
    if not matched:
        return False
    prop_file.write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in updated) + "\n",
        encoding="utf-8",
    )
    return True
