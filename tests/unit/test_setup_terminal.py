"""The terminal layer of ``tsw setup install --interactive`` (DN009 §16, CC009 §11): keys,
when the arrow-key interface and colour are used, widths, and the selection widget on a scripted
console."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.terminal_harness import Screen, ScriptedConsole
from tsw.setup import questions as q
from tsw.setup import terminal as t

UC = "UC-SETUP-005"


def keys_of(*chars: str) -> list[str]:
    stream: Iterator[str] = iter(chars)
    found: list[str] = []
    try:
        while True:
            found.append(t.decode_windows(lambda: next(stream)))
    except StopIteration:
        return found


@pytest.mark.use_case(UC)
def test_windows_function_keys_arrive_as_a_prefix_and_a_code() -> None:
    assert keys_of("\xe0", "H", "\x00", "P", "\xe0", "K", "\xe0", "M") == [
        t.UP,
        t.DOWN,
        t.LEFT,
        t.RIGHT,
    ]
    assert keys_of("\xe0", "G", "\xe0", "O", "\x00", ";") == [t.HOME, t.END, ""]
    assert keys_of("\r", "\x1b", "\x03", "y", "3") == [t.ENTER, t.ESC, t.INTERRUPT, "y", "3"]


def mode(env: dict[str, str] | None = None, /, **overrides: object) -> t.Mode:
    values: dict[str, object] = {
        "stdin_tty": True,
        "stdout_tty": True,
        "plain": False,
        "escapes": True,
        "keys_supported": True,
        "encoding": "utf-8",
    }
    values.update(overrides)
    return t.decide(env or {}, **values)  # type: ignore[arg-type]


@pytest.mark.use_case(UC)
def test_the_arrow_keys_and_colour_need_a_console_and_fall_back_otherwise() -> None:
    assert mode() == t.Mode(keys=True, color=True, unicode=True)
    # Piped input: the line questions, still in colour on the console (DN009 §16.4).
    assert mode(stdin_tty=False) == t.Mode(keys=False, color=True, unicode=True)
    for off in ({"stdout_tty": False}, {"escapes": False}, {"plain": True}):
        found = mode(**off)
        assert not found.keys and not found.color, off
    assert mode({"TERM": "dumb"}) == t.Mode(keys=False, color=False, unicode=True)
    assert mode({"NO_COLOR": "1"}) == t.Mode(keys=True, color=False, unicode=True)
    assert mode({"NO_COLOR": ""}).color  # an empty NO_COLOR does not count
    assert mode(keys_supported=False) == t.Mode(keys=False, color=True, unicode=True)
    assert not mode(encoding="cp437").unicode and not mode(encoding=None).unicode


@pytest.mark.use_case(UC)
def test_styles_and_symbols() -> None:
    plain = t.Style(t.Mode(keys=False, color=False, unicode=False))
    assert plain.bold("x") == "x" and plain.bad("x") == "x" and plain.selected("x") == "x"
    assert plain.mark("ok") == "+" and plain.mark("doing") == ">"
    keys_only = t.Style(t.Mode(keys=True, color=False, unicode=True))
    assert keys_only.mark("ok") == "√" and keys_only.ok("x") == "x"
    assert keys_only.selected("x") == "\x1b[7mx\x1b[0m"  # reverse video instead of colour
    colour = t.Style(t.Mode(keys=True, color=True, unicode=True))
    assert colour.mark("bad") == "\x1b[31m×\x1b[0m"
    assert colour.dim("") == ""


@pytest.mark.use_case(UC)
def test_widths_count_wide_characters_twice_and_escapes_not_at_all() -> None:
    assert t.display_width("abc") == 3
    assert t.display_width("数据目录") == 8
    assert t.display_width("\x1b[1;36m►\x1b[0m 是") == 4
    assert t.display_width("e\u0301") == 1
    assert t.rows("", 10) == 1 and t.rows("x" * 10, 10) == 1 and t.rows("x" * 11, 10) == 2
    assert t.rows("中" * 6, 10) == 2


def style() -> t.Style:
    return t.Style(t.Mode(keys=True, color=False, unicode=True))


OPTIONS = [t.Option("只在会话中", "in-session"), t.Option("另加 Teams 自聊", "teams-chat")]


@pytest.mark.use_case(UC)
@pytest.mark.parametrize(
    ("script", "start", "chosen"),
    [
        (["enter"], 1, 1),
        (["down", "enter"], 1, 0),  # round from the last to the first
        (["up", "enter"], 0, 1),
        (["right", "left", "left", "enter"], 0, 1),
        (["2", "9", "enter"], 0, 1),  # a digit past the list is ignored
        (["end", "home", "enter"], 1, 0),
        (["x", "esc", "enter"], 0, 0),  # Esc does nothing without a question to go back to
    ],
)
def test_choose_moves_with_the_keys_and_confirms_with_enter(
    script: list[str], start: int, chosen: int
) -> None:
    console = ScriptedConsole(script)
    assert t.choose(console, style(), ["", " 问题"], OPTIONS, start) == chosen
    assert console.script == []
    # The block is erased: nothing of the question is left on the screen.
    assert console.screen() == ""
    assert console.output().startswith("\x1b[?25l") and console.output().endswith("\x1b[?25h")


@pytest.mark.use_case(UC)
def test_choose_shortcuts_back_and_cancel() -> None:
    console = ScriptedConsole(["n"])
    yes_no = [t.Option("是"), t.Option("否")]
    assert t.choose(console, style(), [], yes_no, 0, shortcuts={"y": 0, "n": 1}) == 1
    assert t.choose(ScriptedConsole(["Y"]), style(), [], yes_no, 1, shortcuts={"y": 0}) == 0
    assert t.choose(ScriptedConsole(["esc"]), style(), [], OPTIONS, 0, back=True) is None
    console = ScriptedConsole(["down", "interrupt"])
    with pytest.raises(t.Cancelled):
        t.choose(console, style(), [], OPTIONS, 0)
    assert console.output().endswith("\x1b[?25h")  # the cursor is shown again


@pytest.mark.use_case(UC)
def test_the_cursor_line_is_marked_and_the_default_shown() -> None:
    console = ScriptedConsole(["down", "enter"])
    t.choose(console, style(), [" 怎样找到你"], OPTIONS, 0, back=True)
    # One frame per key: the first option had the cursor, then the second.
    frames = [
        Screen().feed(frame).text().splitlines()
        for frame in re.split(r"\x1b\[\d+F\x1b\[J", console.output())
    ]
    assert len(frames) == 3 and frames[2] == []
    first, second = frames[0], frames[1]
    assert first[0] == " 怎样找到你"
    # Labels are padded to one column (wide characters count twice), the values line up.
    assert first[1] == "   ► 只在会话中       in-session"
    assert first[2] == "     另加 Teams 自聊  teams-chat"
    assert second[1].startswith("     只在会话中") and second[2].startswith("   ► 另加")
    assert first[3] == "   ↑↓ 选择  回车 确认  Esc 上一问  Ctrl+C 取消"


@pytest.mark.use_case(UC)
def test_a_block_erases_the_rows_its_wrapped_lines_took() -> None:
    console = ScriptedConsole([("line", "D:\\a\\long\\path")], columns=10)
    console.write("kept\n")
    block = t.Block(console)
    block.line("1234567890abc")  # two rows
    reply = block.ask("> ")
    assert reply == "D:\\a\\long\\path" and block.used == 4
    block.erase()
    assert console.screen() == "kept"


@pytest.mark.use_case(UC)
def test_choice_labels_join_the_question_list_without_changing_the_choices() -> None:
    found = {x.id: x for x in q.questions({}, Path.cwd())}
    channels = found["human_channels"].to_dict()
    assert channels["choices"] == ["in-session", "teams-chat"]
    assert channels["labels"] == {"in-session": "只在会话中", "teams-chat": "另加 Teams 自聊"}
    assert "labels" not in found["language"].to_dict()
    assert found["dashboard_port"].short == "Dashboard 端口"


@pytest.mark.use_case(UC)
def test_a_wide_character_that_does_not_fit_moves_whole_to_the_next_row() -> None:
    line = "a" * 59 + "中" + "b" * 59
    assert t.rows(line, 60) == 3
    assert len(Screen(60).feed(line + "\n").text().splitlines()) == 3
    assert t.rows("a" * 58 + "中", 60) == 1


@pytest.mark.use_case(UC)
def test_the_interactive_entry_puts_the_probed_git_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tsw import cli  # noqa: PLC0415

    seen: list[str] = []

    def run(_args: argparse.Namespace) -> dict[str, object]:
        seen.append(os.environ["PATH"])
        return {}

    monkeypatch.setattr(cli.setup_interactive, "run", run)
    monkeypatch.setenv("PATH", r"C:\Windows")
    git = Path("C:/Program Files/Git/cmd/git.exe")
    cli._setup_interactive(argparse.Namespace(git=str(git)))
    assert seen == [os.pathsep.join([str(git.parent), r"C:\Windows"])]
