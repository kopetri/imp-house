import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from imp_house.mjpeg import MultipartMjpegParser, write_playback_index
from imp_house.models import Clip, ClipStatus, Prompt
from tests.conftest import jpeg


async def _create_done_clip(app, data_dir: Path) -> uuid.UUID:
    clip_id = uuid.uuid4()
    rel = Path("clips") / "2026-10-03" / str(clip_id)
    clip_dir = data_dir / rel
    clip_dir.mkdir(parents=True)
    (clip_dir / "playback.mjpeg").write_bytes(jpeg(b"a") + jpeg(b"b") + jpeg(b"c"))
    write_playback_index(clip_dir / "playback.mjpeg", 50.0)
    (clip_dir / "thumb.jpg").write_bytes(jpeg())
    (clip_dir / "original.mp4").write_bytes(b"mp4")
    async with app.state.imp.db.sessionmaker() as session:
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
                duration_seconds=12.0,
                status=ClipStatus.DONE,
                original_path=str(rel / "original.mp4"),
                thumbnail_path=str(rel / "thumb.jpg"),
                playback_path=str(rel / "playback.mjpeg"),
                playback_fps=12.0,
                playback_frames=144,
                prompt_name="Snow",
                attempts=1,
            )
        )
        await session.commit()
    return clip_id


async def test_device_token_flow(app, admin_client: httpx.AsyncClient, settings) -> None:
    clip_id = await _create_done_clip(app, settings.data_dir)
    created = (await admin_client.post("/api/devices", json={"name": "CYD"})).json()
    token = created["token"]
    assert token.startswith("imp_")
    assert "token" not in (await admin_client.get("/api/devices")).json()[0]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as device:
        assert (await device.get("/api/device/clips")).status_code == 401
        assert (
            await device.get("/api/device/clips", headers={"Authorization": "Bearer imp_wrong"})
        ).status_code == 401
        auth = {"Authorization": f"Bearer {token}"}
        listing = (await device.get("/api/device/clips", headers=auth)).json()
        assert listing["total"] == 1
        assert listing["clips"][0] == {"id": str(clip_id), "t": "03.10. 14:00", "p": "Snow", "d": 12}

        playback = await device.get(f"/api/device/clips/{clip_id}/playback.mjpeg", headers=auth)
        assert playback.status_code == 200
        assert playback.headers["content-type"].startswith("multipart/x-mixed-replace")
        frames = MultipartMjpegParser("impframe").feed(playback.content)
        assert frames == [jpeg(b"a"), jpeg(b"b"), jpeg(b"c")]

        # cookie session must not grant device access and device token must not grant user API access
        assert (await admin_client.get("/api/device/clips")).status_code == 401
        assert (await device.get("/api/clips", headers=auth)).status_code == 401

        await admin_client.delete(f"/api/devices/{created['id']}")
        assert (await device.get("/api/device/clips", headers=auth)).status_code == 401


async def test_clip_files_and_listing(app, admin_client: httpx.AsyncClient, settings) -> None:
    clip_id = await _create_done_clip(app, settings.data_dir)
    page = (await admin_client.get("/api/clips")).json()
    assert page["total"] == 1
    assert page["items"][0]["has_augmented"] is False
    assert page["items"][0]["has_snapshot"] is False
    assert page["items"][0]["has_original"] is True
    thumb = await admin_client.get(f"/api/clips/{clip_id}/thumb.jpg")
    assert thumb.status_code == 200
    assert thumb.content == jpeg()
    assert (await admin_client.get(f"/api/clips/{clip_id}/augmented.mp4")).status_code == 404
    assert (await admin_client.get(f"/api/clips/{clip_id}/..%2F..%2Fetc%2Fpasswd")).status_code == 404
    ranged = await admin_client.get(f"/api/clips/{clip_id}/original.mp4", headers={"Range": "bytes=0-1"})
    assert ranged.status_code == 206
    assert ranged.content == b"mp"
    assert (await admin_client.post(f"/api/clips/{clip_id}/process")).status_code == 409

    assert (await admin_client.delete(f"/api/clips/{clip_id}")).status_code == 204
    assert not (settings.data_dir / "clips" / "2026-10-03" / str(clip_id)).exists()


