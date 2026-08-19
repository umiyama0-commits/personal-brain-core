"""services/gemini_query.py — Gemini API で query 拡張 + 検索結果 re-rank

★2026-05-26 海山指示「Gemini Workspace 連携で Drive 内検索を bot 経由で」:
Drive API は fullText 検索 (= name + 中身) を index 経由で高速提供するが、
- 自然言語 query 「武蔵小山店の今月予算は?」 → 「武蔵小山」 / 「予算」 / 「FY26」 等への分解
- 結果 N 件から「最も関連する top 3」 を意味的に判定
は LLM 力が必要。

Gemini API (= aistudio.google.com で無料 key 取得) で 2 step:
  1. expand_query(query) → list[str] (= 検索 keyword 候補 3-5 個)
  2. rerank_results(query, files) → list[dict] (= top 3、各々 reason 付き)

無料枠: 15 RPM / 1M token/day (= bot 利用ペースなら無料で完結)。
有料化目安: 月 10K query 超 で paid tier 検討。

env:
  GEMINI_API_KEY=...  (= https://aistudio.google.com で 5 分取得)
  GEMINI_MODEL=gemini-2.5-flash-lite  (= default、thinking 無・高速・billing 必須)

usage:
  from services.gemini_query import expand_query, rerank_results
  keywords = await expand_query("武蔵小山店の今月予算は?")
  top = await rerank_results("武蔵小山店の今月予算は?", files=[...])
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ★2026-06-08 海山指示「各種 API 残高枯渇の自動連絡」: Gemini の枯渇 (quota exceeded 等) を検知。
try:
    import sys as _sys
    from pathlib import Path as _Path
    _scripts = str(_Path(__file__).resolve().parent.parent / "scripts")
    if _scripts not in _sys.path:
        _sys.path.insert(0, _scripts)
    from quota_alert import maybe_alert_quota as _maybe_alert_quota  # type: ignore
except Exception:  # pragma: no cover
    def _maybe_alert_quota(*_a, **_k):
        return False

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_TIMEOUT_SEC = 15


class GeminiUnavailableError(Exception):
    """GEMINI_API_KEY 未設定 or API 失敗時に raise."""


def _ensure_api_key() -> None:
    if not GEMINI_API_KEY:
        raise GeminiUnavailableError(
            "GEMINI_API_KEY が .env に未設定。https://aistudio.google.com で取得して設定"
        )


async def _generate(prompt: str, response_json: bool = False, max_tokens: int = 512) -> str:
    """Gemini API の generateContent を 1 回呼ぶ. raw text 返す."""
    _ensure_api_key()
    url = f"{GEMINI_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
        },
    }
    if response_json:
        body["generationConfig"]["responseMimeType"] = "application/json"
    async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT_SEC) as client:
        try:
            r = await client.post(url, json=body)
        except Exception as e:
            raise GeminiUnavailableError(f"network error: {type(e).__name__}: {e}")
    if r.status_code != 200:
        # ★枯渇 (quota exceeded 等) なら LINE 通知 (transient rate-limit は classify が無視)
        try:
            _maybe_alert_quota("gemini", status_code=r.status_code,
                               body_text=(r.text or "")[:1000])
        except Exception:
            pass
        raise GeminiUnavailableError(
            f"API status {r.status_code}: {r.text[:200]}"
        )
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise GeminiUnavailableError(f"unexpected response shape: {e} / {data}")


async def expand_query(query: str, max_keywords: int = 5) -> list[str]:
    """自然言語 query → 検索 keyword に分解 (後方互換 wrapper、実体は structured 版)。
    must 語 (制約語) も keyword 先頭に merge して返す。"""
    st = await expand_query_structured(query, max_keywords=max_keywords)
    merged = list(dict.fromkeys((st.get("must") or []) + (st.get("keywords") or [])))
    return merged[:max(max_keywords, 5)]


async def expand_query_structured(query: str, max_keywords: int = 6) -> dict:
    """自然言語 query → {"must": [...], "keywords": [...]} に構造化分解.

    ★2026-07-13 海山指示「Drive検索の精度が悪い。GRPと単価を分けてワード検索するとか。
    よりLLM的な観点で精度の高いDrive検索を」(実例: 「石川県のTVCMのGRP単価」で
    水戸/東海の PDCA シートが top 3 = 地域制約の無視 + 複合語のまま検索 + 当て推量 rerank):
    - must: 答えの file が**必ず関係すべき制約語** (地域/店舗/固有名詞 = query の判別軸)。
      例「石川県のTVCMのGRP単価」→ ["石川"]。must 非ヒット file は下流で減点/除外。
    - keywords: 検索 fan-out 語。複合語は複合のまま + **判別力のある原子語**にも分解
      (「GRP単価」→「GRP単価」+「GRP」。「単価」単独は generic すぎ noise → 分解で出さない)。

    返り値が空 dict 相当なら caller 側 fallback (元 query のみで検索)。
    """
    query = (query or "").strip()
    if not query:
        return {"must": [], "keywords": []}
    # ★2026-05-26 海山指示「意味を読み取り検索クエリを提案」: 表面 keyword 分解では
    # 「副業規定」 と 「副業募集」 が同じ keyword「副業」で hit するため意味的に合わない
    # 結果が出る。意図リフレーズ + 同義語 + 関連語 で検索精度向上.
    prompt = (
        "あなたは Google Drive 内検索の前処理 assistant。\n"
        "ユーザの自然言語 query から、**質問の本質的な意図** を読み取り、\n"
        "Drive fullText 検索で hit しやすい検索 keyword を抽出する。\n\n"
        "★重要: 表面的な単語分解ではなく、質問の **意味** を理解して、\n"
        "それに合致する 関連語 / 同義語 / 別の言い方 / 公式用語 を含める。\n"
        "意図と離れた別文脈の単語 (= 同字異義) は除外する。\n\n"
        "例 1: 「副業規定について教えて」\n"
        "  意図: 社員向けの副業に関する就業規則 / 申請ルール\n"
        "  → [\"副業規程\", \"副業 就業規則\", \"副業 申請\", \"副業 兼業\", \"副業 ガイドライン\"]\n"
        "  注意: 「副業募集」「副業バイト」 は意味が違うので **除外**\n\n"
        "例 2: 「武蔵小山店の今月予算は?」\n"
        "  意図: 特定店舗の月次予算 / 計画\n"
        "  → [\"武蔵小山\", \"予算\", \"計画\", \"月次\", \"5月\"]\n\n"
        "例 3: 「OWNDAYS の理念は?」\n"
        "  意図: 企業理念 / mission / vision\n"
        "  → [\"OWNDAYS\", \"理念\", \"mission\", \"vision\", \"philosophy\"]\n\n"
        "例 4: 「店長給与のレンジ知りたい」\n"
        "  意図: 店長の給与水準 / 体系 / レンジ (= 個人別給与ではない、集計値)\n"
        "  → [\"店長給与\", \"給与レンジ\", \"給与体系\", \"職位別\", \"店長 報酬\"]\n\n"
        "例 5: 「来週月曜日の新店会議の議題のクリエイトリンクって何?」\n"
        "  意図: 「クリエイトリンク」 という固有名詞 (会社名/案件名/PJ名) が何かを知りたい。\n"
        "        「新店会議」「議題」「来週月曜」 は その固有名詞に出会った **状況説明** であって\n"
        "        検索対象そのものではない。\n"
        "  → [\"クリエイトリンク\", \"クリエイトリンク 出店\", \"クリエイトリンク PJ\", \"包括出店\", \"新店 出店\"]\n"
        "  注意: カタカナ社名/案件名/PJ名 は **最優先** keyword。\n"
        "        「会議」「議題」「アジェンダ」「打合せ」「来週」「月曜」 等の generic な場面・日程語は\n"
        "        質問の主題でない限り **検索 keyword から外す** (= file 名に出にくく noise になる)\n\n"
        "例 6 (★must 語 + 複合語分解): 「石川県のTVCMのGRP単価は?」\n"
        "  意図: 石川県 (地域制約!) のテレビ CM の GRP 単価を知りたい。\n"
        "  must: [\"石川\"] (= 県抜き表記。答えの file は必ず石川に関係する。他地域の GRP 資料は不正解)\n"
        "  keywords: [\"石川県\", \"金沢\", \"TVCM\", \"GRP単価\", \"GRP\", \"テレビCM\"]\n"
        "  注意: 「GRP単価」は複合のまま + 判別力のある「GRP」にも分解。「単価」単独は\n"
        "        あらゆる資料に出る generic 語なので **入れない**。地域は関連都市 (金沢) も。\n"
        "  ★must に「TVCM」「GRP」を入れない (= 全 CM 資料が該当して絞り込みが崩壊する)。\n"
        "    must は「どのデータか」を絞る scope 語 (ここでは地域 = 石川) だけ。\n\n"
        "ルール:\n"
        "- ★ **must = 「どのデータか」を絞る scope 語だけ** (地域/店舗/PJ名/取引先名 等) を 0-2 個。\n"
        "     質問が**測りたい対象** (TVCM/GRP/単価/売上/予算 等の指標・話題語) は must に**入れない**\n"
        "     (→ keywords へ)。must は OR 判定なので、広い語を 1 つでも混ぜると絞り込みが無効化する。\n"
        "     部分一致で使うので短い核形 (「石川県」→「石川」)。無ければ [] \n"
        "- ★ 固有名詞 (カタカナ社名/案件名/商品名/PJ名/人名/店舗名) は質問の **焦点** → keyword 先頭・最優先\n"
        "- ★ 「Xって何?」「Xとは」「Xについて」 の X は焦点 entity → 必ず keyword 先頭に置く\n"
        "- ★ 複合語 (GRP単価/出店基準/検眼機器 等) は複合のまま + **判別力のある原子語** にも分解。\n"
        "     ただし「単価/基準/資料/データ/一覧」のような単独では generic な原子語は出さない\n"
        "- ★ 「会議/議題/アジェンダ/打合せ/ミーティング」 等の generic な場面語、および「来週/今週/明日/月曜」\n"
        "     等の会話的な日程語は、それ自体が主題でない限り **keyword から除外** (= 状況説明、検索対象でない)\n"
        "  注: ただし会計期間 (=「今月」「5月」「FY26」「年度」「上期」 等、データ範囲を絞る語) は keyword に残す\n"
        "- 各 keyword は 1-10 字程度 (= 単語 or 短いフレーズ可)\n"
        "- 助詞・助動詞 (の/は/を/に等) は除外、固有名詞は残す\n"
        "- 数字や年度は keyword に含める (= 「FY26」「2026」「5月」 等)\n"
        "- 同義語 / 類義語 / 公式用語 / 関連分野語を 3-7 個\n"
        "- ★ 意図と違う 別文脈の同字異義語 は **絶対に含めない**\n"
        '- 出力 format: JSON object {"must": [...], "keywords": [...]}、説明文不要\n\n'
        f"query: {query}\n\n"
        f'JSON output ({{"must": [0-2 個], "keywords": [3-{max_keywords} 個]}}):'
    )
    try:
        text = await _generate(prompt, response_json=True, max_tokens=250)
    except GeminiUnavailableError as e:
        logger.warning(f"expand_query Gemini unavailable: {e}")
        return {"must": [], "keywords": []}
    try:
        data = json.loads(text)
        # 旧形式 (list) を返してきた場合も受ける (モデル揺れ防御)
        if isinstance(data, list):
            return {"must": [],
                    "keywords": [str(k).strip() for k in data if k][:max_keywords]}
        if isinstance(data, dict):
            # bare string (非 list) が来たら 1 要素 list 扱い (文字単位 iterate 事故防止)
            raw_must = data.get("must") or []
            raw_kws = data.get("keywords") or []
            if isinstance(raw_must, str):
                raw_must = [raw_must]
            if isinstance(raw_kws, str):
                raw_kws = [raw_kws]
            must = [str(m).strip() for m in raw_must if str(m).strip()][:2]
            kws = [str(k).strip() for k in raw_kws if str(k).strip()]
            return {"must": must, "keywords": kws[:max_keywords]}
    except json.JSONDecodeError:
        logger.warning(f"expand_query: JSON parse failed: {text[:200]}")
    return {"must": [], "keywords": []}



def _rerank_fallback(files: list, top_n: int, score_ordered: bool) -> list:
    """Gemini 失敗時の fallback。★2026-07-13: 新経路 (evidence/must_terms あり = caller が
    決定論スコア順で渡している) は入力順を維持 (旧 recency 順は「今日更新の無関係 file が
    並ぶ」精度事故の元凶)。旧来の直接呼び出し (順序保証無し) は従来どおり recency。
    ★rerank_confidence="degraded" を付ける = 意味判定を経ていない機械選別であることを
    呼び手に伝え、表示側が「参考」扱いに落とす (海山「無理に提示する必要がない」)。"""
    picked = files[:top_n] if score_ordered else sorted(
        files, key=lambda f: f.get("modifiedTime", ""), reverse=True)[:top_n]
    out = []
    for f in picked:
        f = dict(f)
        f["rerank_confidence"] = "degraded"
        out.append(f)
    return out


async def rerank_results(query: str, files: list[dict], top_n: int = 3,
                         evidence: dict | None = None,
                         must_terms: list | None = None) -> list[dict]:
    """検索結果 files から query に最関連の top_n を返す (= reason 付き).

    Args:
        query: 元 query
        files: Drive API discover 結果 (= 各 dict は id / name / mimeType / modifiedTime / webViewLink 含む)
        top_n: 返す件数 (= default 3)
        evidence: fid → ヒットした検索語 list (★2026-07-13。「名前からの当て推量」を
            「どの語に fullText ヒットしたか」の根拠付き判定に格上げする)
        must_terms: 制約語 (地域等)。非ヒット file は原則選ばない。

    Returns:
        files の subset、最関連順 sort、各々に "rerank_reason" 追加。
        Gemini 失敗時は入力順 (caller の決定論スコア順) で先頭 N 件 fallback。
    """
    if not files:
        return []
    if len(files) <= top_n:
        # ★Phase 1.2 (海山指示): 短絡 path でも modifiedTime DESC で sort (= 最新優先 inherit)
        return sorted(
            files,
            key=lambda f: -_iso_to_unix(f.get("modifiedTime", "")),
        )[:top_n]

    # files の要約を作る (= Gemini context 短縮、上位 30 件まで。★2026-07-13: 入力順 =
    # caller の決定論スコア順 (must→照合数→新しさ) なので「先頭 30 件」が最有力 30 件になる)
    short_files = files[:30]
    listing = []
    for i, f in enumerate(short_files, start=1):
        name = (f.get("name") or "")[:80]
        mime = (f.get("mimeType") or "").split("/")[-1][:20]
        mod = (f.get("modifiedTime") or "")[:10]
        # ★2026-06-21 世界基準評価 B-4 (security RISK2): 社員 (owner) 表示名を外部 Gemini に送らない。
        #   relevance ランキングには file 名/mime/mod で十分、owner 名は PII につき外部 egress から除外。
        line = f"{i}. [{mime}] {name} | mod={mod}"
        hqs = (evidence or {}).get(f.get("id", ""))
        if hqs:
            line += f" | 中身ヒット語: {', '.join(str(q)[:20] for q in hqs)}"
        listing.append(line)

    # ★2026-05-26 海山指示 (= 推奨 A+B): prompt instruction で 「最新優先」 hint +
    # rerank 後 tie-breaker sort で 同関連度 ties の場合 modifiedTime DESC を確定。
    # ★2026-05-26 海山指示 (= 意味的関連): file 名表面 keyword 一致ではなく、query の
    # 本質的な意味と file 名から推測される内容の **意味的関連** で判定。
    # 意味的に合うものが無ければ空 array 返却可 (= 「該当無し」 を caller で扱える).
    prompt = (
        "あなたは Google Drive 検索結果の re-rank assistant。\n"
        f"ユーザ query: {query}\n\n"
        "以下は検索 hit ファイル一覧。**その file を開けば query に答えられる** file だけを "
        "最大 " + str(top_n) + " 個選ぶ。\n\n"
        + "\n".join(listing) + "\n\n"
        "★判定基準 (2026-07-13 海山指示「関連したものが見つからない場合は、無理に提示する"
        "必要がない」): 選ぶ基準は『開けば答えが載っているか』であって『語が中身のどこかに"
        "出てくるか』ではない。**迷ったら選ばない**。0 件 = 空 array [] が正解のケースは"
        "普通にある。\n\n"
        "ルール:\n"
        "- 番号で最大 " + str(top_n) + " 個 select (確信のあるものだけ。無理に埋めない)\n"
        "- 各々 1 行 reason (= その file に答えが載っていると考える根拠、25 字以内)\n"
        "- 各々 confidence: \"high\" (= 主題が query と一致、開けば答えがある) or "
        "\"low\" (= 関連しそうだが答えが載っている確信は無い)\n"
        "- ★ **網羅シートの罠**: マスタ一覧 / フォーム受領データ / 支払稟議 / 議事録 / 管理表 / "
        "台帳 のような巨大な網羅 file は、あらゆる語が偶然中身に出るため『中身ヒット語あり』"
        "でも答えは載っていないことが多い。**そのシート自体が query の主題でない限り選ばない**。\n"
        + (("- ★ **制約語**: この query の答えは必ず「" + "、".join(str(m) for m in must_terms)
            + "」に関係する。file 名にも中身ヒット語にもこの制約語が無い file は"
            "**選ばない** (他地域・他対象の類似資料は不正解)。制約語に合う file が"
            "1 件も無ければ空 array [] を返す。\n") if must_terms else "")
        + "- ★ 「中身ヒット語」= その file の fullText に実際にヒットした検索語 (= 根拠)。"
        "ヒット語が query の焦点 (固有名詞/制約語) を含む file を優先し、generic 語 1 個"
        "だけの file は選ばない。reason は「〜の可能性」の推測ではなくヒット語を根拠に書く。\n"
        "- ★ **重要: query の本質的な意味** を理解して、file 名から推測される内容と "
        "意味的に合致するか判断。表面的な単語一致 (= 同字異義) で選ばない。\n"
        "  例: query「副業規定」 → 「副業規程.docx」「副業 就業規則.pdf」 を選ぶ。\n"
        "     「副業募集案内.docx」 (= 副業バイト募集、意味違う) は **選ばない**。\n"
        "  例: query「石川県のTVCMのGRP単価」 → 「石川 TVCM 出稿計画.xlsx」(答えが主題) を選ぶ。\n"
        "     「取引先マスタ全リスト」「営業稟議支払い」 (= 語が偶然出るだけの網羅シート) は "
        "**選ばない**。該当が無ければ [] を返す。\n"
        "- ★ **同程度の関連性なら、modifiedTime (= mod=) が新しい方を優先** "
        "(= 古い情報は陳腐化リスク、新しい方が現状反映)\n"
        "- ★ **『開けば答えがある』と言える file が 1 件も無ければ 空 array `[]` 返却** "
        "(= 「該当無し」 が user に正直に伝わる方が、無関係 file を出すより良い)\n\n"
        'output JSON: [{"index": int, "reason": str, "confidence": "high|low"}, ...] or []\n'
        "JSON output:"
    )
    try:
        text = await _generate(prompt, response_json=True, max_tokens=400)
    except GeminiUnavailableError as e:
        logger.warning(f"rerank Gemini unavailable: {e} → deterministic fallback (score/recency 順)")
        return _rerank_fallback(files, top_n, bool(evidence or must_terms))
    try:
        picks = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"rerank: JSON parse failed: {text[:200]}")
        return _rerank_fallback(files, top_n, bool(evidence or must_terms))
    if not isinstance(picks, list):
        return _rerank_fallback(files, top_n, bool(evidence or must_terms))

    # ★Phase 1.2 (海山指示 B): Gemini が返す picks を gemini_rank で保持しつつ、
    # modifiedTime DESC を二次 sort key にして tie-breaker (= 同 rank なら新しい方先)。
    # 実用上 Gemini は通常 distinct rank で返すが、defensive 構造として実装。
    # 加えて 「重複 index」「partial fill」 のエッジケースで modifiedTime 二次 sort が機能。
    result_with_rank = []
    seen_ids: set[str] = set()
    for rank, pick in enumerate(picks[:top_n]):
        if not isinstance(pick, dict):
            continue
        try:
            idx = int(pick.get("index", -1))
        except (ValueError, TypeError):
            continue
        if idx < 1 or idx > len(short_files):
            continue
        f = dict(short_files[idx - 1])
        fid = f.get("id", "")
        if fid and fid in seen_ids:
            continue  # dedupe
        seen_ids.add(fid)
        f["rerank_reason"] = str(pick.get("reason", ""))[:120]
        # ★2026-07-13 海山指示「関連したものが見つからない場合は、無理に提示する必要がない」:
        # Gemini に confidence (high/low) を出させ、呼び手が表示を確度で出し分ける
        f["rerank_confidence"] = (
            "low" if str(pick.get("confidence", "high")).lower() == "low" else "high"
        )
        f["_gemini_rank"] = rank
        result_with_rank.append(f)

    # ★2026-07-13 partial fill (top_n 未達を modifiedTime DESC で埋める) を**廃止**:
    # 埋め合わせは「無理に提示」そのもの (海山指示)。Gemini が確信を持って選んだ分だけ返す。
    # 完全空 [] = 意味的該当無し、も従来どおり空で返す (呼び手が正直に「該当無し」を表示)。

    # ★二次 sort: gemini_rank ASC、同 rank なら modifiedTime DESC
    result_with_rank.sort(
        key=lambda f: (f.get("_gemini_rank", 999), -_iso_to_unix(f.get("modifiedTime", ""))),
    )

    # _gemini_rank field を caller に漏らさず削除
    for f in result_with_rank:
        f.pop("_gemini_rank", None)
    return result_with_rank


def _iso_to_unix(iso_str: str) -> float:
    """ISO 8601 (= modifiedTime 形式) → unix timestamp、parse 失敗時 0."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime
        # Drive API は RFC3339 (= ISO 8601 互換、末尾 Z)
        if iso_str.endswith("Z"):
            iso_str = iso_str[:-1] + "+00:00"
        return datetime.fromisoformat(iso_str).timestamp()
    except Exception:
        return 0.0


