"""Write commands and their results (DN001 §7.2: ``command_id`` and ``expected_version``)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tsw.events import Event, EventDraft
from tsw.home import Home
from tsw.storage.base import LedgerTail, StorageBackend


@dataclass
class CommitContext:
    """What a command sees while it holds the commit lock: the caught-up projection."""

    conn: sqlite3.Connection
    storage: StorageBackend
    home: Home
    tail: LedgerTail


@dataclass
class Command:
    """A write command. ``build`` checks guards and returns the events to append.

    ``input`` is the canonical command input; its digest makes ``command_id`` idempotent.
    Derived files (such as ``todo/TODO.md``) are not written here but by the derived views
    in :mod:`tsw.views`, which the ledger brings up to date after every commit and retry.
    """

    actor: Mapping[str, Any]
    input: Mapping[str, Any]
    build: Callable[[CommitContext], Sequence[EventDraft]]
    command_id: str | None = None


@dataclass
class CommitResult:
    events: list[Event] = field(default_factory=lambda: [])
    duplicate: bool = False

    @property
    def last_sequence(self) -> int | None:
        return self.events[-1].sequence if self.events else None
