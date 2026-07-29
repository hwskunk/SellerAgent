"""
知识库管理器 — 高级 API

- 文档解析：Docling 统一处理 PDF/DOCX/XLSX/PPTX/HTML/图片
- 文本分块：langchain RecursiveCharacterTextSplitter
- 向量存储：Milvus（chunk 粒度）
- 元数据：SQLite doc_store
- 源文件同步：knowledge/ 目录实时同步增删
"""
import hashlib
import os
import re as _re
import shutil
import tempfile
import uuid
from pathlib import Path

from src.kb.milvus_client import get_kb
from src.kb import doc_store

# Docling 单例
_converter = None

CHUNK_SIZE = 500      # 每 chunk 约 500 字符

# 源文件存档目录（项目根目录下）
KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"
CHUNK_OVERLAP = 50    # chunk 间重叠 50 字符


def _get_converter():
    """获取 Docling 转换器单例（开启 OCR，支持中英文）。

    - PDF：开启 RapidOCR 文字识别，处理扫描件和 PPT 转 PDF 等图片型 PDF
    - 图片：通过 ImageFormatOption 开启 OCR
    - 其他格式（DOCX/XLSX/PPTX/HTML）：默认配置即可
    """
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption, ImageFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions, ImagePipelineOptions
        from docling.datamodel.base_models import InputFormat

        # PDF OCR 配置
        pdf_options = PdfPipelineOptions()
        pdf_options.do_ocr = True
        pdf_options.ocr_options.lang = ["zh", "en"]

        # 图片 OCR 配置
        img_options = ImagePipelineOptions()
        img_options.do_ocr = True
        img_options.ocr_options.lang = ["zh", "en"]

        _converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
                InputFormat.IMAGE: ImageFormatOption(pipeline_options=img_options),
            }
        )
        print("[Manager] Docling converter initialized with OCR (zh+en)")
    return _converter


def _normalize_for_splitting(text: str) -> str:
    """预处理：为单行长文本按句子边界插入换行，使行号追踪有意义。

    同时保护 Markdown 特殊区域（表格、代码块、分隔线）不被破坏。
    """
    import re

    lines = text.split("\n")
    result = []

    in_code_fence = False
    in_table = False

    for line in lines:
        stripped = line.strip()

        # 追踪代码块边界
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            result.append(line)
            continue
        if in_code_fence:
            result.append(line)
            continue

        # 追踪表格边界
        is_table_line = bool(re.match(r'^\|.*\|$', stripped))
        is_separator_line = bool(re.match(r'^[\|\s\-:]+$', stripped))
        if is_table_line and not in_table:
            in_table = True
        elif not is_table_line:
            in_table = False
        if in_table:
            result.append(line)
            continue

        # 空行和标题保持不变
        if not stripped or stripped.startswith("#"):
            result.append(line)
            continue

        # 单行过长 → 在句子边界插入换行（让行号追踪有意义）
        if len(line) > 150 and "。" in line:
            parts = re.split(r'(?<=[。！？])(?=[^」』\)）])', line)
            result.append(parts[0].rstrip())
            for p in parts[1:]:
                result.append(p.lstrip())
        else:
            result.append(line)

    result_text = "\n".join(result)
    # 连续空行压缩
    result_text = re.sub(r'\n{3,}', '\n\n', result_text)
    return result_text


def _split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """智能分块：Markdown 感知 + 语义边界保持。

    切分优先级（递归）：
    1. 表格 — 整表不拆（先提取出来单独处理）
    2. Markdown 标题 (##) — 标题前切开
    3. 段落边界 (\\n\\n) → 行边界 (\\n)
    4. 句子边界 (。！？) → 子句边界 (；，)
    5. 硬切
    """
    import re

    # Step 0: 提取表格区域（整体保护不对其内部切分）
    text, tables = _extract_tables(text)

    # Step 1: Markdown 标题前插入双换行
    text = re.sub(r'(?<!\n)\n(#{1,3}\s)', r'\n\n\1', text)

    # Step 2: 段落 → 行递归合拢
    chunks = _recursive_split(text, chunk_size)

    # Step 3: 超长块精细切分
    final_chunks = []
    for c in chunks:
        if len(c) <= chunk_size:
            final_chunks.append(c)
        else:
            final_chunks.extend(_fine_split(c, chunk_size))

    # Step 4: 表格插回（chunk 策略：每块都带表头，数据行按 chunk_size 分组）
    for tbl in tables:
        if len(tbl) <= chunk_size:
            final_chunks.append(tbl)
        else:
            lines = tbl.split("\n")
            # 表头 = 列名行 + 分隔行（两行固定，每个 chunk 都复制）
            header_lines = lines[:2]  # e.g. ["| A | B |", "|---|---|"]
            data_lines = lines[2:]    # 只算数据行

            buf = ""
            for row in data_lines:
                candidate = buf + "\n" + row if buf else row
                header_size = len(header_lines[0]) + len(header_lines[1]) + 2
                if header_size + len(candidate) <= chunk_size:
                    buf = candidate
                else:
                    if buf:
                        final_chunks.append(f"{header_lines[0]}\n{header_lines[1]}\n{buf}")
                    buf = row
            if buf:
                final_chunks.append(f"{header_lines[0]}\n{header_lines[1]}\n{buf}")

    # Step 5: 块间重叠（表格 chunk 跳过 overlap，自身已带完整表头）
    overlapped = _apply_overlap(final_chunks, overlap)
    return [c for c in overlapped if c.strip()]


