import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.api.deps import AppState, current_admin, current_device, current_user, get_session, get_state
from imp_house.camera import CameraHub
from imp_house.db import as_utc
from imp_house.mjpeg import CONTENT_TYPE, encode_part, iter_playback_frames
from imp_house.models import Clip, ClipStatus, Device, ScheduledRecording, User
from imp_house.recorder import active_prompt, queue_clip

log = logging.getLogger(__name__)

clips_router = APIRouter(prefix="/api/clips", tags=["clips"])
live_router = APIRouter(prefix="/api", tags=["live"])
device_router = APIRouter(prefix="/api/device", tags=["device"])

_NO_CACHE = {"Cache-Control": "no-store"}


class ClipOut(BaseModel):
    id: uuid.UUID
    recorded_at: datetime
    duration_seconds: float
    status: ClipStatus
    prompt_name: str | None
    prompt_text: str | None
    model: str | None
    attempts: int
    error: str | None
    has_snapshot: bool
    has_original: bool
    has_mask: bool
    has_reference: bool
    has_augmented: bool
    scene_prompt: str | None
    processing_stage: str | None
    playback_frames: int | None

    @classmethod
    def from_clip(cls, clip: Clip) -> "ClipOut":
        return cls(
            id=clip.id,
            recorded_at=clip.recorded_at,
            duration_seconds=clip.duration_seconds,
            status=clip.status,
            prompt_name=clip.prompt_name,
            prompt_text=clip.prompt_text,
            model=clip.model,
            attempts=clip.attempts,
            error=clip.error,
            has_snapshot=clip.snapshot_path is not None,
            has_original=clip.original_path is not None,
            has_mask=clip.mask_path is not None,
            has_reference=clip.reference_path is not None,
            has_augmented=clip.augmented_path is not None,
            scene_prompt=clip.scene_prompt,
            processing_stage=clip.pipeline_stage,
            playback_frames=clip.playback_frames,
        )


class ClipPage(BaseModel):
    items: list[ClipOut]
    total: int


class ScheduledOut(BaseModel):
    id: int
    scheduled_at: datetime
    status: str
    clip_id: uuid.UUID | None
    error: str | None

    model_config = {"from_attributes": True}


def _safe_path(state: AppState, relative: str | None) -> Path:
    if not relative:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not available")
    path = (state.settings.data_dir / relative).resolve()
    if not path.is_relative_to(state.settings.data_dir) or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not available")
    return path


async def _get_clip(session: AsyncSession, clip_id: uuid.UUID) -> Clip:
    clip = await session.get(Clip, clip_id)
    if clip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Clip not found")
    return clip


