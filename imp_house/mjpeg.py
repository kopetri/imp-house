import json
import re
from collections.abc import Iterator
from pathlib import Path

BOUNDARY = "impframe"
CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={BOUNDARY}"

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"
_CONTENT_LENGTH = re.compile(rb"content-length:\s*(\d+)", re.IGNORECASE)
_MAX_BUFFER = 2 * 1024 * 1024


def parse_boundary(content_type: str) -> str | None:
    match = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).removeprefix("--")


class MultipartMjpegParser:
    """Incremental parser for multipart/x-mixed-replace JPEG streams."""

    def __init__(self, boundary: str) -> None:
        self._delimiter = b"--" + boundary.encode()
        self._buffer = bytearray()
        self._pending_length: int | None = None
        self._in_body = False

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        if len(self._buffer) > _MAX_BUFFER:
            self._buffer.clear()
            self._in_body = False
            self._pending_length = None
        frames: list[bytes] = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return frames
            if frame.startswith(_SOI):
                frames.append(frame)

    def _next_frame(self) -> bytes | None:
        if not self._in_body:
            start = self._buffer.find(self._delimiter)
            if start < 0:
                return None
            header_end = self._buffer.find(b"\r\n\r\n", start)
            if header_end < 0:
                return None
            headers = bytes(self._buffer[start:header_end])
            del self._buffer[: header_end + 4]
            match = _CONTENT_LENGTH.search(headers)
            self._pending_length = int(match.group(1)) if match else None
            self._in_body = True

        if self._pending_length is not None:
            if len(self._buffer) < self._pending_length:
                return None
            frame = bytes(self._buffer[: self._pending_length])
            del self._buffer[: self._pending_length]
        else:
            end = self._buffer.find(self._delimiter)
            if end < 0:
                return None
            frame = bytes(self._buffer[:end]).rstrip(b"\r\n")
            del self._buffer[:end]
        self._in_body = False
        self._pending_length = None
        return frame


def encode_part(frame: bytes) -> bytes:
    header = f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n"
    return header.encode() + frame + b"\r\n"


def split_jpegs(data: bytes) -> list[tuple[int, int]]:
    """Return (offset, length) of each JPEG in a concatenated ffmpeg mjpeg stream."""
    frames: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = data.find(_SOI, pos)
        if start < 0:
            return frames
        end = data.find(_EOI, start + 2)
        if end < 0:
            return frames
        frames.append((start, end + 2 - start))
        pos = end + 2


def index_path_for(playback_path: Path) -> Path:
    return playback_path.with_suffix(".idx.json")


def write_playback_index(playback_path: Path, fps: float) -> int:
    frames = split_jpegs(playback_path.read_bytes())
    max_frame_bytes = max((length for _, length in frames), default=0)
    index_path_for(playback_path).write_text(
        json.dumps({"fps": fps, "frames": frames, "max_frame_bytes": max_frame_bytes})
    )
    return len(frames)


def max_playback_frame_bytes(playback_path: Path) -> int:
    return int(json.loads(index_path_for(playback_path).read_text()).get("max_frame_bytes", 0))


def iter_playback_frames(playback_path: Path) -> tuple[float, Iterator[bytes]]:
    index = json.loads(index_path_for(playback_path).read_text())

    def frames() -> Iterator[bytes]:
        with playback_path.open("rb") as handle:
            for offset, length in index["frames"]:
                handle.seek(offset)
                yield handle.read(length)

    return float(index["fps"]), frames()
