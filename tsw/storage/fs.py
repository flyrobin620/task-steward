"""The ``fs`` storage backend: a local data directory (DN006 §9.3).

Ledger layout: ``ledger/events/<YYYY-MM>-<first sequence>.jsonl`` is the active segment and
``.jsonl.gz`` files are sealed segments. A segment is sealed when the month of the next event
changes or when it would exceed ``segment_max_bytes``. Sealed bytes never change.

Appends write the whole active segment to a temporary file and atomically replace it, so a crash
never leaves a torn line. Locks are OS file locks that die with their process.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import os
import re
import shutil
import socket
import sys
import time
from collections.abc import Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from tsw import __version__
from tsw.errors import ErrorCode, TswError, rejected
from tsw.events import Event, sha256_hex, utc_now
from tsw.faults import fault
from tsw.home import BACKED_UP, LAYOUT, MARKER, Home, open_home
from tsw.locks import FileLock
from tsw.storage.base import PLUGIN_API, BackupManifest, Lease, LedgerTail

DEFAULT_SEGMENT_MAX_BYTES = 16 * 1024 * 1024
MANIFEST = "backup-manifest.json"
QUIESCE_MARKER = "quiesced.json"
ARTIFACT_ROOTS = ("runs", "todo", "outbox", "legacy", "config", "views")
_SEGMENT = re.compile(r"^(?P<month>\d{4}-\d{2})-(?P<first>\d{10})\.jsonl(?P<gz>\.gz)?$")
_REPLACE_DEADLINE = 5.0
_READ_RETRIES = 5


@dataclass(frozen=True)
class Segment:
    path: Path
    month: str
    first: int
    sealed: bool
    compressed: bool


def _fsync_dir(path: Path) -> None:
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file and an atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    deadline = time.monotonic() + _REPLACE_DEADLINE
    while True:
        try:
            tmp.replace(path)
            break
        except PermissionError:
            # Windows refuses to replace a file another process has open; readers are brief.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
    _fsync_dir(path.parent)


def _last_line(data: bytes) -> bytes | None:
    stripped = data.rstrip(b"\n")
    if not stripped:
        return None
    return stripped.rsplit(b"\n", 1)[-1]


class FsStorage:
    """Local file-system storage for one data directory."""

    def __init__(
        self,
        home: Home,
        *,
        segment_max_bytes: int = DEFAULT_SEGMENT_MAX_BYTES,
        lock_timeout: float = 30.0,
    ) -> None:
        self.home = home
        self.segment_max_bytes = segment_max_bytes
        self.lock_timeout = lock_timeout

    # ------------------------------------------------------------------ capabilities

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "name": "fs",
            "plugin_api": PLUGIN_API,
            "atomic_append": True,
            "cross_machine": False,
        }

    # ------------------------------------------------------------------ segments

    def segments(self) -> list[Segment]:
        found: dict[tuple[int, bool], Segment] = {}
        directory = self.home.ledger_dir
        if not directory.is_dir():
            return []
        for entry in directory.iterdir():
            match = _SEGMENT.match(entry.name)
            if not match:
                continue
            compressed = match["gz"] is not None
            seg = Segment(
                path=entry,
                month=match["month"],
                first=int(match["first"]),
                sealed=compressed,
                compressed=compressed,
            )
            found[(seg.first, compressed)] = seg
        firsts = sorted({first for first, _ in found})
        result: list[Segment] = []
        for first in firsts:
            source = found.get((first, False))
            if source is None:
                result.append(found[(first, True)])
            elif (first, True) in found and first != firsts[-1]:
                # A seal that did not finish removing its source: the .jsonl is authoritative
                # and the segment is sealed; the next append removes the leftover.
                result.append(Segment(source.path, source.month, first, True, False))
            else:
                result.append(source)
        active = [seg for seg in result if not seg.sealed]
        if len(active) > 1 or (active and active[0] is not result[-1]):
            raise rejected(
                "账本分段异常：存在多个活动分段或活动分段不是最后一段",
                reason="ledger_corrupt",
                segments=[seg.path.name for seg in result],
            )
        return result

    @staticmethod
    def _segment_bytes(seg: Segment) -> bytes:
        data = seg.path.read_bytes()
        return gzip.decompress(data) if seg.compressed else data

    def tail(self) -> LedgerTail:
        for attempt in range(_READ_RETRIES):
            try:
                for seg in reversed(self.segments()):
                    line = _last_line(self._segment_bytes(seg))
                    if line is not None:
                        event = Event.from_line(line)
                        return LedgerTail(event.sequence, event.checksum)
                return LedgerTail.empty()
            except FileNotFoundError:
                # A concurrent append sealed and removed a segment we had listed; list again.
                if attempt == _READ_RETRIES - 1:
                    raise
        return LedgerTail.empty()

    # ------------------------------------------------------------------ ledger

    def read(self, since: int = 0) -> Iterator[Event]:
        """Yield events after ``since``; retries when a concurrent seal moves a segment."""
        last = since
        for attempt in range(_READ_RETRIES):
            try:
                for event in self._read_once(last):
                    last = event.sequence
                    yield event
                return
            except FileNotFoundError:
                if attempt == _READ_RETRIES - 1:
                    raise

    def _read_once(self, since: int) -> Iterator[Event]:
        segments = self.segments()
        start = 0
        for index, seg in enumerate(segments):
            if seg.first <= since + 1:
                start = index
        expected = None
        for seg in segments[start:]:
            for line in self._segment_bytes(seg).splitlines():
                if not line:
                    continue
                event = Event.from_line(line)
                if expected is None and event.sequence > since + 1:
                    raise rejected(
                        "账本序号不连续",
                        reason="ledger_corrupt",
                        expected=since + 1,
                        found=event.sequence,
                    )
                if expected is not None and event.sequence != expected:
                    raise rejected(
                        "账本序号不连续",
                        reason="ledger_corrupt",
                        expected=expected,
                        found=event.sequence,
                    )
                expected = event.sequence + 1
                if event.sequence > since:
                    yield event

    def append(self, events: Sequence[Event], *, expected_sequence: int) -> int:
        """Atomic optimistic append. The append lock makes check-and-replace one step even for
        callers that do not hold the ledger's commit lock."""
        if not events:
            return expected_sequence
        with FileLock(self.home.locks_dir / "append.lock", timeout=self.lock_timeout):
            return self._append_locked(events, expected_sequence)

    def _append_locked(self, events: Sequence[Event], expected_sequence: int) -> int:
        self._recover_seal()
        tail = self.tail()
        if tail.sequence != expected_sequence:
            raise rejected(
                "账本末端已变化",
                reason="sequence_conflict",
                expected=expected_sequence,
                actual=tail.sequence,
            )
        prev = tail.checksum
        for offset, event in enumerate(events):
            if event.sequence != expected_sequence + 1 + offset:
                raise rejected("事件序号不连续", reason="invalid_events", sequence=event.sequence)
            if event.prev != prev or event.computed_checksum() != event.checksum:
                raise rejected("事件摘要链不连续", reason="invalid_events", sequence=event.sequence)
            prev = event.checksum
        payload = b"".join(event.to_line() for event in events)
        month = events[0].time[:7]
        segments = self.segments()
        active = segments[-1] if segments and not segments[-1].sealed else None
        target = self.home.ledger_dir / f"{month}-{events[0].sequence:010d}.jsonl"
        current = b""
        if active is not None:
            existing = active.path.read_bytes()
            if active.month != month or len(existing) + len(payload) > self.segment_max_bytes:
                self._seal(active, existing)
            else:
                target, current = active.path, existing
        fault("fs.append.before_replace")
        atomic_write(target, current + payload)
        fault("fs.append.after_replace")
        return events[-1].sequence

    def _seal(self, seg: Segment, data: bytes) -> None:
        sealed = seg.path.with_name(seg.path.name + ".gz")
        atomic_write(sealed, gzip.compress(data, mtime=0))
        fault("fs.seal.before_remove")
        seg.path.unlink()
        _fsync_dir(seg.path.parent)

    def _recover_seal(self) -> None:
        """Finish a seal interrupted between writing the .gz and removing the source."""
        directory = self.home.ledger_dir
        if not directory.is_dir():
            return
        for entry in directory.glob("*.jsonl"):
            sealed = entry.with_name(entry.name + ".gz")
            if not sealed.exists():
                continue
            try:
                same = gzip.decompress(sealed.read_bytes()) == entry.read_bytes()
            except (OSError, EOFError):
                same = False
            if same:
                entry.unlink()
            else:
                sealed.unlink()

    # ------------------------------------------------------------------ artifacts

    def _artifact_path(self, rel: str) -> Path:
        pure = PurePosixPath(rel.replace("\\", "/"))
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise rejected("非法的相对路径", path=rel)
        if pure.parts[0] not in ARTIFACT_ROOTS:
            raise rejected("只能写入 tsw 管理的目录", path=rel, allowed=list(ARTIFACT_ROOTS))
        return self.home.root.joinpath(*pure.parts)

    def put_artifact(self, rel: str, data: bytes) -> str:
        atomic_write(self._artifact_path(rel), data)
        return sha256_hex(data)

    def read_artifact(self, rel: str) -> bytes:
        return self._artifact_path(rel).read_bytes()

    # ------------------------------------------------------------------ locks

    def _lock_path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise rejected("非法的锁名", name=name)
        return self.home.locks_dir / f"{name}.lock"

    @contextlib.contextmanager
    def lock(self, name: str, ttl: float) -> Generator[Lease, None, None]:
        """An exclusive named lock. ``ttl`` is recorded for diagnosis; the OS lock is the guard."""
        path = self._lock_path(name)
        with FileLock(path, timeout=self.lock_timeout):
            holder = {
                "pid": os.getpid(),
                "machine": socket.gethostname(),
                "acquired_at": utc_now(),
                "ttl_seconds": ttl,
            }
            info = path.with_suffix(".json")
            atomic_write(info, (json.dumps(holder) + "\n").encode("utf-8"))
            try:
                yield Lease(name, holder)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    info.unlink()

    def _gate(self, shared: bool) -> FileLock:
        return FileLock(
            self.home.locks_dir / "write-gate.lock", shared=shared, timeout=self.lock_timeout
        )

    @property
    def _quiesce_marker(self) -> Path:
        return self.home.locks_dir / QUIESCE_MARKER

    def is_quiesced(self) -> bool:
        return self._quiesce_marker.exists()

    @contextlib.contextmanager
    def writer(self) -> Generator[None, None, None]:
        gate = self._gate(shared=True)
        gate.acquire()
        try:
            if self.is_quiesced():
                raise TswError(ErrorCode.QUIESCED, "数据目录已静止，暂不接受写入")
            yield
        finally:
            gate.release()

    @contextlib.contextmanager
    def exclusive(self) -> Generator[None, None, None]:
        with self._gate(shared=False):
            yield

    def quiesce(self) -> BackupManifest:
        """Stop all writes until :meth:`resume_writes`; the directory can then be copied as is."""
        if not self.is_quiesced():
            marker = {"quiesced_at": utc_now(), "pid": os.getpid()}
            atomic_write(self._quiesce_marker, (json.dumps(marker) + "\n").encode("utf-8"))
        with self.exclusive():
            manifest = self._manifest(self.home.root)
            atomic_write(self.home.path(MANIFEST), _manifest_bytes(manifest))
        return manifest

    def resume_writes(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.home.path(MANIFEST).unlink()
        with contextlib.suppress(FileNotFoundError):
            self._quiesce_marker.unlink()

    # ------------------------------------------------------------------ backup

    def _manifest(self, root: Path) -> BackupManifest:
        tail = FsStorage(Home(root)).tail()
        files: list[dict[str, Any]] = []
        for top in (*BACKED_UP, MARKER):
            base = root / top
            paths = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
            for path in paths:
                if path.name.endswith(".tmp"):
                    continue
                files.append(
                    {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size}
                )
        return {
            "format": 1,
            "created_at": utc_now(),
            "created_by": f"tsw {__version__}",
            "ledger_tail": {"sequence": tail.sequence, "checksum": tail.checksum},
            "files": files,
        }

    def snapshot(self, dest: Path) -> BackupManifest:
        if dest.exists() and any(dest.iterdir()):
            raise rejected("备份目标目录非空", path=str(dest))
        with self.exclusive():
            dest.mkdir(parents=True, exist_ok=True)
            for top in BACKED_UP:
                source = self.home.path(top)
                if source.is_dir():
                    shutil.copytree(source, dest / top, ignore=shutil.ignore_patterns("*.tmp"))
            shutil.copy2(self.home.path(MARKER), dest / MARKER)
            manifest = self._manifest(dest)
            atomic_write(dest / MANIFEST, _manifest_bytes(manifest))
        work = self.home.path("work")
        if work.is_dir():
            # Best effort only (DN006 §9.5): worker drafts are not guaranteed to be consistent.
            with contextlib.suppress(OSError):
                shutil.copytree(work, dest / "work")
        return manifest


def _manifest_bytes(manifest: BackupManifest) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def verify_manifest(root: Path) -> list[str]:
    """Problems that make a copy unverified; an empty list means the manifest matches."""
    path = root / MANIFEST
    if not path.is_file():
        return ["缺少 backup-manifest.json"]
    manifest = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    tail = FsStorage(Home(root)).tail()
    recorded = manifest.get("ledger_tail", {})
    if recorded.get("sequence") != tail.sequence or recorded.get("checksum") != tail.checksum:
        problems.append("账本末端与 manifest 不一致")
    for entry in manifest.get("files", []):
        file = root / entry["path"]
        if not file.is_file():
            problems.append(f"缺少文件 {entry['path']}")
        elif file.stat().st_size != entry["bytes"]:
            problems.append(f"文件大小不一致 {entry['path']}")
    return problems


def restore(src: Path, dest: Path, *, allow_unverified: bool = False) -> list[str]:
    """Copy a backup into an empty ``dest``. Returns the manifest problems that were accepted.

    The caller must audit the restored ledger and rebuild the projection before using it.
    """
    open_home(src)
    if dest.exists() and any(dest.iterdir()):
        raise rejected("恢复目标目录非空", path=str(dest))
    problems = verify_manifest(src)
    if problems and not allow_unverified:
        raise rejected(
            "未核实的备份：需要 --allow-unverified，并在恢复后通过全量审计",
            problems=problems,
        )
    dest.mkdir(parents=True, exist_ok=True)
    for top in (*BACKED_UP, "work"):
        source = src / top
        if source.is_dir():
            shutil.copytree(source, dest / top, ignore=shutil.ignore_patterns("*.tmp"))
    shutil.copy2(src / MARKER, dest / MARKER)
    for rel in LAYOUT:
        (dest / rel).mkdir(parents=True, exist_ok=True)
    return problems
