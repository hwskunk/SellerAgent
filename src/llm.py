"""
LLM 配置 — SellerAgent
参考: ConversationalbuildingAgent/builder_agent/src/llm.py
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

# ── 加载 .env ──
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_PATH)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
if not DASHSCOPE_API_KEY:
    raise RuntimeError(
        "DASHSCOPE_API_KEY 未设置！请在 SellerAgent/.env 文件中配置。\n"
        "参考 .env.example 文件，或设置环境变量 DASHSCOPE_API_KEY。\n"
        f"期望的 .env 路径: {ENV_PATH}"
    )

LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "qwen-plus")


def get_llm(temperature: float = 0.3, model: str = LLM_MODEL_NAME, max_tokens: int = 2048):
    """获取 ChatOpenAI 实例（通过 DashScope 调用 Qwen 模型）。"""
    return ChatOpenAI(
        model=model,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key=DASHSCOPE_API_KEY,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def get_fast_llm():
    """快速/廉价 LLM — 用于意图分类等简单任务。"""
    return get_llm(temperature=0, model="qwen-turbo", max_tokens=256)


def get_smart_llm():
    """智能 LLM — 用于回复生成。"""
    return get_llm(temperature=0.3, model="qwen-plus", max_tokens=2048)
