"""
微信客服消息处理 — 企业微信「微信客服」官方通道（外部客户在微信里咨询）

流程（回调通知 + 主动拉取）：
  1. 客户（任何微信用户）扫码进入客服会话，在微信里发消息
  2. 企微推送加密 XML 回调（Event=kf_msg_or_event，含 Token + OpenKfId，不含正文）
  3. 我们解密后，用回调里的 Token 调 kf/sync_msg 主动拉取消息内容
  4. 过滤 origin=3（微信客户发送）的文本消息
  5. 每条消息走 RAG 管线 → kf/send_msg 回复

官方限制：
  - 仅可在客户最后一条消息后 48 小时内回复，且最多回复 5 条
  - 回调 Token 10 分钟内有效
  - sync_msg 只能拉最近 3 天消息（用 cursor 增量拉取 + msgid 去重）

记忆隔离：每个外部客户 = 一个 thread（`wecom_kf_{external_userid}`），复用现有 Summary Buffer。
RAG 管线（检索/LLM/记忆/Prompt）全部复用现有代码，零改动。
"""
import asyncio
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ── 微信客服配置 ──

CORP_ID = os.environ.get("WECOM_CORP_ID", "")
# 新规范：微信客服 API 需通过自建应用调用，优先用自建应用 Secret；未配置时回退客服 Secret
APP_SECRET = os.environ.get("WECOM_APP_SECRET", "")
KF_SECRET = os.environ.get("WECOM_KF_SECRET", "")
TOKEN = os.environ.get("WECOM_TOKEN", "")               # 回调 Token（与后台配置一致）
ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")  # 回调密钥
OPEN_KFID = os.environ.get("WECOM_OPEN_KFID", "")       # 客服账号 ID（可选，用于启动校验）

BASE_URL = "https://qyapi.weixin.qq.com/cgi-bin"

# ── access_token 缓存（2 小时有效，自动刷新）──

_token: str = ""
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()

# ── sync_msg 游标持久化（按 open_kfid，进程内）──
# 重启后首次 cursor 为空 → 从 3 天内最早拉取，配合 msgid 去重避免重复回复
_cursors: dict[str, str] = {}

# ── 已处理消息去重 ──
_seen_msgids: set[str] = set()
_SEEN_MAX = 5000


def get_crypto():
    """获取企微加解密器（复用 crypto.py，回调加密机制一致）。"""
    from src.wecom.crypto import WeComCrypto

    missing = []
    if not CORP_ID:
        missing.append("WECOM_CORP_ID")
    if not TOKEN:
        missing.append("WECOM_TOKEN")
    if not ENCODING_AES_KEY:
        missing.append("WECOM_ENCODING_AES_KEY")
    if missing:
        raise RuntimeError(
            f"微信客服配置缺失: {', '.join(missing)}。"
            f"请检查 .env 文件（配置项后面不要写行内注释）。"
        )
    if len(ENCODING_AES_KEY) != 43:
        raise RuntimeError(
            f"WECOM_ENCODING_AES_KEY 长度必须为 43 位（当前 {len(ENCODING_AES_KEY)}），"
            f"请检查 .env 中是否误填了其他内容"
        )
    return WeComCrypto(TOKEN, ENCODING_AES_KEY, CORP_ID)


async def get_access_token() -> str:
    """获取企微 access_token（优先自建应用 Secret，兼容旧客服 Secret）。"""
    global _token, _token_expires_at
    if _token and time.time() < _token_expires_at - 60:
        return _token
    secret = APP_SECRET or KF_SECRET
    if not secret:
        raise RuntimeError(
            "缺少 Secret: 请配置 WECOM_APP_SECRET（新规范，推荐）或 WECOM_KF_SECRET"
        )
    async with _token_lock:
        if _token and time.time() < _token_expires_at - 60:
            return _token
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{BASE_URL}/gettoken",
                params={"corpid": CORP_ID, "corpsecret": secret},
            )
            data = resp.json()
            if data.get("errcode", 0) != 0:
                raise RuntimeError(f"获取 access_token 失败: {data}")
            _token = data["access_token"]
            _token_expires_at = time.time() + int(data.get("expires_in", 7200))
            print(f"[KF] access_token 已刷新（Secret 来源: {'APP' if APP_SECRET else 'KF'}）")
            return _token


# ── 回调事件解析 ──

def _text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def parse_callback(xml_text: str) -> dict:
    """解析微信客服回调事件 XML → dict（Event/Token/OpenKfId）。"""
    root = ET.fromstring(xml_text)
    return {
        "event": _text(root, "Event"),
        "token": _text(root, "Token"),
        "open_kfid": _text(root, "OpenKfId"),
        "to_user": _text(root, "ToUserName"),
        "create_time": _text(root, "CreateTime"),
    }


# ── 消息拉取（sync_msg）──

