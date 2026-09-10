"""全局配置。

设计要点：
1. 所有配置集中于此，通过 ``pydantic-settings`` 从 ``.env`` / 环境变量加载，
   环境变量统一带 ``SKIN_`` 前缀，避免与系统里其它服务的变量串味。
2. 路径类配置留空时回落到 ``backend/data``，保证「克隆即用」。
3. 用 ``lru_cache`` 包成单例，FastAPI 的依赖注入与 LangGraph 节点共享同一份配置。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/config.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """应用配置。字段名即环境变量名去掉 ``SKIN_`` 前缀后的小写形式。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="SKIN_",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 应用 ----------------
    app_name: str = "Skin Diagnosis Agent"
    debug: bool = False
    api_prefix: str = "/api/v1"
    # 注意 NoDecode：pydantic-settings v2 默认会把 list/dict 字段当 JSON 解析，
    # 而这里希望支持 `a,b,c` 这种更符合运维直觉的逗号分隔写法（见下方 validator）。
    # 不加 NoDecode 的话，逗号串会在进入 validator 之前就先抛 SettingsError。
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # ---------------- Ollama ----------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    vision_model: str = "qwen2.5vl:7b"
    text_model: str = "qwen2.5vl:7b"
    fast_model: str = "qwen2.5vl:3b"
    # 视觉调用后端：
    #   rest      -> 直连 Ollama /api/chat，可用 format 参数做结构化输出约束（推荐，最稳）
    #   langchain -> 走 ChatOllama 的多模态消息（image_url content block），便于统一链式编排
    vision_backend: Literal["rest", "langchain"] = "rest"
    # 关键：Ollama 默认 num_ctx 仅 2048~4096，RAG 长上下文会被静默截断，必须显式抬高
    num_ctx: int = 8192
    # 单次生成的最大 token 数
    num_predict: int = 1200
    # 模型常驻显存时长，避免每次请求都重新加载模型（本地部署体验的关键）
    keep_alive: str = "30m"

    # ---------------- Embedding ----------------
    embedding_backend: Literal["ollama", "hf", "chroma_default"] = "ollama"
    embedding_model: str = "bge-m3"
    embedding_hf_model: str = "BAAI/bge-small-zh-v1.5"

    # ---------------- 向量库 ----------------
    chroma_collection: str = "skin_kb"
    chroma_dir: Path | None = None

    # ---------------- 分块 ----------------
    chunk_size: int = 600
    chunk_overlap: int = 100

    # ---------------- 检索 ----------------
    retrieval_top_k: int = 8
    rerank_top_n: int = 4
    vector_weight: float = 0.6
    bm25_weight: float = 0.4
    rrf_k: int = 60
    min_relevance: float = 0.15
    rerank_enabled: bool = False

    # ---------------- 图像 ----------------
    max_image_mb: float = 8.0
    image_max_side: int = 1280
    image_jpeg_quality: int = 88

    # ---------------- 会话 ----------------
    session_history_turns: int = 6

    # ---------------- 超时 ----------------
    llm_timeout: float = 180.0
    vision_timeout: float = 300.0

    # ---------------- 持久化 ----------------
    sqlite_path: Path | None = None
    # 数据根目录。留空则用 backend/data（本地开发）。
    # 容器部署时必须指向挂载的持久化卷，否则容器重启后向量库、会话历史、
    # 上传文件全部丢失（魔搭创空间对应 /mnt/workspace）。
    data_root: Path | None = None
    # 前端构建产物目录。存在时 FastAPI 会把它挂到 "/" 上同源托管，
    # 这样前后端只暴露一个端口（魔搭创空间只转发单一入口）。
    web_dir: Path | None = None

    # ================= 校验 =================

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """允许用逗号分隔的字符串书写 CORS 白名单。"""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    # ================= 派生路径 =================

    @computed_field  # type: ignore[prop-decorator]
    @property
    def data_dir(self) -> Path:
        """数据根目录，存放向量库、上传文件与元数据库。

        优先取 ``SKIN_DATA_ROOT``；未配置时回落到 ``backend/data``，
        保证本地开发「克隆即用」。
        """
        path = self.data_root or (BACKEND_DIR / "data")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @computed_field  # type: ignore[prop-decorator]
    @property
    def docs_dir(self) -> Path:
        """知识库原始文档目录。"""
        path = self.data_dir / "docs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_dir(self) -> Path:
        """运行时上传的文档与图片落盘目录。"""
        path = self.data_dir / "uploads"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_chroma_dir(self) -> Path:
        """Chroma 持久化目录。"""
        path = self.chroma_dir or (self.data_dir / "chroma")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_sqlite_path(self) -> Path:
        """SQLite 元数据库路径（文档登记表 / 会话历史）。"""
        return self.sqlite_path or (self.data_dir / "app.db")

    @property
    def resolved_web_dir(self) -> Path:
        """前端构建产物目录（不自动创建；不存在表示纯 API 模式）。"""
        return self.web_dir or (BACKEND_DIR / "web")

    @property
    def ollama_api(self) -> str:
        """Ollama 原生 REST 接口基址（不带 /v1）。"""
        return self.ollama_base_url.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return Settings()
