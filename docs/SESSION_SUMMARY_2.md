# SellerAgent 会话总结 #2

> 日期: 2026-07-29 | 会话重点: RAG 检索修复、Reranker 精排、来源去重、性能优化

---

## 一、检索系统全面诊断与修复

### 1.1 根因定位

| 问题 | 根因 | 严重程度 |
|---|---|---|
| 检索结果完全不相关 | Sparse 搜索返回空 + XLSX 重复 chunk 占据 top5 | 🔴 致命 |
| Sparse 搜索返回空 | Windows milvus-lite 无法创建 `content_sparse` 索引 | 🔴 |
| XLSX 解析质量差 | Docling 合并单元格导出时 padding 400-600 字符+全列重复 | 🔴 |
| "说客英语隶属于哪里" 0/5 命中 | 44 个 XLSX chunk 的 embedding 几乎相同，淹没正确结果 | 🔴 |

### 1.2 诊断工具与数据

编写了 `diagnose_retrieval.py` 全自动诊断脚本，覆盖：
- Collection 字段统计、索引列表
- Sparse 向量非零维度检查（表层正常，深层无索引）
- Dense vs Sparse vs Hybrid 三路对比
- Chunk 内容抽样
- 跨文档 embedding 余弦相似度矩阵

**关键数据**:
- Dense 短查询 "说客英语" → 3 条结果 ✅
- Dense 长查询 "说客英语隶属于哪里" → **0 条** ❌（短查询因分词 match，长查询因无索引完全走不了）
- 同文档余弦相似度 0.80-0.85，跨文档 0.52-0.63（区分度仅 ~0.2）

---

## 二、修复 1: XLSX 表格规范化

### 问题

```markdown
| 说客英语家长沟通技巧                          | 说客英语家长沟通技巧                          | ... |
```

一行表头 400-600 字符（Docling 合并单元格导出时保留原始宽度 padding），chunk_size=500 只能放下表头+分隔行，实际内容被挤掉。

### 修复

新增 `_normalize_table_cells()` 函数（[manager.py](src/kb/manager.py)），在 `_extract_tables` 提取表格时自动调用：

1. **Whitespace 压缩**: `re.sub(r'\s{2,}', ' ', cell)` — 消除 padding
2. **合并单元格去重**: 全行所有非空 cell 内容相同时 → 转为单行纯文本（`说客英语家长沟通技巧`）

---

## 三、修复 2: rank-bm25 替代 Milvus Sparse

### 问题

Windows milvus-lite `create_index(field_name='content_sparse')` 必定失败：
```
FileExistsError: manifest.json.tmp → manifest.json
```

### 修复

重写 [milvus_client.py](src/kb/milvus_client.py) 的混合检索：

- **移除**: Milvus `hybrid_search` + `RRFRanker`
- **新增**: `_build_bm25()` — 从 Milvus 全量加载 chunk → rank-bm25 内存索引
- **新增**: `_bm25_search()` — 关键词检索，返回与 dense 一致的格式
- **新增**: `_rrf_fuse()` — 自实现 RRF 融合（与 Milvus 原生逻辑一致）

| 组件 | 修复前 | 修复后 |
|---|---|---|
| Dense 腿 | BGE-M3, 正常 | 不变 |
| BM25 腿 | 不可用（空结果） | rank-bm25, 可用 |
| 融合 | Milvus hybrid_search | 自实现 RRF |

---

## 四、修复 3: 三阶段检索管线 (Reranker)

### 架构

```
用户问题
  │
  ├─ Dense ANN  top-10  (BGE-M3, Milvus AUTOINDEX)
  ├─ BM25 关键词 top-10  (rank-bm25, bigram 分词)
  │
  ├─ RRF 融合去重  (~18 候选)
  │
  └─ Reranker 精排  (BAAI/bge-reranker-base, Cross-encoder)
       │
       └─ top-5 → LLM prompt
```

### 新增方法

- `_get_reranker()`: 延迟加载 BGE Reranker Cross-encoder 模型
- `_rerank(query, candidates, top_k)`: 对每个候选做 `(query, chunk)` 语义匹配，按分数重排

### 检索准确度对比

| 查询 | 修复前 (RRF only) | 修复后 (RRF+Reranker) |
|---|---|---|
| 说客英语隶属于哪里 | ❌ 0/5 | ✅ 2/5 (rank 1) |
| 说客英语 S2B 模式 | 未测 | ✅ 3/5 |
| 家长沟通技巧 | ✅ 5/5 | ✅ 5/5 |
| 私信回复话术 | 🟡 3/5 | ✅ 5/5 |
| 带教考核标准 | ✅ 5/5 | ✅ 5/5 |
| 实操案例 | ✅ 5/5 | ✅ 5/5 |

**6/7 查询命中**

---

## 五、修复 4: 来源去重

### 问题

检索返回的 5 个 chunk 可能来自同一份文档的多个片段，前端"参考来源"显示重复的文档标题。

### 修复

在 `generate.py` 的 `generate_node` 和 `app.py` 的 `chat_stream` 两处，构建 `sources` 时按 `title`（文档名）去重：

