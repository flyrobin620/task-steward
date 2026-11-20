"""The orchestrator changes and reverts the unified configuration through tsw (DN003 §7), and runs
the data-directory maintenance the user opened to it (DN006 §9.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.import_fixture import build
from tests.machine_harness import MACHINE, Harness
from tsw import confedit, lease
from tsw.config import load_config
from tsw.errors import TswError
from tsw.faults import ENV as FAULTS_ENV
from tsw.faults import FaultInjected
from tsw.home import Home
from tsw.host import maintenance as maintenance_mod
from tsw.host.maintenance import Maintenance
from tsw.machine.base import Caller

CONFIG = "config/task-steward.json"


def file_values(home: Home) -> dict[str, Any]:
    return json.loads(home.path(CONFIG).read_text(encoding="utf-8"))


def changes(h: Harness) -> list[dict[str, Any]]:
    return [confedit.summary(e) for e in confedit.change_events(h.ledger.storage)]


def authorize(h: Harness, text: str = "同意开启推送") -> dict[str, Any]:
    return {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": f"orch-1:msg-{text}",
        "text": text,
        "target": ["config", confedit.text_sha(confedit.read_text(h.home)), "authorize"],
    }


def rejection(exc: pytest.ExceptionInfo[TswError]) -> str:
    return str(exc.value.details.get("reason"))


pytestmark = pytest.mark.use_case("UC-CONFIG-002")


def test_an_ordinary_change_is_validated_recorded_and_written(home: Home) -> None:
    h = Harness(home)
    before = confedit.read_text(home)
    out = h.run(
        "config_change",
        ops=[{"op": "set", "path": "scheduling.max_workers", "value": 3}],
        reason="用户要求同时最多 3 个 worker",
    )
    event = out["events"][0]
    assert event["type"] == "config.changed"
    assert event["data"]["change_id"] == "C001"
    assert event["data"]["changed"] == ["scheduling.max_workers"]
    assert event["data"]["permission"] == []
    assert event["data"]["before"] == before
    assert file_values(home)["scheduling"] == {"max_workers": 3}
    assert load_config(home).get("scheduling.max_workers") == 3
    assert event["data"]["after_sha256"] == confedit.text_sha(confedit.read_text(home))
    assert [c["change_id"] for c in changes(h)] == ["C001"]


def test_an_invalid_result_is_refused_and_nothing_is_written(home: Home) -> None:
    h = Harness(home)
    before = confedit.read_text(home)
    with pytest.raises(TswError) as exc:
        h.run(
            "config_change",
            ops=[{"op": "set", "path": "roles.worker.effort", "value": "huge"}],
            reason="试错",
        )
    assert rejection(exc) == "config_invalid"
    assert confedit.read_text(home) == before
    assert changes(h) == []
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=[{"op": "unset", "path": "nope.x"}], reason="r")
    assert rejection(exc) == "no_such_key"


def test_permission_keys_need_the_users_own_words_for_this_change(home: Home) -> None:
    h = Harness(home)
    ops = [{"op": "set", "path": "repositories.demo.delivery", "value": {"push": True}}]
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=ops, reason="开启推送")
    assert rejection(exc) == "g_user"
    stale = {**authorize(h), "target": ["config", "0" * 64, "authorize"]}
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=ops, reason="开启推送", user_source=stale)
    assert rejection(exc) == "g_user"
    worker_words = {**authorize(h), "channel": "in-session"}
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=ops, reason="开启推送", user_source=worker_words)
    assert rejection(exc) == "g_user"
    assert changes(h) == []

    out = h.run("config_change", ops=ops, reason="开启推送", user_source=authorize(h))
    data = out["events"][0]["data"]
    assert data["permission"] == ["repositories.demo.delivery.push"]
    assert data["user_source"]["text"] == "同意开启推送"
    assert load_config(home).get("repositories.demo.delivery.push") is True

    # Narrowing is a permission change too, and adding or removing a repository is one.
    for op in (
        {"op": "set", "path": "repositories.demo.delivery.push", "value": False},
        {"op": "set", "path": "repositories.extra", "value": {"path": "~/src/extra"}},
        {"op": "set", "path": "review.max_rounds", "value": 5},
        {"op": "set", "path": "roles.reviewer.timeout_seconds", "value": 60},
    ):
        with pytest.raises(TswError) as exc:
            h.run("config_change", ops=[op], reason="r")
        assert rejection(exc) == "g_user", op


def test_a_worker_cannot_change_the_configuration(home: Home) -> None:
    h = Harness(home)
    worker = Caller(role="worker", session_id="w-1", machine=MACHINE)
    with pytest.raises(TswError) as exc:
        h.run("config_change", worker, ops=[{"op": "set", "path": "x", "value": 1}], reason="r")
    assert rejection(exc) == "role_denied"
    other = Caller(role="orchestrator", session_id="orch-2", machine=MACHINE)
    with pytest.raises(TswError) as exc:
        h.run("config_change", other, ops=[{"op": "unset", "path": "version"}], reason="r")
    assert rejection(exc) == "lease_held"


def test_revert_restores_the_text_before_a_change_and_is_itself_recorded(home: Home) -> None:
    h = Harness(home)
    original = confedit.read_text(home)
    h.run(
        "config_change",
        ops=[{"op": "set", "path": "sessions.archive_on_accept", "value": True}],
        reason="a",
    )
    h.run(
        "config_change",
        ops=[{"op": "set", "path": "repositories.demo.delivery", "value": {"push": True}}],
        reason="b",
        user_source=authorize(h),
    )
    # Going back past C002 withdraws a permission: the user's words are needed again.
    with pytest.raises(TswError) as exc:
        h.run("config_revert", to_change="C001", reason="撤销")
    assert rejection(exc) == "g_user"
    out = h.run("config_revert", to_change="C001", reason="撤销", user_source=authorize(h, "撤销"))
    data = out["events"][0]["data"]
    assert (data["change_id"], data["kind"], data["reverts"]) == ("C003", "revert", "C001")
    assert confedit.read_text(home) == original
    # The revert is a change like any other and can be reverted in turn.
    h.run("config_revert", to_change="C003", reason="恢复", user_source=authorize(h, "恢复"))
    assert load_config(home).get("repositories.demo.delivery.push") is True
    with pytest.raises(TswError) as exc:
        h.run("config_revert", to_change="C999", reason="r")
    assert rejection(exc) == "no_such_change"


def test_one_user_message_authorizes_one_change(home: Home) -> None:
    h = Harness(home)
    ops = [{"op": "set", "path": "repositories.demo.delivery", "value": {"push": True}}]
    first = authorize(h, "同意推送")
    h.run("config_change", ops=ops, reason="开启", user_source=first, command_id="c-1")
    # A retry of the same command is a duplicate, not a second use of the words.
    again = h.run("config_change", ops=ops, reason="开启", user_source=first, command_id="c-1")
    assert again["duplicate"] is True
    h.run("config_revert", to_change="C001", reason="撤销", user_source=authorize(h, "撤销推送"))
    # The file is back to what the first words were bound to; they still cannot be replayed.
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=ops, reason="再次开启", user_source=first)
    assert rejection(exc) == "source_reused"
    assert load_config(home).get("repositories.demo.delivery.push") is None


def test_a_dotted_repository_name_does_not_hide_a_permission_key(home: Home) -> None:
    h = Harness(home)
    add = [{"op": "set", "path": ["repositories", "my.repo"], "value": {"path": "~/src/my"}}]
    h.run("config_change", ops=add, reason="新仓库", user_source=authorize(h, "加仓库"))
    push = [{"op": "set", "path": ["repositories", "my.repo", "delivery", "push"], "value": True}]
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=push, reason="开启推送")
    assert rejection(exc) == "g_user"
    out = h.run("config_change", ops=push, reason="开启推送", user_source=authorize(h, "推送"))
    assert out["events"][0]["data"]["changed_keys"] == [
        ["repositories", "my.repo", "delivery", "push"]
    ]


def test_a_rebuild_does_not_write_a_finished_change_over_a_hand_restore(home: Home) -> None:
    h = Harness(home)
    original = confedit.read_text(home)
    h.run(
        "config_change",
        ops=[{"op": "set", "path": "scheduling.max_workers", "value": 2}],
        reason="r",
    )
    assert confedit.read_text(home) != original
    assert original is not None
    home.path(CONFIG).write_text(original, encoding="utf-8", newline="")
    h.ledger.rebuild()
    h.register("T001")
    assert confedit.read_text(home) == original


def test_a_hand_edit_is_kept_as_the_base_of_the_next_change(home: Home) -> None:
    h = Harness(home)
    values = file_values(home)
    values["sessions"] = {"archive_on_accept": True}
    home.path(CONFIG).write_text(json.dumps(values), encoding="utf-8")
    hand = confedit.read_text(home)
    out = h.run(
        "config_change",
        ops=[{"op": "set", "path": "scheduling.max_workers", "value": 2}],
        reason="r",
        expected_sha256=confedit.text_sha(hand),
    )
    assert out["events"][0]["data"]["before"] == hand
    assert file_values(home)["sessions"] == {"archive_on_accept": True}
    with pytest.raises(TswError) as exc:
        h.run(
            "config_change",
            ops=[{"op": "set", "path": "scheduling.max_workers", "value": 4}],
            reason="r",
            expected_sha256=confedit.text_sha(hand),
        )
    assert rejection(exc) == "config_conflict"


def test_an_invalid_configuration_can_be_repaired_through_the_tool(home: Home) -> None:
    h = Harness(home)
    home.path(CONFIG).write_text("{ not json", encoding="utf-8")
    assert not load_config(home).valid
    with pytest.raises(TswError) as exc:
        h.run("config_change", ops=[{"op": "set", "path": "x", "value": 1}], reason="r")
    assert rejection(exc) == "config_unreadable"
    repaired = {"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}
    words = {
        **authorize(h, "按原样修复"),
        "target": ["config", confedit.text_sha("{ not json"), "authorize"],
    }
    h.run(
        "config_change",
        ops=[{"op": "set", "path": "", "value": repaired}],
        reason="修复手改坏的文件",
        user_source=words,
    )
    assert load_config(home).valid


@pytest.mark.parametrize("hand_edit", [False, True])
def test_an_interrupted_write_is_finished_by_the_next_commit(
    home: Home, monkeypatch: pytest.MonkeyPatch, hand_edit: bool
) -> None:
    """Fault injection: the event is appended, the file write never happens."""
    h = Harness(home)
    before = confedit.read_text(home)
    monkeypatch.setenv(FAULTS_ENV, "ledger.commit.after_append")
    with pytest.raises(FaultInjected):
        h.run(
            "config_change",
            ops=[{"op": "set", "path": "scheduling.max_workers", "value": 2}],
            reason="r",
        )
    monkeypatch.delenv(FAULTS_ENV)
    assert confedit.read_text(home) == before
    if hand_edit:
        home.path(CONFIG).write_text(json.dumps({"version": 1}), encoding="utf-8")
    h.register("T001")  # any later commit finishes derived files
    if hand_edit:
        assert file_values(home) == {"version": 1}
    else:
        assert file_values(home)["scheduling"] == {"max_workers": 2}
    # A rebuild does not write the change again over a later state.
    home.path(CONFIG).write_text(json.dumps(file_values(home)), encoding="utf-8")
    kept = confedit.read_text(home)
    h.ledger.rebuild()
    assert confedit.read_text(home) == kept


# ---------------------------------------------------------------------- maintenance


@pytest.mark.use_case("UC-STORE-003")
def test_backup_audit_and_rebuild_through_maintenance(home: Home, tmp_path: Path) -> None:
    h = Harness(home)
    tools = Maintenance(home, h.ledger, "orch-1", MACHINE)
    with pytest.raises(TswError) as exc:
        tools.backup(str(home.root / "backups" / "b1"))
    assert rejection(exc) == "backup_inside_home"
    with pytest.raises(TswError) as exc:
        tools.backup("relative/dir")
    assert rejection(exc) == "invalid_input"
    out = tools.backup(str(tmp_path / "b1"))
    assert (tmp_path / "b1" / "backup-manifest.json").is_file()
    assert out["files"] > 0
    assert tools.audit()["ok"]
    rebuilt = tools.rebuild()
    assert rebuilt["kind"] == "ledger_rebuild"
    kinds = [
        e.data.get("kind") for e in h.ledger.storage.read() if e.type == "maintenance.performed"
    ]
    assert kinds == ["backup", "ledger_rebuild"]
    stranger = Maintenance(home, h.ledger, "orch-2", MACHINE)
    with pytest.raises(TswError) as exc:
        stranger.backup(str(tmp_path / "b2"))
    assert rejection(exc) == "lease_held"


@pytest.mark.use_case("UC-STORE-003")
def test_a_real_import_needs_the_users_words_and_a_matching_dry_run(
    home: Home, tmp_path: Path
) -> None:
    h = Harness(home)
    _, snapshot = build(tmp_path)
    tools = Maintenance(home, h.ledger, "orch-1", MACHINE)
    words = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "orch-1:msg-9",
        "text": "可以正式导入",
        "target": ["import", "orchestrator-agent", "authorize"],
    }
    with pytest.raises(TswError) as exc:
        tools.import_orchestrator_agent({"snapshot": str(snapshot)})
    assert rejection(exc) == "g_user"
    with pytest.raises(TswError) as exc:
        tools.import_orchestrator_agent({"snapshot": str(snapshot), "user_source": words})
    assert rejection(exc) == "dry_run_required"
    dry = tools.import_orchestrator_agent({"snapshot": str(snapshot), "dry_run": True})
    assert not any(e.type == "import.started" for e in h.ledger.storage.read())
    with pytest.raises(TswError) as exc:  # different arguments: the dry run does not count
        tools.import_orchestrator_agent(
            {"snapshot": str(snapshot), "exclude": ["T009"], "user_source": words}
        )
    assert rejection(exc) == "dry_run_required"
    tools.import_orchestrator_agent({"snapshot": str(snapshot), "user_source": words})
    assert any(e.type == "import.completed" for e in h.ledger.storage.read())
    assert dry["params_digest"]
    # After an import, a new real run needs a new dry run, and new words.
    with pytest.raises(TswError) as exc:
        tools.import_orchestrator_agent({"snapshot": str(snapshot), "user_source": words})
    assert rejection(exc) == "dry_run_required"
    tools.import_orchestrator_agent({"snapshot": str(snapshot), "dry_run": True})
    with pytest.raises(TswError) as exc:
        tools.import_orchestrator_agent({"snapshot": str(snapshot), "user_source": words})
    assert rejection(exc) == "source_reused"


@pytest.mark.use_case("UC-STORE-003")
def test_an_import_rechecks_the_lease_under_the_import_lock(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease is lost between the first check and the import lock: nothing is imported."""
    h = Harness(home)
    _, snapshot = build(tmp_path)
    holder = Maintenance(home, h.ledger, "orch-1", MACHINE)
    holder.import_orchestrator_agent({"snapshot": str(snapshot), "dry_run": True})
    late = Maintenance(home, h.ledger, "orch-2", MACHINE)
    monkeypatch.setattr(late, "_require_lease", lambda: None)  # passed before the lease moved
    words = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "orch-2:msg-1",
        "text": "导入",
        "target": ["import", "orchestrator-agent", "authorize"],
    }
    with pytest.raises(TswError) as exc:
        late.import_orchestrator_agent({"snapshot": str(snapshot), "user_source": words})
    assert rejection(exc) == "lease_held"
    assert not any(e.type.startswith("import.") for e in h.ledger.storage.read())


