"""
Kindle Image Formatter — Gradio web UI
Run:  python app.py
Then open http://localhost:7860 in your browser.
"""

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import base64
import io
import json
import queue
import tempfile
import threading
import zipfile
from pathlib import Path

import gradio as gr
from PIL import Image

from kindle_processor import (
    KINDLE_MODELS,
    get_dimensions,
    make_precrop_image,
    default_crop_rect,
    render_transformed_size,
    render_overview,
    render_preview,
    render_final,
)
from calibre_sender import add_to_library
from koreader_sender import send_files as koreader_send_files, get_local_ip

MAX_FILES = 10

# Server-side session store. gr.State holds only the session key (a short string);
# the actual editor state lives here so it can't be clobbered by a stale client
# copy in the middle of rapid drag commits. Keyed by a per-process counter.
SESSION_STATES: dict[str, dict] = {}
_SESSION_COUNTER = [0]

def _new_session_key() -> str:
    _SESSION_COUNTER[0] += 1
    return f"s{_SESSION_COUNTER[0]}"

def _get_state(key):
    """Fetch server-side state by key. Returns None if the session is unknown
    (client sent a stale key after a server restart, etc.)."""
    if not key:
        return None
    return SESSION_STATES.get(key)


# ── Theme, CSS, JS ────────────────────────────────────────────────────────────

FOREST_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.green,
    secondary_hue=gr.themes.colors.emerald,
    neutral_hue=gr.themes.colors.stone,
    font=[gr.themes.GoogleFont("Nunito"), "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
).set(
    body_background_fill="linear-gradient(135deg, #f0f4e8 0%, #e6ede0 100%)",
    body_background_fill_dark="linear-gradient(135deg, #1a2419 0%, #0f1a10 100%)",
    block_background_fill="rgba(255,255,255,0.75)",
    block_background_fill_dark="rgba(30,40,30,0.85)",
    block_border_width="1px",
    block_border_color="#c8d4b8",
    block_border_color_dark="#3a4a35",
    block_radius="12px",
    block_shadow="0 2px 8px rgba(60,80,40,0.08)",
    button_primary_background_fill="#4a7c2b",
    button_primary_background_fill_hover="#3d6822",
    button_primary_text_color="white",
    button_secondary_background_fill="#8b6f47",
    button_secondary_background_fill_hover="#755a37",
    button_secondary_text_color="white",
)

FOREST_CSS = """
.gradio-container {
    max-width: 1400px !important;
    margin-left: auto !important;
    margin-right: auto !important;
}
body { display: flex; justify-content: center; }
h1, h2, h3 { color: #2d4a1a; }
.dark h1, .dark h2, .dark h3 { color: #a8c88a; }

/* Cropper container — img is source of truth for size; svg absolutely overlays it */
#kindle-crop-wrap {
    background: rgba(0,0,0,0.85);
    padding: 12px;
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
    align-items: center;
    /* NO overflow: hidden — we must not clip the source image */
}
#kindle-crop-container {
    position: relative;
    display: inline-block;
    user-select: none;
    touch-action: none;
    line-height: 0;
    max-width: 100%;
}
#kindle-crop-container img {
    display: block;
    max-width: 100%;
    max-height: 75vh;
    width: auto;
    height: auto;
    pointer-events: none;
}
#kindle-crop-container svg {
    position: absolute;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    pointer-events: none;
}
#kindle-preview-wrap {
    background: rgba(0,0,0,0.85);
    padding: 12px;
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
}
#kindle-preview-header {
    color: #a8c88a;
    font-family: monospace;
    font-size: 12px;
    align-self: flex-start;
}
#kindle-preview-wrap img {
    display: block;
    max-width: 100%;
    max-height: 75vh;
    width: auto;
    height: auto;
    border: 1px solid rgba(212,175,55,0.4);
    border-radius: 4px;
}
/* Checkerboard behind both panes so transparency is visible, not mistaken
   for solid black. */
#kindle-preview-wrap img,
#kindle-crop-container img {
    background-color: #2b2b2b;
    background-image:
        linear-gradient(45deg, #3d3d3d 25%, transparent 25%),
        linear-gradient(-45deg, #3d3d3d 25%, transparent 25%),
        linear-gradient(45deg, transparent 75%, #3d3d3d 75%),
        linear-gradient(-45deg, transparent 75%, #3d3d3d 75%);
    background-size: 18px 18px;
    background-position: 0 0, 0 9px, 9px -9px, -9px 0;
}
#crop-debug {
    color: #d4af37;
    font-family: monospace;
    font-size: 12px;
    text-align: left;
    margin-top: 10px;
    padding: 8px 12px;
    background: rgba(0,0,0,0.6);
    border-radius: 6px;
    max-width: 100%;
    word-break: break-word;
    line-height: 1.5;
}
#kindle-crop-container .crop-body,
#kindle-crop-container .crop-handle {
    pointer-events: auto;
}
#kindle-crop-container .crop-body { cursor: move; }
#kindle-crop-container .crop-handle-nw,
#kindle-crop-container .crop-handle-se { cursor: nwse-resize; }
#kindle-crop-container .crop-handle-ne,
#kindle-crop-container .crop-handle-sw { cursor: nesw-resize; }

/* Hide the bridge button but keep it clickable via JS */
#crop-bridge-btn {
    position: absolute !important;
    left: -9999px !important;
    width: 1px !important;
    height: 1px !important;
    opacity: 0 !important;
    pointer-events: none !important;
}

/* Toolbar */
.toolbar-row { gap: 6px !important; }
.toolbar-btn button {
    min-width: 44px !important;
    padding: 6px 10px !important;
    font-size: 14px !important;
}

/* Panels */
.panel-card {
    padding: 16px !important;
    border-radius: 14px !important;
}

/* Gallery hover for delete affordance */
.gradio-gallery img { transition: transform 0.15s ease; }
.gradio-gallery img:hover { transform: scale(1.02); }
"""

