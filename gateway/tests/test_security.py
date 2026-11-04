"""鉴权单测：口令哈希、JWT 角色声明与角色依赖。"""

import asyncio

import pytest
from fastapi import HTTPException

from app.security import (
    ROLE_OPERATOR,
    ROLE_VIEWER,
    create_access_token,
    decode_access_token,
    hash_password,
    require_role,
    verify_password,
)


def test_password_hash_roundtrip():
    stored = hash_password("s3cret")
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret", stored) is True
    assert verify_password("wrong", stored) is False


def test_password_hash_is_salted():
    """同一口令两次哈希结果必须不同（随机 salt）。"""
    assert hash_password("same") != hash_password("same")


def test_verify_password_rejects_malformed():
    assert verify_password("any", "") is False
    assert verify_password("any", "md5$abc") is False


def test_token_carries_role():
    token = create_access_token("operator", role=ROLE_OPERATOR)
    payload = decode_access_token(token)
    assert payload["sub"] == "operator"
    assert payload["role"] == ROLE_OPERATOR


def test_require_role_denies_viewer_for_operator_action():
    dependency = require_role(ROLE_OPERATOR)

    async def call():
        await dependency(user={"sub": "viewer", "role": ROLE_VIEWER})

    with pytest.raises(HTTPException) as exc:
        asyncio.run(call())
    assert exc.value.status_code == 403


def test_require_role_allows_admin_everywhere():
    dependency = require_role(ROLE_VIEWER)

    async def call():
        return await dependency(user={"sub": "root", "role": "admin"})

    assert asyncio.run(call())["sub"] == "root"
