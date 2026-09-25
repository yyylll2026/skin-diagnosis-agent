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

    # ---------------- 模型后端 ----------------
    #   ollama -> 本机/容器内 Ollama（数据不出机器，需 ≥4GB 内存跑视觉模型）
    #   cloud  -> OpenAI 兼容云端 API（如阿里云百炼）；小内存免费 PaaS 用这个
    llm_backend: Literal["ollama", "cloud"] = "ollama"
    # 云端 OpenAI 兼容基址（默认阿里云百炼 compatible-mode）
    cloud_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    # 同时兼容 DASHSCOPE_API_KEY 环境变量（百炼控制台常用名）
    cloud_api_key: str = ""

    # ---------------- Ollama ----------------
    ollama_base_url: str = "http://127.0.0.1:11434"
    # 本地默认仍是 7b；SKIN_LLM_BACKEND=cloud 时由 effective_* 换成云端模型名，
    # 避免用户忘改模型名时把 `qwen2.5vl:7b` 发给 DashScope 导致 400。
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
    #   cloud -> 走同一套 OpenAI 兼容 /embeddings（百炼 text-embedding-v3）
    embedding_backend: Literal["ollama", "hf", "chroma_default", "cloud"] = "ollama"
    embedding_model: str = "bge-m3"
    embedding_hf_model: str = "BAAI/bge-small-zh-v1.5"
    cloud_embedding_model: str = "text-embedding-v3"

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

    # ---------------- 追问澄清（信息完备性判断） ----------------
    clarify_enabled: bool = True
    # 命中多少个「核心维度」才算信息足够。核心维度共 4 个：
    #   出现时间 / 部位与范围 / 伴随感觉 / 变化趋势
    # 取 3 表示「最多缺 1 项就作答」——既避免信息太少导致回答太泛，
    # 也避免为了凑满 4 项而反复追问惹人烦。
    # 调大 → 追问更积极但轮次更多；调小 → 更快作答但回答更泛。
    clarify_min_dimensions: int = 3
    # 追问轮次上限。达到上限仍不完备时转入「带缺口的兜底作答」：
    # 条件化分析 + 说明缺失项的影响 + 就医提醒，而不是继续追问。
    clarify_max_rounds: int = 2

    # ---------------- 鉴权与权限 ----------------
    # 默认关闭：现有 108 条测试与本地开发都依赖「匿名即可调用」。
    # 关闭时所有请求被视为「合成管理员」，并跳过会话归属校验——
    # 所以开关只影响「是否校验身份」，不改变任何既有接口的业务语义。
    auth_enabled: bool = False
    # 会话 cookie 名与密钥。用 JWT 时才必需，这里保留是因为
    # 将来给 cookie 加签名或引入 CSRF token 都需要一个稳定的密钥源。
    secret_key: str = "change-me-in-production"
    auth_cookie_name: str = "skin_session"
    # 公网 HTTPS 部署必须置 true，否则浏览器不会回传 cookie；
    # 本地 http 下必须保持 false，否则会出现「登录成功但立刻又未登录」。
    auth_cookie_secure: bool = False
    # 绝对超时：从登录那一刻算起最长存活多久，到期强制重登
    auth_session_ttl_hours: float = 12.0
    # 空闲超时：多久无操作即失效（医疗数据场景收紧一些）
    auth_idle_timeout_minutes: float = 120.0
    # 登录失败锁定：连续失败 N 次后锁定 M 分钟
    auth_max_failed_attempts: int = 5
    auth_lockout_minutes: float = 15.0
    # 是否允许自助注册。本部署选择开放注册；
    # 关闭后只能由管理员建号（scripts/user_cli.py 创建初始管理员始终可用）。
    auth_allow_registration: bool = True
    # 密码最小长度。开放注册场景下这是第一道门，不宜过短。
    auth_min_password_length: int = 8

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

    @field_validator("cloud_api_key", mode="before")
    @classmethod
    def _strip_cloud_key(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator(
        "chroma_dir",
        "sqlite_path",
        "data_root",
        "web_dir",
        mode="before",
    )
    @classmethod
    def _empty_path_is_none(cls, v: object) -> object:
        """`.env` 里写成空字符串时按未配置处理，回落到默认 data 目录。

        否则 pydantic 会把 `SKIN_SQLITE_PATH=` 解析成 `Path('.')`，
        sqlite 无法打开当前目录，健康检查直接 500。
        """
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
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

    @property
    def resolved_cloud_api_base(self) -> str:
        """云端 OpenAI 兼容基址（去掉尾部 /）。"""
        return self.cloud_api_base.rstrip("/")

    @property
    def resolved_cloud_api_key(self) -> str:
        """云端 API Key：显式配置优先，其次读 DASHSCOPE_API_KEY。"""
        import os

        key = (self.cloud_api_key or "").strip()
        if key:
            return key
        return (os.environ.get("DASHSCOPE_API_KEY") or "").strip()

    @property
    def effective_vision_model(self) -> str:
        """当前后端实际使用的视觉模型名。"""
        if self.llm_backend != "cloud":
            return self.vision_model
        if self.vision_model.startswith("qwen2.5vl") or self.vision_model.startswith("qwen3-vl"):
            return "qwen-vl-plus"
        return self.vision_model

    @property
    def effective_text_model(self) -> str:
        """当前后端实际使用的文本模型名。"""
        if self.llm_backend != "cloud":
            return self.text_model
        if self.text_model.startswith("qwen2.5vl") or ":" in self.text_model:
            return "qwen-plus"
        return self.text_model

    @property
    def effective_fast_model(self) -> str:
        """当前后端实际使用的轻量模型名。"""
        if self.llm_backend != "cloud":
            return self.fast_model
        if self.fast_model.startswith("qwen2.5vl") or ":" in self.fast_model:
            return "qwen-turbo"
        return self.fast_model

    @property
    def effective_embedding_model(self) -> str:
        """当前后端实际使用的向量模型名。"""
        if self.embedding_backend == "cloud":
            return self.cloud_embedding_model
        return self.embedding_model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return Settings()
