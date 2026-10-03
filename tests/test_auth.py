import asyncio

import httpx
import pytest

from imp_house.api.auth import LoginAttempts
from tests.conftest import CSRF, signup


async def test_first_user_is_admin_second_is_not(client: httpx.AsyncClient) -> None:
    first = await signup(client, "Admin@Example.com")
    assert first.status_code == 201
    assert first.json()["is_admin"] is True
    assert first.json()["email"] == "admin@example.com"
    client.cookies.clear()
    second = await signup(client, "user@example.com")
    assert second.json()["is_admin"] is False


async def test_duplicate_email_conflict(client: httpx.AsyncClient) -> None:
    await signup(client, "a@example.com")
    assert (await signup(client, "A@example.com")).status_code == 409


async def test_short_password_rejected(client: httpx.AsyncClient) -> None:
    assert (await signup(client, "a@example.com", "short")).status_code == 422


async def test_csrf_header_required(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as bare:
        response = await bare.post("/api/auth/signup", json={"email": "a@example.com", "password": "x" * 12})
        assert response.status_code == 403


async def test_session_cookie_flags(client: httpx.AsyncClient) -> None:
    response = await signup(client, "a@example.com")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


async def test_login_logout_me(client: httpx.AsyncClient) -> None:
    await signup(client, "a@example.com")
    client.cookies.clear()
    assert (await client.get("/api/auth/me")).status_code == 401
    bad = await client.post("/api/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
    assert bad.status_code == 401
    ok = await client.post("/api/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"})
    assert ok.status_code == 200
    assert (await client.get("/api/auth/me")).json()["email"] == "a@example.com"
    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_login_rate_limited(client: httpx.AsyncClient) -> None:
    await signup(client, "a@example.com")
    for _ in range(10):
        await client.post("/api/auth/login", json={"email": "a@example.com", "password": "nope-nope-1"})
    blocked = await client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "correct-horse-1"}
    )
    assert blocked.status_code == 429


def test_login_attempt_store_is_bounded() -> None:
    attempts = LoginAttempts(max_keys=2)
    attempts.fail("first@example.com")
    attempts.fail("second@example.com")
    attempts.fail("third@example.com")
    assert len(attempts._failures) == 2
    assert "first@example.com" not in attempts._failures


async def test_concurrent_first_signup_has_one_admin(app) -> None:
    if app.state.imp.db.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL advisory lock")
    transport = httpx.ASGITransport(app=app)

    async def create_user(email: str) -> httpx.Response:
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as client:
            return await signup(client, email)

    responses = await asyncio.gather(
        create_user("first@example.com"), create_user("second@example.com")
    )
    assert all(response.status_code == 201 for response in responses)
    assert sum(response.json()["is_admin"] for response in responses) == 1


async def test_tampered_session_rejected(client: httpx.AsyncClient) -> None:
    await signup(client, "a@example.com")
    client.cookies.set("imp_session", client.cookies["imp_session"][:-2] + "xx")
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_admin_can_disable_user(app, admin_client: httpx.AsyncClient) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as other:
        user_id = (await signup(other, "user@example.com")).json()["id"]
        assert (await other.get("/api/users")).status_code == 403
        response = await admin_client.patch(f"/api/users/{user_id}", json={"is_active": False})
        assert response.json()["is_active"] is False
        assert (await other.get("/api/auth/me")).status_code == 401
    me = (await admin_client.get("/api/auth/me")).json()
    assert (await admin_client.patch(f"/api/users/{me['id']}", json={"is_admin": False})).status_code == 400
