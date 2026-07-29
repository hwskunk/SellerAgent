# SellerAgent 会话总结 #3

> 日期: 2026-07-29 | 会话重点: knowledge/ 源文件同步、内容去重、会话标题、路径修正

---

## 一、knowledge/ 源文件实时同步

### 需求

前端增删文档时，`knowledge/` 目录同步增删对应文件，作为源文件的持久化存储。

### 修改

| 文件 | 改动 |
|---|---|
| `src/kb/doc_store.py` | `documents` 表新增 `knowledge_path` 列，记录源文件路径；兼容旧表自动 `ALTER TABLE` |
| `src/kb/manager.py` | 新增 `KNOWLEDGE_DIR` 常量指向项目根目录 `knowledge/`；`add_text_document` 将文本写入 `{doc_id}_{标题}.md`；`add_file_document` 拷贝上传文件到 `knowledge/` 并保留原始文件名；`delete_document` 同步删除源文件 |
| `src/kb/manager.py` | 新增 `_sanitize_filename()` 辅助函数，剔除文件名中的非法字符 |

### 文件命名规则

```
knowledge/
├── {doc_id}_{标题}.md              ← 前端文本输入
├── {原始文件名}.pdf                ← 上传的文件
└── 重名时追加 _{doc_id} 后缀        ← 避免覆盖
```

### 注意事项

- **单向同步**：前端操作 → 后端（Milvus + SQLite + knowledge/ 三端一致）。直接在文件夹删文件不会反映到前端，因为后端不知道
- 首个版本存在 bug：上传文件时 `source` 引用了临时文件路径（`tmph_1h7hsw`），导致 knowledge/ 里的文件名为乱码。已在本次会话中修复

---

## 二、内容去重

### 需求

上传相同内容的文件或粘贴相同文本时，拒绝重复入库。

### 实现

- `documents` 表新增 `content_hash` 列（SHA256）
- **统一按解析后的文本内容**（而非原始字节）算哈希，确保旧记录补哈希、文本添加、文件上传三者一致
- 文件上传：先 Docling 解析 → 对解析后的 Markdown 文本算 SHA256
- 文本添加：直接对用户输入的文本算 SHA256
- `doc_store.get_document_by_hash()` 方法按哈希查重
- 重复时抛出 `ValueError`，前端显示错误提示

### 关键变更

[manager.py:443-467](src/kb/manager.py) — `add_file_document` 从"读原始字节算哈希"改为"先解析再算哈希"：

```python
# 修复前（BUG）: 原始字节哈希 → 与旧记录的 text_hash 对不上
with open(file_path, "rb") as f:
    file_hash = hashlib.sha256(f.read()).hexdigest()

# 修复后: 解析后的文本哈希 → 三者一致
content = _parse_file(file_path)
file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
```

### 知识库重建

旧记录的 `content_hash` 全部为 NULL，需要重建。操作步骤：
1. 清空 `milvus_fresh/seller_kb.db` 和 `src/knowledge.db`
2. 从 `knowledge/` 目录重新导入全部 6 个文件
3. 每个文件入库时自动生成 `content_hash`

导入脚本：`rebuild_kb.py`（已用完删除）

---

## 三、会话标题

### 问题

会话列表显示 `thread_id`（如 `7bd2ba832e6b`），全是乱码，无法区分。

### 修改

| 文件 | 改动 |
|---|---|
| `src/memory.py` | `summaries` 表新增 `title` 列；`list_threads()` 返回第一条用户消息作为默认标题；新增 `_make_thread_title()` 和 `rename_thread()` 函数 |
| `app.py` | 新增 `PUT /api/threads/{thread_id}/rename` 接口 |
| `static/js/app.js` | `refreshThreadList()` 显示 `t.title` 而非 `t.thread_id`；新增 `startRename()` 双击重命名逻辑；发送消息后自动 `refreshThreadList()` |

### 交互

- 新会话发送第一条消息后，自动用消息内容做标题（截断 20 字）
- **双击标题**进入编辑模式，回车确认，Esc 取消
- 自定义标题持久化在 SQLite `summaries.title`

> ⚠️ 旧会话的中文消息存在编码问题，标题显示为乱码。需要删除旧会话重建，或双击手动重命名。

---

## 四、路径修正

`run.py:17` — 默认 `MILVUS_DB_PATH` 从 `milvus_v2/kb.db` 改为 `milvus_fresh/seller_kb.db`，与 `.env` 保持一致，消除歧义。

---

## 五、发现但未处理的代码问题

[milvus_client.py:416-422](src/kb/milvus_client.py#L416-L422) — 注释和实际行为不一致：

```python
# ── Step 4: 加权融合精排（零额外推理）──
# 不做 Cross-encoder，CPU 上每对 0.5s 太慢     ← 注释说砍掉了
reranked = self._rerank(query, fused, top_k)    ← 实际还在调用 Cross-encoder
```

`_rerank()` 方法（[milvus_client.py:115-148]）仍然加载 BGE-reranker-base Cross-encoder 做精排，注释是愿望，代码是现实。每次检索仍需要 5-7s 的 Reranker 推理。

---

## 六、相关文档

- [[SESSION_SUMMARY_1]] — 项目初始化、架构搭建
- [[SESSION_SUMMARY_2]] — RAG 检索修复、Reranker 精排、性能分析
- [[RAG_SYSTEM_ARCHITECTURE]] — 完整架构文档

---

## 七、运行信息

| 项目 | 值 |
|---|---|
| 启动命令 | `cd SellerAgent && ..\rag_env\Scripts\python.exe run.py` |
| 端口 | 8080 |
| 清理僵尸进程 | `Get-Process -Name "python" \| Stop-Process -Force` |
| 清理 Milvus 锁 | 确保没有其他 Python 进程占着 |
| 数据库目录 | `milvus_fresh/seller_kb.db/`（Milvus）、`src/knowledge.db`（文档元数据）、`conversations.db`（对话） |
