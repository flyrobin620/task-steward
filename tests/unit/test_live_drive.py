"""What a live run does in the operator's place (tools/live_drive.py, T340): the deep links it
opens, the rules that decide which scripted message goes where and when, and the WebSocket
frames it speaks to workdeck. The live run itself needs the real app and is not run here."""

from __future__ import annotations

import struct
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from tools import live_drive as d


def ev(type_: str, task: str = "", actor: str | None = None, **data: Any) -> dict[str, Any]:
    return {
        "type": type_,
        "task_id": task,
        "actor": {"role": "orchestrator", "session_id": actor} if actor else {"role": "worker"},
        "data": data,
    }


def test_a_deep_link_carries_the_folder_and_the_message_and_nothing_else() -> None:
    folder = Path("C:/runs/live-x/home")
    url = d.deeplink(folder, "执行 T001、T002")
    assert url.startswith("claude://code/new?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert query == {"folder": [str(folder)], "q": ["执行 T001、T002"]}
    # Spaces and separators are escaped, so the app reads the whole folder and message.
    assert " " not in url and "+" not in url
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(d.deeplink(folder)).query) == {
        "folder": [str(folder)]
    }
    # The app drops a link whose message is a slash command.
    with pytest.raises(ValueError):
        d.deeplink(folder, "/clear")


def test_only_claude_links_are_opened() -> None:
    with pytest.raises(ValueError):
        d.open_link("https://example.com/")


def test_a_rule_waits_for_every_event_and_fires_once() -> None:
    rule = d.Rule.parse(
        {
            "id": "accept",
            "to": "main",
            "text": "我验收 {code_tasks}",
            "after": [
                {"event": "task.accepted", "task": "T001"},
                {"event": "task.accepted", "task": "T002"},
            ],
        },
        {"code_tasks": "T001、T002"},
    )
    assert rule.text == "我验收 T001、T002" and rule.tasks == {"T001", "T002"}
    one = [ev("task.accepted", "T001")]
    assert d.due([rule], one, []) == []
    both = [*one, ev("task.accepted", "T002")]
    assert d.due([rule], both, []) == [rule]
    assert d.due([rule], both, ["accept"]) == []


def test_conditions_match_data_values() -> None:
    cond = {"event": "input.escalated", "task": "T001", "data": {"kind": "in_session"}}
    assert not d.holds(cond, [ev("input.escalated", "T001", kind="handoff")])
    assert not d.holds(cond, [ev("input.escalated", "T002", kind="in_session")])
    assert d.holds(cond, [ev("input.escalated", "T001", kind="in_session", request_id="Q1")])
    # ``min``: the second acceptance (after a requirement change), not the first.
    twice = {"event": "task.accepted", "task": "T001", "min": 2}
    once = [ev("task.accepted", "T001")]
    assert not d.holds(twice, once) and d.holds(twice, once * 2)


def test_rules_for_tasks_outside_the_run_are_dropped() -> None:
    to_worker = d.Rule.parse({"id": "a", "to": "T003", "text": "x"}, {})
    by_tasks = d.Rule.parse({"id": "b", "to": "main", "text": "x", "tasks": ["T003"]}, {})
    plain = d.Rule.parse({"id": "c", "to": "main", "text": "x"}, {})
    assert d.applicable([to_worker, by_tasks, plain], ["T001", "T002"]) == [plain]
    assert len(d.applicable([to_worker, by_tasks, plain], ["T001", "T003"])) == 3


def test_messages_go_to_the_lease_holder_or_the_bound_worker() -> None:
    evs = [
        ev("task.registered", "T001"),
        ev("attempt.start_requested", "T001", actor="local_a"),
        ev("attempt.bound", "T001", session_ref="local_w1"),
        ev("lease.acquired", actor="local_b"),
        ev("attempt.bound", "T001", session_ref="local_w2"),
    ]
    assert d.session_of("main", evs) == "local_b"
    assert d.session_of("T001", evs) == "local_w2"
    assert d.session_of("T002", evs) is None
    assert d.session_of("main", []) is None


def test_client_frames_are_masked_and_sized() -> None:
    mask = b"\x01\x02\x03\x04"
    small = d.frame(b"hi", mask=mask)
    assert small[0] == 0x81 and small[1] == 0x80 | 2 and small[2:6] == mask
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(small[6:])) == b"hi"
    medium = d.frame(b"x" * 300, mask=mask)
    assert medium[1] == 0x80 | 126 and struct.unpack(">H", medium[2:4])[0] == 300
    large = d.frame(b"x" * 70000, mask=mask)
    assert large[1] == 0x80 | 127 and struct.unpack(">Q", large[2:10])[0] == 70000
    # RFC 6455 §1.3's example key.
    assert d.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_the_tree_includes_side_sessions_and_the_run_folder_check_is_strict(
    tmp_path: Path,
) -> None:
    projects = [
        {"sessions": [{"id": "a", "cwd": "x", "children": [{"id": "b", "children": []}]}]},
        {"sessions": [{"id": "c"}]},
    ]
    assert sorted(n["id"] for n in d.sessions(projects)) == ["a", "b", "c"]
    run = tmp_path / "live-x"
    assert d.inside(str(run / "home" / "work"), run)
    assert d.inside(str(run), run)
    assert not d.inside(str(tmp_path / "live-xy"), run)
    assert not d.inside("", run)


def test_the_workdeck_command_prefers_the_named_cli(tmp_path: Path) -> None:
    cli = tmp_path / "cli.js"
    assert d.workdeck_command({"TSW_LIVE_WORKDECK": str(cli)})[-1] == str(cli)
    exe = tmp_path / "workdeck.cmd"
    assert d.workdeck_command({"TSW_LIVE_WORKDECK": str(exe)}) == [str(exe)]
