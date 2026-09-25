"""FastAPI 应用入口。

分层：``main.py`` 只负责「组装与横切关注点」（日志、CORS、异常、生命周期），
业务逻辑一律下沉到 ``api/v1`` → ``agent`` → ``services``。这样 ``main.py``
在任何时候都只有几十行，新人接手时一眼能看清系统有哪些横切能力。

启动：
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api.router import api_router
from app.config import get_settings
from app.core.errors import AppError, error_payload


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动预热 + 优雅关闭。

    预热的意义：Chroma 建集合、LangGraph 编译图、embedding 模型首次加载
    都会耗时数秒到数十秒。如果留给第一个用户请求承担，体验会很差（首请求超时）。
    """
    settings = get_settings()
    from app.core.errors import setup_logging

    setup_logging(debug=settings.debug, log_dir=settings.data_dir)

    logger.info("=" * 68)
    logger.info("{} 启动中……", settings.app_name)
    if settings.llm_backend == "cloud":
        logger.info(
            "模型后端=cloud｜{}｜视觉: {}",
            settings.resolved_cloud_api_base,
            settings.effective_vision_model,
        )
    else:
        logger.info("Ollama: {}｜视觉模型: {}", settings.ollama_api, settings.vision_model)
    logger.info(
        "Embedding: {} / {}", settings.embedding_backend, settings.effective_embedding_model
    )
    logger.info("向量库: {}（集合 {}）", settings.resolved_chroma_dir, settings.chroma_collection)

    try:
        from app.dependencies import warmup

        warmup()
        from app.agent.graph import graph_mermaid

        mermaid = graph_mermaid()
        if mermaid:
            logger.debug("智能体拓扑（Mermaid）：\n{}", mermaid)
    except Exception as exc:  # noqa: BLE001 - 预热失败不应阻止服务启动
        logger.warning("预热失败（不影响启动，首个请求会较慢）：{}", exc)

    logger.info("启动完成，接口前缀 {}", settings.api_prefix)
    logger.info("=" * 68)

    yield

    logger.info("正在关闭……")
    try:
        from app.dependencies import get_ollama_client_dep

        await get_ollama_client_dep().aclose()
    except Exception:  # noqa: BLE001
        pass


def create_app() -> FastAPI:
    """构造 FastAPI 应用（工厂函数便于测试时创建隔离实例）。"""
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "基于 Ollama + Qwen2.5-VL + LangGraph + Chroma 的皮肤诊断智能体。\n\n"
            "- **知识库问答**：文档向量化检索 + 检索增强生成\n"
            "- **图片识别**：Qwen2.5-VL 结构化视觉分析 + 知识库依据融合\n\n"
            "⚠️ 本系统仅用于健康科普与初步参考，不构成医疗诊断。"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    # 前端跑在 5173（Vite dev server），必须放行 CORS；
    # SSE 同样受 CORS 约束，因此 allow_origins 不能写 "*" 与 credentials 并存
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Elapsed-Ms"],
    )

    # ---------------- 全局异常处理 ----------------

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """业务异常 -> 结构化 JSON（前端 ``onError`` 直接取 message）。"""
        logger.warning("业务异常 [{}] {} -> {}", exc.code, request.url.path, exc.message)
        return JSONResponse(status_code=exc.http_status, content=error_payload(exc))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """参数校验失败 -> 400，并把首个错误点出来，便于定位。

        ## 为什么要自己筛字段，而不是直接把 `exc.errors()` 丢回去

        这是踩出来的两个坑：

        1. **不能序列化**。校验器里 `raise ValueError(...)` 时，Pydantic 会把
           原始异常对象放进 `ctx.error`，`JSONResponse` 序列化它直接抛 TypeError，
           于是 400 变成 500。只要任何一个字段用了 `raise ValueError` 就会触发。
        2. **会泄露用户提交的原文**。`errors()` 里带 `input`，而它正是本次请求的
           字段值——密码字段一旦校验失败，密码就会原样回显到响应体里。

        因此这里只保留「位置 + 原因 + 类型」三个字段：
        对定位问题足够，既不会序列化失败，也不会把密码吐出去。
        """
        errors = exc.errors()
        first = errors[0] if errors else {}
        location = ".".join(str(part) for part in first.get("loc", []))
        cleaned = [
            {
                "loc": ".".join(str(part) for part in item.get("loc", [])),
                "msg": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
            for item in errors[:5]
        ]
        return JSONResponse(
            status_code=400,
            content={
                "code": "validation_error",
                "message": f"请求参数有误：{location} {first.get('msg', '')}".strip(),
                "detail": cleaned,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底异常 -> 500，内部细节只进日志不返前端。"""
        logger.exception("未捕获异常: {} {}", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "服务器内部错误，请查看后端日志",
                "detail": None,
            },
        )

    # ---------------- 中间件：耗时观测 ----------------

    @app.middleware("http")
    async def add_timing_header(request: Request, call_next: Any) -> Any:
        """给每个响应加上服务端耗时头，便于前后端联调定位慢点。"""
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = int((time.perf_counter() - started) * 1000)
        response.headers["X-Elapsed-Ms"] = str(elapsed)
        if elapsed > 5000 and not request.url.path.endswith("/health"):
            logger.warning("慢请求 {} {} 耗时 {}ms", request.method, request.url.path, elapsed)
        return response

    # ---------------- 路由 ----------------

    # 1) API 路由先注册，保证 /api/v1/** 不会被后面的静态资源挂载吞掉
    #    （Starlette 按注册顺序匹配，挂载 "/" 必须放最后）
    app.include_router(api_router, prefix=settings.api_prefix)

    def _service_info() -> dict[str, Any]:
        """服务基本信息。"""
        return {
            "name": settings.app_name,
            "version": "0.1.0",
            "docs": "/docs",
            "api_prefix": settings.api_prefix,
            "models": {
                "vision": settings.vision_model,
                "text": settings.text_model,
                "embedding": f"{settings.embedding_backend}:{settings.embedding_model}",
            },
            "disclaimer": "本系统仅用于健康科普与初步参考，不构成医疗诊断。",
        }

    # 2) 前端构建产物存在时，由后端同源托管。
    #    云平台（魔搭创空间 / HF Spaces 等）只转发单一端口，前后端必须合并到一个
    #    服务里；同源托管还顺带免除了 CORS 与反向代理配置——前端的相对路径
    #    `/api/v1/...` 天然指向同一个后端。
    web_dir = settings.resolved_web_dir
    if (web_dir / "index.html").is_file():
        # 根路径被前端占用，服务信息挪到 /service-info。
        # 注意顺序：必须在 mount("/") **之前**注册，否则会被静态挂载吞掉
        # （StaticFiles 会去找名为 service-info 的文件，直接 404）。
        app.add_api_route("/service-info", _service_info, tags=["系统"], summary="服务信息")
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
        logger.info("已挂载前端静态资源: {}", web_dir)
    else:
        app.add_api_route("/", _service_info, tags=["系统"], summary="服务信息")

    return app


app = create_app()