async def test_snapshot_artifact_routes(app, admin_client: httpx.AsyncClient, settings) -> None:
    clip_id = uuid.uuid4()
    relative_dir = Path("clips") / "2026-10-04" / str(clip_id)
    clip_dir = settings.data_dir / relative_dir
    clip_dir.mkdir(parents=True)
    (clip_dir / "snapshot.jpg").write_bytes(jpeg())
    (clip_dir / "mask.png").write_bytes(b"mask")
    (clip_dir / "reference.jpg").write_bytes(jpeg(b"reference"))
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
                reference_path=str(relative_dir / "reference.jpg"),
                thumbnail_path=str(relative_dir / "snapshot.jpg"),
                prompt_text="A small green imp",
                scene_prompt="waves beside the plant",
                pipeline_stage="video",
            )
        )
        await session.commit()

    clip = (await admin_client.get(f"/api/clips/{clip_id}")).json()
    assert clip["has_snapshot"] is True
    assert clip["has_mask"] is True
    assert clip["has_reference"] is True
    assert clip["scene_prompt"] == "waves beside the plant"
    assert clip["processing_stage"] == "video"
    for name, content_type in (
        ("snapshot.jpg", "image/jpeg"),
        ("mask.png", "image/png"),
        ("reference.jpg", "image/jpeg"),
    ):
        response = await admin_client.get(f"/api/clips/{clip_id}/{name}")
        assert response.status_code == 200
        assert response.headers["content-type"] == content_type


async def test_manual_capture_is_bodyless(app, admin_client: httpx.AsyncClient) -> None:
    calls = 0

    async def capture_snapshot() -> None:
        nonlocal calls
        calls += 1

    app.state.imp.scheduler.record_now = capture_snapshot
    response = await admin_client.post("/api/clips/record")

    assert response.status_code == 202
    assert response.json() == {"status": "capturing"}
    assert calls == 1


async def test_process_snapshot_resets_pipeline(app, admin_client: httpx.AsyncClient, settings) -> None:
    clip_id = uuid.uuid4()
    relative_dir = Path("clips") / "retry" / str(clip_id)
    clip_dir = settings.data_dir / relative_dir
    clip_dir.mkdir(parents=True)
    (clip_dir / "snapshot.jpg").write_bytes(jpeg())
    async with app.state.imp.db.sessionmaker() as session:
        prompt = Prompt(name="Imp", text="A green imp", is_active=True)
        session.add(prompt)
        await session.flush()
        session.add(
            Clip(
                id=clip_id,
                recorded_at=datetime.now(UTC),
                duration_seconds=5,
                status=ClipStatus.FAILED,
                original_path=None,
                snapshot_path=str(relative_dir / "snapshot.jpg"),
                mask_path="clips/retry/mask.png",
                reference_path="clips/retry/reference.jpg",
                thumbnail_path=str(relative_dir / "snapshot.jpg"),
                prompt_text="Old character",
                scene_prompt="old action",
                mask_bbox={"x": 0.2, "y": 0.2, "width": 0.2, "height": 0.2},
                pipeline_stage="video",
                provider_job_id="old-job",
                provider_output="old-output",
                augmented_path="clips/retry/augmented.mp4",
                playback_path="clips/retry/playback.mjpeg",
                attempts=2,
                error="Seedance failed",
            )
        )
        await session.commit()

    response = await admin_client.post(f"/api/clips/{clip_id}/process")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["processing_stage"] == "scene"
    assert response.json()["has_mask"] is False
    assert response.json()["has_reference"] is False
    async with app.state.imp.db.sessionmaker() as session:
        clip = await session.get(Clip, clip_id)
        assert clip.provider_job_id is None
        assert clip.provider_output is None
        assert clip.scene_prompt is None


async def test_prompt_activation_is_exclusive(app, admin_client: httpx.AsyncClient) -> None:
    ids = []
    for name in ("A", "B"):
        response = await admin_client.post("/api/prompts", json={"name": name, "text": f"make it {name}"})
        assert response.status_code == 201
        ids.append(response.json()["id"])
    await admin_client.post(f"/api/prompts/{ids[0]}/activate")
    await admin_client.post(f"/api/prompts/{ids[1]}/activate")
    prompts = (await admin_client.get("/api/prompts")).json()
    assert [p["is_active"] for p in prompts] == [False, True]
    assert prompts[0]["text"] == "make it A"
    assert "model" not in prompts[0]
    assert "extra_input" not in prompts[0]


async def test_prompt_rejects_legacy_model_config(admin_client: httpx.AsyncClient) -> None:
    bad = await admin_client.post(
        "/api/prompts", json={"name": "x", "text": "y", "model": "../evil"}
    )
    assert bad.status_code == 422


async def test_settings_validation_and_admin_only(app, admin_client: httpx.AsyncClient) -> None:
    current = (await admin_client.get("/api/settings")).json()
    assert current["clips_per_day"] == 3
    assert "min_seconds" not in current
    assert "max_seconds" not in current
    bad = {**current, "window_start": "20:00:00", "window_end": "08:00:00"}
    assert (await admin_client.put("/api/settings", json=bad)).status_code == 422
    legacy = {**current, "min_seconds": 10, "max_seconds": 20}
    assert (await admin_client.put("/api/settings", json=legacy)).status_code == 422
    updated = {**current, "clips_per_day": 5}
    assert (await admin_client.put("/api/settings", json=updated)).json()["clips_per_day"] == 5
