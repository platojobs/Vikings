"""刷新令牌生命周期：签发、轮换、登出/改密/管理员吊销。

设计要点（v1.24.0 / P2-4）：

- **只存指纹**：数据库主键是明文令牌的 SHA-256，明文只在登录/刷新的响应中出现一次；
- **单次使用 + 轮换**：刷新时用原子 UPDATE 把旧行标记为 rotated，再签发新行；
  同一旧令牌第二次使用返回 401，并视为疑似被盗，吊销该用户全部会话；
- **不宣称「吊销立即全局生效」**：access token 是无状态 JWT，吊销 refresh 后
  已签发的 access 在剩余 TTL（默认 20 分钟）内仍可用。缩小窗口靠短 TTL，
  见 docs/contracts.md 3.1 与 docs/operations.md 的说明。
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import RefreshToken

settings = get_settings()

REASON_LOGOUT = "logout"
REASON_ROTATED = "rotated"
REASON_PASSWORD_CHANGE = "password_change"
REASON_ADMIN_REVOKE = "admin_revoke"
REASON_REUSE = "reuse_detected"
REASON_EXPIRED = "expired"


class RefreshTokenError(Exception):
    """刷新令牌不可用；`reused=True` 表示检测到旧令牌重放。"""

    def __init__(
        self, message: str, *, reused: bool = False, username: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.reused = reused
        self.username = username


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite 读回的时间是 naive，统一按 UTC 比较。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def fingerprint(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _new_raw_token() -> str:
    # 48 字节 urlsafe 随机串，足以抵抗离线猜测；明文不落库
    return secrets.token_urlsafe(48)


async def issue_refresh_token(
    session: AsyncSession, username: str, *, now: Optional[datetime] = None
) -> str:
    """签发一个新的刷新令牌，返回明文（调用方需随响应下发，不再持久化明文）。"""
    moment = now or _now()
    raw = _new_raw_token()
    session.add(
        RefreshToken(
            token_hash=fingerprint(raw),
            username=username,
            issued_at=moment,
            expires_at=moment + timedelta(days=settings.refresh_token_ttl_days),
            revoked_at=None,
            revoked_reason=None,
        )
    )
    await session.flush()
    return raw


async def rotate_refresh_token(
    session: AsyncSession, raw: str, *, now: Optional[datetime] = None
) -> Tuple[str, str]:
    """用旧刷新令牌换取新的（旧令牌立即失效），返回 `(新明文, 用户名)`。"""
    moment = now or _now()
    token_hash = fingerprint(raw)
    row = await session.get(RefreshToken, token_hash)
    if row is None:
        raise RefreshTokenError("刷新令牌无效")

    if row.revoked_at is not None:
        if row.revoked_reason == REASON_ROTATED:
            # 旧令牌被重放：可能是令牌泄露，按安全策略吊销该用户全部会话
            await revoke_all_for_user(session, row.username, REASON_REUSE, now=moment)
            raise RefreshTokenError(
                "刷新令牌已被使用，已吊销该账号全部会话",
                reused=True,
                username=row.username,
            )
        raise RefreshTokenError("刷新令牌已被吊销", username=row.username)

    if _aware(row.expires_at) <= moment:
        row.revoked_at = moment
        row.revoked_reason = REASON_EXPIRED
        await session.flush()
        raise RefreshTokenError("刷新令牌已过期", username=row.username)

    # 原子「占用」旧令牌：并发请求里只有一个能拿到 rowcount=1
    consumed = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=moment, revoked_reason=REASON_ROTATED)
    )
    if consumed.rowcount != 1:
        # 竞争失败说明旧令牌刚被别处用掉，按重放处理（宁严勿松）
        await revoke_all_for_user(session, row.username, REASON_REUSE, now=moment)
        raise RefreshTokenError(
            "刷新令牌已被使用，已吊销该账号全部会话",
            reused=True,
            username=row.username,
        )

    new_raw = await issue_refresh_token(session, row.username, now=moment)
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash)
        .values(replaced_by=fingerprint(new_raw))
    )
    return new_raw, row.username


async def revoke_refresh_token(
    session: AsyncSession, raw: str, reason: str = REASON_LOGOUT
) -> Optional[str]:
    """吊销单个刷新令牌（幂等），返回被吊销令牌所属用户名；未命中返回 None。"""
    token_hash = fingerprint(raw)
    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=_now(), revoked_reason=reason)
    )
    if result.rowcount != 1:
        return None
    return (
        await session.execute(
            select(RefreshToken.username).where(RefreshToken.token_hash == token_hash)
        )
    ).scalar_one_or_none()


async def revoke_all_for_user(
    session: AsyncSession,
    username: str,
    reason: str,
    *,
    now: Optional[datetime] = None,
) -> int:
    """吊销某用户全部未吊销的刷新令牌，返回吊销条数。"""
    result = await session.execute(
        update(RefreshToken)
        .where(
            RefreshToken.username == username,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now or _now(), revoked_reason=reason)
    )
    return int(result.rowcount or 0)
