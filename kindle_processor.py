"""
Core image processing logic for Kindle background formatting.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Tuple

from PIL import Image

# Kindle screen resolutions (width x height in pixels)
KINDLE_MODELS: dict[str, Tuple[int, int]] = {
    "basic":        (1072, 1448),   # Kindle 11th gen (2022)
    "paperwhite":   (1236, 1648),   # Kindle Paperwhite 11th/12th gen
    "oasis":        (1264, 1680),   # Kindle Oasis 3rd gen
    "colorsoft":    (1264, 1680),   # Kindle Colorsoft (2024)
    "scribe":       (1860, 2480),   # Kindle Scribe
}


def list_models() -> list[str]:
    return list(KINDLE_MODELS.keys())


def get_dimensions(model: str) -> Tuple[int, int]:
    model = model.lower()
    if model not in KINDLE_MODELS:
        raise ValueError(
            f"Unknown model '{model}'. Available: {', '.join(KINDLE_MODELS)}"
        )
    return KINDLE_MODELS[model]


def remove_background(image: Image.Image) -> Image.Image:
    """Remove background using rembg, return RGBA image."""
    try:
        from rembg import remove as rembg_remove
    except ImportError:
        raise RuntimeError("rembg is required for background removal. Run: pip install rembg")

    buf_in = io.BytesIO()
    image.save(buf_in, format="PNG")
    buf_in.seek(0)

    result_bytes = rembg_remove(buf_in.read())
    return Image.open(io.BytesIO(result_bytes)).convert("RGBA")


def smart_crop(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """
    Resize and center-crop an image to exactly (target_w, target_h).
    Scales so the image fully covers the target area, then crops center.
    """
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return image.crop((left, top, left + target_w, top + target_h))


def fit_with_padding(
    image: Image.Image,
    target_w: int,
    target_h: int,
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Image.Image:
    """
    Fit image inside (target_w, target_h) without cropping, padding with bg_color.
    Returns RGBA image.
    """
    src_w, src_h = image.size
    scale = min(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    resized = image.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (target_w, target_h), bg_color)
    x = (target_w - new_w) // 2
    y = (target_h - new_h) // 2

    if resized.mode == "RGBA":
        canvas.paste(resized, (x, y), resized)
    else:
        canvas.paste(resized, (x, y))

    return canvas


def process_image(
    input_path: str | Path,
    output_path: str | Path,
    model: str = "paperwhite",
    remove_bg: bool = True,
    crop_mode: str = "fill",
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 0),
) -> Path:
    """
    Process an image for use as a Kindle screensaver/background.

    Args:
        input_path:  Source image file.
        output_path: Destination PNG file.
        model:       Kindle model name (see KINDLE_MODELS).
        remove_bg:   Whether to remove the image background.
        crop_mode:   'fill' (crop to fill screen) or 'fit' (letterbox/pad).
        bg_color:    RGBA background color used in 'fit' mode.

    Returns:
        Path to the written output file.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    target_w, target_h = get_dimensions(model)
    image = Image.open(input_path)

    # Normalise to RGBA early so all operations are consistent
    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA")

    if remove_bg:
        print("  Removing background…")
        image = remove_background(image)

    if crop_mode == "fill":
        print(f"  Cropping to {target_w}×{target_h} (fill)…")
        image = smart_crop(image.convert("RGBA"), target_w, target_h)
    else:
        print(f"  Fitting to {target_w}×{target_h} (fit/pad)…")
        image = fit_with_padding(image.convert("RGBA"), target_w, target_h, bg_color)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    print(f"  Saved → {output_path}  ({target_w}×{target_h} px)")
    return output_path
