"""认证路由：演示级账号密码登录，签发 JWT。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.schemas import ApiResponse, LoginRequest, LoginResponse, UserInfo
from app.security import create_access_token
from app.services import audit_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


@router.post("/login", response_model=ApiResponse[LoginResponse])
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)):
    # 单账号演示登录：生产应改为用户表 + 密码哈希 + 刷新令牌
    if body.username != settings.admin_username or body.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(body.username, role="admin")
    await audit_service.log(
        session, operator=body.username, action=audit_service.ACTION_LOGIN
    )
    await session.commit()
    return ApiResponse(
        data=LoginResponse(token=token, user=UserInfo(username=body.username, role="admin"))
    )
