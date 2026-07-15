"""smoke test: alignment_interview の pending 読み書き・status 遷移。"""
from __future__ import annotations

import importlib
import json
import pytest


@pytest.mark.smoke
def test_list_pending_extractions(brain_root, sample_alignment_extracted):
    """投入された extraction が pending として list される。"""
    import alignment_interview
    importlib.reload(alignment_interview)

    pending = alignment_interview.list_pending_extractions()
    assert len(pending) == 1
    p = pending[0]
    assert p["file"] == f"{sample_alignment_extracted}.json"
    assert p["item_count"] == 2
    assert "孤独感" in p["summary"]


@pytest.mark.smoke
def test_get_extraction(brain_root, sample_alignment_extracted):
    """get_extraction で中身が取れる。"""
    import alignment_interview
    importlib.reload(alignment_interview)

    d = alignment_interview.get_extraction(f"{sample_alignment_extracted}.json")
    assert d is not None
    assert d["status"] == "pending_review"
    assert len(d["items"]) == 2
    assert d["items"][0]["category"] == "philosophy"


@pytest.mark.smoke
def test_get_extraction_not_found(brain_root):
    """存在しない fid は None を返す。"""
    import alignment_interview
    importlib.reload(alignment_interview)

    assert alignment_interview.get_extraction("nonexistent.json") is None


@pytest.mark.smoke
def test_list_pending_empty(brain_root):
    """pending 0 件で空 list を返す (例外起こさない)。"""
    import alignment_interview
    importlib.reload(alignment_interview)

    pending = alignment_interview.list_pending_extractions()
    assert pending == []


@pytest.mark.smoke
def test_extraction_status_transition(brain_root, sample_alignment_extracted):
    """status を applied に変えると list_pending から消える。"""
    import alignment_interview
    importlib.reload(alignment_interview)

    edir = brain_root / "alignment" / "interview_extracted"
    fpath = edir / f"{sample_alignment_extracted}.json"
    data = json.loads(fpath.read_text(encoding="utf-8"))
    data["status"] = "applied"
    fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    pending = alignment_interview.list_pending_extractions()
    assert len(pending) == 0


@pytest.mark.smoke
def test_extract_prompt_json_examples_are_valid_json(brain_root):
    """EXTRACT_PROMPT / MAGAZINE_EXTRACT_PROMPT の JSON 出力例が有効な JSON である。

    ★2026-07-05 監査 fix の regression 固定: 旧 EXTRACT_PROMPT は JSON 例の途中
    (items と dims_with_substance の間) に confidence 較正の散文が挟まり、few-shot と
    して**不正な JSON** を LLM に見せていた (= dims_with_substance / session_summary の
    出力脱落や JSON 崩れの温床)。プロンプト中の最初の `{...}` (brace-match) が
    json.loads 可能で、期待キーを含むことを pin する。
    """
    import alignment_interview
    importlib.reload(alignment_interview)

    for name in ("EXTRACT_PROMPT", "MAGAZINE_EXTRACT_PROMPT"):
        prompt = getattr(alignment_interview, name)
        start = prompt.index("{")
        depth, end = 0, None
        for i, ch in enumerate(prompt[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        assert end, f"{name}: brace 不整合"
        data = json.loads(prompt[start:end])  # 例の途中に散文が挟まると必ず fail
        assert "items" in data, name
        assert "dims_with_substance" in data, name
        assert "session_summary" in data, name
