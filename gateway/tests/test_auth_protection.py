"""v1.16.0 登录防护：生产 fail-fast、演示账号开关、限流与口令强度。"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import main
from app.api.routes import auth as auth_route
from app.config import Settings
from app.database import get_session
from app.models import AppUser
from app.security import ROLE_ADMIN, ROLE_OPERATOR, hash_password
from app.services import user_service
from app.services.login_guard import LoginRateLimiter
from db_utils import create_test_engine


def _secure_production_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "jwt_secret": "k" * 48,
        "admin_password": "StrongAdmin!2026",
        "database_url": "postgresql+asyncpg://iot:StrongDbPass2026@db:5432/iot",
        "seed_demo_users": None,
        # v1.17.0：生产环境 Broker 禁止匿名，网关与客户端凭证必须显式配置
        "mqtt_username": "iot-gateway",
        "mqtt_password": "GatewayBrokerSecret!2026",
        "mqtt_web_password": "WebBrokerSecret!2026",
        "mqtt_mobile_password": "MobileBrokerSecret!2026",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_production_default_secrets_abort_startup(monkeypatch):
    """APP_ENV=production 且仍是默认 JWT/口令/SQLite 时，lifespan 直接拒绝启动。

    显式指定默认 SQLite 连接串：CI 的 Postgres 作业会注入 DATABASE_URL，
    若不显式设置，这条用例会被环境变量「意外修好」，失去原本的断言价值。
    """
    insecure = Settings(
        _env_file=None,
        app_env="production",
        database_url="sqlite+aiosqlite:///./data/iot.db",
    )
    monkeypatch.setattr(main, "settings", insecure)

    async def boot():
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(boot())

    message = str(exc.value)
    assert "JWT_SECRET" in message
    assert "ADMIN_PASSWORD" in message
    assert "DATABASE_URL" in message


def test_production_secure_settings_pass_validation():
    _secure_production_settings().validate_for_production()


def test_production_rejects_short_jwt_even_if_not_known_default():
    cfg = _secure_production_settings(jwt_secret="short-secret")
    with pytest.raises(RuntimeError) as exc:
        cfg.validate_for_production()
    assert "长度不足 32 位" in str(exc.value)


def test_development_keeps_defaults_with_warnings():
    cfg = Settings(_env_file=None, app_env="development")
    assert cfg.validate_for_production() is None
    warnings = cfg.security_warnings()
    assert any("JWT_SECRET" in item for item in warnings)
    assert any("ADMIN_PASSWORD" in item for item in warnings)


async def _seed_users_and_read(tmp_path, cfg) -> tuple[list[str], list[str]]:
    engine = await create_test_engine(tmp_path, "bootstrap.db")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        created = await user_service.ensure_bootstrap_users(session, cfg)
        await session.commit()
    async with factory() as session:
        usernames = sorted((await session.execute(select(AppUser.username))).scalars().all())
    await engine.dispose()
    return created, usernames


def test_production_does_not_seed_demo_users(tmp_path):
    cfg = _secure_production_settings()
    created, usernames = asyncio.run(_seed_users_and_read(tmp_path, cfg))
    assert created == ["admin"]
    assert usernames == ["admin"]


def test_production_seeds_demo_users_only_when_explicitly_enabled(tmp_path):
    cfg = _secure_production_settings(seed_demo_users=True)
    created, usernames = asyncio.run(_seed_users_and_read(tmp_path, cfg))
    assert usernames == ["admin", "operator", "viewer"]
    assert set(created) == {"admin", "operator", "viewer"}


def test_development_seeds_demo_users_by_default(tmp_path):
    cfg = Settings(_env_file=None, app_env="development")
    created, usernames = asyncio.run(_seed_users_and_read(tmp_path, cfg))
    assert usernames == ["admin", "operator", "viewer"]
    assert set(created) == {"admin", "operator", "viewer"}


def test_production_rejects_missing_mqtt_credentials():
    """Broker 禁止匿名后，生产环境缺 MQTT 凭证必须 fail-fast。"""
    cfg = _secure_production_settings(
        mqtt_password="",
        mqtt_web_password="",
        mqtt_mobile_password="",
    )
    with pytest.raises(RuntimeError) as exc:
        cfg.validate_for_production()
    message = str(exc.value)
    assert "MQTT_PASSWORD" in message
    assert "MQTT_WEB_PASSWORD" in message
    assert "MQTT_MOBILE_PASSWORD" in message


def test_production_requires_gateway_identity_name():
    cfg = _secure_production_settings(mqtt_username="some-device")
    with pytest.raises(RuntimeError) as exc:
        cfg.validate_for_production()
    assert "iot-gateway" in str(exc.value)


class _LoginClient:
    """临时 SQLite + FastAPI 路由，只替换会话，不启动 MQTT。"""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.engine = None
        self.factory = None

    async def __aenter__(self):
        self.engine = await create_test_engine(self.tmp_path, "login.db")
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

        async def override_session():
            async with self.factory() as session:
                try:
                    yield session
                except Exception:
                    await session.rollback()
                    raise

        main.app.dependency_overrides[get_session] = override_session
        self.client = AsyncClient(
            transport=ASGITransport(app=main.app), base_url="http://test"
        )
        await self.client.__aenter__()
        return self.client, self.factory

    async def __aexit__(self, *args):
        await self.client.__aexit__(*args)
        main.app.dependency_overrides.clear()
        await self.engine.dispose()


async def _seed_user(factory, username: str, password: str, role: str = ROLE_ADMIN):
    async with factory() as session:
        session.add(
            AppUser(
                username=username,
                password_hash=hash_password(password),
                role=role,
                active=True,
            )
        )
        await session.commit()


def _run_login(tmp_path, scenario):
    async def wrapper():
        async with _LoginClient(tmp_path) as (client, factory):
            await scenario(client, factory)

    asyncio.run(wrapper())


def test_login_rate_limit_429_and_retry_after(tmp_path, monkeypatch):
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, lockout_seconds=30)
    monkeypatch.setattr(auth_route, "login_limiter", limiter)

    async def scenario(client, factory):
        await _seed_user(factory, "alice", "CorrectPass!2026")
        for _ in range(2):
            resp = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong"}
            )
            assert resp.status_code == 401, resp.text
        blocked = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "CorrectPass!2026"}
        )
        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["code"] == 429
        retry_after = blocked.headers.get("Retry-After")
        assert retry_after is not None and 29 <= int(retry_after) <= 30

    _run_login(tmp_path, scenario)


def test_successful_login_resets_failure_counter(tmp_path, monkeypatch):
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, lockout_seconds=30)
    monkeypatch.setattr(auth_route, "login_limiter", limiter)

    async def scenario(client, factory):
        await _seed_user(factory, "alice", "CorrectPass!2026")
        wrong = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert wrong.status_code == 401
        ok = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "CorrectPass!2026"}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["data"]["token"]

        # 成功登录后计数清零：再失败两次仍只是 401，第三次才 429
        for _ in range(2):
            resp = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong"}
            )
            assert resp.status_code == 401
        blocked = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert blocked.status_code == 429

    _run_login(tmp_path, scenario)


def test_rate_limit_is_per_ip_and_account(tmp_path, monkeypatch):
    limiter = LoginRateLimiter(max_failures=2, window_seconds=60, lockout_seconds=30)
    monkeypatch.setattr(auth_route, "login_limiter", limiter)

    async def scenario(client, factory):
        await _seed_user(factory, "alice", "CorrectPass!2026")
        await _seed_user(factory, "bob", "AnotherPass!2026", role=ROLE_OPERATOR)
        for _ in range(2):
            resp = await client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong"}
            )
            assert resp.status_code == 401
        alice_blocked = await client.post(
            "/api/auth/login", json={"username": "alice", "password": "CorrectPass!2026"}
        )
        assert alice_blocked.status_code == 429

        bob = await client.post(
            "/api/auth/login", json={"username": "bob", "password": "AnotherPass!2026"}
        )
        assert bob.status_code == 200, bob.text
        assert bob.json()["data"]["user"]["role"] == ROLE_OPERATOR

    _run_login(tmp_path, scenario)


def test_weak_passwords_are_rejected_for_create_and_reset(tmp_path):
    async def scenario():
        engine = await create_test_engine(tmp_path, "weak.db")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            created, error = await user_service.create_user(
                session, "weak-user", "admin123", "viewer"
            )
            assert created is None
            assert "口令" in error

            created, error = await user_service.create_user(
                session, "short-user", "abc123", "viewer"
            )
            assert created is None
            assert "至少" in error

            created, error = await user_service.create_user(
                session, "strong-user", "Str0ng-Pass-2026!", "viewer"
            )
            assert created is not None and error == ""

            updated, error = await user_service.update_user(
                session, "strong-user", None, None, "viewer123"
            )
            assert updated is None
            assert "口令" in error
        await engine.dispose()

    asyncio.run(scenario())
