import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.camera import CameraHub
from imp_house.models import Clip, ClipStatus, Prompt

log = logging.getLogger(__name__)

FRAME_TIMEOUT_SECONDS = 15.0
GENERATED_VIDEO_SECONDS = 5


class RecordingError(RuntimeError):
    pass


async def active_prompt(session: AsyncSession) -> Prompt | None:
    return await session.scalar(select(Prompt).where(Prompt.is_active.is_(True)))


def queue_clip(clip: Clip, prompt: Prompt) -> None:
    clip.status = ClipStatus.QUEUED
    clip.prompt_id = prompt.id
    clip.prompt_name = prompt.name
    clip.prompt_text = prompt.text
    clip.model = None
    clip.provider_job_id = None
    clip.provider_output = None
    clip.scene_prompt = None
    clip.mask_bbox = None
    clip.pipeline_stage = "scene"
    clip.attempts = 0
    clip.error = None
    clip.augmented_path = None
    clip.playback_path = None
    clip.playback_fps = None
    clip.playback_frames = None
    clip.mask_path = None
    clip.reference_path = None


async def capture_snapshot(hub: CameraHub) -> bytes:
    if hub.latest_frame is not None:
        return hub.latest_frame
    async with hub.subscribe() as queue:
        try:
            return await asyncio.wait_for(queue.get(), FRAME_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise RecordingError("No frames received from camera") from exc


async def record_clip(
    hub: CameraHub, session: AsyncSession, data_dir: Path, now: datetime
) -> Clip:
    snapshot = await capture_snapshot(hub)
    clip_id = uuid.uuid4()
    relative_dir = Path("clips") / now.strftime("%Y-%m-%d") / str(clip_id)
    clip_dir = data_dir / relative_dir
    clip_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = relative_dir / "snapshot.jpg"
    (data_dir / snapshot_path).write_bytes(snapshot)

    clip = Clip(
        id=clip_id,
        recorded_at=now,
        duration_seconds=GENERATED_VIDEO_SECONDS,
        status=ClipStatus.RECORDED,
        original_path=None,
        snapshot_path=str(snapshot_path),
        thumbnail_path=str(snapshot_path),
        attempts=0,
    )
    prompt = await active_prompt(session)
    if prompt:
        queue_clip(clip, prompt)
    session.add(clip)
    await session.commit()
    log.info("Captured snapshot for clip %s", clip_id)
    return clip
