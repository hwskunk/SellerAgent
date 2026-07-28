"""
意图分类节点
使用 LLM 结构化输出判断用户意图，含关键词兜底。
"""
from langchain_core.messages import SystemMessage, HumanMessage
from src.state import SellerState
from src.schemas import SellerIntent
from src.llm import get_fast_llm
from src.prompts.templates import CLASSIFY_SYSTEM, CLASSIFY_USER


async def classify_node(state: SellerState) -> dict:
    """分类用户意图。"""
    messages = state.get("messages", [])
    if not messages:
        return {"intent": "general_chat"}

    last_msg = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])

    try:
        llm = get_fast_llm()
        structured_llm = llm.with_structured_output(SellerIntent)
        result = structured_llm.invoke([
            SystemMessage(content=CLASSIFY_SYSTEM),
            HumanMessage(content=CLASSIFY_USER.format(user_message=last_msg)),
        ])
        intent = result.intent
        print(f"[classify] intent={intent} | {result.summary}")
    except Exception as e:
        print(f"[classify] LLM classify failed ({e}), using keyword fallback")
        intent = _keyword_fallback(last_msg)

    return {"intent": intent}


def _keyword_fallback(msg: str) -> str:
    """关键词兜底分类。"""
    kw = msg.lower()

    sales_kw = ["多少钱", "价格", "怎么卖", "优惠", "折扣", "下单", "购买", "怎么买", "便宜"]
    if any(k in kw for k in sales_kw):
        return "sales_inquiry"

    product_kw = ["功能", "规格", "参数", "配置", "尺寸", "颜色", "型号", "版本", "支持"]
    if any(k in kw for k in product_kw):
        return "product_info"

    return "general_chat"
