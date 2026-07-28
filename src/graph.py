"""
LangGraph 图 — SellerAgent 主图

3 节点流程: classify → retrieve → generate → END
"""
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from src.state import SellerState
from src.nodes.classify import classify_node
from src.nodes.retrieve import retrieve_node
from src.nodes.generate import generate_node


async def build_graph():
    """构建并编译 SellerAgent 图。

    Graph 结构:
        START → classify → retrieve → generate → END
    """
    checkpointer = MemorySaver()

    builder = StateGraph(SellerState)

    # ── 添加节点 ──
    builder.add_node("classify", classify_node)
    builder.add_node("retrieve", retrieve_node)
    builder.add_node("generate", generate_node)

    # ── 添加边 ──
    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    builder.add_edge("retrieve", "generate")
    builder.add_edge("generate", END)

    return builder.compile(checkpointer=checkpointer)


# ── 全局图实例 ──

_graph = None


async def get_graph():
    """获取或创建编译后的图（单例模式）。"""
    global _graph
    if _graph is None:
        _graph = await build_graph()
    return _graph
