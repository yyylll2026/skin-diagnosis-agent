"""真实 PostgreSQL 上的端到端验证。

**默认跳过**：只有设置了 ``SKIN_TEST_POSTGRES_URL`` 才会运行。

推荐用项目自带的 compose 起库（账号 / 端口与 ``docker-compose.yml`` 一致）：

    docker compose --profile postgres up -d postgres
    SKIN_TEST_POSTGRES_URL=postgresql+psycopg://skin:skin@127.0.0.1:5432/skin \\
        pytest tests/test_postgres_integration.py -q

不想用 compose 也可以单独 ``docker run``（注意端口别和 compose 冲突）：

    docker run -d --name skin-pg -p 5432:5432 \\
        -e POSTGRES_PASSWORD=skin -e POSTGRES_USER=skin -e POSTGRES_DB=skin \\
        postgres:16-alpine
    SKIN_TEST_POSTGRES_URL=postgresql+psycopg://skin:skin@127.0.0.1:5432/skin \\
        pytest tests/test_postgres_integration.py -q

（Windows cmd 用 ``set VAR=...``，PowerShell 用 ``$env:VAR="..."``。）

## 为什么值得单独一个文件

``tests/test_database.py`` 已经把「方言可移植性」在离线层面查得很细
（每条 SQL 都被录下来检查占位符、用离线模式生成 PG 的真实 DDL）。
但有两件事**只有连上真库才能验证**：

1. **占位符改写是否真的被 psycopg 接受** —— 改写逻辑是我们自己写的，
   离线编译通过不代表驱动认；
2. **方言分支的实际语义** —— ``LEAST`` 的取小结果、``ILIKE`` 的大小写不敏感，
   这些是运行时行为，编译期看不出来。

所以这个文件跑的是和 SQLite 完全相同的那套演练（``run_repository_workout``），
外加几个专门打方言分支的断言。
"""

from __future__ import annotations

import os

import pytest

from app.repositories.db import MetadataStore
from tests.repository_workout import NOW, run_repository_workout

PG_URL = os.environ.get("SKIN_TEST_POSTGRES_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="未设置 SKIN_TEST_POSTGRES_URL，跳过真实 PostgreSQL 集成测试",
)

# 删表顺序：先删被引用的（这里没有外键，但保持与 downgrade 一致的倒序）
_TABLES = (
    "audit_logs",
    "auth_sessions",
    "users",
    "analysis_tasks",
    "health_records",
    "clarifications",
    "messages",
    "sessions",
    "documents",
)


def _reset_schema(store: MetadataStore) -> None:
    """把库清空，保证每个用例都从干净状态开始。

    连 ``alembic_version`` 一起删：否则下一次 ``MetadataStore()`` 会认为
    迁移已应用，从而跳过建表。
    """
    with store.database.connect() as connection:
        for table in _TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        connection.execute("DROP TABLE IF EXISTS alembic_version")


@pytest.fixture
def pg_store():
    """一套干净的 PostgreSQL 仓储。"""
    store = MetadataStore(PG_URL)
    _reset_schema(store)
    store = MetadataStore(PG_URL)
    try:
        yield store
    finally:
        store.database.dispose()


def test_schema_is_created_from_scratch(pg_store: MetadataStore) -> None:
    """空库上跑迁移应当建出全部表。"""
    tables = pg_store.database.table_names()
    assert {
        "documents",
        "sessions",
        "messages",
        "clarifications",
        "health_records",
        "analysis_tasks",
        "users",
        "auth_sessions",
        "audit_logs",
    } <= tables


def test_initialisation_is_idempotent(pg_store: MetadataStore) -> None:
    """重复初始化不应报错（每次启动都会跑一遍迁移）。"""
    for _ in range(2):
        MetadataStore(PG_URL)


def test_full_repository_workout_runs_on_postgres(pg_store: MetadataStore) -> None:
    """与 SQLite 完全相同的那套演练——覆盖仓储层每个方法。"""
    run_repository_workout(pg_store)


def test_qmark_placeholders_are_accepted_by_psycopg(pg_store: MetadataStore) -> None:
    """占位符改写必须真的被驱动接受。

    这是整个「同一套 SQL 跑两种库」方案的地基：仓储层写的是 ``?``，
    交给 psycopg 之前会被改写成具名形式。地基塌了，所有查询都会挂。
    """
    pg_store.add_audit_log("smoke", actor="tester", detail="含中文与 % 号", created_at=NOW)
    logs = pg_store.list_audit_logs(action="smoke")
    assert len(logs) == 1
    assert logs[0]["detail"] == "含中文与 % 号"


def test_user_search_is_case_insensitive_on_postgres(pg_store: MetadataStore) -> None:
    """PostgreSQL 的 LIKE 区分大小写，必须走 ILIKE 才能维持 SQLite 的搜索体验。"""
    pg_store.create_user("u-case", "AliceCase", "hash", "customer", "Alice", NOW)

    assert len(pg_store.list_users("alicecase")) == 1
    assert len(pg_store.list_users("ALICECASE")) == 1


def test_interrupted_task_recovery_clamps_progress_on_postgres(
    pg_store: MetadataStore,
) -> None:
    """任务恢复要把 progress 压到 95——PostgreSQL 上走的是 LEAST 而不是 MIN。"""
    pg_store.create_analysis_task(
        {
            "task_id": "t-clamp",
            "user_id": "u-1",
            "session_id": "s-1",
            "status": "running",
            "progress": 100,
            "current_step": "生成中",
            "created_at": NOW,
            "updated_at": NOW,
            "started_at": NOW,
            "finished_at": "",
        }
    )

    assert pg_store.recover_interrupted_analysis_tasks(NOW) == 1

    task = pg_store.get_analysis_task("t-clamp", "u-1")
    assert task is not None
    assert task["status"] == "failed"
    assert task["progress"] == 95


def test_authentication_roundtrip_on_postgres(pg_store: MetadataStore) -> None:
    """鉴权服务在 PostgreSQL 上也要能完整跑通注册 → 登录 → 解析会话。"""
    from app.config import Settings
    from app.services.auth_service import AuthService

    service = AuthService(Settings(_env_file=None, auth_enabled=True), pg_store)

    user, token = service.register("pg_user", "passw0rd1")
    assert user.username == "pg_user"

    logged_in, login_token = service.login("pg_user", "passw0rd1", client_ip="127.0.0.1")
    assert logged_in.user_id == user.user_id

    # 两条会话都要能解析出来，说明 token 哈希落库与回查在 PG 上行为一致
    assert service.resolve_session(token) is not None
    resolved = service.resolve_session(login_token)
    assert resolved is not None
    assert resolved.username == "pg_user"

    # 唯一约束冲突要能翻译成业务异常（PG 抛的是 SQLAlchemy 包装过的 IntegrityError）
    from app.core.errors import UserConflictError

    with pytest.raises(UserConflictError):
        service.register("pg_user", "passw0rd2")
