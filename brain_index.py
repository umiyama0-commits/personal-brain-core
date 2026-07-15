"""
brain_index.py — ベクトル索引レイヤー（ChromaDB）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Wiki（Markdown）は「真実のソース」として残し、
ChromaDBはその検索用索引として機能する。

設計:
  1. Wiki/Raw が更新されるたびにチャンクに分割してChromaDBに登録
  2. 質問が来たらベクトル検索で関連チャンクを取得
  3. 関連チャンクだけをLLMに渡す（RAG）

これにより:
  - 全Wikiをコンテキストに詰めなくてよい（コスト削減・精度向上）
  - 数十万チャンクでも数ms で検索可能
  - タグ・日付・ソースでメタデータフィルタも可能
"""

import asyncio
import os
import hashlib
import logging
from datetime import date
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings
import httpx

logger = logging.getLogger(__name__)

CHROMA_DIR = os.getenv("CHROMA_DIR", "/app/chroma_data")
LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000")
LITELLM_KEY = os.getenv("LITELLM_MASTER_KEY", "")

# チャンク設定
CHUNK_SIZE = 500       # 文字数
CHUNK_OVERLAP = 100    # 重複文字数


class BrainIndex:
    """ベクトル索引マネージャ"""

    def __init__(self, http: httpx.AsyncClient, litellm_url: str = "", litellm_key: str = ""):
        self.http = http
        self.litellm_url = litellm_url or LITELLM_URL
        self.litellm_key = litellm_key or LITELLM_KEY

        self.client = chromadb.PersistentClient(
            path=CHROMA_DIR,
            settings=Settings(anonymized_telemetry=False),
        )

        # コレクション: wiki記事、raw会話、それぞれ分ける
        self.wiki_col = self.client.get_or_create_collection(
            name="wiki",
            metadata={"description": "コンパイル済みWiki記事のチャンク"},
        )
        self.raw_col = self.client.get_or_create_collection(
            name="raw",
            metadata={"description": "生データ（会話・メモ・メール等）のチャンク"},
        )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # インデックス登録
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def index_wiki_file(
        self,
        file_path: Path,
        tags: list[str] = None,
        force_contextual: bool = False,
    ):
        """Wiki記事をチャンク分割してベクトル登録。

        Args:
            file_path: wiki file の絶対 path
            tags: メタデータに追加する tag list
            force_contextual: True なら env (ENABLE_CONTEXTUAL_RETRIEVAL) 無視して
                contextualize_chunks() を強制実行 (= MacBook curl reindex 経由想定)。
                ★2026-05-23 海山指示「Mac Studio 手元無い、MacBook 完結」対応。
        """
        # wiki/ 以下の相対パスを保持 (subdir 込み)
        # 同名ファイルを区別し、retrieval 側で WIKI_DIR / src が解決できるようにする
        parts = file_path.parts
        try:
            i = parts.index("wiki")
            rel_path = str(Path(*parts[i + 1:]))
        except ValueError:
            rel_path = str(file_path.name)

        # ★2026-06-28 personal ドメイン分離: wiki/personal/ 配下 (非OWNDAYS、海山の投資/PJ) は
        #   OWNDAYS の chroma "wiki" collection に索引しない = 公開クローン・vector 検索の全経路から除外。
        #   /personal モードは markdown 直読で答えるため索引不要。これが索引の単一 chokepoint
        #   (_initial_reindex の reindex_all_wiki も _watch_wiki_changes も本関数を通る)。
        #   過去に誤索引された残骸があれば掃除して return (DA cross-check: chroma residue 対策)。
        # ★2026-07-03 (v3 ADR DA R6): interview/ は**意図的に索引除外しない** (is_deep_private_rel
        #   に変えない)。海山専用経路 (/mcp/brain brain_search = P3b vector recall) が interview/ を
        #   引くため索引には必要。公開クローンは chroma where (clone_visibility != private) +
        #   runtime visibility gate + path 強制 private (brain_wiki.py) の三重で遮断。
        from brain_wiki_helpers.domain import is_personal_rel
        if is_personal_rel(rel_path):
            try:
                await asyncio.to_thread(self.wiki_col.delete, where={"file": rel_path})
            except Exception:
                pass
            return

        content = file_path.read_text(encoding="utf-8")

        # front matter からメタデータ抽出
        metadata = self._parse_front_matter(content)
        metadata["file"] = rel_path
        metadata["type"] = "wiki"
        # ★2026-07-03 (v3 ADR DA R6 cross-check 1a): chromadb の $ne は metadata 欠落 chunk を
        #   返す (v1.5.7 実測) ため、深層 private path は索引時に clone_visibility=private を
        #   **強制付与** = 公開経路の chroma where 句を frontmatter 剥落に対しても実効化する。
        #   (personal/ は上で早期 return 済なので実質 interview/ 用。admin 経路は metadata
        #   filter を使わないので無影響。)
        from brain_wiki_helpers.domain import is_deep_private_rel
        if is_deep_private_rel(rel_path):
            metadata["clone_visibility"] = "private"
        if tags:
            metadata["tags"] = ",".join(tags)

        # 古いチャンクを削除（ファイル単位で差し替え）
        # ★2026-06-10: 同期 chromadb 呼び出しを to_thread 化。async ハンドラ内で直接実行すると
        #   reindex(起動時 871 file)中に event loop が固まり全 webhook が stall する。chromadb は
        #   coarse-locking で multithread-safe (CLAUDE.md 1.5 の SIGSEGV は別プロセスの話)。
        try:
            await asyncio.to_thread(self.wiki_col.delete, where={"file": rel_path})
        except Exception:
            pass

        # チャンク分割 → 登録
        chunks = self._split_chunks(content)
        if not chunks:
            return

        # ★2026-05-23 Plan C v2 Step 1 (海山 OK): Contextual Retrieval
        # env ENABLE_CONTEXTUAL_RETRIEVAL=true で有効化、Phase gate で段階拡大
        # ★2026-05-23 force_contextual=True (= MacBook curl reindex 経由) で env 無視
        # 失敗時は元 chunk のまま、retrieval 動作は維持される。
        ctx_stats: dict | None = None
        env_enabled = os.getenv("ENABLE_CONTEXTUAL_RETRIEVAL", "false").lower() == "true"
        if force_contextual or env_enabled:
            # Phase gate: force_contextual=True なら全 dir 対象、env mode は CONTEXTUAL_PHASE_DIRS で絞る
            if force_contextual:
                phase_match = True  # force mode は dir 制限無し
            else:
                phase_dirs = os.getenv("CONTEXTUAL_PHASE_DIRS", "meetings").split(",")
                phase_dirs = [d.strip() for d in phase_dirs if d.strip()]
                phase_match = any(f"/{d}/" in str(file_path) or str(file_path).startswith(f"{d}/") for d in phase_dirs)
            if phase_match:
                try:
                    from brain_wiki_helpers.contextual import contextualize_chunks
                    chunks, ctx_stats = await contextualize_chunks(
                        document_text=content,
                        chunks=chunks,
                        http=self.http,
                        litellm_url=self.litellm_url,
                        litellm_key=self.litellm_key,
                    )
                    logger.info(
                        f"Contextual {rel_path}: succeed={ctx_stats['n_succeed']}, "
                        f"failed={ctx_stats['n_failed']}, "
                        f"short_skip={ctx_stats['skipped_short_doc']}"
                    )
                except Exception as e:
                    logger.warning(f"contextualize import/call error for {rel_path}: {e}")
                    ctx_stats = {"error": str(e)[:200]}

        # metadata に Contextual の状態を記録 (= audit / debug 用)
        has_ctx = bool(ctx_stats and ctx_stats.get("n_succeed", 0) > 0)
        metadata["has_context"] = has_ctx
        if ctx_stats:
            metadata["context_model"] = ctx_stats.get("model", "")

        ids = [f"wiki:{rel_path}:{i}" for i in range(len(chunks))]
        metadatas = [{**metadata, "chunk_index": i} for i in range(len(chunks))]

        # Embedding取得
        embeddings = await self._get_embeddings(chunks)

        if embeddings:
            await asyncio.to_thread(
                self.wiki_col.upsert,
                ids=ids,
                documents=chunks,
                embeddings=embeddings,
                metadatas=metadatas,
            )
            logger.info(f"Indexed wiki: {rel_path} → {len(chunks)} chunks")
        else:
            # Embedding失敗時は upsert をスキップ（dim mismatch 回避）。
            # 既存の collection は 1536次元（OpenAI）で作られているため、
            # ChromaDB デフォルト（384次元）を混ぜるとエラーになる。
            logger.warning(
                f"Skip indexing wiki {rel_path}: embedding API unavailable"
            )

    async def index_raw(self, content: str, source: str, metadata: dict = None):
        """生データ（会話、メモ等）をチャンク分割してベクトル登録"""
        meta = metadata or {}
        meta["source"] = source
        meta["type"] = "raw"
        meta["date"] = meta.get("date", date.today().isoformat())

        chunks = self._split_chunks(content)
        if not chunks:
            return

        # コンテンツハッシュでIDを生成（重複防止）
        content_hash = hashlib.md5(content[:500].encode()).hexdigest()[:12]
        ids = [f"raw:{source}:{content_hash}:{i}" for i in range(len(chunks))]
        metadatas = [{**meta, "chunk_index": i} for i in range(len(chunks))]

        embeddings = await self._get_embeddings(chunks)

        if embeddings:
            await asyncio.to_thread(
                self.raw_col.upsert,
                ids=ids,
                documents=chunks,
                embeddings=embeddings,
                metadatas=metadatas,
            )
            logger.info(f"Indexed raw: {source} → {len(chunks)} chunks")
        else:
            # dim mismatch 回避のため upsert をスキップ
            logger.warning(
                f"Skip indexing raw {source}: embedding API unavailable"
            )

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 検索（RAG用）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def search(
        self,
        query: str,
        n_results: int = 10,
        collection: str = "both",
        where: dict = None,
    ) -> list[dict]:
        """ベクトル類似検索。関連チャンクを返す。

        Args:
            query: 検索クエリ
            n_results: 返すチャンク数
            collection: "wiki", "raw", "both"
            where: メタデータフィルタ（例: {"tags": {"$contains": "仕事"}}）
        """
        query_embedding = await self._get_embeddings([query])

        results = []

        if collection in ("wiki", "both"):
            kwargs = {"n_results": n_results}
            if query_embedding:
                kwargs["query_embeddings"] = query_embedding
            else:
                kwargs["query_texts"] = [query]
            if where:
                kwargs["where"] = where

            try:
                wiki_results = await asyncio.to_thread(self.wiki_col.query, **kwargs)
                for i, doc in enumerate(wiki_results["documents"][0]):
                    meta = wiki_results["metadatas"][0][i]
                    dist = wiki_results["distances"][0][i] if wiki_results.get("distances") else 0
                    # ★2026-05-23 Adversary 指摘 fix: Contextual Retrieval の prefix を
                    # prompt 注入時に剥がす (公式 cookbook invariant)。
                    # contextualize_chunks() で chunk doc に baked-in した [Context: ...] を
                    # search 出口で 1 度 strip、retrieval 利用者は意識不要。
                    if meta.get("has_context"):
                        from brain_wiki_helpers.contextual import strip_context_prefix
                        doc = strip_context_prefix(doc)
                    results.append({
                        "content": doc,
                        "source": f"wiki/{meta.get('file', '')}",
                        "metadata": meta,
                        "distance": dist,
                        "collection": "wiki",
                    })
            except Exception as e:
                logger.warning(f"Wiki search error: {e}")

        if collection in ("raw", "both"):
            kwargs = {"n_results": n_results}
            if query_embedding:
                kwargs["query_embeddings"] = query_embedding
            else:
                kwargs["query_texts"] = [query]
            if where:
                kwargs["where"] = where

            try:
                raw_results = await asyncio.to_thread(self.raw_col.query, **kwargs)
                for i, doc in enumerate(raw_results["documents"][0]):
                    meta = raw_results["metadatas"][0][i]
                    dist = raw_results["distances"][0][i] if raw_results.get("distances") else 0
                    results.append({
                        "content": doc,
                        "source": meta.get("source", "raw"),
                        "metadata": meta,
                        "distance": dist,
                        "collection": "raw",
                    })
            except Exception as e:
                logger.warning(f"Raw search error: {e}")

        # 距離でソート（近い順）
        results.sort(key=lambda x: x["distance"])
        return results[:n_results]

    async def build_context(self, query: str, max_chars: int = 4000,
                            public_only: bool = False) -> str:
        """検索結果をLLMに渡すコンテキスト文字列に変換。

        Wiki は整理済み「真実のソース」、raw は生データ（chat等、未検証）。
        Wiki を優先して最低 60% 確保、残りを raw で埋める。
        これにより raw の量に押されて wiki が context から落ちる事故を防ぐ。

        public_only (★2026-07-03 persona-v3 DA): 公開クローンの応答を検証する用途
        (hallucination check 等) 向け。公開 bot が知らない private 知識で採点すると
        誤 verdict になる + 深層人格 (interview/) が evidence として外部 LLM/alert に
        流れるため、wiki は clone_visibility!=private のみ・raw (未検証/visibility 無) は除外。
        """
        wiki_budget = int(max_chars * 0.6)
        raw_budget = max_chars - wiki_budget

        # Wiki と raw を別々に取得
        _where = {"clone_visibility": {"$ne": "private"}} if public_only else None
        wiki_results = await self.search(query, n_results=10, collection="wiki",
                                         where=_where)
        if public_only:
            # belt-and-suspenders (DA 残指摘): metadata 欠落 chunk が $ne を素通りする可能性に
            # 備え、深層 path (interview*/personal*) は path でも落とす (fail-safe 二重化)
            wiki_results = [
                r for r in wiki_results
                if not (r.get("source") or "").removeprefix("wiki/").startswith(
                    ("interview/", "personal/"))
            ]
        raw_results = [] if public_only else await self.search(
            query, n_results=10, collection="raw")

        def pack(results: list[dict], budget: int) -> tuple[list[str], int]:
            parts: list[str] = []
            used = 0
            for r in results:
                chunk = f"[{r['source']}] {r['content']}"
                if used + len(chunk) > budget:
                    continue
                parts.append(chunk)
                used += len(chunk)
            return parts, used

        wiki_parts, wiki_used = pack(wiki_results, wiki_budget)
        # Wiki が予算を使い切らなかった分は raw に還元
        raw_parts, _ = pack(raw_results, raw_budget + (wiki_budget - wiki_used))

        return "\n\n".join(wiki_parts + raw_parts)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 統計
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def get_stats(self) -> dict:
        return {
            "wiki_chunks": self.wiki_col.count(),
            "raw_chunks": self.raw_col.count(),
            "total_chunks": self.wiki_col.count() + self.raw_col.count(),
        }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 全Wiki再インデックス
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    async def reindex_all_wiki(self, wiki_dir: Path):
        """Wiki全体を再インデックス"""
        count = 0
        for md_file in wiki_dir.rglob("*.md"):
            await self.index_wiki_file(md_file)
            count += 1
        logger.info(f"Reindexed {count} wiki files")

    async def reindex_all_raw(self, raw_dir: Path):
        """Raw全体を再インデックス"""
        count = 0
        for md_file in raw_dir.rglob("*.md"):
            content = md_file.read_text(encoding="utf-8")
            await self.index_raw(content, source=str(md_file.name))
            count += 1
        logger.info(f"Reindexed {count} raw files")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Internal
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _split_chunks(self, text: str) -> list[str]:
        """テキストをチャンクに分割"""
        # front matter を除去
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2]

        text = text.strip()
        if not text:
            return []

        # 段落（## 見出し）で分割を試みる
        sections = []
        current = ""
        for line in text.split("\n"):
            if line.startswith("## ") and current:
                sections.append(current.strip())
                current = line + "\n"
            else:
                current += line + "\n"
        if current.strip():
            sections.append(current.strip())

        # 各セクションをさらにCHUNK_SIZEで分割
        chunks = []
        for section in sections:
            if len(section) <= CHUNK_SIZE:
                chunks.append(section)
            else:
                for i in range(0, len(section), CHUNK_SIZE - CHUNK_OVERLAP):
                    chunk = section[i : i + CHUNK_SIZE]
                    if chunk.strip():
                        chunks.append(chunk.strip())

        return chunks

    def _parse_front_matter(self, text: str) -> dict:
        """Markdown front matter からメタデータを抽出"""
        metadata = {}
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().split("\n"):
                    if ":" in line:
                        key, val = line.split(":", 1)
                        metadata[key.strip()] = val.strip().strip("[]")
        return metadata

    async def _get_embeddings(self, texts: list[str]) -> list[list[float]] | None:
        """LiteLLM 経由で embedding を取得

        OpenAI embedding API のバッチ制約（トークン上限）に対応するため、
        1リクエストあたり最大 EMBED_BATCH_SIZE チャンクに分割して発行する。
        1チャンクでも失敗したら None（=ChromaDB デフォルト fallback）。
        """
        if not texts:
            return []

        # OpenAI text-embedding-3-small: batch内トークン数制限があるため、
        # チャンク数でも明示的に分割する（100チャンク ≈ 50K chars まで）
        EMBED_BATCH_SIZE = 100
        all_embeddings: list[list[float]] = []
        try:
            for i in range(0, len(texts), EMBED_BATCH_SIZE):
                batch = texts[i : i + EMBED_BATCH_SIZE]
                resp = await self.http.post(
                    f"{self.litellm_url}/v1/embeddings",
                    headers={"Authorization": f"Bearer {self.litellm_key}"},
                    json={
                        "model": "text-embedding-3-small",
                        "input": batch,
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                all_embeddings.extend(d["embedding"] for d in data["data"])
            return all_embeddings
        except Exception as e:
            logger.warning(f"Embedding API error: {e} — using ChromaDB default")
            return None
