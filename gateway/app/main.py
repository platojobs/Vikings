"""IoT 业务网关入口：FastAPI + aiomqtt 异步消息消费。

启动顺序：
1. 建表（幂等，补齐缺失表）；
2. 创建 MQTT 客户端并绑定业务处理器；
3. 后台启动消费协程（不阻塞 HTTP 服务）；
4. 优雅关闭时先停消费、再释放数据库连接池。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import alerts, auth, devices, ota, stats
from app.config import get_settings
from app.database import dispose_engine, init_models
from app.mqtt.client import MqttGatewayClient
from app.mqtt.handlers import MessageHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("app")
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("网关启动，数据库: %s", settings.database_url)
    await init_models()

    handler = MessageHandler()
    mqtt = MqttGatewayClient(handler)
    handler.bind(mqtt)
    app.state.mqtt = mqtt
    await mqtt.start()

    yield

    logger.info("网关关闭，停止 MQTT 消费并释放连接池")
    await mqtt.stop()
    await dispose_engine()


app = FastAPI(
    title="IoT 业务网关",
    description="设备消息消费、时序入库、AI 异常诊断、指令下发与后台查询接口",
    version="1.0.0",
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

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(alerts.router)
app.include_router(ota.router)
app.include_router(stats.router)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """把 FastAPI 默认的 {detail} 统一成契约里的 {code, message, data}。"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.status_code, "message": exc.detail, "data": None},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("未捕获异常 path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": 500, "message": "服务内部错误", "data": None},
    )


@app.get("/")
async def root():
    return {
        "code": 0,
        "message": "IoT 业务网关运行中",
        "data": {"docs": "/docs", "health": "/api/health"},
    }
