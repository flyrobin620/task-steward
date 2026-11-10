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


def hook(home: Path, session: str, event: str, **fields: Any) -> dict[str, Any] | None:
    proc = launch(
        PLUGIN / "bin" / "tsw_launch.py",
        "guard",
        event,
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
        cwd=home,
        stdin=json.dumps({"cwd": str(home), **fields}),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


@pytest.mark.use_case("UC-HOST-002")
@pytest.mark.use_case("UC-HOST-003")
@pytest.mark.use_case("UC-DISPATCH-001")
def test_the_users_turn_is_reminded_to_prestart(home: Path) -> None:
    replies = mcp(
        home,
        home,
        "orch-9",
        INIT,
        NOTE,
        call(1, "lease_acquire"),
        call(2, "task_register", title="t", repo="demo", kind="code", goal="g", acceptance=["a"]),
        call(3, "task_plan", task_id="T001", dependencies=[], resources=[], reason="r"),
        call(4, "attention"),
    )
    by_id = {r["id"]: r for r in replies}
    assert payload(by_id[4])["needs_prestart"] == [{"task_id": "T001", "status": "ready"}]
    assert hook(home, "orch-9", "UserPromptSubmit", prompt="开始 T001") is None
    stop = hook(home, "orch-9", "Stop")
    assert stop is not None and stop["decision"] == "block" and "T001" in stop["reason"]
    assert hook(home, "orch-9", "Stop", stop_hook_active=True) is None
    injected = '<cross-session-message from="w">done</cross-session-message>'
    assert hook(home, "orch-9", "UserPromptSubmit", prompt=injected) is None
    assert hook(home, "orch-9", "Stop") is None
    # attempt_start creates the execution directory, so start_session can open it directly.
    started = mcp(
        home,
        home,
        "orch-9",
        INIT,
        NOTE,
        call(5, "attempt_start", task_id="T001", purpose="prestart", user_turn=True),
    )
    cwd = Path(payload({r["id"]: r for r in started}[5])["cwd"])
    assert cwd.is_dir() and cwd.parts[-3:] == ("work", "T001", "a1")


@pytest.mark.use_case("UC-HOST-001")
@pytest.mark.use_case("UC-HOST-002")
@pytest.mark.use_case("UC-LEASE-001")
def test_one_orchestrator_hands_the_lease_to_another(home: Path) -> None:
    """DN001 §6.4 (T254): release, take at the next session start, never taken back silently."""
    replies = mcp(
        home,
        home,
        "orch-a",
        INIT,
        NOTE,
        LIST,
        call(2, "lease_acquire"),
        call(3, "lease_release"),
        call(4, "lease_status"),
        call(5, "whoami"),
    )
    by_id = {r["id"]: r for r in replies}
    assert "lease_release" in {t["name"] for t in by_id[1]["result"]["tools"]}
    assert payload(by_id[3])["released"] is True
    status = payload(by_id[4])
    assert status["lease"] is None and status["stood_down"]["via"] == "release"
    assert payload(by_id[5])["orchestrator_session"] is None
    old = hook(home, "orch-a", "SessionStart")
    assert old is not None
    assert "让出调度租约" in old["hookSpecificOutput"]["additionalContext"]
    assert run_json("lease", "show")[1]["lease"] is None  # not taken back
    new = hook(home, "orch-b", "SessionStart")
    assert new is not None
    assert "已持有调度租约" in new["hookSpecificOutput"]["additionalContext"]
    code, shown = run_json("lease", "show")
    assert (code, shown["lease"]["holder_session"]) == (0, "orch-b")
    replies = mcp(home, home, "orch-a", INIT, NOTE, call(1, "whoami"), call(2, "lease_release"))
    by_id = {r["id"]: r for r in replies}
    assert payload(by_id[1])["orchestrator_session"] == "orch-b"
    assert by_id[2]["result"]["isError"] is True
    assert payload(by_id[2])["details"]["reason"] == "lease_held"


@pytest.mark.use_case("UC-HOST-004")
def test_plugin_package_and_generated_files(home: Path) -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "task-steward"
    # A fixed version keeps Claude Code on the first copy it cached (T247): until the first
    # release, the version comes from the commit.
    assert "version" not in manifest
    servers = json.loads((PLUGIN / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "${CLAUDE_PLUGIN_ROOT}/bin/tsw_launch.py" in servers["tsw"]["args"]
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"SessionStart", "PreToolUse", "UserPromptSubmit", "Stop"}
    for entries in hooks.values():
        for entry in entries:
            assert "tsw_launch.py" in entry["hooks"][0]["command"]
    # The plugin is enabled at user scope, so a skill in it would reach every session in any
    # directory. The orchestrator's skill is rendered into the data directory instead.
    assert not (PLUGIN / "skills").exists()
    market = json.loads(
        (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert market["plugins"][0]["source"] == "./plugin"
    assert "version" not in market["plugins"][0]
    skill = home / ".claude" / "skills" / "orchestrate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("stale", encoding="utf-8")
    settings = home / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "env": {"X": "1"}}), "utf-8"
    )
    code, out = run_json("host", "render")
    assert code == 0
    handle_input = home / ".claude" / "skills" / "handle-input" / "SKILL.md"
    teams_chat = home / ".claude" / "skills" / "teams-chat" / "SKILL.md"
    assert [Path(p) for p in out["written"]] == [
        home / "CLAUDE.md",
        skill,
        handle_input,
        teams_chat,
        settings,
    ]
    # The test's Claude configuration directory has no plugin installed.
    assert out["plugin"]["installed"] == [] and "note" in out["plugin"]
    handling = handle_input.read_text(encoding="utf-8")
    assert handling.startswith("---\nname: handle-input\ndescription: ")
    # The flow (T256): handed off, judged, then told through the configured human channels;
    # never asked again in the main session, and the channel's details are in its own skill.
    assert "handoff_resolve" in handling and "input.channels" in handling
    assert "不在主会话里重复提问" in handling and "该渠道的 skill" in handling
    assert "create_entity" not in handling and "contentType" not in handling
    channel = teams_chat.read_text(encoding="utf-8")
    assert channel.startswith("---\nname: teams-chat\ndescription: ")
    # Teams cards go out with WorkIQ as rich text: the self chat's path, the HTML body, and
    # where channel_sent's fields come from; replies and read failures are handled here too.
    assert "mcp__workiq__create_entity" in channel and "/me/chats/48:notes/messages" in channel
    assert '"contentType": "html"' in channel and "card_html" in channel
    assert "from.user.id" in channel and "mcp__teams__SendMessageToSelf" not in channel
    assert '"channel": "teams-chat"' in channel and "mcp__workiq__fetch" in channel
    # The actions tsw guards itself are allowed; what the user had there is kept (DN006 §6.3).
    written = json.loads(settings.read_text(encoding="utf-8"))
    assert written["env"] == {"X": "1"}
    allow = written["permissions"]["allow"]
    assert allow[0] == "Bash(ls:*)"
    assert {"mcp__plugin_task-steward_tsw", "mcp__ccd_session__start_session"} <= set(allow)
    assert not any(rule.startswith("Bash(gh") for rule in allow)
    assert run_json("host", "render")[0] == 0
    again = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert again == allow
    assert (home / "CLAUDE.md").read_text(encoding="utf-8").startswith("# TaskSteward 数据目录")
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: orchestrate\ndescription: ")
    assert "\n---\n\n# TaskSteward 主会话\n" in text
    code, hint = run_json("host", "install-hint")
    assert code == 0 and "/plugin install task-steward@task-steward" in hint["hint"]
    assert "tsw host render" in hint["hint"]
    # The auto-mode rule is shown, never written: Claude Code reads it from user settings only.
    rules = json.loads(hint["hint"][hint["hint"].index("{") :])["autoMode"]["allow"]
    assert [r.split(":")[0] for r in rules] == [
        "TaskSteward Worker Dispatch",
        "TaskSteward Worker Merge",
        "TaskSteward Routine",
    ]
    assert "--admin" in rules[1]
    # The routine session reads Teams with WorkIQ (T253).
    assert "mcp__workiq__fetch" in rules[2] and "ListChatMessages" not in rules[2]


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


@pytest.mark.use_case("UC-HOST-004")
def test_a_session_inside_a_claiming_data_directory_uses_it(home: Path, tmp_path: Path) -> None:
    """The cached plugin finds the runtime and the data directory from the session's folder,
    with no settings file there and ``TSW_HOME`` naming another data directory."""
    cached = tmp_path / "plugin-cache" / "task-steward" / "0.1.0"
    shutil.copytree(PLUGIN, cached)
    launcher = cached / "bin" / "tsw_launch.py"
    run = tmp_path / "run" / "home"
    claimed = run_tsw("init", "--home", str(run), "--claim-sessions", "--json")
    assert claimed.returncode == 0, claimed.stderr
    assert json.loads(claimed.stdout.splitlines()[-1])["claim_sessions"] is True
    workdir = run / "work" / "T001" / "a1"
    workdir.mkdir(parents=True)
    # TSW_HOME names a data directory with no runtime record: only the claiming one has it.
    marker = json.loads((home / "tsw-home.json").read_text(encoding="utf-8"))
    del marker["product_repo"]
    (home / "tsw-home.json").write_text(json.dumps(marker), encoding="utf-8")
    env = child_env(TSW_HOME=str(home), CLAUDE_CODE_HOST_SESSION_ID="orch-run")
    env.pop("PYTHONPATH")
    found = launch(launcher, "home", "--json", env=env, cwd=workdir)
    assert found.returncode == 0, found.stderr
    assert Path(json.loads(found.stdout.splitlines()[-1])["home"]) == run.resolve()
    # The MCP server resolves the session's identity in the same directory: a session in the
    # claiming data directory is its main session and may write.
    request = "".join(
        json.dumps(m) + "\n" for m in (INIT, NOTE, call(2, "whoami"), call(3, "lease_acquire"))
    )
    served = launch(launcher, "mcp", env=env, cwd=run, stdin=request)
    assert served.returncode == 0, served.stderr
    by_id = {r["id"]: r for r in map(json.loads, served.stdout.splitlines()) if "id" in r}
    who = payload(by_id[2])
    assert (who["role"], Path(who["cwd"]).resolve()) == ("orchestrator", run.resolve())
    assert "expires_at" in payload(by_id[3])
    assert (run / "locks").is_dir() and not (home / "locks" / "lease.json").exists()


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


def stale_copy(root: Path) -> Path:
    """A cached copy as Claude Code kept it from an older product: a changed hook list, a file
    since removed from plugin/, and Claude Code's own bookkeeping that never counts."""
    copy = root / "cache" / "task-steward" / "task-steward" / "0.1.0-dev"
    shutil.copytree(PLUGIN, copy)
    hooks = copy / "hooks" / "hooks.json"
    hooks.write_text(hooks.read_text(encoding="utf-8").replace("|CronCreate", ""), "utf-8")
    (copy / "skills" / "orchestrate").mkdir(parents=True)
    (copy / "skills" / "orchestrate" / "SKILL.md").write_text("old", encoding="utf-8")
    (copy / ".in_use").mkdir()
    (copy / ".in_use" / "4242").write_text("", encoding="utf-8")
    return copy


FAKE_CLAUDE = r"""
import json, os, shutil, sys
from pathlib import Path

with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\n")
if sys.argv[1:] == ["plugin", "update", "task-steward@task-steward"]:
    root = Path(os.environ["CLAUDE_CODE_PLUGIN_CACHE_DIR"])
    records = json.loads((root / "installed_plugins.json").read_text(encoding="utf-8"))
    for install in records["plugins"]["task-steward@task-steward"]:
        fresh = root / "cache" / "task-steward" / "task-steward" / "fedcba987654"
        if not fresh.exists():
            shutil.copytree(os.environ["FAKE_CLAUDE_SOURCE"], fresh)
        install["installPath"] = str(fresh)
    (root / "installed_plugins.json").write_text(json.dumps(records), encoding="utf-8")
    print("Updated task-steward. Restart to apply changes.")
"""


def fake_claude(tmp_path: Path, source: Path) -> dict[str, str]:
    """A ``claude`` command on PATH standing in for Claude Code's plugin update: it points every
    task-steward install at a fresh copy of ``source`` and logs its arguments."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    (bin_dir / "claude.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    shell = bin_dir / "claude"
    shell.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    shell.chmod(0o755)
    return {
        "PATH": str(bin_dir),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.log"),
        "FAKE_CLAUDE_SOURCE": str(source),
    }


@pytest.mark.use_case("UC-HOST-004")
def test_render_updates_the_plugin_and_session_start_reports_a_stale_copy(
    home: Path, tmp_path: Path
) -> None:
    plugins = tmp_path / "plugins"
    stale = stale_copy(plugins)
    records = {
        "version": 2,
        "plugins": {
            "task-steward@task-steward": [
                {"scope": "user", "installPath": str(stale), "version": "0.1.0-dev"}
            ]
        },
    }
    (plugins / "installed_plugins.json").write_text(json.dumps(records), encoding="utf-8")
    fake = fake_claude(tmp_path, PLUGIN)
    env = child_env(CLAUDE_CODE_PLUGIN_CACHE_DIR=str(plugins), **fake)
    code, out = run_json("host", "render", env=env)
    assert code == 0
    report = out["plugin"]
    assert report["update"]["ran"] and report["update"]["returncode"] == 0, report
    assert report["update"]["command"] == "claude plugin update task-steward@task-steward"
    assert [c["current"] for c in report["installed"]] == [True]
    # The state before the update stays in the report: the old copy and how it differed.
    assert report["before"][0]["path"] == str(stale)
    assert report["before"][0]["version"] == "0.1.0-dev"
    assert report["before"][0]["differs"] == ["hooks/hooks.json", "skills/orchestrate/SKILL.md"]
    assert "重启" in report["next"]
    # Idempotent: rendering again runs the update again and changes nothing.
    code, again = run_json("host", "render", env=env)
    assert code == 0 and [c["current"] for c in again["plugin"]["installed"]] == [True]
    assert "next" not in again["plugin"]
    log = (tmp_path / "claude.log").read_text(encoding="utf-8").splitlines()
    assert log == ["plugin update task-steward@task-steward"] * 2

    def started(root: Path | None) -> str:
        extra = {"CLAUDE_CODE_HOST_SESSION_ID": "orch-9"}
        if root is not None:
            extra["CLAUDE_PLUGIN_ROOT"] = str(root)
        proc = launch(
            PLUGIN / "bin" / "tsw_launch.py",
            "guard",
            "SessionStart",
            env=child_env(**extra),
            cwd=home,
            stdin=json.dumps({"cwd": str(home)}),
        )
        assert proc.returncode == 0, proc.stderr
        return str(json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"])

    warned = started(stale)
    assert "插件副本" in warned and "hooks/hooks.json" in warned and "tsw host render" in warned
    fresh = Path(again["plugin"]["installed"][0]["path"])
    for root in (fresh, PLUGIN, None):
        assert "插件副本" not in started(root)


@pytest.mark.use_case("UC-HOST-004")
def test_render_reports_a_copy_the_update_did_not_replace(home: Path, tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    stale = stale_copy(plugins)
    records = {
        "plugins": {"task-steward@task-steward": [{"scope": "user", "installPath": str(stale)}]}
    }
    (plugins / "installed_plugins.json").write_text(json.dumps(records), encoding="utf-8")
    # An update that leaves the old copy in place, as the fixed version string did.
    fake = fake_claude(tmp_path, stale)
    code, out = run_json(
        "host", "render", env=child_env(CLAUDE_CODE_PLUGIN_CACHE_DIR=str(plugins), **fake)
    )
    assert code == 0
    report = out["plugin"]
    assert report["installed"][0]["differs"] == ["hooks/hooks.json", "skills/orchestrate/SKILL.md"]
    assert "claude plugin uninstall task-steward@task-steward" in report["next"]
    # Without a claude command nothing runs and the report says so.
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    env = child_env(CLAUDE_CODE_PLUGIN_CACHE_DIR=str(plugins), PATH=str(empty))
    code, out = run_json("host", "render", env=env)
    assert code == 0 and out["plugin"]["update"]["ran"] is False
