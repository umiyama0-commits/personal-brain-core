"""services/persona_form.py — 人格補完 回答式フォーム 50問 (★2026-07-04 海山指示)。

wiki 採掘 (2026-07-04、43件をレビュー投入済) で「どの既存データにも存在しない」と確定した
空白だけを狙い撃つ 50 問。既知の情報 (食=肉/グミ、旅=無計画、体験>モノ 等) は聞き直さない。

配管: GET /persona-form (token 認証、fail-closed) → 回答 (localStorage 下書き、部分送信可) →
POST /api/persona-form/submit → transcript 形式に整形 → alignment_interview.record_session
(source="form") → 音声と同一の蒸留 (chunk/較正/話者帰属) → レビュー待ち → /align-voice 採用。
本人の能動回答なので coverage 加点は正当 (受動採掘と違い bump してよい)。
"""
from __future__ import annotations

import json
from datetime import datetime

# ─── 50問 (dim ごと、採掘で確定した空白のみ) ───────────────────
# q は音声 probe と同じ「車内でも答えられる」トーン。answer は話し言葉のままで良い。
QUESTIONS: list[dict] = [
    # ── 自伝的記憶 (episodic_memory) 8問 ──
    {"id": "ep1", "dim": "episodic_memory", "q": "人生で何度も思い出す場面を1つ。いつ・どこで・誰と・何を感じたかまで。"},
    {"id": "ep2", "dim": "episodic_memory", "q": "もう1つ、何度も思い出す場面。前の質問とは別の時期のもので。"},
    {"id": "ep3", "dim": "episodic_memory", "q": "自分の事業で初めて「売れた」「客が来た」日のこと、覚えてる範囲で。"},
    {"id": "ep4", "dim": "episodic_memory", "q": "一番古い記憶は? 何歳ごろ、どんな場面?"},
    {"id": "ep5", "dim": "episodic_memory", "q": "忘れられない失敗の「場面」を1つ。教訓じゃなくて、その時の光景と感覚を。"},
    {"id": "ep6", "dim": "episodic_memory", "q": "忘れられない食事の場面。何を・どこで・誰と食べた時?"},
    {"id": "ep7", "dim": "episodic_memory", "q": "最後に泣いた(泣きそうになった)のはいつ、何で?"},
    {"id": "ep8", "dim": "episodic_memory", "q": "深夜にふと蘇ってくる場面ってある? どんなの?"},
    # ── 家族・プライベート (family_private) 6問 — 自分側の感情だけでOK ──
    {"id": "fa1", "dim": "family_private", "q": "家族といる時の自分は、会社の自分とどう違う? スイッチはどこで切り替わる?"},
    {"id": "fa2", "dim": "family_private", "q": "親から受け継いだと思うもの、逆に反面教師にしているもの。"},
    {"id": "fa3", "dim": "family_private", "q": "家で考え込んでいる時、家族にどう見えてると思う? 放っておいてほしい? 気づいてほしい?"},
    {"id": "fa4", "dim": "family_private", "q": "家族との食事で自分が勝手に守っている習慣・こだわりはある?"},
    {"id": "fa5", "dim": "family_private", "q": "子に一つだけ伝わってほしいこと(自分側の思いとして)。"},
    {"id": "fa6", "dim": "family_private", "q": "家族に感謝しているけど、ちゃんと言えていないこと。"},
    # ── 生活の嗜好 (taste_daily) 7問 — 服/酒/住/定番店は完全空白 ──
    {"id": "ta1", "dim": "taste_daily", "q": "服を選ぶ基準は? こだわる所と無頓着な所の境界線はどこ?"},
    {"id": "ta2", "dim": "taste_daily", "q": "酒は飲む? 好きな酒と飲み方、あるいは飲まない理由。"},
    {"id": "ta3", "dim": "taste_daily", "q": "コーヒー・お茶など、毎日の飲み物のルーティンは?"},
    {"id": "ta4", "dim": "taste_daily", "q": "住まいで譲れない条件を3つ挙げるなら?"},
    {"id": "ta5", "dim": "taste_daily", "q": "「通ってる」と言える定番の店はある? なぜそこ?"},
    {"id": "ta6", "dim": "taste_daily", "q": "苦手な食べ物・受け付けないものは?"},
    {"id": "ta7", "dim": "taste_daily", "q": "買って失敗したと思ったもの、最近だと何?"},
    # ── 個人のお金観 (money_personal) 6問 ──
    {"id": "mo1", "dim": "money_personal", "q": "個人の金で、財布が緩む対象と絞まる対象は?"},
    {"id": "mo2", "dim": "money_personal", "q": "会計の瞬間の癖ってある? 昔の金銭感覚の名残みたいなもの。"},
    {"id": "mo3", "dim": "money_personal", "q": "「資産」を一語で言うと? (数字/自由/安全/道具…)"},
    {"id": "mo4", "dim": "money_personal", "q": "個人の金でやった一番大きな「無駄遣い」と、今どう思ってるか。"},
    {"id": "mo5", "dim": "money_personal", "q": "子への金銭教育、どうしたい? 自分が受けた教育と変える?"},
    {"id": "mo6", "dim": "money_personal", "q": "金の不安を感じる瞬間は今でもある? どんな時?"},
    # ── 体・健康 (body_health) 6問 — 運動/睡眠は完全空白 ──
    {"id": "bo1", "dim": "body_health", "q": "運動の習慣はある? 何を、どのくらい? 無いなら体をどう保ってる?"},
    {"id": "bo2", "dim": "body_health", "q": "睡眠は何時間? 質はどう? 寝る前のルーティンある?"},
    {"id": "bo3", "dim": "body_health", "q": "エネルギーが切れる時の前兆は? 切れる前に自分で分かる?"},
    {"id": "bo4", "dim": "body_health", "q": "切れた後、何をすると戻る? 回復の手順を具体的に。"},
    {"id": "bo5", "dim": "body_health", "q": "「老い」を最初に感じた瞬間は? どこに来た?"},
    {"id": "bo6", "dim": "body_health", "q": "自分の体で信頼している所と、正直不安な所。"},
    # ── 笑いの型 (humor) 5問 ──
    {"id": "hu1", "dim": "humor", "q": "最近、声を出して笑ったのは何? その場にいた? 見てた?"},
    {"id": "hu2", "dim": "humor", "q": "自分の冗談の型は? 自虐・皮肉・ボケ・大喜利…昔からそう?"},
    {"id": "hu3", "dim": "humor", "q": "逆に「寒い」「つまらん」と感じる笑いは?"},
    {"id": "hu4", "dim": "humor", "q": "笑ってはいけない場面で笑いそうになった経験、ある?"},
    {"id": "hu5", "dim": "humor", "q": "家族や気心知れた相手にしか見せないふざけ方ってある?"},
    # ── 弱さ・後悔・矛盾 (shadow) 7問 — 司興業時代は完全空白 ──
    {"id": "sh1", "dim": "shadow", "q": "26歳で建設業(司興業)の代表をやっていた頃、一番きつかった/挫折した場面は?"},
    {"id": "sh2", "dim": "shadow", "q": "「飽きっぽさ」が実害を出した具体的なエピソードはある?"},
    {"id": "sh3", "dim": "shadow", "q": "今でもふと蘇るレベルの後悔は? それは消したい? 持っておきたい?"},
    {"id": "sh4", "dim": "shadow", "q": "人には言わないけど自分では分かっている弱点、1つだけなら?"},
    {"id": "sh5", "dim": "shadow", "q": "言ってる事とやってる事がズレてる自覚がある所は?"},
    {"id": "sh6", "dim": "shadow", "q": "昔コンプレックスだったもの。今はどうなった?"},
    {"id": "sh7", "dim": "shadow", "q": "誰にも見せていない顔があるとしたら、どんな顔?"},
    # ── 内的独白 (inner_voice) 5問 — 「頭の中の声」そのもの ──
    {"id": "iv1", "dim": "inner_voice", "q": "でかい決断の直前、頭の中で最後に鳴る言葉を「そのまま」書くと?"},
    {"id": "iv2", "dim": "inner_voice", "q": "落ち込んだ時の立て直しを実況中継風に。頭の中で何と言ってる?"},
    {"id": "iv3", "dim": "inner_voice", "q": "自分を叱る時の口調をそのまま。(例:「おい、○○だろ」みたいに)"},
    {"id": "iv4", "dim": "inner_voice", "q": "自分に言い訳する時・自分を騙す時の定型句ってある?"},
    {"id": "iv5", "dim": "inner_voice", "q": "寝る前、頭の中には何が流れてる? 今日の反省? 明日の段取り? 無?"},
]

