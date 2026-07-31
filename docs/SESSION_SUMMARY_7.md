# SellerAgent 会话总结 #7

> 日期: 2026-07-31 | 会话重点: 知识库数据流梳理、宝塔风格上传弹窗、上传取消机制探索

---

## 一、知识库三层数据流

### 架构

```
上传文件
  │
  ▼
Docling 解析 → Markdown 文本
  │
  ├──→ Milvus（向量库）          ← 真正的"知识"，检索用
  │      └─ parent_doc_id 关联
  │
  ├──→ SQLite / knowledge.db     ← 元数据（标题、字数、时间…）
  │      └─ 前端列表从这里读
  │
  └──→ knowledge/ 目录           ← 源文件存档（.md/.pdf/.docx…）
```

### 删除链路

`manager.delete_document(doc_id)` 一次性清理三层：
1. `kb.delete_chunks(doc_id)` → 删 Milvus 向量
2. `doc_store.delete_document(doc_id)` → 删 SQLite 记录
3. `Path(knowledge_path).unlink()` → 删 knowledge/ 源文件

### 启动一致性校验

`run.py` 启动时执行（不是运行中实时同步）：
- 读取 Milvus 中所有 `parent_doc_id`
- 读取 SQLite 记录的所有 `doc_id`
- SQLite 有但 Milvus 没有的 → 自动删除 SQLite 记录 + `knowledge/` 源文件
- 只在启动时做是因为：正常运行时所有操作走 API → `manager.delete_document()` 同时删三层，不会出现不一致

---

## 二、宝塔风格上传弹窗

### 需求

面向零技术基础用户，类似宝塔面板的上传体验：
1. 点「上传文件」按钮 → 弹出上传面板
2. 面板内点「添加文件」→ 打开文件夹可选择多个文件
3. 选中的文件显示在面板列表中（文件名、大小、类型图标）
4. 可继续追加文件或移除不需要的
5. 点「确认上传」→ 逐个上传，每个文件显示实时状态

### 改动文件

| 文件 | 改动 |
|------|------|
| `static/index.html` | 原来点击上传区域 → 「上传文件」按钮 + 新增上传弹窗面板（`#uploadModal`） |
| `static/css/style-v2.css` | 新增上传弹窗全部样式：`.upload-modal-*` 系列，不同类型文件有不同颜色图标（PDF红、DOCX蓝、XLSX绿…），含移动端适配 |
| `static/js/app.js` | 重写上传逻辑：`UPLOAD_QUEUE` 队列管理、去重（同名同大小跳过）、逐文件上传、逐文件状态更新（等待中→上传中→✓完成/失败）、Escape 优先关闭上传弹窗 |

### 操作流程

1. 右侧面板点「上传文件」→ 弹出面板
2. 点「添加文件」→ 打开文件选择器，可多选
3. 文件显示在列表，可继续追加或点 ✕ 移除
4. 点「确认上传」→ 逐个上传，实时显示状态
5. 全部完成显示汇总：`3 个文件 · 3 成功 · 25秒`

### 细节处理

- 同名同大小文件自动去重
- 上传中关闭弹窗弹确认框
- 上传中禁用移除按钮和确认按钮
- `background: var(--bg-white)` 修复文件列表区域透明问题

---

## 三、上传取消机制探索（未合入主线）

### 问题

当前版本（回退后）取消=前端弹窗关闭不等待响应，但后端仍会完整处理完成并入库。Docling 解析期间（50s+）占用 GIL，导致页面切换/刷新卡住。

### 尝试方案：子进程 Docling

创建 `src/kb/docling_worker.py` 作为独立子进程运行 Docling，主进程通过 `cancel_event` + watcher 线程监听取消信号，取消时 `proc.kill()` 立即释放。

同时在 `app.py` 添加：
- `asyncio.Semaphore(1)` 限制同时上传数
- `Request.is_disconnected()` 后台轮询检测客户端断连
- `run_in_executor` 将重处理放到线程池避免阻塞事件循环

### 失败原因

子进程通过 `sys.executable` 启动，在 Windows 上找不到 docling 等依赖模块。

### 回退

- `manager.py`、`app.py` 回退到简单内联 Docling 版本
- `docling_worker.py` 已删除
- 后续解决方向：用 `multiprocessing.Process` 或传递 `PYTHONPATH` 环境变量

---

## 四、当前 BGE-M3 性能

本地 CPU 跑 BAAI/bge-m3：33 秒 24 个切片，平均 1.4 秒/切片。合理范围。

如需加速可切换云端 Embedding（`.env` 改 `EMBEDDING_MODEL=text-embedding-v3`），24 切片并行 3 路 API 约 2-3 秒。

---

## 五、仓库状态

- **GitHub**: https://github.com/hwskunk/SellerAgent
- **分支**: `main`
- **待提交文件**: `run.py`, `src/kb/manager.py`, `src/kb/milvus_client.py`, `static/css/style-v2.css`, `static/index.html`, `static/js/app.js`
- **未跟踪文件**: `SellerAgent-deploy.zip`, `SellerAgent-deploy.tar.gz`, `static/js/app.js.bak`（不要提交）

### 推送命令

```bash
cd C:\Users\15613\Desktop\LangchainRAGtrain\SellerAgent
git add run.py src/kb/manager.py src/kb/milvus_client.py static/css/style-v2.css static/index.html static/js/app.js
git commit -m "feat: 上传弹窗面板 + 多文件队列上传 + SQLite-Milvus一致性校验"
git push
```

---

## 六、修改文件清单

| 文件 | 本会话改动 |
|------|-----------|
| `static/index.html` | 上传区域 → 「上传文件」按钮 + 上传弹窗面板 HTML |
| `static/css/style-v2.css` | 新增 `.upload-modal-*` 全部样式，修复文件列表透明背景 |
| `static/js/app.js` | 重写上传为弹窗+队列模式，Escape 键兼容上传弹窗 |
| `src/kb/manager.py` | 回退到内联 Docling（无 cancel_event），最终无变更 |
| `app.py` | 回退到简单上传接口（无信号量/断连检测），最终无变更 |
| `src/kb/docling_worker.py` | 创建后删除（子进程方案暂未合入） |
