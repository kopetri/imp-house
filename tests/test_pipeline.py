import asyncio
import base64
import json
import random
import uuid
from datetime import UTC, date, datetime, time
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from PIL import Image

from imp_house.augment import AugmentError, ReplicateAugmenter
from imp_house.camera import CameraHub
from imp_house.imaging import create_feathered_mask
from imp_house.models import Clip, ClipStatus, RecordingSettings
from imp_house.recorder import record_clip
from imp_house.scheduler import plan_times
from imp_house.worker import AugmentWorker, parse_scene_analysis
from tests.conftest import jpeg

TZ = ZoneInfo("Europe/Berlin")


def _image_bytes(width: int = 80, height: int = 64) -> bytes:
    image = Image.new("RGB", (width, height), (120, 180, 100))
    buffer = BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def _settings(**overrides) -> RecordingSettings:
    values = dict(
        enabled=True,
        clips_per_day=5,
        window_start=time(8, 0),
        window_end=time(20, 0),
    )
    return RecordingSettings(**{**values, **overrides})


def test_plan_times_within_window() -> None:
    day = date(2026, 10, 3)
    now = datetime(2026, 10, 3, 0, 0, tzinfo=TZ)
    plan = plan_times(_settings(), day, TZ, now, 5, random.Random(1))
    assert len(plan) == 5
    assert all(isinstance(scheduled, datetime) for scheduled in plan)
    assert plan == sorted(plan)
    for scheduled in plan:
        assert datetime(2026, 10, 3, 8, 0, tzinfo=TZ) <= scheduled <= datetime(2026, 10, 3, 20, 0, tzinfo=TZ)


def test_plan_times_starts_from_now_and_handles_passed_window() -> None:
    day = date(2026, 10, 3)
    now = datetime(2026, 10, 3, 15, 0, tzinfo=TZ)
    plan = plan_times(_settings(), day, TZ, now, 10, random.Random(2))
    assert all(t >= now for t in plan)
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


def test_scene_analysis_parses_normalized_bbox_and_action() -> None:
    analysis = parse_scene_analysis(
        ['{"bbox":{"x":0.2,"y":0.3,"width":0.4,"height":0.5},',
         '"scene_prompt":"climbs onto the wooden stool"}']
    )

    assert analysis.bbox == {"x": 0.2, "y": 0.3, "width": 0.4, "height": 0.5}
    assert analysis.scene_prompt == "climbs onto the wooden stool"


def test_scene_analysis_clamps_bbox_to_image_bounds() -> None:
    output = ['{"bbox":{"x":0.8,"y":0.2,"width":0.4,"height":0.3},"scene_prompt":"walks"}']

    analysis = parse_scene_analysis(output)

    assert analysis.bbox["width"] == pytest.approx(0.2)

    bad_output = ['{"bbox":{"x":1.2,"y":0.2,"width":0.1,"height":0.3},"scene_prompt":"walks"}']
    with pytest.raises(AugmentError, match="normalized bbox"):
        parse_scene_analysis(bad_output)


def test_feathered_mask_matches_snapshot_and_softens_bbox_edge(tmp_path) -> None:
    source = tmp_path / "snapshot.jpg"
    destination = tmp_path / "mask.png"
    source.write_bytes(_image_bytes())

    create_feathered_mask(
        source, {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5}, destination
    )

    with Image.open(destination) as mask:
        assert mask.size == (80, 64)
        assert mask.mode == "L"
        assert mask.getpixel((40, 32)) == 255
        assert mask.getpixel((0, 0)) == 0
        assert 0 < mask.getpixel((20, 32)) < 255


@respx.mock(base_url="https://api.replicate.test/v1", assert_all_called=True)
async def test_replicate_submit_accepts_pinned_model_and_image_inputs(respx_mock) -> None:
    model = (
        "google/gemini-2.5-flash:"
        "37fd5e5ec0769f0bbe58ec8248418fc570778813444b508b226a25cb04679d07"
    )
    create = respx_mock.post("/predictions").respond(201, json={"id": "gemini-job"})
    client = ReplicateAugmenter("test-key", "https://api.replicate.test/v1")

    try:
        job_id = await client.submit(model, {"prompt": "Analyze", "images": ["data:image/jpeg;base64,eA=="]})
    finally:
        await client.aclose()

    payload = json.loads(create.calls.last.request.content)
    assert job_id == "gemini-job"
    assert payload["version"] == model.split(":", 1)[1]
    assert payload["input"]["images"] == ["data:image/jpeg;base64,eA=="]


