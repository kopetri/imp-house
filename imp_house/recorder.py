import asyncio
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from imp_house.camera import CameraHub
from imp_house.media import encode_jpegs_to_mp4
from imp_house.models import Clip, ClipStatus, Prompt

log = logging.getLogger(__name__)

FRAME_TIMEOUT_SECONDS = 15.0


class RecordingError(RuntimeError):
    pass


async def active_prompt(session: AsyncSession) -> Prompt | None:
    return await session.scalar(select(Prompt).where(Prompt.is_active.is_(True)))


def queue_clip(clip: Clip, prompt: Prompt) -> None:
    clip.status = ClipStatus.QUEUED
    clip.prompt_id = prompt.id
    clip.prompt_name = prompt.name
    clip.prompt_text = prompt.text
    clip.model = prompt.model
    clip.model_input = {
        "video_input_key": prompt.video_input_key,
        "prompt_input_key": prompt.prompt_input_key,
        "extra_input": prompt.extra_input or {},
    }
    clip.provider_job_id = None
    clip.attempts = 0
    clip.error = None
    clip.augmented_path = None
    clip.playback_path = None
    clip.playback_fps = None
    clip.playback_frames = None


async def capture_frames(hub: CameraHub, duration_seconds: float) -> tuple[list[bytes], float]:
    frames: list[bytes] = []
    async with hub.subscribe() as queue:
        try:
            frames.append(await asyncio.wait_for(queue.get(), FRAME_TIMEOUT_SECONDS))
        except TimeoutError as exc:
            raise RecordingError("No frames received from camera") from exc
        started = time.monotonic()
        while (remaining := duration_seconds - (time.monotonic() - started)) > 0:
            try:
                frames.append(await asyncio.wait_for(queue.get(), min(remaining, FRAME_TIMEOUT_SECONDS)))
            except TimeoutError:
                if time.monotonic() - started < duration_seconds:
                    raise RecordingError("Camera stopped delivering frames") from None
                break
        elapsed = time.monotonic() - started
    if len(frames) < 2:
        raise RecordingError("Too few frames recorded")
    return frames, (len(frames) - 1) / elapsed


async def record_clip(
    hub: CameraHub, session: AsyncSession, data_dir: Path, duration_seconds: float, now: datetime
) -> Clip:
    frames, fps = await capture_frames(hub, duration_seconds)
    clip_id = uuid.uuid4()
    relative_dir = Path("clips") / now.strftime("%Y-%m-%d") / str(clip_id)
    clip_dir = data_dir / relative_dir
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "thumb.jpg").write_bytes(frames[0])
    await encode_jpegs_to_mp4(frames, fps, clip_dir / "original.mp4")

    clip = Clip(
        id=clip_id,
        recorded_at=now,
        duration_seconds=len(frames) / fps,
        status=ClipStatus.RECORDED,
        original_path=str(relative_dir / "original.mp4"),
        thumbnail_path=str(relative_dir / "thumb.jpg"),
        attempts=0,
    )
    prompt = await active_prompt(session)
    if prompt:
        queue_clip(clip, prompt)
    session.add(clip)
    await session.commit()
    log.info("Recorded clip %s (%d frames, %.1f fps)", clip_id, len(frames), fps)
    return clip
