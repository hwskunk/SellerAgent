# SellerAgent 会话总结 #8

> 日期: 2026-07-31 | 会话重点: 全功能测试、本地模型崩溃修复、knowledge/ 双向同步、代码精简

---

## 一、全功能测试

对项目所有 API 端点做了系统性测试（22 项），结果：

| 功能 | 端点 | 状态 |
|---|---|---|
| 前端页面/CSS/JS/图标 | `GET /`, `/static/*` | ✅ |
| 新建/列表/查看/重命名/删除会话 | `/api/threads` | ✅ |
| 非流式对话 | `POST /api/chat` | ✅ |
| 流式对话 (SSE) | `POST /api/chat/stream` | ✅ |
| 手动添加文本 | `POST /api/kb/docs` | ✅ |
| 空内容拦截 | `POST /api/kb/docs` | ✅ |
| SHA256 内容去重 | `POST /api/kb/docs` | ✅ |
| 文件上传（多格式） | `POST /api/kb/upload` | ✅ |
| 文件去重 | `POST /api/kb/upload` | ✅ |
| 不支持格式拦截 | `POST /api/kb/upload` | ✅ |
| 文档查看/不存在提示 | `GET /api/kb/docs/{id}` | ✅ |
| 文档删除（三层同步） | `DELETE /api/kb/docs/{id}` | ✅ |
| 知识库统计 | `GET /api/kb/stats` | ✅ |

**结论：API 层功能完整可用，无功能缺陷。**

---

## 二、发现并修复的问题

### 🔴 问题 1: BGE-M3 本地模型无法加载 → 服务启动崩溃

**症状**: 使用 `sentence-transformers` 加载 BAAI/bge-m3 时 segfault（exit code 139）。

**根因**: Python 3.10.11 的 `sre_parse` 模块缺陷 + 新版 scipy/sklearn → import 链在 `scipy/_lib/_docscrape.py` 抛出 `TypeError: 'Reader' object is not callable`。Bash 环境下通过工具启动时必现。

**修复**: `.env` 中 `EMBEDDING_MODEL` 从 `BAAI/bge-m3` 改为 `text-embedding-v3`，永久使用 DashScope 云端 Embedding。

### 🔴 问题 2: knowledge/ 目录有 8 个孤儿源文件

**症状**: `knowledge/` 目录 11 个文件，但 SQLite/Milvus 只记录了 3 篇文档。8 个文件（含 5 份 PDF 副本）是历史重建 Milvus 后的残留。

**根因**: 启动时的 SQLite↔Milvus 一致性校验只做了单向（SQLite 有但 Milvus 无 → 清理），缺少反向（knowledge/ 有但 SQLite 无 → 清理）。

**修复**: 
- `src/kb/doc_store.py` 新增 `list_knowledge_paths()` 函数，返回所有记录的 knowledge_path 集合
- `run.py` 新增反向同步：扫描 `knowledge/` → 对比 SQLite → 删除无主文件

启动日志验证：
```
[sync] 清理孤儿源文件: 带教实操案例库.pdf
[sync] 清理孤儿源文件: 带教考核标准细则.pdf
[sync] 清理孤儿源文件: ..._83d646931af1.pdf (5份副本)
[sync] 清理孤儿源文件: 达人分销话术心法.md
[run.py] 清理了 8 个孤儿源文件
```

### ✅ 问题 3: 检索多样性 → 确认为非问题

测试"带教考核标准"时 5 条结果全来自同一文档。原因不是检索算法缺陷，而是知识库中只有 3 篇文档，讲带教的仅 1 篇。跨文档查询"家长沟通+私信回复"时正常命中两份不同文档。

---

## 三、代码精简

`.env` 改为云端 Embedding 后，`milvus_client.py` 中本地模型分支（`_local_model`、`_use_local_embedding`、`SentenceTransformer` 加载）全是死代码，一并删除，净减 ~30 行。

**改前**:
```python
def _get_dense_embedding(self):
    if self._use_local_embedding:
        if self._local_model is None:
            self._local_model = SentenceTransformer(...)
        return self._local_model
    # 云端路径...
```

**改后**:
```python
def _get_dense_embedding(self):
    if self._dense_embedding is None:
        self._dense_embedding = OpenAIEmbeddings(...)
    return self._dense_embedding
```

`_encode_dense()` 同样删掉本地分支。

---

## 四、部署包

重新打包 `SellerAgent-deploy.zip`（66 KB，41 文件），排除项：

| 排除 | 原因 |
|---|---|
| `.env` | 含 API Key，服务器自带 |
| `*.db`, `milvus_fresh/` | 数据库文件，服务器自带 |
| `knowledge/` 文件 | 源文件存档，服务器自带（保留空目录） |
| `__pycache__/`, `*.pyc` | 编译缓存 |
| `test_*.py` (6 个) | 开发测试 |
| `docs/` (8 个 md) | 会话总结 |
| `*.bak` | 备份文件 |
| `.git/` | 版本控制 |

新增 `.gitignore` 规则：`SellerAgent-deploy.zip`、`*.bak`。

---

## 五、服务器 screen 部署

用户无法 SSH，通过宝塔面板操作：

```bash
# 1. 上传解压
cd /root && unzip -o SellerAgent-deploy.zip -d /root/SellerAgent

# 2. 创建 screen 会话
screen -S selleragent

# 3. 启动（start.sh 直接用 ./venv/bin/python3，无需手动激活虚拟环境）
cd /root/SellerAgent && bash start.sh

# 4. 离开 screen（服务继续运行）
Ctrl+A, D

# 5. 后续重入
screen -r selleragent
```

---

## 六、PDF 解析慢的原因

| 文件类型 | 解析方式 | 速度 |
|---|---|---|
| `.md` / `.txt` | 直接读文件 | 毫秒级 |
| `.xlsx` / `.docx` / `.pptx` | Docling 读取 XML 文本结构 | 快 |
| `.pdf` | Docling + RapidOCR 逐页识别 + 表格检测 | 慢 |

PPTX 和 DOCX/XLSX 一样是 Office Open XML 格式，文字以字符串存储在 XML 中，不需要 OCR。只有 PDF 需要渲染+OCR，慢是不可避免的。

---

## 七、修改文件清单

| 文件 | 改动 |
|---|---|
| `.env` | `EMBEDDING_MODEL=text-embedding-v3`（一行） |
| `src/kb/milvus_client.py` | 删除本地模型分支、`_local_model`、`_use_local_embedding`，净减 ~30 行 |
| `src/kb/doc_store.py` | 新增 `list_knowledge_paths()` |
| `run.py` | 新增 knowledge/ → SQLite 反向同步 |
| `.gitignore` | 新增 `SellerAgent-deploy.zip`、`*.bak` |

---

## 八、仓库状态

- **GitHub**: https://github.com/hwskunk/SellerAgent
- **分支**: `main`
- **本次 commit**: `9aca867` — `fix: cloud embedding migration + knowledge/ bidirectional sync + code cleanup`
- **推送日期**: 2026-07-31

---

## 九、运行信息

| 项目 | 值 |
|---|---|
| 启动命令 | `cd SellerAgent && bash start.sh`（服务器）/ `python run.py`（本地） |
| 端口 | 8080 |
| Embedding | DashScope `text-embedding-v3`（永久云端） |
| LLM | qwen-plus |
| 启动时间 | ~4s（含 Docling 预热） |
| 检索延迟 | ~1-2s |
| 知识库文档数 | 3 篇（78 chunks） |
