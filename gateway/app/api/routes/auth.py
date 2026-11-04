"""认证路由：登录签发短 TTL access + 可吊销 refresh，并提供刷新/登出。

生命周期（v1.24.0）：
- access 默认 20 分钟（ACCESS_TOKEN_TTL_SECONDS），无状态 JWT；
- refresh 默认 14 天（REFRESH_TOKEN_TTL_DAYS），服务端持久化指纹、单次使用并轮换；
- 登出只吊销当前 refresh；管理员吊销/改密吊销该用户全部 refresh；
- 已被吊销的 refresh 立即失效，但已签发的 access 仍可用到自身过期为止。
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.metrics import LOGIN_FAILURES_TOTAL, LOGIN_RATE_LIMITED_TOTAL
from app.schemas import (
    ApiResponse,
    LoginRequest,
    LoginResponse,
    MqttClientCredentialOut,
    RefreshRequest,
    UserInfo,
    UserOut,
)
from app.security import create_access_token, require_user
from app.services import audit_service, mqtt_credential_service, token_service, user_service
from app.services.login_guard import LoginRateLimiter

router = APIRouter(prefix="/api/auth", tags=["auth"])

settings = get_settings()
login_limiter = LoginRateLimiter(
    max_failures=settings.login_max_failures,
    window_seconds=settings.login_failure_window_seconds,
    lockout_seconds=settings.login_lockout_seconds,
)


def _client_ip(request: Request) -> str:
    """取直连客户端 IP；不信任可伪造的 X-Forwarded-For（见 login_guard 说明）。"""
    return request.client.host if request.client else "unknown"


@router.post("/login", response_model=ApiResponse[LoginResponse])
async def login(
    body: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    key = (_client_ip(request), body.username)
    retry_after = login_limiter.retry_after(key)
    if retry_after:
        LOGIN_RATE_LIMITED_TOTAL.inc()
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )

    user = await user_service.authenticate(session, body.username, body.password)
    if user is None:
        # 账号不存在 / 已停用 / 口令错误统一提示，避免账号枚举
        login_limiter.record_failure(key)
        LOGIN_FAILURES_TOTAL.inc()
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    login_limiter.reset(key)
    user.last_login_at = datetime.now(timezone.utc)
    token = create_access_token(user.username, role=user.role)
    refresh_token = await token_service.issue_refresh_token(session, user.username)
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
        data=LoginResponse(
            token=token,
            refresh_token=refresh_token,
            expires_in=settings.access_token_ttl_seconds,
            user=UserInfo(username=user.username, role=user.role),
        )
    )


@router.post("/refresh", response_model=ApiResponse[LoginResponse])
async def refresh(body: RefreshRequest, session: AsyncSession = Depends(get_session)):
    """用 refresh 换新 access：旧 refresh 单次使用，成功后立即失效（轮换）。"""
    try:
        new_refresh, username = await token_service.rotate_refresh_token(
            session, body.refresh_token
        )
    except token_service.RefreshTokenError as exc:
        if exc.reused and exc.username:
            await audit_service.log(
                session,
                operator="system",
                action=audit_service.ACTION_REFRESH_REUSE,
                target_type="app_user",
                target_id=exc.username,
                detail={"reason": token_service.REASON_REUSE},
            )
            await session.commit()
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    user = await user_service.get_user(session, username)
    if user is None or not user.active:
        # 账号已停用/删除：连刚签发的新令牌一起吊销，避免残留可用会话
        await token_service.revoke_all_for_user(
            session, username, token_service.REASON_ADMIN_REVOKE
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="账号不存在或已停用")

    access = create_access_token(user.username, role=user.role)
    await session.commit()
    return ApiResponse(
        data=LoginResponse(
            token=access,
            refresh_token=new_refresh,
            expires_in=settings.access_token_ttl_seconds,
            user=UserInfo(username=user.username, role=user.role),
        )
    )


@router.post("/logout", response_model=ApiResponse[dict])
async def logout(body: RefreshRequest, session: AsyncSession = Depends(get_session)):
    """登出：吊销当前 refresh（幂等）。access 仍会自然过期，客户端应同时清本地会话。

    故意不要求 access 鉴权：access 可能已经过期，否则用户将无法登出。
    """
    username = await token_service.revoke_refresh_token(
        session, body.refresh_token, token_service.REASON_LOGOUT
    )
    if username:
        await audit_service.log(
            session,
            operator=username,
            action=audit_service.ACTION_LOGOUT,
            target_type="app_user",
            target_id=username,
        )
    await session.commit()
    return ApiResponse(data={"revoked": username is not None})


@router.get("/mqtt-credentials", response_model=ApiResponse[MqttClientCredentialOut])
async def mqtt_credentials(
    client: str = Query(default="web", pattern="^(web|mobile)$"),
    _: dict = Depends(require_user),
):
    """登录后换取客户端只读 MQTT 凭证。

    口令来自服务端环境变量（MQTT_WEB_PASSWORD / MQTT_MOBILE_PASSWORD），
    不进入 Web/移动端构建产物；9001 监听的 ACL 只有 read 规则，
    客户端即使拿到设备口令也无法发布设备上报 Topic。
    """
    try:
        payload = mqtt_credential_service.client_credential(client)
    except mqtt_credential_service.MqttCredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=MqttClientCredentialOut(**payload))


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
