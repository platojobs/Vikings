"""认证路由：账号密码登录（查用户表 + PBKDF2 校验），签发携带角色的 JWT。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.schemas import ApiResponse, LoginRequest, LoginResponse, UserInfo, UserOut
from app.security import create_access_token, require_user
from app.services import audit_service, user_service

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=ApiResponse[LoginResponse])
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    user = await user_service.authenticate(session, body.username, body.password)
    if user is None:
        # 账号不存在 / 已停用 / 口令错误统一提示，避免账号枚举
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    user.last_login_at = datetime.now(timezone.utc)
    token = create_access_token(user.username, role=user.role)
    await audit_service.log(
        session,
        operator=user.username,
        action=audit_service.ACTION_LOGIN,
        target_type="app_user",
        target_id=user.username,
        detail={"role": user.role},
    )
    await session.commit()
    return ApiResponse(
        data=LoginResponse(token=token, user=UserInfo(username=user.username, role=user.role))
    )


@router.get("/me", response_model=ApiResponse[UserOut])
async def me(
    session: AsyncSession = Depends(get_session),
    current: dict = Depends(require_user),
):
    """返回数据库中的最新账号状态：角色被调整后前端刷新即可生效。"""
    user = await user_service.get_user(session, current["sub"])
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="账号不存在或已停用")
    return ApiResponse(data=UserOut.model_validate(user))
