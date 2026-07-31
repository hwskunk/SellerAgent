"""
Milvus 客户端 — 混合检索（DashScope Embedding + BM25 关键词）

- 存储粒度：文本切片（chunk），通过 parent_doc_id 关联到逻辑文档
- 稠密向量：DashScope text-embedding-v3 (1024d)，API 调用，无需本地模型
- 关键词检索：rank-bm25（独立于 Milvus，纯内存索引）
- 融合：RRF 粗排 → top_k
- 旧 Reranker 代码保留（_rerank / _get_reranker），当前检索流程未启用
"""
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from pymilvus import MilvusClient
from langchain_openai import OpenAIEmbeddings

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = os.environ.get("MILVUS_DB_PATH", str(BASE_DIR / "milvus_data" / "seller_kb.db"))
COLLECTION_NAME = os.environ.get("MILVUS_COLLECTION_NAME", "seller_knowledge")
DENSE_DIM = 1024
RECALL_PER_LEG = 10   # 粗排每路召回量（Dense/BM25 各取 10）

# DashScope embedding 配置（与 AssistantAgent 一致）
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v3")


class MilvusKB:
    """Milvus 知识库客户端。"""

    def __init__(self):
        self.client: Optional[MilvusClient] = None
        self._dense_embedding = None        # OpenAIEmbeddings（云端）或 SentenceTransformer（本地）
        self._local_model = None            # SentenceTransformer 实例（本地模式）
        self._reranker = None        # BGE Reranker Cross-encoder（当前未启用）
        self._bm25 = None            # rank-bm25 索引
        self._bm25_texts: list[str] = []   # 与 BM25 索引对应的原始文本
        self._bm25_metas: list[dict] = []  # 与 BM25 索引对应的元数据
        self._bm25_dirty = True      # 是否需要重建 BM25 索引
        # 自动判断：含 "/" 的是本地 HuggingFace 模型，否则走云端 API
        self._use_local_embedding = "/" in EMBEDDING_MODEL

    # ── 初始化 ──

    def init(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.client = MilvusClient(str(DB_PATH))

        if self.client.has_collection(COLLECTION_NAME):
            self.client.load_collection(COLLECTION_NAME)
            print(f"[Milvus] Loaded collection: {COLLECTION_NAME}")
        else:
            self._create_collection()
        return self

    def list_doc_ids(self) -> set[str]:
        """返回 Milvus 中实际存在的所有 parent_doc_id（用于与 SQLite 同步校验）。"""
        if not self.client or not self.client.has_collection(COLLECTION_NAME):
            return set()
        ids = set()
        offset = 0
        while True:
            batch = self.client.query(
                collection_name=COLLECTION_NAME,
                filter="id >= 0",
                output_fields=["parent_doc_id"],
                limit=500, offset=offset,
            )
            if not batch:
                break
            for row in batch:
                ids.add(row.get("parent_doc_id", ""))
            offset += len(batch)
        return ids

    def _create_collection(self):
        """创建集合（简单 API，兼容 milvus-lite Linux）。"""
        self.client.create_collection(
            collection_name=COLLECTION_NAME,
            dimension=DENSE_DIM,
            metric_type="IP",
        )
        self.client.load_collection(COLLECTION_NAME)
        print(f"[Milvus] Created collection: {COLLECTION_NAME}")

    # ── Embedding（自动适配本地/云端）──

    def _get_dense_embedding(self):
        """获取 Embedding 客户端。

        本地模式（EMBEDDING_MODEL 含 "/"，如 BAAI/bge-m3）：SentenceTransformer 本地推理
        云端模式（如 text-embedding-v3）：DashScope API
        """
        if self._use_local_embedding:
            if self._local_model is None:
                from sentence_transformers import SentenceTransformer
                print(f"[Milvus] Loading local embedding model: {EMBEDDING_MODEL} ...")
                self._local_model = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
            return self._local_model
        else:
            if self._dense_embedding is None:
                import httpx
                http_client = httpx.Client(
                    timeout=30.0,
                    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
                )
                self._dense_embedding = OpenAIEmbeddings(
                    model=EMBEDDING_MODEL,
                    openai_api_key=DASHSCOPE_API_KEY,
                    openai_api_base=DASHSCOPE_BASE_URL,
                    tiktoken_enabled=False,
                    check_embedding_ctx_length=False,
                    http_client=http_client,
                )
            return self._dense_embedding

    def _encode_dense(self, texts: list[str]) -> list[list[float]]:
        """编码文本为稠密向量。

        本地模式：SentenceTransformer.encode() 批量推理（本地 CPU）
        云端模式：DashScope API 并行调用（最多 3 并发）
        """
        if self._use_local_embedding:
            model = self._get_dense_embedding()
            _t0 = time.time()
            vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            print(f"[Milvus] Local encode: {len(texts)} texts in {time.time() - _t0:.1f}s")
            return vectors.tolist()

        from concurrent.futures import ThreadPoolExecutor, as_completed
        emb = self._get_dense_embedding()
        batches = []
        for i in range(0, len(texts), 10):
            batches.append((i // 10, texts[i:i + 10]))
        if not batches:
            return []

        results = {}
        _t0 = time.time()
        with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
            futures = {
                pool.submit(emb.embed_documents, batch): idx
                for idx, batch in batches
            }
            for f in as_completed(futures):
                idx = futures[f]
                results[idx] = f.result()
        _elapsed = time.time() - _t0

        all_vectors = []
        for i in range(len(batches)):
            all_vectors.extend(results[i])

        if len(batches) > 1:
            print(f"[Milvus] Embedding API: {len(texts)} texts in {len(batches)} batches, "
                  f"parallel {min(3, len(batches))}x → {_elapsed:.1f}s")
        return all_vectors

    # ── Reranker 精排（保留代码，当前未启用）──

    def _get_reranker(self):
        """延迟加载 BGE Reranker Cross-encoder 模型。"""
        if self._reranker is None:
            from sentence_transformers import CrossEncoder
            model_name = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")
            print(f"[Milvus] Loading reranker model: {model_name} ...")
            self._reranker = CrossEncoder(model_name, device="cpu")
        return self._reranker

    def _rerank(
        self,
        query: str,
        candidates: list[dict],
        top_k: int,
    ) -> list[dict]:
        """对 RRF 粗排后的候选做 Cross-encoder 精排。

        Cross-encoder 同时编码 (query, chunk) 对，输出相关度分数，
        精度远高于 Bi-encoder 的向量内积。
        """
        if len(candidates) <= top_k:
            return candidates

        reranker = self._get_reranker()
        pairs = [(query, c["content"]) for c in candidates]
        scores = reranker.predict(pairs, show_progress_bar=False)

        for i, c in enumerate(candidates):
            c["_rerank_score"] = float(scores[i])

        candidates.sort(key=lambda c: c.get("_rerank_score", 0), reverse=True)

        top = candidates[:top_k]
        for c in top:
            c["score"] = round(c.get("_rerank_score", 0), 4)
            c.pop("_rerank_score", None)
        for c in candidates[top_k:]:
            c.pop("_rerank_score", None)

        return top

    # ── Chunk CRUD ──

    def insert_chunks(
        self,
        doc_id: str,
        title: str,
        source: str,
        chunks: list[dict],
        # chunks: [{"index": 0, "content": "...", "lines": "1-5"}, ...]
    ) -> int:
        """批量插入文档的所有 chunk 到 Milvus。"""
        if not self.client:
            raise RuntimeError("Milvus 未初始化")
        if not chunks:
            return 0

        contents = [c["content"] for c in chunks]
        _t_emb = time.time()
        vectors = self._encode_dense(contents)
        _emb_time = time.time() - _t_emb
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 简单 API 的 id 是 int64，用 doc_id 哈希做基数确保唯一且可关联
        base = abs(hash(doc_id)) % (10 ** 12)

        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": base * 10000 + i,
                "vector": vectors[i],
                "parent_doc_id": doc_id,
                "title": title,
                "content": chunk["content"],
                "chunk_index": chunk["index"],
                "chunk_count": len(chunks),
                "chunk_lines": chunk.get("lines", ""),
                "source": source,
                "created_at": created_at,
            })

        _t_insert = time.time()
        self.client.insert(collection_name=COLLECTION_NAME, data=data)
        _insert_time = time.time() - _t_insert
        self._bm25_dirty = True  # 数据变更，下次检索时重建 BM25
        print(f"[Milvus] insert_chunks: embedding={_emb_time:.1f}s ({len(chunks)} chunks × API), "
              f"milvus_insert={_insert_time:.1f}s")
        return len(chunks)

    def delete_chunks(self, doc_id: str) -> int:
        """删除指定文档的所有 chunk。"""
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        result = self.client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'parent_doc_id == "{doc_id}"',
        )
        if isinstance(result, dict):
            deleted = result.get("deleted_count", 0) or result.get("delete_count", 0)
        elif isinstance(result, list):
            deleted = len(result)
        else:
            deleted = 0
        self._bm25_dirty = True  # 数据变更，下次检索时重建 BM25
        print(f"[Milvus] Deleted {deleted} chunks for doc: {doc_id}")
        return deleted

    # ── 混合检索 ──

    def _tokenize(self, text: str) -> list[str]:
        """中文分词，用于 BM25 关键词检索。

        优先使用 jieba，不可用时回退到字符级 bigram（BM25 仍有区分度）。
        """
        try:
            import jieba
            return [t for t in jieba.cut(text) if t.strip()]
        except ImportError:
            # 字符级 bigram：对中文友好，对英文退化到空格分词
            import re
            tokens = []
            for word in text.split():
                if re.search(r'[一-鿿]', word):
                    chars = re.findall(r'[一-鿿]', word)
                    for i in range(len(chars)):
                        if i < len(chars) - 1:
                            tokens.append(chars[i] + chars[i+1])
                        else:
                            tokens.append(chars[i])
                else:
                    tokens.append(word.lower())
            return [t for t in tokens if t.strip()]

    def _build_bm25(self):
        """从 Milvus 加载所有 chunk 文本，构建 rank-bm25 索引。"""
        if not self.client:
            return

        from rank_bm25 import BM25Okapi

        # 分页加载所有 chunk
        all_chunks = []
        offset = 0
        while True:
            batch = self.client.query(
                collection_name=COLLECTION_NAME,
                filter="id >= 0",
                output_fields=["id", "parent_doc_id", "title", "content",
                               "source", "created_at", "chunk_index",
                               "chunk_count", "chunk_lines"],
                limit=200, offset=offset,
            )
            if not batch:
                break
            all_chunks.extend(batch)
            offset += len(batch)

        self._bm25_texts = [c.get("content", "") for c in all_chunks]
        self._bm25_metas = all_chunks  # 保存完整元数据，检索时直接取用

        tokenized = [self._tokenize(t) for t in self._bm25_texts]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None
        self._bm25_dirty = False
        print(f"[Milvus] BM25 index built: {len(self._bm25_texts)} chunks")

    def _bm25_search(self, query: str, top_k: int) -> list[dict]:
        """用 rank-bm25 做关键词检索，返回格式与 dense 搜索一致。"""
        if self._bm25_dirty or self._bm25 is None:
            self._build_bm25()

        if not self._bm25 or not self._bm25_texts:
            return []

        tokenized = self._tokenize(query)
        if not tokenized:
            return []

        scores = self._bm25.get_scores(tokenized)
        indexed = list(enumerate(scores))
        indexed.sort(key=lambda x: x[1], reverse=True)
        top_indices = [i for i, s in indexed[:top_k] if s > 0]

        results = []
        for idx in top_indices:
            meta = self._bm25_metas[idx]
            score = float(scores[idx])
            results.append({
                "id": meta.get("id", ""),
                "parent_doc_id": meta.get("parent_doc_id", ""),
                "title": meta.get("title", ""),
                "content": meta.get("content", ""),
                "source": meta.get("source", ""),
                "source_file": meta.get("source", ""),
                "created_at": meta.get("created_at", ""),
                "chunk_index": meta.get("chunk_index", 0),
                "chunk_count": meta.get("chunk_count", 1),
                "chunk_lines": meta.get("chunk_lines", ""),
                "score": round(score, 4),
                "_bm25_score": score,
            })
        return results

    def _rrf_fuse(
        self,
        dense_results: list[dict],
        bm25_results: list[dict],
        k: int = 60,
    ) -> list[dict]:
        """RRF (Reciprocal Rank Fusion) 融合稠密和关键词检索结果。"""
        merged: dict[str, dict] = {}
        rrf_scores: dict[str, float] = {}

        for rank, doc in enumerate(dense_results):
            cid = doc.get("id", "")
            if cid not in merged:
                merged[cid] = doc
                rrf_scores[cid] = 0.0
            rrf_scores[cid] += 1.0 / (k + rank + 1)

        for rank, doc in enumerate(bm25_results):
            cid = doc.get("id", "")
            if cid not in merged:
                merged[cid] = doc
                rrf_scores[cid] = 0.0
            rrf_scores[cid] += 1.0 / (k + rank + 1)

        sorted_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)

        results = []
        for cid in sorted_ids:
            doc = merged[cid]
            doc["score"] = round(rrf_scores[cid], 4)
            doc.pop("_bm25_score", None)
            results.append(doc)

        return results

    def hybrid_search(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索：Dense (DashScope API) + BM25 → RRF 融合。

        当前流程（DashScope，无需本地模型）：
          Dense ANN (DashScope embedding, 10) + BM25 (rank-bm25, 10)
          → RRF 融合去重
          → top_k

        旧流程（如需恢复 Reranker 精排，取消下面注释即可）：
          RRF 融合 → _rerank(query, fused, top_k)
        """
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        t_start = time.perf_counter()

        # ── Step 1: Dense 粗排（DashScope embedding + Milvus ANN）──
        t1 = time.perf_counter()
        dense_vec = self._encode_dense([query])[0]
        t_dense_encode = time.perf_counter() - t1

        dense_raw = self.client.search(
            collection_name=COLLECTION_NAME,
            data=[dense_vec],
            anns_field="vector",
            limit=RECALL_PER_LEG,
            output_fields=["id", "parent_doc_id", "title", "content",
                           "source", "created_at", "chunk_index",
                           "chunk_count", "chunk_lines"],
            search_params={"metric_type": "IP", "params": {"nprobe": 16}},
        )

        dense_results = []
        for hit in dense_raw[0]:
            entity = hit.get("entity", hit)
            pid = entity.get("parent_doc_id", "")
            if not pid:
                continue
            chunk_idx = entity.get("chunk_index", 0)
            chunk_total = entity.get("chunk_count", 1)
            lines = entity.get("chunk_lines", "")
            source_label = f"{entity.get('title', '')} · 片段{chunk_idx + 1}/{chunk_total}"
            if lines:
                source_label += f" (第{lines}行)"

            dense_results.append({
                "id": entity.get("id", ""),
                "parent_doc_id": pid,
                "title": entity.get("title", ""),
                "content": entity.get("content", ""),
                "source": source_label,
                "source_file": entity.get("source", ""),
                "created_at": entity.get("created_at", ""),
                "chunk_index": chunk_idx,
                "chunk_count": chunk_total,
                "chunk_lines": lines,
                "score": round(hit.get("distance", 0), 4),
            })
        t_dense = time.perf_counter() - t1

        # ── Step 2: BM25 粗排 ──
        t2 = time.perf_counter()
        bm25_results = self._bm25_search(query, RECALL_PER_LEG)
        t_bm25 = time.perf_counter() - t2

        # ── Step 3: RRF 融合去重 ──
        t3 = time.perf_counter()
        fused = self._rrf_fuse(dense_results, bm25_results, k=60)

        # ── Step 4: 取 top_k（如需 Reranker 精排，改为 _rerank(query, fused, top_k)）──
        results = fused[:top_k]
        t_fusion = time.perf_counter() - t3

        t_total = time.perf_counter() - t_start
        print(
            f"[Milvus] search timing: "
            f"encode={t_dense_encode:.2f}s "
            f"dense={t_dense:.2f}s ({len(dense_results)} docs) "
            f"bm25={t_bm25:.2f}s ({len(bm25_results)} docs) "
            f"fusion={t_fusion:.2f}s ({len(fused)} candidates → {len(results)}) "
            f"total={t_total:.2f}s"
        )
        return results


_kb_instance: Optional[MilvusKB] = None


def get_kb() -> MilvusKB:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = MilvusKB()
        _kb_instance.init()
    return _kb_instance
