"""
知识检索节点
使用 Milvus 混合检索从知识库中查找相关内容。
"""
import asyncio
from src.state import SellerState
from src.kb.manager import search_knowledge


async def retrieve_node(state: SellerState) -> dict:
    """从知识库检索与用户问题相关的内容。"""
    messages = state.get("messages", [])
    if not messages:
        return {"retrieved_docs": []}

    last_msg = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])

    try:
        # 放到线程池执行，Reranker 的 CPU 密集型计算不会阻塞事件循环
        docs = await asyncio.to_thread(search_knowledge, query=last_msg, top_k=5)
        print(f"[retrieve] Retrieved {len(docs)} docs")
        for i, doc in enumerate(docs):
            print(f"  [{i+1}] score={doc['score']:.4f} | {doc['title'][:40]} | {doc['content'][:60]}...")
    except Exception as e:
        print(f"[retrieve] Search failed: {e}")
        docs = []

    return {"retrieved_docs": docs}
