"""
Milvus 客户端 — 原生混合检索（稠密向量 + BM25 稀疏向量）

- 稠密向量：BGE-M3 (1024d)，Milvus ANN
- 稀疏向量：Milvus 内置 BM25 Function，自动从 content 生成
- 融合：Milvus 原生 hybrid_search + RRF
"""
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from pymilvus import MilvusClient, DataType, Function, FunctionType, AnnSearchRequest

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = os.environ.get("MILVUS_DB_PATH", str(BASE_DIR / "milvus_data" / "seller_kb.db"))
COLLECTION_NAME = os.environ.get("MILVUS_COLLECTION_NAME", "seller_knowledge")
DENSE_DIM = 1024


class MilvusKB:
    """Milvus 知识库客户端。"""

    def __init__(self):
        self.client: Optional[MilvusClient] = None
        self._dense_model = None

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
        schema.add_field("title", DataType.VARCHAR, max_length=512)
        schema.add_field("content", DataType.VARCHAR, max_length=65535, enable_analyzer=True)
        schema.add_field("content_sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("dense_vector", DataType.FLOAT_VECTOR, dim=DENSE_DIM)
        schema.add_field("source", DataType.VARCHAR, max_length=256)
        schema.add_field("created_at", DataType.VARCHAR, max_length=64)
        schema.add_field("char_count", DataType.INT64)

        schema.add_function(Function(
            name="bm25", function_type=FunctionType.BM25,
            input_field_names=["content"], output_field_names="content_sparse",
        ))

        # 只建 dense 索引（content_sparse 在 Windows 下建索引有 milvus-lite bug）
        index_params = self.client.prepare_index_params()
        index_params.add_index(field_name="dense_vector", index_type="AUTOINDEX", metric_type="IP")

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

    # ── CRUD ──

    def add_document(self, title: str, content: str, source: str = "manual") -> str:
        if not self.client:
            raise RuntimeError("Milvus 未初始化")
        doc_id = str(uuid.uuid4())[:8]
        dense_vec = self._encode_dense([content])[0]
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.client.insert(collection_name=COLLECTION_NAME, data=[{
            "id": doc_id, "title": title, "content": content,
            "dense_vector": dense_vec, "source": source, "created_at": created_at,
            "char_count": len(content),
        }])
        print(f"[Milvus] Doc added: id={doc_id}, title={title}")
        return doc_id

    def delete_document(self, doc_id: str) -> bool:
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        result = self.client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'id == "{doc_id}"',
        )
        # pymilvus 版本兼容：2.x 返回 list，3.x 返回 dict
        if isinstance(result, dict):
            deleted = result.get("deleted_count", 0) or result.get("delete_count", 0)
        elif isinstance(result, list):
            deleted = len(result)
        else:
            deleted = 0
        print(f"[Milvus] Doc deleted: id={doc_id}, deleted={deleted}")
        return deleted > 0

    def list_documents(self) -> list[dict]:
        if not self.client:
            return []
        results = self.client.query(
            collection_name=COLLECTION_NAME, filter="id != ''",
            output_fields=["id", "title", "source", "created_at", "char_count"],
            limit=10000,
        )
        return [
            {
                "id": r["id"], "title": r.get("title", ""),
                "source": r.get("source", ""), "created_at": r.get("created_at", ""),
                "char_count": r.get("char_count", 0),
            }
            for r in results
        ]

    def get_document(self, doc_id: str) -> dict | None:
        if not self.client:
            return None
        results = self.client.query(
            collection_name=COLLECTION_NAME, filter=f'id == "{doc_id}"',
            output_fields=["id", "title", "content", "source", "created_at"],
            limit=1,
        )
        if not results:
            return None
        r = results[0]
        content = r.get("content", "")
        return {
            "id": r["id"], "title": r.get("title", ""),
            "content": content,
            "source": r.get("source", ""), "created_at": r.get("created_at", ""),
            "char_count": len(content),
        }

    def get_stats(self) -> dict:
        if not self.client:
            return {"total_documents": 0, "total_characters": 0, "collection_name": COLLECTION_NAME}
        docs = self.list_documents()
        return {"total_documents": len(docs), "total_characters": sum(d["char_count"] for d in docs), "collection_name": COLLECTION_NAME}

    # ── 混合检索 ──

    def hybrid_search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self.client:
            raise RuntimeError("Milvus 未初始化")

        dense_vec = self._encode_dense([query])[0]
        dense_req = AnnSearchRequest(
            data=[dense_vec], anns_field="dense_vector",
            param={"metric_type": "IP", "params": {"nprobe": 16}}, limit=top_k * 2,
        )
        sparse_req = AnnSearchRequest(
            data=[query], anns_field="content_sparse",
            param={"metric_type": "BM25"}, limit=top_k * 2,
        )

        results = self.client.hybrid_search(
            collection_name=COLLECTION_NAME, reqs=[dense_req, sparse_req],
            rerank={"strategy": "rrf", "params": {"k": 60}}, limit=top_k,
            output_fields=["id", "title", "content", "source", "created_at"],
        )

        docs = []
        for hit in results[0]:
            entity = hit.get("entity", hit)
            docs.append({
                "id": entity.get("id", ""), "title": entity.get("title", ""),
                "content": entity.get("content", ""),
                "source": entity.get("source", ""), "created_at": entity.get("created_at", ""),
                "score": round(hit.get("distance", 0), 4),
            })
        print(f"[Milvus] hybrid_search: {len(docs)} results")
        return docs


_kb_instance: Optional[MilvusKB] = None


def get_kb() -> MilvusKB:
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = MilvusKB()
        _kb_instance.init()
    return _kb_instance
