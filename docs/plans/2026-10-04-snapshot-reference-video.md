# Snapshot Reference-and-Video Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace camera-video recording and single-model augmentation with a snapshot-to-reference-image-to-generated-video pipeline.

**Architecture:** Each scheduled/manual capture stores one JPEG. A durable worker asks Gemini to return scene/action text and a normalized placement box, rasterizes a feathered mask, runs FLUX Fill Pro, then runs Seedance 2.0 from the generated reference image. It stores every intermediate and keeps the existing generated-MP4-to-MJPEG playback contract for the CYD. Historical recordings remain available; no archived media is purged.

**Tech Stack:** FastAPI, SQLAlchemy/Alembic, Replicate HTTP API, Pillow, pytest/respx, React/Vite.

---

## Verified Model Contracts

- Gemini `google/gemini-2.5-flash:37fd5e5ec0769f0bbe58ec8248418fc570778813444b508b226a25cb04679d07`: input `prompt` and `images: string[]`; output is an array of text chunks.
- FLUX Fill Pro `black-forest-labs/flux-fill-pro:41c767bcbfffe54ef8f05eb4d0100f9314790f7fc43a7b88d73ec06839deddb9`: required `prompt`, `image`; optional `mask`; output is a URI. Mask must match the source image dimensions; white is inpainted and black preserved.
- Seedance 2.0 `bytedance/seedance-2.0:a6dcbae88b153e75fcccabacfb0eb430ab5be0a7ae27b316fc6f983658b349bc`: input `prompt`, first-frame `image`; output is a URI. Use five seconds, 720p, adaptive aspect ratio, and disable audio for the silent display.

## Data and Processing Contract

- Prompt `text` remains the stored character description; the UI relabels it and removes obsolete single-model/video-key JSON controls.
- Each new capture has `snapshot_path`; each processed capture stores `scene_prompt`, normalized `mask_bbox`, `mask_path`, and `reference_path`. `pipeline_stage` and `provider_job_id` let the worker resume a running Replicate job after restart.
- `original_path` becomes nullable for new rows but remains populated for archived video captures. Historical media is retained and shown read-only; archived videos cannot be requeued through the new image pipeline.
- `duration_seconds` remains the generated-video duration for browser/device display. Scheduled entries and capture settings no longer carry capture duration.

## Tasks

### 1. Capture and scheduling become snapshots

**Files:** `imp_house/recorder.py`, `imp_house/scheduler.py`, `imp_house/models.py`, `imp_house/api/clips.py`, `imp_house/api/config_api.py`, new Alembic revision, `tests/test_pipeline.py`, `tests/test_api.py`.

- Replace frame-window capture/MP4 encoding with one JPEG capture.
- Remove seconds from manual capture, schedule entries, and recording settings; keep count and daily time window.
- Add snapshot/reference/mask/scene/stage fields, allow nullable legacy `original_path`, and migrate away obsolete duration/model-input settings.
- Reject reprocessing legacy video rows with a clear API response while preserving their view/download paths.

### 2. Replicate media and mask primitives

**Files:** `imp_house/augment.py`, new focused helper module if useful, `pyproject.toml`, `uv.lock`, `tests/test_pipeline.py`.

- Generalize Replicate submission to accept prebuilt inputs and pin model versions through `/predictions`.
- Return typed raw outputs so Gemini text chunks are distinct from Flux/Seedance file URLs.
- Add a bounded JPEG/PNG data-URI helper and a Pillow mask builder that validates normalized bbox coordinates, draws a soft-edged white region on black at the source dimensions, and saves a reusable mask.

### 3. Persisted three-stage worker

**Files:** `imp_house/worker.py`, `imp_house/models.py`, `tests/test_pipeline.py`.

- VLM receives the snapshot plus character description and returns JSON containing a normalized bbox and a concise action prompt grounded in visible surroundings.
- FLUX receives snapshot, mask, and a prompt combining character appearance with the generated scene/action.
- Seedance receives the Flux image as its first frame and the action prompt, then its MP4 is transcoded to the existing CYD MJPEG format.
- Persist outputs and stage transitions before advancing; resume the current remote job after restart and retry only the failed stage.

### 4. API and UI switch

**Files:** `imp_house/api/clips.py`, `imp_house/api/config_api.py`, `frontend/src/api.ts`, `frontend/src/pages/Prompts.tsx`, `Dashboard.tsx`, `Settings.tsx`, `Clips.tsx`, `ClipDetail.tsx`.

- Prompt form exposes character description only.
- Dashboard offers “Capture snapshot” without a seconds input; schedule/settings expose daily count and time window only.
- Clip detail shows source snapshot, generated mask, Flux reference, generated video, VLM scene/action, and current stage/error. Archived videos retain their legacy detail display.
- Keep `/api/device/clips` and `/playback.mjpeg` compatible with generated video playback.

### 5. Documentation and verification

**Files:** `README.md`, focused backend and frontend tests.

Run `poetry run pytest`, `poetry run ruff check` on touched Python files, and `npm run lint`. Verify Alembic upgrade against a database containing legacy rows, confirm the mocked three-stage pipeline reaches `DONE`, and inspect `git diff --check`. Do not run a live/billable model prediction or delete archived media.

## Key Risks

- VLM output may not be valid JSON or bbox coordinates; validate strictly and fail at the VLM stage with a useful error rather than creating an unbounded mask.
- A rectangular/feathered mask is approximate; keep the generated mask visible for inspection and make bbox clamping/minimum-size rules deterministic.
- Seedance’s MP4 output must remain compatible with the existing FFmpeg playback transcode and device frame-size constraints.
