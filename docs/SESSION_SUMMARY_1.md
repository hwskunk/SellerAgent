# SellerAgent 会话总结 #1

> 日期: 2026-07-28 | 端口: 8080 | 环境: rag_env | 虚拟环境路径: `C:\Users\15613\Desktop\LangchainRAGtrain\rag_env\`

---

## 一、项目总览

```
SellerAgent/
├── app.py                    # FastAPI 应用
├── run.py                    # 启动入口
├── requirements.txt
├── src/
│   ├── state.py              # SellerState
│   ├── llm.py                # Qwen via DashScope
│   ├── schemas.py            # Pydantic 模型
│   ├── memory.py             # SQLite 会话记忆 + Summary Buffer
│   ├── graph.py              # LangGraph: classify → retrieve → generate
│   ├── kb/
│   │   ├── milvus_client.py  # Milvus 混合检索 (BGE-M3 + BM25 + RRF)
│   │   ├── manager.py        # Docling 解析 + 文本分块 + CRUD
│   │   └── doc_store.py      # SQLite 文档元数据表
│   ├── nodes/
│   │   ├── classify.py       # 意图分类
│   │   ├── retrieve.py       # 知识检索
│   │   └── generate.py       # LLM 回复生成（流式 + 非流式）
│   └── prompts/templates.py  # Prompt 模板
├── static/
│   ├── index.html            # 前端（三栏布局: 会话列表/对话/知识库）
│   ├── css/style.css
│   ├── js/app.js
│   └── icons/*.svg           # 16个SVG图标
├── test_conversation.md      # 测试对话脚本
└── seed_messages.py          # 快速填充32条消息
```

---

## 二、已实现功能

### 核心技术栈
| 组件 | 方案 | 说明 |
|---|---|---|
| LLM | Qwen-plus via DashScope | OpenAI 兼容 API |
| Embedding | BGE-M3 (1024d) | sentence-transformers 本地加载，CPU 运行 |
| 向量数据库 | Milvus Lite | 原生 hybrid_search + RRF |
| 文档解析 | Docling | 统一处理 PDF/DOCX/XLSX/PPTX/HTML/图片 |
| 文本分块 | 自定义递归分块器 | Markdown 感知 + 表格保护 + 表头复制 |
| 会话记忆 | SQLite + Summary Buffer | 最近10轮完整保留，超出部分自动蒸馏 |
| 前端 | 原生 HTML/CSS/JS | 三栏布局 + 流式 SSE + Markdown 渲染 (marked.js) |

### 前端功能
- 对话：流式输出 + Markdown 渲染 + 来源引用（片段号+行号）
- 会话管理：新建/切换/删除会话，切换时加载历史消息
- 知识库：文本输入 + 文件上传（支持多格式）+ 删除（乐观UI）
- 文档查看：点击文档卡片 → 弹窗显示 Markdown 渲染全文

### 后端功能
- `POST /api/chat/stream` — SSE 流式对话
- `POST /api/chat` — 非流式对话（兼容）
- `GET/POST/DELETE /api/kb/docs` — 知识库 CRUD
- `POST /api/kb/upload` — 文件上传
- `GET/POST/DELETE /api/threads` — 会话管理
- `GET /api/kb/stats` — 统计

### 记忆系统
- SQLite `messages` 表 + `summaries` 表
- ≥20条消息（10轮）后触发总结检查
- 每新增10条（5轮）触发一次蒸馏
- 旧总结 + 新对话 → LLM 重写 → 一份精炼总结
- Prompt 拼接顺序: system prompt → 总结 → 最近10轮对话 → 用户输入

### 分块策略
- 500字/块，50字重叠
- Markdown 标题前强制段落分隔
- 表格整体提取保护，大表格每块复制表头
- 单行长文本按句号插入换行（行号追踪有意义）
- 入库时每个 chunk 拼 metadata 头: `[文档: XXX | 片段 3/8 | 第15-22行]`

---

## 三、已知 Bug / 遗留问题

### 🔴🔴 致命 — 项目当前不可用

1. **RAG 检索准确度极低，项目根本使用不了**
   - 现象：问文档 A 相关的问题，返回的全是文档 B 的 chunk
   - 例如：知识库存入"说客英语销售话术"和"私信回复话术库"，问"说客英语隶属于哪里"（应命中话术库），结果 5 条全来自无关文档，并且无法准确回答
   - 根因分析（猜测）：
     - Windows milvus-lite 无法创建 sparse 索引（`os.rename` bug），BM25 关键词检索退化
     - 纯稠密语义检索下，两篇中文销售文档的向量非常接近，RRF 分不出优劣
     - 同文档的多个 chunk 分差极小，容易一个文档占满 top5
     - BGE-M3 对中文销售话术类文本的区分度本身有限
   - 结论：**检索是 RAG 系统的核心，检索不行整个项目没有意义。必须在换 Linux 环境或升级 milvus-lite 后重新验证**

### 🔴 严重

2. **稀疏索引无法创建（Windows milvus-lite bug）**
   - 症状：`create_index` 时 `FileExistsError: manifest.json.tmp → manifest.json`
   - 影响：`content_sparse` 没有索引，BM25 关键词检索退化，等于混合检索只剩一条腿
   - 尝试过的方案：
     - ✅ 同文档限制 2 个 chunk → 治标不治本，已回退
     - ❌ 建立 sparse 索引 → Windows 下必然失败
   - 彻底解决：**换 Linux 服务器**或等 milvus-lite 修复 Windows 支持

3. **8080 端口僵尸进程残留**
   - 旧 Python 进程（PID 31424/53940/40740 等）杀不掉
   - 需要手动 `taskkill //F //PID xxx` 清理
   - 已换到 8080 端口运行

### 🟡 中等

3. **检索多样性不足**
   - `hybrid_search` 同文档限制 2 个 chunk 是折中方案
   - 理想做法：稀疏索引建好后自然解决
   - 当前代码在 `milvus_client.py:183`（`doc_counts` 逻辑）

4. **上传文件时进行对话会卡顿**
   - 两个操作共享 BGE-M3 模型，CPU 抢资源
   - 不是 bug，是资源瓶颈，不会丢数据

5. **knowledge 文件夹已删除**
   - 推 GitHub 时误删，三个源文件丢失：
     - 家长沟通技巧话术参考.xlsx
     - 带教考核标准细则.pdf
     - 2607.22528v1.pdf
   - 需要重新放入 `knowledge/` 文件夹才能用上传功能测试

### 🟢 轻微

6. **文件名编码乱码**
   - curl/PowerShell 上传中文文件名时偶尔出现
   - 前端页面上传正常，不影响使用

7. **启动命令**
```bash
cd C:\Users\15613\Desktop\LangchainRAGtrain\SellerAgent
CUDA_VISIBLE_DEVICES="" ..\rag_env\Scripts\python.exe run.py
```

---

## 四、数据结构

### Milvus Collection Schema (seller_knowledge)
| 字段 | 类型 | 说明 |
|---|---|---|
| id | VARCHAR(64) PK | chunk ID，格式 `{doc_id}_c{index}` |
| parent_doc_id | VARCHAR(64) | 所属文档 ID |
| title | VARCHAR(512) | 文档标题 |
| content | VARCHAR(65535) | chunk 文本（含 metadata 头） |
| content_sparse | SPARSE_FLOAT | BM25 自动生成 |
| dense_vector | FLOAT[1024] | BGE-M3 编码 |
| chunk_index | INT64 | 第几个 chunk (0-based) |
| chunk_count | INT64 | 该文档共几个 chunk |
| chunk_lines | VARCHAR(32) | "15-22" 格式 |
| source | VARCHAR(256) | 原始文件名 |
| created_at | VARCHAR(64) | 入库时间 |

### SQLite (conversations.db)
- `messages` 表: id, thread_id, role, content, created_at
- `summaries` 表: thread_id, summary_text, summarized_until_id, updated_at

### SQLite (knowledge.db)
- `documents` 表: doc_id, title, full_content, chunk_count, char_count, source, created_at

---

## 五、端口与启动信息

| 项目 | 值 |
|---|---|
| 端口 | 8080 |
| 环境 | rag_env (Python 3.10) |
| 工作目录 | `C:\Users\15613\Desktop\LangchainRAGtrain\SellerAgent` |
| API 地址 | http://localhost:8080 |
| 前端地址 | http://localhost:8080 |
| 强制 CPU | CUDA_VISIBLE_DEVICES="" |
| LLM | qwen-plus (DashScope) |
| API Key | 在 .env 文件中 |

---

## 六、GitHub

- 仓库: https://github.com/hwskunk/SellerAgent
- 已推送，39 个文件，2698 行代码
- .gitignore 已排除 .env、milvus_data/、test_*.py、knowledge/
