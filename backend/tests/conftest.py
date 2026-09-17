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
