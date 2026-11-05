from __future__ import annotations

import gzip
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support import TEST_ACTOR, register_task
from tsw.errors import TswError
from tsw.home import Home, init_home
from tsw.ledger import Ledger
from tsw.storage.fs import FsStorage
from tsw.tasks import note


def _ledger(
    home: Home, segment_max_bytes: int = 2048, month: Iterator[str] | None = None
) -> Ledger:
    clock = (lambda: next(month)) if month is not None else (lambda: "2026-11-05T00:00:00Z")
    return Ledger(home, FsStorage(home, segment_max_bytes=segment_max_bytes), clock=clock)


def _fill(ledger: Ledger, notes: int) -> str:
    task_id = register_task(ledger)
    for index in range(notes):
        ledger.commit(note(task_id, f"备注 {index} " + "x" * 200, actor=TEST_ACTOR))
    return task_id


@pytest.mark.use_case("UC-LEDGER-002")
def test_segments_seal_by_size_and_read_across_them(home: Home) -> None:
    ledger = _ledger(home)
    _fill(ledger, 20)
    storage = FsStorage(home)
    segments = storage.segments()
    assert len(segments) > 2
    assert all(seg.sealed for seg in segments[:-1])
    assert not segments[-1].sealed
    assert [e.sequence for e in storage.read()] == list(range(1, 22))
    assert [e.sequence for e in storage.read(since=15)] == list(range(16, 22))
    assert ledger.audit()["ok"]


@pytest.mark.use_case("UC-LEDGER-002")
def test_month_change_starts_a_new_segment(home: Home) -> None:
    times = iter(["2026-10-31T23:59:59Z", "2026-11-01T00:00:00Z"])
    ledger = _ledger(home, segment_max_bytes=1 << 20, month=times)
    _fill(ledger, 1)
    names = [seg.path.name for seg in FsStorage(home).segments()]
    assert names == ["2026-10-0000000001.jsonl.gz", "2026-11-0000000002.jsonl"]


@pytest.mark.use_case("UC-LEDGER-002")
def test_interrupted_seal_is_recovered_on_next_append(home: Home) -> None:
    ledger = _ledger(home)
    task_id = _fill(ledger, 20)
    storage = FsStorage(home)
    sealed = storage.segments()[0]
    source = sealed.path.with_name(sealed.path.name.removesuffix(".gz"))
    source.write_bytes(gzip.decompress(sealed.path.read_bytes()))  # as if unlink never ran
    assert [e.sequence for e in storage.read()] == list(range(1, 22))  # still readable
    ledger.commit(note(task_id, "继续", actor=TEST_ACTOR))
    assert not source.exists()
    assert ledger.audit()["ok"]


@pytest.mark.use_case("UC-LEDGER-002")
def test_audit_detects_tampering_in_a_sealed_segment(home: Home) -> None:
    ledger = _ledger(home)
    _fill(ledger, 20)
    sealed = FsStorage(home).segments()[0]
    text = gzip.decompress(sealed.path.read_bytes()).replace("备注 0".encode(), "篡改 0".encode())
    sealed.path.write_bytes(gzip.compress(text, mtime=0))
    report = ledger.audit()
    assert not report["ok"]
    assert "读取账本失败" in report["problems"][0]


@pytest.mark.use_case("UC-LEDGER-002")
def test_audit_detects_a_removed_event(home: Home) -> None:
    ledger = _ledger(home, segment_max_bytes=1 << 20)
    _fill(ledger, 3)
    active = FsStorage(home).segments()[-1].path
    lines = active.read_bytes().splitlines(keepends=True)
    active.write_bytes(lines[0] + b"".join(lines[2:]))
    report = ledger.audit()
    assert not report["ok"]
    with pytest.raises(TswError):
        _fill(ledger, 1)  # writers refuse to build on a broken chain


@pytest.mark.use_case("UC-LEDGER-002")
def test_audit_reports_projection_mismatch(home: Home, tmp_path: Path) -> None:
    ledger = _ledger(home, segment_max_bytes=1 << 20)
    _fill(ledger, 2)
    other = Home(tmp_path / "other")
    init_home(other.root)
    other_ledger = _ledger(other, segment_max_bytes=1 << 20)
    register_task(other_ledger, title="另一个数据目录")
    ledger.projection.path.write_bytes(other_ledger.projection.path.read_bytes())
    report = ledger.audit()
    assert not report["ok"]
    assert report["problems"] == ["投影记录的摘要与账本不一致"]
