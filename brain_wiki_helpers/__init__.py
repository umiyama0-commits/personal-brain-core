"""
brain_wiki_helpers — brain_wiki.py の純粋関数 helper 集 (★2026-05-22 Phase 1 切り出し)

設計:
- self に依存しない pure function を集約
- brain_wiki.py 本体から import して既存 method を薄い wrapper に
- 既存 API 互換維持 (= BrainWiki._parse_clone_visibility 等は今まで通り呼べる)
- 隔離 smoke test が組みやすい (= 重い chromadb / litellm 依存なし)

Phase 1 切り出し対象:
- visibility: clone_visibility / is_retired の frontmatter parse
- recency_bias: vector hits の last_updated 重み付け (次 commit)
- store_keyword: 店舗名検出 (次 commit)
"""
from __future__ import annotations

from .visibility import parse_clone_visibility, parse_is_retired
from .recency_bias import apply_recency_weight
from .store_keyword import detect_store_keyword
from .llm_retry import post_litellm_with_retry
from .frontmatter import merge_frontmatters, split_h2_with_intro, normalize_heading
from .yoy import format_yoy_ratio
from .domain import (
    PERSONAL_DOMAIN, INTERVIEW_DOMAIN, DEEP_PRIVATE_DIRS, CORE_DIRS, CORE_FILES,
    is_personal_rel, is_personal_path, is_core_rel, is_deep_private_rel, is_owndays_facing,
    domain_of, list_personal_projects, core_files,
    safe_project_slug, personal_project_dir,
)

__all__ = [
    "parse_clone_visibility",
    "parse_is_retired",
    "apply_recency_weight",
    "detect_store_keyword",
    "post_litellm_with_retry",
    "merge_frontmatters",
    "split_h2_with_intro",
    "normalize_heading",
    "format_yoy_ratio",
    "PERSONAL_DOMAIN",
    "INTERVIEW_DOMAIN",
    "DEEP_PRIVATE_DIRS",
    "CORE_DIRS",
    "CORE_FILES",
    "is_personal_rel",
    "is_personal_path",
    "is_core_rel",
    "is_deep_private_rel",
    "is_owndays_facing",
    "domain_of",
    "list_personal_projects",
    "core_files",
]
