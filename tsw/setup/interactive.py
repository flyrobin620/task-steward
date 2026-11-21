"""The terminal entry's flow (DN009 §4.1, §16): check, ask, show the plan, confirm, install.

Two forms of the same flow over the one question list: the arrow-key interface when the console
supports it (:func:`run_keys`), and the line-by-line questions otherwise (:func:`run_lines`, the
form pipes, redirection and ``--plain`` get; its text is unchanged, colour only added when
stdout is a console). Which one runs is decided by :func:`tsw.setup.terminal.open_terminal`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable
from typing import Any

from tsw.errors import ErrorCode, TswError
from tsw.setup import questions as setup_questions
from tsw.setup import steps as setup_steps
from tsw.setup.questions import Question
from tsw.setup.record import save_answers
from tsw.setup.terminal import (
    Block,
    Cancelled,
    Console,
    Option,
    Style,
    choose,
    display_width,
    open_terminal,
)

STEP_TITLES = {
    "machine": "解释器与命令行",
    "shim": "tsw 命令（垫片）",
    "path": "用户 PATH",
    "plugin": "Claude Code 插件",
    "home": "数据目录",
    "user_settings": "Claude Code 用户设置",
    "render": "生成宿主文件（host render）",
    "dashboard": "Dashboard 常驻服务",
    "workdeck": "接入 workdeck",
}
CHECK_TITLES = {
    "shims": "tsw 命令（垫片）",
    "path_persisted": "用户 PATH",
    "plugin_source": "插件来源指向检出",
    "user_settings": "Claude Code 用户设置",
    "home": "数据目录",
    "config_valid": "配置有效",
    "runtime_handshake": "tsw 工具服务能启动",
    "plugin_copy": "插件副本是检出的版本",
    "dashboard": "Dashboard 常驻服务在运行",
    "workdeck": "workdeck 插件",
    "path_effective": "PATH 中的 tsw 是垫片",
    "update_plan": "没有待做的更新",
}
PLAN_STATES = {"ok": "已满足，跳过", "todo": "要做", "skipped": "不做", "manual": "手工"}
_BACK = object()


def run(args: argparse.Namespace) -> dict[str, Any]:
    mode, console, restore = open_terminal(plain=bool(getattr(args, "plain", False)))
    style = Style(mode)
    try:
        if console is None:
            try:
                return run_lines(args, style)
            except KeyboardInterrupt:
                raise _interrupted() from None
        return run_keys(args, console, style)
    finally:
        restore()


def _interrupted() -> TswError:
    """Ctrl+C outside a prompt: steps may have run; a rerun continues from the first step that
    did not finish (DN009 §5)."""
    return TswError(
        ErrorCode.REJECTED,
        "已中断：可能已经执行了部分步骤；重新运行安装会从没有完成的步骤继续",
        reason="interrupted",
    )


def _context(args: argparse.Namespace, answers: dict[str, Any] | None) -> setup_steps.Context:
    if answers is None:
        # The earlier answers, offered as defaults: --answers <file> when given, else the saved
        # ones (DN009 §3).
        return setup_steps.context(
            os.environ,
            answers_file=getattr(args, "answers", None),
            python=args.python,
            claude=args.claude,
            require_answers=False,
        )
    return setup_steps.context(
        os.environ, answers=answers, python=args.python, claude=args.claude, require_answers=True
    )


def _machine(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    """The read-only check with no answers: only the machine can block before the questions
    (saved answers that no longer hold are asked again)."""
    bare = setup_steps.context(
        os.environ, answers={}, python=args.python, claude=args.claude, require_answers=False
    )
    checked = setup_steps.check(bare)
    return checked, [p for p in checked["problems"] if not p.startswith("home：")]


def _preconditions(problems: list[str]) -> TswError:
    return TswError(
        ErrorCode.REJECTED, "安装的前提不满足", reason="setup_preconditions", problems=problems
    )


# ---------------------------------------------------------------------- line by line


def run_lines(args: argparse.Namespace, style: Style) -> dict[str, Any]:
    """The line-by-line questions (DN009 §16.4's fallback): the text of the earlier entry."""
    first = _context(args, None)
    checked, blocking = _machine(args)
    print(f"产品检出：{checked['checkout']['path']}（{checked['checkout']['head']}）")
    print(f"Python：{checked['python']['path']}（{checked['python']['version']}）")
    print(f"Claude Code 命令行：{checked['claude']}")
    if blocking:
        raise _preconditions(blocking)
    print(style.bold("\n请回答下面的问题（直接回车采用默认值）："))
    answers = setup_questions.ask(first.questions, first.answers, input, print)
    path = save_answers(first.env, answers)
    ctx = _context(args, answers)
    planned = setup_steps.plan(ctx)
    say_plan(planned, style)
    if not planned["ok"]:
        raise _preconditions(planned["problems"])
    reply = input("\n按上面的计划安装？[是 / 否]：").strip().lower()
    if reply not in ("y", "yes", "是", "好"):
        return {"installed": False, "answers_file": str(path), "note": "没有安装；答案已保存"}
    result = setup_steps.install(ctx, open_browser=not args.no_browser)
    if args.json:
        return result
    return say_installed(result, style)


def say_plan(planned: dict[str, Any], style: Style) -> None:
    print(style.bold("\n将要执行的步骤："))
    for step in planned["steps"]:
        state = PLAN_STATES.get(step.get("state", ""), step.get("state", ""))
        print(f"  - {step['step']}：{state}")
        actions: list[str] = step.get("actions") or []
        for action in actions:
            print(f"      {action}")
        changes: list[dict[str, Any]] = step.get("diff") or []
        for change in changes:
            print(f"      {change['key']}：{change['before']} → {change['after']}")
        if step.get("manual"):
            print(f"      {step.get('note', '')}")
            print(f"      文件：{step.get('file')}")
            print("\n".join(f"      {line}" for line in str(step["manual"]).splitlines()))
        if step.get("problem"):
            print(style.bad(f"      {step['problem']}"))
    for problem in planned["problems"]:
        print(style.bad(f"  问题：{problem}"))
    notes: list[str] = planned.get("warnings") or []
    for warning in notes:
        print(style.warn(f"  注意：{warning}"))


def say_installed(result: dict[str, Any], style: Style) -> dict[str, Any]:
    """The terminal's summary of an install: each step, the checks, what to do next."""
    print(style.bold("\n安装结果："))
    for step in result["steps"]:
        print(f"  - {step['step']}：{step.get('state')}")
        if step.get("manual"):
            print(f"    {step.get('note', '')}")
            print(f"    文件：{step.get('file')}")
            print("\n".join(f"    {line}" for line in str(step["manual"]).splitlines()))
    verify = result["verify"]
    for check in verify["checks"]:
        mark = {True: "通过", False: "失败", None: "未核对"}[check["ok"]]
        extra = "（重启后生效）" if check.get("pending_reload") else ""
        line = f"  核对 {check['check']}：{mark}{extra}"
        print(style.bad(line) if check["ok"] is False else line)
    warnings: list[str] = result.get("warnings") or []
    for warning in warnings:
        print(style.warn(f"  注意：{warning}"))
    out: dict[str, Any] = {
        "installed": True,
        "record": result["record"],
        "next": result["next"],
        "repositories": result["repositories"],
    }
    if not verify["ok"]:
        out["ok"] = False
        out["failed"] = verify["failed"]
    return out


# ---------------------------------------------------------------------- arrow keys


class _Screen:
    """Writes whole lines to the console; ``block`` starts a part that can be erased."""

    def __init__(self, console: Console, style: Style) -> None:
        self.console = console
        self.style = style
        self.installing = False

    def say(self, text: str = "") -> None:
        self.console.write(text + "\n")

    def title(self, text: str) -> None:
        self.say()
        self.say(f" {self.style.bold(self.style.accent(text))}")


def run_keys(args: argparse.Namespace, console: Console, style: Style) -> dict[str, Any]:
    """The arrow-key interface (DN009 §16.3)."""
    screen = _Screen(console, style)
    try:
        return _run_keys(args, screen)
    except (Cancelled, KeyboardInterrupt):
        screen.say()
        if screen.installing:
            # Ctrl+C while the steps run: some may be done (DN009 §5).
            screen.say(f" {style.mark('bad')} {style.bad(_interrupted().message)}")
            raise _interrupted() from None
        screen.say(f" {style.mark('warn')} {style.warn('已取消，没有执行任何安装步骤')}")
        return _quiet(args, {"installed": False, "note": "已取消，没有安装"})


def _quiet(args: argparse.Namespace, result: dict[str, Any]) -> dict[str, Any]:
    """The screen has said it all: no key-value lines after it, except the JSON ``--json``
    asks for (DN009 §16.3)."""
    return result if args.json else {"_silent": True, **result}


def _run_keys(args: argparse.Namespace, screen: _Screen) -> dict[str, Any]:
    style = screen.style
    first = _context(args, None)
    checked, blocking = _machine(args)
    screen.say(f" {style.bold(style.accent('TaskSteward 安装'))}")
    screen.title("环境")
    checkout = checked["checkout"]
    head = str(checkout["head"] or "")[:12]
    screen.say(f" {style.mark('ok')} 产品检出  {checkout['path']}  {style.dim(head)}")
    python = checked["python"]
    screen.say(f" {style.mark('ok')} Python    {python['path']}  {style.dim(python['version'])}")
    claude = checked["claude"]
    screen.say(f" {style.mark('ok' if claude else 'bad')} Claude Code 命令行  {claude}")
    if blocking:
        for problem in blocking:
            screen.say(f" {style.mark('bad')} {style.bad(problem)}")
        raise _preconditions(blocking)
    screen.title("问题")
    screen.say(f" {style.dim('回车采用默认值；已回答的问题收成一行')}")
    answers = _ask_all(screen, first.questions, first.answers)
    while True:
        path = save_answers(first.env, answers)
        ctx = _context(args, answers)
        planned = setup_steps.plan(ctx)
        _show_plan(screen, planned)
        if not planned["ok"]:
            raise _preconditions(planned["problems"])
        question = ["", f" {style.bold('按上面的计划安装吗？')}"]
        options = [Option("按计划安装"), Option("修改某个答案"), Option("取消", "答案已保存")]
        picked = choose(screen.console, style, question, options, 0)
        if picked == 0:
            break
        if picked == 2:
            screen.say(f" {style.mark('skip')} 没有安装；答案已保存在 {path}")
            note = "没有安装；答案已保存"
            return _quiet(args, {"installed": False, "answers_file": str(path), "note": note})
        _change_one(screen, first.questions, answers)
    result = _install(screen, ctx, open_browser=not args.no_browser)
    if args.json:
        return result
    _show_result(screen, result)
    return _quiet(args, {"ok": result["verify"]["ok"]})


def _asked(q: Question) -> bool:
    return q.asked_by == "setup" and setup_questions.preset(q) is None


def _shown(q: Question, value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    label = q.labels.get(str(value))
    return f"{label}（{value}）" if label else str(value)


def _ask_all(screen: _Screen, qs: list[Question], previous: dict[str, Any]) -> dict[str, Any]:
    """Ask each question in turn; an answered one collapses to one line, Esc in a choice goes
    back to the question before (DN009 §16.3)."""
    style = screen.style
    total = sum(1 for q in qs if _asked(q))
    answers: dict[str, Any] = {}
    shown: list[tuple[int, int]] = []  # (question index, rows its line takes)
    i = 0
    while i < len(qs):
        q = qs[i]
        if q.asked_by != "setup":
            line = f" {style.mark('ok')} {q.short}：{q.default}  {style.dim('（由安装入口决定）')}"
            shown.append((i, _put(screen, line)))
            i += 1
            continue
        fixed = setup_questions.preset(q)
        if fixed is not None:
            answers[q.id] = fixed[0]
            line = f" {style.mark('ok')} {style.dim(q.short + '：' + _shown(q, fixed[0]))}"
            shown.append((i, _put(screen, line)))
            i += 1
            continue
        position = sum(1 for earlier in qs[:i] if _asked(earlier)) + 1
        default = answers.get(q.id, setup_questions.starting_default(q, previous))
        value = _ask_one(screen, q, default, f"{position}/{total}", back=position > 1)
        if value is _BACK:
            target = max(j for j in range(i) if _asked(qs[j]))
            erase = sum(used for index, used in shown if index >= target)
            shown = [(index, used) for index, used in shown if index < target]
            if erase:
                screen.console.write(f"\x1b[{erase}F\x1b[J")
            i = target
            continue
        answers[q.id] = value
        shown.append((i, _put(screen, _answered(style, q, value))))
        i += 1
    return answers


def _answered(style: Style, q: Question, value: Any) -> str:
    return f" {style.mark('ok')} {q.short}：{style.bold(_shown(q, value))}"


def _put(screen: _Screen, line: str) -> int:
    block = Block(screen.console)
    block.line(line)
    return block.used


def _ask_one(screen: _Screen, q: Question, default: Any, position: str, *, back: bool) -> Any:
    style, console = screen.style, screen.console
    head = ["", f" {style.bold(q.prompt)}  {style.dim('问题 ' + position)}"]
    if q.note:
        head.append(f"   {style.dim(q.note)}")
    if q.kind in ("choice", "bool"):
        if q.kind == "choice":
            values: list[Any] = list(q.choices)
            options = [
                _option(q.labels.get(c, c), c if c in q.labels else "", c == q.default)
                for c in q.choices
            ]
            shortcuts = None
        else:
            values = [True, False]
            options = [_option("是", "", q.default is True), _option("否", "", q.default is False)]
            shortcuts = {"y": 0, "n": 1}
        start = values.index(default) if default in values else 0
        picked = choose(console, style, head, options, start, back=back, shortcuts=shortcuts)
        return _BACK if picked is None else values[picked]
    block = Block(console)
    error = ""
    while True:
        for text in head:
            block.line(text)
        if error:
            block.line(f"   {style.mark('bad')} {style.bad(error)}")
        hint = f"回车采用 {default}" if default not in (None, "") else "这一项必须由你给出"
        block.line(f"   {style.dim(hint)}")
        reply = block.ask(f"   {style.accent(style.glyph['cursor'])} ").strip()
        block.erase()
        value: Any = reply or default
        if value in (None, ""):
            error = "这一项必须由你给出。"
            continue
        checked, problems = setup_questions.normalize({q.id: value}, [q])
        if problems:
            error = "；".join(problems)
            continue
        return checked[q.id]


def _option(label: str, value: str, is_default: bool) -> Option:
    return Option(label, value + ("（默认）" if is_default else ""))


def _change_one(screen: _Screen, qs: list[Question], answers: dict[str, Any]) -> None:
    """The plan's “修改某个答案”: pick a question, answer it again."""
    style = screen.style
    asked = [q for q in qs if _asked(q)]
    head = ["", f" {style.bold('修改哪一个答案？')}"]
    options = [Option(q.short, _shown(q, answers[q.id])) for q in asked]
    picked = choose(screen.console, style, head, options, 0, back=True)
    if picked is None:
        return
    q = asked[picked]
    value = _ask_one(screen, q, answers[q.id], "修改", back=True)
    if value is _BACK:
        return
    answers[q.id] = value
    screen.say(_answered(style, q, value))


def _step_title(step: dict[str, Any]) -> str:
    name = str(step["step"])
    return STEP_TITLES.get(name, name)


def _show_plan(screen: _Screen, planned: dict[str, Any]) -> None:
    style = screen.style
    screen.title("计划")
    marks = {"todo": "doing", "ok": "ok", "skipped": "skip", "manual": "warn", "blocked": "bad"}
    steps: list[dict[str, Any]] = planned["steps"]
    width = max(display_width(_step_title(s)) for s in steps)
    for step in steps:
        state = str(step.get("state", ""))
        title = _step_title(step)
        title += " " * (width - display_width(title))
        word = PLAN_STATES.get(state, "受阻" if state == "blocked" else state)
        if state in ("ok", "skipped"):
            screen.say(f" {style.mark(marks[state])} {style.dim(title + '  ' + word)}")
        else:
            screen.say(f" {style.mark(marks.get(state, 'warn'))} {title}  {style.bold(word)}")
        details: list[str] = list(step.get("actions") or [])
        changes: list[dict[str, Any]] = step.get("diff") or []
        details += [f"{c['key']}：{c['before']} → {c['after']}" for c in changes]
        if state == "skipped" and step.get("note"):
            details.append(str(step["note"]))
        if step.get("manual"):
            details += [str(step.get("note", "")), f"文件：{step.get('file')}"]
            details += str(step["manual"]).splitlines()
        for detail in details:
            screen.say(f"     {style.dim(detail)}")
        if step.get("problem"):
            screen.say(f"     {style.bad(str(step['problem']))}")
    for problem in planned["problems"]:
        screen.say(f" {style.mark('bad')} {style.bad('问题：' + problem)}")
    notes: list[str] = planned.get("warnings") or []
    for warning in notes:
        screen.say(f" {style.mark('warn')} {style.warn('注意：' + warning)}")


def _install(screen: _Screen, ctx: setup_steps.Context, *, open_browser: bool) -> dict[str, Any]:
    """Run the install, one line per step that shows it running and then its outcome."""
    style = screen.style
    screen.title("安装")
    current = Block(screen.console)
    running: list[str] = []

    width = max(display_width(title) for title in STEP_TITLES.values())

    def progress(name: str, result: dict[str, Any] | None) -> None:
        title = STEP_TITLES.get(name, name)
        if result is None:
            running[:] = [name]
            current.line(f" {style.mark('doing')} {title}  {style.accent('正在执行…')}")
            return
        title += " " * (width - display_width(title))
        current.erase()
        running.clear()
        state = result.get("state")
        if state == "todo":
            current.console.write(f" {style.mark('ok')} {title}  {style.dim('已完成')}\n")
        elif state == "ok":
            current.console.write(f" {style.mark('ok')} {style.dim(title + '  已满足')}\n")
        elif state == "manual":
            current.console.write(f" {style.mark('warn')} {title}  {style.warn('要你手工完成')}\n")
            lines = [str(result.get("note", "")), f"文件：{result.get('file')}"]
            for line in lines + str(result["manual"]).splitlines():
                current.console.write(f"     {line}\n")
        else:
            note = f"  {result['note']}" if result.get("note") else ""
            current.console.write(f" {style.mark('skip')} {style.dim(title + '  不做' + note)}\n")

    screen.installing = True
    try:
        return setup_steps.install(ctx, open_browser=open_browser, progress=progress)
    except TswError as exc:
        if running:
            current.erase()
            title = STEP_TITLES.get(running[0], running[0])
            screen.say(f" {style.mark('bad')} {title}  {style.bad(exc.message)}")
        raise


def _check_title(check: dict[str, Any]) -> str:
    name = str(check["check"])
    return CHECK_TITLES.get(name, name)


def _show_result(screen: _Screen, result: dict[str, Any]) -> None:
    style = screen.style
    screen.title("核对")
    marks: dict[bool | None, str] = {True: "ok", False: "bad", None: "skip"}
    checks: list[dict[str, Any]] = result["verify"]["checks"]
    width = max(display_width(_check_title(c)) for c in checks)
    for check in checks:
        title = _check_title(check)
        title += " " * (width - display_width(title))
        extra = "（重启后生效）" if check.get("pending_reload") else ""
        word = {True: "通过", False: "失败", None: "未核对"}[check["ok"]]
        text = f"{title}  {word}{extra}"
        painted: Callable[[str], str] = style.bad if check["ok"] is False else str
        screen.say(f" {style.mark(marks[check['ok']])} {painted(text)}")
        if check["ok"] is False and check.get("detail") is not None:
            detail = check["detail"]
            shown = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
            screen.say(f"     {style.dim(shown)}")
    warnings: list[str] = result.get("warnings") or []
    for warning in warnings:
        screen.say(f" {style.mark('warn')} {style.warn('注意：' + warning)}")
    if not result["verify"]["ok"]:
        screen.say(f" {style.mark('bad')} {style.bad('有核对没有通过：修正后重新运行安装脚本')}")
    screen.title("下一步")
    command = str(result.get("start_command", ""))
    for step in re.split(r"；(?=\d\. )", str(result["next"])):
        text = step.replace(command, style.bold(style.accent(command))) if command else step
        screen.say(f"   {text}")
    screen.say(f"   {style.dim(str(result['repositories']))}")
    session = result.get("install_session")
    if session:
        screen.say(f"   {style.dim(str(session['note']))}")