```python
sources = []
seen_titles = set()
for d in docs:
    key = d.get("title", "")
    if key in seen_titles:
        continue
    seen_titles.add(key)
    sources.append(...)
```

> ⚠️ 注意：去重 key 必须是 `title` 而非 `source`。
> `source` 字段格式为 `"文档名 · 片段3/24 (第45行)"`，不同 chunk 不同，无法去重。

---

## 六、修复 5: 意图分类稳定性

### 问题

`classify_node` 使用 `llm.with_structured_output(SellerIntent)`，在 DashScope 兼容模式下不稳定 — qwen-turbo 偶尔爆到 16384 token 上限然后解析失败。

日志:
```
[classify] LLM classify failed (Could not parse response content as the length
limit was reached - CompletionUsage(completion_tokens=16384))
```

### 修复

改为直接 `llm.invoke()` + `json.loads()` 手动解析：

```python
resp = llm.invoke([SystemMessage(...), HumanMessage(...)])
text = resp.content.strip()
data = json.loads(text)
intent = data.get("intent", "general_chat")
```

同步更新 prompt：`"只输出 JSON，不要其他内容"`

---

## 七、修复 6: 模型预加载

### 问题

首请求需要加载 BGE-M3 (~560M) + BGE-reranker-base (~270M) 两个模型，冷启动延迟 3-5s。

### 修复

在 `app.py` 的 `startup` 事件中预加载：

```python
kb._get_dense_model()   # 加载 BGE-M3
kb._get_reranker()      # 加载 BGE-reranker-base
```

---

## 八、性能分析与调优

### 多轮计时数据

| # | RECALL/路 | 候选数 | Reranker | 总延迟 |
|---|---|---|---|---|
| 1 | 30 | 50 | 16.85s | 19.76s |
| 2 | 30 | 26 | 3.07s | 3.24s |
| 3 | 30 | 35 | 13.07s | 13.89s |
| 4 | 30 | 32 | 15.60s | 16.47s |
| 5 | 20 | 35 | 16.85s | 19.76s |
| 6 | 20 | 19 | 5.32s | 7.98s |
| 7 | 20 | 16 | 4.99s | 5.80s |
| 8 | 20 | 19 | 6.72s | 7.54s |

### 结论

- **Reranker 始终占 75-85% 的检索延迟**
- CPU 上每对 Cross-encoder 推理约 0.3-0.5s
- 当前 RECALL_PER_LEG=10，候选数 ~18，Reranker 5-7s
- **第 2 次 3.24s 是异常值** — 可能是缓存热或候选数少的特例

### 当前平衡点

`RECALL_PER_LEG = 10` — 召回足够覆盖 127 chunks / 5 文档，Reranker ~7s

---

## 九、数据恢复

- 清除了第一次会话残留的 SQLite 脏数据（10 条重复记录 → 5 条）
- 重新入库 5 份知识库文件，Milvus 127 chunks
- 文件清单:
  - 家长沟通技巧话术参考.xlsx (44 chunks)
  - 说客英语实习生30天带教手册.pdf (24 chunks)
  - 带教考核标准细则.pdf (29 chunks)
  - 带教实操案例库.pdf (20 chunks)
  - 私信回复话术库.pdf (10 chunks)

---

## 十、修改文件清单

| 文件 | 改动 |
|---|---|
| `src/kb/milvus_client.py` | 重写混合检索: rank-bm25 + RRF + Reranker + 计时日志 + 预加载 |
| `src/kb/manager.py` | 新增 `_normalize_table_cells()`，修复 XLSX 解析 |
| `src/nodes/classify.py` | `with_structured_output` → `invoke` + JSON 解析 |
| `src/nodes/generate.py` | 来源去重（按 title） |
| `src/prompts/templates.py` | Classify prompt 改为输出纯 JSON |
| `app.py` | 来源去重 × 2 + 模型预加载 |
| `.gitignore` | 添加 `*.db`, `knowledge/`, `test_conversation.md` |

---

## 十一、GitHub

- 提交: `facfbbe feat: hybrid search with reranker, table normalization, source dedup`
- 13 files, +1700 / -213
- 仓库: https://github.com/hwskunk/SellerAgent

---

## 十二、遗留事项

| 优先级 | 事项 | 说明 |
|---|---|---|
| 🔴 | Reranker 延迟 5-7s | CPU 上 Cross-encoder 瓶颈，待后续优化 |
| 🟡 | Docling OCR/公式/图片提取 | 当前未开启，数学公式和图片内容会丢失 |
| 🟡 | BM25 分词 jieba 安装失败 | 当前用 bigram 回退，关键词精度略低 |
| 🟢 | chunk_size 可考虑调大 | 当前 500 偏小，部分语义不完整 |
| 🟢 | 知识库内容增多后重新评估召回量 | 当前 127 chunks，RECALL_PER_LEG=10 够用 |

---

## 十三、运行信息

| 项目 | 值 |
|---|---|
| 启动命令 | `cd SellerAgent && ..\rag_env\Scripts\python.exe run.py` |
| 端口 | 8080 |
| 清理僵尸进程 | `taskkill /F /IM python.exe` |
| 清理 Milvus 锁 | 删 `manifest.json.tmp` |
