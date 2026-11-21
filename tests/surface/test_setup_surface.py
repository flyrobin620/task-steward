"""``tsw setup``, ``setup.ps1`` and the session entry's scripts as separate processes (DN009 §4,
§8; CC009 §4, §6, §8; CC008 §5.8).

Claude Code's plugin commands, winget, the user ``Path`` (HKCU) and Task Scheduler are stand-ins
(simulated); PowerShell, Python, git and the files are real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.setup_harness import (
    claude_calls,
    fake_claude,
    git,
    make_product,
    record,
    run_setup,
    settings,
    setup_env,
    write_answers,
)
from tests.support import REPO_ROOT, child_env, run_tsw
from tests.surface.test_self_update_surface import bound_worker_home

UC = "UC-SETUP-001"


def stood_down(home: Path, session: str) -> str | None:
    """How ``session`` stood down in ``home``'s ledger, or None."""
    from tsw.home import open_home  # noqa: PLC0415
    from tsw.ledger import Ledger  # noqa: PLC0415

    with Ledger(open_home(home)).view() as conn:
        row = conn.execute(
            "SELECT via FROM lease_stood_down WHERE session_id = ?", (session,)
        ).fetchone()
    return None if row is None else str(row["via"])


GATE = "UC-SETUP-002"
POWERSHELL = "powershell"


@pytest.mark.use_case(UC)
def test_check_lists_the_one_question_list(tmp_path: Path) -> None:
    product = make_product(tmp_path)
    env = setup_env(tmp_path)
    code, out, text = run_setup(product, "check", "--questions", env=env)
    assert code == 0, text
    assert out["ready"] is False
    assert "home：还没有给出数据目录位置（必填，没有默认值）" in out["problems"]
    questions = {q["id"]: q for q in out["questions"]}
    assert questions["home"]["required"] is True and questions["home"]["default"] is None
    assert questions["checkout"]["asked_by"] == "entry"
    assert Path(out["answers_file"]).name == "setup-answers.json"
    assert Path(out["checkout"]["path"]) == product
    # install needs answers (or --interactive); nothing is written without them.
    code, out, _ = run_setup(product, "install", env=env)
    assert code == 3 and out["details"]["reason"] == "invalid_input"


