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

import tempfile
import zipfile
from pathlib import Path

import gradio as gr

from kindle_processor import (
    KINDLE_MODELS,
    get_dimensions,
    make_precrop_image,
    make_final_image,
    make_overview_image,
    make_preview_image,
)
from calibre_sender import add_to_library
from koreader_sender import send_files as koreader_send_files, get_local_ip

MAX_FILES = 10


# ── Crop step helpers ─────────────────────────────────────────────────────────

def _build_editor_outputs(state: dict) -> tuple:
    """Render the crop editor for state["current_idx"]. Returns 7 values."""
    idx = state["current_idx"]
    total = len(state["precrop_paths"])
    path = state["precrop_paths"][idx]
    x_pct = state["crop_settings"][idx]["x_pct"]
    y_pct = state["crop_settings"][idx]["y_pct"]
    kw, kh = state["kindle_w"], state["kindle_h"]
    mode, bg = state["crop_mode"], state["bg_color"]
    sw, sh = state["precrop_sizes"][idx]

    x_free = (sw > kw) if mode == "fill" else (sw < kw)
    y_free = (sh > kh) if mode == "fill" else (sh < kh)

    overview = make_overview_image(path, kw, kh, mode, x_pct, y_pct, bg)
    preview = make_preview_image(path, kw, kh, mode, x_pct, y_pct, bg)
    counter = f"**Image {idx + 1} of {total}** — {Path(state['files'][idx]).name}"

    return (
        overview,
        preview,
        counter,
        gr.update(value=round(x_pct * 100), interactive=x_free),
        gr.update(value=round(y_pct * 100), interactive=y_free),
        gr.update(interactive=(idx > 0)),
        gr.update(interactive=(idx < total - 1)),
    )


# ── Step 1: Process (bg removal + scale, no crop yet) ─────────────────────────

