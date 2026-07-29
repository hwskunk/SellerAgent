# SellerAgent RAG 检索系统架构

> 版本: 2.0 | 最后更新: 2026-07-29 | 关联: [[SESSION_SUMMARY_1]] [[SESSION_SUMMARY_2]]

---

## 一、整体架构

```
                         ┌─────────────────────────────────────┐
                         │           知识库入库流水线            │
                         │                                     │
   PDF/DOCX/XLSX/PPTX/   │  Docling     递归分块器     BGE-M3  │
   MD/TXT/HTML/图片 ───►│  解析  ──►  500字/块    ──► 向量化  │
                         │           (表格感知)               │
                         └───────────────┬─────────────────────┘
                                         │
                                         ▼
                         ┌─────────────────────────────────────┐
                         │        Milvus Lite (本地文件)        │
                         │  ┌─────────────────────────────────┐│
                         │  │ Content (VARCHAR)               ││
                         │  │ Dense Vector (FLOAT[1024])      ││
                         │  │ Chunk Meta (title/source/lines) ││
                         │  └─────────────────────────────────┘│
                         └───────────────┬─────────────────────┘
                                         │
                                         ▼
                         ┌─────────────────────────────────────┐
用户消息                  │          检索管线 (三阶段)           │
   │                     │                                     │
   ▼                     │  ① 粗排召回                         │
┌──────────┐             │     Dense ANN  top-10 (BGE-M3 IP)   │
│ 意图分类  │             │     BM25 关键词 top-10 (rank-bm25)   │
│ qwen-turbo│            │                              │      │
└────┬─────┘             │  ② RRF 融合去重 (~18 候选)   │      │
     │                   │                              ▼      │
     ▼                   │  ③ Reranker 精排                     │
┌──────────┐             │     BGE-reranker-base Cross-encoder  │
│ 知识检索  │────────────│     (query, chunk) → 语义分数        │
└────┬─────┘             │                              │      │
     │                   │                              ▼      │
     ▼                   │                         top-5 chunks │
┌──────────┐             └─────────────────────────────────────┘
│ LLM 生成  │◄─────────── 拼接 prompt + 检索结果
│ qwen-plus│              
└──────────┘              
     │
     ▼
  SSE 流式输出
```

---

## 二、文档入库

### 2.1 文件解析 (Docling)

```python
# manager.py: _get_converter()
DocumentConverter()  # 默认配置，支持全部格式
```

**支持格式**: PDF, DOCX, XLSX, PPTX, Markdown, HTML, TXT, PNG/JPG

**输出**: 统一 Markdown 文本

**两阶段处理**:
- 文本型文件 (DOCX/PPTX/MD/HTML/TXT): 直接提取
- 图片+PDF 页面: RapidOCR 识别（onnxruntime 引擎，CPU 运行）

### 2.2 文本预处理

```python
# manager.py: _normalize_for_splitting()
```

