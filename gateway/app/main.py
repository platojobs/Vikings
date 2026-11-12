"""IoT 业务网关入口：FastAPI + aiomqtt 异步消息消费。

启动顺序：
1. 数据库迁移（开发自动 upgrade，生产只校验版本）；
2. 创建 MQTT 客户端并绑定业务处理器；
3. 后台启动消费协程（不阻塞 HTTP 服务）；
4. 优雅关闭时先停消费、再释放数据库连接池。
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    alerts,
    audit,
    auth,
    devices,
    exports,
    health,
    notifications,
    ota,
    rules,
    shadows,
    stats,
    system,
    users,
)
from app.config import get_settings
from app.database import SessionFactory, dispose_engine, prepare_database
from app.metrics import DB_READY
from app.mqtt.client import MqttGatewayClient
from app.mqtt.handlers import MessageHandler
from app.observability import ObservabilityMiddleware, configure_logging
from app.scheduler import BackgroundScheduler
from app.services import alert_rule_service, rule_service, user_service

configure_logging()
logger = logging.getLogger("app")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 生产环境先做配置安全校验：默认/弱密钥、弱口令或 SQLite 直接拒绝启动。
    # 开发/测试环境允许默认值，但用醒目 warning 提醒风险。
    settings.validate_for_production()
    for warning in settings.security_warnings():
        logger.warning("安全配置提醒：%s", warning)

    logger.info("网关启动（APP_ENV=%s），数据库: %s", settings.app_env, settings.safe_database_url)
    app.state.started_at = datetime.now(timezone.utc)
    await prepare_database()
    DB_READY.set(1)

    # 启动时加载阈值覆盖与告警规则：恢复上次在线修改的配置，并补齐默认规则。
    # 规则判定读进程内缓存，管理端写入后 write-through 刷新，无需重启。
    async with SessionFactory() as session:
        await rule_service.load_overrides(session)
        seeded = await alert_rule_service.ensure_default_rules(session)
        if seeded:
            logger.info("告警规则表为空，已按默认阈值补齐 %d 条默认规则", seeded)
        await alert_rule_service.load_rules(session)
        created_users = await user_service.ensure_bootstrap_users(session)
        await session.commit()
    if created_users:
        logger.info("初始化后台账号: %s", ", ".join(created_users))

    handler = MessageHandler()
    mqtt = MqttGatewayClient(handler)
    handler.bind(mqtt)
    app.state.mqtt = mqtt
    await mqtt.start()

    # 后台定时任务：离线巡检（LWT 兜底）+ 时序数据保留清理
    scheduler = BackgroundScheduler()
    await scheduler.start()

    yield

    logger.info("网关关闭，停止 MQTT 消费与后台调度")
    await scheduler.stop()
    await mqtt.stop()
    await dispose_engine()


app = FastAPI(
    title="IoT 业务网关",
    description="设备消息消费、时序入库、AI 异常诊断、指令下发与后台查询接口",
    version="1.21.0",
    lifespan=lifespan,
)

# 后台前端跨域：Vite 开发服务器默认 5173
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求 ID、结构化访问日志与 HTTP 指标；放在 CORS 外层，保证响应头一定回写
app.add_middleware(ObservabilityMiddleware)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(alerts.router)
app.include_router(ota.router)
app.include_router(stats.router)
app.include_router(rules.router)
app.include_router(audit.router)
app.include_router(users.router)
app.include_router(system.router)
app.include_router(notifications.router)
app.include_router(exports.router)
app.include_router(shadows.router)
app.include_router(health.router)
app.include_router(health.metrics_router)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """把 FastAPI 默认的 {detail} 统一成契约里的 {code, message, data}。

    额外带 requestId：用户报错时可直接把该 ID 交给运维检索日志。
    """
    headers = dict(exc.headers or {})
    request_id = _request_id(request)
    if request_id:
        headers["X-Request-Id"] = request_id
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": exc.detail,
            "data": None,
            "requestId": request_id,
        },
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = _request_id(request)
    logger.exception(
        "未捕获异常 path=%s request_id=%s",
        request.url.path,
        request_id,
        extra={"requestId": request_id},
    )
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "message": "服务内部错误",
            "data": None,
            "requestId": request_id,
        },
        headers={"X-Request-Id": request_id} if request_id else None,
    )


@app.get("/")
async def root():
    return {
        "code": 0,
        "message": "IoT 业务网关运行中",
        "data": {"docs": "/docs", "health": "/api/health", "ready": "/api/health/ready"},
    }
