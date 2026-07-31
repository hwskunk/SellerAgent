"""
SellerAgent — 启动入口

使用方法:
    python run.py

然后在浏览器中打开 http://localhost:8080
"""
import os, sys
from pathlib import Path
from dotenv import load_dotenv

# ── 必须在一开始就加载 ──
load_dotenv(Path(__file__).parent / ".env")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("MILVUS_DB_PATH", str(Path(__file__).parent / "milvus_fresh" / "seller_kb.db"))

import uvicorn

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    # ── 在 uvicorn 启动前（纯同步线程）预初始化 Milvus + 模型 ──
    # milvus-lite 底层 gRPC 在 asyncio event loop 中不稳定，
    # 必须在 fastapi startup 事件之前完成初始化
    import time as _time
    print("[run.py] 预初始化 Milvus ...")

    _t0 = _time.time()
    from src.kb.milvus_client import get_kb
    kb = get_kb()
    print(f"[run.py] Milvus 初始化完成 ({_time.time() - _t0:.1f}s)")

    # ── 同步 SQLite ↔ Milvus ──
    # 确保前端显示的文档列表与向量库实际内容一致
    print("[run.py] 校验知识库一致性 ...")
    from src.kb import doc_store
    milvus_ids = kb.list_doc_ids()
    sqlite_docs = doc_store.list_documents()
    orphan_count = 0
    for doc in sqlite_docs:
        if doc["id"] not in milvus_ids:
            print(f"  [sync] 清理孤立记录: {doc['title']} ({doc['id']})")
            # 同步清理 knowledge/ 源文件
            full_doc = doc_store.get_document(doc["id"])
            if full_doc and full_doc.get("knowledge_path"):
                kp = Path(full_doc["knowledge_path"])
                if kp.exists():
                    kp.unlink()
                    print(f"  [sync] 清理源文件: {kp}")
            doc_store.delete_document(doc["id"])
            orphan_count += 1
    if orphan_count:
        print(f"[run.py] 清理了 {orphan_count} 条孤立文档记录")
    else:
        print(f"[run.py] 知识库一致，{len(sqlite_docs)} 篇文档")

    # ── 反向同步: knowledge/ → SQLite ──
    # 清理 knowledge/ 中有但 SQLite 无对应记录的孤儿源文件。
    # 这些文件可能来自：Milvus 重建后未重新入库、旧版本的残留文件等。
    print("[run.py] 校验 knowledge/ 源文件 ...")
    knowledge_dir = Path(__file__).parent / "knowledge"
    if knowledge_dir.exists():
        known_paths = doc_store.list_knowledge_paths()
        orphan_files = 0
        for f in knowledge_dir.iterdir():
            if f.is_file():
                f_abs = str(f.resolve())
                if f_abs not in known_paths:
                    print(f"  [sync] 清理孤儿源文件: {f.name}")
                    f.unlink()
                    orphan_files += 1
        if orphan_files:
            print(f"[run.py] 清理了 {orphan_files} 个孤儿源文件")
        else:
            print(f"[run.py] knowledge/ 一致，无孤儿文件")
    else:
        os.makedirs(knowledge_dir, exist_ok=True)
        print(f"[run.py] 创建 knowledge/ 目录")

    # ── 预初始化 Docling ──
    # Docling 首次使用时会从 HuggingFace 下载布局分析/表格识别/OCR 等模型（几百 MB），
    # 在低配服务器上可能耗时数分钟。此处用一个最小文档触发模型下载和加载，
    # 避免用户上传文件时的长时间等待。
    print("[run.py] 预初始化 Docling 文档解析引擎（首次需下载模型，请耐心等待）...")
    _t1 = _time.time()
    # 用 manager 里配置好的 converter（TableFormerMode.FAST + 8线程），
    # 确保预热和实际使用是完全相同的配置
    from src.kb.manager import _get_converter
    _docling_converter = _get_converter()
    # 用一个极小的内置文档做一次完整转换，触发所有子模型加载
    import tempfile

    _warmup_path = None
    try:
        # 构造一个只有一行文字的最小 PDF，触发全部管线加载
        # 如果 reportlab 不可用则回退到纯文本文件
        try:
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4
            _tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            _warmup_path = _tmp.name
            _c = canvas.Canvas(_warmup_path, pagesize=A4)
            _c.drawString(100, 750, "Warmup")
            _c.save()
            _tmp.close()
        except ImportError:
            # reportlab 不可用，用纯文本文件
            _tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w")
            _warmup_path = _tmp.name
            _tmp.write("Warmup document for Docling initialization.")
            _tmp.close()

        _result = _docling_converter.convert(_warmup_path)
        _md = _result.document.export_to_markdown()
        print(f"[run.py] Docling 预热完成 ({_time.time() - _t1:.1f}s) → {len(_md)} chars markdown")
    except Exception as e:
        print(f"[run.py] Docling 预初始化失败（不影响启动，首次上传时会重试）: {e}")
    finally:
        if _warmup_path:
            try:
                os.unlink(_warmup_path)
            except Exception:
                pass

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