@clips_router.get("", response_model=ClipPage)
async def list_clips(
    status_filter: ClipStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ClipPage:
    query = select(Clip)
    count = select(func.count()).select_from(Clip)
    if status_filter:
        query = query.where(Clip.status == status_filter)
        count = count.where(Clip.status == status_filter)
    clips = (await session.scalars(query.order_by(Clip.recorded_at.desc()).limit(limit).offset(offset))).all()
    return ClipPage(items=[ClipOut.from_clip(c) for c in clips], total=await session.scalar(count) or 0)


@clips_router.get("/schedule", response_model=list[ScheduledOut])
async def list_schedule(
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> list[ScheduledRecording]:
    today = datetime.now(state.settings.timezone).date()
    query = select(ScheduledRecording).where(ScheduledRecording.plan_date == today)
    return list((await session.scalars(query.order_by(ScheduledRecording.scheduled_at))).all())


@clips_router.post("/record", status_code=status.HTTP_202_ACCEPTED)
async def record_now(
    background: BackgroundTasks,
    _: User = Depends(current_admin),
    state: AppState = Depends(get_state),
) -> dict[str, str]:
    async def run() -> None:
        try:
            await state.scheduler.record_now()
        except Exception as exc:
            log.warning("Manual recording failed: %s", exc)

    background.add_task(run)
    return {"status": "capturing"}


@clips_router.get("/{clip_id}", response_model=ClipOut)
async def get_clip(
    clip_id: uuid.UUID, _: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> ClipOut:
    return ClipOut.from_clip(await _get_clip(session, clip_id))


@clips_router.get("/{clip_id}/{kind}")
async def clip_file(
    clip_id: uuid.UUID,
    kind: str,
    _: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> FileResponse:
    clip = await _get_clip(session, clip_id)
    files = {
        "original.mp4": (clip.original_path, "video/mp4"),
        "snapshot.jpg": (clip.snapshot_path, "image/jpeg"),
        "mask.png": (clip.mask_path, "image/png"),
        "reference.jpg": (clip.reference_path, "image/jpeg"),
        "augmented.mp4": (clip.augmented_path, "video/mp4"),
        "thumb.jpg": (clip.thumbnail_path, "image/jpeg"),
    }
    if kind not in files:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown file")
    relative, media_type = files[kind]
    return FileResponse(_safe_path(state, relative), media_type=media_type)


@clips_router.post("/{clip_id}/process", response_model=ClipOut)
async def process_clip(
    clip_id: uuid.UUID, _: User = Depends(current_admin), session: AsyncSession = Depends(get_session)
) -> ClipOut:
    clip = await _get_clip(session, clip_id)
    if clip.status in (ClipStatus.QUEUED, ClipStatus.PROCESSING):
        raise HTTPException(status.HTTP_409_CONFLICT, "Clip is already being processed")
    if not clip.snapshot_path:
        raise HTTPException(status.HTTP_409_CONFLICT, "Archived video clips cannot be processed")
    prompt = await active_prompt(session)
    if prompt is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No active prompt")
    queue_clip(clip, prompt)
    await session.commit()
    return ClipOut.from_clip(clip)


@clips_router.delete("/{clip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_clip(
    clip_id: uuid.UUID,
    _: User = Depends(current_admin),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> None:
    clip = await _get_clip(session, clip_id)
    if clip.status == ClipStatus.PROCESSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Clip is being processed")
    relative_path = clip.snapshot_path or clip.original_path or clip.thumbnail_path
    clip_dir = (state.settings.data_dir / relative_path).resolve().parent
    await session.delete(clip)
    await session.commit()
    if clip_dir.is_relative_to(state.settings.data_dir / "clips"):
        shutil.rmtree(clip_dir, ignore_errors=True)


async def _live_parts(hub: CameraHub, request: Request) -> AsyncIterator[bytes]:
    async with hub.subscribe() as queue:
        while not await request.is_disconnected():
            try:
                frame = await asyncio.wait_for(queue.get(), 5.0)
            except TimeoutError:
                continue
            yield encode_part(frame)


def _live_response(state: AppState, request: Request) -> StreamingResponse:
    return StreamingResponse(_live_parts(state.hub, request), media_type=CONTENT_TYPE, headers=_NO_CACHE)


@live_router.get("/live.mjpeg")
async def live(request: Request, _: User = Depends(current_user), state: AppState = Depends(get_state)):
    return _live_response(state, request)


@live_router.get("/live/status")
async def live_status(_: User = Depends(current_user), state: AppState = Depends(get_state)) -> dict:
    return {"connected": state.hub.connected}


class DeviceClip(BaseModel):
    id: uuid.UUID
    t: str
    p: str
    d: int


class DeviceClipPage(BaseModel):
    clips: list[DeviceClip]
    total: int


@device_router.get("/clips", response_model=DeviceClipPage)
async def device_clips(
    limit: int = Query(default=5, ge=1, le=20),
    offset: int = Query(default=0, ge=0),
    _: Device = Depends(current_device),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> DeviceClipPage:
    done = Clip.status == ClipStatus.DONE
    clips = (
        await session.scalars(
            select(Clip).where(done).order_by(Clip.recorded_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(Clip).where(done)) or 0
    tz = state.settings.timezone
    return DeviceClipPage(
        clips=[
            DeviceClip(
                id=clip.id,
                t=as_utc(clip.recorded_at).astimezone(tz).strftime("%d.%m. %H:%M"),
                p=(clip.prompt_name or "")[:24],
                d=round((clip.playback_frames or 0) / (clip.playback_fps or 1)),
            )
            for clip in clips
        ],
        total=total,
    )


async def _playback_parts(path: Path, request: Request) -> AsyncIterator[bytes]:
    fps, frames = iter_playback_frames(path)
    interval = 1.0 / fps
    next_at = time.monotonic()
    for frame in frames:
        if await request.is_disconnected():
            return
        delay = next_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        yield encode_part(frame)
        next_at += interval


@device_router.get("/clips/{clip_id}/playback.mjpeg")
async def device_playback(
    clip_id: uuid.UUID,
    request: Request,
    _: Device = Depends(current_device),
    session: AsyncSession = Depends(get_session),
    state: AppState = Depends(get_state),
) -> StreamingResponse:
    clip = await _get_clip(session, clip_id)
    if clip.status != ClipStatus.DONE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Clip not ready")
    path = _safe_path(state, clip.playback_path)
    return StreamingResponse(_playback_parts(path, request), media_type=CONTENT_TYPE, headers=_NO_CACHE)


@device_router.get("/live.mjpeg")
async def device_live(
    request: Request, _: Device = Depends(current_device), state: AppState = Depends(get_state)
):
    return _live_response(state, request)