def _normalize_table_cells(table_text: str) -> str:
    """规范化 Markdown 表格单元格内的空白字符和重复表头。

    Docling 导出合并单元格 XLSX 时存在两个问题：
    1. 单元格内填充大量空格 — 一行表头 400-600 字符
    2. 合并单元格被复制到全部列 — 出现 "说客英语|说客英语|说客英语|说客英语"

    处理：逐 cell 压缩空格；若一行全部非空 cell 内容相同，
    说明是合并单元格产物，转为纯文本行，避免关键词在 embedding
    中被反复强化。
    """
    import re
    lines = table_text.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        # 分隔行（|---|---|）保持原样
        if re.match(r'^[\|\s\-:]+$', stripped):
            result.append(stripped)
            continue
        if stripped.startswith("|"):
            cells = stripped.split("|")
            normalized = []
            for cell in cells:
                cell = cell.strip()
                cell = re.sub(r'\s{2,}', ' ', cell)
                normalized.append(cell)

            # 如果所有非空 cell 内容相同（合并单元格产物），转纯文本
            non_empty = [c for c in normalized if c]
            if len(non_empty) >= 2 and all(c == non_empty[0] for c in non_empty):
                result.append(non_empty[0])
            else:
                result.append("|".join(normalized))
        else:
            result.append(line)
    return "\n".join(result)


def _extract_tables(text: str) -> tuple[str, list[str]]:
    """提取 Markdown 表格区域，返回 (剩余文本, 表格列表)。

    表格判定：连续两行以上以 | 开头或为分隔行 |---|...|
    提取后用占位符替换，切分完成后再插回。

    提取时自动对表格单元格做 whitespace normalize，
    解决 Docling 导出 XLSX 合并单元格时 padding 过大的问题。
    """
    import re
    lines = text.split("\n")
    tables = []
    result_lines = []
    in_table = False
    table_lines = []

    for line in lines:
        stripped = line.strip()
        is_table_line = bool(re.match(r'^\|.*\|$', stripped))
        is_separator = bool(re.match(r'^[\|\s\-:]+$', stripped))

        if is_table_line or (in_table and is_separator):
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(line)
        else:
            if in_table:
                raw = "\n".join(table_lines)
                tables.append(_normalize_table_cells(raw))
                table_lines = []
                in_table = False
            result_lines.append(line)

    if in_table:
        raw = "\n".join(table_lines)
        tables.append(_normalize_table_cells(raw))

    return "\n".join(result_lines), tables


def _recursive_split(text: str, chunk_size: int) -> list[str]:
    """递归合拢切分：段落 → 行 → 返回（超长的留给 _fine_split 处理）。"""
    for sep in ("\n\n", "\n"):
        parts = text.split(sep)
        chunks, buf = [], ""
        for part in parts:
            candidate = (buf + sep + part).lstrip(sep) if buf else part
            if len(candidate) <= chunk_size:
                buf = candidate
            else:
                if buf:
                    chunks.append(buf)
                buf = part if len(part) <= chunk_size else ""
                if not buf:
                    chunks.append(part)
        if buf:
            chunks.append(buf)
        if all(len(c) <= chunk_size for c in chunks):
            return chunks
        # 仍有超长块，降级到更细分隔符继续
        text = "\n".join(chunks)
    return [text]  # 实在没法细切，交给 _fine_split


def _fine_split(text: str, chunk_size: int) -> list[str]:
    """对超长块做句子 → 子句 → 硬切。"""
    import re
    result = []
    for part in re.split(r'(?<=[。！？])(?!\n)', text):
        if len(part) <= chunk_size:
            result.append(part)
        else:
            for sub in re.split(r'(?<=[；，])(?!\n)', part):
                if len(sub) <= chunk_size:
                    result.append(sub)
                else:
                    result.extend(_force_split(sub, chunk_size))
    return result


