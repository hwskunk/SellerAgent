"""
SellerAgent — FastAPI 应用

提供:
- GET  /              → 前端页面
- POST /api/chat      → 对话接口（LangGraph）
- GET  /api/kb/docs   → 列出知识库文档
- POST /api/kb/docs   → 添加知识库文档
- DELETE /api/kb/docs/{id} → 删除知识库文档
- POST /api/kb/upload → 上传文件到知识库
- GET  /api/kb/stats  → 知识库统计
"""
import os
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage

from src.state import SellerState
from src.graph import get_graph
from src.memory import (
    get_recent_messages, get_summary, add_message, maybe_summarize, clear_history,
    list_threads, get_thread_messages, rename_thread,
)
from src.schemas import (
    ChatRequest, ChatResponse,
    KBDocumentAdd, KBDocumentDelete, KBDocumentInfo, KBStats,
)
from src.kb.manager import (
    add_text_document,
    add_file_from_bytes,
    delete_document,
    get_document,
    list_documents,
    get_stats,
)

# ── 初始化 ──

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="SellerAgent - 销售智能体", version="1.0.0")

# 挂载静态文件目录（CSS、JS 等）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    """启动时编译 LangGraph（Milvus 已在 run.py 中预初始化）。"""
    from src.kb.milvus_client import get_kb
    get_kb()
    print("[SellerAgent] KB 就绪")

    await get_graph()
    print("[SellerAgent] 启动完成")


# ═══════════════════════════════════════════════════════════════
# 前端页面
# ═══════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    """返回前端页面。"""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return HTMLResponse("<h1>SellerAgent 前端页面未找到</h1>", status_code=404)


# ═══════════════════════════════════════════════════════════════
# 对话 API
# ═══════════════════════════════════════════════════════════════

@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """处理用户消息，返回智能体回复（非流式，供兼容使用）。"""
    graph = await get_graph()

    thread_id = req.thread_id or "default"
    config = {"configurable": {"thread_id": thread_id}}

    input_state: SellerState = {
        "messages": [HumanMessage(content=req.message)],
        "intent": "",
        "retrieved_docs": [],
        "final_response": "",
    }

    try:
        result = await graph.ainvoke(input_state, config)
        reply = result.get("final_response", "抱歉，我暂时无法处理您的请求。")
        intent = result.get("intent", "")
        docs = result.get("retrieved_docs", [])

        # 保存对话历史
        add_message(thread_id, "user", req.message)
        add_message(thread_id, "assistant", reply)
        maybe_summarize(thread_id)

        # 构建来源（按 title 去重）
        sources = []
        seen_titles = set()
        for doc in docs:
            key = doc.get("title", "")
            if key in seen_titles:
                continue
            seen_titles.add(key)
            sources.append({
                "title": key,
                "source": doc.get("source", ""),
                "score": doc.get("score", 0),
            })

        return ChatResponse(
            reply=reply,
            intent=intent,
            retrieved_count=len(docs),
            sources=sources,
        )
    except Exception as e:
        print(f"[API] 对话处理失败: {e}")
        return ChatResponse(
            reply=f"处理您的请求时出现错误: {str(e)}",
            intent="error",
            retrieved_count=0,
            sources=[],
        )


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """流式对话接口 — SSE，逐 token 推送，带 Summary Buffer 记忆。"""
    import json as _json
    from fastapi.responses import StreamingResponse
    from src.nodes.classify import classify_node
    from src.nodes.retrieve import retrieve_node
    from src.nodes.generate import stream_response

    thread_id = req.thread_id or "default"

    # Step 1 & 2: 意图分类 + 知识检索
    state: SellerState = {
        "messages": [HumanMessage(content=req.message)],
        "intent": "",
        "retrieved_docs": [],
        "final_response": "",
    }
    classify_result = await classify_node(state)
    intent = classify_result.get("intent", "general_chat")
    retrieve_result = await retrieve_node(state)
    docs = retrieve_result.get("retrieved_docs", [])

    # 加载记忆：总结 + 最近10轮
    summary = get_summary(thread_id)
    recent_msgs, _total = get_recent_messages(thread_id)

    # 保存用户消息
    add_message(thread_id, "user", req.message)

    # 构建来源（按 title 去重，保留第一条）
    sources = []
    seen_titles = set()
    for d in docs:
        key = d.get("title", "")
        if key in seen_titles:
            continue
        seen_titles.add(key)
        sources.append({"title": key, "source": d.get("source", ""), "score": d.get("score", 0)})

    async def generate():
        yield f"data: {_json.dumps({'type':'meta','intent':intent,'retrieved_count':len(docs),'sources':sources}, ensure_ascii=False)}\n\n"

        full_reply = ""
        try:
            async for token in stream_response(
                req.message, intent, docs,
                history=recent_msgs, summary=summary,
            ):
                full_reply += token
                yield f"data: {_json.dumps({'type':'token','content':token}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type':'error','content':str(e)}, ensure_ascii=False)}\n\n"

        if full_reply:
            add_message(thread_id, "assistant", full_reply)
            # 检查是否需要触发总结（在生成回复之后异步执行）
            maybe_summarize(thread_id)

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ═══════════════════════════════════════════════════════════════
# 会话管理 API
# ═══════════════════════════════════════════════════════════════

