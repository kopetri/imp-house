from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.api.auth import UserOut
from imp_house.api.deps import current_admin, current_user, get_session
from imp_house.models import Device, User
from imp_house.security import hash_device_token, new_device_token

users_router = APIRouter(prefix="/api/users", tags=["users"])
devices_router = APIRouter(prefix="/api/devices", tags=["devices"])


class UserUpdate(BaseModel):
    is_active: bool | None = None
    is_admin: bool | None = None


@users_router.get("", response_model=list[UserOut])
async def list_users(
    _: User = Depends(current_admin), session: AsyncSession = Depends(get_session)
) -> list[User]:
    return list((await session.scalars(select(User).order_by(User.id))).all())


@users_router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    admin: User = Depends(current_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id and (body.is_active is False or body.is_admin is False):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot deactivate or demote yourself")
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    await session.commit()
    return user


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class DeviceOut(BaseModel):
    id: int
    name: str
    owner_id: int
    created_at: datetime
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class DeviceCreated(DeviceOut):
    token: str


@devices_router.get("", response_model=list[DeviceOut])
async def list_devices(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Device]:
    query = select(Device).order_by(Device.id)
    if not user.is_admin:
        query = query.where(Device.owner_id == user.id)
    return list((await session.scalars(query)).all())


@devices_router.post("", response_model=DeviceCreated, status_code=status.HTTP_201_CREATED)
async def create_device(
    body: DeviceCreate, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> DeviceCreated:
    token = new_device_token()
    device = Device(name=body.name, token_hash=hash_device_token(token), owner_id=user.id)
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return DeviceCreated.model_validate({**DeviceOut.model_validate(device).model_dump(), "token": token})


@devices_router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(
    device_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> None:
    device = await session.get(Device, device_id)
    if device is None or (device.owner_id != user.id and not user.is_admin):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    await session.delete(device)
    await session.commit()
