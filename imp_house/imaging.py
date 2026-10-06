import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter


class MaskError(ValueError):
    pass


def prepare_flux_images(snapshot_path: Path, mask_path: Path, output_dir: Path) -> tuple[Path, Path]:
    try:
        with Image.open(snapshot_path) as snapshot, Image.open(mask_path) as mask:
            image_width, image_height = snapshot.size
            if mask.size != snapshot.size:
                raise MaskError("Snapshot and mask dimensions do not match")
            scale = max(1.0, 256 / image_width, 256 / image_height)
            if scale == 1:
                return snapshot_path, mask_path

            output_size = (
                max(256, round(image_width * scale)),
                max(256, round(image_height * scale)),
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            flux_snapshot = output_dir / "snapshot.jpg"
            flux_mask = output_dir / "mask.png"
            snapshot.convert("RGB").resize(output_size, Image.Resampling.LANCZOS).save(
                flux_snapshot, format="JPEG", quality=95
            )
            mask.convert("L").resize(output_size, Image.Resampling.LANCZOS).save(
                flux_mask, format="PNG"
            )
            return flux_snapshot, flux_mask
    except OSError as exc:
        raise MaskError(f"Cannot read FLUX input images: {exc}") from exc


def create_feathered_mask(snapshot_path: Path, bbox: dict[str, Any], output_path: Path) -> None:
    try:
        with Image.open(snapshot_path) as snapshot:
            image_width, image_height = snapshot.size
    except OSError as exc:
        raise MaskError(f"Cannot read snapshot image: {exc}") from exc

    bbox_x, bbox_y, bbox_width, bbox_height = _read_bbox(bbox)
    left = round(bbox_x * image_width)
    top = round(bbox_y * image_height)
    right = min(image_width, max(left + 1, round((bbox_x + bbox_width) * image_width)))
    bottom = min(image_height, max(top + 1, round((bbox_y + bbox_height) * image_height)))

    mask = Image.new("L", (image_width, image_height), 0)
    ImageDraw.Draw(mask).rectangle((left, top, right - 1, bottom - 1), fill=255)
    feather_radius = max(2, min(24, round(min(image_width, image_height) * 0.02)))
    mask = mask.filter(ImageFilter.GaussianBlur(feather_radius))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mask.save(output_path, format="PNG")


def _read_bbox(bbox: dict[str, Any]) -> tuple[float, float, float, float]:
    values: list[float] = []
    for key in ("x", "y", "width", "height"):
        value = bbox.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise MaskError("Bounding box values must be normalized numbers")
        coordinate = float(value)
        if not math.isfinite(coordinate) or not 0 <= coordinate <= 1:
            raise MaskError("Bounding box values must be normalized numbers")
        values.append(coordinate)

    bbox_x, bbox_y, bbox_width, bbox_height = values
    if bbox_x >= 1 or bbox_y >= 1 or bbox_width <= 0 or bbox_height <= 0:
        raise MaskError("Bounding box must have positive area within the image")
    return bbox_x, bbox_y, min(bbox_width, 1 - bbox_x), min(bbox_height, 1 - bbox_y)