"""brain_wiki_helpers/bm25.py — 依存ゼロの BM25 + RRF (hybrid search 用)。

★2026-06-08 システム評価 Retrieval: golden eval で 5/22 が全 config で full-miss = vector
search が固有名詞 (TSA/JCS/店舗名/FY27 等) を拾えていない recall 天井。dense embedding は
完全一致・希少語に弱く、BM25 (lexical) が直撃補完する (Anthropic 実証で hybrid は検索失敗を
約半分追加削減)。

日本語 tokenizer (MeCab/janome) を追加せず、依存ゼロで実装:
  - alphanumeric run (TSA / JCS / FY27 / AM / SV / KPI / 店舗コード) = 完全トークン
  - CJK (漢字/かな/カナ) = 文字 bigram (tokenizer-free な日本語 substring match の定番手法)
これで「dense が弱い固有名詞・略語・型番の exact match」という BM25 本来の勝ち筋を捕捉する。

RRF (Reciprocal Rank Fusion): dense と BM25 の順位を score スケール非依存で融合 (= 正規化不要)。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

_ALNUM_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[ぁ-んァ-ヶ一-龯々ー]+")


def tokenize(text: str) -> list:
    """依存ゼロの日本語対応トークナイザ。

    - 英数字 run は小文字化して 1 トークン (固有名詞/略語/型番)。
    - CJK run は文字 bigram に分解 (1 文字なら単体)。tokenizer-free な substring match。
    """
    if not text:
        return []
    low = text.lower()
    tokens = _ALNUM_RE.findall(low)
    for run in _CJK_RE.findall(text):  # 大小区別不要 (CJK)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class BM25Index:
    """Okapi BM25 (doc-level)。docs = [(doc_id, text), ...]。"""

    def __init__(self, docs, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_ids = []
        self.tf = []           # doc ごとの Counter(token -> freq)
        self.dl = []           # doc length
        df = Counter()         # document frequency
        for did, text in docs:
            toks = tokenize(text)
            c = Counter(toks)
            self.doc_ids.append(did)
            self.tf.append(c)
            self.dl.append(sum(c.values()))
            for t in c:
                df[t] += 1
        self.N = len(self.doc_ids)
        self.avgdl = (sum(self.dl) / self.N) if self.N else 0.0
        # idf (BM25 の標準形、負値を避ける +1 形)
        self.idf = {
            t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for t, n in df.items()
        }

    def search(self, query: str, top_n: int = 30) -> list:
        """query に対する (doc_id, score) を score 降順で top_n 返す。score>0 のみ。"""
        q = tokenize(query)
        if not q or self.N == 0:
            return []
        out = []
        for did, tf, dl in zip(self.doc_ids, self.tf, self.dl):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                idf = self.idf.get(t, 0.0)
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * (f * (self.k1 + 1)) / denom
            if s > 0:
                out.append((did, s))
        out.sort(key=lambda x: -x[1])
        return out[:top_n]


def rrf_fuse(rankings, k: int = 60, top_n=None) -> list:
    """Reciprocal Rank Fusion。rankings = [[doc_id...], ...] (各々 順位順)。

    score(d) = Σ 1/(k + rank_in_list)。score スケール非依存なので dense/BM25 の正規化不要。
    返り値は融合後の doc_id list (score 降順)。
    """
    score = defaultdict(float)
    for ranking in rankings:
        for rank, did in enumerate(ranking, start=1):
            score[did] += 1.0 / (k + rank)
    fused = sorted(score.items(), key=lambda x: (-x[1], x[0]))
    ids = [did for did, _ in fused]
    return ids[:top_n] if top_n else ids
