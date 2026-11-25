"""Delivering the user's answer from the Dashboard to a worker's session (DN006 §8.10, T357).

Claude Code (CC001 §6.1): every running session publishes ``~/.claude/sessions/<pid>.json`` with
its host session id (``hostSessionId``, the ``local_…`` id tsw keeps for an attempt) and its inbox
pipe (``messagingSocketPath``), and next to it ``<pid>.<sha256 of the lower-cased pipe path>.key``
with the peer token. A local process writes two JSON lines to the pipe, an ``auth`` line with the
token and a ``user`` line with the text, the way the workdeck daemon does (workdeck
``src/providers/claude-code/inbox.ts``); the session shows it as a message from another session
without a wrapper, the hook records it as the user's words and the worker registers the answer.
The probe of T357 (Claude Code 2.1.330) showed the host keeps the sender name given here
(``origin.from: tsw-dashboard``).

The token is read just before the write and only kept in memory: it is never logged, returned or
sent to any other pipe. Nothing is retried after a write began: whether the message arrived is
read from the ledger (the hook records it), not from the pipe.

Standard library only. Tests point ``directory`` at their own folder and serve their own pipe.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

SENDER = "tsw-dashboard"
MAX_CHARS = 4000  # what the Dashboard accepts as one answer (DN006 §8.10 item 4)
# A Windows pipe whose instances are all taken answers ERROR_PIPE_BUSY; it frees up quickly.
PIPE_BUSY = 231
BUSY_WAIT = 2.0


def sessions_dir() -> Path:
    """Where Claude Code publishes its running sessions: ``$CLAUDE_CONFIG_DIR/sessions``, by
    default ``~/.claude/sessions`` (the same place the host's settings are read from)."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config) if config else Path.home() / ".claude") / "sessions"


@dataclass(frozen=True)
class Target:
    """A running session's inbox, as its registry entry names it."""

    pid: int
    pipe: str
    updated: float


@dataclass(frozen=True)
class Outcome:
    """``written``: both lines went into the pipe. ``not_running``: no running session has this
    id (no entry, or its pipe cannot be opened): nothing was written. ``failed``: something kept
    the write from starting (no key file, the pipe refused): nothing was written. ``unknown``: an
    error after writing began: the message may or may not have arrived."""

    state: str
    detail: str = ""


def targets(host_session_id: str, directory: Path | None = None) -> list[Target]:
    """The registry entries of the session with this host id, the most recently updated first."""
    found: list[Target] = []
    folder = directory or sessions_dir()
    try:
        entries = list(folder.glob("*.json"))
    except OSError:
        return []
    for path in entries:
        try:
            raw: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        data = cast("dict[str, Any]", raw)
        if data.get("hostSessionId") != host_session_id:
            continue
        pid, pipe = data.get("pid"), data.get("messagingSocketPath")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if not isinstance(pipe, str) or not pipe:
            continue
        updated = data.get("updatedAt")
        when = float(updated) if isinstance(updated, (int, float)) else 0.0
        found.append(Target(pid, pipe, when))
    found.sort(key=lambda t: t.updated, reverse=True)
    return found


def key_file(directory: Path, pid: int, pipe: str) -> Path:
    digest = hashlib.sha256(pipe.lower().encode("utf-8")).hexdigest()
    return directory / f"{pid}.{digest}.key"


def frames(token: str, text: str, sender: str = SENDER) -> bytes:
    """The two lines the session's inbox reads: authentication, then the message."""
    auth = json.dumps({"type": "auth", "token": token})
    user = json.dumps(
        {
            "type": "user",
            "from": sender,
            "priority": "next",
            "message": {"role": "user", "content": text},
        }
    )
    return f"{auth}\n{user}\n".encode()


def _token(directory: Path, target: Target) -> str | None:
    try:
        raw: Any = json.loads(key_file(directory, target.pid, target.pipe).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    token = cast("dict[str, Any]", raw).get("peerToken") if isinstance(raw, dict) else None
    return token if isinstance(token, str) and token else None


def _open(pipe: str) -> Any:
    """A writable connection to the pipe, or OSError when it cannot be opened."""
    if os.name == "nt" or pipe.startswith("\\\\"):
        deadline = time.monotonic() + BUSY_WAIT
        while True:
            try:
                return Path(pipe).open("r+b", buffering=0)  # closed by the caller
            except OSError as exc:
                if getattr(exc, "winerror", None) != PIPE_BUSY or time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # pyright: ignore[reportAttributeAccessIssue,reportUnknownMemberType,reportUnknownVariableType]
    try:
        sock.settimeout(10)
        sock.connect(pipe)
    except OSError:
        sock.close()
        raise
    return sock


def _write(conn: Any, payload: bytes) -> None:
    if isinstance(conn, socket.socket):
        conn.sendall(payload)
        with contextlib.suppress(OSError):
            conn.shutdown(socket.SHUT_WR)
    else:
        view = memoryview(payload)
        while view:
            written = conn.write(view)
            if not written:
                raise OSError("pipe accepted no data")
            view = view[written:]


def deliver(host_session_id: str, text: str, directory: Path | None = None) -> Outcome:
    """Write ``text`` into the inbox of the running session with this host id, once."""
    folder = directory or sessions_dir()
    found = targets(host_session_id, folder)
    if not found:
        return Outcome("not_running", "没有找到正在运行的 worker 会话")
    last = Outcome("not_running", "worker 会话的收件箱无法打开")
    for target in found:
        token = _token(folder, target)
        if token is None:
            last = Outcome("failed", "找不到 worker 会话收件箱对应的 key 文件")
            continue
        try:
            conn = _open(target.pipe)
        except OSError as exc:
            last = Outcome("not_running", f"worker 会话的收件箱无法打开（{type(exc).__name__}）")
            continue
        payload = frames(token, text)
        del token
        try:
            with contextlib.closing(conn):
                _write(conn, payload)
        except OSError as exc:
            return Outcome("unknown", f"写入中途出错（{type(exc).__name__}）")
        return Outcome("written")
    return last
