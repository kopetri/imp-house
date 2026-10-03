"""Fake ESP32-CAM MJPEG server for local testing (needs ffmpeg)."""

import asyncio
import subprocess
import sys

from imp_house.mjpeg import split_jpegs

BOUNDARY = "123456789000000000000987654321"


async def frames(queue_size: int = 2):
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=640x480:rate=10",
        "-f", "mjpeg", "-q:v", "6", "pipe:1",
        stdout=subprocess.PIPE,
    )
    buffer = b""
    assert process.stdout
    while chunk := await process.stdout.read(65536):
        buffer += chunk
        spans = split_jpegs(buffer)
        for offset, length in spans:
            yield buffer[offset : offset + length]
        if spans:
            end = spans[-1][0] + spans[-1][1]
            buffer = buffer[end:]


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readuntil(b"\r\n\r\n")
    header = f"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace;boundary={BOUNDARY}\r\n\r\n"
    writer.write(header.encode())
    try:
        async for frame in frames():
            writer.write(
                f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n\r\n".encode()
                + frame
                + b"\r\n"
            )
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def main(port: int) -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", port)  # noqa: S104
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8081))
