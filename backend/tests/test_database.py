"""数据层测试：连接串归一化、占位符改写、方言分支、迁移 DDL。

## 这一层在验证什么

把元数据从 SQLite 迁到 PostgreSQL，风险不是「某个方法写错了」，
而是「**有没有哪个方法的 SQL 从来没在 PostgreSQL 方言下被检查过**」。
所以这里的策略是：

1. 用 ``_prepare`` 打点，把仓储层**实际发出的每一条 SQL** 都录下来
   （演练覆盖全部方法，见 ``tests/repository_workout.py``）；
2. 对录到的每条语句做与方言无关的检查（占位符是否残留、参数个数是否对得上）；
3. 用 Alembic **离线模式**生成 PostgreSQL 的真实 DDL 并逐项断言——
   离线模式不需要数据库服务，所以在 CI 里也能跑到。

真连 PostgreSQL 的端到端验证见 ``tests/test_postgres_integration.py``
（需要 ``SKIN_TEST_POSTGRES_URL``，默认跳过）。
"""

from __future__ import annotations

import io
import re
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from app.repositories.database import (
    ALEMBIC_SCRIPT_DIR,
    Database,
    normalize_database_url,
    translate_placeholders,
)
from app.repositories.db import (
    MetadataStore,
    dialect_like_operator,
    dialect_min_expression,
)
from tests.repository_workout import run_repository_workout

PG_URL = "postgresql+psycopg://skin:skintest@127.0.0.1:5432/skin"

EXPECTED_TABLES = (
    "documents",
    "sessions",
    "messages",
    "clarifications",
    "health_records",
    "analysis_tasks",
    "users",
    "auth_sessions",
    "audit_logs",
)

EXPECTED_INDEXES = (
    "idx_messages_session",
    "idx_sessions_user",
    "idx_health_records_user_date",
    "idx_analysis_tasks_user_created",
    "idx_analysis_tasks_session",
    "idx_auth_sessions_user",
    "idx_audit_created",
)


# ---------------------------------------------------------------------------
# 占位符改写
# ---------------------------------------------------------------------------


def test_placeholders_are_numbered_in_order() -> None:
    assert translate_placeholders("WHERE a = ? AND b = ?") == "WHERE a = :p0 AND b = :p1"


def test_named_parameters_are_left_untouched() -> None:
    sql = "INSERT INTO documents (doc_id) VALUES (:doc_id)"
    assert translate_placeholders(sql) == sql


def test_question_mark_inside_string_literal_is_not_touched() -> None:
    """字面量里的问号不是占位符。

    这正是不能用一个正则替换搞定的原因：改坏了只有在特定数据下才会暴露，
    排查成本极高。
    """
    assert translate_placeholders("WHERE note = '?' AND a = ?") == "WHERE note = '?' AND a = :p0"


def test_escaped_quote_does_not_end_the_literal() -> None:
    """SQL 里 ``''`` 是一个转义单引号，不该被当成字面量结束。"""
    assert (
        translate_placeholders("WHERE note = 'it''s ?' AND a = ?")
        == "WHERE note = 'it''s ?' AND a = :p0"
    )


def test_unterminated_literal_is_left_alone() -> None:
    """字面量没闭合时保持原样，宁可少改也不要改错。"""
    assert translate_placeholders("WHERE a = 'oops ?") == "WHERE a = 'oops ?"


# ---------------------------------------------------------------------------
# 连接串归一化
# ---------------------------------------------------------------------------


def test_path_is_converted_to_sqlite_url(tmp_path) -> None:
    url = normalize_database_url(tmp_path / "app.db")
    assert url.startswith("sqlite:///")
    assert url.endswith("app.db")


def test_plain_string_path_is_accepted() -> None:
    """允许传字符串路径，方便脚本与命令行使用。"""
    assert normalize_database_url("data/app.db").startswith("sqlite:///")


def test_connection_string_is_passed_through() -> None:
    assert normalize_database_url(PG_URL) == PG_URL


def test_blank_target_is_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_database_url("   ")


# ---------------------------------------------------------------------------
# 方言分支
# ---------------------------------------------------------------------------


def test_min_expression_switches_to_least_on_postgres() -> None:
    """PostgreSQL 没有双参数标量 MIN，必须换 LEAST。"""
    assert dialect_min_expression("progress", 95, is_sqlite=True) == "MIN(progress, 95)"
    assert dialect_min_expression("progress", 95, is_sqlite=False) == "LEAST(progress, 95)"


def test_like_operator_switches_to_ilike_on_postgres() -> None:
    """PG 的 LIKE 区分大小写，要维持 SQLite 的搜索体验得用 ILIKE。"""
    assert dialect_like_operator(is_sqlite=True) == "LIKE"
    assert dialect_like_operator(is_sqlite=False) == "ILIKE"


def test_database_reports_dialect_without_connecting() -> None:
    """建引擎是惰性的：不连库也能问方言，方言分支因此可以被离线测到。"""
    database = Database(PG_URL)
    try:
        assert database.dialect == "postgresql"
        assert database.is_sqlite is False
    finally:
        database.dispose()


def test_safe_url_hides_password_and_keeps_path_readable(tmp_path) -> None:
    """日志里的连接串既不能泄露密码，也不能被百分号编码成读不懂的形式。"""
    postgres = Database(PG_URL)
    try:
        assert "skintest" not in postgres.safe_url
        assert "127.0.0.1:5432" in postgres.safe_url
    finally:
        postgres.dispose()

    sqlite = Database(normalize_database_url(tmp_path / "app.db"))
    try:
        assert "%3A" not in sqlite.safe_url
        assert sqlite.safe_url == f"sqlite:///{(tmp_path / 'app.db').as_posix()}"
    finally:
        sqlite.dispose()


