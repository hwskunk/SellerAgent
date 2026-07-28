"""
会话记忆管理 — 简单的线程级对话历史存储

每个 thread_id 保留最近 N 轮对话历史。
流式和非流式端点共享同一存储，实现跨请求的上下文记忆。
"""
from collections import defaultdict, deque
from typing import Any

MAX_HISTORY = 20  # 每个会话保留最近 20 条消息

_store: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=MAX_HISTORY))


def add_message(thread_id: str, role: str, content: str):
    """追加一条消息到会话历史。"""
    _store[thread_id].append({"role": role, "content": content})


def get_history(thread_id: str, limit: int = 20) -> list[dict[str, str]]:
    """获取指定会话的最近 N 条历史消息。"""
    items = list(_store[thread_id])
    if limit and limit > 0:
        items = items[-limit:]
    return [{"role": m["role"], "content": m["content"]} for m in items]


def clear_history(thread_id: str):
    """清除指定会话的历史。"""
    _store.pop(thread_id, None)
