"""model_params.py — モデル別に「送ってはいけないパラメータ」を落とす (pure)。

★2026-08-03 コスト/品質事故 (docs/decisions/2026-08-03-silent-fallback-cost-leak.md):
Anthropic の新世代モデル (Opus 4.8 / Fable 5 等) は `temperature` を **HTTP 400 で拒否**する
(`"temperature" is deprecated for this model.`)。ところが呼び出し側は一律 temperature を
送っていたため、**model="smart" の呼び出しが全滅** → litellm が無言で smart-fallback (gpt-4o)
へ転送していた。25 日で 5,183 call / $182。しかも夜間 regression の judge も同経路で gpt-4o に
落ち、bot (GPT-5.4) と同一 provider = §1.15 の self-eval 防壁が無効化されていた。
どちらも HTTP 200 が返るため完全に無音だった。

方針: **alias 名で判定しない** (alias は .env で差し替わる)。litellm 側の drop_params と
二重防御にし、ここでは「temperature 非対応ファミリの alias 集合」を env で上書き可能にする。
"""
from __future__ import annotations

import os

# temperature/top_p を受け付けないモデルを指す alias (litellm_config.yaml と対応)。
# 新しい Anthropic 世代を足す時はここと litellm_config の drop_params 両方を更新する。
# ★cross-check S5: smart-sonnet (Sonnet 5) も非既定 sampling を 400 拒否するため追加。
#   smart-legacy (Opus 4 2025-05) は temperature 対応世代なので除外 (retire 済で 404 だが正確に)。
_DEFAULT_NO_TEMPERATURE = "smart,supervisor,smart-sonnet"


def no_temperature_aliases() -> set[str]:
    raw = os.getenv("NO_TEMPERATURE_MODEL_ALIASES", _DEFAULT_NO_TEMPERATURE)
    return {a.strip() for a in raw.split(",") if a.strip()}


def supports_temperature(model: str) -> bool:
    """その alias に temperature を送ってよいか。"""
    return (model or "").strip() not in no_temperature_aliases()


def apply_model_params(payload: dict, model: str, temperature=None) -> dict:
    """payload に temperature を条件付きで載せて返す (非対応モデルには載せない)。

    temperature=None なら常に載せない (呼び出し側が明示的に不要とした場合)。
    """
    out = dict(payload)
    out["model"] = model
    if temperature is not None and supports_temperature(model):
        out["temperature"] = temperature
    else:
        out.pop("temperature", None)
    return out