DIM_META = {
    "episodic_memory": ("自伝的記憶", "場面の保存。教訓化しなくていい、光景と感覚のまま。"),
    "family_private": ("家族・プライベート", "書くのは自分側の感情だけでOK。家族本人の事実は書かなくていい。"),
    "taste_daily": ("生活の嗜好", "服・酒・住まい・定番店 — 今のデータに一切ない領域。"),
    "money_personal": ("個人のお金観", "事業の金じゃなく、自分の財布の癖。"),
    "body_health": ("体・健康", "態度と体感でOK。診断名や数値は書かなくていい(保存もしない)。"),
    "humor": ("笑いの型", "何で笑うかは人格の指紋。実例が一番効く。"),
    "shadow": ("弱さ・後悔・矛盾", "影のない人格は嘘くさい。書ける範囲で。"),
    "inner_voice": ("内的独白", "外に出る言葉じゃなく、頭の中の声をそのまま。ここが「脳の複製」の核心。"),
}

assert len(QUESTIONS) == 50, f"QUESTIONS must be 50, got {len(QUESTIONS)}"

_Q_BY_ID = {q["id"]: q for q in QUESTIONS}


def format_answers_transcript(answers: dict[str, str]) -> str:
    """回答 dict {qid: text} → 蒸留パイプラインに流す transcript 形式。
    未回答/空は除外、各回答は 2000 字で切る (蒸留 chunk 側の上限保護)。"""
    lines = [f"# 人格補完フォーム回答 ({datetime.now().strftime('%Y-%m-%d %H:%M')})"]
    n = 0
    for q in QUESTIONS:  # 質問定義順で安定出力
        a = (answers.get(q["id"]) or "").strip()
        if not a:
            continue
        lines.append(f"AI: {q['q']}")
        lines.append(f"海山: {a[:2000]}")
        n += 1
    if n == 0:
        return ""
    return "\n".join(lines) + "\n"


