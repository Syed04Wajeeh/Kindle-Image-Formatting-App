"""
Calibre integration — add processed images to the library and optionally
push them to a connected KOReader via Calibre's wireless device protocol.
"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

_CALIBREDB_CANDIDATES = [
    r"C:\Program Files\Calibre2\calibredb.exe",
    r"C:\Program Files (x86)\Calibre2\calibredb.exe",
    "/Applications/calibre.app/Contents/MacOS/calibredb",
    "/usr/bin/calibredb",
    "/usr/local/bin/calibredb",
]


def find_calibredb() -> str | None:
    found = shutil.which("calibredb")
    if found:
        return found
    for p in _CALIBREDB_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _run(args: list[str]) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        if "Forbidden" in msg or "403" in msg:
            raise RuntimeError(
                "Calibre content server rejected the request (403 Forbidden). "
                "Write operations require a user account. In Calibre go to: "
                "Preferences → Sharing → Sharing over the network → "
                "Manage user accounts, create a user with 'Allow making changes', "
                "then enter those credentials in the app."
            )
        raise RuntimeError(msg or f"calibredb exited with code {result.returncode}")
    return result.stdout


DEFAULT_CALIBRE_URL = "http://localhost:8080"


def _build_args(
    library: str,
    username: str = "",
    password: str = "",
) -> list[str]:
    """
    Build the --with-library (and optional credential) flags for calibredb.
    calibredb must connect via the content server URL when Calibre GUI is open.
    """
    args = ["--with-library", library.strip() or DEFAULT_CALIBRE_URL]
    if username.strip():
        args += ["--username", username.strip()]
    if password.strip():
        args += ["--password", password.strip()]
    return args


def add_to_library(
    image_paths: list[Path],
    library_path: str = "",
    username: str = "",
    password: str = "",
) -> list[int]:
    """Add images to Calibre library via the content server. Returns book IDs."""
    calibredb = find_calibredb()
    if not calibredb:
        raise RuntimeError(
            "calibredb not found. Make sure Calibre is installed and on your PATH."
        )

    lib_args = _build_args(library_path, username, password)
    book_ids: list[int] = []
    for img in image_paths:
        cmd = [calibredb, "add"] + lib_args + [str(img)]
        stdout = _run(cmd)
        for line in stdout.splitlines():
            if "Added book ids:" in line:
                for token in line.split(":", 1)[1].split(","):
                    token = token.strip()
                    if token.isdigit():
                        book_ids.append(int(token))
    return book_ids