def run_step1(files, model_key, remove_bg, crop_mode, bg_color_hex):
    if not files:
        raise gr.Error("Please upload at least one image.")
    if len(files) > MAX_FILES:
        raise gr.Error(f"Maximum {MAX_FILES} images per batch.")

    kw, kh = get_dimensions(model_key)

    hex_col = bg_color_hex.lstrip("#")
    try:
        r, g, b = int(hex_col[0:2], 16), int(hex_col[2:4], 16), int(hex_col[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 0, 0, 0
    bg_color = (r, g, b, 255)

    out_dir = Path(tempfile.mkdtemp(prefix="kindle_"))
    precrop_paths, precrop_sizes, source_files, errors = [], [], [], []

    for file_path in files:
        src = Path(file_path)
        dest = out_dir / f"{src.stem}_precrop.png"
        try:
            _, size = make_precrop_image(src, dest, model_key, remove_bg, crop_mode)
            precrop_paths.append(str(dest))
            precrop_sizes.append(size)
            source_files.append(str(src))
        except Exception as exc:
            errors.append(f"{src.name}: {exc}")

    if not precrop_paths:
        raise gr.Error("All images failed:\n" + "\n".join(errors))

    state = {
        "model": model_key,
        "crop_mode": crop_mode,
        "bg_color": bg_color,
        "kindle_w": kw,
        "kindle_h": kh,
        "out_dir": str(out_dir),
        "files": source_files,
        "precrop_paths": precrop_paths,
        "precrop_sizes": precrop_sizes,
        "crop_settings": [{"x_pct": 0.5, "y_pct": 0.5} for _ in precrop_paths],
        "current_idx": 0,
        "final_paths": [],
    }

    overview, preview, counter, x_upd, y_upd, prev_upd, next_upd = _build_editor_outputs(state)

    status = f"Scaled {len(precrop_paths)}/{len(files)} image(s). "
    status += "Adjust crop position, then click Finalize."
    if errors:
        status += "\nErrors:\n" + "\n".join(errors)

    return (
        state,
        overview, preview, counter,
        x_upd, y_upd, prev_upd, next_upd,
        gr.update(visible=False),   # upload_panel
        gr.update(visible=True),    # crop_panel
        gr.update(visible=False),   # results_panel
        status,
    )


# ── Step 2a: Slider changes ───────────────────────────────────────────────────

def _update_previews(state, x_val, y_val):
    if not state or not state.get("precrop_paths"):
        return state, None, None
    idx = state["current_idx"]
    state["crop_settings"][idx]["x_pct"] = x_val / 100.0
    state["crop_settings"][idx]["y_pct"] = y_val / 100.0
    kw, kh = state["kindle_w"], state["kindle_h"]
    mode, bg = state["crop_mode"], state["bg_color"]
    path = state["precrop_paths"][idx]
    overview = make_overview_image(path, kw, kh, mode, x_val / 100.0, y_val / 100.0, bg)
    preview = make_preview_image(path, kw, kh, mode, x_val / 100.0, y_val / 100.0, bg)
    return state, overview, preview


def on_x_change(state, x_val, y_val):
    return _update_previews(state, x_val, y_val)


def on_y_change(state, y_val, x_val):
    return _update_previews(state, x_val, y_val)


# ── Step 2b: Navigation ───────────────────────────────────────────────────────

def _navigate(state, x_val, y_val, direction):
    if not state or not state.get("precrop_paths"):
        return (state, None, None, "", gr.update(), gr.update(), gr.update(), gr.update())
    idx = state["current_idx"]
    state["crop_settings"][idx]["x_pct"] = x_val / 100.0
    state["crop_settings"][idx]["y_pct"] = y_val / 100.0
    new_idx = max(0, min(len(state["precrop_paths"]) - 1, idx + direction))
    state["current_idx"] = new_idx
    overview, preview, counter, x_upd, y_upd, prev_upd, next_upd = _build_editor_outputs(state)
    return state, overview, preview, counter, x_upd, y_upd, prev_upd, next_upd


def navigate_prev(state, x_val, y_val):
    return _navigate(state, x_val, y_val, -1)


def navigate_next(state, x_val, y_val):
    return _navigate(state, x_val, y_val, +1)


# ── Step 3: Finalize all crops ────────────────────────────────────────────────

def run_finalize(state, send_calibre, calibre_library, calibre_username, calibre_password):
    if not state or not state.get("precrop_paths"):
        raise gr.Error("No images to finalize. Run 'Process images' first.")

    out_dir = Path(state["out_dir"])
    kw, kh = state["kindle_w"], state["kindle_h"]
    bg, mode = state["bg_color"], state["crop_mode"]
    final_paths, errors = [], []

    for i, precrop_path in enumerate(state["precrop_paths"]):
        src_stem = Path(state["files"][i]).stem
        out_png = out_dir / f"{src_stem}_kindle.png"
        x_pct = state["crop_settings"][i]["x_pct"]
        y_pct = state["crop_settings"][i]["y_pct"]
        try:
            make_final_image(precrop_path, out_png, kw, kh, mode, x_pct, y_pct, bg)
            final_paths.append(str(out_png))
        except Exception as exc:
            errors.append(f"{Path(precrop_path).name}: {exc}")

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
        state,
        final_paths,            # processed_paths state
        final_paths,            # gallery
        str(zip_path),          # download file
        status,
        gr.update(visible=False),   # crop_panel
        gr.update(visible=True),    # results_panel
    )


# ── KOReader send (generator for streaming status) ────────────────────────────

def send_to_koreader(paths, port_str, password):
    if not paths:
        yield "No finalized images. Complete the crop step first.", gr.update()
        return

    port = int(port_str.strip()) if port_str and port_str.strip() else 9090
    local_ip = get_local_ip()

    yield (
        f"Server listening on {local_ip}:{port}\n"
        "Steps:\n"
        "  1. Make sure Calibre's wireless server is stopped (same port).\n"
        "  2. On KOReader tap  Calibre > Connect.\n"
        f"  If KOReader says 'can't discover': open KOReader > Calibre plugin settings\n"
        f"  and set server IP to  {local_ip}  port  {port}  then tap Connect again.\n"
        "Waiting for connection (up to 120 s)..."
    ), gr.update(interactive=False)

    try:
        koreader_send_files(
            file_paths=[Path(p) for p in paths],
            tcp_port=port,
            password=password,
            timeout=120,
        )
        yield f"Sent {len(paths)} file(s) to KOReader successfully!", gr.update(interactive=True)
    except TimeoutError:
        yield (
            f"Timed out — KOReader did not connect within 120 s.\n"
            f"Manual connection: KOReader > Calibre plugin settings > "
            f"server IP = {local_ip}, port = {port}"
        ), gr.update(interactive=True)
    except Exception as exc:
        yield f"KOReader error: {exc}", gr.update(interactive=True)


# ── UI ────────────────────────────────────────────────────────────────────────

def build_ui() -> gr.Blocks:
    model_choices = [(f"{name}  ({w}x{h})", name) for name, (w, h) in KINDLE_MODELS.items()]

    with gr.Blocks(title="Kindle Image Formatter") as demo:
        gr.Markdown(
            """
            # Kindle Image Formatter
            Upload up to **10 images**, choose your Kindle model, and get back
            perfectly-sized PNGs — with manual crop control for each image.

            > **Free & offline** — background removal runs locally via U2-Net.
            """
        )

        crop_state = gr.State({})
        processed_paths = gr.State([])

        # ── Panel 1: Upload & settings ──────────────────────────────────────
        with gr.Column(visible=True) as upload_panel:
            with gr.Row():
                with gr.Column(scale=1):
                    gr.Markdown("### Settings")

                    file_input = gr.File(
                        label=f"Upload images (max {MAX_FILES})",
                        file_count="multiple",
                        file_types=["image"],
                        type="filepath",
                    )
                    model_dropdown = gr.Dropdown(
                        label="Kindle model",
                        choices=model_choices,
                        value=model_choices[1][1],
                    )
                    remove_bg_checkbox = gr.Checkbox(label="Remove background", value=True)
                    crop_mode_radio = gr.Radio(
                        label="Crop mode",
                        choices=["fill", "fit"],
                        value="fill",
                        info=(
                            "fill — scale to fill screen, crop excess.  "
                            "fit — scale to fit, pad the remainder."
                        ),
                    )
                    bg_color_picker = gr.ColorPicker(
                        label="Padding colour (fit mode only)",
                        value="#000000",
                        visible=False,
                    )
                    crop_mode_radio.change(
                        fn=lambda m: gr.update(visible=(m == "fit")),
                        inputs=crop_mode_radio,
                        outputs=bg_color_picker,
                    )

                    with gr.Accordion("Add to Calibre library", open=False):
                        gr.Markdown(
                            "_Requires Calibre content server: Preferences > Sharing > "
                            "Sharing over the network > Start the Content Server._"
                        )
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
                                calibre_username_input = gr.Textbox(
                                    label="Username",
                                    placeholder="leave blank if no auth",
                                )
                                calibre_password_input = gr.Textbox(
                                    label="Password",
                                    placeholder="leave blank if no auth",
                                    type="password",
                                )
                        send_calibre_chk.change(
                            fn=lambda v: gr.update(visible=v),
                            inputs=send_calibre_chk,
                            outputs=calibre_options,
                        )

                    with gr.Accordion("Send directly to KOReader", open=False):
                        gr.Markdown(
                            "_Stop Calibre's wireless server first (same port). "
                            "Then on KOReader tap **Calibre > Connect**._"
                        )
                        with gr.Row():
                            koreader_port_input = gr.Textbox(label="Port", value="9090")
                            koreader_password_input = gr.Textbox(
                                label="Password (if set in KOReader)",
                                placeholder="leave blank if none",
                                type="password",
                            )

                    process_btn = gr.Button("Process images", variant="primary", size="lg")

                with gr.Column(scale=2):
                    status_box = gr.Textbox(label="Status", lines=4, interactive=False)

        # ── Panel 2: Crop editor ────────────────────────────────────────────
        with gr.Column(visible=False) as crop_panel:
            gr.Markdown("### Adjust crop position")
            crop_counter = gr.Markdown("**Image 1 of 1**")

            with gr.Row():
                crop_overview = gr.Image(
                    label="Full image — gold box shows crop window",
                    interactive=False,
                    height=420,
                )
                crop_preview = gr.Image(
                    label="Preview — exact Kindle output",
                    interactive=False,
                    height=420,
                )

            x_slider = gr.Slider(
                minimum=0, maximum=100, value=50, step=1,
                label="Horizontal position (%)",
            )
            y_slider = gr.Slider(
                minimum=0, maximum=100, value=50, step=1,
                label="Vertical position (%)",
            )

            with gr.Row():
                prev_btn = gr.Button("< Previous", interactive=False, size="sm")
                next_btn = gr.Button("Next >", interactive=False, size="sm")

            with gr.Row():
                back_btn = gr.Button("Back to settings", size="sm")
                finalize_btn = gr.Button(
                    "Finalize all images", variant="primary", size="lg"
                )

        # ── Panel 3: Results ────────────────────────────────────────────────
        with gr.Column(visible=False) as results_panel:
            gr.Markdown("### Results")
            results_status = gr.Textbox(label="Status", lines=4, interactive=False)
            results_gallery = gr.Gallery(
                label="Processed images",
                columns=3,
                height="auto",
                object_fit="contain",
            )
            download_btn = gr.File(label="Download all as ZIP", interactive=False)

            with gr.Row():
                koreader_btn = gr.Button(
                    "Send to KOReader", variant="secondary", size="lg"
                )
                redo_btn = gr.Button("Start over", size="lg")

        # ── Event wiring ────────────────────────────────────────────────────

        process_btn.click(
            fn=run_step1,
            inputs=[
                file_input, model_dropdown, remove_bg_checkbox,
                crop_mode_radio, bg_color_picker,
            ],
            outputs=[
                crop_state,
                crop_overview, crop_preview, crop_counter,
                x_slider, y_slider, prev_btn, next_btn,
                upload_panel, crop_panel, results_panel,
                status_box,
            ],
        )

        # Use .release() so previews only regenerate when slider drag ends
        x_slider.release(
            fn=on_x_change,
            inputs=[crop_state, x_slider, y_slider],
            outputs=[crop_state, crop_overview, crop_preview],
        )
        y_slider.release(
            fn=on_y_change,
            inputs=[crop_state, y_slider, x_slider],
            outputs=[crop_state, crop_overview, crop_preview],
        )

        prev_btn.click(
            fn=navigate_prev,
            inputs=[crop_state, x_slider, y_slider],
            outputs=[
                crop_state, crop_overview, crop_preview, crop_counter,
                x_slider, y_slider, prev_btn, next_btn,
            ],
        )
        next_btn.click(
            fn=navigate_next,
            inputs=[crop_state, x_slider, y_slider],
            outputs=[
                crop_state, crop_overview, crop_preview, crop_counter,
                x_slider, y_slider, prev_btn, next_btn,
            ],
        )

        back_btn.click(
            fn=lambda: (gr.update(visible=True), gr.update(visible=False)),
            inputs=[],
            outputs=[upload_panel, crop_panel],
        )

        finalize_btn.click(
            fn=run_finalize,
            inputs=[
                crop_state,
                send_calibre_chk, calibre_library_input,
                calibre_username_input, calibre_password_input,
            ],
            outputs=[
                crop_state, processed_paths,
                results_gallery, download_btn, results_status,
                crop_panel, results_panel,
            ],
        )

        koreader_btn.click(
            fn=send_to_koreader,
            inputs=[processed_paths, koreader_port_input, koreader_password_input],
            outputs=[results_status, koreader_btn],
        )

        redo_btn.click(
            fn=lambda: (gr.update(visible=True), gr.update(visible=False)),
            inputs=[],
            outputs=[upload_panel, results_panel],
        )

        gr.Markdown(
            """
            ---
            **Supported formats:** JPEG · PNG · WEBP · BMP · TIFF
            **Output:** always PNG
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
        theme=gr.themes.Soft(),
    )
