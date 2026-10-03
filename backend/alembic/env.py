"""Alembic 运行环境。

连接串的优先级：
1. ``config.attributes["sqlalchemy_url"]`` —— 程序内调用（``run_migrations``）走这条，
   能避开 ini 的 ``%`` 插值问题；
2. ``SKIN_DATABASE_URL`` 环境变量 —— 方便在命令行手跑 ``alembic upgrade head``；
3. ``alembic.ini`` 里的 ``sqlalchemy.url`` —— 兜底。

``render_as_batch`` 对 SQLite 开启：SQLite 不支持 ``ALTER COLUMN`` /
``DROP COLUMN``，Alembic 的 batch 模式会走「建新表 → 拷数据 → 换名」的迂回路径。
现在只有一个初始迁移用不到，但下一个人改列类型时会需要它，
开着比事后排查「SQLite 为什么改不了列」划算。
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool


def _database_url() -> str:
    """按优先级解析出要迁移的连接串。"""
    injected = context.config.attributes.get("sqlalchemy_url")
    if injected:
        return str(injected)
    from_env = os.environ.get("SKIN_DATABASE_URL", "").strip()
    if from_env:
        return from_env
    return str(context.config.get_main_option("sqlalchemy.url") or "")


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（``alembic upgrade head --sql``）。"""
    context.configure(
        url=_database_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        output_buffer=context.config.output_buffer,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    engine = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=None,
                render_as_batch=connection.dialect.name == "sqlite",
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
