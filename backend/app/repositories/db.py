"""元数据仓储。

职责边界刻意收窄：**只存结构化元数据与对话历史**，不存向量。
向量归 Chroma 管，元数据归关系库管，两者通过 ``doc_id`` 关联。

## 数据层现状

SQL 全部是显式语句（不引入 ORM），但**不再绑定 SQLite**：
连接与方言差异收敛在 ``database.py``，建表交给 Alembic。
只要 ``SKIN_DATABASE_URL`` 指向 ``postgresql+psycopg://...`` 就能直接跑。

少数语句必须按方言分支，全部集中在本文件里并标注了原因：
- ``MIN(a, b)`` / ``LEAST(a, b)``：PostgreSQL 没有双参数标量 MIN；
- ``LIKE`` / ``ILIKE``：SQLite 的 LIKE 对 ASCII 天然不分大小写，
  PostgreSQL 的 LIKE 区分大小写，要维持同样的搜索体验得用 ILIKE。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.repositories.database import Database, DatabaseConnection, normalize_database_url
from app.schemas.knowledge import DocumentInfo


def dialect_min_expression(column: str, ceiling: int, *, is_sqlite: bool) -> str:
    """构造「取两者较小值」的表达式。

    SQLite 的双参数 ``MIN(a, b)`` 是标量函数，PostgreSQL 的 ``MIN`` 只有聚合形式，
    双参数会直接报「函数 min(integer, integer) 不存在」，得改用 ``LEAST``。

    抽成函数是为了能被单独测到——这类分支如果只写在方法体里，
    除非真连上 PostgreSQL 否则永远验证不到。
    """
    function = "MIN" if is_sqlite else "LEAST"
    return f"{function}({column}, {ceiling})"


def dialect_like_operator(*, is_sqlite: bool) -> str:
    """构造模糊匹配运算符。

    SQLite 的 ``LIKE`` 对 ASCII 天然不区分大小写，PostgreSQL 的 ``LIKE`` 区分。
    想让「搜 admin 能搜到 Admin」在两套库上表现一致，PG 侧必须用 ``ILIKE``。
    """
    return "LIKE" if is_sqlite else "ILIKE"


class MetadataStore:
    """元数据仓储。每次操作开独立连接，避免跨线程共享连接的问题。"""

    def __init__(self, target: Path | str) -> None:
        """初始化连接层，并把库结构升级到最新版本。

        Args:
            target: SQLite 文件路径（向后兼容），或任意 SQLAlchemy 连接串
                （如 ``postgresql+psycopg://user:pw@host:5432/skin``）。

        Raises:
            FileNotFoundError: 打包时漏拷 Alembic 配置或迁移脚本目录。
        """
        self.database = Database(normalize_database_url(target))
        self.database.ensure_schema()

    @contextmanager
    def _connect(self) -> Iterator[DatabaseConnection]:
        """开一个连接并保证提交/回滚。"""
        with self.database.connect() as connection:
            yield connection

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
        """删除会话消息、追问进度及关联后台任务。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM clarifications WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM analysis_tasks WHERE session_id = ?", (session_id,))

    # ------------------------------------------------------------------
    # 用户健康记录
    # ------------------------------------------------------------------

    def create_health_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """创建一条归属指定用户的健康记录。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO health_records (
                    record_id, user_id, recorded_on, body_site, symptoms, severity,
                    notes, created_at, updated_at
                ) VALUES (
                    :record_id, :user_id, :recorded_on, :body_site, :symptoms, :severity,
                    :notes, :created_at, :updated_at
                )
                """,
                record,
            )
        return self.get_health_record(str(record["record_id"]), str(record["user_id"])) or {}

    def list_health_records(
        self, user_id: str, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        """只列出该用户的记录，按发生日期倒序。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM health_records WHERE user_id = ?
                ORDER BY recorded_on DESC, created_at DESC, record_id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_health_records(self, user_id: str) -> list[dict[str, Any]]:
        """不分页读取某用户的全部记录，供本人数据导出使用。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM health_records WHERE user_id = ?
                ORDER BY recorded_on DESC, created_at DESC, record_id DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_health_record(self, record_id: str, user_id: str) -> dict[str, Any] | None:
        """按记录 ID 和归属用户查记录，避免跨用户读取。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM health_records WHERE record_id = ? AND user_id = ?",
                (record_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def list_health_records_since(self, user_id: str, since: str) -> list[dict[str, Any]]:
        """取某用户 ``recorded_on >= since`` 的记录，按时间正序。

        趋势聚合专用：只拉窗口内的数据，不把全部历史读进内存。
        ``recorded_on`` 存的是 ``YYYY-MM-DD``，字符串比较即日期比较。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM health_records WHERE user_id = ? AND recorded_on >= ?
                ORDER BY recorded_on ASC, created_at ASC, record_id ASC
                """,
                (user_id, since),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_health_records(self, user_id: str) -> int:
        """统计某用户的历史记录总数（不限时间窗口）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM health_records WHERE user_id = ?", (user_id,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def update_health_record(
        self, record_id: str, user_id: str, fields: dict[str, Any], updated_at: str
    ) -> bool:
        """更新当前用户的记录；字段名由固定白名单限制。"""
        allowed = {"recorded_on", "body_site", "symptoms", "severity", "notes"}
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self.get_health_record(record_id, user_id) is not None
        updates["updated_at"] = updated_at
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE health_records SET {assignments} WHERE record_id = ? AND user_id = ?",
                (*updates.values(), record_id, user_id),
            )
        return cursor.rowcount > 0

    def delete_health_record(self, record_id: str, user_id: str) -> bool:
        """删除当前用户的一条记录。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM health_records WHERE record_id = ? AND user_id = ?",
                (record_id, user_id),
            )
        return cursor.rowcount > 0

    def delete_user_health_records(self, user_id: str) -> int:
        """清除被删除用户的健康记录，避免个人资料成为孤儿数据。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM health_records WHERE user_id = ?", (user_id,))
        return cursor.rowcount

    # ------------------------------------------------------------------
    # 异步分析任务
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_analysis_task(row: dict[str, Any]) -> dict[str, Any]:
        data = dict(row)
        try:
            data["result"] = json.loads(data.pop("result_json") or "{}") or None
        except (json.JSONDecodeError, TypeError):
            data["result"] = None
            data.pop("result_json", None)
        return data

    def create_analysis_task(self, task: dict[str, Any]) -> dict[str, Any]:
        """持久化后台任务元数据，不接收或写入图片输入。"""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analysis_tasks (
                    task_id, user_id, session_id, status, progress, current_step,
                    created_at, updated_at, started_at, finished_at
                ) VALUES (
                    :task_id, :user_id, :session_id, :status, :progress, :current_step,
                    :created_at, :updated_at, :started_at, :finished_at
                )
                """,
                task,
            )
        return self.get_analysis_task(str(task["task_id"]), str(task["user_id"])) or {}

    def get_analysis_task(self, task_id: str, user_id: str) -> dict[str, Any] | None:
        """按任务 ID 和归属用户读取任务。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_tasks WHERE task_id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        return self._decode_analysis_task(row) if row else None

    def list_analysis_tasks(
        self, user_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        """只列出当前用户的任务，最新任务优先。"""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM analysis_tasks WHERE user_id = ?
                ORDER BY created_at DESC, task_id DESC LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ).fetchall()
        return [self._decode_analysis_task(row) for row in rows]

    def update_analysis_task(self, task_id: str, fields: dict[str, Any]) -> bool:
        """按白名单更新任务状态及结果。"""
        allowed = {
            "status",
            "progress",
            "current_step",
            "result",
            "error",
            "attempt_count",
            "updated_at",
            "started_at",
            "finished_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return False
        if "result" in updates:
            result = updates.pop("result")
            updates["result_json"] = json.dumps(result or {}, ensure_ascii=False)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE analysis_tasks SET {assignments} WHERE task_id = ?",
                (*updates.values(), task_id),
            )
        return cursor.rowcount > 0

    def recover_interrupted_analysis_tasks(self, updated_at: str) -> int:
        """启动时把上次进程遗留的未完成任务标记为中断，不假装可恢复。"""
        # 双参数取小：SQLite 是 MIN(a, b)，PostgreSQL 没有标量 MIN（MIN 是聚合函数），
        # 得用 LEAST。写错的表现是 PG 上直接报「聚合函数 MIN 参数过多」。
        clamp = dialect_min_expression("progress", 95, is_sqlite=self.database.is_sqlite)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE analysis_tasks
                SET status = 'failed', progress = {clamp},
                    current_step = '服务重启中断',
                    error = '服务重启导致任务中断；出于隐私保护，图片未持久化，请重新提交分析。',
                    updated_at = ?, finished_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (updated_at, updated_at),
            )
        return cursor.rowcount

    def delete_analysis_tasks_for_session(self, session_id: str, user_id: str) -> int:
        """删除会话相关的后台任务。"""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM analysis_tasks WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
        return cursor.rowcount

    def delete_analysis_tasks_for_user(self, user_id: str) -> int:
        """删除用户所有分析任务。"""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM analysis_tasks WHERE user_id = ?", (user_id,))
        return cursor.rowcount

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
        """创建用户。用户名冲突时抛 DBAPI 的 IntegrityError，由服务层翻译成业务异常。"""
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
                # SQLite 的 LIKE 对 ASCII 本就不区分大小写，PostgreSQL 的 LIKE 区分。
                # 想让「搜 admin 能搜到 Admin」在两套库上表现一致，PG 侧必须用 ILIKE。
                like = dialect_like_operator(is_sqlite=self.database.is_sqlite)
                rows = conn.execute(
                    f"""
                    SELECT * FROM users
                    WHERE username {like} ? OR display_name {like} ?
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
        """删除用户、登录会话、健康记录与分析任务；对话历史保留便于审计追溯。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM health_records WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM analysis_tasks WHERE user_id = ?", (user_id,))
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
