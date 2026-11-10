"""The user's own operations outside any session (DN006 §8.10).

The terminal commands ``tsw task accept`` / ``reject`` / ``cancel`` and the buttons of the STATUS
app (:mod:`tsw.host.status_app`) both come here. They run the state-machine tools as the ``user``
caller, which no session resolves to, and record the source themselves on the ``user-cli`` or
``status-app`` channel. Both refuse to run inside a host session: a session that could run them
could put words in the user's mouth (DN008 §9).
"""

from __future__ import annotations

import getpass
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tsw import lease
from tsw.config import EffectiveConfig
from tsw.errors import ErrorCode, TswError, rejected
from tsw.ledger import Ledger
from tsw.machine.base import Caller
from tsw.machine.model import task
from tsw.machine.runner import run_tool

# Claude Code sets these in every session's processes (CC008 §5.5); either one means a session.
SESSION_MARKERS = ("CLAUDE_CODE_HOST_SESSION_ID", "CLAUDECODE")

# action -> (tool, decision kind of the G-USER target, label)
ACTIONS: dict[str, tuple[str, str, str]] = {
    "accept": ("user_accept", "accept", "验收"),
    "reject": ("user_reject", "reject", "退回"),
    "cancel": ("task_cancel", "cancel", "取消"),
}
CANCELLABLE = (
    "intake",
    "waiting_dependency",
    "ready",
    "dispatching",
    "in_progress",
    "waiting_user",
    "paused",
    "verifying",
    "done",
)
ACTIVE_PHASES = ("starting", "standby", "live")


def in_host_session(env: Mapping[str, str]) -> bool:
    return any(env.get(name) for name in SESSION_MARKERS)


def require_outside_session(env: Mapping[str, str], what: str) -> None:
    """Refuse inside any host session, whatever its role (DN006 §8.10)."""
    if in_host_session(env):
        raise TswError(
            ErrorCode.REJECTED,
            f"{what}是用户本人的操作，只能在会话外的终端中运行",
            reason="host_session_cli",
        )


def principal() -> str:
    try:
        user = getpass.getuser()
    except (OSError, KeyError):  # no user name in the environment
        user = "unknown"
    return f"{user}@{lease.this_machine()}"


def source(
    channel: str, *, task_id: str, revision: int, kind: str, text: str, message_ref: str
) -> dict[str, Any]:
    return {
        "channel": channel,
        "principal": principal(),
        "message_ref": message_ref,
        "text": text,
        "target": [task_id, revision, kind],
    }


@dataclass(frozen=True)
class Availability:
    """Which direct actions a task offers, and why cancel is not offered when it is not."""

    actions: tuple[str, ...]
    cancel_blocked: str | None = None


def availability(conn: sqlite3.Connection, row: Mapping[str, Any]) -> Availability:
    status = str(row["status"])
    actions: list[str] = []
    if status == "done":
        actions += ["accept", "reject"]
    blocked: str | None = None
    if status in CANCELLABLE:
        marks = ",".join("?" * len(ACTIVE_PHASES))
        active = conn.execute(
            f"SELECT 1 FROM attempts WHERE task_id = ? AND phase IN ({marks})",
            (row["task_id"], *ACTIVE_PHASES),
        ).fetchone()
        integrating = conn.execute(
            "SELECT 1 FROM integrations WHERE task_id = ? AND status IN ('started', 'conflict')",
            (row["task_id"],),
        ).fetchone()
        if active is not None:
            blocked = "worker 仍在运行：请在主会话中先停止 worker 再取消"
        elif integrating is not None:
            blocked = "集成进行中：请在主会话中先撤回集成再取消"
        else:
            actions.append("cancel")
    return Availability(tuple(actions), blocked)


def perform(
    ledger: Ledger,
    config: EffectiveConfig,
    action: str,
    task_id: str,
    *,
    channel: str,
    message_ref: str,
    text: str,
    revision: int | None = None,
    reason: str | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    """Run one direct action as the ``user`` caller; ``revision`` defaults to the current one."""
    if action not in ACTIONS:
        raise rejected("未知的操作", reason="invalid_input", action=action)
    tool, kind, _ = ACTIONS[action]
    if revision is None:
        with ledger.view() as conn:
            revision = int(task(conn, task_id)["revision"])
    payload: dict[str, Any] = {
        "task_id": task_id,
        "user_source": source(
            channel,
            task_id=task_id,
            revision=revision,
            kind=kind,
            text=text,
            message_ref=message_ref,
        ),
    }
    if action == "reject":
        if not reason or not reason.strip():
            raise rejected("退回须写明理由", reason="invalid_input", field="reason")
        payload["reason"] = reason
    caller = Caller(role="user", session_id=None, machine=machine or lease.this_machine())
    result = run_tool(ledger, config, caller, tool, payload)
    event = result["events"][0]
    return {
        "action": action,
        "task_id": task_id,
        "revision": revision,
        "event": event["type"],
        "sequence": event["sequence"],
        "channel": channel,
    }
