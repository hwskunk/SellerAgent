"""
Milvus 客户端 — 混合检索（稠密向量 + BM25 关键词 + Reranker 精排）

- 存储粒度：文本切片（chunk），通过 parent_doc_id 关联到逻辑文档
- 稠密向量：BGE-M3 (1024d)，Milvus ANN
- 关键词检索：rank-bm25（独立于 Milvus，避免 Windows sparse 索引 bug）
- 精排：BAAI/bge-reranker-base Cross-encoder
- 融合：RRF 粗排 → Reranker 精排 → top_k

Schema:
  id             VARCHAR(64)  PK — chunk 唯一 ID
  parent_doc_id  VARCHAR(64)     — 所属文档 ID
  title          VARCHAR(512)    — 文档标题
  content        VARCHAR(65535)  — chunk 文本
  content_sparse SPARSE_FLOAT    — BM25 自动生成（Milvus 内置，仅存储不用）
  dense_vector   FLOAT[1024]     — BGE-M3 编码
  chunk_index    INT64           — 第几个 chunk (0-based)
  chunk_count    INT64           — 该文档共有几个 chunk
  chunk_lines    VARCHAR(32)     — "45-60" 格式，chunk 对应的原文行范围
  source         VARCHAR(256)    — 原始文件名
  created_at     VARCHAR(64)
"""
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from pymilvus import MilvusClient, DataType, Function, FunctionType, AnnSearchRequest, RRFRanker

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = os.environ.get("MILVUS_DB_PATH", str(BASE_DIR / "milvus_data" / "seller_kb.db"))
COLLECTION_NAME = os.environ.get("MILVUS_COLLECTION_NAME", "seller_knowledge")
DENSE_DIM = 1024
RECALL_PER_LEG = 10   # 粗排每路召回量（Dense/BM25 各取 10）
RERANKER_MODEL = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")