# ─── Drive 検索 alias (★2026-06-07 Phase1b: synthetic agent の自律レバー、海山承認 gate 付) ─
# data/brain/drive_search_aliases.json: {term: {"aliases":[...], "enabled":bool, "src":..., "added":...}}
# synthetic_employee_agent が keyword_miss 検知時に「確実な別表記/略称のみ」を enabled=False で自律追記。
# ★cross-check 2026-06-07: rerank は Gemini 失敗/候補≤top_n 等で意味判定を bypass する経路があり
#   「最終フィルタ」にならない。よって検索に効かせるのは **海山が enabled=True にした alias のみ**
#   (= verify-before-activate、未承認 alias による誤リンク再生産を遮断)。findability 限定・事実不介入。
DRIVE_ALIASES_PATH = os.path.join(
    os.getenv("BRAIN_APP_ROOT", "/app"), "data", "brain", "drive_search_aliases.json"
)


def _load_drive_aliases() -> dict:
    try:
        if os.path.exists(DRIVE_ALIASES_PATH):
            with open(DRIVE_ALIASES_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"_load_drive_aliases failed: {e}")
    return {}


def _drive_alias_expansions(query: str, keywords: list, max_add: int = 5) -> list:
    """query/keyword に含まれる term の別表記/略称を返す (検索 fan-out 用、bounded + dedup)。

    ★安全 (cross-check 2026-06-07): rerank は Gemini 失敗 / 候補数 ≤ top_n / partial-fill 等で
    意味判定を bypass し modifiedTime DESC に落ちる経路があるため「最終フィルタ」になり得ない。
    よって **海山が承認した (enabled=True) alias のみ** 検索に効かせる (= 未承認の自律追記は無効、
    誤 alias による誤リンク再生産を承認 gate で遮断)。短すぎる term は部分文字列誤爆源なので除外。
    """
    aliases = _load_drive_aliases()
    if not aliases:
        return []
    hay = (query or "") + " " + " ".join(keywords or [])
    seen = set(keywords or [])
    seen.add(query or "")
    out: list = []
    for term, meta in aliases.items():
        # 海山未承認 (enabled != True) は検索に効かせない。term は誤爆防止に最低 2 字。
        if not isinstance(meta, dict) or meta.get("enabled") is not True:
            continue
        if not term or len(str(term)) < 2 or str(term) not in hay:
            continue
        for s in meta.get("aliases", []):
            s = str(s).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out[:max_add]


