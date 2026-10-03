import json

from imp_house.mjpeg import (
    MultipartMjpegParser,
    encode_part,
    iter_playback_frames,
    max_playback_frame_bytes,
    parse_boundary,
    split_jpegs,
    write_playback_index,
)
from tests.conftest import jpeg


def _stream(frames: list[bytes], with_length: bool) -> bytes:
    out = b""
    for frame in frames:
        length = f"Content-Length: {len(frame)}\r\n" if with_length else ""
        out += f"--abc\r\nContent-Type: image/jpeg\r\n{length}\r\n".encode() + frame + b"\r\n"
    return out + b"--abc\r\n"


def test_parse_boundary_variants() -> None:
    assert parse_boundary("multipart/x-mixed-replace;boundary=123456789000000000000987654321") == (
        "123456789000000000000987654321"
    )
    assert parse_boundary('multipart/x-mixed-replace; boundary="--frame"') == "frame"
    assert parse_boundary("image/jpeg") is None


def test_parser_handles_arbitrary_chunking() -> None:
    frames = [jpeg(b"one"), jpeg(b"two" * 100), jpeg(b"\r\n--abc inside")]
    data = _stream(frames, with_length=True)
    for size in (1, 7, 64, len(data)):
        parser = MultipartMjpegParser("abc")
        out = []
        for i in range(0, len(data), size):
            out += parser.feed(data[i : i + size])
        assert out == frames


def test_parser_without_content_length() -> None:
    frames = [jpeg(b"a"), jpeg(b"b")]
    parser = MultipartMjpegParser("abc")
    assert parser.feed(_stream(frames, with_length=False)) == frames


def test_encode_part_roundtrip() -> None:
    frame = jpeg(b"payload")
    parser = MultipartMjpegParser("impframe")
    assert parser.feed(encode_part(frame) + encode_part(frame)) == [frame, frame]


def test_playback_index(tmp_path) -> None:
    frames = [jpeg(b"1"), jpeg(b"22"), jpeg(b"333")]
    path = tmp_path / "playback.mjpeg"
    path.write_bytes(b"".join(frames))
    assert split_jpegs(path.read_bytes()) == [(0, 7), (7, 8), (15, 9)]
    assert write_playback_index(path, 12.0) == 3
    assert json.loads(path.with_suffix(".idx.json").read_text())["fps"] == 12.0
    assert max_playback_frame_bytes(path) == 9
    fps, iterator = iter_playback_frames(path)
    assert fps == 12.0
    assert list(iterator) == frames
