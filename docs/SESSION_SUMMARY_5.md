# SellerAgent 会话总结 #5

> 日期: 2026-07-30 | 会话重点: 阿里云 Linux 服务器部署、Milvus Lite 兼容性修复、DashScope 云端化改造

---

## 一、部署打包

首次整理部署包 `SellerAgent-deploy.zip`（59KB），包含：

| 文件/目录 | 说明 |
|---|---|
| `app.py`, `run.py`, `start.sh` | 应用入口 + Linux 启动脚本 |
| `requirements.txt`, `.env.example` | 依赖 + 环境变量模板 |
| `README_DEPLOY.md` | 完整部署指南（含 systemd 自启） |
| `src/` | 全部源码 |
| `static/` | 前端 |
| `knowledge/` | 空目录（知识库源文件存档） |

排除：`.env`、`*.db`、`milvus_data/`、`milvus_fresh/`、测试文件、`docs/`。

---

## 二、部署踩坑记录

### 🔴 milvus-lite 3.1.1 Linux 兼容性（三连坑）

| # | 报错 | 根因 | 修复 |
|---|---|---|---|
| 1 | `Cannot send a request, as the client has been closed` | 复杂 schema（`SPARSE_FLOAT_VECTOR` + `BM25 Function` + `DataType`）在 Linux 上不兼容。AssistantAgent 用简单 API 没事 | `milvus_client.py`：删除自定义 schema、`Function`/`FunctionType` import，改为 `create_collection(dimension=1024, metric_type="IP")` |
| 2 | `DataNotMatchException: {id} field should be int64` | 简单 API 的 `id` 是 INT64 自增，代码还在传字符串 `"doc_id_c0"` | `id` 改为 `hash(doc_id) * 10000 + i`，BM25 查询过滤从 `id != ''` 改为 `id >= 0` |
| 3 | gRPC `GOAWAY too_many_pings` | milvus-lite 内部心跳过频，仅警告不影响功能 | 忽略，可正常使用 |

### 🔴 HuggingFace 网络不通

| 问题 | 修复 |
|---|---|
| `[Errno 101] Network is unreachable`，阿里云访问不了 `huggingface.co` | `start.sh` 加 `export HF_ENDPOINT=https://hf-mirror.com` |
| Docling 上传文件 401（CAS 服务需要认证） | `.env` 加 `HF_TOKEN`（Read-Only token） |

### 🔴 宝塔防火墙

阿里云安全组开了但宝塔面板自带防火墙未放行 → 宝塔安全 → 添加 8080 端口。

### 🟡 FastAPI async 事件循环稳定性

`app.py` async startup 里调 Milvus gRPC 可能不稳定 → 移到 `run.py` 同步主线程，在 uvicorn 启动前完成 Milvus 初始化。

### 🟡 CPU 阻塞事件循环

Reranker `CrossEncoder.predict()` 占用 CPU 5-7s，期间其他 HTTP 请求被阻塞 → `retrieve_node` 用 `asyncio.to_thread()` 把检索放到线程池。

---

## 三、Embedding 云端化改造

### 动机

| | 本地 BGE-M3 + Reranker | DashScope API |
|---|---|---|
| 启动时间 | ~5 分钟（加载 830MB 模型） | 秒级 |
| 检索延迟 | 7-10s（Reranker 5-7s） | ~1-2s（API + RRF） |
| 内存 | ~1.5GB | 几乎为零 |
| HuggingFace 依赖 | 模型下载 + CAS 认证 | 无 |

### 改动

[**milvus_client.py**](src/kb/milvus_client.py)：

- `_get_dense_model()` / `SentenceTransformer` → `_get_dense_embedding()` / `OpenAIEmbeddings`（DashScope `text-embedding-v3`）
- `_encode_dense()` 改为批量 ≤10 条（DashScope API 限制）
- `hybrid_search()` 跳过 Reranker，RRF 融合后直接取 top_k
- `_get_reranker()` / `_rerank()` 代码保留，可随时恢复

[**run.py**](run.py)：删除模型预加载（`_get_dense_model` + `_get_reranker`）

[**app.py**](app.py)：startup 中删除模型加载

[**.env.example**](.env.example)：`EMBEDDING_MODEL=text-embedding-v3`，Reranker 配置注释掉

### 额外修复

- DashScope embedding 单次 API 最多 10 条 → 分批循环
- 客服端首次上传时 `.env` 中 `EMBEDDING_MODEL` 仍为旧值 `BAAI/bge-m3` → 需手动改为 `text-embedding-v3`

---

## 四、前端修复

**欢迎语 HTML 标签显示为文本**：`marked.js` v5+ 默认 `html: false` → 改为 `html: true`（[app.js](static/js/app.js)）

---

## 五、修改文件清单

| 文件 | 改动 |
|---|---|
| `src/kb/milvus_client.py` | 重写：删除复杂 schema → 简单 API；embedding 切 DashScope；Reranker 保留代码但检索流程跳过；ID 整数化；批量编码 ≤10 |
| `run.py` | Milvus 初始化移到 uvicorn 前；添加计时日志；删除模型预加载 |
| `app.py` | startup 简化，删除模型加载 |
| `src/nodes/retrieve.py` | `asyncio.to_thread()` 包检索调用 |
| `start.sh` | `HF_ENDPOINT=https://hf-mirror.com` 固化 |
| `.env.example` | 新增 `HF_TOKEN`、`RERANKER_MODEL` 注释；Embedding 默认改 `text-embedding-v3` |
| `static/js/app.js` | `marked.setOptions` 加 `html: true` |
| `static/css/style.css` | 无改动 |
| `README_DEPLOY.md` | 新增部署指南 |

---

## 六、服务器运维速查

```bash
# 启动
cd /root/SellerAgent && bash start.sh

# 停止
pkill -9 python

# 清理数据（重建知识库时需要）
rm -rf /root/SellerAgent/milvus_data/

# 更新部署（覆盖文件）
unzip -o SellerAgent-deploy.zip -d /root/SellerAgent

# 清理 HuggingFace 缓存（释放 ~830MB）
rm -rf ~/.cache/huggingface/hub/models--BAAI--bge-m3/
rm -rf ~/.cache/huggingface/hub/models--BAAI--bge-reranker-base/

# 检查端口
ss -tlnp | grep 8080
```

---

## 七、当前状态

| 项目 | 值 |
|---|---|
| 启动命令 | `bash /root/SellerAgent/start.sh` |
| 端口 | 8080 |
| Python | 3.11（服务器） |
| Embedding | DashScope `text-embedding-v3` |
| LLM | qwen-plus |
| Reranker | 已禁用（代码保留，可恢复） |
| HuggingFace | 模型文件已删除，仅 HF_TOKEN 用于 Docling CAS |
| 启动时间 | 秒级 |
| 检索延迟 | ~1-2s |
