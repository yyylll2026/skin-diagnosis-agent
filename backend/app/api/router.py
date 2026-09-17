"""API 路由汇总。

所有子路由在此挂载，最终由 ``main.py`` 以 ``settings.api_prefix``（默认 ``/api/v1``）
为前缀注册，保证「版本号只在一个地方出现」，将来出 v2 时不必改各业务文件。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, chat, knowledge, system, users

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(users.audit_router)
api_router.include_router(chat.router)
api_router.include_router(knowledge.router)
api_router.include_router(system.router)
api_router.include_router(system.sessions_router)

__all__ = ["api_router"]