CROPPER_JS = r"""
<script>
window.initKindleCropper = function() {
    const container = document.getElementById('kindle-crop-container');
    if (!container) return;
    let cfg;
    try {
        cfg = JSON.parse(container.dataset.cropConfig || 'null');
    } catch (e) { cfg = null; }
    if (!cfg) return;
    // Skip if this render_id already wired (avoids double-binding on MutationObserver churn)
    const renderId = String(cfg.render_id || '');
    if (container.dataset.wired === renderId) return;
    container.dataset.wired = renderId;
    window.KINDLE_CROP_CONFIG = cfg;

    // Display -> source scaling
    const dw = cfg.display_w, dh = cfg.display_h;
    const sw = cfg.source_w, sh = cfg.source_h;
    const ratio = cfg.target_ratio;
    const scaleD = dw / sw;          // source px -> display px
    const scaleS = sw / dw;          // display px -> source px

    // Convert incoming source-space rect to display space
    const srcRect = cfg.rect;
    let rect = {
        x: srcRect.x * scaleD,
        y: srcRect.y * scaleD,
        w: srcRect.w * scaleD,
        h: srcRect.h * scaleD,
    };

    const minSide = 40;              // min crop rect side in display px
    const bounds = { w: dw, h: dh };

    const svg = container.querySelector('svg');
    const mask = container.querySelector('.crop-rect-mask');
    const body = container.querySelector('.crop-body');
    const handleNW = container.querySelector('.crop-handle-nw');
    const handleNE = container.querySelector('.crop-handle-ne');
    const handleSW = container.querySelector('.crop-handle-sw');
    const handleSE = container.querySelector('.crop-handle-se');
    const handles = [handleNW, handleNE, handleSW, handleSE];
    const HANDLE_SIZE = 14;

    function paint() {
        const r = rect;
        mask.setAttribute('x', r.x);
        mask.setAttribute('y', r.y);
        mask.setAttribute('width', r.w);
        mask.setAttribute('height', r.h);
        body.setAttribute('x', r.x);
        body.setAttribute('y', r.y);
        body.setAttribute('width', r.w);
        body.setAttribute('height', r.h);
        const dbg = document.getElementById('crop-debug');
        if (dbg) {
            const svgClient = svg.getBoundingClientRect();
            const srcX = Math.round(r.x * scaleS), srcY = Math.round(r.y * scaleS);
            const srcW = Math.round(r.w * scaleS), srcH = Math.round(r.h * scaleS);
            const covPct = ((srcW * srcH) / (sw * sh) * 100).toFixed(1);
            const cutLeft = srcX;
            const cutRight = sw - (srcX + srcW);
            const cutTop = srcY;
            const cutBot = sh - (srcY + srcH);
            const rectAspect = (srcW / srcH).toFixed(4);
            const targetAspect = ratio.toFixed(4);
            const aspectOk = Math.abs(srcW/srcH - ratio) < 0.01 ? '✓' : '✗';
            dbg.innerHTML =
                `source <b>${sw}x${sh}</b> · display(viewBox) ${dw}x${dh} · svg rendered ${Math.round(svgClient.width)}x${Math.round(svgClient.height)}<br/>` +
                `crop rect (src px): x=<b>${srcX}</b> y=<b>${srcY}</b> w=<b>${srcW}</b> h=<b>${srcH}</b> (${covPct}% of source area)<br/>` +
                `cut: left=${cutLeft} right=${cutRight} top=${cutTop} bottom=${cutBot} · ` +
                `aspect ${rectAspect} vs target ${targetAspect} ${aspectOk}`;
        }
        const positions = {
            nw: [r.x, r.y],
            ne: [r.x + r.w, r.y],
            sw: [r.x, r.y + r.h],
            se: [r.x + r.w, r.y + r.h],
        };
        for (const [key, [hx, hy]] of Object.entries(positions)) {
            const h = container.querySelector('.crop-handle-' + key);
            h.setAttribute('x', hx - HANDLE_SIZE / 2);
            h.setAttribute('y', hy - HANDLE_SIZE / 2);
            h.setAttribute('width', HANDLE_SIZE);
            h.setAttribute('height', HANDLE_SIZE);
        }
    }

    function clampMove(r) {
        r.x = Math.max(0, Math.min(r.x, bounds.w - r.w));
        r.y = Math.max(0, Math.min(r.y, bounds.h - r.h));
        return r;
    }

    function resizeFrom(corner, mx, my) {
        const oldRight = rect.x + rect.w;
        const oldBottom = rect.y + rect.h;
        const oldLeft = rect.x;
        const oldTop = rect.y;
        let x, y, w, h;
        switch (corner) {
            case 'se': {
                w = Math.max(minSide, mx - oldLeft);
                h = w / ratio;
                if (oldTop + h > bounds.h) { h = bounds.h - oldTop; w = h * ratio; }
                if (oldLeft + w > bounds.w) { w = bounds.w - oldLeft; h = w / ratio; }
                x = oldLeft; y = oldTop; break;
            }
            case 'sw': {
                w = Math.max(minSide, oldRight - mx);
                h = w / ratio;
                if (oldTop + h > bounds.h) { h = bounds.h - oldTop; w = h * ratio; }
                if (w > oldRight) { w = oldRight; h = w / ratio; }
                x = oldRight - w; y = oldTop; break;
            }
            case 'ne': {
                w = Math.max(minSide, mx - oldLeft);
                h = w / ratio;
                if (h > oldBottom) { h = oldBottom; w = h * ratio; }
                if (oldLeft + w > bounds.w) { w = bounds.w - oldLeft; h = w / ratio; }
                x = oldLeft; y = oldBottom - h; break;
            }
            case 'nw': {
                w = Math.max(minSide, oldRight - mx);
                h = w / ratio;
                if (h > oldBottom) { h = oldBottom; w = h * ratio; }
                if (w > oldRight) { w = oldRight; h = w / ratio; }
                x = oldRight - w; y = oldBottom - h; break;
            }
        }
        return { x, y, w, h };
    }

    function getMouse(evt) {
        const r = svg.getBoundingClientRect();
        const pt = evt.touches ? evt.touches[0] : evt;
        // Convert client px into the svg's viewBox coord space
        const vb = svg.viewBox.baseVal;
        const scaleX = vb.width / r.width;
        const scaleY = vb.height / r.height;
        return {
            x: (pt.clientX - r.left) * scaleX,
            y: (pt.clientY - r.top) * scaleY,
        };
    }

    let dragging = null;
    let dragStart = null;

    function onDown(evt, mode) {
        evt.preventDefault();
        dragging = mode;
        const m = getMouse(evt);
        dragStart = { m, rect: { ...rect } };
        // Pointer capture ensures we get all subsequent events even if the pointer
        // leaves the SVG (or the browser window entirely).
        if (evt.pointerId != null && evt.target && evt.target.setPointerCapture) {
            try { evt.target.setPointerCapture(evt.pointerId); } catch (e) {}
        }
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
        window.addEventListener('pointercancel', onUp);
        window.addEventListener('blur', onUp);       // browser lost focus mid-drag
        document.addEventListener('mouseleave', onUp); // pointer left the viewport
    }
    function onMove(evt) {
        if (!dragging) return;
        evt.preventDefault();
        const m = getMouse(evt);
        if (dragging === 'move') {
            rect.x = dragStart.rect.x + (m.x - dragStart.m.x);
            rect.y = dragStart.rect.y + (m.y - dragStart.m.y);
            rect = clampMove(rect);
        } else {
            rect = resizeFrom(dragging, m.x, m.y);
            rect = clampMove(rect);
        }
        paint();
    }
    function onUp() {
        if (!dragging) return;
        dragging = null;
        window.removeEventListener('pointermove', onMove);
        window.removeEventListener('pointerup', onUp);
        window.removeEventListener('pointercancel', onUp);
        window.removeEventListener('blur', onUp);
        document.removeEventListener('mouseleave', onUp);
        commit();
    }

    function commit() {
        // Only the closure belonging to the currently-mounted render may commit.
        // If the container was replaced (navigation, transform re-render), this
        // closure is stale and must stay silent.
        const liveContainer = document.getElementById('kindle-crop-container');
        if (!liveContainer || liveContainer.dataset.wired !== renderId) {
            console.log('[cropper commit SKIPPED - stale closure]', renderId);
            return;
        }
        const srcRect = {
            x: Math.round(rect.x * scaleS),
            y: Math.round(rect.y * scaleS),
            w: Math.round(rect.w * scaleS),
            h: Math.round(rect.h * scaleS),
            idx: cfg.idx,
            render_id: renderId,
        };
        window.KINDLE_CROP_RESULT = srcRect;
        const btn = document.getElementById('crop-bridge-btn');
        console.log('[cropper commit]', srcRect);
        if (btn) btn.click();
    }

    body.addEventListener('pointerdown', (e) => onDown(e, 'move'));
    for (const [corner, el] of [['nw', handleNW], ['ne', handleNE], ['sw', handleSW], ['se', handleSE]]) {
        el.addEventListener('pointerdown', (e) => { e.stopPropagation(); onDown(e, corner); });
    }

    paint();
    window.KINDLE_CROP_RESULT = {
        x: srcRect.x, y: srcRect.y, w: srcRect.w, h: srcRect.h,
    };
    console.log('[cropper init] renderId=', renderId, 'rect=', srcRect);
};

// Re-init whenever new cropper HTML is injected (Gradio replaces innerHTML)
if (!window.__kindleObserver) {
    const tryInit = () => {
        if (document.getElementById('kindle-crop-container')) {
            window.initKindleCropper();
        }
    };
    window.__kindleObserver = new MutationObserver(tryInit);
    const start = () => window.__kindleObserver.observe(document.body, {
        childList: true, subtree: true, attributes: true, attributeFilter: ['data-crop-config'],
    });
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
}
</script>
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_tfx() -> dict:
    return {
        "rotate": 0,
        "flip_h": False,
        "flip_v": False,
        "brightness": 1.0,
        "contrast": 1.0,
        "grayscale": False,
        "auto_contrast": False,
    }


def _img_to_data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=False)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def cropper_html(state: dict) -> str:
    """Render the interactive cropper HTML for the current image."""
    if not state or not state.get("precrop_paths"):
        return '<div id="kindle-crop-wrap"><em style="color:#888">No image loaded.</em></div>'

    idx = state["current_idx"]
    path = state["precrop_paths"][idx]
    tfx = state["transforms"][idx]
    rect = state["crop_rects"][idx]
    kw, kh = state["kindle_w"], state["kindle_h"]

    from kindle_processor import _apply_pipeline  # local import to avoid circular
    # Keep RGBA — a CSS checkerboard sits behind the img so transparent regions
    # read as transparent rather than looking like solid black.
    src = _apply_pipeline(path, tfx)
    sw, sh = src.size

    max_display = 720
    scale = min(max_display / max(sw, sh), 1.0)
    dw, dh = round(sw * scale), round(sh * scale)
    display_img = src.resize((dw, dh), Image.LANCZOS) if scale < 1.0 else src

    src_uri = _img_to_data_uri(display_img)
    import time
    render_id = f"{idx}-{time.time_ns()}"
    # Remember which render is live. Commits carrying a different render_id are
    # from a stale closure (e.g. one left over from a previous image) and get
    # rejected instead of silently overwriting good state.
    state["active_render_id"] = render_id
    config = {
        "display_w": dw,
        "display_h": dh,
        "source_w": sw,
        "source_h": sh,
        "target_ratio": kw / kh,
        "rect": rect,
        "render_id": render_id,
        "idx": idx,
    }
    cfg_json = json.dumps(config).replace('"', "&quot;")

    return f"""
    <div id="kindle-crop-wrap">
        <div id="kindle-crop-container" data-crop-config="{cfg_json}">
            <img src="{src_uri}" width="{dw}" height="{dh}" alt="crop editor"/>
            <svg viewBox="0 0 {dw} {dh}" preserveAspectRatio="none">
                <defs>
                    <mask id="crop-mask">
                        <rect width="100%" height="100%" fill="white"/>
                        <rect class="crop-rect-mask" fill="black"/>
                    </mask>
                </defs>
                <rect width="100%" height="100%" fill="black" fill-opacity="0.55" mask="url(#crop-mask)"/>
                <rect class="crop-body" fill="transparent" stroke="#d4af37" stroke-width="3" vector-effect="non-scaling-stroke"/>
                <rect class="crop-handle crop-handle-nw" fill="#d4af37" stroke="white" stroke-width="1" vector-effect="non-scaling-stroke"/>
                <rect class="crop-handle crop-handle-ne" fill="#d4af37" stroke="white" stroke-width="1" vector-effect="non-scaling-stroke"/>
                <rect class="crop-handle crop-handle-sw" fill="#d4af37" stroke="white" stroke-width="1" vector-effect="non-scaling-stroke"/>
                <rect class="crop-handle crop-handle-se" fill="#d4af37" stroke="white" stroke-width="1" vector-effect="non-scaling-stroke"/>
            </svg>
        </div>
        <div id="crop-debug"></div>
    </div>
    """


def preview_image(state: dict) -> Image.Image | None:
    if not state or not state.get("precrop_paths"):
        return None
    idx = state["current_idx"]
    return render_preview(
        state["precrop_paths"][idx],
        state["transforms"][idx],
        state["crop_rects"][idx],
        state["kindle_w"],
        state["kindle_h"],
    )


def preview_html_content(state: dict) -> str:
    """Preview rendered as base64-embedded img inside our own container.
    We do this instead of gr.Image because Gradio 6's image frame kept
    collapsing to width=0 in this layout, hiding the actual preview."""
    img = preview_image(state)
    if img is None:
        return '<div id="kindle-preview-wrap"><em style="color:#aaa">No preview yet.</em></div>'
    uri = _img_to_data_uri(img)
    w, h = img.size
    # Show the actual rect Python used, so we can compare with the visual rect
    idx = state["current_idx"]
    r = state["crop_rects"][idx]
    sw, sh = render_transformed_size(state["precrop_paths"][idx], state["transforms"][idx])
    return f"""
    <div id="kindle-preview-wrap">
        <div id="kindle-preview-header">
            <div>Preview <b>{w}×{h}</b></div>
            <div style="font-size:11px;opacity:0.85">
                py rect from src ({sw}×{sh}): x=<b>{r['x']}</b> y=<b>{r['y']}</b> w=<b>{r['w']}</b> h=<b>{r['h']}</b>
            </div>
        </div>
        <img src="{uri}" width="{w}" height="{h}" alt="Kindle output preview"/>
    </div>
    """


def counter_md(state: dict) -> str:
    if not state or not state.get("precrop_paths"):
        return ""
    idx = state["current_idx"]
    total = len(state["precrop_paths"])
    name = Path(state["files"][idx]).name
    return f"**Image {idx + 1} of {total}** &nbsp;·&nbsp; {name}"


def _editor_bundle(state: dict) -> tuple:
    """Returns (state, cropper_html, preview_html, counter, nav_prev, nav_next,
    rotate_val, flip_h, flip_v, brightness, contrast, grayscale, auto_contrast)."""
    idx = state["current_idx"] if state.get("precrop_paths") else 0
    total = len(state.get("precrop_paths", []))
    tfx = state["transforms"][idx] if state.get("precrop_paths") else _default_tfx()
    return (
        state,
        cropper_html(state),
        preview_html_content(state),
        counter_md(state),
        gr.update(interactive=(idx > 0)),
        gr.update(interactive=(idx < total - 1)),
        tfx["rotate"],
        tfx["flip_h"],
        tfx["flip_v"],
        tfx["brightness"],
        tfx["contrast"],
        tfx["grayscale"],
        tfx["auto_contrast"],
    )


def _editor_bundle_by_key(key: str) -> tuple:
    """Same as _editor_bundle, but the first tuple element is the session KEY
    (a short string) rather than the full state dict. This lets gr.State stay
    small on the client so drag commits can't ship stale state back."""
    state = _get_state(key) or {"precrop_paths": []}
    idx = state["current_idx"] if state.get("precrop_paths") else 0
    total = len(state.get("precrop_paths", []))
    tfx = state["transforms"][idx] if state.get("precrop_paths") else _default_tfx()
    return (
        key,
        cropper_html(state),
        preview_html_content(state),
        counter_md(state),
        gr.update(interactive=(idx > 0)),
        gr.update(interactive=(idx < total - 1)),
        tfx["rotate"],
        tfx["flip_h"],
        tfx["flip_v"],
        tfx["brightness"],
        tfx["contrast"],
        tfx["grayscale"],
        tfx["auto_contrast"],
    )


