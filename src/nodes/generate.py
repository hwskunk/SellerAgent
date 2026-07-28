"""
回复生成节点
基于检索到的知识生成销售回复。
"""
from typing import AsyncIterator
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from src.state import SellerState
from src.llm import get_smart_llm
from src.prompts.templates import (
    GENERATE_SYSTEM,
    GENERATE_USER,
    FALLBACK_RESPONSE,
)


def build_prompt(
    user_message: str,
    intent: str,
    retrieved_docs: list[dict],
    history: list[dict] | None = None,
) -> str:
    """构建生成提示词（供流式和非流式共用）。"""
    if retrieved_docs:
        context_parts = []
        for i, doc in enumerate(retrieved_docs):
            context_parts.append(
                f"【参考资料 {i+1}】来源: {doc.get('source', '未知')}\n"
                f"标题: {doc.get('title', '无标题')}\n"
                f"内容: {doc['content']}\n"
            )
        retrieved_context = "\n".join(context_parts)
    else:
        retrieved_context = "（知识库中暂无相关内容）"

    # 对话历史
    history_text = ""
    if history:
        lines = []
        for m in history[-10:]:  # 最近 10 轮
            role_label = "用户" if m["role"] == "user" else "销售顾问"
            lines.append(f"{role_label}: {m['content']}")
        history_text = "\n".join(lines)

    return GENERATE_USER.format(
        user_message=user_message,
        intent=intent,
        retrieved_context=retrieved_context,
        history_text=history_text,
    )


async def stream_response(
    user_message: str,
    intent: str,
    retrieved_docs: list[dict],
    history: list[dict] | None = None,
    temperature: float = 0.3,
) -> AsyncIterator[str]:
    """流式生成销售回复，逐 token 产出。"""
    llm = get_smart_llm()
    llm.temperature = temperature
    user_prompt = build_prompt(user_message, intent, retrieved_docs, history)

    async for chunk in llm.astream([
        SystemMessage(content=GENERATE_SYSTEM),
        HumanMessage(content=user_prompt),
    ]):
        if chunk.content:
            yield chunk.content


async def generate_node(state: SellerState) -> dict:
    """生成最终回复（非流式，供 LangGraph 内部使用）。"""
    messages = state.get("messages", [])
    retrieved_docs = state.get("retrieved_docs", [])
    intent = state.get("intent", "general_chat")

    if not messages:
        return {"final_response": "您好！请问有什么可以帮助您的？"}

    last_msg = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])

    try:
        llm = get_smart_llm()
        user_prompt = build_prompt(last_msg, intent, retrieved_docs)
        response = llm.invoke([
            SystemMessage(content=GENERATE_SYSTEM),
            HumanMessage(content=user_prompt),
        ])
        reply = response.content
        print(f"[generate] Reply generated ({len(reply)} chars)")
    except Exception as e:
        print(f"[generate] Generation failed: {e}")
        reply = FALLBACK_RESPONSE

    # 构建来源信息
    sources = []
    for doc in retrieved_docs:
        sources.append({
            "title": doc.get("title", ""),
            "source": doc.get("source", ""),
            "score": doc.get("score", 0),
        })

    return {
        "final_response": reply,
        "messages": [AIMessage(content=reply)],
    }
