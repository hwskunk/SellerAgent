"""
SellerState — 销售智能体共享状态
"""
from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages


class SellerState(TypedDict):
    """销售智能体的共享状态，通过 LangGraph 持久化"""

    # ── 对话 ──
    messages: Annotated[list, add_messages]

    # ── 意图分类 ──
    intent: str  # "sales_inquiry" | "product_info" | "kb_management" | "general_chat"

    # ── 知识检索 ──
    retrieved_docs: list[dict]
    # 检索到的知识库文档列表
    # e.g. [{"id": "...", "content": "...", "score": 0.92, "source": "产品手册.pdf"}]

    # ── 最终回复 ──
    final_response: str
