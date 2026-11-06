"""File helpers for imports: removing trees that hold read-only files (git objects, specs)."""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path
from typing import Any


def make_writable(path: Path) -> None:
    path.chmod(stat.S_IWRITE | stat.S_IREAD)


def _writable_then_retry(func: Any, path: str, _exc: Any) -> None:
    make_writable(Path(path))
    func(path)


def remove_tree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_writable_then_retry)
    else:  # pragma: no cover - Python 3.10 / 3.11
        shutil.rmtree(path, onerror=_writable_then_retry)
