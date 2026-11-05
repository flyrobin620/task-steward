"""The write path (DN007 §3.2): admission, commit lock, reduce, guard, append, reduce.

Every write command:

1. takes the shared write gate and checks the quiesce marker;
2. takes the commit lock;
3. brings the projection up to the ledger tail (``reduce_to``);
4. deduplicates by ``command_id`` and runs the command's guards against the projection;
5. appends the new events with ``expected_sequence = tail``;
6. reduces the new events (the same ``reduce_to``) and runs the command's follow-up step;
7. releases the commit lock and the write gate.

If the process stops between 5 and 6 the events are committed and the projection is behind;
the next read or write catches up through ``reduce_to``.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Generator
from typing import Any

from tsw.commands import Command, CommitContext, CommitResult
from tsw.errors import rejected
from tsw.events import GENESIS, Event, digest, seal, utc_now
from tsw.faults import fault
from tsw.home import Home
from tsw.projection import Projection, projected_tail
from tsw.reducers import REDUCERS
from tsw.storage.base import LedgerTail, StorageBackend
from tsw.storage.fs import FsStorage
from tsw.views import refresh as refresh_views

COMMIT_LOCK = "commit"
DEFAULT_CHECKPOINT_EVERY = 64


class Ledger:
    def __init__(
        self,
        home: Home,
        storage: StorageBackend | None = None,
        *,
        clock: Callable[[], str] = utc_now,
        checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY,
    ) -> None:
        self.home = home
        self.storage: StorageBackend = storage if storage is not None else FsStorage(home)
        self.clock = clock
        self.checkpoint_every = checkpoint_every
        self.projection = Projection(home, self.storage, REDUCERS)

    # ------------------------------------------------------------------ reads

    @contextlib.contextmanager
    def view(self) -> Generator[sqlite3.Connection, None, None]:
        """A projection connection that is caught up with the ledger tail."""
        with self.storage.lock(COMMIT_LOCK, ttl=60), self.projection.connection() as conn:
            self.projection.reduce_to(conn)
            yield conn

    # ------------------------------------------------------------------ writes

    def commit(self, command: Command) -> CommitResult:
        command_digest = digest(dict(command.input))
        with (
            self.storage.writer(),
            self.storage.lock(COMMIT_LOCK, ttl=60),
            self.projection.connection() as conn,
        ):
            tail = self.projection.reduce_to(conn)
            # Catch up derived files left behind by an interrupted earlier commit.
            refresh_views(CommitContext(conn=conn, storage=self.storage, home=self.home, tail=tail))
            if command.command_id is not None:
                existing = conn.execute(
                    "SELECT command_digest, first_sequence, last_sequence FROM commands "
                    "WHERE command_id = ?",
                    (command.command_id,),
                ).fetchone()
                if existing is not None:
                    if existing["command_digest"] != command_digest:
                        raise rejected(
                            "command_id 已用于内容不同的命令",
                            reason="command_conflict",
                            command_id=command.command_id,
                        )
                    events = [
                        event
                        for event in self.storage.read(since=existing["first_sequence"] - 1)
                        if event.sequence <= existing["last_sequence"]
                    ]
                    return CommitResult(events=events, duplicate=True)
            ctx = CommitContext(conn=conn, storage=self.storage, home=self.home, tail=tail)
            drafts = list(command.build(ctx))
            events: list[Event] = []
            prev = tail.checksum
            time = self.clock()
            for offset, draft in enumerate(drafts):
                event = seal(
                    draft,
                    sequence=tail.sequence + 1 + offset,
                    prev=prev,
                    time=time,
                    actor=command.actor,
                    command_id=command.command_id,
                    command_digest=command_digest if command.command_id is not None else None,
                )
                events.append(event)
                prev = event.checksum
            if not events:
                return CommitResult()
            self.storage.append(events, expected_sequence=tail.sequence)
            fault("ledger.commit.after_append")
            new_tail = self.projection.reduce_to(conn)
            ctx.tail = new_tail
            refresh_views(ctx)
            if new_tail.sequence // self.checkpoint_every > tail.sequence // self.checkpoint_every:
                self.projection.checkpoint(conn)
            return CommitResult(events=events)

    # ------------------------------------------------------------------ maintenance

    def rebuild(self) -> LedgerTail:
        """Discard the projection and checkpoints, reduce the whole ledger again and regenerate
        the derived files (a write: refused while the data directory is quiesced)."""
        with self.storage.writer(), self.storage.lock(COMMIT_LOCK, ttl=600):
            self.projection.discard()
            with self.projection.connection() as conn:
                tail = self.projection.reduce_to(conn)
                self.projection.checkpoint(conn)
                refresh_views(
                    CommitContext(conn=conn, storage=self.storage, home=self.home, tail=tail)
                )
                return tail

    def audit(self) -> dict[str, Any]:
        """Verify the whole digest chain and compare the projection with the ledger.

        Runs under the commit lock so that the ledger and the projection are read at one point.
        """
        with self.storage.lock(COMMIT_LOCK, ttl=600):
            return self._audit()

    def _audit(self) -> dict[str, Any]:
        problems: list[str] = []
        count = 0
        prev = GENESIS
        expected = 1
        try:
            for event in self.storage.read(since=0):
                if event.sequence != expected:
                    problems.append(f"序号应为 {expected}，实际为 {event.sequence}")
                    break
                if event.prev != prev:
                    problems.append(f"事件 {event.sequence} 的 prev 与前一事件摘要不一致")
                    break
                prev = event.checksum
                expected += 1
                count += 1
        except Exception as exc:  # report every read failure as an audit problem
            problems.append(f"读取账本失败：{exc}")
        tail = LedgerTail(count, prev)
        projection: dict[str, Any] = {"present": self.projection.path.exists()}
        if projection["present"] and not problems:
            conn = sqlite3.connect(self.projection.path)
            conn.row_factory = sqlite3.Row
            try:
                projected = projected_tail(conn)
            except sqlite3.DatabaseError as exc:
                problems.append(f"投影无法读取：{exc}")
                projected = None
            finally:
                conn.close()
            if projected is not None:
                projection["projected_seq"] = projected.sequence
                if projected.sequence > tail.sequence:
                    problems.append("投影领先于账本")
                elif projected.sequence > 0:
                    match = next(
                        (
                            e.checksum == projected.checksum
                            for e in self.storage.read(since=projected.sequence - 1)
                        ),
                        False,
                    )
                    if not match:
                        problems.append("投影记录的摘要与账本不一致")
        return {
            "ok": not problems,
            "events": count,
            "tail": {"sequence": tail.sequence, "checksum": tail.checksum},
            "segments": [seg.path.name for seg in self._segments()],
            "projection": projection,
            "problems": problems,
        }

    def _segments(self) -> list[Any]:
        segments = getattr(self.storage, "segments", None)
        if segments is None:
            return []
        try:
            return list(segments())
        except Exception:
            return []
