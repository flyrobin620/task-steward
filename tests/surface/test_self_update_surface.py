"""``tsw host render`` inside a session, as separate processes (T282, CC008 §5.7)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support import REPO_ROOT, child_env, run_json, run_tsw
from tests.surface.test_host_surface import launch

pytestmark = pytest.mark.use_case("UC-HOST-008")

ORCH = ["--role", "orchestrator", "--session", "orch-1", "--machine", "m1"]
LAUNCHER = REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"


def tool(name: str, **inp: Any) -> dict[str, Any]:
    code, out = run_json("tool", "run", name, "--input", json.dumps(inp, ensure_ascii=False), *ORCH)
    assert code == 0, out
    return out


@pytest.fixture
def home(tmp_path: Path) -> tuple[Path, Path]:
    """A data directory with a bound worker ``w-1`` working in the returned directory."""
    code, out = run_json("init")
    assert code == 0
    root = Path(out["home"])
    (root / "config" / "task-steward.json").write_text(
        json.dumps({"version": 1, "repositories": {"demo": {"path": "~/src/demo"}}}),
        encoding="utf-8",
    )
    assert run_json("lease", "acquire", "--session", "orch-1", "--machine", "m1")[0] == 0
    tool(
        "task_register",
        task_id="T001",
        title="导出",
        repo="demo",
        kind="code",
        goal="导出 CSV",
        acceptance=["可导出"],
    )
    tool("task_plan", task_id="T001", reason="无依赖")
    workdir = tmp_path / "wt" / "T001"
    workdir.mkdir(parents=True)
    tool("task_dispatch", task_id="T001", workdir=str(workdir), user_turn=True)
    started = tool("attempt_start", task_id="T001", purpose="dispatch", user_turn=True)
    tool(
        "attempt_reconcile",
        task_id="T001",
        attempt="a1",
        generation=started["events"][0]["data"]["generation"],
        outcome="created",
        session_ref="w-1",
        brief_delivered=True,
    )
    return root, workdir


def render(cwd: Path, **env: str) -> tuple[int, dict[str, Any]]:
    proc = run_tsw("host", "render", "--json", env=child_env(**env), cwd=cwd)
    stream = proc.stdout if proc.returncode == 0 else proc.stderr
    return proc.returncode, json.loads(stream.strip().splitlines()[-1])


def test_the_main_session_runs_host_render(home: tuple[Path, Path]) -> None:
    root, _ = home
    (root / "CLAUDE.md").unlink(missing_ok=True)
    code, out = render(root, CLAUDE_CODE_HOST_SESSION_ID="orch-1", CLAUDECODE="1")
    assert code == 0, out
    assert (root / "CLAUDE.md").is_file()
    assert str(root / "roles" / "worker.md") in out["written"]
    assert "不能重启" in out["restart"]
    # Without tsw on PATH the skill runs the plugin's launcher, still from the data directory.
    proc = launch(
        LAUNCHER,
        "host",
        "render",
        "--json",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-1"),
        cwd=root,
    )
    assert proc.returncode == 0, proc.stderr
    assert "restart" in json.loads(proc.stdout.strip().splitlines()[-1])


def test_other_sessions_are_refused_with_the_reason(
    home: tuple[Path, Path], tmp_path: Path
) -> None:
    root, workdir = home
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / "CLAUDE.md").unlink(missing_ok=True)
    for cwd, env, role in (
        (workdir, {"CLAUDE_CODE_HOST_SESSION_ID": "w-1"}, "worker"),
        (elsewhere, {"CLAUDE_CODE_HOST_SESSION_ID": "other-1"}, "none"),
        (root, {"CLAUDECODE": "1"}, "unknown"),
    ):
        code, err = render(cwd, **env)
        assert code == 3, (role, err)
        assert (err["details"]["reason"], err["details"]["role"]) == ("host_session_cli", role)
        assert "只有主会话" in err["message"] and "终端" in err["message"]
    assert not (root / "CLAUDE.md").exists()


def test_the_users_commands_stay_refused_in_the_main_session(home: tuple[Path, Path]) -> None:
    root, _ = home
    env = child_env(CLAUDE_CODE_HOST_SESSION_ID="orch-1")
    for args in (("task", "accept", "T001", "--yes"), ("dashboard", "--no-open")):
        proc = run_tsw(*args, "--json", env=env, cwd=root)
        assert proc.returncode == 3, args
        assert json.loads(proc.stderr)["details"]["reason"] == "host_session_cli", args


def test_the_hook_refuses_host_render_for_a_worker_only(home: tuple[Path, Path]) -> None:
    root, workdir = home

    def decide(session: str, cwd: Path) -> dict[str, Any] | None:
        payload = {
            "cwd": str(cwd),
            "tool_name": "Bash",
            "tool_input": {"command": "tsw host render"},
        }
        proc = launch(
            LAUNCHER,
            "guard",
            "PreToolUse",
            env=child_env(CLAUDE_CODE_HOST_SESSION_ID=session),
            cwd=cwd,
            stdin=json.dumps(payload),
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    assert decide("orch-1", root) is None
    refused = decide("w-1", workdir)
    assert refused is not None
    output = refused["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "只有主会话" in output["permissionDecisionReason"]
