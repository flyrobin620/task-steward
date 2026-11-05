"""Repository helpers shared by the checks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    ".local",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}


def tracked_files(root: Path) -> list[Path]:
    """Files git tracks or would track (untracked but not ignored), else a filtered walk."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and not SKIP_DIRS.intersection(path.relative_to(root).parts)
        )
    names = [name for name in proc.stdout.decode("utf-8").split("\0") if name]
    return sorted(root / name for name in names if (root / name).is_file())


def utf8_stdio() -> None:
    """Print UTF-8 on any console (Windows consoles default to a legacy code page)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