# ── Step 1: Process ───────────────────────────────────────────────────────────

def run_process(files, model_key, remove_bg):
    if not files:
        raise gr.Error("Please upload at least one image.")
    if len(files) > MAX_FILES:
        raise gr.Error(f"Maximum {MAX_FILES} images per batch.")

    kw, kh = get_dimensions(model_key)
    out_dir = Path(tempfile.mkdtemp(prefix="kindle_"))
    precrop_paths, source_files, crop_rects, errors = [], [], [], []

    for file_path in files:
        src = Path(file_path)
        dest = out_dir / f"{src.stem}_precrop.png"
        try:
            _, size = make_precrop_image(src, dest, remove_bg)
            precrop_paths.append(str(dest))
            source_files.append(str(src))
            crop_rects.append(default_crop_rect(size[0], size[1], kw, kh))
        except Exception as exc:
            errors.append(f"{src.name}: {exc}")

    if not precrop_paths:
        raise gr.Error("All images failed:\n" + "\n".join(errors))

    state = {
        "model": model_key,
        "kindle_w": kw,
        "kindle_h": kh,
        "out_dir": str(out_dir),
        "files": source_files,
        "precrop_paths": precrop_paths,
        "transforms": [_default_tfx() for _ in precrop_paths],
        "crop_rects": crop_rects,
        "current_idx": 0,
        "final_paths": [],
    }
    key = _new_session_key()
    SESSION_STATES[key] = state

    status = f"Processed {len(precrop_paths)}/{len(files)} image(s). Drag/resize the gold rectangle to frame each image, then click Finalize."
    if errors:
        status += "\nErrors:\n" + "\n".join(errors)

    bundle = _editor_bundle_by_key(key)
    return (
        *bundle,
        gr.update(visible=False),   # upload_panel
        gr.update(visible=True),    # editor_panel
        gr.update(visible=False),   # results_panel
        status,
    )


