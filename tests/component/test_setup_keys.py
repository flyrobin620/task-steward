"""The arrow-key interface of ``tsw setup install --interactive`` (DN009 §16, CC009 §11).

The console is the scripted stand-in (``tests/terminal_harness.py``): keys and lines come from
the test, what is written is replayed on a terminal model. Everything behind it is real, as in
``test_setup.py``: the product checkout's runtime, files, git, the shims, the data directory and
``host render``; Claude Code's plugin commands, the user ``Path`` and Task Scheduler are the
usual stand-ins (simulated).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.setup_harness import fake_claude, make_product, record, setup_env

UC = "UC-SETUP-005"
DRIVER = Path(__file__).resolve().parents[1] / "fixtures" / "drive_setup_keys.py"


def drive(tmp_path: Path, script: list[Any] | dict[str, Any], *args: str) -> dict[str, Any]:
    product, claude = make_product(tmp_path), fake_claude(tmp_path)
    env = setup_env(tmp_path)
    script_file, out_file = tmp_path / "script.json", tmp_path / "out.json"
    script_file.write_text(json.dumps(script), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(DRIVER),
            str(product),
            str(script_file),
            str(out_file),
            "--no-browser",
            "--claude",
            str(claude),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(tmp_path),
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out: dict[str, Any] = json.loads(out_file.read_text(encoding="utf-8"))
    out["env"] = env
    return out


def saved_answers(env: dict[str, str]) -> dict[str, Any]:
    return json.loads((Path(env["TSW_STATE_DIR"]) / "setup-answers.json").read_text("utf-8"))


@pytest.mark.use_case(UC)
def test_choices_are_made_with_keys_and_answered_questions_collapse(tmp_path: Path) -> None:
    home = tmp_path / "data"
    script: list[Any] = [
        ["line", "data"],  # not absolute: refused, asked again
        ["line", str(home)],
        ["line", ""],  # language: the default
        "enter",  # reviewer: the default
        "down",  # human channels: move to teams-chat …
        "esc",  # … then back to the reviewer
        "enter",  # reviewer again
        "end",
        "up",
        "enter",  # human channels: in-session
        "n",  # Dashboard service: no, at once
        ["line", ""],  # port: the default
        "enter",  # user settings: yes (the default)
        "down",
        "enter",  # the plan: change an answer …
        "6",
        "enter",  # … the port
        ["line", "47802"],
        "enter",  # the new plan: install
    ]
    out = drive(tmp_path, script)
    assert out["code"] == 0, out
    assert out["left"] == []
    assert out["result"] == {"_silent": True, "ok": True}
    answers = saved_answers(out["env"])
    assert answers["home"] == str(home)
    assert answers["human_channels"] == "in-session"
    assert answers["dashboard"] is False
    assert answers["dashboard_port"] == 47802
    assert record(out["env"])["status"] == "installed"
    screen = out["screen"]
    # Each answered question is one line; the questions themselves and the refusal are gone.
    assert f"√ 数据目录：{home}" in screen
    assert "√ 怎样找到你：只在会话中（in-session）" in screen
    assert "√ Dashboard 常驻服务：否" in screen
    assert "√ Dashboard 端口：47802" in screen
    assert "√ 宿主：Claude Code（claude-code）" in screen  # one host: chosen without asking
    assert "须为绝对路径" not in screen
    assert "问题 1/" not in screen and "↑↓ 选择" not in screen
    # The reviewer was answered once on the screen although it was asked twice.
    assert screen.count("√ 评审器：") == 1
    # The plan, each step once installed, the checks and what to do next.
    assert re.search(r"► Claude Code 插件 +要做", screen)
    # Changing an answer showed the plan again below the first one, which stayed whole.
    # (the last step of both plans, and of the install)
    assert screen.count(" 计划") == 2 and len(re.findall(r"– 接入 workdeck +不做", screen)) == 3
    assert re.search(r"√ Claude Code 插件 +已完成", screen) and "正在" not in screen
    assert re.search(r"√ 插件来源指向检出 +通过", screen)
    assert "下一步" in screen and "/orchestrate" in screen
    # Colour and cursor sequences were written; the screen model accepted every one of them.
    assert "\x1b[32m√\x1b[0m" in out["output"] and "\x1b[?25l" in out["output"]


@pytest.mark.use_case(UC)
def test_ctrl_c_cancels_before_anything_is_installed(tmp_path: Path) -> None:
    home = tmp_path / "data"
    out = drive(tmp_path, [["line", str(home)], ["line", ""], "interrupt"])
    assert out["code"] == 0 and out["left"] == []
    assert out["result"] == {"_silent": True, "installed": False, "note": "已取消，没有安装"}
    assert "已取消，没有执行任何安装步骤" in out["screen"]
    assert not (Path(out["env"]["TSW_STATE_DIR"]) / "install.json").exists()
    assert not home.exists()


@pytest.mark.use_case(UC)
@pytest.mark.parametrize("json_flag", [(), ("--json",)])
def test_cancelling_at_the_plan_keeps_the_answers(
    tmp_path: Path, json_flag: tuple[str, ...]
) -> None:
    home = tmp_path / "data"
    keys: list[Any] = [["line", str(home)], ["line", "en-US"], "enter", "enter", "y"]
    keys += [["line", ""], "enter", "up", "enter"]  # the plan: “取消”, the last option
    out = drive(tmp_path, keys, *json_flag)
    assert out["code"] == 0 and out["left"] == []
    # The screen said it all; with --json the cancelled result is still the JSON (DN009 §16.3).
    assert out["result"].pop("_silent", False) is not bool(json_flag)
    assert out["result"]["installed"] is False and out["result"]["answers_file"]
    assert saved_answers(out["env"])["language"] == "en-US"
    assert saved_answers(out["env"])["dashboard"] is True
    assert "没有安装；答案已保存在" in out["screen"]
    assert not home.exists()


@pytest.mark.use_case(UC)
def test_lines_that_wrap_on_a_narrow_console_are_erased_whole(tmp_path: Path) -> None:
    home = tmp_path / "a-data-directory-whose-path-wraps-on-a-console-sixty-columns-wide"
    keys: list[Any] = [["line", str(home)], ["line", ""], "enter", "esc", "esc"]
    keys += [["line", "en-US"], "enter", "enter", "y", ["line", ""], "enter", "up", "enter"]
    out = drive(tmp_path, {"columns": 60, "keys": keys})
    assert out["code"] == 0 and out["left"] == [], out["screen"]
    screen = out["screen"]
    assert max(len(line) for line in screen.splitlines()) <= 60
    flat = screen.replace("\n", "")
    assert f"√ 数据目录：{home}" in flat
    assert screen.count("√ 交流语言：") == 1 and "√ 交流语言：en-US" in screen
    assert screen.count("√ 评审器：") == 1 and "↑↓" not in screen and "问题 " not in screen
    assert saved_answers(out["env"])["language"] == "en-US"
