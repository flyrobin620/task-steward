"""The storage backend interface (DN006 §9.3).

The design lists ``append``, ``read``, ``put_artifact``, ``lock`` and ``snapshot``. Backends also
expose the write admission and quiescing used by every tsw write entry point (DN006 §9.4) and a
few read helpers. Failures raise :class:`tsw.errors.TswError` with a unified error code.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from tsw.events import GENESIS, Event

PLUGIN_API = 1


@dataclass(frozen=True)
class LedgerTail:
    sequence: int
    checksum: str

    @staticmethod
    def empty() -> LedgerTail:
        return LedgerTail(0, GENESIS)


@dataclass(frozen=True)
class Lease:
    name: str
    holder: Mapping[str, Any]


BackupManifest = dict[str, Any]


class StorageBackend(Protocol):
    def capabilities(self) -> Mapping[str, Any]: ...

    def tail(self) -> LedgerTail: ...

    def append(self, events: Sequence[Event], *, expected_sequence: int) -> int:
        """Atomically append ``events``; ``expected_sequence`` must equal the current tail."""
        ...

    def read(self, since: int = 0) -> Iterator[Event]:
        """Yield events with ``sequence > since`` in order."""
        ...

    def put_artifact(self, rel: str, data: bytes) -> str:
        """Atomically write a file under a tsw-owned directory; returns its SHA-256."""
        ...

    def read_artifact(self, rel: str) -> bytes: ...

    def lock(self, name: str, ttl: float) -> AbstractContextManager[Lease]: ...

    def writer(self) -> AbstractContextManager[None]:
        """Write admission: shared write gate, then the quiesce check (DN006 §9.4)."""
        ...

    def quiesce(self) -> BackupManifest: ...

    def resume_writes(self) -> None: ...

    def is_quiesced(self) -> bool: ...

    def snapshot(self, dest: Path) -> BackupManifest: ...
