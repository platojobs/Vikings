"""可观测性（P1-1 / v1.20.0）：请求 ID、结构化日志脱敏、ready 探针与指标。"""

import asyncio
import io
import json
import logging
from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.api.routes import auth as auth_route
from app.api.routes import health as health_route
from app.database import Base, get_session
from app.models import AppUser
from app.observability import (
    JsonFormatter,
    RequestContextFilter,
    redact_text,
)
from app.security import create_access_token, hash_password
from app.services.login_guard import LoginRateLimiter


class _HealthyMqtt:
    is_connected = True
    last_error = None
    message_count = 0


class _OfflineMqtt:
    is_connected = False
    last_error = "connection refused"

    def __init__(self):
        self.message_count = 0


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test")


def _prepare_state(mqtt=None) -> None:
    main.app.state.started_at = datetime.now(timezone.utc)
    main.app.state.mqtt = mqtt or _HealthyMqtt()


def test_request_id_echo_and_generation():
    async def scenario():
        _prepare_state()
        async with _client() as client:
            supplied = await client.get(
                "/api/health", headers={"X-Request-Id": "trace-abc-123"}
            )
            assert supplied.status_code == 200
            assert supplied.headers["x-request-id"] == "trace-abc-123"

            generated = await client.get("/api/health")
            request_id = generated.headers["x-request-id"]
            assert len(request_id) == 32
            assert all(ch in "0123456789abcdef" for ch in request_id)

    asyncio.run(scenario())


def test_error_response_carries_request_id():
    async def scenario():
        _prepare_state()
        async with _client() as client:
            resp = await client.get(
                "/api/devices/not-found",
                headers={"X-Request-Id": "trace-error-1"},
            )
            assert resp.status_code in (401, 404)
            assert resp.headers["x-request-id"] == "trace-error-1"
            assert resp.json()["requestId"] == "trace-error-1"

    asyncio.run(scenario())


def test_ready_ok_returns_200(monkeypatch):
    async def fake_probe():
        return {
            "ok": True,
            "backend": "sqlite",
            "latencyMs": 1.2,
            "error": None,
        }

    monkeypatch.setattr(health_route, "probe_database", fake_probe)

    async def scenario():
        _prepare_state(_HealthyMqtt())
        async with _client() as client:
            resp = await client.get("/api/health/ready")
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["status"] == "ready"
            assert data["components"]["database"]["ok"] is True
            assert data["components"]["mqtt"]["ok"] is True

    asyncio.run(scenario())


def test_ready_reports_db_failure_with_503(monkeypatch):
    async def failing_probe():
        return {
            "ok": False,
            "backend": "postgresql",
            "latencyMs": 3000.0,
            "error": "TimeoutError: database query timed out",
        }

    monkeypatch.setattr(health_route, "probe_database", failing_probe)

    async def scenario():
        _prepare_state(_HealthyMqtt())
        async with _client() as client:
            resp = await client.get("/api/health/ready")
            assert resp.status_code == 503
            data = resp.json()["data"]
            assert data["status"] == "not_ready"
            assert data["components"]["database"]["ok"] is False
            assert data["components"]["mqtt"]["ok"] is True

            # 存活探针不查库：数据库故障不影响进程存活语义
            live = await client.get("/api/health")
            assert live.status_code == 200

    asyncio.run(scenario())


def test_ready_reports_mqtt_failure_with_503(monkeypatch):
    async def ok_probe():
        return {"ok": True, "backend": "sqlite", "latencyMs": 0.8, "error": None}

    monkeypatch.setattr(health_route, "probe_database", ok_probe)

    async def scenario():
        _prepare_state(_OfflineMqtt())
        async with _client() as client:
            resp = await client.get("/api/health/ready")
            assert resp.status_code == 503
            mqtt = resp.json()["data"]["components"]["mqtt"]
            assert mqtt["ok"] is False
            assert "connection refused" in mqtt["lastError"]

    asyncio.run(scenario())


def test_metrics_endpoint_exposes_expected_series():
    async def scenario():
        _prepare_state()
        async with _client() as client:
            await client.get("/api/health")
            resp = await client.get("/metrics")
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/plain")
            body = resp.text
            for name in (
                "iot_http_requests_total",
                "iot_http_request_duration_seconds",
                "iot_mqtt_connected",
                "iot_mqtt_disconnects_total",
                "iot_mqtt_connect_failures_total",
                "iot_login_failures_total",
                "iot_login_rate_limited_total",
                "iot_db_query_errors_total",
                "iot_db_pool_checked_out",
                "iot_db_ready",
            ):
                assert name in body, f"缺少指标 {name}"

    asyncio.run(scenario())


def test_json_formatter_redacts_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.observability.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info(
            "login Authorization: Bearer %s password=%s",
            "secret.token.value",
            "hunter2",
        )
        logger.error(
            "db connect failed postgresql://iot:db-pass@db:5432/iot",
        )
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert "secret.token.value" not in output
    assert "hunter2" not in output
    assert "db-pass" not in output
    payload = json.loads(output.splitlines()[0])
    assert payload["level"] == "INFO"
    assert "[REDACTED]" in payload["message"]


def test_middleware_logs_user_without_token():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestContextFilter())
    logger = logging.getLogger("app.request")
    logger.addHandler(handler)
    old_level, old_propagate = logger.level, logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False

    token = create_access_token("alice", role="viewer")

    async def scenario():
        _prepare_state()
        async with _client() as client:
            resp = await client.get(
                "/api/health",
                headers={"Authorization": f"Bearer {token}", "X-Request-Id": "trace-log-1"},
            )
            assert resp.status_code == 200

    try:
        asyncio.run(scenario())
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    output = stream.getvalue()
    assert token not in output
    record = json.loads(output.splitlines()[-1])
    assert record["user"] == "alice"
    assert record["requestId"] == "trace-log-1"
    assert record["method"] == "GET"
    assert record["path"] == "/api/health"
    assert record["status"] == 200
    assert "durationMs" in record


def test_login_metrics_count_failures_and_429(tmp_path, monkeypatch):
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, lockout_seconds=30)
    monkeypatch.setattr(auth_route, "login_limiter", limiter)

    before_failures = REGISTRY.get_sample_value("iot_login_failures_total") or 0
    before_limited = REGISTRY.get_sample_value("iot_login_rate_limited_total") or 0

    async def scenario():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'obs-login.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            session.add(
                AppUser(
                    username="alice",
                    password_hash=hash_password("CorrectPass!2026"),
                    role="admin",
                    active=True,
                )
            )
            await session.commit()

        async def override_session():
            async with factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        main.app.dependency_overrides[get_session] = override_session
        try:
            async with _client() as client:
                for _ in range(2):
                    resp = await client.post(
                        "/api/auth/login",
                        json={"username": "alice", "password": "wrong"},
                    )
                    assert resp.status_code == 401
                blocked = await client.post(
                    "/api/auth/login",
                    json={"username": "alice", "password": "CorrectPass!2026"},
                )
                assert blocked.status_code == 429
        finally:
            main.app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(scenario())
    assert (REGISTRY.get_sample_value("iot_login_failures_total") or 0) == before_failures + 2
    assert (REGISTRY.get_sample_value("iot_login_rate_limited_total") or 0) == before_limited + 1


def test_redact_text_handles_bearer_and_dsn():
    assert "abc.def" not in redact_text("Authorization: Bearer abc.def")
    assert "secret" not in redact_text("token=secret")
    assert "p@ss" not in redact_text("postgresql://iot:p@ss@db:5432/iot")
