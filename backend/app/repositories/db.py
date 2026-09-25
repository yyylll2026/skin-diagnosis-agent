"""SQLite 元数据仓储。

职责边界刻意收窄：**只存结构化元数据与对话历史**，不存向量。
向量归 Chroma 管，元数据归 SQLite 管，两者通过 ``doc_id`` 关联。

之所以不引入 SQLAlchemy：本项目的元数据操作都是单表简单语句（文档登记、会话与消息、
追问进度三组），用标准库 sqlite3 可以省掉一个重量级依赖和一套 ORM 心智负担。
"""

from __future__ import annotations

import json
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
    updated_at  TEXT NOT NULL,
    -- 归属用户。旧库由 _MIGRATIONS 补这一列；空串表示「鉴权关闭时期创建的会话」。
    user_id     TEXT NOT NULL DEFAULT ''
);

-- 注意：这里**不能**建 idx_sessions_user。
-- 索引引用了 user_id，而旧库的 sessions 表没有这一列；`CREATE INDEX` 会在
-- 迁移补列之前执行，于是直接报 "no such column: user_id"。
-- 依赖新列的索引统一放到 _apply_migrations 的最后一步建。

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    has_image   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS clarifications (
    session_id     TEXT PRIMARY KEY,
    collected      TEXT NOT NULL DEFAULT '{}',
    pending_fields TEXT NOT NULL DEFAULT '[]',
    rounds         INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);

-- ============================ 鉴权相关 ============================

CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL DEFAULT '',
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'customer',
    status          TEXT NOT NULL DEFAULT 'active',
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    last_login_at   TEXT NOT NULL DEFAULT ''
);

-- 登录会话。
-- 刻意不叫 sessions：那张表是「对话会话」，两者语义完全不同。
-- 存 token 的 SHA256 而非 token 原文——万一库文件泄露，拿到的哈希无法直接当凭据用。
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_agent   TEXT NOT NULL DEFAULT '',
    client_ip    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT 'ok',
    detail      TEXT NOT NULL DEFAULT '',
    client_ip   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(id DESC);
