#!/usr/bin/env python3
"""litellm_route_probe.py — 「要求した model が本当にその model で処理されたか」を毎日検証する。

★2026-08-03 コスト実測で判明した事故 (docs/decisions/2026-08-03-silent-fallback-cost-leak.md):
  Anthropic は claude-opus-4-8 に対し `temperature` を **HTTP 400 で拒否**する。
  brain_wiki._call_llm が常に temperature を送っていたため、**model="smart" の呼び出しが全滅**し、
  litellm が無言で smart-fallback (gpt-4o) へ転送していた。25 日間で 5,183 call / $182 (支出の 42%)。
  さらに夜間 regression の judge も同じ経路で gpt-4o に落ち、bot (GPT-5.4) と同一 provider =
  **self-eval 防壁 (§1.15) が無効化**されていた。どちらも 200 OK が返るため完全に無音だった。

本 probe は「本番と同じ payload 形状」で各 alias に極小 call を投げ、litellm の応答ヘッダ
  x-litellm-model-group / x-litellm-attempted-fallbacks
を見て **要求 alias ≠ 実処理 model group** を検知したら loud_fail (§1.18) する。

コスト: 1 回 ~10 token × alias 数 = 実質ゼロ。
実行: python3 scripts/litellm_route_probe.py [--json]
cron: health_cron.sh から日次 (05:30)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

LITELLM_URL = os.getenv("LITELLM_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# (alias, 本番と同じ payload 形状か) — temperature を送る経路が事故の起点なので必ず含める。
# expect_group=None は「要求 alias と同じ group で処理されること」を期待。
PROBES = [
    # ★修正後の本番形状 (temperature を送らない) — client 側ガードが生きているかの本命
    {"alias": "smart", "temperature": None},
    # 旧形状 = litellm 側 drop_params の canary (client ガードが外れても救えるか)
    {"alias": "smart", "temperature": 0.1},
    # 夜間 regression / hallucination judge (clone_improve_lib、temperature 0.0)
    {"alias": "smart", "temperature": 0.0},
    # analyst / consultant (0.2) と reflex/alignment extractor (0.3) — cross-check DA R4
    {"alias": "smart", "temperature": 0.2},
    {"alias": "smart", "temperature": 0.3},
    # 社員クローン本番応答
    {"alias": "smart-gpt", "temperature": 0.3},
    # wiki compile 本番
    {"alias": "fast-gpt", "temperature": 0.1},
    # 監督者層 (Fable 5 は temperature 非対応 = 送らないのが正)
    {"alias": "supervisor", "temperature": None},
]


def probe_one(client: httpx.Client, alias: str, temperature) -> dict:
    payload = {
        "model": alias,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    try:
        r = client.post(f"{LITELLM_URL}/v1/chat/completions",
                        headers={"Authorization": f"Bearer {LITELLM_KEY}"},
                        json=payload, timeout=120)
    except Exception as e:
        return {"alias": alias, "temperature": temperature, "ok": False,
                "error": f"{type(e).__name__}: {e}"}
    served = r.headers.get("x-litellm-model-group", "")
    attempted = r.headers.get("x-litellm-attempted-fallbacks", "0")
    try:
        n_fb = int(attempted)
    except ValueError:
        n_fb = 0
    # ★cross-check S4: ヘッダ欠落を OK にしない (「無音故障の検知役が無音で死ぬ」を防ぐ)。
    # litellm 仕様変更でヘッダが消えたら NG 扱いにして気づけるようにする。
    routed_ok = (r.status_code == 200 and n_fb == 0 and served == alias)
    return {
        "alias": alias, "temperature": temperature, "status": r.status_code,
        "served_group": served or "(header 無し)", "attempted_fallbacks": n_fb,
        "ok": routed_ok,
        "error": "" if r.status_code == 200 else r.text[:180],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not LITELLM_KEY:
        # ★§1.18: silent skip にしない (probe が動いていないこと自体が検知漏れ)
        print("LITELLM_MASTER_KEY 未設定 → probe 実行不能", file=sys.stderr)
        try:
            from clone_improve_lib import loud_fail
            loud_fail("litellm_model_route", False,
                      "route probe が LITELLM_MASTER_KEY 未設定で実行できない "
                      "(= モデル経路の検証が止まっている)", threshold=2, cooldown_h=24)
        except Exception:
            pass
        return 1

    results = []
    with httpx.Client() as client:
        for p in PROBES:
            results.append(probe_one(client, p["alias"], p["temperature"]))

    bad = [r for r in results if not r["ok"]]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "OK " if r["ok"] else "NG "
            print(f"{mark} {r['alias']:<12} temp={str(r['temperature']):<5} "
                  f"→ {r.get('served_group','?'):<16} fallbacks={r.get('attempted_fallbacks','?')} "
                  f"{r.get('error','')[:80]}")

    # §1.18: 要求 alias と実処理がズレたら loud (無言 fallback = 今回の $182 事故の再発)
    try:
        from clone_improve_lib import loud_fail
        detail = "; ".join(
            f"{r['alias']}(temp={r['temperature']})→{r.get('served_group','?')}" for r in bad)
        loud_fail(
            "litellm_model_route", not bad,
            f"要求した model と実処理 model がズレている: {detail}。"
            "無言 fallback = コスト/品質の両方が設計と違う状態 (2026-08-03 事故の再発)",
            threshold=2, cooldown_h=24,
        )
    except Exception as e:
        print(f"loud_fail 失敗 (非致命): {e}", file=sys.stderr)

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
