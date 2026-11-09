"""Who is calling (DN001 §7.1).

The app gives every session process its app session ID (``CLAUDE_CODE_HOST_SESSION_ID``), for
the MCP server and for hooks alike. A session bound to an attempt is that attempt's worker; a
session opened in the data directory is the orchestrator role; anything else may only ask who
it is. A session that was ever bound to an attempt keeps that binding: once the attempt is
replaced or closed the session is ``revoked`` (it may only ask who it is and read), and it never
falls back to the orchestrator role even if it is opened in the data directory. The session
bound to the newest routine generation is the routine session (DN006 §8.9); one bound to an older
generation is revoked. The working directory is a consistency check, never the identity itself.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsw.home import Home, workdir_path
from tsw.machine.model import row_dict

SESSION_ENV = "CLAUDE_CODE_HOST_SESSION_ID"
WORKER_PHASES = frozenset({"live", "standby", "stopped"})
# The only ``tsw`` commands a host session may run on the command line; everything that writes
# the ledger goes through the MCP server, where the caller is the session itself (DN001 §7.1).
HOST_CLI_COMMANDS = frozenset(
    {
        "mcp",
        "guard",
        "review run",
        "home",
        "config show",
        "config validate",
        "tool list",
        "host install-hint",
        "lease show",
        "ledger audit",
    }
)


def _norm(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def within(path: Path, root: Path) -> bool:
    target, base = _norm(path), _norm(root)
    return target == base or target.startswith(base.rstrip("\\/") + os.sep)


@dataclass(frozen=True)
class Identity:
    role: str  # orchestrator | worker | routine | revoked | none
    session_id: str | None
    cwd: Path
    task_id: str | None = None
    attempt: str | None = None
    phase: str | None = None
    brief_path: str | None = None
    brief_revision: int | None = None
    writable_roots: tuple[Path, ...] = field(default_factory=tuple)
    cwd_consistent: bool = True
    pending: bool = False  # not bound yet, but started in a starting attempt's directory
    pending_role: str | None = None  # the role a pending session is about to get

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "task_id": self.task_id,
            "attempt": self.attempt,
            "phase": self.phase,
            "brief_path": self.brief_path,
            "brief_revision": self.brief_revision,
            "writable_roots": [str(p) for p in self.writable_roots],
            "cwd_consistent": self.cwd_consistent,
            "pending": self.pending,
            **({"pending_role": self.pending_role} if self.pending_role else {}),
        }


def resolve(conn: sqlite3.Connection, home: Home, env: Mapping[str, str], cwd: Path) -> Identity:
    session = env.get(SESSION_ENV) or None
    if session:
        row = row_dict(
            conn.execute(
                "SELECT * FROM attempts WHERE session_ref = ? ORDER BY rowid DESC LIMIT 1",
                (session,),
            ).fetchone()
        )
        if row is not None and row["phase"] not in WORKER_PHASES:
            return Identity(
                role="revoked",
                session_id=session,
                cwd=cwd,
                task_id=str(row["task_id"]),
                attempt=str(row["attempt"]),
                phase=str(row["phase"]),
                cwd_consistent=False,
            )
        if row is not None:
            roots = [home.root / "work" / str(row["task_id"]) / str(row["attempt"])]
            if row["workdir"]:
                roots.insert(0, workdir_path(home, str(row["workdir"])))
            consistent = any(within(cwd, root) for root in roots)
            return Identity(
                role="worker",
                session_id=session,
                cwd=cwd,
                task_id=str(row["task_id"]),
                attempt=str(row["attempt"]),
                phase=str(row["phase"]),
                brief_path=row["brief_path"],
                brief_revision=row["brief_revision"],
                writable_roots=tuple(roots),
                cwd_consistent=consistent,
            )
    if session:
        routine = row_dict(
            conn.execute(
                "SELECT * FROM routine_sessions WHERE session_ref = ? "
                "ORDER BY generation DESC LIMIT 1",
                (session,),
            ).fetchone()
        )
        if routine is not None:
            gen = f"routine-g{routine['generation']}"
            if routine["status"] != "bound":
                return Identity(
                    role="revoked",
                    session_id=session,
                    cwd=cwd,
                    attempt=gen,
                    phase=str(routine["status"]),
                    cwd_consistent=False,
                )
            return Identity(
                role="routine",
                session_id=session,
                cwd=cwd,
                attempt=gen,
                phase="live",
                cwd_consistent=within(cwd, home.root / str(routine["workdir"])),
            )
    if session and _norm(cwd) == _norm(home.root):
        return Identity(role="orchestrator", session_id=session, cwd=cwd)
    starting = _awaiting_routine(conn, home, cwd) if session else None
    if starting is not None:
        return Identity(
            role="none",
            session_id=session,
            cwd=cwd,
            attempt=f"routine-g{starting['generation']}",
            phase="starting",
            pending=True,
            pending_role="routine",
        )
    pending = _awaiting_session(conn, home, cwd) if session else None
    if pending is not None:
        return Identity(
            role="none",
            session_id=session,
            cwd=cwd,
            task_id=str(pending["task_id"]),
            attempt=str(pending["attempt"]),
            phase=str(pending["phase"]),
            pending=True,
            pending_role="worker",
        )
    return Identity(role="none", session_id=session, cwd=cwd)


def _awaiting_routine(conn: sqlite3.Connection, home: Home, cwd: Path) -> dict[str, Any] | None:
    """The routine generation being created in ``cwd`` (the data directory's ``routine/``)."""
    row = row_dict(
        conn.execute("SELECT * FROM routine_sessions ORDER BY generation DESC LIMIT 1").fetchone()
    )
    if row is None or row["status"] != "starting" or row["session_ref"] is not None:
        return None
    return row if within(cwd, home.root / str(row["workdir"])) else None


def _awaiting_session(conn: sqlite3.Connection, home: Home, cwd: Path) -> dict[str, Any] | None:
    """The attempt whose session is being created in ``cwd``, if any.

    The orchestrator records the attempt (``starting``), creates the session in the attempt's
    directory and binds it only after the app has returned the session ID. The new session's
    MCP server lists its tools in between, and hosts may keep that first list, so such a
    session is listed as the worker it is about to become; every call still waits for the
    binding.
    """
    for row in conn.execute(
        "SELECT * FROM attempts WHERE phase = 'starting' AND generation_valid = 1 "
        "AND session_ref IS NULL ORDER BY rowid DESC"
    ):
        found = row_dict(row)
        assert found is not None
        roots = [home.root / "work" / str(found["task_id"]) / str(found["attempt"])]
        if found["workdir"]:
            roots.append(workdir_path(home, str(found["workdir"])))
        if any(within(cwd, root) for root in roots):
            return found
    return None