# ── Editor events ─────────────────────────────────────────────────────────────

def on_crop_change(key, rect_json):
    """Called from the JS cropper via the hidden bridge button.

    Operates on server-side SESSION_STATES, and rejects any commit that does
    not match the currently-live render (wrong image index, or a render_id
    from a closure that belonged to a previous cropper mount). Returns the
    cropper HTML as well as the preview, so the gold rectangle the user sees
    is always redrawn from Python's authoritative state — the visible box and
    the produced output cannot drift apart."""
    state = _get_state(key)
    if not state or not state.get("precrop_paths"):
        empty = state or {"precrop_paths": []}
        return cropper_html(empty), preview_html_content(empty)

    def unchanged(reason):
        print(f"[on_crop_change] REJECTED ({reason}) payload={rect_json!r}", flush=True)
        return cropper_html(state), preview_html_content(state)

    try:
        rect = json.loads(rect_json) if isinstance(rect_json, str) else rect_json
    except Exception as e:
        return unchanged(f"json parse error: {e}")
    if not isinstance(rect, dict) or not all(k in rect for k in ("x", "y", "w", "h")):
        return unchanged("malformed rect")

    idx = state["current_idx"]

    # Identity checks — these are what stop a stale commit from corrupting state.
    payload_idx = rect.get("idx")
    if payload_idx is not None and int(payload_idx) != idx:
        return unchanged(f"idx mismatch: payload={payload_idx} current={idx}")
    payload_render = rect.get("render_id")
    active_render = state.get("active_render_id")
    if payload_render and active_render and payload_render != active_render:
        return unchanged(f"stale render_id: payload={payload_render} active={active_render}")

    prev = state["crop_rects"][idx]
    new_rect = {
        "x": int(rect["x"]),
        "y": int(rect["y"]),
        "w": int(rect["w"]),
        "h": int(rect["h"]),
    }
    print(f"[on_crop_change] ACCEPTED idx={idx}  prev={prev}  new={new_rect}", flush=True)
    state["crop_rects"][idx] = new_rect
    return cropper_html(state), preview_html_content(state)


