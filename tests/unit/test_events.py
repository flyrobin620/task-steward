from __future__ import annotations

import json

import pytest

from tsw.errors import ErrorCode, TswError
from tsw.events import GENESIS, SCHEMA, Event, EventDraft, canonical_bytes, digest, seal

pytestmark = pytest.mark.use_case("UC-LEDGER-001")


def _sealed(sequence: int = 1, prev: str = GENESIS, **data: object) -> Event:
    return seal(
        EventDraft(type="task.note", task_id="T001", revision=1, data=data or {"text": "x"}),
        sequence=sequence,
        prev=prev,
        time="2026-11-05T00:00:00Z",
        actor={"role": "cli"},
        command_id="c1",
        command_digest=digest({"op": "x"}),
    )


def test_canonical_bytes_sorts_keys_and_keeps_unicode() -> None:
    assert canonical_bytes({"b": 1, "a": "任务"}) == '{"a":"任务","b":1}'.encode()


def test_canonical_bytes_rejects_nan() -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})


def test_sealed_event_round_trips_through_a_line() -> None:
    event = _sealed()
    line = event.to_line()
    assert line.endswith(b"\n")
    assert Event.from_line(line.rstrip(b"\n")) == event
    assert event.checksum == event.computed_checksum()


def test_chain_links_prev_to_previous_checksum() -> None:
    first = _sealed()
    second = _sealed(sequence=2, prev=first.checksum)
    assert second.prev == first.checksum
    assert second.checksum != first.checksum


def test_tampered_line_is_rejected() -> None:
    raw = json.loads(_sealed().to_line())
    raw["data"]["text"] = "changed"
    with pytest.raises(TswError) as info:
        Event.from_dict(raw)
    assert info.value.code is ErrorCode.REJECTED
    assert info.value.details["reason"] == "ledger_corrupt"


def test_unsupported_schema_is_rejected_before_use() -> None:
    raw = json.loads(_sealed().to_line())
    raw["schema"] = SCHEMA + 1
    with pytest.raises(TswError) as info:
        Event.from_dict(raw)
    assert info.value.details["reason"] == "unsupported_schema"


@pytest.mark.parametrize("line", [b"not json", b"[1,2]", b'{"schema":1}'])
def test_malformed_lines_are_reported_as_corrupt(line: bytes) -> None:
    with pytest.raises(TswError) as info:
        Event.from_line(line)
    assert info.value.details["reason"] == "ledger_corrupt"


def test_optional_fields_are_omitted_from_the_body() -> None:
    event = seal(
        EventDraft(type="task.note"),
        sequence=1,
        prev=GENESIS,
        time="2026-11-05T00:00:00Z",
        actor={"role": "cli"},
        command_id=None,
        command_digest=None,
    )
    body = event.body()
    for key in ("task_id", "revision", "attempt", "command_id", "command_digest"):
        assert key not in body


def test_unknown_optional_fields_from_a_newer_writer_are_kept_and_verified() -> None:
    body = _sealed().body()
    body["added_by_a_newer_writer"] = {"x": 1}
    raw = {**body, "checksum": digest(body)}
    event = Event.from_dict(raw)
    assert event.extra == {"added_by_a_newer_writer": {"x": 1}}
    assert json.loads(event.to_line()) == raw
    raw["added_by_a_newer_writer"] = {"x": 2}
    with pytest.raises(TswError) as info:
        Event.from_dict(raw)
    assert info.value.details["reason"] == "ledger_corrupt"