def build_form_html(config_url: str) -> str:
    """フォーム HTML (self-contained、スマホ対応、localStorage 下書き、部分送信可)。"""
    qjson = json.dumps(QUESTIONS, ensure_ascii=False)
    djson = json.dumps(DIM_META, ensure_ascii=False)
    return """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>人格補完 50問</title>
<style>
  :root { --bg:#1c1d1f; --card:#26282b; --ink:#d8d6d0; --sub:#8f8d86; --acc:#4a8577;
          --line:#3a3c40; --warn:#a8845c; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font-family:-apple-system,'Hiragino Sans',sans-serif; line-height:1.7; }
  .wrap { max-width:680px; margin:0 auto; padding:20px 16px 120px; }
  h1 { font-size:20px; font-weight:600; margin:8px 0 4px; }
  .lead { color:var(--sub); font-size:13px; margin-bottom:20px; }
  .sec { margin:28px 0 10px; }
  .sec h2 { font-size:16px; color:var(--acc); margin:0 0 2px; font-weight:600; }
  .sec p { font-size:12px; color:var(--sub); margin:0; }
  .qcard { background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:12px 14px; margin:10px 0; }
  .qcard.answered { border-color:var(--acc); }
  .qtext { font-size:14px; margin-bottom:8px; }
  textarea { width:100%; min-height:64px; background:#1a1b1d; color:var(--ink);
             border:1px solid var(--line); border-radius:8px; padding:10px;
             font-size:16px; font-family:inherit; resize:vertical; }
  textarea:focus { outline:none; border-color:var(--acc); }
  .bar { position:fixed; left:0; right:0; bottom:0; background:#202124f2;
         border-top:1px solid var(--line); padding:10px 16px;
         display:flex; gap:12px; align-items:center; backdrop-filter:blur(6px); }
  .prog { flex:1; font-size:13px; color:var(--sub); }
  button { background:var(--acc); color:#0d1512; border:none; border-radius:8px;
           padding:12px 20px; font-size:15px; font-weight:600; }
  button:disabled { background:#3a3c40; color:#777; }
  .msg { position:fixed; top:12px; left:50%; transform:translateX(-50%);
         background:var(--acc); color:#0d1512; padding:10px 18px; border-radius:8px;
         font-size:14px; display:none; z-index:9; }
  .note { font-size:11px; color:var(--sub); margin-top:6px; }
</style></head><body>
<div class="msg" id="msg"></div>
<div class="wrap">
  <h1>人格補完 50問</h1>
  <div class="lead">どのデータにも無い空白だけを聞く。話し言葉のまま1〜3行でOK。
  途中保存される(この端末)。答えた分だけ送信でき、何回に分けてもいい。
  送信後は音声と同じくレビュー(/align-voice)を通ってから人格に入る。</div>
  <div id="form"></div>
</div>
<div class="bar">
  <div class="prog" id="prog"></div>
  <button id="copy" style="background:#3a3c40;color:#d8d6d0">書き出し</button>
  <button id="send">回答した分を送信</button>
</div>
<script>
const QUESTIONS = __QJSON__;
const DIMS = __DJSON__;
const MODE = "__MODE__";               // "server" | "standalone"
const CONFIG_URL = "__CONFIG_URL__";   // server モードで使用
const API_BASE = "https://brain.example.com/api/persona-form/submit";
const KEY = "persona_form_v1";
const TKEY = "persona_form_token";

function endpoint() {
  if (MODE === "server") return CONFIG_URL;
  // standalone: token はファイルに埋め込まない (git に秘密を入れない)。初回に貼付 → 端末保存。
  let tok = localStorage.getItem(TKEY) || "";
  if (!tok) {
    tok = prompt("アクセストークン (voice-align と同じ) を貼り付け:") || "";
    if (tok.trim()) localStorage.setItem(TKEY, tok.trim());
  }
  return tok.trim() ? (API_BASE + "?token=" + encodeURIComponent(tok.trim())) : "";
}
let draft = {};
try { draft = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch(e) {}

const form = document.getElementById("form");
let curDim = "";
for (const q of QUESTIONS) {
  if (q.dim !== curDim) {
    curDim = q.dim;
    const [label, why] = DIMS[q.dim];
    const sec = document.createElement("div"); sec.className = "sec";
    sec.innerHTML = `<h2>${label}</h2><p>${why}</p>`;
    form.appendChild(sec);
  }
  const card = document.createElement("div"); card.className = "qcard"; card.id = "c_" + q.id;
  card.innerHTML = `<div class="qtext">${q.q}</div>` +
    `<textarea id="t_${q.id}" placeholder="話し言葉のまま、1〜3行でOK"></textarea>`;
  form.appendChild(card);
  const ta = card.querySelector("textarea");
  ta.value = draft[q.id] || "";
  if (ta.value.trim()) card.classList.add("answered");
  ta.addEventListener("input", () => {
    draft[q.id] = ta.value;
    localStorage.setItem(KEY, JSON.stringify(draft));
    card.classList.toggle("answered", !!ta.value.trim());
    updateProg();
  });
}
function answeredIds() {
  return QUESTIONS.filter(q => (draft[q.id] || "").trim()).map(q => q.id);
}
function updateProg() {
  document.getElementById("prog").textContent = `回答 ${answeredIds().length} / 50`;
  document.getElementById("send").disabled = answeredIds().length === 0;
}
updateProg();

function flash(t) {
  const m = document.getElementById("msg");
  m.textContent = t; m.style.display = "block";
  setTimeout(() => { m.style.display = "none"; }, 4000);
}

document.getElementById("send").addEventListener("click", async () => {
  const ids = answeredIds();
  if (!ids.length) return;
  if (!confirm(`${ids.length} 問ぶんを送信する? (未回答分は後日でOK)`)) return;
  const url = endpoint();
  if (!url) { flash("トークン未設定 — 送信できない (書き出しは可能)"); return; }
  const answers = {};
  for (const id of ids) answers[id] = draft[id];
  const btn = document.getElementById("send");
  btn.disabled = true; btn.textContent = "送信中…";
  try {
    const r = await fetch(url, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({answers}),
    });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.detail || r.status);
    for (const id of ids) delete draft[id];
    localStorage.setItem(KEY, JSON.stringify(draft));
    for (const id of ids) {
      const c = document.getElementById("c_" + id);
      c.querySelector("textarea").value = "(送信済)";
      c.querySelector("textarea").disabled = true;
      c.style.opacity = 0.55;
    }
    flash(`✅ ${j.saved} 問を送信。蒸留してレビュー(/align-voice)に載せる`);
  } catch(e) {
    if (String(e.message).includes("401")) localStorage.removeItem(TKEY);
    flash("送信失敗: " + e.message + " — 下書きは残ってる (書き出しも可)");
  }
  btn.textContent = "回答した分を送信";
  updateProg();
});

// 書き出し: 送信できない環境用の逃げ道。回答をテキスト化してコピー (LINE /memo 等へ)。
document.getElementById("copy").addEventListener("click", async () => {
  const ids = answeredIds();
  if (!ids.length) { flash("回答がまだ無い"); return; }
  const lines = ["# 人格補完フォーム回答 (手動書き出し)"];
  for (const q of QUESTIONS) {
    const a = (draft[q.id] || "").trim();
    if (!a) continue;
    lines.push("AI: " + q.q); lines.push("海山: " + a);
  }
  const text = lines.join("\\n");
  try { await navigator.clipboard.writeText(text); flash(`📋 ${ids.length} 問ぶんコピーした`); }
  catch(e) { prompt("コピーして使って:", text); }
});
</script></body></html>""".replace("__QJSON__", qjson).replace("__DJSON__", djson) \
    .replace("__CONFIG_URL__", config_url).replace("__MODE__", "server")


def build_standalone_html() -> str:
    """★2026-07-04 単体ファイル版 (入力形式 HTML)。どこでも開ける。トークンは埋め込まず
    初回貼付 → 端末保存 (git に秘密を残さない)。送信先は本番 API 固定 + 書き出し fallback。"""
    html = build_form_html("__UNUSED__")
    html = html.replace('const MODE = "server"', 'const MODE = "standalone"')
    # pre-commit end-of-file-fixer と整合 (末尾改行) — freshness test の一致条件
    return html if html.endswith("\n") else html + "\n"
