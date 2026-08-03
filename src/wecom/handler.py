"""
企业微信消息处理 — 异步主动回复（方式B）

流程:
    收到消息 → 解密(在 app.py 完成) → 解析 XML → RAG 管线 → 调企微 API 主动发送

- 每个企微用户 = 一个独立 thread（记忆隔离）
- 群聊 = 共享一个 thread（`wecom_group_{chat_id}`，群成员共用上下文）
- 记忆复用现有 Summary Buffer（memory.py），零改动
- 发送采用方式B：回调先返回 200，后台异步处理完再调 API 主动发消息
"""
import asyncio
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ── 企微配置 ──

CORP_ID = os.environ.get("WECOM_CORP_ID", "")
APP_SECRET = os.environ.get("WECOM_APP_SECRET", "")
APP_ID = int(os.environ.get("WECOM_APP_ID", "0") or 0)
TOKEN = os.environ.get("WECOM_TOKEN", "")
ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"


# ── access_token 缓存（有效期 2 小时，自动刷新）──

_token: str = ""
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


def get_crypto():
    """获取企微加解密器（懒加载，避免启动时依赖未就绪）。"""
    from src.wecom.crypto import WeComCrypto

    if not (TOKEN and ENCODING_AES_KEY and CORP_ID):
        raise RuntimeError("企微配置缺失: 请检查 .env 中的 WECOM_TOKEN / WECOM_ENCODING_AES_KEY / WECOM_CORP_ID")
    return WeComCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID)


async def get_access_token() -> str:
    """获取企微 access_token（缓存 + 过期自动刷新，提前 60s 留余量）。"""
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at - 60:
        return _token
    async with _token_lock:
        if _token and time.time() < _token_expires_at - 60:
            return _token
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/gettoken",
                params={"corpid": CORP_ID, "corpsecret": APP_SECRET},
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                raise RuntimeError(f"获取 access_token 失败: {data}")
            _token = data["access_token"]
            _token_expires_at = time.time() + int(data.get("expires_in", 7200))
            print(f"[WeCom] access_token 已刷新（有效期 {data.get('expires_in', 7200)}s）")
            return _token


# ── 消息解析 ──

def parse_message(xml_text: str) -> dict:
    """解析企微回调消息 XML → 结构化 dict。

    单聊 XML 无 ChatId；群聊 XML 含 <ChatId> 元素。
    """
    root = ET.fromstring(xml_text)

    def _text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    return {
        "to_user": _text("ToUserName"),
        "from_user": _text("FromUserName"),
        "create_time": _text("CreateTime"),
        "msg_type": _text("MsgType"),
        "content": _text("Content"),
        "msg_id": _text("MsgId"),
        "agent_id": _text("AgentID"),
        "chat_id": _text("ChatId"),  # 群聊时存在，单聊为空
    }


# ── 发送回复（方式B：主动调用 API）──

async def send_text_to_user(user_id: str, content: str) -> dict:
    """单聊回复（message/send 接口）。"""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{BASE_URL}/message/send?access_token={token}",
            json={
                "touser": user_id,
                "msgtype": "text",
                "agentid": APP_ID,
                "text": {"content": content},
                "safe": 0,
            },
        )
        return resp.json()


async def send_text_to_chat(chat_id: str, content: str) -> dict:
    """群聊回复（appchat/send 接口）。"""
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{BASE_URL}/appchat/send?access_token={token}",
            json={
                "chatid": chat_id,
                "msgtype": "text",
                "text": {"content": content},
                "safe": 0,
            },
        )
        return resp.json()


# ── 消息处理入口 ──

