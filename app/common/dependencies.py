"""FastAPI 依赖装配。

Layer: Common（DI 装配）—— 只负责把 Session 包成 Service，
不含任何业务判断。

文档 §14.2 的要求：Router 只做「接收请求 → 转交 Service」，
所以依赖注入是 Router 唯一允许出现的装配逻辑。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_service import ProjectService
from app.application.requirement_service import RequirementService
from app.application.user_service import UserService
from app.infrastructure.db.session import get_session

__all__ = [
    "SessionDep",
    "UserServiceDep",
    "ProjectServiceDep",
    "RequirementServiceDep",
    "LimitQuery",
    "OffsetQuery",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_user_service(session: SessionDep) -> UserService:
    return UserService(session)


def get_project_service(session: SessionDep) -> ProjectService:
    return ProjectService(session)


def get_requirement_service(session: SessionDep) -> RequirementService:
    return RequirementService(session)


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
RequirementServiceDep = Annotated[RequirementService, Depends(get_requirement_service)]

LimitQuery = Annotated[int, Query(ge=1, le=200, description="每页条数")]
OffsetQuery = Annotated[int, Query(ge=0, description="偏移量")]
