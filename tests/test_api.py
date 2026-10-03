import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

from imp_house.mjpeg import MultipartMjpegParser, write_playback_index
from imp_house.models import Clip, ClipStatus
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
    thumb = await admin_client.get(f"/api/clips/{clip_id}/thumb.jpg")
    assert thumb.status_code == 200
    assert thumb.content == jpeg()
    assert (await admin_client.get(f"/api/clips/{clip_id}/augmented.mp4")).status_code == 404
    assert (await admin_client.get(f"/api/clips/{clip_id}/..%2F..%2Fetc%2Fpasswd")).status_code == 404
    ranged = await admin_client.get(f"/api/clips/{clip_id}/original.mp4", headers={"Range": "bytes=0-1"})
    assert ranged.status_code == 206
    assert ranged.content == b"mp"

    assert (await admin_client.delete(f"/api/clips/{clip_id}")).status_code == 204
    assert not (settings.data_dir / "clips" / "2026-10-03" / str(clip_id)).exists()


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
    assert prompts[0]["model"] == "runwayml/gen4-aleph"


async def test_prompt_model_validation(admin_client: httpx.AsyncClient) -> None:
    bad = await admin_client.post("/api/prompts", json={"name": "x", "text": "y", "model": "../evil"})
    assert bad.status_code == 422


async def test_settings_validation_and_admin_only(app, admin_client: httpx.AsyncClient) -> None:
    current = (await admin_client.get("/api/settings")).json()
    assert current["clips_per_day"] == 3
    bad = {**current, "window_start": "20:00:00", "window_end": "08:00:00"}
    assert (await admin_client.put("/api/settings", json=bad)).status_code == 422
    updated = {**current, "clips_per_day": 5}
    assert (await admin_client.put("/api/settings", json=updated)).json()["clips_per_day"] == 5
