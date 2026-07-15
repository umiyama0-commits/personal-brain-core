"""services/voice_align_page.py — Vapi Web SDK 音声ページの HTML (★2026-07-13 main.py から移設 §1.12b).

adapter (交換可能): Vapi Web SDK 固有の JS はこの module に隔離 (§1.19②)。
音声プロバイダを替える時はこのページと services/voice_tools.py だけ差し替える。

─── Vapi Web SDK 経路 (telephony fee 無料化) ★2026-05-21 ───
電話番号経由は telephony per-minute fee がかかる。Web (WebRTC) 直結なら
telephony 料金 0、STT/LLM/TTS/Vapi platform fee のみ → 30-50% 削減。
同じ _build_voice_align_assistant_config() を共有するため動的 prompt
(継続性 + 薄い次元誘導) も完全に踏襲。end-of-call-report は同じ
/webhook/voice-alignment が受けて _process_voice_alignment へ流れるので
distillation pipeline (record_session → extract_session → wiki蒸留) は web/phone 同一動作。

使い方:
  1. Vapi dashboard → API Keys → Create Public Key で pk_... を発行
  2. .env に VAPI_PUBLIC_KEY=pk_... を追加 + (任意) VOICE_ALIGN_TOKEN=任意の文字列
  3. docker compose restart line-bot
  4. ブラウザで brain.example.com/voice-align?token=<VOICE_ALIGN_TOKEN>
  5. iPhone Safari なら「ホーム画面に追加」で PWA 風アイコンに
  6. 大きな丸ボタンをタップ → マイク許可 → 雑談 → 「またね」or 再タップで終了

★2026-07-13 診断計器 3 点 (海山報告「web で発話しても反応が無い」= silence-timed-out、
web 経由の transcript 成立実績 0 件 vs phone 22 件 = day-one の上り音声問題の切り分け用):
  - マイク入力レベルメーター (getUserMedia + AnalyserNode = ブラウザがマイクを拾えているか)
  - リアルタイム文字起こし表示 (vapi message/transcript = 発話が Vapi STT に届いているか)
  - エラー object の再帰 stringify ([object Object] 撲滅、endedReason も表示)
メーターが動く + 🎤 行が出ない → Vapi 側 STT / uplink 問題。メーターが動かない → ブラウザ/デバイス問題。
"""

