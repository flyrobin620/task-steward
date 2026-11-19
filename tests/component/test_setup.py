"""``tsw setup`` on real files, git and subprocesses (DN009 §5, §9, §10; CC009 §3, §5, §8).

Each test installs from its own committed product checkout into a temporary user profile: Claude
Code's plugin commands, the user ``Path`` (HKCU) and Task Scheduler are stand-ins (simulated);
the files, git, the shims, the data directory, ``host render`` and the MCP handshake are real.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.setup_harness import (
    claude_calls,
    fake_claude,
    git,
    make_product,
    record,
    run_setup,
    set_user_path,
    settings,
    setup_env,
    user_path,
    write_answers,
)
from tsw.host.claude_code import AUTO_MODE_RULES

UC = "UC-SETUP-001"
RULE_NAMES = [r.split(":")[0] for r in AUTO_MODE_RULES]


def install(product: Path, answers: Path, env: dict[str, str], claude: Path) -> tuple[int, dict]:
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
    assert out, text
    return code, out


def steps_by_name(out: dict) -> dict[str, dict]:
    return {s["step"]: s for s in out["steps"]}


@pytest.mark.use_case(UC)
def test_a_terminal_install_from_nothing_reaches_a_verified_install(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    set_user_path(env, r"C:\Windows;%USERPROFILE%\bin")
    code, out = install(product, write_answers(tmp_path, home), env, claude)
    assert code == 0, out
    done = steps_by_name(out)
    assert done["plugin"]["report"]["after"]["points_at_checkout"] is True
    # Shims run the checkout's launcher with the recorded interpreter, for cmd and for Git Bash.
    shims = Path(env["TSW_STATE_DIR"]) / "bin"
    assert "tsw_launch.py" in (shims / "tsw.cmd").read_text(encoding="utf-8")
    assert (shims / "tsw").read_text(encoding="utf-8").startswith("#!/bin/sh\nexec ")
    # One entry appended to the user Path, the existing ones (unexpanded) kept.
    assert user_path(env) == rf"C:\Windows;%USERPROFILE%\bin;{shims}"
    # The plugin is registered from the checkout and installed at its HEAD.
    assert claude_calls(tmp_path)[:2] == [
        f"plugin marketplace add {product}",
        "plugin install task-steward@task-steward --scope user",
    ]
    # The data directory: created, its product repository and initial configuration recorded.
    marker = json.loads((home / "tsw-home.json").read_text(encoding="utf-8"))
    assert Path(marker["product_repo"]) == product.resolve()
    assert marker["rendered"]["commit"] == git(product, "rev-parse", "HEAD")
    assert (product / ".local" / "tsw-home").read_text(encoding="utf-8").strip() == str(home)
    config = json.loads((home / "config" / "task-steward.json").read_text(encoding="utf-8"))
    assert config["roles"]["reviewer"]["session"] == "claude-headless"
    assert config["dashboard"]["port"] == 47801
    assert "repositories" not in config and "accounts" not in config
    assert (home / "CLAUDE.md").is_file() and (home / "roles" / "worker.md").is_file()
    # The user settings: the data directory, the interpreter and the three rules.
    user = settings(env)
    assert user["env"]["TSW_HOME"] == str(home)
    assert user["env"]["TSW_PYTHON"]
    assert [r.split(":")[0] for r in user["autoMode"]["allow"]] == RULE_NAMES
    # Ownership recorded per step; the install-phase verify passes and only PATH waits a reload.
    installed = record(env)
    assert installed["status"] == "installed"
    assert installed["steps"]["path"]["added"] is True
    assert installed["steps"]["home"]["created"] is True
    assert installed["steps"]["user_settings"]["env_before"] == {
        "TSW_HOME": None,
        "TSW_PYTHON": None,
    }
    verify = out["verify"]
    assert verify["ok"] is True and verify["failed"] == []
    assert verify["pending_reload"] == ["path_effective"]
    checks = {c["check"]: c for c in verify["checks"]}
    assert checks["runtime_handshake"]["detail"]["server"]["name"] == "tsw"
    assert checks["home"]["detail"]["by"] == ".local/tsw-home"


@pytest.mark.use_case(UC)
def test_a_second_install_changes_nothing(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    answers = write_answers(tmp_path, home)
    assert install(product, answers, env, claude)[0] == 0
    before_calls = len(claude_calls(tmp_path))
    before_settings = settings(env)
    code, out = install(product, answers, env, claude)
    assert code == 0
    states = {s["step"]: s["state"] for s in out["steps"]}
    assert states == {
        "machine": "ok",
        "shim": "ok",
        "path": "ok",
        "plugin": "ok",
        "home": "ok",
        "user_settings": "ok",
        "render": "ok",
        "dashboard": "skipped",
    }
    assert len(claude_calls(tmp_path)) == before_calls  # no claude command ran again
    assert settings(env) == before_settings
    assert len(list((Path(env["TSW_STATE_DIR"]) / "backups").glob("*"))) == 0


@pytest.mark.use_case(UC)
def test_the_session_path_repoints_the_installed_plugin_at_the_checkout(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    # The user installed the plugin from the product repository in a session first.
    import subprocess  # noqa: PLC0415

    subprocess.run(
        [str(claude), "plugin", "install", "task-steward", "--marketplace", str(product)],
        env=env,
        check=True,
        capture_output=True,
    )
    code, out = install(product, write_answers(tmp_path, tmp_path / "data"), env, claude)
    assert code == 0, out
    calls = claude_calls(tmp_path)
    # Re-pointed, not reinstalled; host render then runs Claude Code's (idempotent) update.
    assert calls[1:] == [
        f"plugin marketplace add {product}",
        "plugin update task-steward@task-steward",
    ]
    assert record(env)["steps"]["plugin"]["installed_before"] is True
    assert record(env)["steps"]["plugin"]["marketplace_before"]["source"] == "git"


@pytest.mark.use_case(UC)
def test_an_interrupted_install_keeps_its_ownership_and_uninstall_takes_back_only_its_own(
    tmp_path: Path,
) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    set_user_path(env, r"C:\Tools")
    settings_file = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
    old_rule = RULE_NAMES[0] + ": an older text"
    settings_file.write_text(
        json.dumps(
            {
                "model": "opus",
                "env": {"TSW_HOME": "C:/old-home", "OTHER": "1"},
                "autoMode": {"allow": [old_rule, "Keep: mine"]},
            }
        ),
        encoding="utf-8",
    )
    answers = write_answers(tmp_path, home)
    # The plugin step fails after the shims and Path were changed.
    failing = {**env, "FAKE_CLAUDE_FAIL": "marketplace add"}
    code, out, _ = run_setup(
        product,
        "install",
        "--answers",
        str(answers),
        "--claude",
        str(claude),
        "--no-browser",
        env=failing,
    )
    assert code == 3 and out["details"]["reason"] == "plugin_failed"
    assert out["details"]["step"] == "plugin"
    assert record(env)["steps"]["path"]["added"] is True
    assert record(env)["steps"]["plugin"]["state"] == "started"
    # Rerun after the cause is fixed: the entry is there now, still recorded as ours.
    code, out = install(product, answers, env, claude)
    assert code == 0, out
    installed = record(env)
    assert installed["steps"]["path"]["added"] is True
    assert installed["steps"]["user_settings"]["env_before"]["TSW_HOME"] == "C:/old-home"
    assert installed["steps"]["user_settings"]["rules_before"] == [RULE_NAMES[0]]
    user = settings(env)
    assert user["model"] == "opus" and user["env"]["OTHER"] == "1"
    assert user["autoMode"]["allow"][0] == AUTO_MODE_RULES[0]  # replaced by name, in place
    assert "Keep: mine" in user["autoMode"]["allow"]
    backups = list((Path(env["TSW_STATE_DIR"]) / "backups").glob("claude-settings.*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["env"]["TSW_HOME"] == "C:/old-home"
    # Uninstall: only what the install added goes; the data directory stays.
    code, out, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert user_path(env) == r"C:\Tools"
    user = settings(env)
    assert user["env"] == {"TSW_HOME": "C:/old-home", "OTHER": "1"}
    assert user["autoMode"]["allow"] == [AUTO_MODE_RULES[0], "Keep: mine"]
    assert not (Path(env["TSW_STATE_DIR"]) / "bin").exists()
    assert (home / "tsw-home.json").is_file()
    assert out["kept"]["home"] == str(home)
    calls = claude_calls(tmp_path)
    # The install added both: removing the marketplace also uninstalls its plugin.
    assert calls[-1] == "plugin marketplace remove task-steward"
    assert (
        not (Path(env["CLAUDE_CONFIG_DIR"]) / "plugins" / "known_marketplaces.json")
        .read_text("utf-8")
        .count("task-steward")
    )
    assert record(env)["status"] == "uninstalled"


@pytest.mark.use_case(UC)
def test_an_existing_data_directory_is_reused_and_its_configuration_left_alone(
    tmp_path: Path,
) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    (home / "config").mkdir(parents=True)
    (home / "tsw-home.json").write_text(json.dumps({"home_version": 1}), encoding="utf-8")
    mine = {"version": 1, "control": {"language": "en-US"}}
    (home / "config" / "task-steward.json").write_text(json.dumps(mine), encoding="utf-8")
    env = setup_env(tmp_path)
    code, out = install(product, write_answers(tmp_path, home), env, claude)
    assert code == 0, out
    assert json.loads((home / "config" / "task-steward.json").read_text(encoding="utf-8")) == mine
    differences = steps_by_name(out)["home"]["config_differences"]
    assert any(d.startswith("control.language") for d in differences)
    assert record(env)["steps"]["home"]["created"] is False


@pytest.mark.use_case(UC)
def test_plan_refuses_a_missing_or_unusable_data_directory(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    no_home = tmp_path / "no-home.json"
    no_home.write_text(json.dumps({"language": "zh-CN"}), encoding="utf-8")
    code, out, _ = run_setup(
        product, "plan", "--answers", str(no_home), "--claude", str(claude), env=env
    )
    assert code == 3 and any(p.startswith("home：必填") for p in out["problems"])
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "notes.txt").write_text("x", encoding="utf-8")
    answers = write_answers(tmp_path, busy)
    code, out, _ = run_setup(
        product, "plan", "--answers", str(answers), "--claude", str(claude), env=env
    )
    assert code == 3 and any("不是空目录" in p for p in out["problems"])
    code, out, _ = run_setup(
        product, "install", "--answers", str(answers), "--claude", str(claude), env=env
    )
    assert code == 3 and out["details"]["reason"] == "setup_preconditions"
    assert not (Path(env["TSW_STATE_DIR"]) / "bin").exists()  # nothing was changed
    # Without any install record there is nothing of ours to uninstall.
    code, out, _ = run_setup(product, "uninstall", "--yes", env=env)
    assert code == 3 and out["details"]["reason"] == "not_installed"


@pytest.mark.use_case(UC)
def test_the_plan_shows_the_changes_before_anything_is_written(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    answers = write_answers(tmp_path, home)
    code, out, text = run_setup(
        product, "plan", "--answers", str(answers), "--claude", str(claude), env=env
    )
    assert code == 0, text
    steps = {s["step"]: s for s in out["steps"]}
    assert steps["plugin"]["actions"] == [
        f"claude plugin marketplace add {product}",
        "claude plugin install task-steward@task-steward --scope user",
    ]
    assert {d["key"] for d in steps["user_settings"]["diff"]} == {
        "env.TSW_HOME",
        "env.TSW_PYTHON",
        *(f"autoMode.allow[{n}]" for n in RULE_NAMES),
    }
    assert steps["home"]["config"]["control"]["language"] == "zh-CN"
    assert not home.exists() and not (Path(env["TSW_STATE_DIR"]) / "install.json").exists()
    assert claude_calls(tmp_path) == []


@pytest.mark.use_case(UC)
def test_manual_user_settings_and_the_dashboard_service(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    answers = write_answers(tmp_path, home, user_settings=False, dashboard=True)
    code, out = install(product, answers, env, claude)
    done = steps_by_name(out)
    assert done["user_settings"]["state"] == "manual"
    assert '"TSW_HOME"' in done["user_settings"]["manual"]
    assert not (Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json").exists()
    # The logon task is registered through the stand-in; the browser is not opened.
    tasks = json.loads((Path(env["TSW_STATE_DIR"]) / "fake-schtasks.json").read_text("utf-8"))
    assert any(name.startswith("TaskSteward Dashboard") for name in tasks["tasks"])
    assert done["dashboard"]["opened"] is False
    # No service answers in the test, so the install-phase verify reports it.
    assert code == 3 and "dashboard" in out["verify"]["failed"]
    checks = {c["check"]: c for c in out["verify"]["checks"]}
    assert checks["user_settings"]["ok"] is None
    code, out, _ = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0
    tasks = json.loads((Path(env["TSW_STATE_DIR"]) / "fake-schtasks.json").read_text("utf-8"))
    assert tasks["tasks"] == {}


@pytest.mark.use_case(UC)
def test_verify_after_reload_needs_the_shim_on_path_and_nothing_pending(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    assert install(product, write_answers(tmp_path, home), env, claude)[0] == 0
    code, out, _ = run_setup(product, "verify", "--after-reload", env=env)
    checks = {c["check"]: c for c in out["checks"]}
    assert code == 3 and checks["path_effective"]["ok"] is False
    reloaded = {**env, "PATH": str(Path(env["TSW_STATE_DIR"]) / "bin") + ";" + env["PATH"]}
    head = git(product, "rev-parse", "HEAD")
    code, out, text = run_setup(
        product, "verify", "--after-reload", "--running", head, env=reloaded
    )
    assert code == 0, text
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["path_effective"]["ok"] is True
    assert checks["update_plan"]["detail"]["pending"] == []


@pytest.mark.use_case(UC)
def test_the_cmd_shim_an_uninstall_runs_through_is_deleted_after_it_exits(tmp_path: Path) -> None:
    from tsw.setup import winenv  # noqa: PLC0415

    shims = tmp_path / "bin"
    winenv.write_shims(shims, "python", tmp_path / "product")
    report = winenv.remove_shims(shims, {"TSW_SHIM": str(shims / "tsw.cmd")})
    # cmd reads tsw.cmd again after tsw exits: it is deleted later, the sh shim at once.
    assert report == {"removed": ["tsw"], "deferred": ["tsw.cmd"]}
    assert (shims / "tsw.cmd").is_file() and not (shims / "tsw").exists()
    assert winenv.remove_shims(tmp_path / "none", {}) == {"removed": [], "deferred": []}


def _claude(claude: Path, env: dict[str, str], *args: str) -> None:
    import subprocess  # noqa: PLC0415

    subprocess.run([str(claude), *args], env=env, check=True, capture_output=True)


@pytest.mark.use_case(UC)
def test_uninstall_leaves_a_plugin_the_install_found_from_a_local_directory(
    tmp_path: Path,
) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    mine = make_product(tmp_path, "developer-checkout")
    env = setup_env(tmp_path)
    # A developer installed the plugin from another checkout of their own.
    _claude(claude, env, "plugin", "marketplace", "add", str(mine))
    _claude(claude, env, "plugin", "install", "task-steward@task-steward", "--scope", "user")
    assert install(product, write_answers(tmp_path, tmp_path / "data"), env, claude)[0] == 0
    assert record(env)["steps"]["plugin"]["marketplace_before"]["path"] == str(mine)
    code, _, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert claude_calls(tmp_path)[-3:] == [  # its old source again, and its plugin
        "plugin marketplace remove task-steward",
        f"plugin marketplace add {mine}",
        "plugin install task-steward@task-steward --scope user",
    ]
    plugins = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins"
    known = json.loads((plugins / "known_marketplaces.json").read_text("utf-8"))
    assert known["task-steward"]["source"]["path"] == str(mine)
    installed = json.loads((plugins / "installed_plugins.json").read_text("utf-8"))
    assert "task-steward@task-steward" in installed["plugins"]


@pytest.mark.use_case(UC)
def test_an_install_that_failed_before_the_plugin_step_leaves_the_plugin_alone(
    tmp_path: Path,
) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    _claude(claude, env, "plugin", "install", "task-steward", "--marketplace", str(product))
    # The user Path cannot be written (a folder where the value should be): the install stops.
    Path(env["TSW_REG"]).mkdir(parents=True)
    answers = write_answers(tmp_path, home)
    code, _, _ = run_setup(
        product,
        "install",
        "--answers",
        str(answers),
        "--claude",
        str(claude),
        "--no-browser",
        env=env,
    )
    assert code != 0 and "plugin" not in record(env)["steps"]
    Path(env["TSW_REG"]).rmdir()
    code, out, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert out["removed"]["plugin"]["note"] == "安装没有动过插件，保留"
    assert not any("uninstall" in call for call in claude_calls(tmp_path))


@pytest.mark.use_case(UC)
def test_only_a_user_scope_install_counts_as_installed(tmp_path: Path) -> None:
    import shutil  # noqa: PLC0415

    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    plugins = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins"
    plugins.mkdir(parents=True)
    copy = plugins / "project-copy"
    shutil.copytree(product / "plugin", copy)
    records = {
        "version": 2,
        "plugins": {
            "task-steward@task-steward": [
                {"scope": "project", "installPath": str(copy), "version": "x"}
            ]
        },
    }
    (plugins / "installed_plugins.json").write_text(json.dumps(records), encoding="utf-8")
    code, out = install(product, write_answers(tmp_path, tmp_path / "data"), env, claude)
    assert code == 0, out
    assert "plugin install task-steward@task-steward --scope user" in claude_calls(tmp_path)
    after = json.loads((plugins / "installed_plugins.json").read_text("utf-8"))
    scopes = [r["scope"] for r in after["plugins"]["task-steward@task-steward"]]
    assert scopes == ["project", "user"]  # the project install is left as it is


@pytest.mark.use_case(UC)
def test_a_dashboard_step_interrupted_after_the_task_was_created_is_recovered(
    tmp_path: Path,
) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    home = tmp_path / "data"
    env = setup_env(tmp_path)
    answers = write_answers(tmp_path, home, dashboard=True)
    install(product, answers, env, claude)
    # As if the run stopped right after the task was created: the record still says started.
    state = Path(env["TSW_STATE_DIR"]) / "install.json"
    data = json.loads(state.read_text(encoding="utf-8"))
    data["steps"]["dashboard"] = {
        "task": data["steps"]["dashboard"]["task"],
        "installed_before": False,
        "state": "started",
    }
    state.write_text(json.dumps(data), encoding="utf-8")
    calls = Path(env["TSW_STATE_DIR"]) / "fake-schtasks.json"
    before = len(json.loads(calls.read_text("utf-8"))["calls"])
    _, out = install(product, answers, env, claude)
    assert steps_by_name(out)["dashboard"].get("recovered") is True
    # The task may never have been started: the recovery starts it (End, then Run).
    later = [c[0] for c in json.loads(calls.read_text("utf-8"))["calls"][before:]]
    assert "/Run" in later
    assert record(env)["steps"]["dashboard"]["installed"] is True
    # Interrupted again, then uninstalled directly: the task is still taken back.
    data = json.loads(state.read_text(encoding="utf-8"))
    data["steps"]["dashboard"].update(state="started", installed=False)
    state.write_text(json.dumps(data), encoding="utf-8")
    code, _, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    tasks = json.loads((Path(env["TSW_STATE_DIR"]) / "fake-schtasks.json").read_text("utf-8"))
    assert tasks["tasks"] == {}


@pytest.mark.use_case(UC)
def test_uninstall_restores_a_remote_install_that_was_there_before(tmp_path: Path) -> None:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    # The session path's first step, or an earlier install from the product repository (its
    # address written differently from the checkout's folder, as a remote URL would be).
    remote = str(product) + "/"
    _claude(claude, env, "plugin", "install", "task-steward", "--marketplace", remote)
    assert install(product, write_answers(tmp_path, tmp_path / "data"), env, claude)[0] == 0
    code, out, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    calls = claude_calls(tmp_path)
    assert not any(call.startswith("plugin uninstall") for call in calls)
    assert calls[-3:] == [  # its old source again, and the plugin installed from it
        "plugin marketplace remove task-steward",
        f"plugin marketplace add {remote}",
        "plugin install task-steward@task-steward --scope user",
    ]
    notes = out["removed"]["plugin"]["notes"]
    assert any("claude plugin uninstall task-steward@task-steward" in n for n in notes)


@pytest.mark.use_case(UC)
def test_uninstall_restores_a_marketplace_that_had_no_user_install(tmp_path: Path) -> None:
    import shutil  # noqa: PLC0415

    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    mine = make_product(tmp_path, "developer-checkout")
    env = setup_env(tmp_path)
    _claude(claude, env, "plugin", "marketplace", "add", str(mine))
    plugins = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins"
    copy = plugins / "project-copy"
    shutil.copytree(product / "plugin", copy)
    project = {"scope": "project", "installPath": str(copy), "version": "x"}
    records = {"version": 2, "plugins": {"task-steward@task-steward": [project]}}
    (plugins / "installed_plugins.json").write_text(json.dumps(records), encoding="utf-8")
    assert install(product, write_answers(tmp_path, tmp_path / "data"), env, claude)[0] == 0
    code, _, text = run_setup(product, "uninstall", "--yes", "--claude", str(claude), env=env)
    assert code == 0, text
    assert claude_calls(tmp_path)[-2:] == [
        "plugin marketplace remove task-steward",  # also takes the user install it added
        f"plugin marketplace add {mine}",  # the developer's marketplace, as it was
    ]
    after = json.loads((plugins / "installed_plugins.json").read_text("utf-8"))
    assert after["plugins"]["task-steward@task-steward"] == [project]
