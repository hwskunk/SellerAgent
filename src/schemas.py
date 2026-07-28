"""
Pydantic schemas — SellerAgent 结构化输出
"""
from pydantic import BaseModel, Field
from typing import Literal


# ── 意图分类 ──

class SellerIntent(BaseModel):
    """classify 节点的输出 — 用户想做什么？"""
    intent: Literal[
        "sales_inquiry",   # 销售咨询（产品、价格、库存等）
        "product_info",    # 产品信息查询
        "kb_management",   # 知识库管理相关
        "general_chat",    # 闲聊/其他
    ] = Field(description="分类后的用户意图")
    summary: str = Field(description="用户意图的一句话总结")


# ── API 请求/响应模型 ──

class ChatRequest(BaseModel):
    """聊天请求"""
    message: str = Field(description="用户消息")
    thread_id: str = Field(default="default", description="会话 ID")


class ChatResponse(BaseModel):
    """聊天响应"""
    reply: str = Field(description="智能体回复")
    intent: str = Field(default="", description="识别的意图")
    retrieved_count: int = Field(default=0, description="检索到的知识条数")
    sources: list[dict] = Field(default_factory=list, description="引用的知识来源")


class KBDocumentAdd(BaseModel):
    """添加知识库文档"""
    content: str = Field(default="", description="文本内容")
    title: str = Field(default="", description="文档标题")
    source: str = Field(default="manual", description="来源标识")


class KBDocumentDelete(BaseModel):
    """删除知识库文档"""
    doc_id: str = Field(description="要删除的文档 ID")


class KBDocumentInfo(BaseModel):
    """知识库文档信息"""
    id: str
    title: str
    content_preview: str  # 内容预览（前200字）
    source: str
    created_at: str
    char_count: int


class KBStats(BaseModel):
    """知识库统计"""
    total_documents: int
    total_characters: int
    collection_name: str
