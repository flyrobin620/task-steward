"""The MCP server, the hook entry point and the plugin package, each as a separate process."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support import REPO_ROOT, child_env, run_json, run_tsw

PLUGIN = REPO_ROOT / "plugin"


@pytest.fixture
def home() -> Path:
    code, out = run_json("init")
    assert code == 0
    root = Path(out["home"])
    (root / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}),
        encoding="utf-8",
    )
    return root


def mcp(home: Path, cwd: Path, session: str | None, *messages: Any) -> list[dict[str, Any]]:
    env = child_env()
    if session:
        env["CLAUDE_CODE_HOST_SESSION_ID"] = session
    lines = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(PLUGIN / "bin" / "tsw_launch.py"), "mcp"],
        input=lines,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def call(i: int, name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18"},
}
NOTE = {"jsonrpc": "2.0", "method": "notifications/initialized"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def payload(response: dict[str, Any]) -> Any:
    return json.loads(response["result"]["content"][0]["text"])


@pytest.mark.use_case("UC-HOST-001")
@pytest.mark.use_case("UC-HOST-003")
def test_orchestrator_tools_over_stdio(home: Path) -> None:
    replies = mcp(
        home,
        home,
        "orch-9",
        INIT,
        NOTE,
        LIST,
        call(2, "whoami"),
        call(3, "lease_acquire"),
        call(4, "task_register", title="t", repo="demo", kind="code", goal="g", acceptance=["a"]),
        call(5, "attention"),
        call(6, "submit", task_id="T001"),
        {"jsonrpc": "2.0", "id": 7, "method": "server/discover"},
        call(8, "inbox"),
    )
    by_id = {r["id"]: r for r in replies}
    assert by_id[0]["result"]["serverInfo"]["name"] == "tsw"
    assert by_id[0]["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in by_id[1]["result"]["tools"]}
    assert {"task_dispatch", "attention", "inbox", "sync", "judge"} <= names
    assert "submit" not in names
    assert payload(by_id[2])["role"] == "orchestrator"
    assert "expires_at" in payload(by_id[3])
    assert payload(by_id[4])["events"][0]["task_id"] == "T001"
    assert payload(by_id[5])["to_start"] == []
    assert by_id[6]["result"]["isError"] is True
    assert payload(by_id[6])["details"]["reason"] == "role_denied"
    assert by_id[7]["error"]["code"] == -32601
    assert payload(by_id[8]) == []


@pytest.mark.use_case("UC-HOST-001")
def test_an_unknown_session_only_sees_whoami(home: Path, tmp_path: Path) -> None:
    replies = mcp(home, tmp_path, "nobody", INIT, LIST, call(2, "task_list"))
    by_id = {r["id"]: r for r in replies}
    assert [t["name"] for t in by_id[1]["result"]["tools"]] == ["whoami"]
    assert by_id[2]["result"]["isError"] is True


@pytest.mark.use_case("UC-HOST-002")
def test_guard_entry_point_denies_ledger_edits(home: Path) -> None:
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-9")
    event = {
        "cwd": str(home),
        "tool_name": "Write",
        "tool_input": {"file_path": str(home / "ledger" / "x")},
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(PLUGIN / "bin" / "tsw_launch.py"),
            "guard",
            "PreToolUse",
        ],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    garbage = subprocess.run(
        [sys.executable, "-X", "utf8", str(PLUGIN / "bin" / "tsw_launch.py"), "guard", "Stop"],
        input="not json",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
        check=False,
    )
    assert (garbage.returncode, garbage.stdout) == (0, "")


@pytest.mark.use_case("UC-HOST-004")
def test_plugin_package_and_generated_files(home: Path) -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "task-steward"
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "${CLAUDE_PLUGIN_ROOT}/bin/tsw_launch.py" in servers["tsw"]["args"]
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "PreToolUse", "UserPromptSubmit", "Stop"}
    for entries in hooks.values():
        for entry in entries:
            assert "tsw_launch.py" in entry["hooks"][0]["command"]
    assert (
        (PLUGIN / "skills" / "orchestrate" / "SKILL.md")
        .read_text(encoding="utf-8")
        .startswith("---\nname: orchestrate")
    )
    market = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert market["plugins"][0]["source"] == "./plugin"
    code, _ = run_json("host", "render")
    assert code == 0
    assert (home / "CLAUDE.md").read_text(encoding="utf-8").startswith("# TaskSteward 数据目录")
    code, hint = run_json("host", "install-hint")
    assert code == 0 and "/plugin install task-steward@task-steward" in hint["hint"]


def launch(
    launcher: Path, *args: str, env: dict[str, str], cwd: Path, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(launcher), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
        timeout=120,
        check=False,
    )


@pytest.mark.use_case("UC-HOST-004")
def test_the_plugin_runs_from_a_copy_outside_the_repository(home: Path, tmp_path: Path) -> None:
    cached = tmp_path / "plugin-cache" / "task-steward" / "0.1.0"
    shutil.copytree(PLUGIN, cached)
    launcher = cached / "bin" / "tsw_launch.py"
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-9")
    env.pop("PYTHONPATH")
    request = json.dumps(INIT) + "\n"
    # tsw init recorded the product repository in the data directory.
    marker = json.loads((home / "tsw-home.json").read_text(encoding="utf-8"))
    assert Path(marker["product_repo"]) == REPO_ROOT.resolve()
    found = launch(launcher, "mcp", env=env, cwd=home, stdin=request)
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout)["result"]["serverInfo"]["name"] == "tsw"
    # Without the record the runtime cannot be found: the server says how to fix it, a hook
    # stays silent, and TSW_REPO names the repository.
    del marker["product_repo"]
    (home / "tsw-home.json").write_text(json.dumps(marker), encoding="utf-8")
    missing = launch(launcher, "mcp", env=env, cwd=home, stdin=request)
    assert missing.returncode == 1 and "TSW_REPO" in missing.stderr
    hook = launch(launcher, "guard", "Stop", env=env, cwd=home, stdin="{}")
    assert (hook.returncode, hook.stdout) == (0, "")
    named = launch(
        launcher, "mcp", env={**env, "TSW_REPO": str(REPO_ROOT)}, cwd=home, stdin=request
    )
    assert named.returncode == 0 and "serverInfo" in named.stdout


@pytest.mark.use_case("UC-HOST-001")
def test_the_command_line_does_not_take_a_role_inside_a_tsw_session(
    home: Path, tmp_path: Path
) -> None:
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-9")
    tool_run = run_tsw(
        "tool", "run", "task_list", "--role", "system", "--input", "{}", "--json", env=env, cwd=home
    )
    assert tool_run.returncode == 3
    assert json.loads(tool_run.stderr)["details"]["reason"] == "host_session_cli"
    assert run_tsw("config", "validate", env=env, cwd=home).returncode == 0
    # A Claude session that is not part of the data directory is not affected.
    assert run_tsw("task", "list", env=env, cwd=tmp_path).returncode == 0


@pytest.mark.use_case("UC-HOST-001")
def test_protocol_versions_and_invalid_messages(home: Path) -> None:
    replies = mcp(
        home,
        home,
        "orch-9",
        {**INIT, "params": {"protocolVersion": "1999-01-01"}},
        {"id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": True, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": "x"},
        [{"jsonrpc": "2.0", "id": 4, "method": "ping"}],
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": 3}},
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": []},
        },
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "id": 7, "result": {}},
        {"jsonrpc": "2.0", "id": 8, "method": "ping"},
    )
    assert replies[0]["result"]["protocolVersion"] == "2025-11-25"
    assert replies[0]["result"]["capabilities"]["tools"]["listChanged"] is True
    codes = [(r["id"], r["error"]["code"]) for r in replies[1:-1]]
    assert codes == [
        (2, -32600),
        (None, -32600),
        (3, -32600),
        (None, -32600),
        (5, -32602),
        (6, -32602),
    ]
    assert replies[-1] == {"jsonrpc": "2.0", "id": 8, "result": {}}
