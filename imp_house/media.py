import asyncio
import logging
from pathlib import Path

from imp_house.mjpeg import max_playback_frame_bytes, write_playback_index

log = logging.getLogger(__name__)

PLAYBACK_WIDTH = 320
PLAYBACK_HEIGHT = 240
PLAYBACK_FPS = 12.0
MAX_CYD_JPEG_BYTES = 60 * 1024


class MediaError(RuntimeError):
    pass


async def _ffmpeg(args: list[str], stdin: bytes | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate(stdin)
    if process.returncode != 0:
        raise MediaError(f"ffmpeg failed ({process.returncode}): {stderr.decode(errors='replace')[-500:]}")


async def encode_jpegs_to_mp4(frames: list[bytes], fps: float, output: Path) -> None:
    await _ffmpeg(
        [
            "-f",
            "image2pipe",
            "-framerate",
            f"{fps:.3f}",
            "-c:v",
            "mjpeg",
            "-i",
            "-",
            "-vf",
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ],
        stdin=b"".join(frames),
    )


async def transcode_to_playback(source: Path, output: Path) -> tuple[float, int]:
    attempts = [
        *((PLAYBACK_WIDTH, PLAYBACK_HEIGHT, quality) for quality in (6, 12, 18, 24, 30)),
        *((288, 216, quality) for quality in (16, 24, 30)),
        *((256, 192, quality) for quality in (20, 28)),
        (224, 168, 30),
    ]
    for width, height, quality in attempts:
        scale = (
            f"fps={PLAYBACK_FPS},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
        args = ["-i", str(source), "-an", "-vf", scale, "-c:v", "mjpeg", "-q:v", str(quality)]
        await _ffmpeg([*args, "-f", "mjpeg", str(output)])
        frame_count = write_playback_index(output, PLAYBACK_FPS)
        if frame_count == 0:
            raise MediaError("Transcode produced no frames")
        if max_playback_frame_bytes(output) < MAX_CYD_JPEG_BYTES:
            return PLAYBACK_FPS, frame_count
        log.info("Reducing CYD playback output to %dx%d at JPEG quality %d", width, height, quality)

    raise MediaError("Could not encode CYD playback frames below 60 KiB")
