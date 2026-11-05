"""Who is calling (DN001 §7.1).

The app gives every session process its app session ID (``CLAUDE_CODE_HOST_SESSION_ID``), for
the MCP server and for hooks alike. A session bound to an attempt is that attempt's worker; a
session opened in the data directory is the orchestrator role; anything else may only ask who
it is. A session that was ever bound to an attempt keeps that binding: once the attempt is
replaced or closed the session is ``revoked`` (it may only ask who it is and read), and it never
falls back to the orchestrator role even if it is opened in the data directory. The working
directory is a consistency check, never the identity itself.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tsw.home import Home
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
    role: str  # orchestrator | worker | revoked | none
    session_id: str | None
    cwd: Path
    task_id: str | None = None
    attempt: str | None = None
    phase: str | None = None
    brief_path: str | None = None
    writable_roots: tuple[Path, ...] = field(default_factory=tuple)
    cwd_consistent: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "session_id": self.session_id,
            "cwd": str(self.cwd),
            "task_id": self.task_id,
            "attempt": self.attempt,
            "phase": self.phase,
            "brief_path": self.brief_path,
            "writable_roots": [str(p) for p in self.writable_roots],
            "cwd_consistent": self.cwd_consistent,
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
                roots.insert(0, Path(str(row["workdir"])).expanduser())
            consistent = any(within(cwd, root) for root in roots)
            return Identity(
                role="worker",
                session_id=session,
                cwd=cwd,
                task_id=str(row["task_id"]),
                attempt=str(row["attempt"]),
                phase=str(row["phase"]),
                brief_path=row["brief_path"],
                writable_roots=tuple(roots),
                cwd_consistent=consistent,
            )
    if session and _norm(cwd) == _norm(home.root):
        return Identity(role="orchestrator", session_id=session, cwd=cwd)
    return Identity(role="none", session_id=session, cwd=cwd)
