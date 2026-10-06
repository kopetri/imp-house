import enum
import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from imp_house.db import Base


class ClipStatus(enum.StrEnum):
    RECORDED = "recorded"
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ScheduleStatus(enum.StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, native_enum=False, length=20, values_callable=lambda e: [m.value for m in e])


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    owner: Mapped[User] = relationship()


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (
        Index(
            "uq_prompts_single_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    duration_seconds: Mapped[float] = mapped_column(Float)
    status: Mapped[ClipStatus] = mapped_column(_enum(ClipStatus, "clip_status"), index=True)
    original_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(500))
    mask_path: Mapped[str | None] = mapped_column(String(500))
    reference_path: Mapped[str | None] = mapped_column(String(500))
    thumbnail_path: Mapped[str] = mapped_column(String(500))
    prompt_id: Mapped[int | None] = mapped_column(ForeignKey("prompts.id", ondelete="SET NULL"))
    prompt_name: Mapped[str | None] = mapped_column(String(100))
    prompt_text: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(200))
    provider_job_id: Mapped[str | None] = mapped_column(String(100))
    provider_output: Mapped[Any] = mapped_column(JSON, nullable=True)
    scene_prompt: Mapped[str | None] = mapped_column(Text)
    mask_bbox: Mapped[dict | None] = mapped_column(JSON)
    pipeline_stage: Mapped[str | None] = mapped_column(String(30))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    augmented_path: Mapped[str | None] = mapped_column(String(500))
    playback_path: Mapped[str | None] = mapped_column(String(500))
    playback_fps: Mapped[float | None] = mapped_column(Float)
    playback_frames: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RecordingSettings(Base):
    __tablename__ = "recording_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    clips_per_day: Mapped[int] = mapped_column(Integer, default=3)
    window_start: Mapped[time] = mapped_column(Time, default=time(8, 0))
    window_end: Mapped[time] = mapped_column(Time, default=time(20, 0))


class ScheduledRecording(Base):
    __tablename__ = "scheduled_recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_date: Mapped[date] = mapped_column(Date, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[ScheduleStatus] = mapped_column(_enum(ScheduleStatus, "schedule_status"))
    clip_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("clips.id", ondelete="SET NULL"))
    error: Mapped[str | None] = mapped_column(Text)