async def test_record_clip_saves_one_snapshot_without_source_video(app, settings) -> None:
    hub = CameraHub("http://camera.test/stream")
    hub.latest_frame = jpeg(b"snapshot")
    recorded_at = datetime(2026, 10, 3, 12, 0, tzinfo=TZ)

    async with app.state.imp.db.sessionmaker() as session:
        clip = await record_clip(hub, session, settings.data_dir, recorded_at)

    clip_dir = settings.data_dir / "clips" / "2026-10-03" / str(clip.id)
    assert clip.snapshot_path == str(Path("clips/2026-10-03") / str(clip.id) / "snapshot.jpg")
    assert clip.original_path is None
    assert clip.duration_seconds == 5
    assert (clip_dir / "snapshot.jpg").read_bytes() == jpeg(b"snapshot")
    assert not (clip_dir / "original.mp4").exists()


@respx.mock(base_url="https://api.replicate.test/v1", assert_all_called=True)
async def test_worker_runs_snapshot_pipeline(respx_mock, app, settings, monkeypatch) -> None:
    clip_id = uuid.uuid4()
    relative_dir = Path("clips/2026-10-03") / str(clip_id)
    clip_dir = settings.data_dir / relative_dir
    clip_dir.mkdir(parents=True)
    snapshot = _image_bytes(320, 240)
    reference = _image_bytes(96, 64)
    (clip_dir / "snapshot.jpg").write_bytes(snapshot)

    async with app.state.imp.db.sessionmaker() as session:
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime.now(UTC),
                duration_seconds=5,
                status=ClipStatus.QUEUED,
                original_path=None,
                snapshot_path=str(relative_dir / "snapshot.jpg"),
                thumbnail_path=str(relative_dir / "snapshot.jpg"),
                prompt_text="A small moss-green imp with a brass bell",
                pipeline_stage="scene",
                attempts=0,
            )
        )
        await session.commit()

    create = respx_mock.post("/predictions").mock(
        side_effect=[
            httpx.Response(201, json={"id": "scene-job"}),
            httpx.Response(201, json={"id": "reference-job"}),
            httpx.Response(201, json={"id": "video-job"}),
        ]
    )
    respx_mock.get("/predictions/scene-job").respond(
        json={
            "status": "succeeded",
            "output": [
                '{"bbox":{"x":0.2,"y":0.2,"width":0.4,"height":0.5},',
                '"scene_prompt":"rings the bell beside the wooden stool"}',
            ],
        }
    )
    respx_mock.get("/predictions/reference-job").respond(
        json={"status": "succeeded", "output": "https://delivery.test/reference.png"}
    )
    respx_mock.get("/predictions/video-job").respond(
        json={"status": "succeeded", "output": "https://delivery.test/video.mp4"}
    )
    reference_download = respx_mock.get("https://delivery.test/reference.png").respond(content=reference)
    video_download = respx_mock.get("https://delivery.test/video.mp4").respond(content=b"generated-video")

    async def fake_transcode(source: Path, output: Path) -> tuple[float, int]:
        output.write_bytes(b"cyd-playback")
        return 12.0, 60

    monkeypatch.setattr("imp_house.worker.transcode_to_playback", fake_transcode)

    augmenter = ReplicateAugmenter("test-key", "https://api.replicate.test/v1")
    worker = AugmentWorker(app.state.imp.db.sessionmaker, augmenter, settings.data_dir, poll_seconds=0)
    try:
        assert await worker.process_next() is True
    finally:
        await augmenter.aclose()

    assert len(create.calls) == 3
    scene_payload, reference_payload, video_payload = [
        json.loads(call.request.content) for call in create.calls
    ]
    scene_image_uri = scene_payload["input"]["images"][0]
    assert scene_image_uri.startswith("data:image/jpeg;base64,")
    with Image.open(BytesIO(base64.b64decode(scene_image_uri.split(",", 1)[1]))) as scene_image:
        assert scene_image.size == (320, 240)
    assert "moss-green imp" in scene_payload["input"]["prompt"]
    assert "moss-green imp" in reference_payload["input"]["prompt"]
    reference_image_uri = reference_payload["input"]["image"]
    assert reference_image_uri.startswith("data:image/jpeg;base64,")
    with Image.open(BytesIO(base64.b64decode(reference_image_uri.split(",", 1)[1]))) as reference_image:
        assert reference_image.size == (341, 256)
    mask_uri = reference_payload["input"]["mask"]
    assert mask_uri.startswith("data:image/png;base64,")
    with Image.open(BytesIO(base64.b64decode(mask_uri.split(",", 1)[1]))) as mask:
        assert mask.size == (341, 256)
        assert mask.getpixel((137, 115)) > 240
    assert video_payload["input"]["image"].startswith("data:image/jpeg;base64,")
    assert "rings the bell beside the wooden stool" in video_payload["input"]["prompt"]
    assert video_payload["input"]["duration"] == 5
    assert video_payload["input"]["resolution"] == "720p"
    assert video_payload["input"]["aspect_ratio"] == "adaptive"
    assert video_payload["input"]["generate_audio"] is False
    assert "Authorization" not in reference_download.calls.last.request.headers
    assert "Authorization" not in video_download.calls.last.request.headers

    async with app.state.imp.db.sessionmaker() as session:
        clip = await session.get(Clip, clip_id)
        assert clip.status == ClipStatus.DONE
        assert clip.pipeline_stage == "done"
        assert clip.scene_prompt == "rings the bell beside the wooden stool"
        assert clip.mask_bbox == {"x": 0.2, "y": 0.2, "width": 0.4, "height": 0.5}
        assert clip.playback_frames == 60
        with Image.open(settings.data_dir / clip.snapshot_path) as saved_snapshot:
            assert saved_snapshot.size == (320, 240)
        with Image.open(settings.data_dir / clip.mask_path) as saved_mask:
            assert saved_mask.size == (320, 240)
        with Image.open(settings.data_dir / clip.reference_path) as saved_reference:
            assert saved_reference.format == "JPEG"
            assert saved_reference.size == (96, 64)
        assert (settings.data_dir / clip.augmented_path).read_bytes() == b"generated-video"