@app.get("/api/threads")
async def api_list_threads():
    """列出所有会话。"""
    try:
        threads = list_threads()
        return JSONResponse({"success": True, "data": threads})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/threads")
async def api_new_thread():
    """创建新会话，返回 thread_id。"""
    thread_id = uuid.uuid4().hex[:12]
    return JSONResponse({"success": True, "thread_id": thread_id})


@app.get("/api/threads/{thread_id}")
async def api_get_thread_messages(thread_id: str):
    """获取指定会话的全部消息。"""
    try:
        msgs = get_thread_messages(thread_id)
        return JSONResponse({"success": True, "data": msgs})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/threads/{thread_id}")
async def api_delete_thread(thread_id: str):
    """删除指定会话及其所有消息。"""
    try:
        clear_history(thread_id)
        return JSONResponse({"success": True, "message": f"会话 {thread_id} 已删除"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.put("/api/threads/{thread_id}/rename")
async def api_rename_thread(req: ChatRequest, thread_id: str):
    """重命名会话。请求体: {"message": "新名称"}（复用 ChatRequest.message 字段）"""
    try:
        rename_thread(thread_id, req.message)
        return JSONResponse({"success": True, "message": "已重命名"})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ═══════════════════════════════════════════════════════════════
# 知识库管理 API
# ═══════════════════════════════════════════════════════════════

@app.get("/api/kb/docs")
async def api_list_docs():
    """列出知识库中的所有文档。"""
    try:
        docs = list_documents()
        return JSONResponse({"success": True, "data": docs, "total": len(docs)})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/kb/docs")
async def api_add_doc(req: KBDocumentAdd):
    """添加文本知识文档。"""
    try:
        if not req.content.strip():
            return JSONResponse({"success": False, "error": "内容不能为空"}, status_code=400)
        title = req.title or f"文档-{uuid.uuid4().hex[:6]}"
        result = add_text_document(title=title, content=req.content, source=req.source)
        return JSONResponse({"success": True, "data": result})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/kb/docs/{doc_id}")
async def api_get_doc(doc_id: str):
    """获取单篇文档的完整内容。"""
    try:
        doc = get_document(doc_id)
        if doc is None:
            return JSONResponse({"success": False, "error": "文档不存在"}, status_code=404)
        return JSONResponse({"success": True, "data": doc})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.delete("/api/kb/docs/{doc_id}")
async def api_delete_doc(doc_id: str):
    """删除指定文档。"""
    try:
        ok = delete_document(doc_id)
        if ok:
            return JSONResponse({"success": True, "message": f"文档 {doc_id} 已删除"})
        else:
            return JSONResponse({"success": False, "error": "文档不存在"}, status_code=404)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/kb/upload")
async def api_upload_file(file: UploadFile = File(...), filename: str = Form(default="")):
    """上传文件到知识库。

    支持: .txt, .md, .pdf, .docx, .xlsx, .pptx, .html, .png, .jpg

    文件名优先使用前端显式传入的 filename 字段（避免 HTTP multipart 头编码问题），
    未传时回退到 file.filename。
    """
    try:
        file_bytes = await file.read()
        if len(file_bytes) == 0:
            return JSONResponse({"success": False, "error": "文件为空"}, status_code=400)

        name = filename.strip() or file.filename or "uploaded_file"
        name = _fix_filename_encoding(name)

        result = add_file_from_bytes(file_bytes, name)
        return JSONResponse({"success": True, "data": result})
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


def _fix_filename_encoding(filename: str) -> str:
    """修复文件名编码问题。

    某些客户端上传中文文件名时，字节可能被错误解释。
    常见场景：GBK/UTF-8 字节 → Latin-1 解码 → 乱码。
    尝试检测并恢复原始编码。
    """
    # ASCII 文件名无需处理
    try:
        filename.encode("ascii")
        return filename
    except UnicodeEncodeError:
        pass

    # 检测字符串是否含 CJK 字符（unicode 范围 0x4E00-0x9FFF）
    def has_cjk(s):
        return any(0x4E00 <= ord(c) <= 0x9FFF for c in s)

    # 尝试 Latin-1 → UTF-8
    try:
        recovered = filename.encode("latin-1").decode("utf-8")
        if has_cjk(recovered):
            return recovered
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    # 尝试 Latin-1 → GBK (Windows 中文版常见)
    try:
        recovered = filename.encode("latin-1").decode("gbk")
        if has_cjk(recovered):
            return recovered
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    return filename


@app.get("/api/kb/stats")
async def api_get_stats():
    """获取知识库统计信息。"""
    try:
        stats = get_stats()
        return JSONResponse({"success": True, "data": stats})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
