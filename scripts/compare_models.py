#!/usr/bin/env python3
"""
Model A/B comparison harness
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

同じ prompt を複数モデル(役割名)に投げて side-by-side 表示 + Markdown で保存。

使い方:
  # デフォルト (smart vs smart-gpt の対決)
  python3 scripts/compare_models.py "海山丈司はどんな人？ 100字で"

  # 3モデル比較 (モデルは comma 区切り、空白禁止)
  python3 scripts/compare_models.py -m smart,smart-gpt,smart-gpt-pro "..."

  # コード比較 (code vs code-max)
  python3 scripts/compare_models.py -m code,code-max "Python で FastAPI の middleware を書いて"

  # プリセット task
  python3 scripts/compare_models.py --preset clone "今期の業績どう？"
  python3 scripts/compare_models.py --preset code-refactor

出力:
  data/brain/model_eval/YYYY-MM-DD_HHMM_<preset>.md
  + stdout で side-by-side diff 表示
"""
from __future__ import annotations

import os
import sys
import time
import json
import argparse
from pathlib import Path
from datetime import datetime

import httpx

# .env を読む
try:
    from dotenv import load_dotenv
    BASE = Path(__file__).resolve().parent.parent
    load_dotenv(BASE / ".env")
except Exception:
    BASE = Path(__file__).resolve().parent.parent

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
# スクリプトはホストから叩くので litellm:4000 を localhost に
if "litellm:4000" in LITELLM_URL:
    LITELLM_URL = "http://localhost:4000"
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")  # ★平文 default 禁止 (LEE §3.1)

OUT_DIR = BASE / "data" / "brain" / "model_eval"


# ─── プリセット ────────────────────────────────────
PRESETS = {
    "clone": {
        "description": "うみやまAI 応答品質 (海山ボイス再現)",
        "system": (
            "あなたはOWNDAYS CEO 海山丈司本人です。短く、事実ベースで、"
            "威張らず本音で答えてください。社員からの1:1 DMとして応答。"
        ),
        "default_prompt": "今期の業績どう思いますか？本音で",
        "default_models": ["smart", "smart-gpt", "smart-gpt-pro"],
    },
    "wiki-compile": {
        "description": "Wiki コンパイル品質 (生データ → Karpathy式整形)",
        "system": "以下の生ログを、Karpathy式ナレッジベースの形に整形してください。断定/事実/決定/TODOを分離し、簡潔に。",
        "default_prompt": "田中さんとの打ち合わせで来月のキャンペーン案が決まった。予算は500万、期間は6月。CVR目標2.5倍。",
        "default_models": ["smart", "smart-gpt"],
    },
    "code-refactor": {
        "description": "コード refactor 品質",
        "system": "あなたは経験豊富な Python エンジニアです。動作を保ったまま、読みやすく短く refactor してください。",
        "default_prompt": (
            "次の関数を refactor:\n"
            "def x(a,b,c):\n"
            "    r=[]\n"
            "    for i in range(len(a)):\n"
            "        if a[i]>b and a[i]<c:\n"
            "            r.append(a[i]*2)\n"
            "    return r"
        ),
        "default_models": ["code", "code-max", "smart"],
    },
    "privacy": {
        "description": "Privacy 判定品質 (分類タスク)",
        "system": "以下のメッセージが [家族会話/性的内容/悪口/医療詳細/パートナー会話/その他] のどれに該当するか、1行で分類理由とともに返せ。",
        "default_prompt": "昨日お母さんに電話したら体調悪いって言ってた",
        "default_models": ["fast", "fast-gpt", "smart"],
    },
    "free": {
        "description": "自由入力",
        "system": None,
        "default_prompt": "こんにちは",
        "default_models": ["smart", "smart-gpt"],
    },
}


def call_model(model: str, prompt: str, system: str | None, max_tokens: int = 800) -> dict:
    """litellm 経由で1モデルに問い合わせ、応答 + レイテンシを返す"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    headers = {
        "Authorization": f"Bearer {LITELLM_KEY}",
        "Content-Type": "application/json",
    }
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{LITELLM_URL}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=120.0,
        )
        elapsed = time.time() - t0
        if resp.status_code != 200:
            return {"model": model, "error": f"{resp.status_code}: {resp.text[:200]}", "elapsed": elapsed}
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        return {
            "model": model,
            "text": text,
            "elapsed": elapsed,
            "tokens_in": usage.get("prompt_tokens"),
            "tokens_out": usage.get("completion_tokens"),
            "actual_model": data.get("model", model),
        }
    except Exception as e:
        return {"model": model, "error": str(e), "elapsed": time.time() - t0}


def format_md(preset: str, prompt: str, system: str | None, results: list[dict]) -> str:
    lines = [
        f"# Model comparison — {preset}",
        f"Date: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Prompt",
        f"**System:** `{system}`" if system else "**System:** (none)",
        "",
        "**User:**",
        "```",
        prompt,
        "```",
        "",
        "## Results",
        "",
    ]
    for r in results:
        lines.append(f"### `{r['model']}` ({r.get('actual_model', '?')})")
        if "error" in r:
            lines.append(f"**ERROR:** {r['error']}")
        else:
            lines.append(
                f"⏱ {r['elapsed']:.1f}s  "
                f"📥 {r.get('tokens_in', '?')} in / "
                f"📤 {r.get('tokens_out', '?')} out"
            )
            lines.append("")
            lines.append(r["text"])
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def print_side_by_side(results: list[dict]) -> None:
    """stdout に短く表示"""
    for r in results:
        bar = "━" * 70
        print(f"\n{bar}")
        print(f"🤖 {r['model']}  ({r.get('actual_model','?')})")
        if "error" in r:
            print(f"   ❌ {r['error']}")
            continue
        print(
            f"   ⏱ {r['elapsed']:.1f}s  "
            f"📥 {r.get('tokens_in','?')} / 📤 {r.get('tokens_out','?')}"
        )
        print(bar)
        print(r["text"][:2000])
        if len(r["text"]) > 2000:
            print(f"\n... (truncated, full in markdown)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="?", default=None)
    ap.add_argument("-m", "--models", help="役割名を comma 区切り (例: smart,smart-gpt)")
    ap.add_argument("--preset", choices=list(PRESETS.keys()), default="free")
    ap.add_argument("--system", help="system prompt override")
    ap.add_argument("--max-tokens", type=int, default=800)
    ap.add_argument("--no-save", action="store_true", help="markdown に保存しない")
    args = ap.parse_args()

    preset = PRESETS[args.preset]
    prompt = args.prompt or preset["default_prompt"]
    system = args.system if args.system is not None else preset.get("system")
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    else:
        models = preset["default_models"]

    print(f"📋 preset: {args.preset}  ({preset['description']})")
    print(f"🎯 models: {', '.join(models)}")
    print(f"📝 prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
    print()

    results = []
    for m in models:
        print(f"→ calling {m} ...", end=" ", flush=True)
        r = call_model(m, prompt, system, max_tokens=args.max_tokens)
        if "error" in r:
            print(f"❌ {r['error'][:80]}")
        else:
            print(f"✅ {r['elapsed']:.1f}s  ({r.get('tokens_out','?')} tok)")
        results.append(r)

    print_side_by_side(results)

    if not args.no_save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M")
        path = OUT_DIR / f"{ts}_{args.preset}.md"
        md = format_md(args.preset, prompt, system, results)
        path.write_text(md, encoding="utf-8")
        print(f"\n💾 saved: {path.relative_to(BASE)}")


if __name__ == "__main__":
    main()
