"""Role instructions: one source for every host (DN006 §6.2).

``plugin/roles/worker.md`` in the product repository is the only source of what a worker is
told about its work. ``tsw host render`` (and ``tsw init`` for a new data directory) copies it
to ``roles/worker.md`` in the data directory, a file only tsw writes; every brief tsw writes
(prestart, dispatch, requirement change) starts with that copy. A missing copy, or one that
differs from the source the running tsw ships with, refuses the brief: the product was updated
without ``tsw host render``, and a worker would otherwise get rules the running tsw no longer
follows.
"""

from __future__ import annotations

from pathlib import Path

from tsw.errors import rejected
from tsw.storage.fs import atomic_write

# The product's own copy of the role instructions; not in the Python package, like ``plugin/``
# (an editable install from a checkout, DN006 §9.1).
SOURCE_DIR = Path(__file__).resolve().parent.parent / "plugin" / "roles"
# Where tsw puts them in the data directory (DN006 §9.2): written only by tsw.
RENDERED_DIR = "roles"
WORKER = "worker.md"
RENDER_HINT = "运行 tsw host render（产品仓库更新后都要运行）"


def worker_source() -> Path:
    return SOURCE_DIR / WORKER


def worker_copy(root: Path) -> Path:
    return root / RENDERED_DIR / WORKER


def render_roles(root: Path) -> list[Path]:
    """Copy the worker instructions into the data directory; returns what was written."""
    copy = worker_copy(root)
    atomic_write(copy, worker_source().read_bytes())
    return [copy]


def ensure_roles(root: Path) -> list[Path]:
    """Write the copy when the data directory has none and the source is at hand (``tsw init``)."""
    if worker_copy(root).is_file() or not worker_source().is_file():
        return []
    return render_roles(root)


def worker_instructions(root: Path) -> str:
    """The worker instructions every brief starts with: the data directory's copy.

    Refused when the copy is missing, or differs from the source next to the running tsw (when
    that is at hand: an install without ``plugin/`` has only the copy).
    """
    copy = worker_copy(root)
    try:
        data = copy.read_bytes()
    except FileNotFoundError:
        raise rejected(
            f"数据目录中没有 worker 说明 {RENDERED_DIR}/{WORKER}，不能生成任务书：{RENDER_HINT}",
            reason="worker_instructions_missing",
            path=str(copy),
        ) from None
    source = worker_source()
    if source.is_file() and source.read_bytes() != data:
        raise rejected(
            f"数据目录中的 worker 说明 {RENDERED_DIR}/{WORKER} 与产品仓库的 plugin/roles/{WORKER} "
            f"不一致，不能生成任务书：{RENDER_HINT}",
            reason="worker_instructions_stale",
            path=str(copy),
            source=str(source),
        )
    return data.decode("utf-8").replace("\r\n", "\n")
