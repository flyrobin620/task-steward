"""The hosted Dashboard's pure parts and the token guardrail (DN006 §8.10, CC006 §12, CC-B12)."""

from __future__ import annotations

import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from tsw.errors import TswError
from tsw.home import Home
from tsw.host import dashboard_service as ds
from tsw.host.guard import same_dir, touches_secret

pytestmark = pytest.mark.use_case("UC-DIRECT-006")

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def test_the_task_starts_at_the_users_logon_unelevated_and_never_times_out() -> None:
    xml = ds.task_xml(
        user="CORP\\me & you",
        command=r"C:\Python\pythonw.exe",
        arguments='-m tsw dashboard service --home "C:\\data"',
        workdir=r"C:\repo",
    )
    root = ET.fromstring(xml.split("\n", 1)[1])  # the declaration names UTF-16
    trigger = root.find("t:Triggers/t:LogonTrigger", NS)
    assert trigger is not None and trigger.findtext("t:UserId", namespaces=NS) == "CORP\\me & you"
    principal = root.find("t:Principals/t:Principal", NS)
    assert principal is not None
    assert principal.findtext("t:LogonType", namespaces=NS) == "InteractiveToken"
    assert principal.findtext("t:RunLevel", namespaces=NS) == "LeastPrivilege"
    settings = root.find("t:Settings", NS)
    assert settings is not None
    assert settings.findtext("t:ExecutionTimeLimit", namespaces=NS) == "PT0S"
    assert settings.findtext("t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert settings.findtext("t:StopIfGoingOnBatteries", namespaces=NS) == "false"
    action = root.find("t:Actions/t:Exec", NS)
    assert action is not None
    assert action.findtext("t:Command", namespaces=NS) == r"C:\Python\pythonw.exe"
    assert action.findtext("t:Arguments", namespaces=NS).endswith('--home "C:\\data"')  # type: ignore[union-attr]
    assert action.findtext("t:WorkingDirectory", namespaces=NS) == r"C:\repo"


def test_one_service_per_data_directory(tmp_path: Path) -> None:
    a, b = Home(tmp_path / "a"), Home(tmp_path / "b")
    assert len(ds.home_key(a)) == 8 and ds.home_key(a) != ds.home_key(b)
    assert ds.home_key(a) == ds.home_key(Home(tmp_path / "a" / ".." / "a"))
    assert ds.task_name(ds.home_key(a)).startswith("TaskSteward Dashboard (")


def test_the_token_lives_outside_the_data_directory_under_the_guarded_name(tmp_path: Path) -> None:
    env = {"LOCALAPPDATA": str(tmp_path / "local")}
    path = ds.secret_path(env, "abcd1234")
    assert path == tmp_path / "local" / "TaskSteward" / ds.SECRET_DIR / "abcd1234.json"
    assert ds.state_dir({ds.STATE_ENV: str(tmp_path / "s")}) == tmp_path / "s"
    assert ds.state_dir({"XDG_STATE_HOME": str(tmp_path / "x")}) == tmp_path / "x" / "task-steward"


def test_only_the_hashes_of_issued_tokens_are_stored(tmp_path: Path) -> None:
    env = {ds.STATE_ENV: str(tmp_path / "s")}
    home = Home(tmp_path / "h")
    with pytest.raises(TswError):
        ds.issue_token(env, "k")  # not installed
    assert ds.ensure_store(env, "k", home) is True
    assert ds.ensure_store(env, "k", home) is False  # a reinstall keeps the sign-ins
    admits = ds.verifier(env, "k")
    first = ds.issue_token(env, "k")
    second = ds.issue_token(env, "k")
    stored = ds.secret_path(env, "k").read_text(encoding="utf-8")
    assert first not in stored and second not in stored and ds.digest(first) in stored
    assert admits(first) and admits(second) and not admits("forged") and not admits("")
    issued = [ds.issue_token(env, "k") for _ in range(ds.MAX_TOKENS)]
    assert not admits(first) and all(admits(t) for t in issued)  # the newest are kept
    assert not ds.verifier(env, "other")(issued[-1])


def test_the_windowless_interpreter_is_used_when_present(tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    python.write_text("")
    assert ds.pythonw(str(python)) == str(python)
    (tmp_path / "pythonw.exe").write_text("")
    assert ds.pythonw(str(python)) == str(tmp_path / "pythonw.exe")


@pytest.mark.parametrize(
    ("tool", "tool_input", "refused"),
    [
        (
            "Read",
            {"file_path": r"D:\state\TaskSteward\tsw-dashboard-secret\a.json"},
            True,
        ),
        (
            "Read",
            {"file_path": "/srv/state/task-steward/TSW-Dashboard-Secret/a.json"},
            True,
        ),
        ("Grep", {"pattern": "token", "path": r"C:\x\tsw-dashboard-secret"}, True),
        ("Glob", {"pattern": "**/tsw-dashboard-secret/*.json"}, True),
        ("Edit", {"file_path": "C:/x/tsw-dashboard-secret/a.json"}, True),
        ("Bash", {"command": "cat ~/AppData/Local/TaskSteward/tsw-dashboard-secret/a.json"}, True),
        ("Bash", {"command": "cd x && type tsw-dashboard-secret\\a.json"}, True),
        ("Bash", {"command": "echo $(cat /s/tsw-dashboard-secret/a.json)"}, True),
        ("PowerShell", {"command": "Get-Content C:\\s\\tsw-dashboard-secret\\a.json"}, True),
        ("Bash", {"command": "grep -rn tsw-dashboard-secret-name tsw"}, False),
        ("Bash", {"command": "grep -rn SECRET_DIR tsw"}, False),
        ("Read", {"file_path": r"C:\repo\tsw\host\dashboard_service.py"}, False),
        ("Grep", {"pattern": "tsw-dashboard-secret", "path": r"C:\repo"}, False),
    ],
)
def test_cc_b12_refuses_tools_that_reach_the_token_folder(
    tool: str, tool_input: dict[str, Any], refused: bool
) -> None:
    assert touches_secret(tool, tool_input) is refused


def test_same_dir_compares_the_directory_itself(tmp_path: Path) -> None:
    (tmp_path / "home" / "work").mkdir(parents=True)
    assert same_dir(str(tmp_path / "home"), tmp_path / "home")
    assert same_dir(str(tmp_path / "home" / "work" / ".."), tmp_path / "home")
    assert not same_dir(str(tmp_path / "home" / "work"), tmp_path / "home")
    assert not same_dir("", tmp_path / "home")


def test_the_task_names_the_data_directory_as_resolved_at_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    arguments = ds.service_arguments(Home(Path("data")))
    assert arguments.endswith(f'--home "{(tmp_path / "data").resolve()}"')


def test_concurrent_sign_ins_all_survive(tmp_path: Path) -> None:
    env = {ds.STATE_ENV: str(tmp_path / "s")}
    ds.ensure_store(env, "k", Home(tmp_path / "h"))
    issued: list[str] = []
    threads = [
        threading.Thread(target=lambda: issued.append(ds.issue_token(env, "k")))
        for _ in range(ds.MAX_TOKENS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    admits = ds.verifier(env, "k")
    assert len(issued) == ds.MAX_TOKENS and all(admits(t) for t in issued)
