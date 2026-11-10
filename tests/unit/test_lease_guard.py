from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from tsw import lease
from tsw.errors import TswError
from tsw.projection import SCHEMA_SQL

pytestmark = [pytest.mark.use_case("UC-LEASE-001"), pytest.mark.transition("G-LEASE")]


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO leases VALUES('scheduler', 's1', 'm1', '2026-11-05T00:00:00Z', "
        "'2026-11-05T01:00:00Z', 1)"
    )
    yield db
    db.close()


@pytest.mark.parametrize(
    ("session", "machine", "now", "reason"),
    [
        ("s2", "m1", "2026-11-05T00:10:00Z", "lease_held"),
        ("s1", "m2", "2026-11-05T00:10:00Z", "lease_held"),
        ("s1", "m1", "2026-11-05T01:00:00Z", "lease_expired"),
    ],
)
def test_guard_rejections(
    conn: sqlite3.Connection, session: str, machine: str, now: str, reason: str
) -> None:
    with pytest.raises(TswError) as info:
        lease.require_lease(conn, session, machine, now)
    assert info.value.details["reason"] == reason


def test_guard_passes_for_the_holder_before_expiry(conn: sqlite3.Connection) -> None:
    lease.require_lease(conn, "s1", "m1", "2026-11-05T00:59:59Z")


def test_time_round_trip_and_expiry() -> None:
    assert lease.format_time(lease.parse_time("2026-11-05T00:00:00Z")) == "2026-11-05T00:00:00Z"
    row = {"expires_at": "2026-11-05T01:00:00Z"}
    assert not lease.is_expired(row, "2026-11-05T00:59:59Z")
    assert lease.is_expired(row, "2026-11-05T01:00:00Z")


def test_a_stand_down_ends_once_the_session_holds_a_later_lease(conn: sqlite3.Connection) -> None:
    """T254 review r1 F1: a runtime that predates the stand-down acquires without removing the
    record; holding the lease from a later event still counts as having taken it back."""
    conn.execute(
        "INSERT INTO lease_stood_down VALUES('s1', 'm1', 'release', '2026-11-05T00:30:00Z', 2)"
    )
    conn.execute("UPDATE leases SET holder_session = NULL, source_seq = 3")
    assert lease.stood_down(conn, "s1") is not None  # released, nobody holds it
    assert lease.stood_down(conn, "s2") is None and lease.stood_down(conn, None) is None
    conn.execute("UPDATE leases SET holder_session = 's2', machine = 'm1', source_seq = 4")
    assert lease.stood_down(conn, "s1") is not None  # another session holds it
    conn.execute("UPDATE leases SET holder_session = 's1', source_seq = 5")
    assert lease.stood_down(conn, "s1") is None  # taken back by an older runtime
    conn.execute("UPDATE leases SET source_seq = 1")
    assert lease.stood_down(conn, "s1") is not None  # a lease older than the stand-down