def _force_split(text: str, chunk_size: int) -> list[str]:
    """硬切：优先在标点/空格处断开，否则在 chunk_size 强行切开。"""
    result = []
    while len(text) > chunk_size:
        # 优先断点：标点 > 空格 > 硬切
        split_at = -1
        for ch in ["。", "！", "？", "；", "，", " ", "\n"]:
            idx = text.rfind(ch, 0, chunk_size)
            if idx > chunk_size * 0.5:  # 不能太偏前
                split_at = idx + len(ch)
                break
        if split_at <= 0:
            split_at = chunk_size

        result.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()

    if text.strip():
        result.append(text)
    return result


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """块间重叠：后一块的开头从前一块结尾取 overlap 字符。
    表格 chunk（以 | 开头）不做 overlap，避免破坏表头结构。
    """
    if not chunks or overlap <= 0:
        return chunks
    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = result[-1]
        current = chunks[i]
        prev_is_table = prev.strip().startswith("|")
        current_is_table = current.strip().startswith("|")

        if prev_is_table or current_is_table:
            result.append(current)
        elif len(prev) > overlap:
            tail = prev[-overlap:]
            for ch in ["\n", "。", "！", "？", "；", "，", " "]:
                idx = tail.find(ch)
                if idx >= 0:
                    tail = tail[idx + 1:]
                    break
            result.append(tail + current)
        else:
            result.append(current)
    return result


def _chunk_and_insert(title: str, content: str, source: str, knowledge_path: str = "", doc_id: str = "", content_hash: str = "") -> dict:
    """文本分块 → Milvus 入库 → doc_store 记录元数据。"""
    doc_id = doc_id or uuid.uuid4().hex[:12]
    kb = get_kb()

    # 预处理：归一化单行长文本
    normalized = _normalize_for_splitting(content)

    # 分块（返回 [(text, separator_name), ...]）
    raw_chunks = _split_text(normalized)
    chunk_texts = [c for c in raw_chunks if c.strip()]
    if not chunk_texts:
        chunk_texts = [content]

    # 构建 chunk 元数据 + 计算行号
    line_positions = _compute_line_positions(normalized)
    chunks = []
    current_pos = 0

    for i, chunk_text in enumerate(chunk_texts):
        # 在原文中定位此 chunk（处理重复文本时找下一个匹配位置）
        start_pos = normalized.find(chunk_text, current_pos)
        if start_pos < 0:
            # fallback: 忽略空白差异重新找
            start_pos = normalized.find(chunk_text.strip())
        end_pos = (start_pos + len(chunk_text)) if start_pos >= 0 else 0
        current_pos = max(current_pos, end_pos)

        start_line = _pos_to_line(line_positions, start_pos) if start_pos >= 0 else i + 1
        end_line = _pos_to_line(line_positions, end_pos) if end_pos > 0 else start_line

        if start_line == end_line:
            lines_label = str(start_line)
        else:
            lines_label = f"{start_line}-{end_line}"

        # 给 chunk 文本加上 metadata 头，LLM 看到后能理解上下文
        total = len(chunk_texts)
        metadata_header = f"[文档: {title} | 片段 {i+1}/{total}"
        if lines_label:
            metadata_header += f" | 第{lines_label}行"
        metadata_header += "]\n\n"
        chunk_with_meta = metadata_header + chunk_text

        chunks.append({
            "index": i,
            "content": chunk_with_meta,
            "lines": lines_label,
        })

    # 插入 Milvus
    kb.insert_chunks(doc_id=doc_id, title=title, source=source, chunks=chunks)

    # 记录元数据（含 knowledge_path，用于删除时同步清理源文件）
    doc_store.add_document(
        doc_id=doc_id, title=title, full_content=content,
        chunk_count=len(chunks), source=source,
        knowledge_path=knowledge_path,
        content_hash=content_hash,
    )

    print(f"[Manager] Doc chunked: {len(chunks)} chunks, title={title}")
    return {"id": doc_id, "title": title, "chunk_count": len(chunks), "char_count": len(content)}


def _compute_line_positions(text: str) -> list[int]:
    positions = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            positions.append(i + 1)
    return positions


def _pos_to_line(positions: list[int], pos: int) -> int:
    for i, p in enumerate(positions):
        if p > pos:
            return i
    return len(positions)


# ═══════════════════════════════════════════════════════════════
# 公开 API
# ═══════════════════════════════════════════════════════════════

def _sanitize_filename(name: str, max_len: int = 80) -> str:
    """把标题转为安全的文件名片段。"""
    name = _re.sub(r'[\\/:*?"<>|]', '_', name)
    name = _re.sub(r'\s+', '_', name)
    return name[:max_len].strip("_")


