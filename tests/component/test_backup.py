from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

import pytest

from tests.support import TEST_ACTOR, register_task
from tsw.errors import ErrorCode, TswError
from tsw.home import Home, open_home
from tsw.ledger import Ledger
from tsw.storage.fs import MANIFEST, FsStorage, restore, verify_manifest
from tsw.tasks import note, task_row

pytestmark = pytest.mark.use_case("UC-STORE-001")


def test_quiesce_blocks_writes_until_resumed(home: Home, ledger: Ledger) -> None:
    task_id = register_task(ledger)
    storage = FsStorage(home)
    manifest = storage.quiesce()
    assert manifest["ledger_tail"]["sequence"] == 1
    with pytest.raises(TswError) as info:
        ledger.commit(note(task_id, "x", actor=TEST_ACTOR))
    assert info.value.code is ErrorCode.QUIESCED
    with ledger.view() as conn:  # reads keep working
        assert task_row(conn, task_id)["version"] == 1
    storage.resume_writes()
    assert not (home.root / MANIFEST).exists()
    ledger.commit(note(task_id, "x", actor=TEST_ACTOR))


def test_quiesce_waits_for_a_writer_that_was_already_admitted(home: Home) -> None:
    storage = FsStorage(home)
    admitted = threading.Event()
    release = threading.Event()
    finished: list[float] = []

    def writer() -> None:
        with storage.writer():
            admitted.set()
            release.wait(10)
            finished.append(time.monotonic())

    thread = threading.Thread(target=writer)
    thread.start()
    assert admitted.wait(10)
    quiesced: list[float] = []
    quiescer = threading.Thread(
        target=lambda: (storage.quiesce(), quiesced.append(time.monotonic()))
    )
    quiescer.start()
    deadline = time.monotonic() + 10
    while not storage.is_quiesced() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert storage.is_quiesced()
    assert not quiesced  # still waiting for the admitted writer
    release.set()
    thread.join(10)
    quiescer.join(10)
    assert finished and quiesced and quiesced[0] >= finished[0]
    with pytest.raises(TswError) as info, storage.writer():
        pass
    assert info.value.code is ErrorCode.QUIESCED


def test_online_backup_and_restore_round_trip(home: Home, ledger: Ledger, tmp_path: Path) -> None:
    task_id = register_task(ledger)
    ledger.commit(note(task_id, "备份前", actor=TEST_ACTOR))
    (home.root / "work" / "draft.md").write_text("草稿", encoding="utf-8")
    manifest = FsStorage(home).snapshot(tmp_path / "backup")
    assert manifest["ledger_tail"]["sequence"] == 2
    assert not (tmp_path / "backup" / "cache").exists()
    assert verify_manifest(tmp_path / "backup") == []
    problems = restore(tmp_path / "backup", tmp_path / "restored")
    assert problems == []
    restored = Ledger(open_home(tmp_path / "restored"))
    assert restored.audit()["ok"]
    restored.rebuild()
    with restored.view() as conn:
        assert [n["text"] for n in task_row(conn, task_id)["notes"]] == ["备份前"]
    assert (tmp_path / "restored" / "work" / "draft.md").read_text(encoding="utf-8") == "草稿"


def test_cold_copy_after_quiesce_is_a_verified_backup(
    home: Home, ledger: Ledger, tmp_path: Path
) -> None:
    register_task(ledger)
    storage = FsStorage(home)
    storage.quiesce()
    shutil.copytree(home.root, tmp_path / "copy")
    assert verify_manifest(tmp_path / "copy") == []
    storage.resume_writes()


def test_plain_copy_is_unverified_unless_explicitly_allowed(
    home: Home, ledger: Ledger, tmp_path: Path
) -> None:
    register_task(ledger)
    shutil.copytree(home.root, tmp_path / "copy")
    with pytest.raises(TswError) as info:
        restore(tmp_path / "copy", tmp_path / "restored")
    assert "缺少 backup-manifest.json" in info.value.details["problems"]
    problems = restore(tmp_path / "copy", tmp_path / "restored", allow_unverified=True)
    assert problems == ["缺少 backup-manifest.json"]
    assert Ledger(open_home(tmp_path / "restored")).audit()["ok"]


def test_stale_manifest_is_detected(home: Home, ledger: Ledger, tmp_path: Path) -> None:
    task_id = register_task(ledger)
    FsStorage(home).snapshot(tmp_path / "backup")
    ledger.commit(note(task_id, "之后", actor=TEST_ACTOR))
    shutil.copytree(home.ledger_dir, tmp_path / "backup" / "ledger" / "events", dirs_exist_ok=True)
    assert "账本末端与 manifest 不一致" in verify_manifest(tmp_path / "backup")


def test_backup_and_restore_refuse_non_empty_targets(home: Home, tmp_path: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "x").write_text("x", encoding="utf-8")
    with pytest.raises(TswError):
        FsStorage(home).snapshot(target)
    FsStorage(home).snapshot(tmp_path / "backup")
    with pytest.raises(TswError):
        restore(tmp_path / "backup", target)
