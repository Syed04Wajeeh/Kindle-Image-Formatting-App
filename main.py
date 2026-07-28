#!/usr/bin/env python3
"""
Kindle Image Formatter
======================
Crop an image to the correct Kindle screen size and optionally remove its
background, then save as a transparent PNG.

Usage
-----
  python main.py input.jpg                          # Paperwhite, bg removed, fill crop
  python main.py input.jpg -o out.png               # custom output path
  python main.py input.jpg --model oasis            # different Kindle model
  python main.py input.jpg --no-remove-bg           # keep original background
  python main.py input.jpg --crop-mode fit          # letterbox instead of crop
  python main.py input.jpg --bg-color 0 0 0 255     # black background in fit mode
  python main.py --list-models                      # show all supported models
"""

import argparse
import sys
from pathlib import Path

from kindle_processor import list_models, process_image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kindle-formatter",
        description="Format an image as a Kindle screensaver/background PNG.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "input",
        nargs="?",
        help="Path to the source image (JPEG, PNG, WEBP, …).",
    )
    parser.add_argument(
        "-o", "--output",
        help="Output PNG path. Defaults to <input>_kindle.png in the same directory.",
    )
    parser.add_argument(
        "--model",
        default="paperwhite",
        metavar="MODEL",
        help=f"Kindle model. One of: {', '.join(list_models())}. Default: paperwhite",
    )
    parser.add_argument(
        "--no-remove-bg",
        dest="remove_bg",
        action="store_false",
        default=True,
        help="Skip background removal and keep the original background.",
    )
    parser.add_argument(
        "--crop-mode",
        choices=["fill", "fit"],
        default="fill",
        help=(
            "'fill' (default): scale and center-crop to exactly fill the screen. "
            "'fit': scale to fit without cropping, padding the remainder."
        ),
    )
    parser.add_argument(
        "--bg-color",
        nargs=4,
        type=int,
        default=[0, 0, 0, 0],
        metavar=("R", "G", "B", "A"),
        help="RGBA background color used in 'fit' mode (default: 0 0 0 0 = transparent).",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List all supported Kindle models and exit.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_models:
        from kindle_processor import KINDLE_MODELS
        print("Supported Kindle models:")
        for name, (w, h) in KINDLE_MODELS.items():
            print(f"  {name:<14} {w} × {h} px")
        return 0

    if not args.input:
        print("Error: please provide an input image path.", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        return 1

    input_path = Path(args.input)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(input_path.stem + "_kindle.png")

    print(f"Processing: {input_path}")
    print(f"  Model   : {args.model}")
    print(f"  Remove BG: {args.remove_bg}")
    print(f"  Crop mode: {args.crop_mode}")

    try:
        process_image(
            input_path=input_path,
            output_path=output_path,
            model=args.model,
            remove_bg=args.remove_bg,
            crop_mode=args.crop_mode,
            bg_color=tuple(args.bg_color),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
