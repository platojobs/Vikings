"""账号服务：登录校验、账号增删改查与首次启动的演示账号初始化。

口令只以 PBKDF2 哈希落库；角色变更立即影响接口鉴权，
但已签发的 JWT 不主动失效（演示级取舍，生产可配合短 TTL + 刷新令牌）。
"""

from typing import List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AppUser
from app.security import (
    ALL_ROLES,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_VIEWER,
    hash_password,
    verify_password,
)

settings = get_settings()

# 首次启动时额外初始化的演示账号，便于直接演示角色差异
_DEMO_ACCOUNTS = (
    ("operator", "operator123", ROLE_OPERATOR, "运维：可下发指令 / OTA / 确认告警"),
    ("viewer", "viewer123", ROLE_VIEWER, "只读：仅可查看数据"),
)


async def ensure_bootstrap_users(session: AsyncSession) -> List[str]:
    """首次启动初始化账号，返回本次新建的用户名列表。

    幂等策略：表为空时初始化 admin + 两个演示账号；已有账号时不再覆盖，
    避免运维改过的口令在重启后被环境变量「重置」。
    """
    total = int((await session.execute(select(func.count()).select_from(AppUser))).scalar_one())
    if total > 0:
        return []

    created: List[str] = []
    if settings.admin_username:
        session.add(
            AppUser(
                username=settings.admin_username,
                password_hash=hash_password(settings.admin_password),
                role=ROLE_ADMIN,
                active=True,
            )
        )
        created.append(settings.admin_username)

    for username, password, role, _ in _DEMO_ACCOUNTS:
        if username == settings.admin_username:
            continue
        session.add(
            AppUser(
                username=username,
                password_hash=hash_password(password),
                role=role,
                active=True,
            )
        )
        created.append(username)

    await session.flush()
    return created


async def get_user(session: AsyncSession, username: str) -> Optional[AppUser]:
    return await session.get(AppUser, username)


async def authenticate(session: AsyncSession, username: str, password: str) -> Optional[AppUser]:
    """校验用户名与口令；账号不存在或已停用一律返回 None，不区分原因。"""
    user = await session.get(AppUser, username)
    if user is None or not user.active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


async def list_users(session: AsyncSession) -> List[AppUser]:
    stmt = select(AppUser).order_by(AppUser.created_at.asc())
    return list((await session.execute(stmt)).scalars().all())


async def create_user(
    session: AsyncSession, username: str, password: str, role: str
) -> Tuple[Optional[AppUser], str]:
    """新建账号；用户名重复时返回错误文案由路由转 409。"""
    if role not in ALL_ROLES:
        return None, "角色非法"
    if await session.get(AppUser, username) is not None:
        return None, "用户名已存在"
    user = AppUser(username=username, password_hash=hash_password(password), role=role, active=True)
    session.add(user)
    await session.flush()
    return user, ""


async def update_user(
    session: AsyncSession,
    username: str,
    role: Optional[str],
    active: Optional[bool],
    password: Optional[str],
) -> Tuple[Optional[AppUser], str]:
    """更新角色 / 启停 / 口令；保护最后一个可用管理员不被降权或停用。"""
    user = await session.get(AppUser, username)
    if user is None:
        return None, "账号不存在"

    losing_admin = (
        role is not None and role != ROLE_ADMIN and user.role == ROLE_ADMIN
    ) or (active is False and user.role == ROLE_ADMIN)
    if losing_admin and await count_active_admins(session) <= 1:
        return None, "系统必须保留至少一个可用的管理员账号"

    if role is not None:
        user.role = role
    if active is not None:
        user.active = active
    if password:
        user.password_hash = hash_password(password)

    await session.flush()
    return user, ""


async def delete_user(session: AsyncSession, username: str) -> Tuple[bool, str]:
    user = await session.get(AppUser, username)
    if user is None:
        return False, "账号不存在"
    if user.role == ROLE_ADMIN and await count_active_admins(session) <= 1:
        return False, "系统必须保留至少一个可用的管理员账号"
    await session.delete(user)
    await session.flush()
    return True, ""


async def count_active_admins(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(AppUser).where(
        AppUser.role == ROLE_ADMIN, AppUser.active.is_(True)
    )
    return int((await session.execute(stmt)).scalar_one())
