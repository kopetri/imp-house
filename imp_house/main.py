import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse

from imp_house.api import auth, clips, config_api, users
from imp_house.api.deps import AppState
from imp_house.augment import ReplicateAugmenter
from imp_house.camera import CameraHub
from imp_house.config import Settings, get_settings
from imp_house.db import Database
from imp_house.scheduler import RecordingScheduler
from imp_house.worker import AugmentWorker

log = logging.getLogger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
        "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
    ),
}


def create_app(settings: Settings | None = None, start_background: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = Database(settings.database_url)
        hub = CameraHub(settings.camera_url)
        scheduler = RecordingScheduler(db.sessionmaker, hub, settings.data_dir, settings.timezone)
        app.state.imp = AppState(settings=settings, db=db, hub=hub, scheduler=scheduler)
        augmenter = worker = None
        if start_background:
            hub.start()
            scheduler.start()
            if settings.replicate_api_key:
                augmenter = ReplicateAugmenter(settings.replicate_api_key.get(), settings.replicate_base_url)
                worker = AugmentWorker(db.sessionmaker, augmenter, settings.data_dir)
                worker.start()
            else:
                log.warning("REPLICATE_API_KEY not set; clips will not be augmented")
        try:
            yield
        finally:
            if worker:
                await worker.stop()
            if augmenter:
                await augmenter.aclose()
            await scheduler.stop()
            await hub.stop()
            await db.dispose()

    app = FastAPI(title="imp-house", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    for router in (
        auth.router,
        users.users_router,
        users.devices_router,
        config_api.prompts_router,
        config_api.settings_router,
        clips.clips_router,
        clips.live_router,
        clips.device_router,
    ):
        app.include_router(router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if settings.static_dir and settings.static_dir.is_dir():
        _mount_spa(app, settings.static_dir)
    return app


def _mount_spa(app: FastAPI, static_dir: Path) -> None:
    index = static_dir / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        candidate = (static_dir / path).resolve()
        if path and candidate.is_relative_to(static_dir) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


def run() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, proxy_headers=True)  # noqa: S104
