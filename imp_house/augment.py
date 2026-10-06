import base64
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

GEMINI_MODEL = "google/gemini-2.5-flash:37fd5e5ec0769f0bbe58ec8248418fc570778813444b508b226a25cb04679d07"
FLUX_FILL_MODEL = (
    "black-forest-labs/flux-fill-pro:41c767bcbfffe54ef8f05eb4d0100f9314790f7fc43a7b88d73ec06839deddb9"
)
SEEDANCE_MODEL = "bytedance/seedance-2.0:a6dcbae88b153e75fcccabacfb0eb430ab5be0a7ae27b316fc6f983658b349bc"
_PINNED_MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*/[a-z0-9][a-z0-9_.-]*:[a-f0-9]{64}$")
_MAX_IMAGE_DATA_URI_LENGTH = 10 * 1024 * 1024


class AugmentError(RuntimeError):
    """Provider rejected or failed the job; retrying the same input is unlikely to help."""


@dataclass
class JobState:
    status: str  # pending | succeeded | failed
    output: Any = None
    error: str | None = None


class PredictionClient(Protocol):
    async def submit(self, model: str, inputs: dict[str, Any]) -> str: ...

    async def poll(self, job_id: str) -> JobState: ...

    async def download(self, url: str, destination: Path) -> None: ...


class ReplicateAugmenter:
    def __init__(
        self, api_key: str, base_url: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_host = urlparse(self._base_url).hostname
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            transport=transport,
        )
        self._download_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True, transport=transport
        )
        self._api_key = api_key

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._download_client.aclose()

    async def submit(self, model: str, inputs: dict[str, Any]) -> str:
        if not _PINNED_MODEL_PATTERN.fullmatch(model):
            raise AugmentError(f"Model must use a pinned Replicate version: {model!r}")
        payload = {"version": model.rsplit(":", 1)[1], "input": inputs}
        response = await self._client.post("/predictions", json=payload)
        if response.status_code in (400, 404, 422):
            raise AugmentError(f"Replicate rejected prediction: {_detail(response)}")
        response.raise_for_status()
        return response.json()["id"]

    async def poll(self, job_id: str) -> JobState:
        response = await self._client.get(f"/predictions/{job_id}")
        response.raise_for_status()
        body = response.json()
        status = body.get("status")
        if status == "succeeded":
            output = body.get("output")
            if output is None:
                return JobState("failed", error="Prediction succeeded without an output")
            return JobState("succeeded", output=output)
        if status in ("failed", "canceled", "aborted"):
            return JobState("failed", error=str(body.get("error") or status)[:1000])
        return JobState("pending")

    async def download(self, url: str, destination: Path) -> None:
        is_api = urlparse(url).hostname == self._api_host
        headers = {"Authorization": f"Bearer {self._api_key}"} if is_api else {}
        tmp = destination.with_suffix(destination.suffix + ".part")
        async with self._download_client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    handle.write(chunk)
        tmp.replace(destination)


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))[:500]
    except ValueError:
        return response.text[:500]


def image_data_uri(image: Path) -> str:
    content_type, _ = mimetypes.guess_type(image.name)
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise AugmentError(f"Unsupported image type for Replicate input: {image.suffix or '(none)'}")
    encoded_length = 4 * ((image.stat().st_size + 2) // 3)
    prefix = f"data:{content_type};base64,"
    if len(prefix) + encoded_length > _MAX_IMAGE_DATA_URI_LENGTH:
        raise AugmentError("Image exceeds the application's 10 MiB data URI limit")
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    return f"{prefix}{encoded}"
