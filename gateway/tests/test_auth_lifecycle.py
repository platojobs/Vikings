"""v1.24.0 / P2-4：访问令牌短 TTL + 可吊销 refresh 的真实行为。

覆盖：签发字段、轮换单次使用、登出/改密/管理员吊销、过期拒绝、
旧令牌重放检测，以及「吊销后旧 access 在剩余 TTL 内仍可用」的明确语义。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import main
from app.api.routes import auth as auth_route
from app.database import get_session
from app.models import AppUser, RefreshToken
from app.security import ROLE_ADMIN, decode_access_token, hash_password
from app.services import token_service
from app.services.login_guard import LoginRateLimiter
from db_utils import create_test_engine

ACCESS_PASSWORD = "StrongPass!2026"


class _AuthClient:
    """临时 SQLite + FastAPI 路由；不启动 MQTT，不依赖外部 Broker。"""

    def __init__(self, tmp_path, filename="auth.db"):
        self.tmp_path = tmp_path
        self.filename = filename
        self.engine = None
        self.factory = None

    async def __aenter__(self):
        self.engine = await create_test_engine(self.tmp_path, self.filename)
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


def _run(tmp_path, scenario, filename="auth.db"):
    limiter = LoginRateLimiter(max_failures=50, window_seconds=300, lockout_seconds=30)
    auth_route.login_limiter = limiter

    async def wrapper():
        async with _AuthClient(tmp_path, filename) as (client, factory):
            await scenario(client, factory)

    asyncio.run(wrapper())


async def _seed(factory, username: str, password: str = ACCESS_PASSWORD, role: str = ROLE_ADMIN):
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


async def _login(client, username: str) -> dict:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": ACCESS_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _refresh(client, refresh_token: str):
    return await client.post(
        "/api/auth/refresh", json={"refresh_token": refresh_token}
    )


def test_login_returns_short_access_and_hashed_refresh(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        data = await _login(client, "alice")
        assert data["token"] and data["refresh_token"]
        assert data["token_type"] == "bearer"
        assert data["expires_in"] == 1200

        payload = decode_access_token(data["token"])
        assert payload["sub"] == "alice"
        ttl = payload["exp"] - datetime.now(timezone.utc).timestamp()
        assert 0 < ttl <= 1200, f"access TTL 应不超过配置值，实际 {ttl}"

        async with factory() as session:
            rows = (
                await session.execute(select(RefreshToken))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].token_hash == token_service.fingerprint(data["refresh_token"])
            hashes = {row.token_hash for row in rows}
            assert data["refresh_token"] not in hashes, "数据库不得保存明文 refresh"

    _run(tmp_path, scenario, "login.db")


def test_refresh_rotates_and_old_token_is_single_use(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        data = await _login(client, "alice")
        first_refresh = data["refresh_token"]

        renewed = await _refresh(client, first_refresh)
        assert renewed.status_code == 200, renewed.text
        new_data = renewed.json()["data"]
        assert new_data["refresh_token"] != first_refresh

        me = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {new_data['token']}"},
        )
        assert me.status_code == 200, me.text

        # 轮换后的旧令牌立即失效
        replay = await _refresh(client, first_refresh)
        assert replay.status_code == 401
        assert "已被使用" in replay.json()["message"]

        # 重放会按安全策略吊销该用户全部会话，包括刚换到的新令牌
        after_reuse = await _refresh(client, new_data["refresh_token"])
        assert after_reuse.status_code == 401

    _run(tmp_path, scenario, "rotate.db")


def test_logout_revokes_current_refresh(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        data = await _login(client, "alice")

        out = await client.post(
            "/api/auth/logout", json={"refresh_token": data["refresh_token"]}
        )
        assert out.status_code == 200, out.text
        assert out.json()["data"]["revoked"] is True

        again = await _refresh(client, data["refresh_token"])
        assert again.status_code == 401

    _run(tmp_path, scenario, "logout.db")


def test_password_change_revokes_all_sessions_but_access_survives_ttl(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        await _seed(factory, "bob", role=ROLE_ADMIN)
        first = await _login(client, "alice")
        second = await _login(client, "alice")

        admin = await _login(client, "bob")
        patched = await client.patch(
            "/api/users/alice",
            json={"password": "NewStrongPass!2026"},
            headers={"Authorization": f"Bearer {admin['token']}"},
        )
        assert patched.status_code == 200, patched.text

        for token in (first["refresh_token"], second["refresh_token"]):
            assert (await _refresh(client, token)).status_code == 401

        # 明确语义：已签发的 access 在剩余 TTL 内仍可用（不是「立即全局生效」）
        still_valid = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {first['token']}"},
        )
        assert still_valid.status_code == 200, still_valid.text

    _run(tmp_path, scenario, "password.db")


def test_admin_revoke_all_sessions(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        await _seed(factory, "bob", role=ROLE_ADMIN)
        sessions = [await _login(client, "alice") for _ in range(2)]
        admin = await _login(client, "bob")

        resp = await client.post(
            "/api/users/alice/sessions/revoke",
            headers={"Authorization": f"Bearer {admin['token']}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["revoked"] == 2

        for item in sessions:
            assert (await _refresh(client, item["refresh_token"])).status_code == 401

    _run(tmp_path, scenario, "admin_revoke.db")


def test_expired_refresh_token_is_rejected(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        data = await _login(client, "alice")

        async with factory() as session:
            await session.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.token_hash
                    == token_service.fingerprint(data["refresh_token"])
                )
                .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
            )
            await session.commit()

        resp = await _refresh(client, data["refresh_token"])
        assert resp.status_code == 401
        assert "过期" in resp.json()["message"]

    _run(tmp_path, scenario, "expired.db")


def test_unknown_refresh_token_is_rejected(tmp_path):
    async def scenario(client, factory):
        await _seed(factory, "alice")
        resp = await _refresh(client, "not-a-real-refresh-token")
        assert resp.status_code == 401
        assert "无效" in resp.json()["message"]

    _run(tmp_path, scenario, "unknown.db")
