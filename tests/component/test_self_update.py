"""The main session updates itself: ``tsw host render`` inside a session (T282, CC008 §5.7)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.component.test_host import guard, worker_task
from tests.machine_harness import Harness
from tsw.cli import main as cli_main
from tsw.home import Home
from tsw.host.identity import SESSION_ENV

pytestmark = pytest.mark.use_case("UC-HOST-008")


@pytest.fixture
def h(home: Home) -> Harness:
    return Harness(home)


def render(
    h: Harness,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cwd: Path,
    env: dict[str, str],
) -> tuple[int, dict[str, object]]:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(cwd)
    code = cli_main(["--home", str(h.home.root), "host", "render", "--json"])
    captured = capsys.readouterr()
    stream = captured.out if code == 0 else captured.err
    return code, json.loads(stream.strip().splitlines()[-1])


def test_the_main_session_runs_host_render(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (h.home.root / "CLAUDE.md").unlink(missing_ok=True)
    code, out = render(h, monkeypatch, capsys, h.home.root, {SESSION_ENV: "orch-1"})
    assert code == 0, out
    assert (h.home.root / "CLAUDE.md").is_file()
    assert str(h.home.root / "roles" / "worker.md") in out["written"]  # type: ignore[operator]
    # The session cannot restart itself: the output says so, for the main session to tell the user.
    assert "重启" in str(out["restart"]) and "不能重启" in str(out["restart"])


def test_a_worker_may_not_run_host_render(
    h: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session, workdir = worker_task(h, tmp_path)
    (h.home.root / "CLAUDE.md").unlink(missing_ok=True)
    code, err = render(h, monkeypatch, capsys, workdir, {SESSION_ENV: session})
    assert code == 3
    details = err["details"]
    assert isinstance(details, dict)
    assert (details["reason"], details["role"]) == ("host_session_cli", "worker")
    assert "只有主会话" in str(err["message"])
    assert not (h.home.root / "CLAUDE.md").exists()  # refused before anything is written


def test_the_routine_session_may_not_run_host_render(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = h.run("routine_start", user_turn=True)["events"][0]["data"]
    h.run("routine_bind", generation=started["generation"], session_id="routine-1")
    code, err = render(h, monkeypatch, capsys, h.home.root / "routine", {SESSION_ENV: "routine-1"})
    details = err["details"]
    assert isinstance(details, dict)
    assert (code, details["reason"], details["role"]) == (3, "host_session_cli", "routine")


def test_sessions_outside_the_data_directory_may_not_run_host_render(
    h: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    code, err = render(h, monkeypatch, capsys, elsewhere, {SESSION_ENV: "other-1"})
    details = err["details"]
    assert isinstance(details, dict)
    assert (code, details["reason"], details["role"]) == (3, "host_session_cli", "none")
    # A process in a Claude Code session that does not say which session it is.
    monkeypatch.delenv(SESSION_ENV)
    code, err = render(h, monkeypatch, capsys, h.home.root, {"CLAUDECODE": "1"})
    details = err["details"]
    assert isinstance(details, dict)
    assert (code, details["reason"], details["role"]) == (3, "host_session_cli", "unknown")


def test_outside_any_session_host_render_runs_as_before(
    h: Harness,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    elsewhere = tmp_path / "terminal"
    elsewhere.mkdir()
    code, out = render(h, monkeypatch, capsys, elsewhere, {})
    assert code == 0, out


def test_the_main_session_still_may_not_run_the_users_commands(
    h: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(SESSION_ENV, "orch-1")
    monkeypatch.chdir(h.home.root)
    h.register("T001")
    for args in (
        ["task", "accept", "T001", "--yes"],
        ["dashboard", "--no-open"],
        ["tool", "run", "task_list", "--role", "orchestrator", "--input", "{}"],
    ):
        assert cli_main(["--home", str(h.home.root), *args, "--json"]) == 3, args
        assert "host_session_cli" in capsys.readouterr().err, args


def test_the_hook_lets_only_the_main_session_run_host_render(h: Harness, tmp_path: Path) -> None:
    command = {"command": "tsw host render"}
    main = guard(h, "orch-1", h.home.root, tool_name="Bash", tool_input=command)
    assert main.run("PreToolUse") is None
    session, workdir = worker_task(h, tmp_path)
    refused = guard(h, session, workdir, tool_name="Bash", tool_input=command).run("PreToolUse")
    assert refused is not None
    reason = refused["hookSpecificOutput"]["permissionDecisionReason"]
    assert "只有主会话" in reason
    # Other commands stay refused for the main session too.
    accept = {"command": "tsw task accept T001 --yes"}
    refused = guard(h, "orch-1", h.home.root, tool_name="Bash", tool_input=accept)
    assert refused.run("PreToolUse") is not None
