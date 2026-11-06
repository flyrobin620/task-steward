"""L1 projection: SQLite tables reduced from the ledger (DN007 §3).

The projection is derived data. ``reduce_to`` applies every event exactly once and in order
inside one SQLite transaction; checkpoints are content-addressed snapshots that let a lost
``state.sqlite`` be restored without replaying every sealed segment.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path

from tsw.errors import ErrorCode, TswError, rejected
from tsw.events import GENESIS, Event, sha256_hex
from tsw.faults import fault
from tsw.home import Home
from tsw.storage.base import LedgerTail, StorageBackend
from tsw.storage.fs import atomic_write

# Bump when tables or reduction rules change; older projections and checkpoints then need a
# rebuild (DN007 §3.4).
REDUCER_VERSION = 5
IMPORTS = frozenset({"import.task_imported"})  # events that carry their own times
KEEP_CHECKPOINTS = 3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS commands(
    command_id TEXT PRIMARY KEY,
    command_digest TEXT,
    first_sequence INTEGER NOT NULL,
    last_sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    version INTEGER NOT NULL,
    spec_path TEXT NOT NULL,
    spec_sha256 TEXT NOT NULL,
    registered_seq INTEGER NOT NULL,
    last_activity TEXT NOT NULL,
    source_seq INTEGER NOT NULL,
    goal TEXT NOT NULL DEFAULT '',
    acceptance TEXT NOT NULL DEFAULT '[]',
    resources TEXT NOT NULL DEFAULT '[]',
    substate TEXT,
    cycle INTEGER NOT NULL DEFAULT 0,
    stale INTEGER NOT NULL DEFAULT 0,
    stale_ack TEXT,
    waiting_revision INTEGER,
    pause TEXT,
    submission TEXT,
    current_attempt TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    previous_status TEXT,
    done_at TEXT,
    accepted_at TEXT,
    business_changed_at TEXT
);
CREATE TABLE IF NOT EXISTS dependencies(
    task_id TEXT NOT NULL,
    dep_id TEXT NOT NULL,
    bound_revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(task_id, dep_id)
);
CREATE TABLE IF NOT EXISTS attempts(
    task_id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    phase TEXT NOT NULL,
    session_ref TEXT,
    session_name TEXT,
    adapter TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    generation_valid INTEGER NOT NULL DEFAULT 0,
    invalid_generations TEXT NOT NULL DEFAULT '[]',
    purpose TEXT,
    controller TEXT,
    workdir TEXT,
    branch TEXT,
    brief_path TEXT,
    brief_revision INTEGER,
    read_set TEXT NOT NULL DEFAULT '[]',
    receipt TEXT,
    late_sessions TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(task_id, attempt)
);
CREATE TABLE IF NOT EXISTS review_cycles(
    task_id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    rounds INTEGER NOT NULL DEFAULT 0,
    orchestrator_extension INTEGER NOT NULL DEFAULT 0,
    user_extension INTEGER NOT NULL DEFAULT 0,
    last_round INTEGER,
    last_reviewed_head TEXT,
    last_decision TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(task_id, attempt, cycle)
);
CREATE TABLE IF NOT EXISTS review_rounds(
    task_id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    round INTEGER NOT NULL,
    cycle INTEGER NOT NULL,
    head TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    verdict TEXT,
    reviewed_head TEXT,
    decision TEXT,
    PRIMARY KEY(task_id, attempt, round)
);
CREATE TABLE IF NOT EXISTS findings(
    task_id TEXT NOT NULL,
    attempt TEXT NOT NULL,
    round INTEGER NOT NULL,
    finding_id TEXT NOT NULL,
    cycle INTEGER NOT NULL,
    severity TEXT NOT NULL,
    blocking INTEGER NOT NULL,
    action TEXT,
    reason TEXT,
    verification TEXT,
    disposition TEXT,
    decision_ref TEXT,
    PRIMARY KEY(task_id, attempt, round, finding_id)
);
CREATE TABLE IF NOT EXISTS judgments(
    item_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    decision_ref TEXT
);
CREATE TABLE IF NOT EXISTS decisions(
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    basis TEXT NOT NULL,
    confidence TEXT NOT NULL,
    sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS input_requests(
    request_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    question TEXT NOT NULL,
    binding TEXT NOT NULL,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS outputs(
    task_id TEXT NOT NULL,
    output_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ref TEXT NOT NULL,
    role TEXT,
    delivery TEXT,
    verified_at TEXT,
    evidence TEXT NOT NULL,
    PRIMARY KEY(task_id, output_id)
);
CREATE TABLE IF NOT EXISTS resource_claims(
    resource TEXT NOT NULL,
    owner_task TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(resource, owner_task)
);
CREATE TABLE IF NOT EXISTS integrations(
    task_id TEXT NOT NULL,
    repo TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(task_id, repo)
);
CREATE TABLE IF NOT EXISTS task_notes(
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY(task_id, sequence)
);
CREATE TABLE IF NOT EXISTS leases(
    name TEXT PRIMARY KEY,
    holder_session TEXT,
    machine TEXT,
    acquired_at TEXT,
    expires_at TEXT,
    source_seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS inbox_acks(
    sequence INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS imported_tasks(
    task_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    event_seq INTEGER NOT NULL,
    source_sequence INTEGER NOT NULL,
    old_status TEXT,
    resumable INTEGER NOT NULL,
    runs TEXT,
    legacy TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_queue(
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    PRIMARY KEY(task_id, sequence)
);
"""

