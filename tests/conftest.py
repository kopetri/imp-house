import os
from collections.abc import AsyncIterator
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from imp_house.api.auth import login_attempts
from imp_house.config import Secret, Settings
from imp_house.db import Base
from imp_house.main import create_app

CSRF = {"X-Requested-With": "imp-house"}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    return Settings(
        database_url=url,
        secret_key=Secret("x" * 40),
        replicate_api_key=Secret("test-key"),
        camera_url="http://camera.test/stream",
        data_dir=tmp_path / "data",
        timezone=ZoneInfo("Europe/Berlin"),
        cookie_secure=False,
    )


@pytest.fixture
async def app(settings: Settings):
    application = create_app(settings, start_background=False)
    async with application.router.lifespan_context(application):
        engine = application.state.imp.db.engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        login_attempts._failures.clear()
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=CSRF) as c:
        yield c


async def signup(client: httpx.AsyncClient, email: str, password: str = "correct-horse-1") -> httpx.Response:
    return await client.post("/api/auth/signup", json={"email": email, "password": password})


@pytest.fixture
async def admin_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    response = await signup(client, "admin@example.com")
    assert response.status_code == 201
    return client


def jpeg(payload: bytes = b"x") -> bytes:
    return b"\xff\xd8\xff\xe0" + payload + b"\xff\xd9"
