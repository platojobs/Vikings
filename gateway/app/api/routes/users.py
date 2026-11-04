"""账号管理路由：仅管理员可访问，所有变更写入审计日志。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import (
    ApiResponse,
    SessionRevokeOut,
    UserCreateRequest,
    UserOut,
    UserUpdateRequest,
)
from app.security import ROLE_ADMIN, require_role
from app.services import audit_service, token_service, user_service

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=ApiResponse[list[UserOut]])
async def list_users(
    session: AsyncSession = Depends(get_session),
    _: dict = Depends(require_role(ROLE_ADMIN)),
):
    rows = await user_service.list_users(session)
    return ApiResponse(data=[UserOut.model_validate(row) for row in rows])


@router.post("", response_model=ApiResponse[UserOut])
async def create_user(
    body: UserCreateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    created, err = await user_service.create_user(
        session, body.username, body.password, body.role
    )
    if created is None:
        raise HTTPException(status_code=409 if "已存在" in err else 400, detail=err)

    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_USER_CREATE,
        target_type="app_user",
        target_id=created.username,
        detail={"role": created.role},
    )
    await session.commit()
    return ApiResponse(data=UserOut.model_validate(created))


@router.patch("/{username}", response_model=ApiResponse[UserOut])
async def update_user(
    username: str,
    body: UserUpdateRequest,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    target, err = await user_service.update_user(
        session, username, body.role, body.active, body.password
    )
    if target is None:
        raise HTTPException(status_code=404 if "不存在" in err else 400, detail=err)

    detail = {
        key: value
        for key, value in {
            "role": body.role,
            "active": body.active,
            "passwordReset": bool(body.password),
        }.items()
        # 不把口令本身写进审计日志
        if value is not None and value is not False
    }
    if body.password:
        # 改密（含管理员重置）：吊销该用户全部 refresh，旧会话无法再续期
        detail["sessionsRevoked"] = await token_service.revoke_all_for_user(
            session, username, token_service.REASON_PASSWORD_CHANGE
        )
    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_USER_UPDATE,
        target_type="app_user",
        target_id=username,
        detail=detail,
    )
    await session.commit()
    return ApiResponse(data=UserOut.model_validate(target))


@router.post("/{username}/sessions/revoke", response_model=ApiResponse[SessionRevokeOut])
async def revoke_user_sessions(
    username: str,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    """管理员强制某用户下线：吊销其全部刷新令牌（access 仍会自然过期）。"""
    target = await user_service.get_user(session, username)
    if target is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    revoked = await token_service.revoke_all_for_user(
        session, username, token_service.REASON_ADMIN_REVOKE
    )
    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_SESSION_REVOKE,
        target_type="app_user",
        target_id=username,
        detail={"revoked": revoked},
    )
    await session.commit()
    return ApiResponse(data=SessionRevokeOut(username=username, revoked=revoked))


@router.delete("/{username}", response_model=ApiResponse[dict])
async def delete_user(
    username: str,
    session: AsyncSession = Depends(get_session),
    user: dict = Depends(require_role(ROLE_ADMIN)),
):
    if username == user["sub"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    ok, err = await user_service.delete_user(session, username)
    if not ok:
        raise HTTPException(status_code=404 if "不存在" in err else 400, detail=err)

    revoked = await token_service.revoke_all_for_user(
        session, username, token_service.REASON_ADMIN_REVOKE
    )
    await audit_service.log(
        session,
        operator=user["sub"],
        action=audit_service.ACTION_USER_DELETE,
        target_type="app_user",
        target_id=username,
        detail={"sessionsRevoked": revoked},
    )
    await session.commit()
    return ApiResponse(data={"deleted": username})
