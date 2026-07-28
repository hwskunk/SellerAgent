"""
知识库管理器 — 高级 API

封装 MilvusKB，提供文件上传解析、批量导入等高级功能。
文档解析统一使用 Docling，支持 PDF / DOCX / XLSX / PPTX / HTML / 图片等格式。
"""
import os
import tempfile
from pathlib import Path

from src.kb.milvus_client import get_kb

# Docling 单例，避免每次解析都重新初始化
_converter = None


def _get_converter():
    """延迟加载 Docling DocumentConverter。"""
    global _converter
    if _converter is None:
        from docling.document_converter import DocumentConverter
        _converter = DocumentConverter()
    return _converter


def add_text_document(title: str, content: str, source: str = "manual") -> dict:
    """添加纯文本知识文档。"""
    kb = get_kb()
    doc_id = kb.add_document(title=title, content=content, source=source)
    return {"id": doc_id, "title": title, "char_count": len(content)}


def add_file_document(file_path: str, file_name: str = "") -> dict:
    """从文件导入知识文档。

    支持: .txt, .md, .pdf, .docx, .xlsx, .pptx, .html, .png, .jpg 等
    统一由 Docling 解析为 Markdown 后入库。
    """
    content = _parse_file(file_path)
    if not content.strip():
        raise ValueError(f"文件内容为空或无法解析: {file_path}")

    title = file_name or Path(file_path).name
    source = Path(file_path).name

    return add_text_document(title=title, content=content, source=source)


def add_file_from_bytes(file_bytes: bytes, file_name: str) -> dict:
    """从上传的字节数据导入知识文档。"""
    suffix = Path(file_name).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        result = add_file_document(tmp_path, file_name)
        return result
    finally:
        os.unlink(tmp_path)


def delete_document(doc_id: str) -> bool:
    """删除知识文档。"""
    kb = get_kb()
    return kb.delete_document(doc_id)


def get_document(doc_id: str) -> dict | None:
    """获取单篇文档完整内容。"""
    kb = get_kb()
    return kb.get_document(doc_id)


def list_documents() -> list[dict]:
    """列出所有知识文档。"""
    kb = get_kb()
    return kb.list_documents()


def get_stats() -> dict:
    """获取知识库统计信息。"""
    kb = get_kb()
    return kb.get_stats()


def search_knowledge(query: str, top_k: int = 5) -> list[dict]:
    """检索知识库（混合检索）。"""
    kb = get_kb()
    return kb.hybrid_search(query=query, top_k=top_k)


# ── 文件解析 ──

SUPPORTED_EXTENSIONS = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".pptx", ".html", ".htm",
                        ".png", ".jpg", ".jpeg", ".bmp", ".tiff"}


def _parse_file(file_path: str) -> str:
    """使用 Docling 统一解析文档为 Markdown。

    纯文本文件（.txt / .md）直接读取以保持原始格式。
    其余格式统一由 Docling 处理：
    - PDF: 表格 + 公式 + 布局全部保留
    - DOCX: 表格结构 + OMML 公式转换
    - XLSX: 合并单元格正确填充
    - PPTX: 按幻灯片顺序提取
    - HTML / 图片: 内容提取
    """
    suffix = Path(file_path).suffix.lower()

    # 纯文本文件直接读取，避免 Docling 过度处理
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
    markdown = result.document.export_to_markdown()

    return markdown
