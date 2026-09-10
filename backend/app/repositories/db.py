"""SQLite 元数据仓储。

职责边界刻意收窄：**只存结构化元数据与对话历史**，不存向量。
向量归 Chroma 管，元数据归 SQLite 管，两者通过 ``doc_id`` 关联。

之所以不引入 SQLAlchemy：本项目的元数据操作只有 8 个简单语句，
用标准库 sqlite3 可以省掉一个重量级依赖和一套 ORM 心智负担。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.schemas.knowledge import DocumentInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    source       TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '其他',
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新会话',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    has_image   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class MetadataStore:
    """SQLite 元数据仓储。每次操作开独立连接，避免跨线程共享连接的问题。"""

    def __init__(self, db_path: Path) -> None:
        """初始化并建表。

        Args:
            db_path: SQLite 文件路径。
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        logger.info("元数据库就绪: {}", self.db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """打开连接并保证提交/回滚。"""
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 文档登记
    # ------------------------------------------------------------------

    def upsert_document(self, info: DocumentInfo) -> None:
        """登记或更新一份文档（doc_id 相同时覆盖，保证重复上传幂等）。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO documents (doc_id, title, source, category, chunk_count,
                                       size_bytes, created_at)
                VALUES (:doc_id, :title, :source, :category, :chunk_count,
                        :size_bytes, :created_at)
                ON CONFLICT(doc_id) DO UPDATE SET
                    title=excluded.title,
                    category=excluded.category,
                    chunk_count=excluded.chunk_count,
                    size_bytes=excluded.size_bytes,
                    created_at=excluded.created_at
                """,
                info.model_dump(),
            )

    def list_documents(self) -> list[DocumentInfo]:
        """列出全部文档，按入库时间倒序。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM documents ORDER BY created_at DESC"
            ).fetchall()
        return [DocumentInfo(**dict(row)) for row in rows]

    def get_document(self, doc_id: str) -> DocumentInfo | None:
        """按 doc_id 查询文档登记信息。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        return DocumentInfo(**dict(row)) if row else None

    def delete_document(self, doc_id: str) -> bool:
        """删除文档登记信息，返回是否命中。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 会话与消息
    # ------------------------------------------------------------------

    def ensure_session(self, session_id: str, title: str = "新会话") -> None:
        """确保会话存在（不存在则创建）。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, title, now, now),
            )

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出全部会话，按最近活跃倒序。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 200"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_message(
        self, session_id: str, role: str, content: str, has_image: bool = False
    ) -> None:
        """追加一条消息。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, has_image, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, content, int(has_image), now),
            )

    def list_messages(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """按时间正序取出会话消息（用于历史回放与上下文拼装）。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content, has_image, created_at FROM messages
                WHERE session_id = ? ORDER BY id DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def delete_session(self, session_id: str) -> None:
        """删除会话及其全部消息。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
