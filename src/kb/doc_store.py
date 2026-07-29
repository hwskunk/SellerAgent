"""
文档元数据存储 — SQLite

追踪逻辑文档与 Milvus 中 chunk 的映射关系。
每篇文档对应多条 Milvus chunk 记录，通过 parent_doc_id 关联。
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "knowledge.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            doc_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            full_content TEXT NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 1,
            char_count INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.commit()


def add_document(doc_id: str, title: str, full_content: str, chunk_count: int, source: str = "manual"):
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        "INSERT INTO documents (doc_id, title, full_content, chunk_count, char_count, source) VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, title, full_content, chunk_count, len(full_content), source),
    )
    conn.commit()
    conn.close()


def delete_document(doc_id: str):
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    conn.commit()
    conn.close()


def get_document(doc_id: str) -> dict | None:
    conn = _get_conn()
    _ensure_table(conn)
    row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["doc_id"],
        "title": row["title"],
        "content": row["full_content"],
        "chunk_count": row["chunk_count"],
        "char_count": row["char_count"],
        "source": row["source"],
        "created_at": row["created_at"],
    }


def list_documents() -> list[dict]:
    conn = _get_conn()
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT doc_id, title, chunk_count, char_count, source, created_at FROM documents ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["doc_id"],
            "title": r["title"],
            "chunk_count": r["chunk_count"],
            "char_count": r["char_count"],
            "source": r["source"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_stats() -> dict:
    conn = _get_conn()
    _ensure_table(conn)
    row = conn.execute(
        "SELECT COUNT(*) as total, COALESCE(SUM(char_count), 0) as total_chars FROM documents"
    ).fetchone()
    conn.close()
    return {"total_documents": row["total"], "total_characters": row["total_chars"]}