@pytest.mark.use_case(UC)
def test_the_terminal_flow_asks_shows_the_plan_and_installs(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    home = tmp_path / "data"
    replies = [
        "",  # the data directory is required: asked again
        str(home),
        "",  # language
        "",  # reviewer: the one implemented reviewer
        "",  # human channels
        "否",  # no Dashboard service
        "",  # port
        "",  # user settings
        "是",  # the plan is confirmed
    ]
    code, out, text = run_setup(
        product,
        "install",
        "--interactive",
        "--no-browser",
        "--claude",
        str(claude),
        env=env,
        stdin="\n".join(replies) + "\n",
    )
    assert code == 0, text
    assert "这一项必须由你给出。" in text
    assert "将要执行的步骤：" in text and "user_settings：要做" in text
    assert out["installed"] is True and out["verify"]["ok"] is True
    saved = json.loads(Path(env["TSW_STATE_DIR"], "setup-answers.json").read_text("utf-8"))
    assert saved["home"] == str(home) and saved["dashboard"] is False
    # A rerun reads the saved answers; verify and uninstall need none.
    code, out, _ = run_setup(product, "verify", env=env)
    assert code == 0 and out["phase"] == "install"


@pytest.mark.use_case(GATE)
def test_a_session_bound_to_no_data_directory_may_install(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path, CLAUDE_CODE_HOST_SESSION_ID="first-install", CLAUDECODE="1")
    answers = write_answers(tmp_path, tmp_path / "data")
    code, out, text = run_setup(
        product,
        "install",
        "--answers",
        str(answers),
        "--claude",
        str(claude),
        "--no-browser",
        env=env,
    )
    assert code == 0, text
    assert record(env)["status"] == "installed"
    # The install session does not take the lease when it is later opened as the main
    # session: it stands down (DN009 §15.1, the user's decision of 2026-11-20).
    assert out["install_session"]["session_id"] == "first-install"
    assert stood_down(tmp_path / "data", "first-install") == "setup"
    assert "/orchestrate" in out["next"] and out["start_command"] == "/orchestrate"
    assert out["demo_prompt"] == "开始示例任务"


@pytest.mark.use_case(GATE)
def test_the_main_session_may_rerun_the_install(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    answers = write_answers(tmp_path, home)
    args = ("install", "--answers", str(answers), "--claude", str(claude), "--no-browser")
    assert run_setup(product, *args, env=env)[0] == 0
    main = {**env, "TSW_HOME": str(home), "CLAUDE_CODE_HOST_SESSION_ID": "orch-9"}
    code, out, text = run_setup(product, *args, env=main, cwd=home)
    assert code == 0, text
    assert {s["state"] for s in out["steps"]} <= {"ok", "skipped"}
    assert "install_session" not in out and stood_down(home, "orch-9") is None


@pytest.mark.use_case(GATE)
def test_a_worker_may_not_install_or_uninstall(tmp_path: Path) -> None:
    root, workdir = bound_worker_home(tmp_path)
    for command in (("install", "--answers", "a.json"), ("uninstall", "--yes")):
        proc = run_tsw(
            "setup",
            *command,
            "--json",
            env=child_env(CLAUDE_CODE_HOST_SESSION_ID="w-1", CLAUDECODE="1"),
            cwd=workdir,
        )
        error = json.loads(proc.stderr.strip().splitlines()[-1])
        assert proc.returncode == 3
        assert error["details"]["reason"] == "host_session_cli"
        assert error["details"]["role"] == "worker"
    assert not Path(os.environ["TSW_STATE_DIR"], "install.json").exists()
    # The read-only commands run in any session.
    proc = run_tsw(
        "setup",
        "check",
        "--json",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID="w-1", CLAUDECODE="1"),
        cwd=workdir,
    )
    assert proc.returncode == 0, proc.stderr
    assert root.is_dir()


@pytest.mark.use_case(GATE)
def test_the_pre_tool_use_hook_refuses_a_worker_running_setup(tmp_path: Path) -> None:
    _, workdir = bound_worker_home(tmp_path)
    payload = {
        "session_id": "w-1",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "tsw setup install --answers a.json"},
        "cwd": str(workdir),
    }
    launcher = REPO_ROOT / "plugin" / "bin" / "tsw_launch.py"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(launcher), "guard", "PreToolUse"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=child_env(CLAUDE_CODE_HOST_SESSION_ID="w-1"),
        cwd=workdir,
        timeout=120,
        check=False,
    )
    decision = json.loads(proc.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "不能运行 tsw setup install" in decision["permissionDecisionReason"]


# ---------------------------------------------------------------------- PowerShell entries


def powershell(script: Path, *args: str, env: dict[str, str], stdin: str = "") -> Any:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
        check=False,
    )


