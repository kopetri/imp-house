import asyncio
import base64
import json
import random
import uuid
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from imp_house.augment import AugmentError, ReplicateAugmenter, _video_data_uri
from imp_house.camera import CameraHub
from imp_house.models import Clip, ClipStatus, RecordingSettings
from imp_house.scheduler import plan_times
from imp_house.worker import AugmentWorker
from tests.conftest import jpeg

TZ = ZoneInfo("Europe/Berlin")


def _settings(**overrides) -> RecordingSettings:
    values = dict(
        enabled=True,
        clips_per_day=5,
        window_start=time(8, 0),
        window_end=time(20, 0),
        min_seconds=10,
        max_seconds=20,
    )
    return RecordingSettings(**{**values, **overrides})


def test_plan_times_within_window() -> None:
    day = date(2026, 10, 3)
    now = datetime(2026, 10, 3, 0, 0, tzinfo=TZ)
    plan = plan_times(_settings(), day, TZ, now, 5, random.Random(1))
    assert len(plan) == 5
    assert [t for t, _ in plan] == sorted(t for t, _ in plan)
    for scheduled, duration in plan:
        assert datetime(2026, 10, 3, 8, 0, tzinfo=TZ) <= scheduled <= datetime(2026, 10, 3, 20, 0, tzinfo=TZ)
        assert 10 <= duration <= 20


def test_plan_times_starts_from_now_and_handles_passed_window() -> None:
    day = date(2026, 10, 3)
    now = datetime(2026, 10, 3, 15, 0, tzinfo=TZ)
    plan = plan_times(_settings(), day, TZ, now, 10, random.Random(2))
    assert all(t >= now for t, _ in plan)
    late = datetime(2026, 10, 3, 21, 0, tzinfo=TZ)
    assert plan_times(_settings(), day, TZ, late, 3, random.Random(3)) == []


async def test_camera_hub_fans_out_frames() -> None:
    frames = [jpeg(bytes([i])) for i in range(3)]

    async def body():
        for frame in frames:
            yield f"--b\r\nContent-Length: {len(frame)}\r\n\r\n".encode() + frame + b"\r\n"
            await asyncio.sleep(0.01)
        await asyncio.sleep(10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "multipart/x-mixed-replace;boundary=b"}, content=body()
        )

    hub = CameraHub("http://camera.test/stream", transport=httpx.MockTransport(handler))
    hub.start()
    try:
        async with hub.subscribe() as queue:
            received = [await asyncio.wait_for(queue.get(), 2) for _ in range(3)]
        assert received == frames
        assert hub.latest_frame == frames[-1]
    finally:
        await hub.stop()


def test_video_data_uri_rejects_oversized_video(tmp_path) -> None:
    video_path = tmp_path / "oversized.mp4"
    video_path.write_bytes(b"x" * 4_000_000)

    with pytest.raises(AugmentError, match="5 MiB"):
        _video_data_uri(video_path)


@respx.mock(base_url="https://api.replicate.test/v1", assert_all_called=True)
async def test_worker_augments_clip(respx_mock, app, settings, monkeypatch) -> None:
    clip_id = uuid.uuid4()
    rel = Path("clips/2026-10-03") / str(clip_id)
    (settings.data_dir / rel).mkdir(parents=True)
    (settings.data_dir / rel / "original.mp4").write_bytes(b"original")

    async with app.state.imp.db.sessionmaker() as session:
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime.now(UTC),
                duration_seconds=10,
                status=ClipStatus.QUEUED,
                original_path=str(rel / "original.mp4"),
                thumbnail_path=str(rel / "thumb.jpg"),
                prompt_text="make it snow",
                model="runwayml/gen4-aleph",
                model_input={
                    "video_input_key": "video",
                    "prompt_input_key": "prompt",
                    "extra_input": {"seed": 1},
                },
                attempts=0,
            )
        )
        await session.commit()

    create = respx_mock.post("/models/runwayml/gen4-aleph/predictions").respond(201, json={"id": "p1"})
    respx_mock.get("/predictions/p1").mock(
        side_effect=[
            httpx.Response(200, json={"status": "processing"}),
            httpx.Response(200, json={"status": "succeeded", "output": "https://delivery.test/out.mp4"}),
        ]
    )
    download = respx_mock.route(url="https://delivery.test/out.mp4").respond(content=b"augmented")

    async def fake_transcode(source: Path, output: Path) -> tuple[float, int]:
        output.write_bytes(source.read_bytes())
        return 12.0, 120

    monkeypatch.setattr("imp_house.worker.transcode_to_playback", fake_transcode)

    augmenter = ReplicateAugmenter("test-key", "https://api.replicate.test/v1")
    worker = AugmentWorker(app.state.imp.db.sessionmaker, augmenter, settings.data_dir, poll_seconds=0)
    try:
        assert await worker.process_next() is True
    finally:
        await augmenter.aclose()

    sent = create.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test-key"
    sent_input = json.loads(sent.content)["input"]
    assert sent_input["seed"] == 1
    assert sent_input["prompt"] == "make it snow"
    data_uri = sent_input["video"]
    prefix = "data:video/mp4;base64,"
    assert data_uri.startswith(prefix)
    assert base64.b64decode(data_uri.removeprefix(prefix)) == b"original"
    assert "Authorization" not in download.calls.last.request.headers

    async with app.state.imp.db.sessionmaker() as session:
        clip = await session.get(Clip, clip_id)
        assert clip.status == ClipStatus.DONE
        assert clip.playback_frames == 120
        assert (settings.data_dir / clip.augmented_path).read_bytes() == b"augmented"


@respx.mock(base_url="https://api.replicate.test/v1")
async def test_worker_marks_failed_prediction(respx_mock, app, settings) -> None:
    clip_id = uuid.uuid4()
    rel = Path("clips/x") / str(clip_id)
    (settings.data_dir / rel).mkdir(parents=True)
    (settings.data_dir / rel / "original.mp4").write_bytes(b"original")
    async with app.state.imp.db.sessionmaker() as session:
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime.now(UTC),
                duration_seconds=10,
                status=ClipStatus.QUEUED,
                original_path=str(rel / "original.mp4"),
                thumbnail_path=str(rel / "thumb.jpg"),
                prompt_text="p",
                model="owner/model",
                model_input={},
                attempts=0,
            )
        )
        await session.commit()
    respx_mock.post("/models/owner/model/predictions").respond(201, json={"id": "p2"})
    respx_mock.get("/predictions/p2").respond(json={"status": "failed", "error": "NSFW"})

    augmenter = ReplicateAugmenter("k", "https://api.replicate.test/v1")
    worker = AugmentWorker(app.state.imp.db.sessionmaker, augmenter, settings.data_dir, poll_seconds=0)
    try:
        await worker.process_next()
    finally:
        await augmenter.aclose()
    async with app.state.imp.db.sessionmaker() as session:
        clip = await session.get(Clip, clip_id)
        assert clip.status == ClipStatus.FAILED
        assert clip.error == "NSFW"
        assert clip.provider_job_id is None
