# SellerAgent 会话总结 #4

> 日期: 2026-07-29 | 会话重点: 会话标题乱码修复、依赖版本对齐、图片 OCR 支持、错误提示优化

---

## 一、会话标题乱码排查与修复

### 问题

前端会话列表标题显示为乱码。

### 排查过程

逐层验证了 SQLite → Python API → HTTP 响应 → 前端渲染四层：

| 层级 | 结果 |
|---|---|
| `conversations.db` 中文内容 | ✅ UTF-8 正确 |
| `list_threads()` 返回 | ✅ 中文正常 |
| HTTP 响应原始字节 | ✅ 正确 UTF-8 |
| 前端 `app.js` 渲染逻辑 | ✅ `t.title \|\| '新会话'` 正确 |

### 根因

**两个僵尸 Python 进程**（PID 6764、57136）运行的是 `title` 列加入之前的旧代码。杀掉旧进程、启动新服务后问题解决。

### 教训

- 关闭终端不会自动杀掉 Python 子进程
- 每次重启前执行 `Get-Process -Name "python" \| Stop-Process -Force` 清理

---

## 二、requirements.txt 版本对齐

### 问题

`requirements.txt` 中的版本号严重滞后于实际安装（如 `langgraph>=0.2.0` 实际为 `1.1.9`），虽然 `>=` 约束技术上满足，但新环境可能装到不兼容的旧版本。

### 修改

[requirements.txt](requirements.txt) — 14 个包全部对齐到 `rag_env` 实际版本：

| 包名 | 旧约束 | 新约束 |
|---|---|---|
| langgraph | `>=0.2.0` | `>=1.1.0` |
| langchain | `>=0.3.0` | `>=1.2.0` |
| pymilvus | `>=2.4.0` | `>=3.0.0` |
| milvus-lite | `>=2.4.0` | `>=3.1.0` |
| sentence-transformers | `>=3.0.0` | `>=5.4.0` |
| fastapi | `>=0.115.0` | `>=0.139.0` |
| docling | `>=2.0.0` | `>=2.115.0` |

新增：`rapidocr-onnxruntime>=1.4.0`（OCR 引擎）。

---

## 三、图片 OCR 支持 (Docling + RapidOCR)

### 需求

支持上传 PNG/JPG 等图片文件，提取其中的文字内容。

### 结论

- **有文字的图片**：Docling 默认启用 OCR（`do_ocr=True`），自动调用 RapidOCR 识别中英文文字 → 导出 Markdown → 正常入库 ✅
- **无文字的图片**：OCR 检测不到文字 → `export_to_markdown()` 输出 `<!-- image -->` 占位符 → 被拦截，显示友好提示 ✅
- **嵌图 PDF**（PPT 转 PDF 等扫描件）：同图片逻辑，有文字就识别，无文字就提示

### 踩过的坑

1. **`ImagePipelineOptions` 不存在**：这是 docling 2.115.0 中不存在的类名，已从代码中移除
2. **`do_picture_description` (VLM)**：Docling 内置的 SmolVLM-256M 图片描述功能，但（a）依赖的 torch 版本与当前环境不兼容（`SubgraphCPUBenchmarkRequest` 导入失败）；（b）对独立图片文件不生效（独立图片本身是文档页面，不走内嵌图片管线）
3. **`<!-- image -->` 占位符**：无文字图片导出 markdown 时输出 HTML 注释而非空字符串，需额外判断

### 当前 `_get_converter()` 最终形态

```python
def _get_converter():
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter
        _converter = DocumentConverter()  # OCR 默认开启
    return _converter
```

### 错误提示

| 场景 | 提示信息 |
|---|---|
| 图片无文字 | "该文件未检测到文字内容，无法提取信息。请使用包含文字的文件，或将内容手动输入为文本。" |
| 其他文件为空 | "文件内容为空或无法解析，请检查文件是否包含可提取的文字。" |

---

## 四、Python 3.10.11 `re` 模块缺陷

### 发现

当前环境 Python 3.10.11（`D:\Python3.10.11`）的 `sre_parse` 模块存在已知缺陷：处理复杂正则时内部 tokenizer 对象随机损坏（`self.data` 从 `dict` 变成 `type`，或 `'in <string>' requires string as left operand, not int`）。

### 触发场景

- `bs4/soupsieve` 加载时
- `transformers` 加载模型时（`WeightRenaming` 内部正则）
- **非确定性**：同一代码可能成功也可能失败，取决于第三方库内部加载顺序

### 影响

- 偶尔导致服务器启动失败
- 偶遇上传嵌图 PDF 时崩溃
- **手动启动（`python run.py`）通常正常**，是在当前 shell 环境下通过工具启动时容易触发

### 建议

后续考虑升级 Python 到 3.10.12+ 或 3.11/3.12。

---

## 五、Word 文档解析验证

上传了一个 27k 字的 DOCX 文件（`对话式数字员工落地方案.md.docx`），Docling 解析结果：

| 指标 | 数值 |
|---|---|
| 字符数 | 27,168 |
| Chunks | 59 |
| 标题层级 | ✅ 完整保留 |
| 加粗/斜体 | ✅ 正常 |
| 超链接 | ✅ `[文本](URL)` 格式 |
| 表格 | ✅ 正常 |

结论：DOCX 解析质量可靠。

---

## 六、修改文件清单

| 文件 | 改动 |
|---|---|
| [requirements.txt](requirements.txt) | 14 个包版本对齐 + 新增 rapidocr-onnxruntime |
| [src/kb/manager.py](src/kb/manager.py) | `_get_converter()` 回归默认配置；`add_file_document()` 空内容检查增加 `<!-- image -->` 判断 + 友好错误提示；`_parse_file()` 简化 |
| [app.py](app.py) | 会话标题相关（SESSION_SUMMARY_3 遗留） |
| [src/memory.py](src/memory.py) | 会话标题相关（SESSION_SUMMARY_3 遗留） |
| [static/js/app.js](static/js/app.js) | 会话标题相关（SESSION_SUMMARY_3 遗留） |

---

## 七、GitHub

- 提交: `b033c87` — requirements.txt 版本对齐
- 提交: `cad91b8` — OCR/picture description（后续回退部分逻辑）
- 仓库: https://github.com/hwskunk/SellerAgent

---

## 八、运行信息

| 项目 | 值 |
|---|---|
| 启动命令 | `cd SellerAgent && ..\rag_env\Scripts\python.exe run.py` |
| 端口 | 8080 |
| 清理僵尸进程 | `Get-Process -Name "python" \| Stop-Process -Force` |
| Python | 3.10.11（注意 `re` 模块缺陷） |
| 知识库文件 | 7 份（含 1 图片 1 DOCX） |
| 当前状态 | 正常运行，图片 OCR 可用 |