async def search_drive_semantic(
    query: str,
    top_n: int = 5,
    since_days: int | None = 365,
    mime_filter: list[str] | None = None,
    apply_default_filters: bool = True,
) -> dict:
    """★end-to-end: query 拡張 → Drive 検索 → re-rank.

    ★2026-05-26 海山指示 Phase 1 (= データ膨大で noise 問題対応):
    - default で過去 365 日 + 4 mime type (= sheets / docs / slides / PDF) に絞り込み
    - top 5 返す (= 旧 3 から拡大)
    - `apply_default_filters=False` で「全 Drive + 全期間 + 全 type」 への拡大可能 (= no-hit fallback 用)

    Args:
        query: 自然言語 query
        top_n: top N 件返す (default 5)
        since_days: modifiedTime > today-N 日 (default 365、None で全期間)
        mime_filter: 指定 mime のみ (default = BOT_SEARCH_DEFAULT_MIMES)
        apply_default_filters: False なら since_days + mime_filter を ignore (= 拡大検索)

    Returns: {
        "query": str,
        "keywords": list[str],
        "total_hits": int,
        "top": list[dict],            # 各 file + rerank_reason
        "all": list[dict],
        "via_gemini": bool,
        "filters_applied": dict,      # {since_days, mime_filter} 反映状態 (= "全期間に拡大" UX 用)
    }
    """
    # query 拡張 (★2026-07-13 構造化: must = 制約語、keywords = fan-out 語)
    try:
        _st = await expand_query_structured(query)
        must_terms = _st.get("must") or []
        keywords = list(dict.fromkeys(must_terms + (_st.get("keywords") or [])))
        via_gemini = bool(keywords)
    except Exception:
        must_terms = []
        keywords = []
        via_gemini = False

    # filter 確定
    from gdrive_sync import discover, BOT_SEARCH_DEFAULT_MIMES  # late import
    if apply_default_filters:
        eff_since = since_days
        eff_mime = mime_filter if mime_filter is not None else list(BOT_SEARCH_DEFAULT_MIMES)
    else:
        eff_since = None
        eff_mime = None

    # Drive fullText 検索 (= 元 query + 抽出 keyword 個別 で OR 並列、id で union)
    # ★2026-06-07: 旧 keywords[:3] だと、LLM が固有名詞 (= クリエイトリンク 等) を
    #   4 位以下に置いた時に検索漏れ → expand_query は最大 5 個しか返さないので全件検索。
    all_results: dict[str, dict] = {}
    hit_map: dict[str, set] = {}  # fid → ヒットした検索語 (★2026-07-13 照合カウント)
    queries_to_run = [query]
    if keywords:
        queries_to_run += keywords[:6]
    # ★2026-06-07 Phase1b: 海山承認済 (enabled=True) alias の別表記のみ検索に追加。
    #   bounded (max 5)。未承認の自律追記分は無効 (= 誤 alias を検索に入れない承認 gate)。
    #   alias 機構の不具合が drive 検索本体を絶対に壊さないよう防御的に握り潰す。
    try:
        queries_to_run += _drive_alias_expansions(query, keywords, max_add=5)
    except Exception as e:
        logger.warning(f"drive alias expansion failed (ignored): {e}")

    # ★2026-07-13 latency fix (海山「時間がかかり過ぎ」実測 8-9 分): ①discover を並列化
    # (content_check=False = 名前/フォルダ除外のみの高速 metadata 検索) ②content 2 次判定は
    # スコアで候補を絞った後に上位だけ並列実行 (下の content_safe_filter)。
    # §1.9 保証は不変: 表示/rerank (外部 Gemini) に渡る file は必ず content 検証済み。
    import time as _time
    _t0 = _time.monotonic()

    async def _disc(q: str):
        try:
            return q, await asyncio.to_thread(
                discover, q, None, 30, "fulltext", True, eff_since, eff_mime, False,
            )
        except Exception as e:
            logger.warning(f"discover fail for {q!r}: {e}")
            return q, []

    for q, results in await asyncio.gather(*[_disc(q) for q in queries_to_run]):
        for f in results:
            fid = f.get("id")
            if not fid:
                continue
            if fid not in all_results:
                all_results[fid] = f
            hit_map.setdefault(fid, set()).add(q)
    _t_disc = _time.monotonic() - _t0

    # ★2026-07-13 決定論スコア (海山「Drive検索の精度が悪い」の核心 fix):
    # 旧実装は 97 hit の**検索実行順の先頭 30 件**だけを rerank に渡していた = 本当に
    # 関連する file が 31 件目以降だと LLM に見えすらしない。複数の検索語にヒットした
    # file (= AND 的に強い) と must 語 (地域等の制約) を決定論で優先してから渡す。
    def _must_hit(f: dict) -> bool:
        if not must_terms:
            return True
        name = f.get("name") or ""
        hqs = hit_map.get(f.get("id", ""), set())
        # ★must 語は定義上必ず元 query の部分文字列 (query から抽出した語) なので、
        # 「元 query でヒットした」ことは must の判別根拠にならない (トートロジー)。
        # 根拠になるのは file 名 or 分解済み keyword (石川県 等) 単体でのヒットのみ
        # (Drive fullText は token 照合 = 単一 keyword ヒットはその語の実在を意味する)。
        return any(
            m in name or any(m in q for q in hqs if q != query)
            for m in must_terms
        )

    def _score(f: dict):
        hqs = hit_map.get(f.get("id", ""), set())
        # ★照合数も元 query を除外 (cross-check DA F3: 元 query は緩く広くヒットするため
        # noise の count を底上げし、niche な正解 file を沈める。must と扱いを一貫させる)
        distinct = sum(1 for q in hqs if q != query)
        return (
            1 if _must_hit(f) else 0,
            distinct,
            _iso_to_unix(f.get("modifiedTime", "")),
        )

    all_files = sorted(all_results.values(), key=_score, reverse=True)
    filters_applied = {
        "since_days": eff_since,
        "mime_filter": eff_mime,
        "default_filters_on": apply_default_filters,
    }

    if not all_files:
        return {
            "query": query, "keywords": keywords, "total_hits": 0,
            "top": [], "all": [], "via_gemini": via_gemini,
            "filters_applied": filters_applied,
            "must_terms": must_terms, "must_hits": 0,
        }

    # ★2026-07-13 latency fix: content 2 次判定 (§1.9) をスコア上位の候補だけに絞って
    # 並列実行。上限 45 件 = 旧「全 hit × 全 query で直列再チェック (実測 8-9 分)」を
    # 数十秒に短縮しつつ、rerank (外部 Gemini) と表示に渡る file は全て検証済みを維持。
    total_discovered = len(all_files)
    from gdrive_sync import content_safe_filter
    _t1 = _time.monotonic()
    candidates = all_files[:45]
    verified = await asyncio.to_thread(content_safe_filter, candidates)
    # ★cross-check DA D1: PII 密集 query (給与/人事系) で上位 45 件がごっそり除外されると
    # 46 位以降の安全・関連 file が不可視化する (旧全件検証からの精度退行) → top_n 未達なら
    # 次スライスを追検証 (verdict cache 共有なので再訪コストは cache miss 分のみ、上限 135)
    _idx = 45
    _checked = len(candidates)
    while len(verified) < top_n and _idx < len(all_files) and _idx < 135:
        _slice = all_files[_idx:_idx + 45]
        verified += await asyncio.to_thread(content_safe_filter, _slice)
        _checked += len(_slice)
        _idx += 45
    verified = verified[:30]
    _t_content = _time.monotonic() - _t1
    # must_hits は検証後の集合で数え直す (PII 除外 file が must_hits を作って
    # 「直接ヒット無し」の正直表示を抑止しないように)
    must_hits = sum(1 for f in verified if must_terms and _must_hit(f))

    if not verified:
        logger.info(
            f"drive search: {total_discovered} discovered but 0 passed content filter"
        )
        return {
            "query": query, "keywords": keywords, "total_hits": total_discovered,
            "top": [], "all": [], "via_gemini": via_gemini,
            "filters_applied": filters_applied,
            "must_terms": must_terms, "must_hits": 0,
        }

    # re-rank (★2026-07-13 根拠渡し: どの検索語にヒットしたかを rerank の判断材料に。
    # 元 query は除外 = 質問文まるごとが「中身ヒット語」として誤読されるのを防ぐ (DA F4))
    evidence = {
        fid: sorted((q for q in qs if q != query), key=lambda q: (len(q), q))[:4]
        for fid, qs in hit_map.items()
    }
    _t2 = _time.monotonic()
    try:
        top = await rerank_results(query, verified, top_n=top_n,
                                   evidence=evidence, must_terms=must_terms)
    except Exception as e:
        logger.warning(f"rerank fail: {e}")
        # ★fallback も決定論スコア順 (旧: 純 recency = 今日更新の無関係 file が並ぶ元凶)
        top = verified[:top_n]
        via_gemini = False
    logger.info(
        f"drive search timing: discover={_t_disc:.1f}s "
        f"content={_t_content:.1f}s ({_checked} checked) "
        f"rerank={_time.monotonic() - _t2:.1f}s total={_time.monotonic() - _t0:.1f}s"
    )

    return {
        "query": query,
        "keywords": keywords,
        "total_hits": total_discovered,
        "top": top,
        "all": verified,
        "via_gemini": via_gemini,
        "must_terms": must_terms,
        "must_hits": must_hits,
        "filters_applied": filters_applied,
    }
