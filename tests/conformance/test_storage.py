"""Conformance suite for P4 storage backends (DN006 §5.2, §9.3).

Every storage implementation is added to ``BACKENDS`` and must pass the whole suite.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tsw.errors import ErrorCode, TswError
from tsw.events import GENESIS, Event, EventDraft, seal
from tsw.home import Home, init_home
from tsw.storage.base import PLUGIN_API, StorageBackend
from tsw.storage.fs import FsStorage

pytestmark = pytest.mark.use_case("UC-STORE-002")

BACKENDS: dict[str, Callable[[Home], StorageBackend]] = {
    "fs": lambda home: FsStorage(home, lock_timeout=0.3),
}


@pytest.fixture(params=sorted(BACKENDS))
def backend(request: pytest.FixtureRequest, home: Home) -> StorageBackend:
    return BACKENDS[request.param](home)


def _events(start: int, prev: str, count: int) -> list[Event]:
    out: list[Event] = []
    for offset in range(count):
        event = seal(
            EventDraft(type="task.note", task_id="T001", data={"n": start + offset}),
            sequence=start + offset,
            prev=prev,
            time="2026-11-05T00:00:00Z",
            actor={"role": "cli"},
            command_id=None,
            command_digest=None,
        )
        out.append(event)
        prev = event.checksum
    return out


def _code(exc: pytest.ExceptionInfo[TswError]) -> ErrorCode:
    return exc.value.code


def test_declares_capabilities(backend: StorageBackend) -> None:
    caps = backend.capabilities()
    assert caps["plugin_api"] == PLUGIN_API
    assert isinstance(caps["name"], str)


def test_empty_tail_is_genesis(backend: StorageBackend) -> None:
    tail = backend.tail()
    assert (tail.sequence, tail.checksum) == (0, GENESIS)
    assert list(backend.read()) == []


def test_append_and_read_in_order(backend: StorageBackend) -> None:
    first = _events(1, GENESIS, 3)
    assert backend.append(first, expected_sequence=0) == 3
    second = _events(4, first[-1].checksum, 2)
    backend.append(second, expected_sequence=3)
    assert [e.sequence for e in backend.read()] == [1, 2, 3, 4, 5]
    assert [e.sequence for e in backend.read(since=3)] == [4, 5]
    assert backend.tail().checksum == second[-1].checksum


def test_stale_expected_sequence_is_rejected_without_writing(backend: StorageBackend) -> None:
    backend.append(_events(1, GENESIS, 1), expected_sequence=0)
    with pytest.raises(TswError) as info:
        backend.append(_events(1, GENESIS, 1), expected_sequence=0)
    assert _code(info) is ErrorCode.REJECTED
    assert info.value.details["reason"] == "sequence_conflict"
    assert backend.tail().sequence == 1


def test_broken_chain_is_rejected(backend: StorageBackend) -> None:
    with pytest.raises(TswError) as info:
        backend.append(_events(1, "f" * 64, 1), expected_sequence=0)
    assert info.value.details["reason"] == "invalid_events"
    gap = _events(2, GENESIS, 1)
    with pytest.raises(TswError):
        backend.append(gap, expected_sequence=0)
    assert backend.tail().sequence == 0


def test_artifacts_are_confined_to_tsw_directories(backend: StorageBackend) -> None:
    digest = backend.put_artifact("runs/T001/a1/result.md", "报告".encode())
    assert len(digest) == 64
    assert backend.read_artifact("runs/T001/a1/result.md") == "报告".encode()
    for bad in ("../escape.txt", "/abs/path", "ledger/events/x.jsonl", "cache/x", ""):
        with pytest.raises(TswError) as info:
            backend.put_artifact(bad, b"x")
        assert _code(info) is ErrorCode.REJECTED


def test_named_lock_is_exclusive(backend: StorageBackend) -> None:
    with backend.lock("lease", ttl=5) as lease:
        assert lease.name == "lease"
        with pytest.raises(TswError) as info, backend.lock("lease", ttl=5):
            pass
        assert _code(info) is ErrorCode.UNAVAILABLE
    with backend.lock("lease", ttl=5):
        pass


def test_writer_admission_reports_quiesced(backend: StorageBackend) -> None:
    with backend.writer():
        pass
    backend.quiesce()
    assert backend.is_quiesced()
    with pytest.raises(TswError) as info, backend.writer():
        pass
    assert _code(info) is ErrorCode.QUIESCED
    backend.resume_writes()
    with backend.writer():
        pass


def test_snapshot_records_the_ledger_tail(backend: StorageBackend, tmp_path: Path) -> None:
    events = _events(1, GENESIS, 2)
    backend.append(events, expected_sequence=0)
    manifest = backend.snapshot(tmp_path / "snapshot")
    assert manifest["ledger_tail"] == {"sequence": 2, "checksum": events[-1].checksum}
    assert all("sha256" not in entry for entry in manifest["files"])
    copy_home, _ = init_home(tmp_path / "snapshot")
    assert [e.sequence for e in FsStorage(copy_home).read()] == [1, 2]
