"""Pure rules of ``tsw setup`` (DN009 §5–§7, CC009 §5, §8; CC008 §5.8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tsw.config import DEFAULTS
from tsw.home import Home
from tsw.host import claude_install as ci
from tsw.host import guard
from tsw.host import update_plan as up
from tsw.host.claude_code import AUTO_MODE_RULES
from tsw.setup import questions as q
from tsw.setup import winenv
from tsw.setup.record import InstallRecord

UC = "UC-SETUP-001"
GATE = "UC-SETUP-002"
CHECKOUT = Path("C:/product")


def qs() -> list[q.Question]:
    return q.questions({"PATH": ""}, CHECKOUT)


@pytest.mark.use_case(UC)
def test_the_data_directory_is_required_and_has_no_default() -> None:
    home = next(x for x in qs() if x.id == "home")
    assert home.required and home.default is None
    _, problems = q.normalize({}, qs())
    assert any(p.startswith("home：必填") for p in problems)
    _, problems = q.normalize({"home": "  "}, qs())
    assert any(p.startswith("home：必填") for p in problems)
    _, problems = q.normalize({"home": "relative/dir"}, qs())
    assert any("绝对路径" in p for p in problems)


@pytest.mark.use_case(UC)
def test_answers_get_defaults_and_are_checked() -> None:
    home = str(Path.cwd() / "data")
    answers, problems = q.normalize({"home": home, "dashboard": "否", "user_settings": "yes"}, qs())
    assert problems == []
    assert answers["language"] == "zh-CN"
    assert answers["reviewer"] == "codex-app-server"  # the default reviewer
    assert answers["dashboard"] is False and answers["user_settings"] is True
    assert answers["dashboard_port"] == 47800 and answers["host"] == "claude-code"
    assert answers["checkout"] == str(CHECKOUT)
    _, problems = q.normalize(
        {"home": home, "reviewer": "gpt", "dashboard_port": 70000, "token": "x"}, qs()
    )
    assert "未知的答案：token" in problems
    assert any(p.startswith("reviewer：") for p in problems)
    assert any(p.startswith("dashboard_port：") for p in problems)


@pytest.mark.use_case(UC)
def test_repositories_and_accounts_are_never_asked() -> None:
    ids = {x.id for x in qs()}
    assert ids == {
        "home",
        "checkout",
        "language",
        "reviewer",
        "reviewer_model",
        "reviewer_effort",
        "human_channels",
        "host",
        "dashboard",
        "dashboard_port",
        "workdeck",
        "user_settings",
    }
    checkout = next(x for x in qs() if x.id == "checkout")
    assert checkout.asked_by == "entry"


@pytest.mark.use_case(UC)
def test_the_initial_configuration_follows_the_answers(home: Home) -> None:
    from tsw.config import load_config  # noqa: PLC0415

    base: dict[str, Any] = {
        "home": "C:/data",
        "language": "en-US",
        "reviewer": "claude-headless",
        "human_channels": "teams-chat",
        "host": "claude-code",
        "dashboard": True,
        "dashboard_port": 47900,
        "user_settings": True,
    }
    layer = q.config_layer(base)
    assert layer["plugins"]["human_channels"] == ["in-session", "teams-chat"]
    assert "sessions" not in layer["plugins"]  # the default list loads every reviewer
    assert layer["roles"]["reviewer"] == {
        "session": "claude-headless",
        "fallback_session": None,
        "model": "claude-sonnet-5-5",
        "effort": "high",
    }
    assert "repositories" not in layer and "accounts" not in layer
    config = load_config(home, main_layer=layer)
    assert config.valid and not any("独立性" in w for w in config.warnings)
    codex = q.config_layer({**base, "reviewer": "codex-app-server"})
    # Codex: the default model written out, the default fallback kept (DN004 §3.4).
    assert codex["roles"]["reviewer"] == {
        "session": "codex-app-server",
        "model": "gpt-6-astra",
        "effort": "medium",
    }
    assert "sessions" not in codex["plugins"]
    assert load_config(home, main_layer=codex).get("roles.reviewer.fallback_session") == (
        "codex-exec"
    )
    # The model and effort the user picked from the reviewer's list are written.
    picked = q.config_layer(
        {
            **base,
            "reviewer": "copilot-cli",
            "reviewer_model": "gpt-6.2-sol",
            "reviewer_effort": "high",
        }
    )
    assert picked["roles"]["reviewer"] == {
        "session": "copilot-cli",
        "fallback_session": None,
        "model": "gpt-6.2-sol",
        "effort": "high",
    }
    assert load_config(home, main_layer=picked).valid
    copilot = q.config_layer({**base, "reviewer": "copilot-cli"})
    assert copilot["roles"]["reviewer"]["session"] == "copilot-cli"
    assert load_config(home, main_layer=copilot).valid
    # none: no self-review (DN004 §9), the reviewer stays the default.
    none = q.config_layer({**base, "reviewer": "none"})
    assert none["review"] == {"self_review": "off"} and "roles" not in none
    config = load_config(home, main_layer=none)
    assert config.valid and config.get("review.self_review") == "off"
    same = q.config_differences(config.values, {**base, "reviewer": "none"})
    assert not any(d.startswith(("review.self_review", "roles.reviewer")) for d in same)
    assert any(
        d.startswith("review.self_review：配置为 off")
        for d in q.config_differences(config.values, {**base, "reviewer": "codex-app-server"})
    )


@pytest.mark.use_case(UC)
def test_the_terminal_asks_again_until_the_data_directory_is_given() -> None:
    replies = iter(["", str(Path.cwd() / "data"), "", "", "", "", "", ""])
    said: list[str] = []
    answers = q.ask(qs(), {}, lambda _prompt: next(replies), said.append)
    assert answers["home"] == str(Path.cwd() / "data")
    assert "  这一项必须由你给出。" in said
    assert answers["host"] == "claude-code"  # only one host: chosen without asking
    assert "checkout" not in answers  # the entry decided it


@pytest.mark.use_case(UC)
def test_path_entries_are_appended_and_removed_without_touching_the_rest() -> None:
    shims = Path("D:/profile/AppData/Local/TaskSteward/bin")
    value = r"C:\Windows;%USERPROFILE%\bin;"
    added = winenv.with_entry(value, shims)
    assert added == r"C:\Windows;%USERPROFILE%\bin;" + str(shims)
    assert winenv.with_entry(added, shims) == added
    assert winenv.has_entry(added.upper(), shims)  # Windows paths compare case-insensitively
    assert winenv.has_entry(f'"{shims}"', shims)
    assert winenv.without_entry(added, shims) == r"C:\Windows;%USERPROFILE%\bin"
    assert winenv.with_entry(None, shims) == str(shims)


@pytest.mark.use_case(UC)
def test_the_shims_run_the_checkout_launcher_with_the_interpreter() -> None:
    texts = winenv.shim_texts(r"C:\Py\python.exe", Path(r"C:\TSW\task-steward"))
    assert texts["tsw.cmd"] == (
        r'@setlocal & set "TSW_SHIM=%~f0" & "C:\Py\python.exe" -X utf8 '
        r'"C:\TSW\task-steward\plugin\bin\tsw_launch.py" %*' + "\r\n"
    )
    assert texts["tsw"] == (
        '#!/bin/sh\nexec "C:/Py/python.exe" -X utf8 '
        '"C:/TSW/task-steward/plugin/bin/tsw_launch.py" "$@"\n'
    )


@pytest.mark.use_case(UC)
def test_user_settings_are_merged_by_rule_name_and_nothing_else_changes() -> None:
    old = AUTO_MODE_RULES[1].split(":")[0] + ": old text"
    data: dict[str, Any] = {
        "model": "x",
        "env": {"OTHER": "1"},
        "autoMode": {"allow": ["Mine: keep", old, old]},
    }
    values = {"TSW_HOME": "C:/data", "TSW_PYTHON": "C:/py.exe"}
    merged = ci.merged_settings(data, values, AUTO_MODE_RULES)
    assert merged["model"] == "x" and merged["env"]["OTHER"] == "1"
    assert merged["autoMode"]["allow"] == [
        "Mine: keep",
        AUTO_MODE_RULES[1],
        AUTO_MODE_RULES[0],
        *AUTO_MODE_RULES[2:],
    ]
    assert data["autoMode"]["allow"][1] == old  # the input is not changed
    assert ci.settings_diff(merged, values, AUTO_MODE_RULES) == []
    diff = ci.settings_diff(data, values, AUTO_MODE_RULES)
    assert [d["key"] for d in diff] == [
        "env.TSW_HOME",
        "env.TSW_PYTHON",
        "autoMode.allow[TaskSteward Worker Dispatch]",
        "autoMode.allow[TaskSteward Worker Merge]",
        "autoMode.allow[TaskSteward Routine]",
        "autoMode.allow[TaskSteward Worker Answer]",
    ]
    assert [d["before"] for d in diff[2:]] == ["missing", "outdated", "missing", "missing"]


@pytest.mark.use_case(UC)
def test_removing_user_settings_takes_back_only_what_is_still_ours(tmp_path: Path) -> None:
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    settings = tmp_path / "settings.json"
    names = list(ci.RULE_NAMES)
    settings.write_text(
        json.dumps(
            {
                "env": {"TSW_HOME": "C:/data", "TSW_PYTHON": "C:/changed-by-user.exe"},
                "autoMode": {"allow": [*AUTO_MODE_RULES, "Mine: keep"]},
            }
        ),
        encoding="utf-8",
    )
    owned = {
        "written_env": {"TSW_HOME": "C:/data", "TSW_PYTHON": "C:/py.exe"},
        "env_before": {"TSW_HOME": None, "TSW_PYTHON": None},
        "rules": names,
        "rules_before": [names[1]],  # the user had added Worker Merge by hand
    }
    report = ci.remove_user_settings(env, owned, tmp_path / "backups")
    after = json.loads(settings.read_text(encoding="utf-8"))
    assert after["env"] == {"TSW_PYTHON": "C:/changed-by-user.exe"}
    assert after["autoMode"]["allow"] == [AUTO_MODE_RULES[1], "Mine: keep"]
    assert report["backup"] and Path(report["backup"]).is_file()
    assert ci.remove_user_settings(env, owned, tmp_path / "backups")["changed"] == []


@pytest.mark.use_case(UC)
def test_install_ownership_recorded_first_survives_a_rerun(tmp_path: Path) -> None:
    env = {"TSW_STATE_DIR": str(tmp_path)}
    record = InstallRecord.load(env)
    record.begin("path", {"entry": "C:/bin", "added": True})
    again = InstallRecord.load(env)
    entry = again.begin("path", {"entry": "C:/bin", "added": False})  # now it looks present
    assert entry["added"] is True and entry["state"] == "started"
    again.finish("path")
    assert InstallRecord.load(env).step("path")["state"] == "done"


@pytest.mark.use_case(UC)
def test_update_plan_reruns_the_install_for_a_setup_install() -> None:
    state: dict[str, Any] = {
        "render": {"commit": "new"},
        "plugin": {"installed": True, "current": True},
        "host_rules": [{"rule": "TaskSteward Routine", "state": "outdated"}],
        "running": ["new"],
        "setup": {"installed": True, "user_settings": True},
    }
    planned = {
        a["action"]: a
        for a in up.actions({"install": ["setup.ps1"]}, config=DEFAULTS, state=state, to="new")
    }
    assert planned["reinstall"]["who"] == "orchestrator"
    assert "tsw setup install" in planned["reinstall"]["how"]
    assert planned["host_rules"]["who"] == "orchestrator"
    assert "tsw setup install" in planned["host_rules"]["how"]
    manual = {**state, "setup": {}}
    planned = {
        a["action"]: a
        for a in up.actions({"install": ["setup.ps1"]}, config=DEFAULTS, state=manual, to="new")
    }
    assert planned["reinstall"]["who"] == "user"
    assert "install-hint" in planned["host_rules"]["how"]


@pytest.mark.use_case(GATE)
@pytest.mark.parametrize(
    ("command", "worker", "orchestrator", "refused"),
    [
        ("tsw setup install --answers a.json", True, False, True),
        ("python -X utf8 -m tsw setup uninstall --yes", True, False, True),
        ("python C:/x/plugin/bin/tsw_launch.py setup install --answers a.json", True, False, True),
        ("tsw setup install --answers a.json", False, True, False),
        ("tsw setup check --questions", True, False, False),
        ("tsw setup plan --answers a.json", True, False, False),
        ("tsw setup verify --after-reload", True, False, False),
        ("tsw setup models --reviewer copilot-cli --json", True, False, False),
    ],
)
def test_the_hook_keeps_workers_from_installing_the_host(
    home: Home, command: str, worker: bool, orchestrator: bool, refused: bool
) -> None:
    problem = guard.bash_problem(
        command,
        home=home,
        push_allowed=False,
        pr_allowed=False,
        worker=worker,
        orchestrator=orchestrator,
    )
    assert (problem is not None) is refused
    if refused:
        assert problem is not None and "不能运行 tsw setup" in problem


WORKDECK = "UC-SETUP-004"


@pytest.mark.use_case(WORKDECK)
def test_the_workdeck_question_is_asked_only_where_workdeck_is_found(tmp_path: Path) -> None:
    found = q.questions({"PATH": ""}, CHECKOUT)
    question = next(x for x in found if x.id == "workdeck")
    assert question.default is False and "找不到 workdeck" in question.note
    # Nothing to ask without the command: the answer is no, and no reply is used for it.
    replies = iter([str(tmp_path / "data"), "", "", "", "", "", ""])
    answers = q.ask(found, {}, lambda _prompt: next(replies), lambda _text: None)
    assert answers["workdeck"] is False and answers["user_settings"] is True
    folder = tmp_path / "npm"
    folder.mkdir()
    (folder / "workdeck.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (folder / "workdeck").write_text("#!/bin/sh\n", encoding="utf-8")
    (folder / "workdeck").chmod(0o755)
    here = q.questions({"PATH": str(folder)}, CHECKOUT)
    question = next(x for x in here if x.id == "workdeck")
    assert question.default is True and "替换" in question.note
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return {"数据目录": str(tmp_path / "data"), "workdeck": "否"}.get(
            next((k for k in ("数据目录", "workdeck") if k in prompt), ""), ""
        )

    answers = q.ask(here, {}, read, lambda _text: None)
    assert answers["workdeck"] is False
    assert any("workdeck 门户" in p and "（默认 是）" in p for p in prompts)
    normalized, problems = q.normalize({"home": "C:/data"}, here)
    assert normalized["workdeck"] is True and not problems


@pytest.mark.use_case(UC)
def test_only_implemented_reviewers_are_offered_and_a_missing_codex_is_a_warning() -> None:
    reviewer = next(x for x in qs() if x.id == "reviewer")
    assert reviewer.choices == ("codex-app-server", "claude-headless", "copilot-cli", "none")
    assert reviewer.default == "codex-app-server"
    _, problems = q.normalize({"home": str(Path.cwd() / "d"), "reviewer": "claude-subagent"}, qs())
    assert any(p.startswith("reviewer：") for p in problems)
    # The install goes on; tasks cannot review until the chosen reviewer's command is there.
    assert "codex" in str(q.reviewer_warning({"PATH": ""}))
    assert "copilot" in str(q.reviewer_warning({"PATH": ""}, "copilot-cli"))
    assert "claude -p" in str(q.reviewer_warning({"PATH": ""}, "claude-headless"))
    assert q.reviewer_warning({"PATH": ""}, "none") is None