Reducer = Callable[[sqlite3.Connection, Event], None]


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def projected_tail(conn: sqlite3.Connection) -> LedgerTail:
    seq = get_meta(conn, "projected_seq")
    checksum = get_meta(conn, "projected_checksum")
    if seq is None or checksum is None:
        return LedgerTail.empty()
    return LedgerTail(int(seq), checksum)


class Projection:
    def __init__(self, home: Home, storage: StorageBackend, reducers: dict[str, Reducer]) -> None:
        self.home = home
        self.storage = storage
        self.reducers = reducers

    @property
    def path(self) -> Path:
        return self.home.cache_dir / "state.sqlite"

    @property
    def checkpoint_dir(self) -> Path:
        return self.home.cache_dir / "checkpoints"

    # ------------------------------------------------------------------ open

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Open the projection, restoring from a checkpoint or creating it when missing.

        Callers that reduce must hold the commit lock.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._restore_latest_checkpoint()
        conn = connect(self.path)
        try:
            conn.executescript(SCHEMA_SQL)
            version = get_meta(conn, "reducer_version")
            if version is None:
                set_meta(conn, "reducer_version", str(REDUCER_VERSION))
            elif int(version) != REDUCER_VERSION:
                raise rejected(
                    "投影版本与当前 tsw 不一致，需要运行 tsw ledger rebuild",
                    reason="rebuild_required",
                    projection=int(version),
                    current=REDUCER_VERSION,
                )
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ reduce

    def reduce_to(self, conn: sqlite3.Connection) -> LedgerTail:
        """Apply every event after ``projected_seq`` exactly once, in order, in one transaction."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = projected_tail(conn)
            ledger = self.storage.tail()
            if current.sequence > ledger.sequence:
                raise rejected(
                    "投影领先于账本，请运行 tsw ledger audit",
                    reason="projection_ahead",
                    projected=current.sequence,
                    ledger=ledger.sequence,
                )
            if current.sequence == ledger.sequence and current.checksum != ledger.checksum:
                raise rejected(
                    "投影与账本属于不同的历史，请运行 tsw ledger audit",
                    reason="chain_mismatch",
                    sequence=current.sequence,
                )
            if current.sequence < ledger.sequence:
                prev = current.checksum
                for event in self.storage.read(since=current.sequence):
                    if event.prev != prev:
                        raise rejected(
                            "账本摘要链与投影不一致，请运行 tsw ledger audit",
                            reason="chain_mismatch",
                            sequence=event.sequence,
                        )
                    self._apply(conn, event)
                    prev = event.checksum
                    current = LedgerTail(event.sequence, event.checksum)
                set_meta(conn, "projected_seq", str(current.sequence))
                set_meta(conn, "projected_checksum", current.checksum)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return current

    def trial(self, conn: sqlite3.Connection, events: list[Event]) -> None:
        """Apply ``events`` in a transaction that is always rolled back.

        The ledger runs this before appending, so an event its reducer would refuse is never
        written: a stuck ledger (every later read failing on one event) cannot be produced.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            for event in events:
                try:
                    self._apply(conn, event)
                except TswError:
                    raise
                except Exception as exc:  # e.g. a constraint violation in a reducer
                    raise rejected(
                        "命令产生的事件无法归约，已拒绝",
                        reason="reducer_rejected",
                        type=event.type,
                        error=str(exc),
                    ) from exc
        finally:
            conn.execute("ROLLBACK")

    def _apply(self, conn: sqlite3.Connection, event: Event) -> None:
        reducer = self.reducers.get(event.type)
        if reducer is None:
            raise TswError(
                ErrorCode.UNSUPPORTED,
                "账本中有当前 tsw 不认识的事件类型，请升级 tsw",
                reason="unknown_event_type",
                type=event.type,
                sequence=event.sequence,
            )
        if event.command_id is not None:
            conn.execute(
                "INSERT INTO commands(command_id, command_digest, first_sequence, last_sequence) "
                "VALUES(?, ?, ?, ?) ON CONFLICT(command_id) DO UPDATE SET "
                "last_sequence = excluded.last_sequence",
                (event.command_id, event.command_digest, event.sequence, event.sequence),
            )
        reducer(conn, event)
        if event.task_id is not None:
            # An imported task keeps the business time it had in its source system (DN005 §4.3).
            conn.execute(
                "UPDATE tasks SET last_activity = CASE WHEN ? THEN last_activity ELSE ? END, "
                "source_seq = ? WHERE task_id = ?",
                (event.type in IMPORTS, event.time, event.sequence, event.task_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO index_queue(task_id, sequence) VALUES(?, ?)",
                (event.task_id, event.sequence),
            )
        fault("projection.apply")

    # ------------------------------------------------------------------ checkpoints

    def checkpoint(self, conn: sqlite3.Connection) -> Path:
        """Write a content-addressed snapshot of the projection (DN007 §3.4)."""
        tail = projected_tail(conn)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        scratch = self.checkpoint_dir / "snapshot.sqlite.tmp"
        scratch.unlink(missing_ok=True)
        target_conn = sqlite3.connect(scratch)
        try:
            conn.backup(target_conn)
        finally:
            target_conn.close()
        data = scratch.read_bytes()
        scratch.unlink()
        name = f"{tail.sequence:010d}-{sha256_hex(data)}.sqlite"
        path = self.checkpoint_dir / name
        if not path.exists():
            atomic_write(path, data)
        for old in self._checkpoints()[KEEP_CHECKPOINTS:]:
            old.unlink(missing_ok=True)
        return path

    def _checkpoints(self) -> list[Path]:
        if not self.checkpoint_dir.is_dir():
            return []
        return sorted(self.checkpoint_dir.glob("*.sqlite"), reverse=True)

    def _restore_latest_checkpoint(self) -> None:
        ledger_tail = self.storage.tail()
        for candidate in self._checkpoints():
            if self._checkpoint_usable(candidate, ledger_tail):
                atomic_write(self.path, candidate.read_bytes())
                return

    def _checkpoint_usable(self, path: Path, ledger_tail: LedgerTail) -> bool:
        seq_text, _, rest = path.stem.partition("-")
        try:
            data = path.read_bytes()
        except OSError:
            return False
        if sha256_hex(data) != rest:
            return False
        conn = sqlite3.connect(path)
        try:
            conn.row_factory = sqlite3.Row
            version = get_meta(conn, "reducer_version")
            tail = projected_tail(conn)
        except sqlite3.DatabaseError:
            return False
        finally:
            conn.close()
        if version is None or int(version) != REDUCER_VERSION:
            return False
        if tail.sequence != int(seq_text) or tail.sequence > ledger_tail.sequence:
            return False
        if tail.sequence == 0:
            return tail.checksum == GENESIS
        for event in self.storage.read(since=tail.sequence - 1):
            return event.sequence == tail.sequence and event.checksum == tail.checksum
        return False

    def discard(self) -> None:
        """Remove the projection and all checkpoints (used by a full rebuild)."""
        self.path.unlink(missing_ok=True)
        for path in self._checkpoints():
            path.unlink(missing_ok=True)