def _tfx_update(key, updates: dict):
    """Apply transform changes to server-side state."""
    state = _get_state(key)
    if not state or not state.get("precrop_paths"):
        return _editor_bundle_by_key(key)
    idx = state["current_idx"]
    tfx = state["transforms"][idx]
    geometry_changed = (
        ("rotate" in updates and updates["rotate"] != tfx["rotate"]) or
        ("flip_h" in updates and updates["flip_h"] != tfx["flip_h"]) or
        ("flip_v" in updates and updates["flip_v"] != tfx["flip_v"])
    )
    tfx.update(updates)
    if geometry_changed:
        new_size = render_transformed_size(state["precrop_paths"][idx], tfx)
        state["crop_rects"][idx] = default_crop_rect(
            new_size[0], new_size[1], state["kindle_w"], state["kindle_h"]
        )
    return _editor_bundle_by_key(key)


def rotate_left(key):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"): return _editor_bundle_by_key(key)
    idx = state["current_idx"]
    return _tfx_update(key, {"rotate": (state["transforms"][idx]["rotate"] - 90) % 360})


def rotate_right(key):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"): return _editor_bundle_by_key(key)
    idx = state["current_idx"]
    return _tfx_update(key, {"rotate": (state["transforms"][idx]["rotate"] + 90) % 360})


def toggle_flip_h(key):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"): return _editor_bundle_by_key(key)
    idx = state["current_idx"]
    return _tfx_update(key, {"flip_h": not state["transforms"][idx]["flip_h"]})


def toggle_flip_v(key):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"): return _editor_bundle_by_key(key)
    idx = state["current_idx"]
    return _tfx_update(key, {"flip_v": not state["transforms"][idx]["flip_v"]})


