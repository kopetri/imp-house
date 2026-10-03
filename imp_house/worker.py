import asyncio
import contextlib
import logging
import time
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from imp_house.augment import AugmentError, VideoAugmenter
from imp_house.media import transcode_to_playback
from imp_house.models import Clip, ClipStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class AugmentWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        augmenter: VideoAugmenter,
        data_dir: Path,
        idle_seconds: float = 10.0,
        poll_seconds: float = 10.0,
        job_timeout_seconds: float = 45 * 60,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._augmenter = augmenter
        self._data_dir = data_dir
        self._idle = idle_seconds
        self._poll = poll_seconds
        self._job_timeout = job_timeout_seconds
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="augment-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        async with self._sessionmaker() as session:
            await session.execute(
                update(Clip).where(Clip.status == ClipStatus.PROCESSING).values(status=ClipStatus.QUEUED)
            )
            await session.commit()
        while True:
            try:
                worked = await self.process_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Augment worker iteration failed")
                worked = False
            if not worked:
                await asyncio.sleep(self._idle)

    async def process_next(self) -> bool:
        async with self._sessionmaker() as session:
            clip = await session.scalar(
                select(Clip).where(Clip.status == ClipStatus.QUEUED).order_by(Clip.recorded_at).limit(1)
            )
            if clip is None:
                return False
            clip.status = ClipStatus.PROCESSING
            clip.attempts += 1
            await session.commit()
            clip_id, attempts = clip.id, clip.attempts
            try:
                await self._process(session, clip)
            except AugmentError as exc:
                await self._fail(session, clip_id, str(exc), retry=False, clear_job=True)
            except Exception as exc:
                log.warning("Clip %s attempt %d failed: %s", clip_id, attempts, exc)
                await self._fail(session, clip_id, str(exc), retry=attempts < MAX_ATTEMPTS, clear_job=False)
            return True

    async def _process(self, session: AsyncSession, clip: Clip) -> None:
        if not clip.provider_job_id:
            config = clip.model_input or {}
            inputs = dict(config.get("extra_input", {}))
            inputs[config.get("prompt_input_key", "prompt")] = clip.prompt_text
            clip.provider_job_id = await self._augmenter.submit(
                self._data_dir / clip.original_path,
                clip.model or "",
                inputs,
                config.get("video_input_key", "video"),
            )
            await session.commit()
            log.info("Clip %s submitted as job %s", clip.id, clip.provider_job_id)

        deadline = time.monotonic() + self._job_timeout
        while True:
            state = await self._augmenter.poll(clip.provider_job_id)
            if state.status == "succeeded":
                break
            if state.status == "failed":
                raise AugmentError(state.error or "Prediction failed")
            if time.monotonic() > deadline:
                raise AugmentError("Prediction timed out")
            await asyncio.sleep(self._poll)

        clip_dir = (self._data_dir / clip.original_path).parent
        augmented = clip_dir / "augmented.mp4"
        playback = clip_dir / "playback.mjpeg"
        await self._augmenter.download(state.output_url or "", augmented)
        fps, frames = await transcode_to_playback(augmented, playback)
        clip.augmented_path = str(augmented.relative_to(self._data_dir))
        clip.playback_path = str(playback.relative_to(self._data_dir))
        clip.playback_fps = fps
        clip.playback_frames = frames
        clip.status = ClipStatus.DONE
        clip.error = None
        await session.commit()
        log.info("Clip %s augmented (%d playback frames)", clip.id, frames)

    async def _fail(
        self, session: AsyncSession, clip_id: object, error: str, retry: bool, clear_job: bool
    ) -> None:
        await session.rollback()
        clip = await session.get(Clip, clip_id, populate_existing=True)
        if clip is None:
            return
        clip.error = error[:1000]
        clip.status = ClipStatus.QUEUED if retry else ClipStatus.FAILED
        if clear_job:
            clip.provider_job_id = None
        await session.commit()