def bare_path() -> str:
    """Windows' own System32 folders only: no Python, git or claude on PATH. The Windows folder
    itself is left out: the Python launcher ``py.exe`` lives there (CI runners have it)."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return ";".join([rf"{root}\System32", rf"{root}\System32\WindowsPowerShell\v1.0"])


@pytest.mark.use_case(UC)
def test_the_probe_finds_python_git_and_claude(tmp_path: Path) -> None:
    claude = fake_claude(tmp_path)
    env = setup_env(tmp_path)
    env["PATH"] = os.pathsep.join([str(claude.parent), env["PATH"]])
    proc = powershell(REPO_ROOT / "plugin" / "setup" / "prereq.ps1", "-Json", env=env)
    assert proc.returncode == 0, proc.stderr
    found = json.loads(proc.stdout.strip().splitlines()[-1])
    assert found["missing"] == []
    assert tuple(int(x) for x in found["python"]["version"].split(".")[:2]) >= (3, 10)
    assert Path(found["claude"]["path"]) == claude
    assert found["git"]["path"]


@pytest.mark.use_case(UC)
def test_the_probe_reports_missing_items_and_installs_with_winget(tmp_path: Path) -> None:
    env = setup_env(tmp_path)
    env["PATH"] = bare_path()
    log = tmp_path / "winget.log"
    stand_in = tmp_path / "winget.cmd"
    # The stand-in "installs" Python where the official installer puts it for one user.
    target = Path(env["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312"
    stand_in.write_text(
        "@echo off\r\n"
        f'echo %* >> "{log}"\r\n'
        f'mkdir "{target}" 2>nul\r\n'
        f'copy /y "{sys.executable}" "{target}\\python.exe" >nul\r\n',
        encoding="utf-8",
    )
    env["TSW_WINGET"] = str(stand_in)
    script = REPO_ROOT / "plugin" / "setup" / "prereq.ps1"
    proc = powershell(script, "-Json", env=env)
    found = json.loads(proc.stdout.strip().splitlines()[-1])
    assert set(found["missing"]) >= {"python", "claude"}, found
    assert any("Claude Code" in note for note in found["notes"])
    proc = powershell(script, "-Json", "-Install", "python", env=env)
    found = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "--id Python.Python.3.12" in log.read_text(encoding="utf-8")
    assert found["installed"] == ["python"] or found["python"] is None
    # A copied python.exe may not run without its DLLs: only the call is the platform's.


@pytest.mark.use_case(UC)
@pytest.mark.use_case("UC-SETUP-005")
def test_setup_ps1_runs_the_interactive_install(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    env["PATH"] = os.pathsep.join(
        [str(claude.parent), str(Path(sys.executable).parent), env["PATH"]]
    )
    home = tmp_path / "data"
    replies = [str(home), "", "", "", "否", "", "", "是"]
    proc = powershell(product / "setup.ps1", "-NoBrowser", env=env, stdin="\n".join(replies) + "\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Piped input and output: the line questions without colour or cursor movement (DN009 §16.4).
    assert "\x1b" not in proc.stdout and "请回答下面的问题" in proc.stdout
    assert (home / "tsw-home.json").is_file()
    installed = record(env)
    assert installed["status"] == "installed"
    assert Path(installed["claude"]) == claude
    assert settings(env)["env"]["TSW_HOME"] == str(home)
    assert claude_calls(tmp_path)[0] == f"plugin marketplace add {product}"


# ---------------------------------------------------------------------- the checkout


def fetch(*args: str, env: dict[str, str]) -> tuple[int, dict[str, Any]]:
    script = REPO_ROOT / "plugin" / "setup" / "fetch_checkout.py"
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
        check=False,
    )
    return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.use_case(UC)
def test_the_session_entry_clones_the_installed_plugins_commit(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    first = git(product, "rev-parse", "HEAD")
    subprocess.run(
        [str(claude), "plugin", "install", "task-steward", "--marketplace", str(product)],
        env=env,
        check=True,
        capture_output=True,
    )
    (product / "later.txt").write_text("x", encoding="utf-8")
    git(product, "add", "later.txt")
    git(product, "commit", "-q", "-m", "later")
    code, out = fetch(env=env)
    dest = Path(env["LOCALAPPDATA"]) / "TaskSteward" / "task-steward"
    assert code == 0, out
    assert Path(out["checkout"]) == dest and out["created"] is True
    assert out["head"] == first and out["same_as_plugin"] is True  # the plugin's commit
    assert git(dest, "branch", "--show-current") == "main"
    # An existing clean checkout of the same repository is only checked.
    code, out = fetch(env=env)
    assert code == 0 and out["created"] is False
    (dest / "plugin" / "roles" / "worker.md").write_text("changed", encoding="utf-8")
    code, out = fetch(env=env)
    assert code == 1 and "未提交的改动" in out["problem"]
    other = make_product(tmp_path, "other")
    code, out = fetch("--dest", str(dest), "--url", str(other), env=env)
    assert code == 1 and "另一个仓库" in out["problem"]


@pytest.mark.use_case(UC)
def test_a_rerun_uses_the_saved_answers_and_manual_settings_are_shown(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    home = tmp_path / "data"
    replies = [str(home), "", "", "", "否", "", "否", "是"]
    launcher = product / "plugin" / "bin" / "tsw_launch.py"
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(launcher),
            "setup",
            "install",
            "--interactive",
            "--no-browser",
            "--claude",
            str(claude),
        ],
        input="\n".join(replies) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The terminal shows what to merge by hand when the settings are not maintained.
    assert '"TSW_HOME"' in proc.stdout and "settings.json" in proc.stdout
    # update-plan's reinstall runs the bare command: the saved answers are used.
    code, out, text = run_setup(
        product, "install", "--claude", str(claude), "--no-browser", env=env
    )
    assert code == 0, text
    assert {s["step"]: s["state"] for s in out["steps"]}["home"] == "ok"


@pytest.mark.use_case(UC)
def test_git_outside_path_is_given_by_the_probe(tmp_path: Path) -> None:
    import shutil  # noqa: PLC0415

    product = make_product(tmp_path)
    git_path = shutil.which("git")
    assert git_path
    env = setup_env(tmp_path)
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), bare_path()])
    _, out, _ = run_setup(product, "check", env=env)
    assert any(p.startswith("git：") for p in out["problems"])
    _, out, _ = run_setup(product, "check", "--git", git_path, env=env)
    assert not any(p.startswith("git：") for p in out["problems"])
    assert out["checkout"]["head"] == git(product, "rev-parse", "HEAD")


@pytest.mark.use_case(GATE)
def test_an_install_session_in_the_data_directory_stands_down_even_after_a_failure(
    tmp_path: Path,
) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    home.mkdir()  # the session's own (empty) folder becomes the data directory
    env = setup_env(tmp_path, CLAUDE_CODE_HOST_SESSION_ID="in-place", CLAUDECODE="1")
    answers = write_answers(tmp_path, home)
    args = ("install", "--answers", str(answers), "--claude", str(claude), "--no-browser")
    # The user settings cannot be written (not JSON): the install fails after the home step.
    settings_file = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    settings_file.write_text("{not json", encoding="utf-8")
    code, _, _ = run_setup(product, *args, env=env, cwd=home)
    assert code == 3 and stood_down(home, "in-place") == "setup"
    settings_file.write_text("{}", encoding="utf-8")
    # The rerun in the same session, whose folder is now the data directory, still counts as
    # the installer; a SessionStart there does not take the lease.
    code, out, text = run_setup(product, *args, env=env, cwd=home)
    assert code == 0, text
    assert out["install_session"]["session_id"] == "in-place"
    launcher = product / "plugin" / "bin" / "tsw_launch.py"
    hook = subprocess.run(
        [sys.executable, "-X", "utf8", str(launcher), "guard", "SessionStart"],
        input=json.dumps({"session_id": "in-place", "cwd": str(home)}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**env, "TSW_HOME": str(home)},
        cwd=str(home),
        timeout=120,
        check=False,
    )
    assert hook.returncode == 0, hook.stderr
    from tsw import lease  # noqa: PLC0415
    from tsw.home import open_home  # noqa: PLC0415
    from tsw.ledger import Ledger  # noqa: PLC0415

    with Ledger(open_home(home)).view() as conn:
        held = lease.lease_row(conn)
    assert held is None or held["holder_session"] != "in-place"


@pytest.mark.use_case(UC)
def test_saved_answers_with_an_unimplemented_reviewer_still_rerun(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    home = tmp_path / "data"
    # An earlier version saved claude-headless when no codex was found.
    state = Path(env["TSW_STATE_DIR"])
    state.mkdir(parents=True, exist_ok=True)
    saved = {
        "home": str(home),
        "language": "zh-CN",
        "reviewer": "claude-headless",
        "human_channels": "in-session",
        "host": "claude-code",
        "dashboard": False,
        "dashboard_port": 47801,
        "user_settings": True,
    }
    (state / "setup-answers.json").write_text(json.dumps(saved), encoding="utf-8")
    # The bare rerun (update-plan's reinstall) takes the implemented reviewer and says so.
    code, out, text = run_setup(
        product, "install", "--claude", str(claude), "--no-browser", env=env
    )
    assert code == 0, text
    assert any(w.startswith("reviewer：答案中的评审器 claude-headless") for w in out["warnings"])
    # The interactive entry asks again instead of stopping before the questions.
    (state / "setup-answers.json").write_text(json.dumps(saved), encoding="utf-8")
    replies = ["", "", "", "", "否", "", "", "是"]
    code, out, text = run_setup(
        product,
        "install",
        "--interactive",
        "--no-browser",
        "--claude",
        str(claude),
        env=env,
        stdin="\n".join(replies) + "\n",
    )
    assert code == 0, text
    again = json.loads((state / "setup-answers.json").read_text("utf-8"))
    assert again["reviewer"] == "codex-app-server" and again["home"] == str(home)
    # An answers file given explicitly is checked as it is: the reviewer is refused.
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps(saved), encoding="utf-8")
    code, out, _ = run_setup(
        product, "plan", "--answers", str(explicit), "--claude", str(claude), env=env
    )
    assert code == 3 and any(p.startswith("reviewer：") for p in out["problems"])