1. **保护区域**: 代码块 (` ``` `)、Markdown 表格 → 不拆行
2. **长句拆行**: 单行 >150 字符且有 `。` → 按句号插入换行
3. **空行压缩**: 连续 3+ 空行 → 压缩为单个
4. **合并单元格修复**: `_normalize_table_cells()` — 见 §2.3

### 2.3 表格规范化

```python
# manager.py: _normalize_table_cells()
```

Docling 解析 XLSX 时合并单元格会产生两类问题：

| 问题 | 表现 | 修复 |
|---|---|---|
| 空白 padding | 一个 cell 填充 400-600 空格 | `re.sub(r'\s{2,}', ' ', cell)` 压缩 |
| 合并单元格复制 | `说客英语\|说客英语\|说客英语\|说客英语` | 检测全行非空 cell 相同 → 转为纯文本行 |

### 2.4 切片策略

```
chunk_size = 500 字符
overlap    = 50  字符
```

**五层递归切分** (`_split_text`):

```
原始文本
  ├── Step 0: 提取 Markdown 表格 → 暂存
  ├── Step 1: ## 标题前插入 \n\n（语义边界）
  ├── Step 2: 段落( \n\n ) → 行( \n ) 递归合拢到 ≤500
  ├── Step 3: 超长块 → 句子(。！？) → 子句(；，) → 硬切
  ├── Step 4: 表格插回（表头每 chunk 复制，数据行逐行累积）
  └── Step 5: 块间 overlap=50（表格 chunk 跳过）
```

**每个 chunk 最终格式**:

```markdown
[文档: 说客英语实习生30天带教手册.pdf | 片段 3/24 | 第45-60行]

...原始内容...
```

---

## 三、检索管线

### 3.1 粗排召回 (Recall)

#### Dense 路 — 语义检索

| 项目 | 值 |
|---|---|
| 模型 | BAAI/bge-m3 |
| 维度 | 1024 |
| 索引 | Milvus AUTOINDEX |
| 度量 | IP (Inner Product) |
| 召回量 | 10 (RECALL_PER_LEG) |
| 速度 | ~0.1s (ANN) + ~0.5s (embedding) |

#### BM25 路 — 关键词检索

| 项目 | 值 |
|---|---|
| 引擎 | rank-bm25 (Python 库) |
| 分词 | 字符级 bigram（中文）/ 空格分词（英文） |
| 索引 | 纯内存，首次检索时从 Milvus 全量加载构建 |
| 召回量 | 10 (RECALL_PER_LEG) |
| 速度 | ~0.01s（索引已构建） |

> **为什么不用 Milvus 内置 BM25？**
> Windows 下 milvus-lite 无法为 `content_sparse` 创建索引（`os.rename` 在 `manifest.json.tmp → manifest.json` 时抛出 `FileExistsError`）。改用独立 `rank-bm25` 库完全规避此 bug，功能等价。

### 3.2 RRF 融合去重

```
rrf_score(doc) = Σ 1/(k + rank_i + 1)    # k=60

其中 rank_i 是文档在第 i 条检索腿中的排名
```

同一个 chunk 在 Dense 和 BM25 两路都出现时，分数累加。去重按 chunk id。

### 3.3 精排 (Rerank)

| 项目 | 值 |
|---|---|
| 模型 | BAAI/bge-reranker-base |
| 架构 | Cross-encoder (272M params) |
| 原理 | `(query, chunk)` 拼接 → 单次 Transformer 前向 → 相关度分数 |
| 候选数 | ~18（RRF 去重后） |
| 速度 | ~5-7s（CPU，每对 ~0.3-0.5s） |
| 优势 | 真正理解语义，远优于向量内积 |

> **精度对比**: Bi-encoder (BGE-M3) 只能做向量内积 → 语义近似。Cross-encoder 同时编码 query 和 chunk → 精准语义匹配。代价是每对一个 forward pass，无法预计算。

### 3.4 性能数据

| 阶段 | 单次耗时 | 占比 |
|---|---|---|
| Dense embedding | 0.5-1.0s | 8% |
| Dense ANN 搜索 | 0.8-1.3s | 12% |
| BM25 搜索 | 0.01-1.7s | 5% |
| Reranker 精排 (18候选) | 5-7s | **75%** |
| **总检索** | **7-8s** | |

> 首请求：额外 2-3s 构建 BM25 索引。模型预加载到内存后，后续请求不再加载。

---

## 四、数据存储

### 4.1 Milvus Collection (seller_knowledge)

| 字段 | 类型 | 说明 |
|---|---|---|
| id | VARCHAR(64) PK | `{doc_id}_c{index}` |
| parent_doc_id | VARCHAR(64) | 文档 ID |
| title | VARCHAR(512) | 文档标题 |
| content | VARCHAR(65535) | chunk 全文（含 metadata 头） |
| dense_vector | FLOAT[1024] | BGE-M3 编码 |
| chunk_index | INT64 | 第几块 (0-based) |
| chunk_count | INT64 | 本文档总块数 |
| chunk_lines | VARCHAR(32) | 原文行范围 |
| source | VARCHAR(256) | 原始文件名 |
| created_at | VARCHAR(64) | 入库时间 |

### 4.2 SQLite (src/knowledge.db)

`documents` 表: doc_id, title, full_content, chunk_count, char_count, source, created_at

### 4.3 SQLite (conversations.db)

- `messages`: id, thread_id, role, content, created_at
- `summaries`: thread_id, summary_text, summarized_until_id, updated_at

---

## 五、LLM 环节

| 功能 | 模型 | 参数 |
|---|---|---|
| 意图分类 | qwen-turbo (DashScope) | max_tokens=256, temperature=0 |
| 回复生成 | qwen-plus (DashScope) | max_tokens=2048, temperature=0.3 |
| 总结蒸馏 | qwen-plus | max_tokens=512 |

**意图分类输出格式**: JSON 手动解析（不用 `with_structured_output`，DashScope 兼容模式下不稳定）

```json
{"intent": "sales_inquiry", "summary": "用户想了解产品价格"}
```

有效意图: `sales_inquiry` | `product_info` | `kb_management` | `general_chat`

---

## 六、记忆系统 (Summary Buffer)

- 最近 10 轮对话 → 完整保留在 prompt 中
- ≥20 条消息 → 触发总结检查
- 每新增 10 条 → LLM 蒸馏旧对话 → 一份精炼总结
- LLM 输入拼接顺序: `System → 总结 → 最近10轮 → 用户输入`

---

## 七、优化队列

| 优先级 | 事项 | 预期效果 |
|---|---|---|
| 🔴 | Reranker 延迟 (5-7s) | 待内容量增大后考虑轻量模型或 GPU |
| 🟡 | BM25 分词 — 当前 bigram，可换 jieba | 关键词匹配精度提升 |
| 🟡 | Docling OCR/公式/图片 未开启 | 当前只做文本提取 |
| 🟢 | chunk_size 可调大至 800-1000 | 减少候选碎片化 |

---

## 八、关键决策记录

1. **为什么自建 BM25 而不用 Milvus 内置？** Windows milvus-lite 无法创建 sparse 索引（manifest.json.tmp 重命名 bug），独立 rank-bm25 完全规避且功能等价。

2. **为什么用 Cross-encoder 精排？** Bi-encoder 向量检索在语义相近的文档间区分度不足（同文档相似度 0.80-0.85，跨文档 0.52-0.63，差距仅 ~0.2）。Cross-encoder 能真正理解 query-chunk 语义关系。

3. **为什么召回量定在 10？** 实测数据：30/路 → 50 候选 → 17s rerank；20/路 → 35 候选 → 14s；10/路 → 18 候选 → 7s。当前 127 chunk / 5 文档规模下 10 足够覆盖。

4. **来源去重按什么 key？** 按 `title`（文档名）去重而非 `source`（含片段号）。因为同一个文档的多个 chunk 是同一份资料的组成部分。