@respx.mock(base_url="https://api.replicate.test/v1", assert_all_called=True)
async def test_worker_rejects_archived_video_clip(respx_mock, app, settings) -> None:
    clip_id = uuid.uuid4()
    relative_dir = Path("clips/legacy") / str(clip_id)
    clip_dir = settings.data_dir / relative_dir
    clip_dir.mkdir(parents=True)
    (clip_dir / "original.mp4").write_bytes(b"legacy-video")
    async with app.state.imp.db.sessionmaker() as session:
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime.now(UTC),
                duration_seconds=10,
                status=ClipStatus.QUEUED,
                original_path=str(relative_dir / "original.mp4"),
                snapshot_path=None,
                thumbnail_path=str(relative_dir / "thumb.jpg"),
                prompt_text="ignored",
                attempts=0,
            )
        )
        await session.commit()

    augmenter = ReplicateAugmenter("test-key", "https://api.replicate.test/v1")
    worker = AugmentWorker(app.state.imp.db.sessionmaker, augmenter, settings.data_dir, poll_seconds=0)
    try:
        assert await worker.process_next() is True
    finally:
        await augmenter.aclose()
    async with app.state.imp.db.sessionmaker() as session:
        clip = await session.get(Clip, clip_id)
        assert clip.status == ClipStatus.FAILED
        assert "archived video" in (clip.error or "").lower()
        assert clip.provider_job_id is None


@respx.mock(base_url="https://api.replicate.test/v1", assert_all_called=True)
async def test_worker_resumes_saved_provider_job(respx_mock, app, settings, monkeypatch) -> None:
    clip_id = uuid.uuid4()
    relative_dir = Path("clips/resume") / str(clip_id)
    clip_dir = settings.data_dir / relative_dir
    clip_dir.mkdir(parents=True)
    (clip_dir / "snapshot.jpg").write_bytes(_image_bytes())
    (clip_dir / "mask.png").write_bytes(b"mask")
    reference = _image_bytes(96, 64)

    async with app.state.imp.db.sessionmaker() as session:
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime.now(UTC),
                duration_seconds=5,
                status=ClipStatus.QUEUED,
                original_path=None,
                snapshot_path=str(relative_dir / "snapshot.jpg"),
                mask_path=str(relative_dir / "mask.png"),
                thumbnail_path=str(relative_dir / "snapshot.jpg"),
                prompt_text="A moss-green imp",
                scene_prompt="waves beside the plant",
                pipeline_stage="reference",
                provider_job_id="saved-flux-job",
                attempts=1,
            )
        )
        await session.commit()

    respx_mock.get("/predictions/saved-flux-job").respond(
        json={"status": "succeeded", "output": "https://delivery.test/reference.jpg"}
    )
    create_video = respx_mock.post("/predictions").respond(201, json={"id": "new-video-job"})
    respx_mock.get("/predictions/new-video-job").respond(
        json={"status": "succeeded", "output": "https://delivery.test/video.mp4"}
    )
    respx_mock.get("https://delivery.test/reference.jpg").respond(content=reference)
    respx_mock.get("https://delivery.test/video.mp4").respond(content=b"generated-video")

    async def fake_transcode(source: Path, output: Path) -> tuple[float, int]:
        output.write_bytes(b"playback")
        return 12.0, 60

    monkeypatch.setattr("imp_house.worker.transcode_to_playback", fake_transcode)

    augmenter = ReplicateAugmenter("test-key", "https://api.replicate.test/v1")
    worker = AugmentWorker(app.state.imp.db.sessionmaker, augmenter, settings.data_dir, poll_seconds=0)
    try:
        assert await worker.process_next() is True
    finally:
        await augmenter.aclose()

    assert len(create_video.calls) == 1
    video_payload = json.loads(create_video.calls.last.request.content)
    assert video_payload["version"] == "a6dcbae88b153e75fcccabacfb0eb430ab5be0a7ae27b316fc6f983658b349bc"
    async with app.state.imp.db.sessionmaker() as session:
        clip = await session.get(Clip, clip_id)
        assert clip.status == ClipStatus.DONE
        assert clip.pipeline_stage == "done"