def set_brightness(key, val):
    return _tfx_update(key, {"brightness": float(val)})


def set_contrast(key, val):
    return _tfx_update(key, {"contrast": float(val)})


def toggle_grayscale(key, val):
    return _tfx_update(key, {"grayscale": bool(val)})


def toggle_auto_contrast(key, val):
    return _tfx_update(key, {"auto_contrast": bool(val)})


def reset_edits(key):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"):
        return _editor_bundle_by_key(key)
    idx = state["current_idx"]
    state["transforms"][idx] = _default_tfx()
    new_size = render_transformed_size(state["precrop_paths"][idx], state["transforms"][idx])
    state["crop_rects"][idx] = default_crop_rect(
        new_size[0], new_size[1], state["kindle_w"], state["kindle_h"]
    )
    return _editor_bundle_by_key(key)


def navigate(key, direction):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"):
        return _editor_bundle_by_key(key)
    total = len(state["precrop_paths"])
    state["current_idx"] = max(0, min(total - 1, state["current_idx"] + direction))
    return _editor_bundle_by_key(key)


# ── Step 3: Finalize ──────────────────────────────────────────────────────────

def run_finalize(key, flatten, send_calibre, calibre_library, calibre_username, calibre_password):
    state = _get_state(key)
    if not state or not state.get("precrop_paths"):
        raise gr.Error("No images to finalize. Process images first.")

    out_dir = Path(state["out_dir"])
    kw, kh = state["kindle_w"], state["kindle_h"]
    final_paths, errors = [], []

    for i, precrop in enumerate(state["precrop_paths"]):
        stem = Path(state["files"][i]).stem
        out_png = out_dir / f"{stem}_kindle.png"
        try:
            render_final(
                precrop, out_png, state["transforms"][i], state["crop_rects"][i],
                kw, kh, flatten=bool(flatten),
            )
            final_paths.append(str(out_png))
        except Exception as exc:
            errors.append(f"{Path(precrop).name}: {exc}")

    if not final_paths:
        raise gr.Error("Finalization failed:\n" + "\n".join(errors))

    zip_path = out_dir / "kindle_images.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in final_paths:
            zf.write(p, Path(p).name)

    state["final_paths"] = final_paths
    status = f"Finalized {len(final_paths)}/{len(state['precrop_paths'])} image(s)."
    if errors:
        status += "\nErrors:\n" + "\n".join(errors)

    if send_calibre and final_paths:
        try:
            book_ids = add_to_library(
                [Path(p) for p in final_paths],
                calibre_library, calibre_username, calibre_password,
            )
            status += f"\nAdded {len(book_ids)} book(s) to Calibre."
        except Exception as exc:
            status += f"\nCalibre error: {exc}"

    return (
        key,
        final_paths,
        final_paths,
        str(zip_path),
        status,
        gr.update(visible=False),   # editor_panel
        gr.update(visible=True),    # results_panel
    )


# ── KOReader send ─────────────────────────────────────────────────────────────

def send_to_koreader(paths, port_str, password):
    if not paths:
        yield "No finalized images. Complete the crop step first.", gr.update()
        return
    port = int(port_str.strip()) if port_str and port_str.strip() else 9090
    local_ip = get_local_ip()
    total = len(paths)

    header = (
        f"Server listening on {local_ip}:{port}\n"
        "  1. Stop Calibre's wireless server (same port).\n"
        "  2. On KOReader tap Calibre > Connect.\n"
        f"  Manual fallback: server IP {local_ip}, port {port}.\n"
    )
    yield header + f"Waiting for connection (up to 120 s)... 0/{total} sent.", gr.update(interactive=False)

    # The transfer blocks, so run it on a thread and stream progress out through
    # a queue. Without this the UI sat silent for the whole batch.
    events: queue.Queue = queue.Queue()

    def _progress(sent, tot, name):
        events.put(("progress", sent, tot, name))

    result = {}

    def _worker():
        try:
            sent, failed = koreader_send_files(
                file_paths=[Path(p) for p in paths],
                tcp_port=port,
                password=password,
                timeout=120,
                on_progress=_progress,
            )
            result["ok"] = (sent, failed)
        except Exception as exc:
            result["err"] = exc
        finally:
            events.put(("done",))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    last = header + f"Waiting for connection... 0/{total} sent."
    while True:
        try:
            evt = events.get(timeout=1.0)
        except queue.Empty:
            continue
        if evt[0] == "done":
            break
        _, sent, tot, name = evt
        last = header + f"Transferring... {sent}/{tot} sent (latest: {name})"
        yield last, gr.update(interactive=False)

    t.join(timeout=5)

    if "err" in result:
        exc = result["err"]
        if isinstance(exc, TimeoutError):
            yield (
                f"Timed out — KOReader did not connect.\n"
                f"Manual: KOReader > Calibre plugin > IP {local_ip}, port {port}."
            ), gr.update(interactive=True)
        else:
            yield f"KOReader error: {exc}", gr.update(interactive=True)
        return

    sent, failed = result.get("ok", (0, []))
    msg = f"Sent {sent}/{total} file(s) to KOReader."
    if failed:
        msg += "\nSkipped:\n" + "\n".join(f"  - {f}" for f in failed)
    yield msg, gr.update(interactive=True)


# ── Results panel: delete ─────────────────────────────────────────────────────

def _on_gallery_select(evt: gr.SelectData):
    return evt.index, gr.update(interactive=True)