_VOICE_ALIGN_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="うみやま音声">
<title>うみやま 音声アラインメント</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
  html, body {
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Hiragino Sans", sans-serif;
    background: #f5f5f7;
    color: #1d1d1f;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: env(safe-area-inset-top, 20px) 20px env(safe-area-inset-bottom, 20px);
  }
  h1 { font-size: 22px; margin-bottom: 8px; font-weight: 600; }
  .status {
    color: #86868b; margin-bottom: 12px; font-size: 14px;
    min-height: 20px; text-align: center; max-width: 320px;
  }
  .mic {
    color: #34c759; margin-bottom: 16px; font-size: 13px; min-height: 18px;
    font-family: ui-monospace, "SF Mono", monospace; letter-spacing: 1px;
  }
  .btn {
    width: 220px; height: 220px; border-radius: 50%;
    background: linear-gradient(135deg, #007aff, #5856d6);
    display: flex; align-items: center; justify-content: center;
    color: white; font-size: 18px; font-weight: 600;
    cursor: pointer; border: none; outline: none;
    box-shadow: 0 4px 24px rgba(0, 122, 255, 0.4);
    transition: transform 0.15s, box-shadow 0.15s;
    user-select: none; -webkit-user-select: none;
  }
  .btn:active { transform: scale(0.95); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn.live {
    background: linear-gradient(135deg, #ff3b30, #ff9500);
    animation: pulse 1.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { box-shadow: 0 4px 24px rgba(255, 59, 48, 0.5); }
    50%      { box-shadow: 0 4px 48px rgba(255, 59, 48, 0.9); }
  }
  .footer {
    margin-top: 28px; font-size: 13px; color: #86868b;
    text-align: center; max-width: 320px; line-height: 1.6;
  }
  .live-line {
    margin-top: 14px; width: 100%; max-width: 360px; min-height: 18px;
    font-size: 13px; color: #007aff; text-align: center;
  }
  .log {
    margin-top: 10px; width: 100%; max-width: 360px;
    max-height: 180px; overflow-y: auto;
    font-size: 11px; color: #999;
    font-family: ui-monospace, "SF Mono", monospace;
  }
  .log div { padding: 1px 0; }
  .log .u { color: #1d1d1f; font-weight: 600; }
</style>
</head>
<body>
  <h1>うみやま AI 音声</h1>
  <div class="status" id="status">タップして話す</div>
  <div class="mic" id="mic"></div>
  <button class="btn" id="callBtn">話す</button>
  <div class="footer">
    Web 直結 (電話番号なし・通話料無料)。<br>
    終了: 「またね」「じゃあまた」「ありがとう、また」<br>
    or 同じボタンを再タップ。<br>
    transcript は自動蒸留 → LINE で <code>/align-voice</code> 確認。
  </div>
  <div class="live-line" id="live"></div>
  <div class="log" id="log"></div>

<script type="module">
  // ★fix 2026-05-21: @vapi-ai/web は ESM-only パッケージで、jsdelivr 直 src= だと
  // グローバル Vapi が定義されず "Can't find variable: Vapi" になっていた。
  // esm.sh 経由で ESM import して module scope に取り込む方式に変更。
  import Vapi from "https://esm.sh/@vapi-ai/web@2";
  const PUBLIC_KEY = "__VAPI_PUBLIC_KEY__";
  const CONFIG_URL = "__CONFIG_URL__";
  const btn = document.getElementById("callBtn");
  const status = document.getElementById("status");
  const log = document.getElementById("log");
  const micEl = document.getElementById("mic");
  const liveEl = document.getElementById("live");
  let inCall = false;

  function setStatus(s) { status.textContent = s; }
  function addLog(s, cls) {
    const ts = new Date().toLocaleTimeString("ja-JP");
    const div = document.createElement("div");
    div.textContent = `[${ts}] ${s}`;
    if (cls) div.className = cls;
    log.insertBefore(div, log.firstChild);
    while (log.children.length > 40) log.removeChild(log.lastChild);
  }
  // ★2026-07-13: [object Object] 撲滅 — error payload を再帰的に文字列化
  function errText(e) {
    if (e == null) return "unknown";
    if (typeof e === "string") return e;
    if (e instanceof Error) return e.message;
    try { return JSON.stringify(e, null, 0).slice(0, 300); } catch (_) { return String(e); }
  }

  // ★2026-07-13 マイク入力レベルメーター (Vapi と独立の getUserMedia。
  // メーターが動く=ブラウザ/デバイスは正常 → 問題は Vapi uplink/STT 側と確定できる)
  let micCtx = null, micStream = null, micTimer = null;
  async function startMicMeter() {
    if (micCtx) return;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      micCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = micCtx.createMediaStreamSource(micStream);
      const an = micCtx.createAnalyser();
      an.fftSize = 512;
      src.connect(an);
      const buf = new Uint8Array(an.frequencyBinCount);
      const dev = (micStream.getAudioTracks()[0] || {}).label || "?";
      addLog("mic device: " + dev);
      micTimer = setInterval(() => {
        an.getByteFrequencyData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i];
        const lvl = Math.min(12, Math.round(sum / buf.length / 6));
        micEl.textContent = "マイク入力 " + "▮".repeat(lvl) + "▯".repeat(12 - lvl);
      }, 150);
    } catch (e) {
      addLog("mic meter failed: " + errText(e));
      micEl.textContent = "マイク取得失敗: " + errText(e);
    }
  }
  function stopMicMeter() {
    if (micTimer) clearInterval(micTimer);
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    if (micCtx) micCtx.close();
    micCtx = null; micStream = null; micTimer = null;
    micEl.textContent = "";
  }

  if (!PUBLIC_KEY || PUBLIC_KEY.startsWith("__")) {
    setStatus("VAPI_PUBLIC_KEY が server 未設定。.env に追加 + docker restart 必要。");
    btn.disabled = true;
  }

  let vapi = null;
  try {
    vapi = new Vapi(PUBLIC_KEY);
    vapi.on("call-start", () => {
      inCall = true;
      btn.classList.add("live");
      btn.textContent = "通話中";
      setStatus("通話中 — 自然に話して、終了は「またね」 or ボタン再タップ");
      addLog("call started");
      startMicMeter();
    });
    vapi.on("call-end", () => {
      inCall = false;
      btn.classList.remove("live");
      btn.textContent = "話す";
      setStatus("通話終了 — transcript 自動蒸留中、LINE の /align-voice で確認");
      addLog("call ended");
      stopMicMeter();
      liveEl.textContent = "";
    });
    vapi.on("error", (e) => {
      addLog("ERROR: " + errText(e));
      setStatus("エラー: " + errText(e).slice(0, 120));
      inCall = false;
      btn.classList.remove("live");
      btn.textContent = "話す";
      stopMicMeter();
    });
    vapi.on("speech-start", () => addLog("AI 発話開始"));
    vapi.on("speech-end", () => addLog("AI 発話終了"));
    // ★2026-07-13: リアルタイム文字起こし — 🎤 行が出ない = 発話が Vapi STT に届いていない
    vapi.on("message", (m) => {
      try {
        if (!m || !m.type) return;
        if (m.type === "transcript") {
          const who = m.role === "user" ? "🎤" : "AI";
          if (m.transcriptType === "final") {
            addLog(`${who}: ${m.transcript}`, m.role === "user" ? "u" : "");
            liveEl.textContent = "";
          } else {
            liveEl.textContent = `${who}… ${m.transcript}`;
          }
        } else if (m.type === "speech-update") {
          addLog(`speech ${m.status || ""} (${m.role || "?"})`);
        }
      } catch (_) {}
    });
  } catch (e) {
    setStatus("Vapi SDK init 失敗: " + errText(e));
    addLog("init failed: " + errText(e));
    btn.disabled = true;
  }

  btn.addEventListener("click", async () => {
    if (!vapi) return;
    if (inCall) { vapi.stop(); return; }
    setStatus("接続中… (マイク許可ダイアログが出たら許可)");
    try {
      const r = await fetch(CONFIG_URL);
      if (!r.ok) {
        const t = await r.text();
        throw new Error(`config ${r.status}: ${t.slice(0, 100)}`);
      }
      const cfg = await r.json();
      await vapi.start(cfg);
    } catch (e) {
      addLog("start failed: " + errText(e));
      setStatus("接続失敗: " + errText(e).slice(0, 120));
    }
  });
</script>
</body>
</html>
"""


def render_page(public_key: str, config_url: str) -> str:
    """テンプレート置換して最終 HTML を返す (呼び手 = main.voice_align_web_page)。"""
    return (
        _VOICE_ALIGN_HTML
        .replace("__VAPI_PUBLIC_KEY__", public_key)
        .replace("__CONFIG_URL__", config_url)
    )
