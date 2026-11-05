from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support import TEST_ACTOR, register_task
from tsw.errors import ErrorCode, TswError
from tsw.events import Event, EventDraft, seal, utc_now
from tsw.home import Home
from tsw.ledger import Ledger
from tsw.projection import REDUCER_VERSION
from tsw.storage.fs import FsStorage
from tsw.tasks import note, task_row

pytestmark = pytest.mark.use_case("UC-LEDGER-003")


class RecordingStorage(FsStorage):
    """The real fs backend, recording the ``since`` of every read."""

    def __init__(self, home: Home) -> None:
        super().__init__(home)
        self.reads: list[int] = []

    def read(self, since: int = 0) -> Iterator[Event]:
        self.reads.append(since)
        return super().read(since)


def test_checkpoints_restore_a_lost_projection_without_full_replay(home: Home) -> None:
    ledger = Ledger(home, checkpoint_every=4)
    task_id = register_task(ledger)
    for index in range(9):
        ledger.commit(note(task_id, f"n{index}", actor=TEST_ACTOR))
    checkpoints = sorted((home.cache_dir / "checkpoints").glob("*.sqlite"))
    assert [p.name[:10] for p in checkpoints] == ["0000000004", "0000000008"]
    ledger.projection.path.unlink()
    storage = RecordingStorage(home)
    restored = Ledger(home, storage)
    with restored.view() as conn:
        assert task_row(conn, task_id)["version"] == 10
    assert 0 not in storage.reads
    assert 8 in storage.reads


def test_corrupt_checkpoint_falls_back_to_an_older_one_or_full_replay(home: Home) -> None:
    ledger = Ledger(home, checkpoint_every=2)
    task_id = register_task(ledger)
    for index in range(4):
        ledger.commit(note(task_id, f"n{index}", actor=TEST_ACTOR))
    for path in (home.cache_dir / "checkpoints").glob("*.sqlite"):
        path.write_bytes(b"garbage")
    ledger.projection.path.unlink()
    with ledger.view() as conn:
        assert task_row(conn, task_id)["version"] == 5


def test_rebuild_recreates_the_projection(ledger: Ledger) -> None:
    task_id = register_task(ledger)
    ledger.commit(note(task_id, "n", actor=TEST_ACTOR))
    with ledger.view() as conn:
        before = task_row(conn, task_id)
    tail = ledger.rebuild()
    assert tail.sequence == 2
    with ledger.view() as conn:
        assert task_row(conn, task_id) == before


def test_reducer_version_change_requires_rebuild(ledger: Ledger) -> None:
    register_task(ledger)
    with ledger.view() as conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'reducer_version'", (str(REDUCER_VERSION + 1),)
        )
    with pytest.raises(TswError) as info, ledger.view():
        pass
    assert info.value.details["reason"] == "rebuild_required"
    ledger.rebuild()
    with ledger.view():
        pass


def test_unknown_event_type_stops_the_projection(home: Home, ledger: Ledger) -> None:
    register_task(ledger)
    storage = FsStorage(home)
    tail = storage.tail()
    future = seal(
        EventDraft(type="task.from_the_future"),
        sequence=tail.sequence + 1,
        prev=tail.checksum,
        time=utc_now(),
        actor={"role": "cli"},
        command_id=None,
        command_digest=None,
    )
    storage.append([future], expected_sequence=tail.sequence)
    with pytest.raises(TswError) as info, ledger.view():
        pass
    assert info.value.code is ErrorCode.UNSUPPORTED
    assert info.value.details["type"] == "task.from_the_future"


def test_projection_ahead_of_ledger_is_refused(home: Home, ledger: Ledger) -> None:
    register_task(ledger)
    register_task(ledger)
    for path in home.ledger_dir.iterdir():
        path.unlink()
    with pytest.raises(TswError) as info, ledger.view():
        pass
    assert info.value.details["reason"] == "projection_ahead"
