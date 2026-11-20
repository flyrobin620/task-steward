"""Derived files generated from the projection, kept recoverable.

Each derived view records in the projection the sequence it was last generated for. Every write
command (including a duplicate retry) and every rebuild regenerates the views that are behind,
so an interruption after an append never leaves a derived file permanently stale.
"""

from __future__ import annotations

from collections.abc import Callable

from tsw.commands import CommitContext
from tsw.confedit import apply_pending
from tsw.host.status import write_status
from tsw.projection import get_meta, projected_tail, set_meta
from tsw.tasks import write_index

DerivedView = Callable[[CommitContext], None]

DERIVED_VIEWS: tuple[tuple[str, DerivedView], ...] = (
    ("todo_index", write_index),
    ("status_page", write_status),
    # Not generated from the projection: finishes a recorded configuration change (DN003 §7).
    ("config_file", apply_pending),
)


def refresh(ctx: CommitContext) -> list[str]:
    """Regenerate every view that is behind the projection. Returns the regenerated names."""
    tail = projected_tail(ctx.conn)
    refreshed: list[str] = []
    for name, generate in DERIVED_VIEWS:
        key = f"view:{name}"
        if get_meta(ctx.conn, key) == str(tail.sequence):
            continue
        generate(ctx)
        set_meta(ctx.conn, key, str(tail.sequence))
        refreshed.append(name)
    return refreshed
