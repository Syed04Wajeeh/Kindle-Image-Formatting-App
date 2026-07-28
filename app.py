"""
Kindle Image Formatter — Gradio web UI
Run:  python app.py
Then open http://localhost:7860 in your browser.
"""

import shutil
import tempfile
import zipfile
from pathlib import Path

import gradio as gr

from kindle_processor import KINDLE_MODELS, process_image

MAX_FILES = 10


def process_batch(
    files: list,
    model: str,
    remove_bg: bool,
    crop_mode: str,
    bg_color_hex: str,
) -> tuple[list[str], str]:
    """Process up to MAX_FILES images and return (gallery_paths, zip_path)."""
    if not files:
        raise gr.Error("Please upload at least one image.")
    if len(files) > MAX_FILES:
        raise gr.Error(f"Maximum {MAX_FILES} images per batch.")

    # Parse hex colour → RGBA
    hex_col = bg_color_hex.lstrip("#")
    try:
        r, g, b = int(hex_col[0:2], 16), int(hex_col[2:4], 16), int(hex_col[4:6], 16)
    except (ValueError, IndexError):
        r, g, b = 0, 0, 0
    # In fill mode bg_color is unused; in fit mode alpha 255 makes it opaque.
    bg_rgba = (r, g, b, 0 if crop_mode == "fill" else 255)

    out_dir = Path(tempfile.mkdtemp(prefix="kindle_"))
    preview_paths: list[str] = []
    errors: list[str] = []

    for i, file_path in enumerate(files, 1):
        src = Path(file_path)
        out_png = out_dir / f"{src.stem}_kindle.png"
        try:
            process_image(
                input_path=src,
                output_path=out_png,
                model=model,
                remove_bg=remove_bg,
                crop_mode=crop_mode,
                bg_color=bg_rgba,
            )
            preview_paths.append(str(out_png))
        except Exception as exc:
            errors.append(f"{src.name}: {exc}")

    if not preview_paths:
        raise gr.Error("All images failed to process:\n" + "\n".join(errors))

    # Build a zip so the user can download everything in one click
    zip_path = out_dir / "kindle_images.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in preview_paths:
            zf.write(p, Path(p).name)

    status = f"Processed {len(preview_paths)}/{len(files)} image(s)."
    if errors:
        status += "\nErrors:\n" + "\n".join(errors)

    return preview_paths, str(zip_path), status


def build_ui() -> gr.Blocks:
    model_choices = list(KINDLE_MODELS.keys())
    model_labels = [
        f"{name}  ({w}×{h} px)" for name, (w, h) in KINDLE_MODELS.items()
    ]
    model_map = dict(zip(model_labels, model_choices))

    with gr.Blocks(title="Kindle Image Formatter") as demo:
        gr.Markdown(
            """
            # 📚 Kindle Image Formatter
            Upload up to **10 images**, choose your Kindle model, and get back
            perfectly-sized PNGs with the background removed — ready to use as
            Kindle screensavers or covers.

            > **Free & offline** — background removal runs locally using the
            > U²-Net model. No API keys, no usage costs.
            """
        )

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
                    choices=model_labels,
                    value=model_labels[1],  # Paperwhite default
                )

                remove_bg_checkbox = gr.Checkbox(
                    label="Remove background",
                    value=True,
                )

                crop_mode_radio = gr.Radio(
                    label="Crop mode",
                    choices=["fill", "fit"],
                    value="fill",
                    info=(
                        "fill — scale & center-crop to fill the screen exactly.  "
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

                run_btn = gr.Button("Process images", variant="primary", size="lg")

            with gr.Column(scale=2):
                gr.Markdown("### Results")
                status_box = gr.Textbox(
                    label="Status",
                    lines=2,
                    interactive=False,
                )
                gallery = gr.Gallery(
                    label="Processed images (preview)",
                    columns=3,
                    height="auto",
                    object_fit="contain",
                )
                download_btn = gr.File(
                    label="Download all as ZIP",
                    interactive=False,
                )

        run_btn.click(
            fn=lambda files, mdl_label, rm_bg, mode, col: process_batch(
                files,
                model_map[mdl_label],
                rm_bg,
                mode,
                col,
            ),
            inputs=[
                file_input,
                model_dropdown,
                remove_bg_checkbox,
                crop_mode_radio,
                bg_color_picker,
            ],
            outputs=[gallery, download_btn, status_box],
        )

        gr.Markdown(
            """
            ---
            **Supported formats:** JPEG · PNG · WEBP · BMP · TIFF
            **Output:** always PNG (preserves transparency after bg removal)
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
