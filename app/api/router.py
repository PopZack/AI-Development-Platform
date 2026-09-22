"""v1 路由汇总。

新增模块时在这里登记，main.py 只管挂载总路由和前缀。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, projects, requirements, users, workflows

__all__ = ["api_router"]

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(projects.router)
api_router.include_router(requirements.router)
api_router.include_router(workflows.router)