"""

# 对既有库的补丁式迁移。
# `CREATE TABLE IF NOT EXISTS` 只会建新表，**不会给已存在的表加列**——
# 所以「给 sessions 加 user_id」这类改动必须显式 ALTER，否则老库升级后
# 查询会直接报 no such column。这里做最小化的列存在性检查。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "user_id", "TEXT NOT NULL DEFAULT ''"),
)

# 依赖「迁移新增列」的索引。必须在补列之后创建，否则老库上会因列不存在而报错。
#
# 这个坑值得记一笔：`_SCHEMA` 里的 `CREATE TABLE IF NOT EXISTS` 在老库上是空操作，
# 紧随其后的 `CREATE INDEX ... (新列)` 却会真的执行并失败。
# 换句话说，**只测「全新建库」是测不出这个问题的**，
# 必须针对「老格式库」单独测（见 tests/test_db_migration.py）。
_POST_MIGRATION_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC)",
)


class MetadataStore:
    """SQLite 元数据仓储。每次操作开独立连接，避免跨线程共享连接的问题。"""

    def __init__(self, db_path: Path) -> None:
        """初始化、建表，并对既有库做补齐式迁移。

        Args:
            db_path: SQLite 文件路径。
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._apply_migrations(conn)
        logger.info("元数据库就绪: {}", self.db_path)

    @staticmethod
    def _apply_migrations(conn: sqlite3.Connection) -> None:
        """给已存在的表补上后来新增的列，再建依赖这些列的索引（幂等）。

        顺序不能颠倒：索引引用的列必须先存在。
        """
        for table, column, ddl in _MIGRATIONS:
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:
                continue  # 表还不存在（全新库由 _SCHEMA 建好，含该列）
            if column in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            logger.info("迁移：为 {} 补充列 {}", table, column)

        for statement in _POST_MIGRATION_INDEXES:
            conn.execute(statement)

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
            rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        return [DocumentInfo(**dict(row)) for row in rows]

    def get_document(self, doc_id: str) -> DocumentInfo | None:
        """按 doc_id 查询文档登记信息。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return DocumentInfo(**dict(row)) if row else None

    def delete_document(self, doc_id: str) -> bool:
        """删除文档登记信息，返回是否命中。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # 会话与消息
    # ------------------------------------------------------------------

    def ensure_session(self, session_id: str, title: str = "新会话", user_id: str = "") -> None:
        """确保会话存在（不存在则创建），并记录归属用户。

        `user_id` **只在插入时写入**，冲突时不覆盖：同一 session_id 后续请求
        可能来自不同身份（例如鉴权开关切换），把归属改成后来者会造成历史串号。
        """
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, title, created_at, updated_at, user_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (session_id, title, now, now, user_id),
            )

    def list_sessions(self, user_id: str | None = None) -> list[dict[str, Any]]:
        """列出会话，按最近活跃倒序。

        Args:
            user_id: 传入时只返回该用户的会话（客户端）；为 None 时返回全部（管理员）。
        """
        with self._connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 200"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200",
                    (user_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_session_owner(self, session_id: str) -> str | None:
        """取会话的归属用户；会话不存在返回 None，存在但无归属返回空串。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return None if row is None else str(row["user_id"] or "")

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
        """删除会话及其全部消息与追问进度。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM clarifications WHERE session_id = ?", (session_id,))

    # ------------------------------------------------------------------
    # 追问澄清进度
    #
    # 用「会话 + 一行 JSON」存追问状态，而不是加一堆列：字段集合会随维度设计
    # 演进（加一个「既往史」维度就要加一列），JSON 让仓储层不必跟着改表。
    # 代价是无法按维度检索——但追问状态永远是按 session_id 整取的，不需要。
    # ------------------------------------------------------------------

    def get_clarification(self, session_id: str) -> dict[str, Any] | None:
        """读取某会话的追问进度；不存在返回 None。

        Returns:
            ``{"session_id", "collected", "pending_fields", "rounds", "updated_at"}``，
            其中 ``collected`` 与 ``pending_fields`` 已反序列化为 Python 对象。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM clarifications WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None

        data = dict(row)
        # JSON 列可能被外部工具改坏，解析失败时回落到空值而不是抛异常——
        # 追问进度损坏不应该让整轮对话失败
        for key, fallback in (("collected", {}), ("pending_fields", [])):
            try:
                data[key] = json.loads(data.get(key) or ("{}" if fallback == {} else "[]"))
            except (json.JSONDecodeError, TypeError):
                logger.warning("追问进度字段 {} 解析失败，已重置：{}", key, data.get(key))
                data[key] = fallback
        return data

    def upsert_clarification(
        self,
        session_id: str,
        collected: str,
        pending_fields: str,
        rounds: int,
        updated_at: str,
    ) -> None:
        """写入或更新追问进度（入参为已序列化的 JSON 字符串）。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO clarifications (session_id, collected, pending_fields, rounds, updated_at)
                VALUES (:session_id, :collected, :pending_fields, :rounds, :updated_at)
                ON CONFLICT(session_id) DO UPDATE SET
                    collected=excluded.collected,
                    pending_fields=excluded.pending_fields,
                    rounds=excluded.rounds,
                    updated_at=excluded.updated_at
                """,
                {
                    "session_id": session_id,
                    "collected": collected,
                    "pending_fields": pending_fields,
                    "rounds": rounds,
                    "updated_at": updated_at,
                },
            )

    def delete_clarification(self, session_id: str) -> None:
        """清除追问进度（信息补齐、被旁路或转入兜底作答之后）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM clarifications WHERE session_id = ?", (session_id,))

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str,
        role: str,
        display_name: str = "",
        created_at: str = "",
    ) -> dict[str, Any]:
        """创建用户。用户名冲突时抛 sqlite3.IntegrityError，由服务层翻译成业务异常。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, password_hash, role, display_name,
                                   status, created_at)
                VALUES (?, ?, ?, ?, ?, 'active', ?)
                """,
                (user_id, username, password_hash, role, display_name, created_at),
            )
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        """按 user_id 查用户。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        """按用户名查用户（登录入口）。"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def list_users(self, keyword: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """列出用户（可按用户名/显示名模糊搜索），按创建时间倒序。"""
        with self._connect() as conn:
            if keyword.strip():
                pattern = f"%{keyword.strip()}%"
                rows = conn.execute(
                    """
                    SELECT * FROM users
                    WHERE username LIKE ? OR display_name LIKE ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (pattern, pattern, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM users ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def update_user(self, user_id: str, fields: dict[str, Any]) -> bool:
        """局部更新用户字段，返回是否命中。

        列名来自服务层白名单，不做拼接防注入处理之外的宽容——
        这里显式限定允许的列，避免把请求体字段直接拼进 SQL。
        """
        allowed = {
            "display_name",
            "role",
            "status",
            "password_hash",
            "failed_attempts",
            "locked_until",
            "last_login_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return False
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE users SET {assignments} WHERE user_id = ?",
                (*updates.values(), user_id),
            )
        return cursor.rowcount > 0

    def delete_user(self, user_id: str) -> bool:
        """删除用户，并连带清掉其登录会话（对话历史保留，便于审计追溯）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            cursor = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0

    def count_active_admins(self, exclude_user_id: str = "") -> int:
        """统计启用状态的管理员人数（用于保护「最后一个管理员」）。

        Args:
            exclude_user_id: 排除该用户（判断「改动他之后还剩几个」时用）。
        """
        sql = "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'"
        params: tuple[Any, ...] = ()
        if exclude_user_id:
            sql += " AND user_id != ?"
            params = (exclude_user_id,)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # 登录会话（auth_sessions）
    # ------------------------------------------------------------------

    def create_auth_session(
        self,
        token_hash: str,
        user_id: str,
        created_at: str,
        expires_at: str,
        user_agent: str = "",
        client_ip: str = "",
    ) -> None:
        """登记一条登录会话。只存 token 的哈希，不存原文。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at,
                                           last_seen_at, user_agent, client_ip)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, created_at, expires_at, created_at, user_agent, client_ip),
            )

    def get_auth_session(self, token_hash: str) -> dict[str, Any] | None:
        """按 token 哈希取登录会话。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def touch_auth_session(self, token_hash: str, expires_at: str, last_seen_at: str) -> None:
        """滑动续期：刷新过期时间与最后活跃时间。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE auth_sessions SET expires_at = ?, last_seen_at = ? WHERE token_hash = ?",
                (expires_at, last_seen_at, token_hash),
            )

    def delete_auth_session(self, token_hash: str) -> bool:
        """删除单条登录会话（登出）。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))
        return cursor.rowcount > 0

    def delete_user_auth_sessions(self, user_id: str) -> int:
        """踢掉某用户的全部登录会话（改密、禁用、删除、强制下线时调用）。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
        return cursor.rowcount

    def purge_expired_auth_sessions(self, now: str) -> int:
        """清理已过期的登录会话，避免表无限增长。

        在登录时顺手调用即可，不需要定时任务——过期会话的唯一作用就是占空间。
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        return cursor.rowcount

    def list_user_auth_sessions(self, user_id: str) -> list[dict[str, Any]]:
        """列出某用户的活跃登录会话（管理员排查「谁在线」用）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM auth_sessions WHERE user_id = ? ORDER BY last_seen_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 审计日志
    # ------------------------------------------------------------------

    def add_audit_log(
        self,
        action: str,
        actor: str = "",
        target: str = "",
        result: str = "ok",
        detail: str = "",
        client_ip: str = "",
        created_at: str = "",
    ) -> None:
        """写入一条审计记录。

        审计写入失败**不应该影响主流程**，因此调用方（服务层）会吞掉这里的异常；
        仓储层本身不做兜底，保持职责单一。
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (created_at, actor, action, target, result, detail, client_ip)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (created_at, actor, action, target, result, detail, client_ip),
            )

    def list_audit_logs(self, limit: int = 100, action: str = "") -> list[dict[str, Any]]:
        """列出审计日志（倒序）。"""
        with self._connect() as conn:
            if action.strip():
                rows = conn.execute(
                    "SELECT * FROM audit_logs WHERE action = ? ORDER BY id DESC LIMIT ?",
                    (action.strip(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]
