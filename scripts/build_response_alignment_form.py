#!/usr/bin/env python3
"""
build_response_alignment_form.py — 想定 30 質問への模範回答を海山本人に記入してもらう
HTML フォーム生成。

出力: data/brain/alignment/response_alignment_form.html
- 単一 HTML (CSS / JS / データ inline)
- 30 問 × 6 カテゴリ
- localStorage 自動保存
- JSON ダウンロード / アップロード (再開)
- 進捗バー
- 各問に想定スケール表示 (XS / S / M / L) — 応答 length のガイド
- 各問にメタ情報 (どの style 軸を検証する想定か) を hint で表示
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "data" / "brain" / "alignment" / "response_alignment_form.html"

QUESTIONS = [
    # === A. 軽い雑談 (S スケール検証) ===
    {"id": "q1", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "最近、休日は何してる?", "hint": "S スケール (3-5 行)、カジュアル語尾"},
    {"id": "q2", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "最近ハマってる食べ物は?", "hint": "S スケール、具体 1-2 個"},
    {"id": "q3", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "普段、移動中は何してる?", "hint": "S スケール、自分の習慣を素朴に"},
    {"id": "q4", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "最近観た映画 / 読んだ本で印象的だったのは?", "hint": "S スケール、1 つ + 短い感想"},
    {"id": "q5", "category": "A", "category_label": "軽い雑談", "scale": "S", "q": "ストレス溜まった時どうしてる?", "hint": "S スケール、自分の対処を素朴に"},

    # === B. 経営判断 (M/L スケール、思考軸) ===
    {"id": "q6", "category": "B", "category_label": "経営判断", "scale": "M-L", "q": "30 億の投資判断、何を見て決める?", "hint": "M/L スケール、判断軸を 2-3 個"},
    {"id": "q7", "category": "B", "category_label": "経営判断", "scale": "M", "q": "不採算店舗の閉店、いつ判断する?", "hint": "M スケール、判断基準と例外"},
    {"id": "q8", "category": "B", "category_label": "経営判断", "scale": "M-L", "q": "海外進出、どの国を優先する?", "hint": "M/L スケール、選定軸"},
    {"id": "q9", "category": "B", "category_label": "経営判断", "scale": "M", "q": "競合と価格戦争になりそうな時の判断軸は?", "hint": "M スケール、避けるか戦うか"},
    {"id": "q10", "category": "B", "category_label": "経営判断", "scale": "M-L", "q": "M&A の話が来た時、最初に何を見る?", "hint": "M/L スケール、最初の見立て"},

    # === C. キャリア・人生相談 ===
    {"id": "q11", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "30 代でキャリア迷ってる後輩にどう声かける?", "hint": "M スケール、コーティング (ニヒル含む)"},
    {"id": "q12", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "会社辞めるか迷ってる社員に何て言う?", "hint": "M スケール、押し付けない"},
    {"id": "q13", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "起業したいって相談されたら何を確認する?", "hint": "M スケール、確認したい点"},
    {"id": "q14", "category": "C", "category_label": "キャリア相談", "scale": "M", "q": "燃え尽きそうな部下にどう接する?", "hint": "M スケール、距離感"},
    {"id": "q15", "category": "C", "category_label": "キャリア相談", "scale": "S-M", "q": "やる気が出ない時、自分はどうしてる?", "hint": "S/M スケール、自虐 OK"},

    # === D. 自伝・回想 ===
    {"id": "q16", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "フランス留学で一番影響を受けたことは?", "hint": "M スケール、回想を素朴に (経歴自慢 NG)"},
    {"id": "q17", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "20 代で何を考えてた?", "hint": "M スケール、淡々と"},
    {"id": "q18", "category": "D", "category_label": "自伝・回想", "scale": "M-L", "q": "OWNDAYS が一番しんどかった時期はどう乗り越えた?", "hint": "M/L スケール、苦労を多めに、勝利を少なめに"},
    {"id": "q19", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "東南アジアでの生活で身についたことは?", "hint": "M スケール"},
    {"id": "q20", "category": "D", "category_label": "自伝・回想", "scale": "M", "q": "起業を決めた瞬間のことは覚えてる?", "hint": "M スケール"},

    # === E. 価値観・哲学 ===
    {"id": "q21", "category": "E", "category_label": "価値観", "scale": "M", "q": "「Take Bold Risks」 を社員に説明するとしたら?", "hint": "M スケール、自分の言葉で"},
    {"id": "q22", "category": "E", "category_label": "価値観", "scale": "M", "q": "「正しさより美しさ」 ってどういう意味?", "hint": "M スケール、内面起点で"},
    {"id": "q23", "category": "E", "category_label": "価値観", "scale": "M", "q": "成功とは何か、いま改めて聞かれたら?", "hint": "M スケール、コーティング推奨 (クサくならないように)"},
    {"id": "q24", "category": "E", "category_label": "価値観", "scale": "M", "q": "リーダーシップで一番大事だと思うことは?", "hint": "M スケール、教科書臭ナシ"},
    {"id": "q25", "category": "E", "category_label": "価値観", "scale": "S-M", "q": "死ぬまでにやりたいことは?", "hint": "S/M スケール、軽さも含む"},

    # === F. 業務オペレーション ===
    {"id": "q26", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "新店候補地を見る時、何を最初にチェックする?", "hint": "M スケール、現場感覚"},
    {"id": "q27", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "不調店舗の見立て、どう立てる?", "hint": "M スケール、見るポイント"},
    {"id": "q28", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "店長候補を選ぶ時、何を見る?", "hint": "M スケール、判断基準"},
    {"id": "q29", "category": "F", "category_label": "業務オペ", "scale": "S-M", "q": "クレーム対応で大事にしてることは?", "hint": "S/M スケール"},
    {"id": "q30", "category": "F", "category_label": "業務オペ", "scale": "M", "q": "売上が落ちた時、まずどこを疑う?", "hint": "M スケール、見方の順序"},
]


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>海山丈司 — 想定 30 質問 模範回答記入フォーム</title>
<style>
:root {
  --bg: #fafafa;
  --card-bg: #fff;
  --border: #e0e0e0;
  --text: #1a1a1a;
  --muted: #666;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --success: #10b981;
  --tag-a: #fef3c7;
  --tag-b: #dbeafe;
  --tag-c: #fce7f3;
  --tag-d: #d1fae5;
  --tag-e: #ede9fe;
  --tag-f: #fed7aa;
}
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Yu Gothic", sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 0; line-height: 1.6; }
header { position: sticky; top: 0; z-index: 100; background: var(--card-bg); border-bottom: 1px solid var(--border); padding: 12px 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
.header-inner { max-width: 920px; margin: 0 auto; }
h1 { margin: 0 0 8px; font-size: 18px; font-weight: 600; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.btn { padding: 6px 14px; border: 1px solid var(--primary); background: var(--primary); color: white; border-radius: 6px; cursor: pointer; font-size: 13px; }
.btn:hover { background: var(--primary-hover); }
.btn-outline { background: white; color: var(--primary); }
.btn-outline:hover { background: #eff6ff; }
.progress { margin-top: 8px; height: 6px; background: #e5e7eb; border-radius: 3px; overflow: hidden; }
.progress-bar { height: 100%; background: var(--success); transition: width 0.3s; }
.progress-text { font-size: 12px; color: var(--muted); margin-top: 4px; }
main { max-width: 920px; margin: 0 auto; padding: 16px; }
.category { margin-bottom: 24px; }
.category h2 { font-size: 16px; margin: 0 0 12px; padding: 6px 10px; border-radius: 4px; }
.cat-A h2 { background: var(--tag-a); }
.cat-B h2 { background: var(--tag-b); }
.cat-C h2 { background: var(--tag-c); }
.cat-D h2 { background: var(--tag-d); }
.cat-E h2 { background: var(--tag-e); }
.cat-F h2 { background: var(--tag-f); }
.question { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.question.answered { border-left: 4px solid var(--success); }
.question .meta { display: flex; gap: 8px; font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.question .meta .scale { padding: 1px 6px; background: #f3f4f6; border-radius: 3px; }
.question .q-text { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
.question .hint { font-size: 12px; color: var(--muted); margin-bottom: 8px; font-style: italic; }
.question textarea { width: 100%; min-height: 70px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 4px; font-family: inherit; font-size: 14px; resize: vertical; }
.question textarea:focus { outline: none; border-color: var(--primary); }
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #1f2937; color: white; padding: 10px 16px; border-radius: 6px; font-size: 13px; z-index: 1000; opacity: 0; transition: opacity 0.3s; }
.toast.show { opacity: 1; }
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <h1>海山丈司 — 想定 30 質問 模範回答 記入フォーム</h1>
    <div class="controls">
      <button class="btn" onclick="exportJSON()">JSON ダウンロード</button>
      <button class="btn btn-outline" onclick="document.getElementById('upload').click()">JSON 読み込み</button>
      <input type="file" id="upload" style="display:none" accept=".json" onchange="importJSON(event)" />
      <button class="btn btn-outline" onclick="clearAll()">全クリア</button>
    </div>
    <div class="progress"><div class="progress-bar" id="progress-bar" style="width:0%"></div></div>
    <div class="progress-text" id="progress-text">記入 0 / 30 問 (0%)</div>
  </div>
</header>
<main id="form-container"></main>
<div class="toast" id="toast"></div>
<script>
const QUESTIONS = __QUESTIONS_JSON__;
const STORAGE_KEY = 'response_alignment_v1';
function loadAnswers() { try { return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch (e) { return {}; } }
function saveAnswers(a) { localStorage.setItem(STORAGE_KEY, JSON.stringify(a)); updateProgress(); }
function updateProgress() {
  const a = loadAnswers();
  const filled = Object.keys(a).filter(k => (a[k] || '').trim().length > 0).length;
  document.getElementById('progress-bar').style.width = (filled / QUESTIONS.length * 100) + '%';
  document.getElementById('progress-text').textContent = `記入 ${filled} / ${QUESTIONS.length} 問 (${Math.round(filled / QUESTIONS.length * 100)}%)`;
  document.querySelectorAll('.question').forEach(el => {
    const qid = el.dataset.qid;
    if (a[qid] && a[qid].trim()) el.classList.add('answered'); else el.classList.remove('answered');
  });
}
function showToast(msg) { const t = document.getElementById('toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 2000); }
function exportJSON() {
  const a = loadAnswers();
  const data = { schema_version: 'response-bank-v1', exported_at: new Date().toISOString(), total: QUESTIONS.length, filled: Object.keys(a).filter(k => (a[k] || '').trim()).length, answers: a };
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a2 = document.createElement('a');
  a2.href = url; a2.download = `umiyama_response_alignment_${new Date().toISOString().slice(0,10)}.json`; a2.click();
  showToast('JSON ダウンロード');
}
function importJSON(event) {
  const f = event.target.files[0]; if (!f) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    try { const data = JSON.parse(e.target.result); const a = data.answers || data; localStorage.setItem(STORAGE_KEY, JSON.stringify(a)); renderForm(); showToast('読み込み完了'); }
    catch (err) { showToast('JSON エラー: ' + err.message); }
  };
  reader.readAsText(f);
}
function clearAll() { if (confirm('全ての回答をクリアしますか?')) { localStorage.removeItem(STORAGE_KEY); renderForm(); showToast('クリア完了'); } }
function renderForm() {
  const a = loadAnswers();
  const cats = {};
  QUESTIONS.forEach(q => { if (!cats[q.category]) cats[q.category] = { label: q.category_label, qs: [] }; cats[q.category].qs.push(q); });
  const html = Object.entries(cats).map(([cat, info]) => `
    <section class="category cat-${cat}">
      <h2>${cat}. ${info.label} (${info.qs.length} 問)</h2>
      ${info.qs.map(q => `
        <div class="question" data-qid="${q.id}">
          <div class="meta"><span class="scale">${q.scale}</span><span>${q.id.toUpperCase()}</span></div>
          <div class="q-text">${q.q}</div>
          <div class="hint">${q.hint}</div>
          <textarea data-qid="${q.id}" oninput="onInput(this)">${(a[q.id] || '').replace(/</g, '&lt;')}</textarea>
        </div>
      `).join('')}
    </section>
  `).join('');
  document.getElementById('form-container').innerHTML = html;
  updateProgress();
}
function onInput(ta) {
  const a = loadAnswers(); a[ta.dataset.qid] = ta.value; saveAnswers(a);
}
renderForm();
</script>
</body>
</html>
"""


def main():
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    html = HTML_TEMPLATE.replace("__QUESTIONS_JSON__", json.dumps(QUESTIONS, ensure_ascii=False))
    OUT_FILE.write_text(html, encoding="utf-8")
    print(f"Generated: {OUT_FILE}")
    print(f"  Open in browser: file://{OUT_FILE}")
    print(f"  Total: {len(QUESTIONS)} questions in 6 categories")


if __name__ == "__main__":
    main()