@pytest.mark.use_case("UC-STORE-003")
def test_files_are_not_copied_after_the_lease_moved(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease moves after the import was authorized, before the history files are copied."""
    h = Harness(home)
    _, snapshot = build(tmp_path)
    tools = Maintenance(home, h.ledger, "orch-1", MACHINE)
    tools.import_orchestrator_agent({"snapshot": str(snapshot), "dry_run": True})
    real = maintenance_mod.run_import

    def lease_moves(*args: Any, on_locked: Any, **kwargs: Any) -> Any:
        on_locked()
        h.ledger.commit(lease.release("orch-1", machine=MACHINE, actor={"role": "orchestrator"}))
        h.ledger.commit(
            lease.acquire(
                "orch-2", machine=MACHINE, lease_minutes=60, actor={"role": "orchestrator"}
            )
        )
        return real(*args, on_locked=None, **kwargs)

    monkeypatch.setattr(maintenance_mod, "run_import", lease_moves)
    words = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "orch-1:msg-late",
        "text": "导入",
        "target": ["import", "orchestrator-agent", "authorize"],
    }
    with pytest.raises(TswError) as exc:
        tools.import_orchestrator_agent({"snapshot": str(snapshot), "user_source": words})
    assert rejection(exc) == "lease_held"
    assert not home.path("legacy").exists() or not any(home.path("legacy").iterdir())
    assert not any(e.type == "import.started" for e in h.ledger.storage.read())