def add_text_document(title: str, content: str, source: str = "manual") -> dict:
    """添加纯文本知识文档（自动分块 + 入库），同步保存到 knowledge/ 目录。"""
    if not content.strip():
        raise ValueError("内容不能为空")

    # 内容去重检查
    text_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = doc_store.get_document_by_hash(text_hash)
    if existing:
        raise ValueError(
            f"内容完全相同的文档已存在于知识库中（文档: {existing['title']}，"
            f"ID: {existing['id']}），无需重复添加"
        )

    # 先生成 doc_id，写入源文件后再入库（确保 knowledge_path 一次到位）
    doc_id = uuid.uuid4().hex[:12]
    safe_title = _sanitize_filename(title) if title else "untitled"
    file_name = f"{doc_id}_{safe_title}.md"
    file_path = KNOWLEDGE_DIR / file_name
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    print(f"[Manager] Source file saved: {file_path}")

    return _chunk_and_insert(
        title=title, content=content, source=source,
        doc_id=doc_id, knowledge_path=str(file_path),
        content_hash=text_hash,
    )


def add_file_document(file_path: str, file_name: str = "") -> dict:
    """从文件导入知识文档（解析 + 分块 + 入库），同步拷贝源文件到 knowledge/。"""
    # 先解析文件得到文本内容
    content = _parse_file(file_path)
    if not content.strip():
        raise ValueError(f"文件内容为空或无法解析: {file_path}")

    # 按解析后的文本内容算哈希（与旧记录补哈希、文本添加保持一致）
    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    # 去重检查
    existing = doc_store.get_document_by_hash(file_hash)
    if existing:
        raise ValueError(
            f"该文件内容已存在于知识库中（文档: {existing['title']}，"
            f"ID: {existing['id']}），无需重复添加"
        )

    title = file_name or Path(file_path).name
    source = file_name or Path(file_path).name  # 用原始文件名，不是临时文件路径

    # 生成 doc_id，拷贝源文件到 knowledge/，保留原始文件名
    doc_id = uuid.uuid4().hex[:12]
    safe_name = _sanitize_filename(source)
    dest_path = KNOWLEDGE_DIR / safe_name
    # 重名时追加 doc_id 后缀
    if dest_path.exists():
        stem, ext = os.path.splitext(safe_name)
        dest_path = KNOWLEDGE_DIR / f"{stem}_{doc_id}{ext}"
    os.makedirs(KNOWLEDGE_DIR, exist_ok=True)
    shutil.copy2(file_path, dest_path)
    print(f"[Manager] Source file saved: {dest_path}")

    return _chunk_and_insert(
        title=title, content=content, source=source,
        doc_id=doc_id, knowledge_path=str(dest_path),
        content_hash=file_hash,
    )


def add_file_from_bytes(file_bytes: bytes, file_name: str) -> dict:
    """从上传的字节数据导入知识文档。"""
    suffix = Path(file_name).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    try:
        return add_file_document(tmp_path, file_name)
    finally:
        os.unlink(tmp_path)


def delete_document(doc_id: str) -> bool:
    """删除文档及其所有 chunk，同步清理 knowledge/ 源文件。"""
    # 先查出 knowledge_path，删完记录后再清理文件
    doc = doc_store.get_document(doc_id)
    kb = get_kb()
    deleted = kb.delete_chunks(doc_id)
    doc_store.delete_document(doc_id)

    # 清理 knowledge/ 中的源文件
    if doc and doc.get("knowledge_path"):
        kp = Path(doc["knowledge_path"])
        if kp.exists():
            kp.unlink()
            print(f"[Manager] Source file deleted: {kp}")

    return deleted > 0


def get_document(doc_id: str) -> dict | None:
    """获取单篇文档完整内容（从 doc_store 读取原文）。"""
    return doc_store.get_document(doc_id)


def list_documents() -> list[dict]:
    """列出所有知识文档（逻辑文档级别）。"""
    return doc_store.list_documents()


def get_stats() -> dict:
    """获取知识库统计信息。"""
    stats = doc_store.get_stats()
    stats["collection_name"] = os.environ.get("MILVUS_COLLECTION_NAME", "seller_knowledge")
    return stats


def search_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索知识库（混合检索，返回最相关的 chunk）。"""
    kb = get_kb()
    return kb.hybrid_search(query=query, top_k=top_k)


# ═══════════════════════════════════════════════════════════════
# 文件解析
# ═══════════════════════════════════════════════════════════════

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm",
                        ".png", ".jpg", ".jpeg", ".bmp", ".tiff"}


def _parse_file(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix in (".txt", ".md"):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"不支持的文件格式: {suffix}\n"
            f"支持的格式: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    converter = _get_converter()
    result = converter.convert(file_path)
    return result.document.export_to_markdown()