async def sync_msg(open_kfid: str, token: str = "") -> dict:
    """调用 kf/sync_msg 拉取指定客服账号的新消息。

    用回调里的 Token 校验本次拉取，游标从 _cursors 取增量。
    """
    access_token = await get_access_token()
    body = {"cursor": _cursors.get(open_kfid, ""), "limit": 1000, "open_kfid": open_kfid}
    if token:
        body["token"] = token
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{BASE_URL}/kf/sync_msg?access_token={access_token}",
            json=body,
        )
        return resp.json()


# ── 发送回复（send_msg）──

async def send_text_to_kf(external_userid: str, open_kfid: str, content: str) -> dict:
    """调用 kf/send_msg 给客户发送文本回复。"""
    access_token = await get_access_token()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{BASE_URL}/kf/send_msg?access_token={access_token}",
            json={
                "touser": external_userid,
                "open_kfid": open_kfid,
                "msgtype": "text",
                "text": {"content": content},
            },
        )
        return resp.json()


# ── 回调入口 ──

def handle_callback(xml_text: str) -> str:
    """处理微信客服回调事件。

    方式B（异步）：立即返回空字符串（200 OK），实际拉取+RAG 在后台协程完成。
    """
    try:
        evt = parse_callback(xml_text)
    except ET.ParseError as e:
        print(f"[KF] XML 解析失败: {e}")
        return ""

    if evt["event"] != "kf_msg_or_event" or not evt["open_kfid"]:
        print(f"[KF] 忽略非 kf_msg_or_event 事件: {evt['event']}")
        return ""

    print(f"[KF] 收到回调: open_kfid={evt['open_kfid']}")
    asyncio.create_task(_process_kf_messages(evt["open_kfid"], evt["token"]))
    return ""


async def _process_kf_messages(open_kfid: str, token: str):
    """后台：拉取该客服账号新消息 → 过滤客户文本消息 → 逐条 RAG 回复。"""
    try:
        data = await sync_msg(open_kfid, token)
        if data.get("errcode", 0) != 0:
            print(f"[KF] sync_msg 失败: {data}")
            return

        # 保存增量游标
        if data.get("next_cursor"):
            _cursors[open_kfid] = data["next_cursor"]

        msgs = data.get("msg_list", [])
        if not msgs:
            print(f"[KF] 无新消息（open_kfid={open_kfid}）")
            return

        for m in msgs:
            # 只处理微信客户（origin=3）发送的文本消息；系统事件(4)/接待人员(5)跳过
            if m.get("origin") != 3 or m.get("msgtype") != "text":
                continue
            external_userid = m.get("external_userid", "")
            content = ((m.get("text") or {}).get("content") or "").strip()
            if not external_userid or not content:
                continue

            # msgid 去重，防止重复回复
            msgid = m.get("msgid", "")
            if msgid:
                if msgid in _seen_msgids:
                    continue
                _seen_msgids.add(msgid)
                if len(_seen_msgids) > _SEEN_MAX:
                    _seen_msgids.clear()

            print(f"[KF] 客户消息: {external_userid[:12]} → {content[:40]}")
            await _reply_to_customer(open_kfid, external_userid, content)
    except Exception as e:
        print(f"[KF] 拉取/处理异常: {type(e).__name__}: {e}")


async def _reply_to_customer(open_kfid: str, external_userid: str, user_msg: str):
    """单个客户：记忆 → RAG 检索 → LLM 生成 → 保存 → send_msg 回复。"""
    # 每个外部客户独立记忆
    thread_id = f"wecom_kf_{external_userid}"
    try:
        from langchain_core.messages import SystemMessage, HumanMessage

        from src.llm import get_smart_llm
        from src.kb.manager import search_knowledge
        from src.memory import (
            add_message, get_summary, get_recent_messages, maybe_summarize,
        )
        from src.prompts.templates import GENERATE_SYSTEM

        # 1. 加载记忆（该客户总结 + 最近 10 轮）
        summary = get_summary(thread_id)
        recent_msgs, _total = get_recent_messages(thread_id)
        add_message(thread_id, "user", user_msg)

        # 2. 知识检索（线程池避免阻塞事件循环）
        docs = await asyncio.to_thread(search_knowledge, query=user_msg, top_k=5)

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

        # 4. LLM 生成（线程池避免阻塞）
        llm = get_smart_llm()
        response = await asyncio.to_thread(
            llm.invoke,
            [SystemMessage(content=GENERATE_SYSTEM), HumanMessage(content=user_prompt)],
        )
        reply = (response.content or "").strip()

        # 5. 保存 AI 回复 + 触发总结
        add_message(thread_id, "assistant", reply)
        await asyncio.to_thread(maybe_summarize, thread_id)

        # 6. 发送回复给客户
        result = await send_text_to_kf(external_userid, open_kfid, reply)
        if result.get("errcode", 0) == 0:
            print(f"[KF] 回复成功: {external_userid[:12]}")
        else:
            print(f"[KF] 回复发送失败: {result}")

    except Exception as e:
        print(f"[KF] RAG/回复异常: {type(e).__name__}: {e}")
