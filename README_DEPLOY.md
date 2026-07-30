# SellerAgent — 部署指南

## 一、环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux (Ubuntu 20.04+ / CentOS 7+) |
| Python | 3.10+ |
| 内存 | ≥ 2GB |
| 磁盘 | ≥ 2GB 空闲 |
| 网络 | 需访问 dashscope.aliyuncs.com（阿里云灵积 API） |

## 二、快速部署

```bash
# 1. 上传解压
unzip SellerAgent-deploy.zip -d /root/SellerAgent

# 2. 配置 API Key
cd /root/SellerAgent
cp .env.example .env
nano .env   # 填入 DASHSCOPE_API_KEY 和 HF_TOKEN

# 3. 启动
bash start.sh
```

## 三、环境变量说明 (.env)

| 变量 | 必填 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | ✅ 是 | 阿里云灵积 API Key |
| `LLM_MODEL_NAME` | 否 | LLM 模型，默认 qwen-plus |
| `EMBEDDING_MODEL` | 否 | Embedding 模型，默认 text-embedding-v3 |
| `HF_TOKEN` | 建议 | HuggingFace Read Token（文档解析需要） |
| `MILVUS_DB_PATH` | 否 | 向量库路径，默认 ./milvus_fresh/seller_kb.db |

## 四、运维命令

```bash
# 启动
cd /root/SellerAgent && bash start.sh

# 停止
pkill -f "app:app"

# 重启
pkill -f "app:app"; sleep 1; bash start.sh

# 查看日志（如有）
tail -f /var/log/selleragent.log

# 清理数据（重建知识库）
rm -rf /root/SellerAgent/milvus_fresh/

# 更新部署（覆盖文件，保留 .env 和数据）
unzip -o SellerAgent-deploy.zip -d /root/SellerAgent
# 注意：如需保留 .env，先备份: cp .env .env.bak

# 检查端口
ss -tlnp | grep 8080
```

## 五、宝塔面板注意事项

- 阿里云安全组 + **宝塔防火墙** 都要放行 8080 端口
- 宝塔 → 安全 → 添加端口规则 → 8080

## 六、systemd 自启动（可选）

```bash
cat > /etc/systemd/system/selleragent.service << 'EOF'
[Unit]
Description=SellerAgent Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/SellerAgent
ExecStart=/root/SellerAgent/venv/bin/python3 /root/SellerAgent/run.py
Restart=always
RestartSec=5
Environment="CUDA_VISIBLE_DEVICES="
Environment="HF_ENDPOINT=https://hf-mirror.com"

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable selleragent
systemctl start selleragent
```

## 七、更新日志

- 2026-07-30: 新增手机端适配（抽屉式布局、汉堡菜单）
- 2026-07-30: Embedding 云端化（DashScope API）、部署流程固化
