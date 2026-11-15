"""The self-update's category rules and actions (DN006 §9.8, CC006 §11.3; T283)."""

from __future__ import annotations

from typing import Any

import pytest

from tsw.config import DEFAULTS
from tsw.host import update_plan as up

pytestmark = pytest.mark.use_case("UC-HOST-009")

CLAUDE_CODE_OLD = 'X = 1\nCLAUDE_MD = """# 数据目录\n- 回复用户使用{language}。\n"""\n'
CLAUDE_CODE_NEW = 'X = 2\nCLAUDE_MD = """# 数据目录\n- 先读 tsw-update。\n"""\n'


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("docs/design/DN006-plugin-layer.md", ["docs"]),
        ("tests/unit/test_x.py", ["docs"]),
        ("tools/gate.py", ["docs"]),
        (".github/workflows/ci.yml", ["docs"]),
        ("README.md", ["docs"]),
        ("README.html", ["docs"]),
        ("pyproject.toml", ["install"]),
        ("plugin/roles/worker.md", ["rendered"]),
        ("plugin/hooks/hooks.json", ["plugin"]),
        ("plugin/bin/tsw_launch.py", ["plugin"]),
        ("tsw/host/orchestrate_skill.md", ["rendered"]),
        ("tsw/host/update_skill.md", ["rendered"]),
        ("tsw/host/claude_code.py", ["rendered", "runtime", "dashboard"]),
        ("tsw/roles.py", ["rendered", "runtime", "dashboard"]),
        ("tsw/ledger.py", ["runtime", "dashboard"]),
        ("tsw/cli.py", ["runtime", "dashboard"]),
        ("tsw/host/status.css", ["runtime", "dashboard"]),
    ],
)
def test_each_path_has_its_categories(path: str, expected: list[str]) -> None:
    assert up.classify(path) == expected


def test_version_constants_and_the_claude_md_template_decide_by_content() -> None:
    texts = {
        ("from", "tsw/projection.py"): "REDUCER_VERSION = 5\n",
        ("to", "tsw/projection.py"): "REDUCER_VERSION = 6\n",
        ("from", "tsw/context/index.py"): "SCHEMA_VERSION = 1\n",
        ("to", "tsw/context/index.py"): "SCHEMA_VERSION = 1\n# comment\n",
        ("from", "tsw/host/claude_code.py"): CLAUDE_CODE_OLD,
        ("to", "tsw/host/claude_code.py"): CLAUDE_CODE_NEW,
    }
    found = up.categorize(
        ["tsw/projection.py", "tsw/context/index.py", "tsw/host/claude_code.py", "docs/a.md"],
        lambda side, path: texts.get((side, path)),
    )
    assert found["projection"] == ["tsw/projection.py"]
    assert "index" not in found  # the version did not change
    assert found["instructions"] == ["tsw/host/claude_code.py"]
    assert found["docs"] == ["docs/a.md"]
    # Categories come in the order their actions run.
    assert list(found) == [c for c in up.CATEGORIES if c in found]


def test_an_unchanged_template_is_not_an_instructions_change() -> None:
    found = up.categorize(
        ["tsw/host/claude_code.py"],
        lambda side, path: (
            CLAUDE_CODE_OLD.replace("X = 1", "X = 3") if side == "to" else CLAUDE_CODE_OLD
        ),
    )
    assert "instructions" not in found
    assert up.claude_md_template(CLAUDE_CODE_OLD) == "# 数据目录\n- 回复用户使用{language}。\n"
    assert up.constant(None, "X") is None and up.constant("Y = 1\n", "X") is None


def test_only_the_mcp_servers_code_and_the_plugin_need_a_reload() -> None:
    categories = {
        "runtime": ["tsw/cli.py", "tsw/host/guard.py", "tsw/ledger.py"],
        "plugin": ["plugin/hooks/hooks.json"],
    }
    assert up.needs_reload(categories) == ["tsw/ledger.py", "plugin/hooks/hooks.json"]
    assert up.needs_reload({"runtime": ["tsw/cli.py"]}) == []


def test_task_ids_come_from_commit_subjects() -> None:
    subjects = [
        "host: the main session may run tsw host render (T282) (#48)",
        "host: flow lines (T278 r4) (#46)",
        "docs: T01 and T28 are transitions, T282 again",
    ]
    assert up.task_ids(subjects) == ["T282", "T278"]


def test_auto_mode_rules_are_missing_or_outdated() -> None:
    wanted = ["A Rule: new text", "B Rule: text", "C Rule: text"]
    allowed = ["A Rule: old text", "B Rule: text"]
    assert up.missing_rules(allowed, wanted) == [
        {"rule": "A Rule", "state": "outdated"},
        {"rule": "C Rule", "state": "missing"},
    ]


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "render": {"commit": "old"},
        "plugin": {"installed": True, "current": True},
        "dashboard": {"installed": True, "commit": "old"},
        "host_rules": [],
        "running": ["old"],
    }
    state.update(overrides)
    return state