def delete_result(paths, idx):
    if idx is None or not paths or idx >= len(paths):
        return paths, paths, gr.update(), None, gr.update(interactive=False)
    try:
        Path(paths[idx]).unlink(missing_ok=True)
    except Exception:
        pass
    new_paths = [p for i, p in enumerate(paths) if i != idx]
    if new_paths:
        zip_path = Path(new_paths[0]).parent / "kindle_images.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in new_paths:
                zf.write(p, Path(p).name)
        new_zip = str(zip_path)
    else:
        new_zip = None
    return new_paths, new_paths, new_zip, None, gr.update(interactive=False)


# ── UI ────────────────────────────────────────────────────────────────────────

def build_ui() -> gr.Blocks:
    model_choices = [(f"{name.title()}  ({w}x{h})", name) for name, (w, h) in KINDLE_MODELS.items()]

    with gr.Blocks(title="Kindle Image Formatter — Forest") as demo:
        gr.Markdown(
            """
            # 🌲 Kindle Image Formatter
            Upload photos, frame each one with a **draggable, aspect-locked crop**,
            adjust for e-ink, and send them straight to your Kindle or KOReader.
            """
        )

        # crop_state holds only a short session KEY, not the full editor state.
        # This means rapid drag commits can't ship a stale dict back to the server.
        crop_state = gr.State("")
        processed_paths = gr.State([])
        gallery_selected_idx = gr.State(None)
        crop_rect_bridge = gr.Textbox(visible=False, elem_id="crop-rect-bridge")

        # ── Panel 1: Upload ─────────────────────────────────────────────────
        with gr.Column(visible=True, elem_classes="panel-card") as upload_panel:
            with gr.Row():
                with gr.Column(scale=2):
                    file_input = gr.File(
                        label=f"Drop images here (max {MAX_FILES})",
                        file_count="multiple",
                        file_types=["image"],
                        type="filepath",
                    )
                with gr.Column(scale=1):
                    model_dropdown = gr.Dropdown(
                        label="Kindle model",
                        choices=model_choices,
                        value=model_choices[1][1],
                    )
                    remove_bg_checkbox = gr.Checkbox(
                        label="Remove background (rembg / U2-Net)",
                        value=True,
                    )
                    flatten_checkbox = gr.Checkbox(
                        label="Flatten transparency to black",
                        value=False,
                        info="Off = saved PNG keeps its alpha channel (transparent).",
                    )
                    process_btn = gr.Button("Process images →", variant="primary", size="lg")

            with gr.Row():
                with gr.Accordion("Add to Calibre library", open=False):
                    gr.Markdown("_Needs Calibre's content server running._")
                    send_calibre_chk = gr.Checkbox(
                        label="Add to Calibre after finalizing",
                        value=False,
                    )
                    with gr.Group(visible=False) as calibre_options:
                        calibre_library_input = gr.Textbox(
                            label="Content server URL",
                            value="http://localhost:8080",
                        )
                        with gr.Row():
                            calibre_username_input = gr.Textbox(label="Username", placeholder="if auth required")
                            calibre_password_input = gr.Textbox(label="Password", placeholder="if auth required", type="password")
                    send_calibre_chk.change(
                        fn=lambda v: gr.update(visible=v),
                        inputs=send_calibre_chk,
                        outputs=calibre_options,
                    )
                with gr.Accordion("Direct-send to KOReader", open=False):
                    gr.Markdown("_Stop Calibre's wireless server first (same port). On KOReader: Calibre > Connect._")
                    with gr.Row():
                        koreader_port_input = gr.Textbox(label="Port", value="9090")
                        koreader_password_input = gr.Textbox(
                            label="Password",
                            placeholder="if set in KOReader",
                            type="password",
                        )

            upload_status = gr.Textbox(label="Status", lines=2, interactive=False)

        # ── Panel 2: Editor ─────────────────────────────────────────────────
        with gr.Column(visible=False, elem_classes="panel-card") as editor_panel:
            with gr.Row():
                with gr.Column(scale=1):
                    counter_md_el = gr.Markdown("**Image 1 of 1**")
                with gr.Column(scale=1):
                    with gr.Row(elem_classes="toolbar-row"):
                        prev_btn = gr.Button("← Prev", interactive=False, size="sm", elem_classes="toolbar-btn")
                        next_btn = gr.Button("Next →", interactive=False, size="sm", elem_classes="toolbar-btn")
                        back_btn = gr.Button("↩ Settings", size="sm", elem_classes="toolbar-btn")

            with gr.Row():
                with gr.Column(scale=3):
                    cropper_html_el = gr.HTML(
                        value='<div id="kindle-crop-wrap"><em style="color:#aaa">No image loaded.</em></div>',
                        label="Crop editor — drag the rectangle, drag its corners to resize",
                    )
                with gr.Column(scale=2):
                    preview_html = gr.HTML(
                        value='<div id="kindle-preview-wrap"><em style="color:#aaa">No preview yet.</em></div>',
                    )

            with gr.Group():
                gr.Markdown("### 🎨 Adjust")
                with gr.Row(elem_classes="toolbar-row"):
                    rot_left_btn = gr.Button("↺ 90°", size="sm", elem_classes="toolbar-btn")
                    rot_right_btn = gr.Button("↻ 90°", size="sm", elem_classes="toolbar-btn")
                    flip_h_btn = gr.Button("⇋ Flip H", size="sm", elem_classes="toolbar-btn")
                    flip_v_btn = gr.Button("⇵ Flip V", size="sm", elem_classes="toolbar-btn")
                    reset_btn = gr.Button("⟲ Reset edits", size="sm", elem_classes="toolbar-btn")

                with gr.Row():
                    brightness_slider = gr.Slider(0.3, 2.0, value=1.0, step=0.05, label="Brightness")
                    contrast_slider = gr.Slider(0.3, 2.0, value=1.0, step=0.05, label="Contrast")
                with gr.Row():
                    auto_contrast_chk = gr.Checkbox(label="Auto-contrast (e-ink pop)", value=False)
                    grayscale_chk = gr.Checkbox(label="Grayscale preview (Kindle looks like this)", value=False)

                # Hidden mirrors of the current tfx for external readability
                rotate_state = gr.State(0)
                flip_h_state = gr.State(False)
                flip_v_state = gr.State(False)

            finalize_btn = gr.Button("Finalize all images →", variant="primary", size="lg")

            # Hidden bridge button clicked by JS cropper on rect commit
            crop_bridge_btn = gr.Button("bridge", elem_id="crop-bridge-btn", visible=True)

        # ── Panel 3: Results ────────────────────────────────────────────────
        with gr.Column(visible=False, elem_classes="panel-card") as results_panel:
            gr.Markdown("### 🌿 Results")
            results_status = gr.Textbox(label="Status", lines=3, interactive=False)
            results_gallery = gr.Gallery(
                label="Processed images — click a thumbnail to select",
                columns=3,
                height="auto",
                object_fit="contain",
                elem_classes="gradio-gallery",
            )
            with gr.Row():
                delete_btn = gr.Button("🗑 Delete selected", variant="stop", interactive=False, size="sm")
                download_btn = gr.File(label="Download all as ZIP", interactive=False)
            with gr.Row():
                koreader_btn = gr.Button("📡 Send to KOReader", variant="secondary", size="lg")
                redo_btn = gr.Button("↩ Start over", size="lg")

        # ── Wiring ──────────────────────────────────────────────────────────

        editor_outputs = [
            crop_state,
            cropper_html_el, preview_html, counter_md_el,
            prev_btn, next_btn,
            rotate_state, flip_h_state, flip_v_state,
            brightness_slider, contrast_slider,
            grayscale_chk, auto_contrast_chk,
        ]

        process_btn.click(
            fn=run_process,
            inputs=[file_input, model_dropdown, remove_bg_checkbox],
            outputs=[
                *editor_outputs,
                upload_panel, editor_panel, results_panel,
                upload_status,
            ],
        )

        # Bridge: JS cropper writes rect to window.KINDLE_CROP_RESULT and clicks
        # the bridge button. We use js= to inject that value as the second input,
        # bypassing the placeholder empty textbox.
        crop_bridge_btn.click(
            fn=on_crop_change,
            inputs=[crop_state, crop_rect_bridge],
            outputs=[cropper_html_el, preview_html],
            js="(state, _) => [state, JSON.stringify(window.KINDLE_CROP_RESULT || {}) ]",
        )

        rot_left_btn.click(fn=rotate_left, inputs=crop_state, outputs=editor_outputs)
        rot_right_btn.click(fn=rotate_right, inputs=crop_state, outputs=editor_outputs)
        flip_h_btn.click(fn=toggle_flip_h, inputs=crop_state, outputs=editor_outputs)
        flip_v_btn.click(fn=toggle_flip_v, inputs=crop_state, outputs=editor_outputs)
        reset_btn.click(fn=reset_edits, inputs=crop_state, outputs=editor_outputs)

        brightness_slider.release(fn=set_brightness, inputs=[crop_state, brightness_slider], outputs=editor_outputs)
        contrast_slider.release(fn=set_contrast, inputs=[crop_state, contrast_slider], outputs=editor_outputs)
        grayscale_chk.change(fn=toggle_grayscale, inputs=[crop_state, grayscale_chk], outputs=editor_outputs)
        auto_contrast_chk.change(fn=toggle_auto_contrast, inputs=[crop_state, auto_contrast_chk], outputs=editor_outputs)

        prev_btn.click(fn=lambda s: navigate(s, -1), inputs=crop_state, outputs=editor_outputs)
        next_btn.click(fn=lambda s: navigate(s, +1), inputs=crop_state, outputs=editor_outputs)

        back_btn.click(
            fn=lambda: (gr.update(visible=True), gr.update(visible=False)),
            inputs=[],
            outputs=[upload_panel, editor_panel],
        )

        finalize_btn.click(
            fn=run_finalize,
            inputs=[
                crop_state, flatten_checkbox,
                send_calibre_chk, calibre_library_input,
                calibre_username_input, calibre_password_input,
            ],
            outputs=[
                crop_state, processed_paths,
                results_gallery, download_btn, results_status,
                editor_panel, results_panel,
            ],
        )

        koreader_btn.click(
            fn=send_to_koreader,
            inputs=[processed_paths, koreader_port_input, koreader_password_input],
            outputs=[results_status, koreader_btn],
        )

        results_gallery.select(
            fn=_on_gallery_select,
            inputs=[],
            outputs=[gallery_selected_idx, delete_btn],
        )

        delete_btn.click(
            fn=delete_result,
            inputs=[processed_paths, gallery_selected_idx],
            outputs=[processed_paths, results_gallery, download_btn, gallery_selected_idx, delete_btn],
        )

        redo_btn.click(
            fn=lambda: (gr.update(visible=True), gr.update(visible=False)),
            inputs=[],
            outputs=[upload_panel, results_panel],
        )

        gr.Markdown(
            """
            ---
            🌱 *Supported formats:* JPEG · PNG · WEBP · BMP · TIFF &nbsp;&nbsp;|&nbsp;&nbsp;
            *Output:* always PNG &nbsp;&nbsp;|&nbsp;&nbsp;
            *Background removal:* runs locally, offline
            """
        )

    return demo


if __name__ == "__main__":
    app = build_ui()
    app.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        theme=FOREST_THEME,
        css=FOREST_CSS,
        head=CROPPER_JS,
    )
