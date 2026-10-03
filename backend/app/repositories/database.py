"""方言无关的数据库连接层。

## 为什么需要这一层

元数据仓储（``db.py``）里全是显式 SQL，这些 SQL 原本是照着 SQLite 写的：
``?`` 占位符、``PRAGMA table_info``、``CREATE TABLE ... AUTOINCREMENT``。
要让同一套仓储跑在 PostgreSQL 上，有三条路：

1. 换成 SQLAlchemy ORM —— 改动面最大，且把「显式 SQL」这个刻意的选择推翻；
2. 每个方法写两份 SQL —— 维护成本翻倍，且必然漂移；
3. **保留显式 SQL，把方言差异收敛到一个薄适配层** —— 本模块的做法。

具体收敛了三件事：

- **占位符**：SQLite 用 ``?``，其余驱动用 ``%s`` / ``%(name)s``。
  这里把 ``?`` 统一改写成 SQLAlchemy 的具名形式 ``:p0``，由 SQLAlchemy
  按目标方言翻译，仓储层的 SQL 一个字都不用改。
- **连接与事务**：每次操作开独立连接、异常回滚，行为与原来的 sqlite3 版本一致。
- **建表与迁移**：交给 Alembic（见 ``alembic/versions/``），
  运行时不再执行任何 DDL——schema 只有一个来源。

## 与「不引入 SQLAlchemy」的取舍

原注释说得很对：本项目都是单表简单语句，标准库 sqlite3 就够。但只要多一个
PostgreSQL 目标，手写两套方言的成本立刻超过引入 SQLAlchemy 的成本；
而这里只用它的 **Core（连接 + 方言 + 迁移）**，不用 ORM，
没有引入实体映射、会话生命周期那套心智负担。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection as SAConnection
from sqlalchemy.engine import Engine

from app.config import BACKEND_DIR

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_SCRIPT_DIR = BACKEND_DIR / "alembic"

# 建库时 SQLite 需要的连接参数：
# - check_same_thread=False：连接池会在不同线程间复用连接，而 FastAPI 的同步
#   依赖跑在线程池里，不关掉这个检查会随机报「SQLite objects created in a thread
#   can only be used in that same thread」。
# - timeout：写锁竞争时的等待时长，与旧实现保持一致。
_SQLITE_CONNECT_ARGS: dict[str, Any] = {"check_same_thread": False, "timeout": 10.0}


def normalize_database_url(target: Path | str) -> str:
    """把「文件路径」或「连接串」统一成 SQLAlchemy 的 URL。

    允许传路径是为了向后兼容：既有代码与测试大量使用
    ``MetadataStore(tmp_path / "x.db")``，不该因为引入 PostgreSQL 就全部改签名。
    """
    if isinstance(target, Path):
        return f"sqlite:///{target.as_posix()}"
    value = str(target).strip()
    if not value:
        raise ValueError("数据库路径/URL 不能为空")
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).as_posix()}"


def translate_placeholders(sql: str) -> str:
    """把 ``?`` 占位符改写成 ``:p0`` / ``:p1`` 具名形式。

    刻意做成「认识字符串字面量」的扫描器而不是一个正则替换：
    一旦哪天有人写了 ``WHERE note = '?'``，正则版本会把它改坏，
    而这类 bug 只有在特定数据下才会暴露，排查代价极高。

    转义规则遵循 SQL 标准：字面量里的 ``''`` 表示一个单引号，不结束字面量。
    """
    out: list[str] = []
    index = 0
    in_string = False
    position = 0
    length = len(sql)

    while position < length:
        char = sql[position]

        if in_string:
            out.append(char)
            if char == "'":
                if position + 1 < length and sql[position + 1] == "'":
                    out.append("'")
                    position += 2
                    continue
                in_string = False
            position += 1
            continue

        if char == "'":
            in_string = True
            out.append(char)
            position += 1
            continue

        if char == "?":
            out.append(f":p{index}")
            index += 1
            position += 1
            continue

        out.append(char)
        position += 1

    return "".join(out)


def _prepare(sql: str, params: Any) -> tuple[str, dict[str, Any] | None]:
    """把 (SQL, 参数) 整理成 SQLAlchemy ``text()`` 能吃的形式。"""
    if "?" not in sql:
        # 已经是具名参数写法（:doc_id 之类），原样交给 SQLAlchemy
        return sql, (dict(params) if params else None)

    statement = translate_placeholders(sql)
    if params is None:
        return statement, None
    if isinstance(params, dict):
        raise TypeError("使用 ? 占位符时参数必须是序列，不能是字典")
    return statement, {f"p{index}": value for index, value in enumerate(params)}


class QueryResult:
    """查询结果的统一外观：行是字典，``rowcount`` 随时可读。

    **结果集在构造时就取完**，这是刻意的：仓储层有若干写法是
    「在 ``with`` 块里 execute、出了块再读 ``rowcount``」，
    而连接出块即归还连接池。若把 rowcount 或行做成惰性读取，
    这些调用会在连接已关闭之后才去碰游标，表现为随机报错。
    取完再返回，调用方怎么写都不会踩到。
    """

    def __init__(self, result: Any) -> None:
        try:
            self.rowcount = int(result.rowcount)
        except Exception:  # noqa: BLE001 - 某些驱动不支持，退化为未知
            self.rowcount = -1
        try:
            self._rows: list[dict[str, Any]] = [dict(row) for row in result.mappings().all()]
        except Exception:  # noqa: BLE001 - DDL 等语句没有结果集
            self._rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        """返回全部行（已取完，可重复调用）。"""
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        """返回第一行，没有则返回 None。"""
        return self._rows[0] if self._rows else None


class DatabaseConnection:
    """对 SQLAlchemy ``Connection`` 的薄包装，屏蔽占位符差异。"""

    def __init__(self, connection: SAConnection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Any = ()) -> QueryResult:
        """执行一条语句。

        Args:
            sql: SQL 文本，占位符可以写 ``?``（自动改写）或 ``:name``。
            params: 与占位符对应的序列或字典。

        Returns:
            已取完结果集的 :class:`QueryResult`。
        """
        statement, values = _prepare(sql, params)
        if values:
            return QueryResult(self._connection.execute(text(statement), values))
        return QueryResult(self._connection.execute(text(statement)))


class Database:
    """持有引擎、按方言提供连接。"""

    def __init__(self, url: str) -> None:
        """初始化引擎。

        Args:
            url: SQLAlchemy 连接串，如 ``sqlite:///app.db`` 或
                ``postgresql+psycopg://user:pw@host/db``。
        """
        self.url = url
        connect_args = _SQLITE_CONNECT_ARGS if url.startswith("sqlite") else {}
        self._engine: Engine = create_engine(url, future=True, connect_args=connect_args)
        logger.info("元数据库连接就绪: {}", self.safe_url)

    @property
    def dialect(self) -> str:
        """方言名：``sqlite`` / ``postgresql``。"""
        return self._engine.dialect.name

    @property
    def is_sqlite(self) -> bool:
        """当前是否跑在 SQLite 上（少数语句需要按此分支）。"""
        return self.dialect == "sqlite"

    @property
    def safe_url(self) -> str:
        """隐去密码的连接串，用于日志与错误提示。

        不用 ``URL.render_as_string()``：它会把路径里的 ``:`` 做百分号编码，
        日志里出现 ``sqlite:///C%3A/...`` 这种读不懂的形式。
        """
        url = self._engine.url
        if self.is_sqlite:
            return f"sqlite:///{url.database or ''}"
        auth = f"{url.username or ''}@{url.host or ''}" if url.username else (url.host or "")
        port = f":{url.port}" if url.port else ""
        return f"{url.drivername}://{auth}{port}/{url.database or ''}"

    @contextmanager
    def connect(self) -> Iterator[DatabaseConnection]:
        """开一个连接，正常结束提交、异常回滚。"""
        connection = self._engine.connect()
        try:
            yield DatabaseConnection(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ensure_schema(self) -> None:
        """把库结构升级到最新版本（幂等，每次启动都会跑）。"""
        run_migrations(self.url)

    def table_names(self) -> set[str]:
        """当前库里的表名。

        用于诊断（健康检查、部署自检）与测试断言——
        比让调用方去碰内部的 engine 更稳妥。
        """
        from sqlalchemy import inspect

        with self._engine.connect() as connection:
            return set(inspect(connection).get_table_names())

    def dispose(self) -> None:
        """释放连接池（测试与脚本退出时用）。"""
        self._engine.dispose()


def run_migrations(url: str) -> None:
    """对目标库执行 ``alembic upgrade head``。

    刻意**不读 ``alembic.ini``**，而是把配置项在代码里设好：

    - ini 由 configparser 解析，连接串里的 ``%``（URL 编码的密码很常见）
      会被当成插值语法而报错；
    - Alembic 读 ini 时用的是系统 locale 编码，一个非 ASCII 注释就能让
      zh-CN Windows 上的启动直接失败——这类问题只在特定机器上复现，极难排查。

    代价是 ``alembic.ini`` 只在命令行手跑时有用，程序路径完全不依赖它。

    Raises:
        FileNotFoundError: 找不到迁移脚本目录（多为打包时漏拷 ``backend/alembic/``）。
    """
    if not ALEMBIC_SCRIPT_DIR.exists():
        raise FileNotFoundError(
            f"找不到迁移脚本目录 {ALEMBIC_SCRIPT_DIR}；"
            "容器镜像需要把 backend/alembic/ 一并拷贝进去。"
        )

    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_DIR))
    config.attributes["sqlalchemy_url"] = url
    command.upgrade(config, "head")


__all__: Sequence[str] = (
    "ALEMBIC_INI",
    "ALEMBIC_SCRIPT_DIR",
    "Database",
    "DatabaseConnection",
    "QueryResult",
    "normalize_database_url",
    "run_migrations",
    "translate_placeholders",
)
