"""认证路由：演示级账号密码登录，签发 JWT。"""

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.schemas import ApiResponse, LoginRequest, LoginResponse, UserInfo
from app.security import create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])
settings = get_settings()


@router.post("/login", response_model=ApiResponse[LoginResponse])
async def login(body: LoginRequest):
    # 单账号演示登录：生产应改为用户表 + 密码哈希 + 刷新令牌
    if body.username != settings.admin_username or body.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(body.username, role="admin")
    return ApiResponse(
        data=LoginResponse(token=token, user=UserInfo(username=body.username, role="admin"))
    )
