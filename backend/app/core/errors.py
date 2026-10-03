"""日志与异常。

日志用 loguru，输出带颜色到控制台、同时落盘到 ``data/app.log``（按天切分、保留 7 天）。
异常体系刻意保持扁平：业务异常继承 ``AppError``，由全局异常处理器统一转成
``{"code": ..., "message": ...}`` 的 JSON，前端 ``onError`` 直接取 ``message`` 展示。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

_CONFIGURED = False


def setup_logging(debug: bool = False, log_dir: Path | None = None) -> None:
    """初始化日志（幂等，重复调用只生效一次）。

    Args:
        debug: 为 True 时输出 DEBUG 级别日志。
        log_dir: 日志落盘目录；为 None 时只输出到控制台。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if debug else "INFO",
        format=(
            "<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
        ),
        colorize=True,
    )

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "app.log",
            level="INFO",
            rotation="00:00",
            retention="7 days",
            encoding="utf-8",
            enqueue=True,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
        )

    _CONFIGURED = True


# ---------------------------------------------------------------------------
# 异常体系
# ---------------------------------------------------------------------------


class AppError(Exception):
    """业务异常基类。``code`` 供前端做分支处理，``message`` 直接面向用户展示。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class OllamaUnavailableError(AppError):
    """Ollama 服务不可达或模型未就绪。"""

    code = "ollama_unavailable"
    http_status = 503


class ModelOutputError(AppError):
    """模型输出无法解析为约定结构（重试后仍失败）。"""

    code = "model_output_invalid"
    http_status = 502


class ImageValidationError(AppError):
    """上传的图片不合法（格式、体积、损坏）。"""

    code = "image_invalid"
    http_status = 400


class DocumentParseError(AppError):
    """文档解析失败或格式不支持。"""

    code = "document_parse_failed"
    http_status = 400


class EmbeddingError(AppError):
    """向量化失败（如 embedding 模型未拉取）。"""

    code = "embedding_failed"
    http_status = 503


# ---------------------------------------------------------------------------
# 鉴权与权限
#
# 401 与 403 的区别必须严格：401 表示「你没登录」（前端应弹登录框），
# 403 表示「你登录了但没权限」（前端应提示无权限，不该弹登录框）。
# 混用会导致已登录用户被反复要求登录，或客户看到无意义的登录提示。
# ---------------------------------------------------------------------------


class AuthenticationError(AppError):
    """未登录或凭据无效/已过期。"""

    code = "unauthenticated"
    http_status = 401


class PermissionDeniedError(AppError):
    """已登录但角色权限不足。"""

    code = "forbidden"
    http_status = 403


class CredentialsError(AppError):
    """用户名或密码错误。

    刻意与 `AuthenticationError` 分开：前者是「凭据不对」（登录接口用），
    后者是「没有有效会话」（守卫用）。两者的 message 也不同——
    凭据错误的提示必须是模糊的（不区分用户名不存在 / 密码错误），
    避免被用来枚举已注册用户。
    """

    code = "invalid_credentials"
    http_status = 401


class UserConflictError(AppError):
    """用户名已被占用。"""

    code = "user_conflict"
    http_status = 409


class AccountLockedError(AppError):
    """账号因连续登录失败被临时锁定。"""

    code = "account_locked"
    http_status = 423


class AccountDisabledError(AppError):
    """账号已被管理员禁用。"""

    code = "account_disabled"
    http_status = 403


class AnalysisTaskConflictError(AppError):
    """后台分析任务当前状态不允许执行所请求的操作。"""

    code = "analysis_task_conflict"
    http_status = 409


class ResourceNotFoundError(AppError):
    """请求的记录、会话或任务不存在，或不属于当前用户。"""

    code = "not_found"
    http_status = 404


def error_payload(exc: BaseException) -> dict[str, Any]:
    """把任意异常压成统一响应体。"""
    if isinstance(exc, AppError):
        return {"code": exc.code, "message": exc.message, "detail": exc.detail}
    return {"code": "internal_error", "message": f"服务器内部错误：{exc}", "detail": None}
