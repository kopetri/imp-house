import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

import httpx

from imp_house.mjpeg import MultipartMjpegParser, parse_boundary

log = logging.getLogger(__name__)


class CameraHub:
    """Holds the single connection to the ESP32-CAM and fans frames out to subscribers."""

    def __init__(
        self,
        url: str,
        idle_disconnect_seconds: float = 10.0,
        reconnect_delay_seconds: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._idle_disconnect = idle_disconnect_seconds
        self._reconnect_delay = reconnect_delay_seconds
        self._transport = transport
        self._subscribers: set[asyncio.Queue[bytes]] = set()
        self._wanted = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.latest_frame: bytes | None = None
        self.connected = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="camera-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[bytes]]:
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        self._wanted.set()
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def _publish(self, frame: bytes) -> None:
        self.latest_frame = frame
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    async def _run(self) -> None:
        while True:
            await self._wanted.wait()
            try:
                await self._stream()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Camera stream error: %s", exc)
            finally:
                self.connected = False
            if not self._subscribers:
                self._wanted.clear()
            else:
                await asyncio.sleep(self._reconnect_delay)

    async def _stream(self) -> None:
        timeout = httpx.Timeout(10.0, read=10.0)
        async with (
            httpx.AsyncClient(timeout=timeout, transport=self._transport) as client,
            client.stream("GET", self._url) as response,
        ):
            response.raise_for_status()
            boundary = parse_boundary(response.headers.get("content-type", ""))
            if not boundary:
                raise ValueError("Camera response has no multipart boundary")
            parser = MultipartMjpegParser(boundary)
            self.connected = True
            log.info("Connected to camera")
            idle_since: float | None = None
            async for chunk in response.aiter_bytes():
                for frame in parser.feed(chunk):
                    self._publish(frame)
                if self._subscribers:
                    idle_since = None
                    continue
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since > self._idle_disconnect:
                    log.info("No subscribers, disconnecting from camera")
                    return
