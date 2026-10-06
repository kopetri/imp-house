import asyncio
import contextlib
import logging
import random
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from imp_house.camera import CameraHub
from imp_house.db import as_utc
from imp_house.models import Clip, RecordingSettings, ScheduledRecording, ScheduleStatus
from imp_house.recorder import record_clip

log = logging.getLogger(__name__)

MISSED_AFTER = timedelta(minutes=10)


async def get_recording_settings(session: AsyncSession) -> RecordingSettings:
    settings = await session.get(RecordingSettings, 1)
    if settings is None:
        settings = RecordingSettings(
            id=1,
            enabled=True,
            clips_per_day=3,
            window_start=time(8, 0),
            window_end=time(20, 0),
        )
        session.add(settings)
        await session.flush()
    return settings


def plan_times(
    settings: RecordingSettings,
    plan_date: date,
    tz: ZoneInfo,
    now: datetime,
    count: int,
    rng: random.Random,
) -> list[datetime]:
    window_start = datetime.combine(plan_date, settings.window_start, tzinfo=tz)
    window_end = datetime.combine(plan_date, settings.window_end, tzinfo=tz)
    start = max(window_start, now)
    if count <= 0 or window_end <= start:
        return []
    span = (window_end - start).total_seconds()
    offsets = sorted(rng.uniform(0, span) for _ in range(count))
    return [start + timedelta(seconds=offset) for offset in offsets]


class RecordingScheduler:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        hub: CameraHub,
        data_dir: Path,
        tz: ZoneInfo,
        poll_seconds: float = 5.0,
        rng: random.Random | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._hub = hub
        self._data_dir = data_dir
        self._tz = tz
        self._poll = poll_seconds
        self._rng = rng or random.Random()  # noqa: S311 - scheduling, not crypto
        self._record_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="recording-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick(datetime.now(UTC))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler tick failed")
            await asyncio.sleep(self._poll)

    async def tick(self, now: datetime) -> None:
        async with self._sessionmaker() as session:
            settings = await get_recording_settings(session)
            today = now.astimezone(self._tz).date()
            if settings.enabled:
                planned = await session.scalar(
                    select(func.count())
                    .select_from(ScheduledRecording)
                    .where(ScheduledRecording.plan_date == today)
                )
                if not planned:
                    await self._add_plan(session, settings, today, now, settings.clips_per_day)
            await session.commit()
            due = (
                await session.scalars(
                    select(ScheduledRecording)
                    .where(
                        ScheduledRecording.status == ScheduleStatus.PENDING,
                        ScheduledRecording.scheduled_at <= now,
                    )
                    .order_by(ScheduledRecording.scheduled_at)
                )
            ).all()
            enabled = settings.enabled

        for entry in due:
            if not enabled or now - as_utc(entry.scheduled_at) > MISSED_AFTER:
                await self._finish(entry.id, ScheduleStatus.FAILED, None, "missed" if enabled else "disabled")
                continue
            try:
                clip = await self.record_now()
            except Exception as exc:
                log.warning("Scheduled recording failed: %s", exc)
                await self._finish(entry.id, ScheduleStatus.FAILED, None, str(exc)[:500])
            else:
                await self._finish(entry.id, ScheduleStatus.DONE, clip, None)

    async def replan_today(self, now: datetime) -> None:
        async with self._sessionmaker() as session:
            settings = await get_recording_settings(session)
            today = now.astimezone(self._tz).date()
            await session.execute(
                delete(ScheduledRecording).where(
                    ScheduledRecording.plan_date == today,
                    ScheduledRecording.status == ScheduleStatus.PENDING,
                )
            )
            used = await session.scalar(
                select(func.count())
                .select_from(ScheduledRecording)
                .where(ScheduledRecording.plan_date == today)
            )
            if settings.enabled:
                await self._add_plan(session, settings, today, now, settings.clips_per_day - (used or 0))
            await session.commit()

    async def record_now(self) -> Clip:
        async with self._record_lock, self._sessionmaker() as session:
            return await record_clip(self._hub, session, self._data_dir, datetime.now(self._tz))

    async def _add_plan(
        self, session: AsyncSession, settings: RecordingSettings, day: date, now: datetime, count: int
    ) -> None:
        times = plan_times(settings, day, self._tz, now, count, self._rng)
        for scheduled_at in times:
            session.add(
                ScheduledRecording(
                    plan_date=day,
                    scheduled_at=scheduled_at,
                    status=ScheduleStatus.PENDING,
                )
            )
        if times:
            log.info("Planned %d recordings for %s", len(times), day)

    async def _finish(
        self, entry_id: int, status: ScheduleStatus, clip: Clip | None, error: str | None
    ) -> None:
        async with self._sessionmaker() as session:
            entry = await session.get(ScheduledRecording, entry_id)
            if entry:
                entry.status = status
                entry.clip_id = clip.id if clip else None
                entry.error = error
                await session.commit()
