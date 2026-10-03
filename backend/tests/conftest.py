"""pytest 公共夹具。

关键点：在导入 ``app`` 之前就把配置指向临时目录。
否则测试会污染真实开发库（``backend/data/``），跑几次测试后
会留下一堆测试文档与向量，非常难清理。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="skin-test-"))
os.environ.setdefault("SKIN_SQLITE_PATH", str(_TEST_ROOT / "app.db"))
os.environ.setdefault("SKIN_CHROMA_DIR", str(_TEST_ROOT / "chroma"))
# 测试不依赖 Ollama；需要向量的用例走 Chroma 内置向量函数
os.environ.setdefault("SKIN_EMBEDDING_BACKEND", "chroma_default")
# 固定鉴权关闭。模块级的 app 会读 backend/.env，而开发者本机可能把鉴权开着
# （auth_enabled=true），那会让 test_api.py 的每个请求都变成 401。
# 用 setdefault 而非 setdefault 之外的写法：环境变量优先级高于 .env 文件，
# 所以这一行足以覆盖住本机配置。需要验证鉴权的用例自己构造开启鉴权的 app
# （见 test_auth_api.py），不依赖这里的全局设置。
os.environ.setdefault("SKIN_AUTH_ENABLED", "false")

from app.config import Settings, get_settings  # noqa: E402

get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """返回一套指向临时目录的隔离配置。"""
    return Settings(
        _env_file=None,  # 显式忽略 .env，避免本机配置影响断言
        sqlite_path=tmp_path / "test.db",
        chroma_dir=tmp_path / "chroma",
        embedding_backend="chroma_default",
        chunk_size=200,
        chunk_overlap=40,
    )


@pytest.fixture
def clean_settings(settings: Settings) -> Settings:
    """语义化别名：明确表示这是一套不污染本机数据的配置。"""
    return settings


# 需要「真实开启鉴权 + 独立数据库」的用例共用这套夹具。
# 之所以要 cache_clear 一整串工厂：单例是进程级的，前一个用例把
# MetadataStore 指到别的库之后，不清缓存就会串数据。
_AUTH_SCOPED_FACTORIES = (
    "get_metadata_store",
    "get_analysis_task_service",
    "get_auth_service",
    "get_clarify_service",
    "get_ingest_service",
)


@pytest.fixture
def auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """构造一个真实开启鉴权、且数据落在临时目录的 API 客户端。"""
    monkeypatch.setenv("SKIN_AUTH_ENABLED", "true")
    monkeypatch.setenv("SKIN_SQLITE_PATH", str(tmp_path / "auth_app.db"))
    monkeypatch.setenv("SKIN_CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("SKIN_EMBEDDING_BACKEND", "chroma_default")

    from app import dependencies
    from app.config import get_settings

    def _clear() -> None:
        get_settings.cache_clear()
        for name in _AUTH_SCOPED_FACTORIES:
            getattr(dependencies, name).cache_clear()

    _clear()
    from app.main import create_app

    # 刻意不用 `with TestClient(...)`：进入上下文会触发 lifespan 预热
    # （建 Chroma 集合、编译图、调模型），单测既慢又脆。见 AGENTS.md。
    client = TestClient(create_app())
    yield client
    _clear()