class MilvusKB:
    """Milvus 知识库客户端。"""

    def __init__(self):
        self.client: Optional[MilvusClient] = None
        self._dense_model = None
        self._reranker = None        # BGE Reranker Cross-encoder
        self._bm25 = None            # rank-bm25 索引
        self._bm25_texts: list[str] = []   # 与 BM25 索引对应的原始文本
        self._bm25_metas: list[dict] = []  # 与 BM25 索引对应的元数据
        self._bm25_dirty = True      # 是否需要重建 BM25 索引

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

    def _create_collection(self):
        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("parent_doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=65535, enable_analyzer=True)
        schema.add_field("content_sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=DENSE_DIM)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("chunk_count", DataType.INT64)
        schema.add_field("chunk_lines", DataType.VARCHAR, max_length=32)
        schema.add_field("source", DataType.VARCHAR, max_length=256)
        schema.add_field("created_at", DataType.VARCHAR, max_length=64)

        schema.add_function(Function(
            name="bm25", function_type=FunctionType.BM25,
            input_field_names=["content"], output_field_names="content_sparse",
        ))

        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="dense_vector", index_type="AUTOINDEX", metric_type="IP")
        # Windows milvus-lite 建第二个索引时 os.rename bug，跳过 sparse 索引

        self.client.create_collection(COLLECTION_NAME, schema=schema, index_params=index_params)
        print(f"[Milvus] Created collection: {COLLECTION_NAME}")

    # ── Embedding ──

    def _get_dense_model(self):
        if self._dense_model is None:
            from sentence_transformers import SentenceTransformer
            model_name = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
            print(f"[Milvus] Loading embedding model: {model_name} ...")
            self._dense_model = SentenceTransformer(model_name, device="cpu")
        return self._dense_model

    def _encode_dense(self, texts: list[str]) -> list[list[float]]:
        model = self._get_dense_model()
        embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()

    # ── Reranker 精排 ──

    def _get_reranker(self):
        """延迟加载 BGE Reranker Cross-encoder 模型。"""
        if self._reranker is None:
            from sentence_transformers import CrossEncoder
            print(f"[Milvus] Loading reranker model: {RERANKER_MODEL} ...")
            self._reranker = CrossEncoder(RERANKER_MODEL, device="cpu")
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

        # 把 reranker 分数写入每个候选
        for i, c in enumerate(candidates):
            c["_rerank_score"] = float(scores[i])

        # 按 reranker 分数重新排序
        candidates.sort(key=lambda c: c.get("_rerank_score", 0), reverse=True)

        # 用 reranker 分数替换原来的 RRF 分数作为对外 score
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
        vectors = self._encode_dense(contents)
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        data = []
        for i, chunk in enumerate(chunks):
            data.append({
                "id": f"{doc_id}_c{i}",
                "parent_doc_id": doc_id,
                "title": title,
                "content": chunk["content"],
                "dense_vector": vectors[i],
                "chunk_index": chunk["index"],
                "chunk_count": len(chunks),
                "chunk_lines": chunk.get("lines", ""),
                "source": source,
                "created_at": created_at,
            })

        self.client.insert(collection_name=COLLECTION_NAME, data=data)
        self._bm25_dirty = True  # 数据变更，下次检索时重建 BM25
        print(f"[Milvus] Inserted {len(chunks)} chunks for doc: {doc_id}")
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
            # 先按空白切
            for word in text.split():
                # 对连续中文做 bigram
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
        """从 Milvus 加载所有 chunk 文本，构建 rank-bm25 索引。

        BM25 是纯内存索引，不依赖 Milvus 的 sparse 功能，
        完全规避 Windows 下 milvus-lite 无法创建 sparse 索引的 bug。
        """
        if not self.client:
            return

        from rank_bm25 import BM25Okapi

        # 分页加载所有 chunk
        all_chunks = []
        offset = 0
        while True:
            batch = self.client.query(
                collection_name=COLLECTION_NAME,
                filter="id != ''",
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
        # 取 top_k 索引（分数越高越好）
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
        """RRF (Reciprocal Rank Fusion) 融合稠密和关键词检索结果。

        每个结果取两种排序的倒数排名加权，k 控制平滑程度。
        与 Milvus 原生 RRFRanker 逻辑一致。
        """
        # 构建 chunk id → 结果 的映射
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

        # 按 RRF 分数排序
        sorted_ids = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)

        results = []
        for cid in sorted_ids:
            doc = merged[cid]
            doc["score"] = round(rrf_scores[cid], 4)
            # 去掉内部字段
            doc.pop("_bm25_score", None)
            results.append(doc)

        return results

    def hybrid_search(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索：RRF 粗排 → Reranker 精排。

        粗排：Dense (Milvus ANN, 10) + BM25 (rank-bm25, 10)
        精排：BGE Cross-encoder 对去重后的候选语义重排，取 top_k。
        """
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        t_start = time.perf_counter()

        # ── Step 1: Dense 粗排（Milvus ANN）──
        t1 = time.perf_counter()
        dense_vec = self._encode_dense([query])[0]
        t_dense_encode = time.perf_counter() - t1

        dense_raw = self.client.search(
            collection_name=COLLECTION_NAME,
            data=[dense_vec],
            anns_field="dense_vector",
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
        fused = self._rrf_fuse(dense_results, bm25_results, k=60)

        # ── Step 4: 加权融合精排（零额外推理）──
        t3 = time.perf_counter()
        # Dense 和 BM25 分数归一化后加权融合，替代 Reranker
        # dense: BGE-M3 内积归一化后 [0, 1]
        # bm25:  原始 BM25 分数，按 min-max 归一化
        # 不做 Cross-encoder，CPU 上每对 0.5s 太慢
        reranked = self._rerank(query, fused, top_k)
        t_fusion = time.perf_counter() - t3

        t_total = time.perf_counter() - t_start
        print(
            f"[Milvus] search timing: "
            f"encode={t_dense_encode:.2f}s "
            f"dense={t_dense:.2f}s ({len(dense_results)} docs) "
            f"bm25={t_bm25:.2f}s ({len(bm25_results)} docs) "
            f"fusion={t_fusion:.2f}s ({len(fused)} candidates → {len(reranked)}) "
            f"total={t_total:.2f}s"
        )
        return reranked


_kb_instance: Optional[MilvusKB] = None


def get_kb() -> MilvusKB:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = MilvusKB()
        _kb_instance.init()
    return _kb_instance
