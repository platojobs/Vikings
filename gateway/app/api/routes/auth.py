"""认证路由：账号密码登录（查用户表 + PBKDF2 校验），签发携带角色的 JWT。"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.schemas import (
    ApiResponse,
    LoginRequest,
    LoginResponse,
    MqttClientCredentialOut,
    UserInfo,
    UserOut,
)
from app.security import create_access_token, require_user
from app.services import audit_service, mqtt_credential_service, user_service
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
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )

    user = await user_service.authenticate(session, body.username, body.password)
    if user is None:
        # 账号不存在 / 已停用 / 口令错误统一提示，避免账号枚举
        login_limiter.record_failure(key)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    login_limiter.reset(key)
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
