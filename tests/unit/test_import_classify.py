"""DN002 §3.4 / DN005 §3: the import rules, in order, first match wins."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tsw.errors import TswError
from tsw.importer.orchestrator_agent import classify, load_snapshot, source_lease

pytestmark = pytest.mark.use_case("UC-IMPORT-001")

CASES: list[tuple[str, dict[str, Any], dict[str, Any], tuple[str, int]]] = [
    ("I01", {"status": "paused", "retirement": {"reason": "x"}}, {}, ("archived", 1)),
    ("I01", {"status": "done"}, {"phase": "archived"}, ("archived", 1)),
    ("I02", {"status": "cancelled"}, {}, ("cancelled", 2)),
    ("I03", {"status": "done"}, {"user_acceptance_valid": True}, ("accepted", 3)),
    ("I04", {"status": "done"}, {"user_acceptance_valid": False}, ("done", 4)),
    ("I05", {"status": "pending"}, {}, ("intake", 5)),
    ("I05", {"status": "ready"}, {}, ("intake", 5)),
    ("I06", {"status": "paused"}, {}, ("paused", 6)),
    ("I06", {"status": "failed"}, {}, ("paused", 6)),
    ("I06", {"status": "running"}, {}, ("paused", 7)),
    ("I06", {"status": "integrating"}, {}, ("paused", 7)),
]


@pytest.mark.parametrize(
    ("task", "derived", "expected"),
    [
        pytest.param(task, derived, expected, marks=pytest.mark.transition(rule))
        for rule, task, derived, expected in CASES
    ],
)
def test_import_rules_first_match_wins(
    task: dict[str, Any], derived: dict[str, Any], expected: tuple[str, int]
) -> None:
    assert classify(task, derived) == expected


@pytest.mark.parametrize("payload", ["[]", '"text"', "null", '{"schema": 99}'])
def test_a_snapshot_of_the_wrong_shape_is_refused_cleanly(tmp_path: Path, payload: str) -> None:
    (tmp_path / "snapshot.json").write_text(payload, encoding="utf-8")
    with pytest.raises(TswError) as caught:
        load_snapshot(tmp_path)
    assert caught.value.details["reason"] == "snapshot_schema"


NOW = datetime(2026, 11, 6, 12, 0, 0, 100_000, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("controller", "state"),
    [
        (None, "released"),
        ({"holder": None, "expires_at": "2026-11-06T13:00:00Z"}, "released"),
        ({"holder": "codex:x", "expires_at": "2026-11-04T15:27:24+00:00"}, "expired"),
        ({"session": "x", "expires_at": "2026-11-06T11:59:59Z"}, "expired"),  # session only
        ({"holder": "codex:x", "expires_at": "2026-11-06T12:00:00.100Z"}, "expired"),  # equal
        ({"holder": "codex:x", "expires_at": "2026-11-06T12:00:00.900Z"}, "active"),  # same second
        ({"holder": "codex:x", "expires_at": "2026-11-06T19:30:00+08:00"}, "expired"),  # offset
        ({"holder": "codex:x", "expires_at": "2026-11-06T21:00:00+08:00"}, "active"),
        ({"holder": "codex:x"}, "active"),  # no expiry: assume it may still dispatch
        ({"holder": "codex:x", "expires_at": "soon"}, "active"),
    ],
)
def test_the_source_lease_is_judged_with_full_precision(
    controller: dict[str, Any] | None, state: str
) -> None:
    assert source_lease({"controller": controller}, NOW)["state"] == state
