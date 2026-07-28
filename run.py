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
os.environ.setdefault("MILVUS_DB_PATH", str(Path(__file__).parent / "milvus_v2" / "kb.db"))

import uvicorn

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
