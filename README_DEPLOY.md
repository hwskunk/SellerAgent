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

### 微信客服接入（企业微信「微信客服」官方通道）

外部客户在**微信**里扫码进入客服会话 → 发消息 → 机器人自动回复。需企业完成微信认证。

| 变量 | 必填 | 说明 |
|---|---|---|
| `WECOM_CORP_ID` | 条件 | 企业 ID（微信客服方案需要） |
| `WECOM_KF_SECRET` | 条件 | 微信客服 Secret（API 管理里获取，非自建应用 Secret） |
| `WECOM_TOKEN` | 条件 | 回调 Token（API 管理里填写） |
| `WECOM_ENCODING_AES_KEY` | 条件 | 回调密钥（43 位，API 管理里生成） |
| `WECOM_OPEN_KFID` | 否 | 客服账号 ID（可选，用于启动校验） |

> ⚠️ 配置项**不要写行内注释**（`KEY=   # 注释` 会被 dotenv 误读为值），注释请单独成行。

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

## 七、微信客服配置步骤（外部客户在微信里咨询）

```bash
# 1. 企业微信管理后台 → 客户联系 → 微信客服 → 创建客服账号
#    （需企业完成微信认证）
# 2. 客服账号 → API 管理：
#    - 设置回调 URL: https://你的域名/api/wecom/callback
#    - 填写 Token、生成 EncodingAESKey
#    - 获取微信客服 Secret（WECOM_KF_SECRET）
# 3. 把以上值填入 .env（见上方变量说明）
# 4. 客服账号生成二维码 → 客户扫码即可在微信里咨询
# 5. 机器人自动回复，每个客户独立记忆（thread: wecom_kf_{external_userid}）
```

工作原理：客户微信发消息 → 企微推送 `kf_msg_or_event` 回调 → 服务器调 `kf/sync_msg` 拉取消息 → RAG 检索+LLM 生成 → `kf/send_msg` 回复。

## 八、更新日志

- 2026-08-03: 新增企业微信「微信客服」接入（外部客户微信咨询自动回复）
- 2026-07-30: 新增手机端适配（抽屉式布局、汉堡菜单）
- 2026-07-30: Embedding 云端化（DashScope API）、部署流程固化
