"""JWT 鉴权与口令哈希：演示级简易后台登录，生产可替换为 OAuth2 / SSO。

角色模型（与前端菜单、接口权限一一对应）：
- admin     全部权限：含阈值配置与用户管理
- operator  运维执行：下发指令、创建 OTA、确认告警
- viewer    只读：仅可查询设备/告警/审计
"""

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request

from app.config import KNOWN_WEAK_PASSWORDS, get_settings

settings = get_settings()
ALGORITHM = "HS256"

# 角色常量：避免业务代码直接写字符串
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ALL_ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_VIEWER)

# PBKDF2 迭代次数：兼顾登录耗时与离线爆破成本
PBKDF2_ITERATIONS = 120000


def hash_password(password: str) -> str:
    """生成口令哈希，格式 pbkdf2_sha256$迭代次数$salt$hash（base64）。

    使用标准库实现，不引入额外依赖；salt 随机生成，同一口令每次结果不同。
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令：使用 compare_digest 防时序侧信道。"""
    try:
        algorithm, iterations, salt_b64, hash_b64 = (stored or "").split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str, min_length: int = 8) -> Optional[str]:
    """新建/重置口令的强度校验；返回错误文案，合规返回 None。

    只约束新写入的口令，既有账号继续可登录，避免升级后把运维锁在门外。
    """
    if len(password) < min_length:
        return f"口令至少需要 {min_length} 位"
    if password.strip().lower() in KNOWN_WEAK_PASSWORDS:
        return "口令过于常见，请更换为不包含默认账号特征的口令"
    return None


def create_access_token(username: str, role: str = ROLE_ADMIN) -> str:
    """签发短 TTL 访问令牌（JWT），携带用户名与角色。

    v1.24.0 起默认 TTL 20 分钟（`ACCESS_TOKEN_TTL_SECONDS`）。JWT 本身无状态，
    吊销 refresh 不会让已签发的 access 立即失效，见 docs/contracts.md 的说明。
    """
    expire = datetime.now(timezone.utc) + timedelta(seconds=settings.access_token_ttl_seconds)
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


def require_role(*roles: str):
    """FastAPI 依赖工厂：限定接口可访问的角色，admin 始终放行。

    用法：`user: dict = Depends(require_role(ROLE_OPERATOR))`
    """

    async def dependency(user: dict = Depends(require_user)) -> dict:
        role = user.get("role")
        if role != ROLE_ADMIN and role not in roles:
            raise HTTPException(status_code=403, detail="当前账号无此操作权限")
        return user

    return dependency
