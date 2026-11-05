"""The scheduling lease (DN001 §6.4, DN006 §9.6) and the G-LEASE guard (DN002 §4).

One orchestrator session at a time holds the lease. It is acquired when free or expired on the
same machine, renewed by its holder, released on purpose (for example before a cold copy) or
taken over after the user confirms that the previous orchestrator has stopped. A lease recorded
by another machine is never acquired silently, even after it expired: only a takeover with
``from_machine`` and the user's confirmation moves it.
"""

from __future__ import annotations

import socket
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.errors import rejected
from tsw.events import Event, EventDraft

LEASE_NAME = "scheduler"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
USER_SOURCE_FIELDS = ("channel", "principal", "message_ref", "text")


def this_machine() -> str:
    return socket.gethostname()


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)


def lease_row(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM leases WHERE name = ?", (LEASE_NAME,)).fetchone()
    if row is None or row["holder_session"] is None:
        return None
    return {key: row[key] for key in row.keys()}  # noqa: SIM118


def is_expired(lease: Mapping[str, Any], now: str) -> bool:
    return parse_time(str(lease["expires_at"])) <= parse_time(now)


def require_lease(conn: sqlite3.Connection, session_id: str, machine: str, now: str) -> None:
    """G-LEASE: the calling orchestrator session holds a valid lease on this machine."""
    lease = lease_row(conn)
    if lease is None:
        raise rejected("没有主会话持有调度租约", reason="lease_missing")
    if lease["holder_session"] != session_id or lease["machine"] != machine:
        raise rejected(
            "调度租约由其他主会话持有，当前会话只读运行",
            reason="lease_held",
            holder=lease["holder_session"],
            machine=lease["machine"],
        )
    if is_expired(lease, now):
        raise rejected("调度租约已过期，请重新领取", reason="lease_expired")


def _expires(now: str, minutes: int) -> str:
    return format_time(parse_time(now) + timedelta(minutes=minutes))


def acquire(
    session_id: str,
    *,
    machine: str,
    lease_minutes: int,
    actor: Mapping[str, Any],
    command_id: str | None = None,
) -> Command:
    if not session_id.strip():
        raise rejected("缺少会话 ID")

    def build(ctx: CommitContext) -> list[EventDraft]:
        lease = lease_row(ctx.conn)
        renew = lease is not None and lease["holder_session"] == session_id
        if lease is not None and lease["machine"] != machine:
            raise rejected(
                "调度租约属于另一台机器且未释放；确认原机器已停止后用 takeover --from-machine 接管",
                reason="lease_other_machine",
                machine=lease["machine"],
            )
        if lease is not None and not renew and not is_expired(lease, ctx.now):
            raise rejected(
                "调度租约由其他主会话持有，当前会话只读运行",
                reason="lease_held",
                holder=lease["holder_session"],
                expires_at=lease["expires_at"],
            )
        return [
            EventDraft(
                type="controller.lease_acquired",
                data={
                    "session_id": session_id,
                    "machine": machine,
                    "expires_at": _expires(ctx.now, lease_minutes),
                    "renewed": renew,
                    "previous": None if lease is None or renew else lease["holder_session"],
                },
            )
        ]

    return Command(
        actor=actor,
        input={"op": "lease.acquire", "session_id": session_id, "machine": machine},
        build=build,
        command_id=command_id,
    )


def release(session_id: str, *, machine: str, actor: Mapping[str, Any]) -> Command:
    def build(ctx: CommitContext) -> list[EventDraft]:
        lease = lease_row(ctx.conn)
        if lease is None:
            raise rejected("没有可释放的调度租约", reason="lease_missing")
        if lease["holder_session"] != session_id or lease["machine"] != machine:
            raise rejected("只有租约持有者可以释放", reason="lease_held")
        return [
            EventDraft(
                type="controller.lease_released",
                data={"session_id": session_id, "machine": machine},
            )
        ]

    return Command(
        actor=actor,
        input={"op": "lease.release", "session_id": session_id, "machine": machine},
        build=build,
    )


def takeover(
    session_id: str,
    *,
    machine: str,
    from_session: str,
    user_source: Mapping[str, Any],
    lease_minutes: int,
    from_machine: bool,
    actor: Mapping[str, Any],
    command_id: str | None = None,
) -> Command:
    missing = [key for key in USER_SOURCE_FIELDS if not str(user_source.get(key, "")).strip()]
    if missing:
        raise rejected(
            "接管需要用户确认原主会话已停止的来源", reason="user_source_missing", missing=missing
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        lease = lease_row(ctx.conn)
        if lease is None:
            raise rejected("没有可接管的租约，直接领取即可", reason="lease_missing")
        if lease["holder_session"] != from_session:
            raise rejected(
                "租约持有者与要接管的会话不一致",
                reason="lease_holder_mismatch",
                holder=lease["holder_session"],
            )
        if lease["machine"] != machine and not from_machine:
            raise rejected(
                "租约属于另一台机器，需要 --from-machine 并确认原机器已停止",
                reason="lease_other_machine",
                machine=lease["machine"],
            )
        return [
            EventDraft(
                type="controller.lease_taken_over",
                data={
                    "session_id": session_id,
                    "machine": machine,
                    "expires_at": _expires(ctx.now, lease_minutes),
                    "from_session": from_session,
                    "from_machine": lease["machine"],
                    "user_source": dict(user_source),
                },
            )
        ]

    return Command(
        actor=actor,
        input={
            "op": "lease.takeover",
            "session_id": session_id,
            "machine": machine,
            "from_session": from_session,
            "from_machine": from_machine,
            "user_source": dict(user_source),
        },
        build=build,
        command_id=command_id,
    )


# ---------------------------------------------------------------------- reducers


def _hold(conn: sqlite3.Connection, event: Event) -> None:
    data = event.data
    conn.execute(
        "INSERT INTO leases(name, holder_session, machine, acquired_at, expires_at, source_seq) "
        "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
        "holder_session = excluded.holder_session, machine = excluded.machine, "
        "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at, "
        "source_seq = excluded.source_seq",
        (
            LEASE_NAME,
            data["session_id"],
            data["machine"],
            event.time,
            data["expires_at"],
            event.sequence,
        ),
    )


def reduce_released(conn: sqlite3.Connection, event: Event) -> None:
    conn.execute(
        "UPDATE leases SET holder_session = NULL, machine = NULL, expires_at = NULL, "
        "source_seq = ? WHERE name = ?",
        (event.sequence, LEASE_NAME),
    )


reduce_acquired = _hold
reduce_taken_over = _hold
