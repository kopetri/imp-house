from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import base64

import httpx

_VIDEO_DATA_URI_PREFIX = "data:video/mp4;base64,"
_MAX_VIDEO_DATA_URI_LENGTH = 5 * 1024 * 1024


class AugmentError(RuntimeError):
    """Provider rejected or failed the job; retrying the same input is unlikely to help."""


@dataclass
class JobState:
    status: str  # pending | succeeded | failed
    output_url: str | None = None
    error: str | None = None


class VideoAugmenter(Protocol):
    async def submit(self, video: Path, model: str, inputs: dict[str, Any], video_key: str) -> str: ...

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

    async def submit(self, video: Path, model: str, inputs: dict[str, Any], video_key: str) -> str:
        video_uri = _video_data_uri(video)
        payload: dict[str, Any] = {"input": {**inputs, video_key: video_uri}}
        if ":" in model:
            payload["version"] = model.split(":", 1)[1]
            response = await self._client.post("/predictions", json=payload)
        else:
            owner, _, name = model.partition("/")
            if not owner or not name:
                raise AugmentError(f"Invalid model identifier: {model!r}")
            response = await self._client.post(f"/models/{owner}/{name}/predictions", json=payload)
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
            url = _first_url(body.get("output"))
            if not url:
                return JobState("failed", error="Prediction succeeded without a video output")
            return JobState("succeeded", output_url=url)
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

def _first_url(output: Any) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return next((item for item in output if isinstance(item, str)), None)
    if isinstance(output, dict):
        return next((value for value in output.values() if isinstance(value, str)), None)
    return None


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))[:500]
    except ValueError:
        return response.text[:500]


def _video_data_uri(video: Path) -> str:
    encoded_length = 4 * ((video.stat().st_size + 2) // 3)
    if len(_VIDEO_DATA_URI_PREFIX) + encoded_length > _MAX_VIDEO_DATA_URI_LENGTH:
        raise AugmentError(
            "Video exceeds Runway's 5 MiB data URI limit; use a shorter clip or a public HTTPS URL "
            "that returns Content-Type: video/mp4."
        )
    encoded = base64.b64encode(video.read_bytes()).decode("ascii")
    return f"{_VIDEO_DATA_URI_PREFIX}{encoded}"
