from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.camera import CameraHub
from imp_house.config import Settings
from imp_house.db import Database, as_utc
from imp_house.models import Device, User
from imp_house.scheduler import RecordingScheduler
from imp_house.security import decode_session_token, hash_device_token

SESSION_COOKIE = "imp_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "imp-house"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_LAST_SEEN_RESOLUTION = timedelta(minutes=1)


@dataclass
class AppState:
    settings: Settings
    db: Database
    hub: CameraHub
    scheduler: RecordingScheduler


def get_state(request: Request) -> AppState:
    return request.app.state.imp


async def get_session(state: AppState = Depends(get_state)) -> AsyncIterator[AsyncSession]:
    async with state.db.sessionmaker() as session:
        yield session


def require_csrf_header(request: Request) -> None:
    if request.method not in _SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing CSRF header")


async def current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> User:
    require_csrf_header(request)
    token = request.cookies.get(SESSION_COOKIE)
    user_id = decode_session_token(token, state.settings.secret_key.get()) if token else None
    user = await session.get(User, user_id) if user_id else None
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin required")
    return user


async def current_device(request: Request, session: AsyncSession = Depends(get_session)) -> Device:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing device token")
    device = await session.scalar(select(Device).where(Device.token_hash == hash_device_token(token.strip())))
    owner = await session.get(User, device.owner_id) if device else None
    if device is None or owner is None or not owner.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid device token")
    now = datetime.now(UTC)
    if device.last_seen_at is None or now - as_utc(device.last_seen_at) > _LAST_SEEN_RESOLUTION:
        device.last_seen_at = now
        await session.commit()
    return device
