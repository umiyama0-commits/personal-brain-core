"""build_analysis_wiki.py — サブPJ分析を private wiki に変換する共有 emitter (S5).

★2026-06-10 cross-check S5/S2/S6: PJ ごとに build_wiki をコピーすると
private/date/分類のバグが N 複製される。frontmatter / visibility / freshness /
PJ分類 を**この1箇所**で一元管理する。各 PJ 側コードは data 整形と本文 section 生成
だけを担い、この emitter を呼ぶ。

ADR: docs/decisions/2026-06-10-subpj-brain-integration.md

PJ 分類 (S6 — 最重要):
  - static-factual : 計測値 (店舗数 / 人口 / 競合店舗)。現パイプラインで連携可。
  - model-estimate : 推定値 (売上予測 / シェア試算)。uncertainty (assumptions) 必須 +
                     tight valid_until + 「推定値」明示。bot が数値を断定で答える事故を構造的に防ぐ。
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DIR = ROOT / "data" / "brain" / "wiki" / "analysis"

VALID_PJ_CLASSES = {"static-factual", "model-estimate"}
# static は競合店舗が四半期で動く想定で長め、model は予測の陳腐化が早いので短め (S3/S6)
DEFAULT_VALID_DAYS = {"static-factual": 180, "model-estimate": 45}


def build_analysis_wiki(
    pj_id: str,
    title: str,
    overview: str,
    sections: list,                  # [(heading, body_markdown), ...]
    *,
    pj_class: str,                   # S6: 必須。static-factual | model-estimate
    sources: list,
    tags: list | None = None,
    visibility: str = "private",     # S2: デフォルト private (fail-safe)
    confidence: str = "high",
    valid_days: int | None = None,
    assumptions: list | None = None,  # S6: model-estimate は必須
    allow_public: bool = False,       # S2: public 昇格は明示 co-sign を要求
    allow_public_reason: str = "",    # S2: marker に焼き込む理由 (★2026-07-11 builder emit 化)
) -> Path:
    """analysis/<pj_id>.md を生成し、生成パスを返す。

    frontmatter (updated 計算 / valid_until / visibility / pj_class) を emitter が所有する。
    """
    # --- S6: PJ 分類の検証 ---
    if pj_class not in VALID_PJ_CLASSES:
        raise ValueError(
            f"pj_class は {sorted(VALID_PJ_CLASSES)} のいずれか必須: {pj_class!r} "
            "(cross-check S6: 静的事実か推定値かで連携の安全策が変わる)"
        )
    # --- S6: model-estimate は不確実性 (assumptions) を必ず明示 ---
    if pj_class == "model-estimate" and not assumptions:
        raise ValueError(
            "model-estimate PJ は assumptions (前提・不確実性) が必須 "
            "(cross-check S6: bot が推定値を断定で答える事故を防ぐ)"
        )
    # --- S2: visibility 検証 + public 昇格の friction ---
    if visibility not in ("private", "public"):
        raise ValueError(f"visibility は private | public: {visibility!r}")
    if visibility == "public":
        if pj_class == "model-estimate":
            raise ValueError(
                "model-estimate を public にはできない (不確実な数値の社員公開は危険、S6)"
            )
        if not allow_public:
            raise ValueError(
                "analysis/ を public にするには allow_public=True の明示 co-sign が必要 "
                "(cross-check S2: 意図的 public の copy-paste 事故を防ぐ)"
            )

    # --- S3: updated は再実行日で計算 (ハードコード禁止)、valid_until で自動退場 ---
    updated = date.today().isoformat()
    days = valid_days if valid_days is not None else DEFAULT_VALID_DAYS[pj_class]
    valid_until = (date.today() + timedelta(days=days)).isoformat()
    tags = tags or []
    exit_vis = "private" if visibility == "private" else "internal"

    fm = [
        "---",
        f"updated: {updated}",
        f"valid_until: {valid_until}",
        f"confidence: {confidence}",
        f"pj_class: {pj_class}",
        f"tags: [{', '.join(tags)}]",
        f"sources: [{', '.join(sources)}]",
        f"clone_visibility: {visibility}",
        f"exit_visibility: {exit_vis}",
        "---",
    ]
    body = [f"# {title}", "", "## 概要", overview, ""]
    # --- S2: public は ALLOW_PUBLIC marker を builder が emit (★2026-07-11 tenpo cross-check) ---
    #   手貼りだと再生成で消えて lint (lint_analysis_visibility) が commit を block する再発型。
    #   allow_public=True の時点で co-sign 済みなので、その理由を機械可読に焼き込む。
    if visibility == "public" and allow_public:
        body.insert(0, f"<!-- ALLOW_PUBLIC: {allow_public_reason or 'builder co-sign (allow_public=True)'} -->")
    # --- S6: model-estimate は冒頭に不確実性を inline (bot が断定を避ける根拠) ---
    if pj_class == "model-estimate":
        body += [
            "## ⚠️ 推定値につき注意",
            "本分析は **model-estimate (推定値)**。断定でなく「予測」として扱い、"
            "幅・前提込みで答えること。",
            "",
            "### 前提・不確実性",
        ]
        body += [f"- {a}" for a in assumptions]
        body += [""]
    for heading, sec_body in sections:
        body += [f"## {heading}", sec_body, ""]

    md = "\n".join(fm) + "\n" + "\n".join(body)
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out = ANALYSIS_DIR / f"{pj_id}.md"
    out.write_text(md, encoding="utf-8")
    return out
