from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.api.deps import AppState, current_admin, current_user, get_session, get_state
from imp_house.models import Prompt, RecordingSettings, User
from imp_house.scheduler import get_recording_settings

prompts_router = APIRouter(prefix="/api/prompts", tags=["prompts"])
settings_router = APIRouter(prefix="/api/settings", tags=["settings"])

class PromptIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


class PromptOut(PromptIn):
    id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "extra": "forbid"}


async def _get_prompt(session: AsyncSession, prompt_id: int) -> Prompt:
    prompt = await session.get(Prompt, prompt_id)
    if prompt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
    return prompt


@prompts_router.get("", response_model=list[PromptOut])
async def list_prompts(
    _: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> list[Prompt]:
    return list((await session.scalars(select(Prompt).order_by(Prompt.id))).all())


@prompts_router.post("", response_model=PromptOut, status_code=status.HTTP_201_CREATED)
async def create_prompt(
    body: PromptIn, _: User = Depends(current_admin), session: AsyncSession = Depends(get_session)
) -> Prompt:
    prompt = Prompt(**body.model_dump(), is_active=False)
    session.add(prompt)
    await session.commit()
    await session.refresh(prompt)
    return prompt


@prompts_router.put("/{prompt_id}", response_model=PromptOut)
async def update_prompt(
    prompt_id: int,
    body: PromptIn,
    _: User = Depends(current_admin),
    session: AsyncSession = Depends(get_session),
) -> Prompt:
    prompt = await _get_prompt(session, prompt_id)
    for key, value in body.model_dump().items():
        setattr(prompt, key, value)
    await session.commit()
    await session.refresh(prompt)
    return prompt


@prompts_router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_prompt(
    prompt_id: int, _: User = Depends(current_admin), session: AsyncSession = Depends(get_session)
) -> None:
    await session.delete(await _get_prompt(session, prompt_id))
    await session.commit()


@prompts_router.post("/{prompt_id}/activate", response_model=PromptOut)
async def activate_prompt(
    prompt_id: int, _: User = Depends(current_admin), session: AsyncSession = Depends(get_session)
) -> Prompt:
    prompt = await _get_prompt(session, prompt_id)
    await session.execute(update(Prompt).where(Prompt.is_active.is_(True)).values(is_active=False))
    await session.flush()
    prompt.is_active = True
    await session.commit()
    await session.refresh(prompt)
    return prompt


@prompts_router.post("/{prompt_id}/deactivate", response_model=PromptOut)
async def deactivate_prompt(
    prompt_id: int, _: User = Depends(current_admin), session: AsyncSession = Depends(get_session)
) -> Prompt:
    prompt = await _get_prompt(session, prompt_id)
    prompt.is_active = False
    await session.commit()
    await session.refresh(prompt)
    return prompt


class SettingsIn(BaseModel):
    model_config = {"extra": "forbid"}

    enabled: bool
    clips_per_day: int = Field(ge=0, le=50)
    window_start: time
    window_end: time

    @model_validator(mode="after")
    def _check_ranges(self) -> "SettingsIn":
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class SettingsOut(SettingsIn):
    model_config = {"from_attributes": True, "extra": "forbid"}


@settings_router.get("", response_model=SettingsOut)
async def get_settings_endpoint(
    _: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> RecordingSettings:
    settings = await get_recording_settings(session)
    await session.commit()
    return settings


@settings_router.put("", response_model=SettingsOut)
async def update_settings(
    body: SettingsIn,
    _: User = Depends(current_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> RecordingSettings:
    settings = await get_recording_settings(session)
    for key, value in body.model_dump().items():
        setattr(settings, key, value)
    await session.commit()
    await state.scheduler.replan_today(datetime.now(state.settings.timezone))
    return settings
