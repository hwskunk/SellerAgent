#!/bin/bash
# ==========================================
# SellerAgent — Linux 启动脚本
# 使用前请先手动 Ctrl+C 关闭旧进程
# 使用方法: bash start.sh
# ==========================================

cd "$(dirname "$0")"

export HF_ENDPOINT=https://hf-mirror.com
export CUDA_VISIBLE_DEVICES=""
export PYTHONIOENCODING=utf-8
export MILVUS_DB_PATH="$(pwd)/milvus_fresh/seller_kb.db"

if [ ! -f .env ]; then
    echo "错误: .env 文件不存在，请先配置"
    exit 1
fi

if [ ! -f "venv/bin/python3" ]; then
    echo "错误: 找不到 venv/bin/python3"
    exit 1
fi

./venv/bin/python3 run.py
