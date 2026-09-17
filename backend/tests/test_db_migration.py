"""数据库迁移测试。

## 为什么单独一个文件

`MetadataStore.__init__` 里做两件事：跑 `_SCHEMA`（全是 `CREATE ... IF NOT EXISTS`）
和跑 `_apply_migrations`（给人已存在的表补列）。

**只测「全新建库」是测不出迁移问题的。** 全新库上 `CREATE TABLE` 会带上所有新列，
一切正常；而老库上 `CREATE TABLE IF NOT EXISTS` 是空操作，紧随其后任何引用新列的
语句（比如建索引）都会直接报 `no such column`。

这个文件就是专门构造「老格式库」来覆盖那条路径——
第一版实现把 `CREATE INDEX ... ON sessions(user_id)` 放在 `_SCHEMA` 里，
在全新库上全绿、在老库上直接崩，正是被这里抓出来的。
"""

from __future__ import annotations

import sqlite3

from app.repositories.db import MetadataStore

# 迁移之前的历史格式：sessions 表没有 user_id 列
_LEGACY_SCHEMA = """
CREATE TABLE documents (
    doc_id       TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    source       TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '其他',
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新会话',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    has_image   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""


def _make_legacy_db(path) -> None:
    """造一个「升级前」的库，并塞进一条历史会话。"""
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO sessions (session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        ("s-legacy", "历史会话", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()


def test_migration_adds_user_id_to_legacy_sessions_table(tmp_path) -> None:
    """打开老库时应当自动补上 user_id 列，且不报错。"""
    db_path = tmp_path / "legacy.db"
    _make_legacy_db(db_path)

    # 构造即完成迁移；本用例关心的是「不抛异常 + 列被补上」
    MetadataStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "user_id" in columns


def test_legacy_rows_get_empty_owner(tmp_path) -> None:
    """历史会话的归属应为空串，且仍可正常读取。"""
    db_path = tmp_path / "legacy2.db"
    _make_legacy_db(db_path)

    store = MetadataStore(db_path)

    assert store.get_session_owner("s-legacy") == ""
    # 空归属不应该挡住访问（否则所有老会话都打不开了）
    from app.config import Settings
    from app.schemas.auth import Role, UserInfo, UserStatus
    from app.services.auth_service import AuthService

    settings = Settings(_env_file=None, sqlite_path=db_path, auth_enabled=True)
    service = AuthService(settings, store)
    stranger = UserInfo(user_id="u-x", username="x", role=Role.CUSTOMER, status=UserStatus.ACTIVE)
    service.ensure_session_access("s-legacy", stranger)  # 不抛异常即通过


def test_migration_is_idempotent(tmp_path) -> None:
    """重复初始化不应报错（每次启动都会跑一遍迁移）。"""
    db_path = tmp_path / "legacy3.db"
    _make_legacy_db(db_path)

    for _ in range(3):
        MetadataStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sessions)")]
    # 列不应被重复添加
    assert columns.count("user_id") == 1


def test_fresh_db_has_user_id_and_index(tmp_path) -> None:
    """全新库同样应该有 user_id 列与索引（由迁移步骤统一创建）。"""
    db_path = tmp_path / "fresh.db"
    MetadataStore(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(sessions)")}
    assert "user_id" in columns
    assert "idx_sessions_user" in indexes


def test_auth_tables_are_created(tmp_path) -> None:
    """鉴权相关的三张表都要建出来。"""
    db_path = tmp_path / "auth_tables.db"
    MetadataStore(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"users", "auth_sessions", "audit_logs"} <= tables


def test_legacy_chat_sessions_still_readable(tmp_path) -> None:
    """升级后老会话能正常列出（管理员视角看全部）。"""
    db_path = tmp_path / "legacy4.db"
    _make_legacy_db(db_path)

    store = MetadataStore(db_path)
    store.add_message("s-legacy", "user", "升级前的一条消息")

    all_sessions = store.list_sessions()  # 管理员：不带过滤
    assert any(item["session_id"] == "s-legacy" for item in all_sessions)

    messages = store.list_messages("s-legacy")
    assert messages[0]["content"] == "升级前的一条消息"
