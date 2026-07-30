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

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
