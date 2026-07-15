"""query category 分類 + few-shot literal leak 検出 (★2026-05-24 Plan C v2 Step 6 monitor 最小実装)

Strategy reviewer 指摘「monitor 後回しにすると Phase 2 拡大がやった気で終わる」を受けて、
本番 bot 応答時に query category / response length / few-shot leak / context leak を inline 記録。

# 機能

1. **categorize(query)**: 海山テイスト query を priority 付き keyword で 8 category に分類
   - 挨拶 / 経営判断 / 業務オペ / キャリア相談 / 反論・聞き返し / 自伝・価値観 / 雑談 / その他
   - その他 が 30%+ なら keyword 拡張 / LLM 分類への移行判断材料

2. **load_fewshot_phrases()**: few-shot v1 の literal phrase を 8 char 以上で抽出、cache 化
   - bot 応答に literal で混入したら逐語複写リスク (= 「逐語複写するな」instruction の compliance 測定)

3. **detect_fewshot_leak(response)**: 応答中の literal phrase 一致を返す
4. **detect_context_leak(response)**: 「[Context:」literal の混入を critical level で検出

# 使い方 (brain_wiki.py で inline 呼出)

```python
from scripts.clone_query_category import (
    categorize, detect_fewshot_leak, detect_context_leak
)
ctx["category"] = categorize(query)
ctx["lines"] = out.count("\\n") + 1
ctx["fewshot_leak"] = detect_fewshot_leak(out)
ctx["context_leak"] = detect_context_leak(out)
```

# settings

- 全 keyword は priority dict (= 上から match)、最初に match した category を返す
- few-shot phrase の最小 char 数 8 (= 一般的フレーズの誤検出回避)
- "OWNDAYS" "客単価" 等の業務語は除外 (= 一般語彙、bot が当然使う)
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger("clone_query_category")

# ─── category 分類 keyword (priority 順、上から match) ────────────────────
# Strategy reviewer 想定: 本番 query 分布は 挨拶 + 雑談 60-70%、経営判断 7 / 業務オペ 6 は eval set 偏重
_CATEGORY_KEYWORDS = [
    # 挨拶 (短文 query は最優先で挨拶判定、雑談 keyword より上)
    ("挨拶", ["お疲れさま", "お疲れ", "おはよう", "こんにちは", "こんばんは",
              "ありがとう", "ありがと", "助かりま"]),
    # 自伝・価値観 (= 海山 / あなた / 出身 / 成功 / 価値観)
    ("自伝・価値観", ["海山さん", "あなた", "出身", "成功とは", "価値観",
                    "人生で", "印象的", "好きな"]),
    # 経営判断 (= 売上数字 / 投資 / 戦略 / KPI 系)
    ("経営判断", ["売上", "客単価", "FF", "CVR", "オプション率", "客数",
                "投資", "ROI", "戦略", "中期計画", "出店", "閉店", "閉鎖",
                "M&A", "買収", "事業", "進出"]),
    # 業務オペ (= 店舗 / シフト / 在庫 / 接客 / 訪問)
    ("業務オペ", ["シフト", "在庫", "接客", "クレーム", "店舗訪問", "店長", "スタッフ",
                "オペ", "標準化", "店舗", "VMD", "陳列", "SNS"]),
    # キャリア相談 (= 辞め / 転職 / 迷っ / モチベ / 燃え尽き)
    ("キャリア相談", ["辞め", "転職", "迷ってる", "迷ってます", "モチベ",
                    "燃え尽き", "やる気", "成長", "キャリア", "起業"]),
    # 反論・聞き返し (= でも / しかし / どう思う / 違うと思 / 教えて)
    ("反論・聞き返し", ["でも", "しかし", "違うと思", "本当に",
                      "どう思う", "なぜ", "理由"]),
    # 雑談 (= 好きな / 最近 / ハマ / 趣味 / 本 / 映画 / 食べ物)
    ("雑談", ["最近", "ハマ", "趣味", "本", "映画", "音楽", "食べ物",
            "ストレス", "週末", "休日", "旅行"]),
]


def categorize(query: str) -> str:
    """query を category に分類。priority 順 match、その他 = 未 match。"""
    if not query:
        return "その他"
    q = query.lower()
    for cat, kws in _CATEGORY_KEYWORDS:
        for kw in kws:
            if kw.lower() in q:
                return cat
    return "その他"


# ─── few-shot phrase 抽出 + leak 検出 ─────────────────────────────────────
_FEWSHOT_JSON_PATH = Path("/app/data/brain/wiki/style/few-shot-examples-v1.json")
_LOCAL_FEWSHOT_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "brain" / "wiki" / "style" /
    "few-shot-examples-v1.json"
)

# 業務語彙 / 一般的表現 (= bot が当然使うので literal leak 検出から除外)
_FEWSHOT_PHRASE_WHITELIST = {
    "OWNDAYS", "客単価", "ROI", "FF", "CVR", "オプション率", "NPS",
    "JINS", "MdM", "海山", "うみやま",
}

_FEWSHOT_PHRASE_CACHE: set[str] | None = None


def load_fewshot_phrases() -> set[str]:
    """few-shot v1 から 8 char 以上の literal phrase を抽出、cache 化。

    対象: assistant field の中の 連続漢字 / カタカナ / かな 8 char 以上、whitelist 除外。
    """
    global _FEWSHOT_PHRASE_CACHE
    if _FEWSHOT_PHRASE_CACHE is not None:
        return _FEWSHOT_PHRASE_CACHE

    paths = [_FEWSHOT_JSON_PATH, _LOCAL_FEWSHOT_PATH]
    phrases: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for ex in data.get("examples", []):
                a = ex.get("assistant", "")
                # 8 char 以上の連続漢字 + カタカナ + ひらがな の固まりを抽出
                for m in re.finditer(r"[぀-ゟ゠-ヿ一-鿿]{8,}", a):
                    phr = m.group(0).strip()
                    if phr and phr not in _FEWSHOT_PHRASE_WHITELIST:
                        phrases.add(phr)
            break
        except Exception as e:
            logger.warning(f"few-shot phrase load failed from {p}: {e}")

    _FEWSHOT_PHRASE_CACHE = phrases
    logger.info(f"loaded {len(phrases)} few-shot literal phrases for leak detection")
    return phrases


def detect_fewshot_leak(response: str) -> list[str]:
    """bot 応答に few-shot literal phrase が混入してたら返す。

    検出時 = bot が逐語複写してる (= instruction 違反)。empty list なら問題なし。
    """
    if not response:
        return []
    phrases = load_fewshot_phrases()
    return [p for p in phrases if p in response][:5]  # 最大 5 件で truncate


def detect_context_leak(response: str) -> bool:
    """bot 応答に Contextual Retrieval の `[Context:` prefix が漏出してたら True。

    fix #1 の strip_context_prefix() で防いでるが、念のための double-check。
    True なら critical alert (= log_bot_event で context_prefix_leak 発火)。
    """
    if not response:
        return False
    return "[Context:" in response
