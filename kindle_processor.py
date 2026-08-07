"""
Core image processing logic for Kindle background formatting.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageEnhance, ImageOps, ImageDraw

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


# ── Transform pipeline ────────────────────────────────────────────────────────

def apply_transforms(
    image: Image.Image,
    rotate: int = 0,
    flip_h: bool = False,
    flip_v: bool = False,
    brightness: float = 1.0,
    contrast: float = 1.0,
    grayscale: bool = False,
    auto_contrast: bool = False,
) -> Image.Image:
    """Apply the photo-editor style transforms in a fixed order.
    rotate is degrees clockwise (0/90/180/270). brightness/contrast are
    multipliers where 1.0 == unchanged."""
    img = image
    if flip_h:
        img = ImageOps.mirror(img)
    if flip_v:
        img = ImageOps.flip(img)
    if rotate:
        # PIL rotates counter-clockwise; negate so `rotate` is clockwise degrees
        img = img.rotate(-rotate, expand=True, resample=Image.BICUBIC)
    if abs(brightness - 1.0) > 1e-3:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if abs(contrast - 1.0) > 1e-3:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if auto_contrast:
        # autocontrast doesn't handle alpha well; do it on RGB channels only
        if img.mode == "RGBA":
            rgb = ImageOps.autocontrast(img.convert("RGB"), cutoff=1)
            alpha = img.split()[-1]
            img = Image.merge("RGBA", (*rgb.split(), alpha))
        else:
            img = ImageOps.autocontrast(img, cutoff=1)
    if grayscale:
        if img.mode == "RGBA":
            gray = ImageOps.grayscale(img.convert("RGB")).convert("RGB")
            alpha = img.split()[-1]
            img = Image.merge("RGBA", (*gray.split(), alpha))
        else:
            img = ImageOps.grayscale(img).convert("RGB")
    return img


# ── Precrop stage (bg removal only; no scaling) ───────────────────────────────

def make_precrop_image(
    input_path: str | Path,
    output_path: str | Path,
    remove_bg: bool = True,
) -> Tuple[Path, Tuple[int, int]]:
    """Stage 1: open + optional bg removal. No scaling — keeps native pixels
    so the crop rectangle has full resolution to work with.
    Returns (path, (width, height))."""
    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    image = Image.open(input_path)
    if image.mode not in ("RGBA", "RGB"):
        image = image.convert("RGBA")

    if remove_bg:
        image = remove_background(image)

    image = image.convert("RGBA")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG", optimize=True)
    return output_path, image.size


# ── Crop-rect helpers ─────────────────────────────────────────────────────────

def default_crop_rect(image_w: int, image_h: int, target_w: int, target_h: int) -> dict:
    """Largest Kindle-aspect rectangle that fits inside the image, centered."""
    target_ratio = target_w / target_h
    image_ratio = image_w / image_h
    if image_ratio >= target_ratio:
        # image wider than kindle aspect: rect height = image height
        h = image_h
        w = round(h * target_ratio)
    else:
        w = image_w
        h = round(w / target_ratio)
    x = (image_w - w) // 2
    y = (image_h - h) // 2
    return {"x": x, "y": y, "w": w, "h": h}


def _clamp_rect(rect: dict, image_w: int, image_h: int) -> dict:
    w = max(1, min(round(rect["w"]), image_w))
    h = max(1, min(round(rect["h"]), image_h))
    x = max(0, min(round(rect["x"]), image_w - w))
    y = max(0, min(round(rect["y"]), image_h - h))
    return {"x": x, "y": y, "w": w, "h": h}


# ── Renderers used by the editor ──────────────────────────────────────────────

def _apply_pipeline(precrop_path: str | Path, tfx: dict) -> Image.Image:
    """Open precrop and apply the transform block."""
    img = Image.open(precrop_path).convert("RGBA")
    return apply_transforms(
        img,
        rotate=tfx.get("rotate", 0),
        flip_h=tfx.get("flip_h", False),
        flip_v=tfx.get("flip_v", False),
        brightness=tfx.get("brightness", 1.0),
        contrast=tfx.get("contrast", 1.0),
        grayscale=tfx.get("grayscale", False),
        auto_contrast=tfx.get("auto_contrast", False),
    )


def render_transformed_size(precrop_path: str | Path, tfx: dict) -> Tuple[int, int]:
    """Return the pixel size after transforms (needed to size the crop UI)."""
    return _apply_pipeline(precrop_path, tfx).size


def render_overview(
    precrop_path: str | Path,
    tfx: dict,
    crop_rect: dict,
    max_display_px: int = 800,
) -> Tuple[Image.Image, float]:
    """Full transformed image, darkened outside the crop rect, with a gold
    border around the rect. Returns (display_image, display_scale) where
    display_scale = display_px / source_px so JS can convert coords."""
    src = _apply_pipeline(precrop_path, tfx)
    sw, sh = src.size

    rect = _clamp_rect(crop_rect, sw, sh)
    rx, ry, rw, rh = rect["x"], rect["y"], rect["w"], rect["h"]

    base = Image.new("RGB", (sw, sh), (0, 0, 0))
    base.paste(src.convert("RGB"), (0, 0), src)
    dark = ImageEnhance.Brightness(base).enhance(0.35)
    dark.paste(base.crop((rx, ry, rx + rw, ry + rh)), (rx, ry))

    draw = ImageDraw.Draw(dark)
    border = max(2, min(sw, sh) // 400)
    draw.rectangle(
        [rx, ry, rx + rw - 1, ry + rh - 1],
        outline=(212, 175, 55),
        width=border,
    )

    scale = min(max_display_px / max(sw, sh), 1.0)
    if scale < 1.0:
        dark = dark.resize((round(sw * scale), round(sh * scale)), Image.LANCZOS)
    return dark, scale


def _crop_and_scale(
    precrop_path: str | Path,
    tfx: dict,
    crop_rect: dict,
    target_w: int,
    target_h: int,
) -> Image.Image:
    """Apply transforms, crop to the rect, scale to exact Kindle dims.
    Always returns RGBA — alpha is preserved so callers can decide whether
    to flatten. Flattening here is what destroyed transparency previously."""
    src = _apply_pipeline(precrop_path, tfx)
    sw, sh = src.size
    rect = _clamp_rect(crop_rect, sw, sh)
    cropped = src.crop((rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]))
    return cropped.resize((target_w, target_h), Image.LANCZOS)


def _flatten(image: Image.Image, bg: Tuple[int, int, int] = (0, 0, 0)) -> Image.Image:
    """Composite an RGBA image onto a solid background, returning RGB."""
    out = Image.new("RGB", image.size, bg)
    rgba = image.convert("RGBA")
    out.paste(rgba.convert("RGB"), (0, 0), rgba)
    return out


def render_preview(
    precrop_path: str | Path,
    tfx: dict,
    crop_rect: dict,
    target_w: int,
    target_h: int,
    flatten: bool = False,
) -> Image.Image:
    """Exact Kindle-sized output preview.
    flatten=False keeps the alpha channel so the UI can show a checkerboard
    and the user can see what is actually transparent."""
    scaled = _crop_and_scale(precrop_path, tfx, crop_rect, target_w, target_h)
    return _flatten(scaled) if flatten else scaled


def render_final(
    precrop_path: str | Path,
    output_path: str | Path,
    tfx: dict,
    crop_rect: dict,
    target_w: int,
    target_h: int,
    flatten: bool = False,
) -> Path:
    """Stage 3: write the final Kindle PNG to disk.
    flatten=False (default) writes RGBA with transparency intact."""
    output_path = Path(output_path)
    scaled = _crop_and_scale(precrop_path, tfx, crop_rect, target_w, target_h)
    final = _flatten(scaled) if flatten else scaled
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(output_path, format="PNG", optimize=True)
    return output_path
