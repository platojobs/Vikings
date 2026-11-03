"""JWT 鉴权：演示级简易后台登录，生产可替换为 OAuth2 / SSO。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request

from app.config import get_settings

settings = get_settings()
ALGORITHM = "HS256"


def create_access_token(username: str, role: str = "admin") -> str:
    """签发 JWT，携带用户名与角色。"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": username, "role": role, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


async def require_user(request: Request) -> dict:
    """FastAPI 依赖：校验 Authorization: Bearer <token>。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少访问令牌")
    payload = decode_access_token(header[7:].strip())
    if not payload:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return payload


CurrentUser = Depends(require_user)
