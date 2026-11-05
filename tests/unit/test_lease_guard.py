from __future__ import annotations

import sqlite3

import pytest

from tsw import lease
from tsw.errors import TswError
from tsw.projection import SCHEMA_SQL

pytestmark = [pytest.mark.use_case("UC-LEASE-001"), pytest.mark.transition("G-LEASE")]


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO leases VALUES('scheduler', 's1', 'm1', '2026-11-05T00:00:00Z', "
        "'2026-11-05T01:00:00Z', 1)"
    )
    return db


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
