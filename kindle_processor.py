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
        print("  Removing background...")
        image = remove_background(image)

    if crop_mode == "fill":
        print(f"  Cropping to {target_w}x{target_h} (fill)...")
        image = smart_crop(image.convert("RGBA"), target_w, target_h)
    else:
        print(f"  Fitting to {target_w}x{target_h} (fit/pad)...")
        image = fit_with_padding(image.convert("RGBA"), target_w, target_h, bg_color)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    print(f"  Saved -> {output_path}  ({target_w}x{target_h} px)")
    return output_path


# ── Manual crop pipeline ───────────────────────────────────────────────────────


def scale_to_cover(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale so image fully covers (target_w x target_h); both dims >= target."""
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    return image.resize((round(src_w * scale), round(src_h * scale)), Image.LANCZOS)


def scale_to_fit(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Scale so image fits inside (target_w x target_h); both dims <= target."""
    src_w, src_h = image.size
    scale = min(target_w / src_w, target_h / src_h)
    return image.resize((round(src_w * scale), round(src_h * scale)), Image.LANCZOS)


def apply_fill_crop(
    scaled_image: Image.Image,
    target_w: int,
    target_h: int,
    x_pct: float,
    y_pct: float,
) -> Image.Image:
    """Crop a scale_to_cover image to (target_w, target_h) at the given position.
    x_pct/y_pct are 0.0–1.0 fractions of the available travel range."""
    sw, sh = scaled_image.size
    max_x = max(sw - target_w, 0)
    max_y = max(sh - target_h, 0)
    x = round(x_pct * max_x)
    y = round(y_pct * max_y)
    return scaled_image.crop((x, y, x + target_w, y + target_h))


def apply_fit_placement(
    scaled_image: Image.Image,
    target_w: int,
    target_h: int,
    x_pct: float,
    y_pct: float,
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 255),
) -> Image.Image:
    """Place a scale_to_fit image on a (target_w x target_h) canvas.
    x_pct/y_pct control alignment: 0.0=top-left, 0.5=center, 1.0=bottom-right."""
    sw, sh = scaled_image.size
    avail_x = max(target_w - sw, 0)
    avail_y = max(target_h - sh, 0)
    x = round(x_pct * avail_x)
    y = round(y_pct * avail_y)
    canvas = Image.new("RGBA", (target_w, target_h), bg_color)
    src = scaled_image.convert("RGBA")
    canvas.paste(src, (x, y), src)
    return canvas


def make_precrop_image(
    input_path: str | Path,
    output_path: str | Path,
    model: str = "paperwhite",
    remove_bg: bool = True,
    crop_mode: str = "fill",
) -> Tuple[Path, Tuple[int, int]]:
    """
    Stage 1 of the manual-crop pipeline: open -> bg removal -> scale (no crop).
    Saves the intermediate PNG to output_path.
    Returns (output_path, (scaled_w, scaled_h)).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    target_w, target_h = get_dimensions(model)
    image = Image.open(input_path)

    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA")

    if remove_bg:
        print("  Removing background...")
        image = remove_background(image)

    image = image.convert("RGBA")

    scaled = scale_to_cover(image, target_w, target_h) if crop_mode == "fill" \
        else scale_to_fit(image, target_w, target_h)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    scaled.save(output_path, format="PNG", optimize=True)
    print(f"  Scaled ({crop_mode}) -> {scaled.size[0]}x{scaled.size[1]}")
    return output_path, scaled.size


def make_final_image(
    precrop_path: str | Path,
    output_path: str | Path,
    target_w: int,
    target_h: int,
    crop_mode: str,
    x_pct: float,
    y_pct: float,
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 255),
) -> Path:
    """Stage 3: apply crop/placement settings and save the final Kindle PNG."""
    output_path = Path(output_path)
    image = Image.open(precrop_path).convert("RGBA")

    if crop_mode == "fill":
        result = apply_fill_crop(image, target_w, target_h, x_pct, y_pct)
    else:
        result = apply_fit_placement(image, target_w, target_h, x_pct, y_pct, bg_color)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path, format="PNG", optimize=True)
    print(f"  Saved -> {output_path}  ({target_w}x{target_h} px)")
    return output_path


def make_overview_image(
    precrop_path: str | Path,
    target_w: int,
    target_h: int,
    crop_mode: str,
    x_pct: float,
    y_pct: float,
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 255),
    max_display_px: int = 700,
) -> Image.Image:
    """
    Crop editor left panel: full scaled image with crop window highlighted.
    Fill mode: shows full cover-scaled image; crop window bright, rest darkened.
    Fit mode: shows the complete Kindle canvas with the image positioned.
    Scaled to fit within max_display_px on the longest axis.
    """
    from PIL import ImageEnhance, ImageDraw

    image = Image.open(precrop_path).convert("RGBA")

    if crop_mode == "fill":
        sw, sh = image.size
        max_x = max(sw - target_w, 0)
        max_y = max(sh - target_h, 0)
        x = round(x_pct * max_x)
        y = round(y_pct * max_y)

        # Composite RGBA on black, then darken, paste bright window back
        base = Image.new("RGB", image.size, (0, 0, 0))
        base.paste(image.convert("RGB"), (0, 0), image)
        dark = ImageEnhance.Brightness(base).enhance(0.3)
        bright_patch = base.crop((x, y, x + target_w, y + target_h))
        dark.paste(bright_patch, (x, y))
        draw = ImageDraw.Draw(dark)
        draw.rectangle(
            [x, y, x + target_w - 1, y + target_h - 1],
            outline=(255, 215, 0),
            width=3,
        )
        result = dark

    else:  # fit
        sw, sh = image.size
        avail_x = max(target_w - sw, 0)
        avail_y = max(target_h - sh, 0)
        xp = round(x_pct * avail_x)
        yp = round(y_pct * avail_y)

        canvas = Image.new("RGBA", (target_w, target_h), bg_color)
        canvas.paste(image, (xp, yp), image)
        result = canvas.convert("RGB")
        draw = ImageDraw.Draw(result)
        draw.rectangle(
            [0, 0, target_w - 1, target_h - 1],
            outline=(255, 215, 0),
            width=2,
        )

    w, h = result.size
    scale = min(max_display_px / max(w, h), 1.0)
    if scale < 1.0:
        result = result.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    return result


def make_preview_image(
    precrop_path: str | Path,
    target_w: int,
    target_h: int,
    crop_mode: str,
    x_pct: float,
    y_pct: float,
    bg_color: Tuple[int, int, int, int] = (0, 0, 0, 255),
) -> Image.Image:
    """Crop editor right panel: exact Kindle-sized output preview."""
    image = Image.open(precrop_path).convert("RGBA")
    if crop_mode == "fill":
        result = apply_fill_crop(image, target_w, target_h, x_pct, y_pct)
    else:
        result = apply_fit_placement(image, target_w, target_h, x_pct, y_pct, bg_color)
    # Composite on black so transparent areas show as black (Kindle background)
    final = Image.new("RGB", result.size, (0, 0, 0))
    final.paste(result.convert("RGB"), (0, 0), result)
    return final
