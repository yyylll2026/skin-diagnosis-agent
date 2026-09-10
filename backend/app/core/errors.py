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


def error_payload(exc: BaseException) -> dict[str, Any]:
    """把任意异常压成统一响应体。"""
    if isinstance(exc, AppError):
        return {"code": exc.code, "message": exc.message, "detail": exc.detail}
    return {"code": "internal_error", "message": f"服务器内部错误：{exc}", "detail": None}
