"""Answering in the Dashboard: the pieces without a ledger (DN006 §8.10, CC001 §6.1, T357)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tsw.errors import TswError
from tsw.host import answer, session_inbox, transcript
from tsw.machine.ask_user import MESSAGE_VIA, options

pytestmark = pytest.mark.use_case("UC-DIRECT-013")


def test_the_two_lines_are_the_ones_workdeck_writes_with_the_dashboard_as_sender() -> None:
    lines = session_inbox.frames("tok", "方案 A\n第二行").decode("utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"type": "auth", "token": "tok"},
        {
            "type": "user",
            "from": "tsw-dashboard",
            "priority": "next",
            "message": {"role": "user", "content": "方案 A\n第二行"},
        },
    ]


def test_the_key_file_is_named_after_the_lower_cased_pipe(tmp_path: Path) -> None:
    pipe = r"\\.\pipe\LOCAL\CC-MSG-ABC"
    digest = hashlib.sha256(pipe.lower().encode("utf-8")).hexdigest()
    assert session_inbox.key_file(tmp_path, 12, pipe) == tmp_path / f"12.{digest}.key"


def test_the_session_is_found_by_its_host_id_newest_first(tmp_path: Path) -> None:
    def registry(pid: object, host: str, pipe: str | None, updated: int | None = None) -> object:
        found = {"pid": pid, "hostSessionId": host, "messagingSocketPath": pipe}
        return found if updated is None else {**found, "updatedAt": updated}

    entries = {
        "1.json": registry(1, "local_a", "p1", 5),
        "2.json": registry(2, "local_a", "p2", 9),
        "3.json": registry(3, "local_b", "p3"),
        "4.json": registry("4", "local_a", "p4"),
        "5.json": registry(5, "local_a", None),
    }
    for name, entry in entries.items():
        (tmp_path / name).write_text(json.dumps(entry), encoding="utf-8")
    (tmp_path / "6.json").write_text("{not json", encoding="utf-8")
    found = session_inbox.targets("local_a", tmp_path)
    assert [(t.pid, t.pipe) for t in found] == [(2, "p2"), (1, "p1")]
    assert session_inbox.targets("local_x", tmp_path) == []
    assert session_inbox.targets("local_a", tmp_path / "missing") == []


def test_the_sessions_folder_follows_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert session_inbox.sessions_dir() == tmp_path / "sessions"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert session_inbox.sessions_dir() == Path.home() / ".claude" / "sessions"


def test_without_an_entry_or_a_key_nothing_is_written(tmp_path: Path) -> None:
    assert session_inbox.deliver("local_a", "A", tmp_path).state == "not_running"
    entry = {"pid": 1, "hostSessionId": "local_a", "messagingSocketPath": "no-such-pipe"}
    (tmp_path / "1.json").write_text(json.dumps(entry), encoding="utf-8")
    outcome = session_inbox.deliver("local_a", "A", tmp_path)
    assert outcome.state == "failed" and "key" in outcome.detail


def test_a_message_the_dashboard_delivered_is_named_as_such() -> None:
    assert transcript.via({"kind": "peer", "from": "tsw-dashboard", "verifiedLocal": True}) == (
        "dashboard"
    )
    assert transcript.via({"kind": "peer", "from": "workdeck"}) == "workdeck"
    assert transcript.via({"kind": "peer", "from": "local_x"}) is None
    assert transcript.via({"kind": "human"}) is None
    assert "dashboard" in MESSAGE_VIA


def test_the_answer_in_effect_names_where_it_came_from() -> None:
    worker = {
        "source": {"channel": "in-session", "message_ref": "M-T1-2", "text": "A"},
        "answer": "用 A",
        "source_kind": "user",
    }
    assert answer.effective(worker) == {"by": "worker 会话", "text": "A", "message_ref": "M-T1-2"}
    orchestrator = {"source": None, "answer": "按任务书用 A", "source_kind": "orchestrator"}
    assert answer.effective(orchestrator) == {"by": "主会话代答", "text": "按任务书用 A"}
    teams = {"source": {"channel": "teams-chat", "text": "T1 用 B"}, "answer": "B"}
    assert answer.effective(teams)["by"] == "Teams"
    assert answer.effective(None)["by"] == "主会话代答"


def test_options_are_at_most_six_distinct_short_texts() -> None:
    assert options({}) == []
    assert options({"options": ["方案 A", "方案 B"]}) == ["方案 A", "方案 B"]
    for bad in (["A", "A"], [str(i) for i in range(7)], ["x" * 81], [""], ["A", 1], "A"):
        with pytest.raises(TswError):
            options({"options": bad})
