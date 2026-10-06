import asyncio
import contextlib
import json
import logging
import math
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from imp_house.augment import (
    FLUX_FILL_MODEL,
    GEMINI_MODEL,
    SEEDANCE_MODEL,
    AugmentError,
    PredictionClient,
    image_data_uri,
)
from imp_house.imaging import MaskError, create_feathered_mask, prepare_flux_images
from imp_house.media import transcode_to_playback
from imp_house.models import Clip, ClipStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
GENERATED_VIDEO_SECONDS = 5
STAGE_LABELS = {
    "scene": "VLM",
    "mask": "Mask",
    "reference": "FLUX Fill",
    "video": "Seedance",
    "transcode": "Playback transcode",
}


@dataclass(frozen=True)
class SceneAnalysis:
    bbox: dict[str, float]
    scene_prompt: str


def parse_scene_analysis(output: Any) -> SceneAnalysis:
    if isinstance(output, str):
        chunks = [output]
    elif isinstance(output, list) and all(isinstance(chunk, str) for chunk in output):
        chunks = output
    else:
        raise AugmentError("VLM stage returned an invalid response")

    text = "".join(chunks)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise AugmentError("VLM stage did not return a JSON scene analysis")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AugmentError("VLM stage returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("bbox"), dict):
        raise AugmentError("VLM stage must return a normalized bbox")

    raw_bbox = payload["bbox"]
    bbox: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        value = raw_bbox.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AugmentError("VLM stage must return a normalized bbox")
        coordinate = float(value)
        if not math.isfinite(coordinate) or not 0 <= coordinate <= 1:
            raise AugmentError("VLM stage must return a normalized bbox")
        bbox[key] = coordinate
    if bbox["x"] >= 1 or bbox["y"] >= 1 or bbox["width"] <= 0 or bbox["height"] <= 0:
        raise AugmentError("VLM stage must return a positive normalized bbox")
    bbox["width"] = min(bbox["width"], 1 - bbox["x"])
    bbox["height"] = min(bbox["height"], 1 - bbox["y"])

    scene_prompt = payload.get("scene_prompt")
    if not isinstance(scene_prompt, str) or not scene_prompt.strip():
        raise AugmentError("VLM stage did not return a scene/action prompt")
    return SceneAnalysis(bbox=bbox, scene_prompt=scene_prompt.strip()[:1000])


class AugmentWorker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        augmenter: PredictionClient,
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
                await self._fail(
                    session,
                    clip_id,
                    _stage_error(clip.pipeline_stage, str(exc)),
                    retry=False,
                    clear_job=True,
                )
            except Exception as exc:
                log.warning("Clip %s attempt %d failed: %s", clip_id, attempts, exc)
                await self._fail(
                    session,
                    clip_id,
                    _stage_error(clip.pipeline_stage, str(exc)),
                    retry=attempts < MAX_ATTEMPTS,
                    clear_job=False,
                )
            return True

    async def _process(self, session: AsyncSession, clip: Clip) -> None:
        if not clip.snapshot_path:
            raise AugmentError("Archived video clips cannot be processed by the snapshot pipeline")
        if not clip.prompt_text:
            raise AugmentError("Character description is empty")
        snapshot = self._data_dir / clip.snapshot_path
        if not snapshot.is_file():
            raise AugmentError("Source snapshot is missing")
        clip_dir = snapshot.parent
        if not clip.pipeline_stage:
            clip.pipeline_stage = "scene"
            await session.commit()

        while True:
            if clip.pipeline_stage == "scene":
                output = await self._prediction_output(
                    session,
                    clip,
                    GEMINI_MODEL,
                    {
                        "prompt": _scene_analysis_prompt(clip.prompt_text),
                        "images": [image_data_uri(snapshot)],
                    },
                    "scene",
                )
                analysis = parse_scene_analysis(output)
                clip.scene_prompt = analysis.scene_prompt
                clip.mask_bbox = analysis.bbox
                clip.pipeline_stage = "mask"
                _clear_prediction(clip)
                await session.commit()
                continue

            if clip.pipeline_stage == "mask":
                try:
                    create_feathered_mask(snapshot, clip.mask_bbox or {}, clip_dir / "mask.png")
                except MaskError as exc:
                    raise AugmentError(str(exc)) from exc
                clip.mask_path = str((clip_dir / "mask.png").relative_to(self._data_dir))
                clip.pipeline_stage = "reference"
                await session.commit()
                continue

            if clip.pipeline_stage == "reference":
                if not clip.mask_path or not clip.scene_prompt:
                    raise AugmentError("VLM stage artifacts are missing")
                inputs: dict[str, Any] = {}
                if clip.provider_output is None and clip.provider_job_id is None:
                    with tempfile.TemporaryDirectory(prefix="imp-house-flux-") as temp_dir:
                        try:
                            flux_snapshot, flux_mask = prepare_flux_images(
                                snapshot, self._data_dir / clip.mask_path, Path(temp_dir)
                            )
                        except MaskError as exc:
                            raise AugmentError(str(exc)) from exc
                        inputs = {
                            "prompt": _reference_prompt(clip.prompt_text, clip.scene_prompt),
                            "image": image_data_uri(flux_snapshot),
                            "mask": image_data_uri(flux_mask),
                        }
                output = await self._prediction_output(
                    session,
                    clip,
                    FLUX_FILL_MODEL,
                    inputs,
                    "reference",
                )
                url = _output_url(output)
                if not url:
                    raise AugmentError("FLUX Fill stage returned no image URL")
                reference = clip_dir / "reference.jpg"
                await self._augmenter.download(url, reference)
                _normalize_reference_image(reference)
                clip.reference_path = str(reference.relative_to(self._data_dir))
                clip.pipeline_stage = "video"
                _clear_prediction(clip)
                await session.commit()
                continue

            if clip.pipeline_stage == "video":
                if not clip.reference_path or not clip.scene_prompt:
                    raise AugmentError("Reference image or scene/action prompt is missing")
                output = await self._prediction_output(
                    session,
                    clip,
                    SEEDANCE_MODEL,
                    {
                        "prompt": _video_prompt(clip.prompt_text, clip.scene_prompt),
                        "image": image_data_uri(self._data_dir / clip.reference_path),
                        "duration": GENERATED_VIDEO_SECONDS,
                        "resolution": "720p",
                        "aspect_ratio": "adaptive",
                        "generate_audio": False,
                    },
                    "video",
                )
                url = _output_url(output)
                if not url:
                    raise AugmentError("Seedance stage returned no video URL")
                augmented = clip_dir / "augmented.mp4"
                await self._augmenter.download(url, augmented)
                clip.augmented_path = str(augmented.relative_to(self._data_dir))
                clip.pipeline_stage = "transcode"
                _clear_prediction(clip)
                await session.commit()
                continue

            if clip.pipeline_stage == "transcode":
                if not clip.augmented_path:
                    raise AugmentError("Seedance video file is missing")
                augmented = self._data_dir / clip.augmented_path
                playback = clip_dir / "playback.mjpeg"
                fps, frames = await transcode_to_playback(augmented, playback)
                clip.playback_path = str(playback.relative_to(self._data_dir))
                clip.playback_fps = fps
                clip.playback_frames = frames
                clip.status = ClipStatus.DONE
                clip.pipeline_stage = "done"
                clip.error = None
                await session.commit()
                log.info("Clip %s generated (%d playback frames)", clip.id, frames)
                return

            if clip.pipeline_stage == "done":
                clip.status = ClipStatus.DONE
                await session.commit()
                return
            raise AugmentError(f"Unknown pipeline stage: {clip.pipeline_stage}")

    async def _prediction_output(
        self,
        session: AsyncSession,
        clip: Clip,
        model: str,
        inputs: dict[str, Any],
        stage: str,
    ) -> Any:
        if clip.provider_output is not None:
            return clip.provider_output
        if not clip.provider_job_id:
            try:
                clip.provider_job_id = await self._augmenter.submit(model, inputs)
            except AugmentError as exc:
                raise AugmentError(_stage_error(stage, str(exc))) from exc
            await session.commit()
            log.info("Clip %s %s prediction submitted as job %s", clip.id, stage, clip.provider_job_id)

        deadline = time.monotonic() + self._job_timeout
        while True:
            state = await self._augmenter.poll(clip.provider_job_id)
            if state.status == "succeeded":
                if state.output is None:
                    raise AugmentError(_stage_error(stage, "Prediction succeeded without an output"))
                clip.provider_output = state.output
                await session.commit()
                return clip.provider_output
            if state.status == "failed":
                raise AugmentError(_stage_error(stage, state.error or "Prediction failed"))
            if time.monotonic() > deadline:
                raise AugmentError(_stage_error(stage, "Prediction timed out"))
            await asyncio.sleep(self._poll)

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


def _scene_analysis_prompt(character_description: str) -> str:
    return (
        "Analyze the provided room snapshot. The character's visual description is: "
        f"{character_description}\n"
        "Choose a plausible visible placement for the character that fits the real scene. "
        "Return only one JSON object with bbox {x,y,width,height} as normalized numbers from 0 to 1, "
        "and scene_prompt as one concise action involving an object or feature "
        "actually visible in the image. "
        "The bbox must contain the whole character and stay inside the image."
    )


def _reference_prompt(character_description: str, scene_prompt: str) -> str:
    return (
        "Add one small photorealistic character inside the white mask. Preserve the image outside the mask. "
        f"Character appearance: {character_description}. Scene and action: {scene_prompt}. "
        "Match the room's lighting, perspective, scale, and camera grain."
    )


def _video_prompt(character_description: str, scene_prompt: str) -> str:
    return (
        f"Animate the character naturally: {scene_prompt}. Character appearance: {character_description}. "
        "Keep the camera and background stable; preserve the reference image composition."
    )


def _stage_error(stage: str | None, message: str) -> str:
    label = STAGE_LABELS.get(stage or "", "Pipeline")
    if message.lower().startswith(label.lower()):
        return message
    return f"{label} stage failed: {message}"


def _clear_prediction(clip: Clip) -> None:
    clip.provider_job_id = None
    clip.provider_output = None


def _output_url(output: Any) -> str | None:
    if isinstance(output, str):
        return output if output.startswith(("https://", "http://")) else None
    if isinstance(output, list):
        return next((url for value in output if (url := _output_url(value))), None)
    if isinstance(output, dict):
        return next((url for value in output.values() if (url := _output_url(value))), None)
    return None


def _normalize_reference_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.load()
            normalized = image.convert("RGB")
    except OSError as exc:
        raise AugmentError(f"FLUX Fill stage returned an invalid image: {exc}") from exc
    temporary = path.with_name(f"{path.stem}.normalized.jpg")
    normalized.save(temporary, format="JPEG", quality=95)
    temporary.replace(path)