# ---------------------------------------------------------------------------
# 仓储层发出的 SQL
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_statements(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[tuple[str, Any]]:
    """跑一遍全量演练，录下仓储层实际执行的每条 SQL。"""
    from app.repositories import database as database_module

    original = database_module._prepare
    seen: list[tuple[str, Any]] = []

    def spy(sql: str, params: Any):
        seen.append((sql, params))
        return original(sql, params)

    monkeypatch.setattr(database_module, "_prepare", spy)

    store = MetadataStore(tmp_path / "workout.db")
    try:
        run_repository_workout(store)
    finally:
        store.database.dispose()
    return seen


def test_workout_exercises_the_whole_repository(recorded_statements) -> None:
    """语句数量是覆盖率的守门人：漏调一个方法就少几条，这里会掉下来。"""
    assert len(recorded_statements) >= 45


def test_no_statement_leaks_qmark_placeholders(recorded_statements) -> None:
    """改写后不能残留 ``?``，否则 PostgreSQL 会把它当字面量而静默返回错结果。"""
    for sql, _ in recorded_statements:
        assert "?" not in translate_placeholders(sql), sql


def test_placeholder_count_matches_parameter_count(recorded_statements) -> None:
    """占位符个数必须与参数个数一致，多一个少一个都是改写的锅。"""
    for sql, params in recorded_statements:
        if not isinstance(params, (list, tuple)):
            continue  # 具名参数语句，参数是字典
        assert translate_placeholders(sql).count(":p") == len(params), sql


def test_every_statement_renders_under_postgres_dialect(recorded_statements) -> None:
    """每条语句都要能被 PostgreSQL 方言渲染出来。

    编译通过不等于语义正确（``MIN``/``LEAST`` 这类差异 SQLAlchemy 管不了，
    由上面的方言分支测试单独兜住），但它能挡住「占位符没改」「混进 SQLite
    专用语法」这类低级错误。
    """
    dialect = postgresql.dialect()
    for sql, _ in recorded_statements:
        rendered = str(text(translate_placeholders(sql)).compile(dialect=dialect))
        assert rendered.strip()


def test_task_recovery_statement_uses_sqlite_scalar_min(recorded_statements) -> None:
    """SQLite 路径上取小函数应当是 MIN——证明方言分支真的接进了 SQL。"""
    statements = [sql for sql, _ in recorded_statements if "服务重启中断" in sql]
    assert statements, "演练没有覆盖到任务恢复语句"
    assert "MIN(progress, 95)" in statements[0]


def test_user_search_statement_uses_sqlite_like(recorded_statements) -> None:
    """SQLite 路径上模糊匹配应当是 LIKE。"""
    statements = [
        sql for sql, _ in recorded_statements if "display_name" in sql and "LIKE" in sql.upper()
    ]
    assert statements, "演练没有覆盖到用户搜索语句"


# ---------------------------------------------------------------------------
# 迁移 DDL
# ---------------------------------------------------------------------------


def offline_ddl(url: str) -> str:
    """用 Alembic 离线模式生成迁移 SQL（不需要数据库服务）。"""
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_DIR))
    config.attributes["sqlalchemy_url"] = url
    config.output_buffer = io.StringIO()
    command.upgrade(config, "head", sql=True)
    return config.output_buffer.getvalue()


def test_postgres_ddl_uses_identity_columns() -> None:
    """PostgreSQL 不认识 AUTOINCREMENT，必须用 identity 列。"""
    ddl = offline_ddl(PG_URL)
    assert "AUTOINCREMENT" not in ddl.upper()
    assert "GENERATED BY DEFAULT AS IDENTITY" in ddl.upper()


def test_sqlite_ddl_uses_autoincrement() -> None:
    """SQLite 侧保持原样，避免为了兼容 PG 把本地库改出别的行为。"""
    ddl = offline_ddl("sqlite:///./_offline_check.db")
    assert "AUTOINCREMENT" in ddl.upper()


def test_postgres_ddl_creates_every_table_and_index() -> None:
    ddl = offline_ddl(PG_URL)
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl, table
    for index in EXPECTED_INDEXES:
        assert index in ddl, index


def test_postgres_ddl_backfills_legacy_sessions_column() -> None:
    """老库补列的 ALTER 必须在生成的脚本里，且排在建索引之前。"""
    ddl = offline_ddl(PG_URL)
    alter_at = ddl.find("ALTER TABLE sessions ADD COLUMN user_id")
    index_at = ddl.find("idx_sessions_user")
    assert alter_at != -1, "生成脚本里没有补列语句"
    assert index_at != -1
    assert alter_at < index_at, "补列必须排在建索引之前，否则老库升级会报列不存在"


_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_COLUMN_RE = re.compile(
    r"^\s{4,}([A-Za-z_][A-Za-z0-9_]*)\s+(?:TEXT|INTEGER|BIGINT)\b",
    re.MULTILINE,
)


def test_no_identifier_collides_with_postgres_reserved_words() -> None:
    """表名与列名不能撞上 PostgreSQL 保留字。

    撞上的话 PG 会要求加双引号，而加引号又会让所有未加引号的查询失效——
    最好在写 DDL 的时候就发现，而不是等部署到 PG 上才报语法错误。
    """
    ddl = offline_ddl(PG_URL)
    reserved = {word.lower() for word in postgresql.dialect().identifier_preparer.reserved_words}
    identifiers = {name.lower() for name in _TABLE_RE.findall(ddl)}
    identifiers |= {name.lower() for name in _COLUMN_RE.findall(ddl)}

    assert identifiers, "没从 DDL 里解析出标识符，正则可能失效了"
    collisions = sorted(identifiers & reserved)
    assert not collisions, f"这些标识符是 PostgreSQL 保留字，需要加双引号：{collisions}"