def test_actions_run_in_order_with_impact_and_who() -> None:
    categories = {
        "projection": ["tsw/projection.py"],
        "rendered": ["tsw/host/orchestrate_skill.md"],
        "runtime": ["tsw/projection.py"],
        "dashboard": ["tsw/projection.py"],
    }
    planned = up.actions(categories, config=DEFAULTS, state=_state(), to="new")
    assert [a["action"] for a in planned] == [
        "ledger_rebuild",
        "render",
        "reread_skills",
        "dashboard_restart",
        "reload",
    ]
    by_name = {a["action"]: a for a in planned}
    assert by_name["ledger_rebuild"]["impact"] == "blocking"
    assert by_name["reread_skills"]["skills"] == ["orchestrate"]
    reload = by_name["reload"]
    assert (reload["mode"], reload["who"], reload["impact"]) == ("restart_host", "user", "session")
    assert by_name["reread_skills"]["manual"] is True
    # Re-reading cannot be seen in any state: the main session confirms it, it is never pending.
    assert [a["action"] for a in planned if a["pending"]] == [
        "ledger_rebuild",
        "render",
        "dashboard_restart",
        "reload",
    ]


def test_new_session_reload_is_the_main_sessions_own_step() -> None:
    config = {**DEFAULTS, "update": {"reload": "new_session", "instructions": "compact"}}
    planned = up.actions(
        {"plugin": ["plugin/hooks/hooks.json"]}, config=config, state=_state(), to="new"
    )
    reload = next(a for a in planned if a["action"] == "reload")
    assert (reload["mode"], reload["who"]) == ("new_session", "orchestrator")


@pytest.mark.parametrize(
    ("instructions", "mode", "who"),
    [
        ("compact", "compact", "user"),
        ("clear", "clear", "orchestrator"),
        ("reload", "restart_host", "user"),
    ],
)
def test_instructions_without_a_reload_follow_the_configuration(
    instructions: str, mode: str, who: str
) -> None:
    config = {**DEFAULTS, "update": {"reload": "restart_host", "instructions": instructions}}
    categories = {"instructions": ["tsw/host/claude_code.py"], "runtime": ["tsw/cli.py"]}
    planned = up.actions(categories, config=config, state=_state(), to="new")
    step = next(a for a in planned if a["action"] == "instructions")
    assert (step["mode"], step["who"]) == (mode, who)
    assert not any(a["action"] == "reload" for a in planned)


def test_a_change_to_the_claude_md_template_alone_follows_update_instructions() -> None:
    """The real classification: only the template in claude_code.py changed (DN003 §3.6)."""
    texts = {"from": CLAUDE_CODE_OLD, "to": CLAUDE_CODE_OLD.replace("{language}", "{language}！")}
    found = up.categorize(["tsw/host/claude_code.py"], lambda side, path: texts[side])
    assert found == {
        "rendered": ["tsw/host/claude_code.py"],
        "instructions": ["tsw/host/claude_code.py"],
    }
    state = _state(render={"commit": "new"}, dashboard={"installed": False}, running=["old"])
    planned = up.actions(found, config=DEFAULTS, state=state, to="new")
    assert [(a["action"], a.get("mode"), a["pending"]) for a in planned] == [
        ("render", None, False),
        ("instructions", "compact", False),
    ]
    assert planned[1]["manual"] is True
    # The template and code together: the code needs a reload, which reloads the instructions.
    both = up.categorize(
        ["tsw/host/claude_code.py"],
        lambda side, path: CLAUDE_CODE_OLD if side == "from" else CLAUDE_CODE_NEW,
    )
    assert "runtime" in both and "instructions" in both
    assert [a["action"] for a in up.actions(both, config=DEFAULTS, state=state, to="new")][
        -1
    ] == "reload"


def test_a_reload_covers_changed_instructions() -> None:
    categories = {"instructions": ["tsw/host/claude_code.py"], "runtime": ["tsw/ledger.py"]}
    planned = up.actions(categories, config=DEFAULTS, state=_state(), to="new")
    assert [a["action"] for a in planned if a["impact"] == "session"] == ["reload"]


def test_nothing_is_pending_once_everything_runs_the_target() -> None:
    state = _state(
        render={"commit": "new"}, dashboard={"installed": True, "commit": "new"}, running=["new"]
    )
    assert up.actions({}, config=DEFAULTS, state=state, to="new") == []


def test_a_skill_only_update_verifies_once_rendered() -> None:
    state = _state(render={"commit": "new"}, dashboard={"installed": False}, running=["old"])
    planned = up.actions(
        {"rendered": ["tsw/host/orchestrate_skill.md"]}, config=DEFAULTS, state=state, to="new"
    )
    assert [(a["action"], a["pending"]) for a in planned] == [
        ("render", False),
        ("reread_skills", False),
    ]


def test_a_projection_that_cannot_be_read_asks_for_the_rebuild_first() -> None:
    state = _state(projection="rebuild_required", render={"commit": "new"}, running=["new"])
    planned = up.actions({}, config=DEFAULTS, state=state, to="new")
    assert planned[0]["action"] == "ledger_rebuild" and planned[0]["pending"]


def test_state_alone_still_asks_for_what_is_behind() -> None:
    state = _state(
        render={"commit": "old"},
        plugin={"installed": True, "current": False},
        dashboard={"installed": True, "commit": "old"},
        host_rules=[{"rule": "TaskSteward Worker Merge", "state": "outdated"}],
        running=["new"],
    )
    planned = up.actions({}, config=DEFAULTS, state=state, to="new")
    assert [a["action"] for a in planned] == [
        "render",
        "plugin_copy",
        "dashboard_restart",
        "host_rules",
    ]
    assert all(a["pending"] for a in planned)


def test_a_dashboard_that_is_not_installed_is_left_alone() -> None:
    state = _state(dashboard={"installed": False}, render={"commit": "new"}, running=["new"])
    assert (
        up.actions({"dashboard": ["tsw/host/status.py"]}, config=DEFAULTS, state=state, to="new")
        == []
    )
