"""结构化日志、请求 ID 与访问日志中间件（P1-1 / v1.20.0）。

设计约束：
- 日志默认人类可读，设置 `LOG_FORMAT=json` 切换为 JSON（生产使用）；
- 每个请求都有 requestId：优先沿用上游 `X-Request-Id`，否则生成；
- 日志只记录方法、路由、状态码、耗时与用户标识，**不记录请求体、请求头与查询串**；
- 所有外发文本都经过 `redact_text`，防止异常信息/配置值里的口令与 token 泄漏。
"""

import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.metrics import observe_request
from app.security import decode_access_token

# 贯穿一次请求的上下文：线程/协程内可被任意日志记录读取
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")
request_user_ctx: ContextVar[str] = ContextVar("request_user", default="-")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 敏感信息脱敏：覆盖 Authorization/Bearer、口令/令牌键值、数据库 URL 中的用户口令
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:password|passwd|pwd|token|access_token|secret|api_key|apikey|jwt_secret)"
        r"\s*[:=]\s*)[^\s,;\"'}]+"
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@\s]+@"),
)

_JSON_EXTRA_FIELDS = (
    "event",
    "method",
    "path",
    "status",
    "durationMs",
    "clientIp",
    "user",
    "error",
)


def redact_text(value: str) -> str:
    """把文本中的口令/token 替换为 [REDACTED]，长度上限 300，避免日志被撑爆。"""
    text = str(value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text[:300]


class RequestContextFilter(logging.Filter):
    """给所有日志记录补 requestId/user，JSON 与文本格式都能取到。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "requestId"):
            record.requestId = request_id_ctx.get()
        if not hasattr(record, "user"):
            record.user = request_user_ctx.get()
        return True


class JsonFormatter(logging.Formatter):
    """单行 JSON，字段固定，便于 Loki/ELK/jq 检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            "requestId": getattr(record, "requestId", "-"),
        }
        for field in _JSON_EXTRA_FIELDS:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """开发用人类可读格式：时间 | 级别 | logger | requestId | 消息。"""

    def format(self, record: logging.LogRecord) -> str:
        message = redact_text(record.getMessage())
        line = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} | "
            f"{record.levelname:<7} | {record.name} | "
            f"[{getattr(record, 'requestId', '-')}] {message}"
        )
        if record.exc_info:
            line += "\n" + redact_text(self.formatException(record.exc_info))
        return line


def configure_logging(
    log_format: Optional[str] = None, level: Optional[str] = None
) -> None:
    """初始化根日志。

    - `LOG_FORMAT=json`：生产用结构化日志；`text`（默认）：本地开发；
    - 关闭 uvicorn 自带 access log，避免和本中间件的访问日志重复且缺少 requestId。
    """
    fmt = (log_format or os.getenv("LOG_FORMAT", "text")).strip().lower()
    lvl = (level or os.getenv("LOG_LEVEL", "INFO")).strip().upper()
    level_value = getattr(logging, lvl, logging.INFO)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RequestContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level_value)

    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def _request_user(request: Request) -> str:
    """从 Bearer Token 解析用户名；解析失败记 anonymous，绝不回传 token。"""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return "-"
    payload = decode_access_token(header[7:].strip())
    if not payload:
        return "anonymous"
    return str(payload.get("sub") or "anonymous")


def _route_path(request: Request) -> str:
    """指标标签使用路由模板，避免设备 ID 等造成基数爆炸。"""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return path
    return "unmatched"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """请求 ID、访问日志与 HTTP 指标的单一入口。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._logger = logging.getLogger("app.request")

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("X-Request-Id", "").strip()
        request_id = (
            incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else uuid.uuid4().hex
        )
        user = _request_user(request)
        token = request_id_ctx.set(request_id)
        user_token = request_user_ctx.set(user)
        request.state.request_id = request_id

        start = time.perf_counter()
        status_code = 500
        error: Optional[str] = None
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        except Exception as exc:  # 交给 FastAPI 的 500 处理器，但先留下结构化记录
            error = redact_text(f"{type(exc).__name__}: {exc}")
            self._logger.error(
                "request failed",
                extra={
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "durationMs": round((time.perf_counter() - start) * 1000, 2),
                    "clientIp": request.client.host if request.client else "-",
                    "user": user,
                    "error": error,
                },
            )
            raise
        finally:
            duration = time.perf_counter() - start
            # /metrics 是抓取端点，不记录自身日志与指标，避免自我放大
            if request.url.path != "/metrics":
                path_label = _route_path(request)
                observe_request(request.method, path_label, status_code, duration)
                log_method = (
                    self._logger.warning
                    if status_code >= 400
                    else self._logger.info
                )
                log_method(
                    "%s %s -> %s %.1fms",
                    request.method,
                    request.url.path,
                    status_code,
                    duration * 1000,
                    extra={
                        "event": "http_request",
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_code,
                        "durationMs": round(duration * 1000, 2),
                        "clientIp": request.client.host if request.client else "-",
                        "user": user,
                        "error": error,
                    },
                )
            request_id_ctx.reset(token)
            request_user_ctx.reset(user_token)