def handle_message(xml_text: str) -> str:
    """处理一条收到的消息。

    方式B：仅启动后台任务立即返回空字符串（200 OK），
    实际 RAG 处理在 _process_and_reply 后台协程中完成。

    Returns:
        空字符串（表示不需要被动回复）
    """
    try:
        msg = parse_message(xml_text)
    except ET.ParseError as e:
        print(f"[WeCom] XML 解析失败: {e}")
        return ""

    # 只处理文本消息（@机器人触发的）
    if msg["msg_type"] != "text" or not msg["content"]:
        print(f"[WeCom] 忽略非文本消息: {msg['msg_type']}")
        return ""

    # 确定 thread_id: 群聊共享 / 单聊独立
    if msg["chat_id"]:
        thread_id = f"wecom_group_{msg['chat_id']}"
    else:
        thread_id = f"wecom_{msg['from_user']}"

    print(f"[WeCom] 收到消息: thread={thread_id} from={msg['from_user']} "
          f"content={msg['content'][:50]}")

    # 启动后台任务（挂到事件循环上，回调立即返回）
    asyncio.create_task(_process_and_reply(thread_id, msg))
    return ""


async def _process_and_reply(thread_id: str, msg: dict):
    """后台处理：记忆 → RAG 检索 → LLM 生成 → 保存 → 发送到企微。"""
    user_msg = msg["content"]
    try:
        from langchain_core.messages import SystemMessage, HumanMessage

        from src.llm import get_smart_llm
        from src.kb.manager import search_knowledge
        from src.memory import (
            add_message, get_summary, get_recent_messages, maybe_summarize,
        )
        from src.prompts.templates import GENERATE_SYSTEM

        # 1. 加载记忆（该 thread 的总结 + 最近 10 轮）
        summary = get_summary(thread_id)
        recent_msgs, _total = get_recent_messages(thread_id)
        add_message(thread_id, "user", user_msg)

        # 2. 知识检索（放到线程池，避免阻塞事件循环）
        docs = await asyncio.to_thread(search_knowledge, query=user_msg, top_k=5)
        print(f"[WeCom] 检索到 {len(docs)} 条结果")

        # 3. 构建 prompt（与网页端 generate.py 的 build_prompt 逻辑一致）
        if docs:
            context_parts = []
            for i, doc in enumerate(docs):
                context_parts.append(
                    f"【参考资料 {i+1}】来源: {doc.get('source', '未知')}\n"
                    f"标题: {doc.get('title', '无标题')}\n"
                    f"内容: {doc['content']}\n"
                )
            retrieved_context = "\n".join(context_parts)
        else:
            retrieved_context = "（知识库中暂无相关内容）"

        summary_text = f"【之前的对话总结】\n{summary}\n\n" if summary else ""
        history_text = ""
        if recent_msgs:
            lines = []
            for m in recent_msgs:
                role_label = "用户" if m["role"] == "user" else "销售顾问"
                lines.append(f"{role_label}: {m['content']}")
            history_text = "\n".join(lines) + "\n"

        user_prompt = (
            f"{summary_text}{history_text}\n"
            f"用户当前消息：{user_msg}\n\n"
            f"知识库检索结果：\n{retrieved_context}\n\n"
            f"请根据以上信息生成回复。如果检索结果为空或无相关内容，请如实告知用户并尝试提供一般性建议。"
        )

        # 4. LLM 生成（同步调用，放在线程池避免阻塞）
        llm = get_smart_llm()
        response = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=GENERATE_SYSTEM), HumanMessage(content=user_prompt)],
        )
        reply = (response.content or "").strip()
        print(f"[WeCom] 回复生成完成: {len(reply)} chars")

        # 5. 保存 AI 回复 + 触发总结检查
        add_message(thread_id, "assistant", reply)
        await asyncio.to_thread(maybe_summarize, thread_id)

        # 6. 发送到企微
        if msg["chat_id"]:
            result = await send_text_to_chat(msg["chat_id"], reply)
        else:
            result = await send_text_to_user(msg["from_user"], reply)

        if result.get("errcode", 0) == 0:
            print(f"[WeCom] 回复发送成功: {msg['from_user']}")
        else:
            print(f"[WeCom] 回复发送失败: {result}")

    except Exception as e:
        print(f"[WeCom] 处理消息失败: {type(e).__name__}: {e}")
        # 尽量通知用户出错
        try:
            err_msg = "抱歉，处理您的问题时出现错误，请稍后再试。"
            if msg["chat_id"]:
                await send_text_to_chat(msg["chat_id"], err_msg)
            else:
                await send_text_to_user(msg["from_user"], err_msg)
        except Exception:
            pass
