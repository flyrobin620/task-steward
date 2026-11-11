# JSON payloads from the host are walked generically; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Claude Code hooks (DN001 §7.3): ``tsw guard <Event>`` reads the hook payload on stdin.

Hooks are the enforced boundary; skills and CLAUDE.md only describe the workflow. A hook never
crashes the session: an internal failure allows the action and reports the error on stderr.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tsw import lease
from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig, lease_minutes, load_config, resolve_repository
from tsw.events import EventDraft, utc_now
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host import shell
from tsw.host.identity import HOST_CLI_COMMANDS, Identity, resolve, within
from tsw.host.plugin_copy import session_hint
from tsw.ledger import Ledger
from tsw.machine import routine as routine_mod
from tsw.machine.ask_user import handoff_due, is_automated, session_message
from tsw.machine.guards import merge_authorized
from tsw.machine.model import row_dict
from tsw.storage.fs import atomic_write

WRITE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
START_SESSION = "mcp__ccd_session__start_session"
# The host's session-scheduling tools, and the tag every scheduled prompt of a session carries.
SCHEDULERS = ("CronCreate", "ScheduleWakeup")
SCHEDULED_TAG = "<scheduled-task"
# Under the data directory's cache/: whether each orchestrator session's current turn is the user's.
TURNS_DIR = "turns"
TSW_OWNED = ("ledger", "runs", "todo", "config", "outbox", "legacy", "views", "cache", "locks")
_HASH = re.compile(
    r"\b(sha\d*sum|md5sum|shasum|certutil\s+-hashfile|get-filehash)\b", re.IGNORECASE
)
_EXECUTABLE = re.compile(r"\.(exe|dll|msi|sys|so|dylib|bin|com|appx|msix)\b", re.IGNORECASE)
_WRITE_WORDS = re.compile(
    r"(>|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|set-content|out-file|remove-item|\bdel\b|\btouch\b)",
    re.IGNORECASE,
)
_IMPORTS_TSW = re.compile(r"\b(import\s+tsw|from\s+tsw[\s.])")
_PYTHONS = {"python", "python3", "py", "pythonw"}
_PYTHON_VALUE_OPTIONS = {"-X", "-W", "--check-hash-based-pycs"}
_TSW_MODULES = ("tsw", "tsw.cli", "tsw.__main__")
_TSW_GLOBAL_OPTIONS = {"--home"}  # options of ``tsw`` that take a value
# gh options that take a value and may come before the command words (``gh -R o/r pr merge``).
_GH_VALUE_OPTIONS = {
    "-R",
    "--repo",
    "--hostname",
    "-X",
    "--method",
    "-H",
    "--header",
    "-f",
    "--raw-field",
    "-F",
    "--field",
    "-q",
    "--jq",
    "-t",
    "--template",
    "--input",
    "-p",
    "--preview",
    "--cache",
}
_TSW_GROUPS = {"task", "ledger", "config", "lease", "tool", "host", "review"}


def deny(event: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


# ---------------------------------------------------------------------- pure rules


SETTINGS = "claude-settings"
MEMORY_REASON = (
    "worker 不写 Claude Code 的记忆目录：它按会话目录分开，以后的会话读不到，还会成为留给后续会话的"
    "指令入口。需要以后记住的决定与约定，写进报告的“决定与约定”一节，由主会话验收时转记"
)


def is_memory_file(path: Path) -> bool:
    """A file in a Claude Code memory directory (``~/.claude/projects/<folder>/memory/``)."""
    parts = [p.lower() for p in path.parts]
    return any(
        parts[i] == ".claude" and parts[i + 1] == "projects" and "memory" in parts[i + 3 : i + 4]
        for i in range(len(parts) - 3)
    )


def is_settings_file(home: Home, path: Path) -> bool:
    """A Claude Code settings file in the data directory or an execution directory under it.

    tsw writes the permission rules there (DN006 §6.3); a session that could edit them would
    grant itself permissions.
    """
    return (
        path.parent.name.lower() == ".claude"
        and path.name.lower().startswith("settings")
        and path.name.lower().endswith(".json")
        and within(path, home.root)
    )


def owned_top(home: Home, path: Path) -> str | None:
    """The tsw-owned top-level directory of the data directory that ``path`` is in, if any
    (``claude-settings`` for the permission settings tsw generates)."""
    if is_settings_file(home, path):
        return SETTINGS
    return next((top for top in TSW_OWNED if within(path, home.root / top)), None)


def owned_reason(top: str) -> str:
    if top == SETTINGS:
        return "数据目录与执行目录中的 .claude/settings*.json 由 tsw 生成，会话不能修改权限设置"
    if top == "config":
        return "数据目录的 config/ 只由 tsw 写入：主会话用 config_change / config_revert 修改配置"
    return f"数据目录的 {top}/ 只由 tsw 写入，请使用 tsw 工具"


def tsw_owned_path(home: Home, path: Path) -> bool:
    return owned_top(home, path) is not None


def tsw_cli_command(program: shell.Program) -> str | None:
    """The ``tsw`` command a program invocation runs (``"tool run"``), or None if it is not tsw."""
    args = list(program.args)
    if program.name == "tsw":
        rest = args
    elif program.name in _PYTHONS:
        kind, value, rest = python_invocation(args)
        launcher = kind == "script" and value.replace("\\", "/").endswith("tsw_launch.py")
        if not (launcher or (kind == "module" and value in _TSW_MODULES)):
            return None
    else:
        return None
    words: list[str] = []
    skip = False
    for arg in rest:
        if skip:
            skip = False
        elif arg in _TSW_GLOBAL_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            words.append(arg)
    if not words:
        return ""
    return " ".join(words[:2]) if words[0] in _TSW_GROUPS else words[0]


def bash_problem(
    command: str,
    *,
    home: Home,
    push_allowed: bool,
    pr_allowed: bool,
    merge_allowed: bool = False,
    merge_repository: str | None = None,
    merge_host: str | None = None,
    worker: bool,
    cwd: Path | None = None,
    writable_roots: Sequence[Path] | None = None,
) -> str | None:
    """Why a shell command is refused, or None (best effort: see :mod:`tsw.host.shell`)."""
    if _HASH.search(command) and _EXECUTABLE.search(command):
        return "不对可执行文件求哈希（policies.executable_hashing），除非账本中有用户批准记录"
    facts = shell.analyze(command, cwd or home.root)
    for path in facts.writes:
        top = owned_top(home, path)
        if top is not None:
            return owned_reason(top)
        if writable_roots is not None and not any(within(path, r) for r in writable_roots):
            return f"只能写入本任务的可写路径（{path} 不在其中）"
    home_text = str(home.root).replace("\\", "/").lower()
    normalized = command.replace("\\", "/").lower()
    if home_text in normalized and _WRITE_WORDS.search(command):
        for top in TSW_OWNED:
            if f"{home_text}/{top}" in normalized:
                return owned_reason(top)
    for program in facts.programs:
        cli = tsw_cli_command(program)
        if cli == "guard":
            # Hooks are run by the host, never from a session's shell: a hook records the user's
            # own words (DN006 §8.1a), so running it by hand would let a session invent them.
            return "hook 入口只由宿主调用，会话中不能运行 tsw guard"
        if cli is not None and cli not in HOST_CLI_COMMANDS:
            return f"宿主会话中不能运行 tsw {cli}；写操作请使用 tsw MCP 工具"
        if program.name in _PYTHONS and _IMPORTS_TSW.search(python_code(program, command)):
            return "宿主会话中不能直接导入 tsw 包；请使用 tsw MCP 工具"
        if not worker:
            continue
        if program.name == "git":
            sub, _ = shell.git_subcommand(program.args)
            if sub == "push" and not push_allowed:
                return "该仓库没有推送授权（repositories.*.delivery.push）"
        elif program.name == "gh":
            words, _ = shell.split_options(program.args, _GH_VALUE_OPTIONS)
            lowered = [w.lower() for w in words]
            merging = lowered[:2] == ["pr", "merge"] or (
                lowered[:1] == ["api"] and any("merge" in w.lower() for w in program.args)
            )
            if merging:
                problem = merge_problem(
                    program.args, command, merge_allowed, merge_repository, merge_host
                )
                if problem is not None:
                    return problem
            words = lowered
            if words[:2] == ["pr", "create"] and not pr_allowed:
                return "创建 PR 需要仓库的 delivery.create_pr 授权"
    return None


# Options of ``gh pr merge`` / ``gh api`` whose values are free text, never a target repository.
_MERGE_TEXT_OPTIONS = {
    "-t",
    "--subject",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "-A",
    "--author-email",
    "--match-head-commit",
    "-f",
    "--raw-field",
    "--field",
    "-X",
    "--method",
    "-H",
    "--header",
}


def merge_problem(
    args: Sequence[str],
    command: str,
    allowed: bool,
    repository: str | None,
    host: str | None = None,
) -> str | None:
    """Why a worker's ``gh`` merge is refused (decision 0003, DN002 §3.5): no authorization in
    the task's repository, or a target that is not, or cannot be shown to be, that repository.

    The merge must name its target explicitly, so that neither the working directory nor gh's
    default remote decides it: ``gh pr merge`` with ``-R`` / ``--repo`` or a PR link on the
    repository's host, ``gh api`` with the REST merge path. Every argument that could name a
    repository (anything with ``/`` or ``:``) and ``--hostname`` must agree with the task's
    repository; anything else, including clustered short options, other hosts and
    ``GH_REPO`` / ``GH_HOST``, is refused rather than guessed."""
    if not allowed:
        return (
            "该任务的仓库没有合并授权（repositories.*.delivery.merge）："
            "PR 由主会话合并，worker 提交时用 merge_skipped 说明"
        )
    if re.search(r"\bGH_(REPO|HOST)\b", command):
        return "合并时不能用 GH_REPO / GH_HOST 改变目标仓库"
    own = (repository or "").lower()
    site = (host or "github.com").lower()
    pr_link = re.compile(rf"https?://{re.escape(site)}/{re.escape(own)}/pull/\d+/?", re.I)
    api_path = re.compile(rf"/?repos/{re.escape(own)}/pulls/\d+/merge", re.I)
    words = [a for a in args if not a.startswith("-")]
    is_api = bool(words) and words[0].lower() == "api"
    # The host is always named (``-R HOST/OWNER/NAME``, ``--hostname HOST`` or a PR link): a bare
    # owner/name and the REST API resolve their host from gh's settings and environment
    # (``GH_HOST``), which the session may have changed.
    host_named = False
    confirmed: list[str] = []
    api_paths: list[str] = []
    stray: list[str] = []
    skip_value = False
    for i, arg in enumerate(args):
        if skip_value:
            skip_value = False
            continue
        if arg in _MERGE_TEXT_OPTIONS:
            skip_value = True
            continue
        if any(
            arg.startswith(option + "=")
            for option in _MERGE_TEXT_OPTIONS
            if option.startswith("--")
        ):
            continue
        if arg in ("-R", "--repo", "--hostname") or arg.startswith(("--repo=", "--hostname=")):
            value = (
                arg.split("=", 1)[1] if "=" in arg else (args[i + 1] if i + 1 < len(args) else "")
            )
            skip_value = "=" not in arg
            if arg.startswith("--hostname"):
                if value.lower() == site:
                    host_named = True
                else:
                    stray.append(value)
            elif value.lower() == f"{site}/{own}":
                confirmed.append(value)
            else:
                stray.append(value)
            continue
        if "/" not in arg and ":" not in arg:
            continue
        if pr_link.fullmatch(arg) and not is_api:
            confirmed.append(arg)  # a link carries its host
            continue
        if is_api and api_path.fullmatch(arg):
            api_paths.append(arg)
            continue
        stray.append(arg)
    if is_api and api_paths and host_named:
        # gh api goes to github.com unless --hostname says otherwise.
        confirmed.extend(api_paths)
    if stray:
        return f"只能合并本任务仓库（{repository}）的 PR；无法确认的目标：{', '.join(stray)}"
    if is_api and not confirmed:
        return (
            "只能用 gh pr merge 或带 --hostname 的 REST 合并接口"
            f"（--hostname {site} repos/<owner>/<name>/pulls/<n>/merge）合并"
        )
    if not confirmed:
        target = f"{site}/{repository}"
        return f"合并须明确指向本任务仓库：gh pr merge <编号> -R {target}，或使用该仓库的 PR 链接"
    return None


def python_invocation(args: Sequence[str]) -> tuple[str, str, list[str]]:
    """What a Python command line runs, read as the interpreter reads its options:
    ``("code", CODE, rest)``, ``("module", NAME, rest)``, ``("script", PATH, rest)`` or
    ``("stdin", "", rest)``. Short options may be clustered (``-Bmtsw``, ``-Ic CODE``), and
    ``-c`` / ``-m`` / ``-W`` / ``-X`` take the rest of their cluster or the next argument."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            # The next argument is the script; ``-`` still means standard input.
            target = args[i + 1] if i + 1 < len(args) else "-"
            if target == "-":
                return "stdin", "", list(args[i + 2 :])
            return "script", target, list(args[i + 2 :])
        if arg == "-":
            return "stdin", "", list(args[i + 1 :])
        if arg.startswith("--"):
            i += 2 if arg in _PYTHON_VALUE_OPTIONS else 1
            continue
        if not arg.startswith("-"):
            return "script", arg, list(args[i + 1 :])
        consumed_next = False
        for j, flag_char in enumerate(arg[1:], start=1):
            if flag_char in "cmWX":
                value = arg[j + 1 :]
                if not value:
                    value = args[i + 1] if i + 1 < len(args) else ""
                    consumed_next = True
                if flag_char in "cm":
                    rest = list(args[i + 2 :] if consumed_next else args[i + 1 :])
                    return ("code" if flag_char == "c" else "module"), value, rest
                break
        i += 2 if consumed_next else 1
    return "stdin", "", []


def python_code(program: shell.Program, command: str) -> str:
    """The code Python runs inline: ``-c CODE``, or stdin (a heredoc) when there is no script."""
    kind, value, _ = python_invocation(list(program.args))
    if kind == "code":
        return value
    return command if kind == "stdin" else ""


# ---------------------------------------------------------------------- the hook


class Guard:
    def __init__(self, home: Home, env: Mapping[str, str], payload: Mapping[str, Any]) -> None:
        self.home = home
        self.env = env
        self.payload = payload
        self.cwd = Path(str(payload.get("cwd") or "."))
        self.machine = lease.this_machine()
        self.config: EffectiveConfig = load_config(home)
        self.ledger = Ledger(home, machine=self.machine)

    def identity(self) -> Identity:
        with self.ledger.view() as conn:
            return resolve(conn, self.home, self.env, self.cwd)

    def run(self, event: str) -> dict[str, Any] | None:
        who = self.identity()
        if who.role == "none":
            if who.pending and who.pending_role == "routine":
                if event == "SessionStart":
                    return context(
                        "SessionStart",
                        "这个会话正在作为 TaskSteward 的例行会话创建，主会话随后会完成绑定。"
                        "先调用 whoami：role 为 routine 时调用 routine_board 开始；仍未绑定"
                        "（pending）时结束这一轮，主会话完成绑定后会发消息通知你。",
                    )
                if event == "PreToolUse":
                    tool = str(self.payload.get("tool_name") or "")
                    if tool in WRITE_TOOLS or tool in ("Bash", "PowerShell", START_SESSION):
                        return deny(
                            "PreToolUse", "CC-B10 例行会话还没有绑定：等主会话完成绑定后再操作"
                        )
                return None
            if who.pending and event == "SessionStart":
                return context(
                    "SessionStart",
                    f"这个会话正在为 TaskSteward 任务 {who.task_id}（attempt {who.attempt}）创建，"
                    "主会话随后会完成绑定。先调用 whoami：role 为 worker 且 phase 为 live 时"
                    "按任务书开始工作；phase 为 standby 时是预启动的待命会话，可以先阅读任务书，"
                    "但不开始工作，结束这一轮，等主会话派发后发来的消息；仍未绑定（pending）时"
                    "结束这一轮，主会话完成绑定后会发消息通知你。",
                )
            if who.pending and event == "PreToolUse":
                tool = str(self.payload.get("tool_name") or "")
                if tool in WRITE_TOOLS or tool in ("Bash", "PowerShell", START_SESSION):
                    return deny(
                        "PreToolUse",
                        "本会话还没有绑定到 attempt：等主会话完成绑定、whoami 返回 worker 后再操作",
                    )
            return None
        if who.role == "revoked" and event not in ("SessionStart", "PreToolUse"):
            return None
        handler = getattr(self, f"on_{event}", None)
        return None if handler is None else handler(who)

    # ------------------------------------------------------------------ events

    def on_SessionStart(self, who: Identity) -> dict[str, Any]:
        if who.role == "revoked":
            return context(
                "SessionStart",
                f"本会话绑定的 TaskSteward {_bound_name(who)} 已{who.phase}，"
                "只能阅读，不能再写入或调用写工具。",
            )
        if who.role == "routine":
            return context(
                "SessionStart",
                f"你是 TaskSteward 的例行会话（{who.attempt}，数据目录 {self.home.root}）。"
                "每次运行（定时提示或主会话的消息）：调用 routine_board；按 maintenance 设定定时"
                "任务（arm：有 replace 时先 CronDelete，再 CronCreate，recurring: true，cron 与 "
                "prompt 原样，然后 routine_armed 登记），逐行执行 jobs（有 fetch 的先按 tool 与 "
                "arguments 取数，再调用 routine_run：参数为该行的 run（没有 run 时为 job_id）加上 "
                "fetched（取到的这一页，原样；工具报错或返回错误时 fetched 为它返回的原文，照样 "
                "routine_run）；没有 fetch 的直接 routine_run）。每次 routine_run "
                "之后：forward.send 为 true 时用 mcp__ccd_session_mgmt__send_message 把 message "
                "原样发给 session_id；返回 next 时按 next 的 tool 与 arguments 取下一页，再用 "
                "next.run 加上这一页调用 routine_run，直到不再返回 next（不管多少页，一页一次，"
                "不要合并、不要自行截断）。有 clear_context 时最后调用 "
                'mcp__ccd_session_mgmt__clear_session(session_id="self")。'
                "不作任何判断，不改文件、不运行命令、不建会话；遇到问题发消息告诉主会话"
                "（routine_board 的 orchestrator_session：当前持有调度租约的主会话）。",
            )
        if who.role == "worker":
            roots = "、".join(str(p) for p in who.writable_roots)
            if who.phase == "standby":
                # A prestarted session already has a brief to read, but no slot yet (DN002 §5.6).
                brief = (
                    f"{who.brief_path or '等待投递'}（待命中：可以先阅读，等主会话派发并发消息后"
                    "再开始工作；派发时任务书按最新 revision 重新生成）"
                )
            else:
                brief = who.brief_path or "等待投递"
            return context(
                "SessionStart",
                f"你是 TaskSteward 任务 {who.task_id} 的 worker（attempt {who.attempt}）。"
                f"任务书：{brief}。只在可写路径内修改：{roots}。"
                "主会话告诉你任务书已更新时，重新读取任务书（whoami 的 brief_revision），"
                "按新 revision 继续。"
                "完成后用 review_run 做自评审并逐条处置；任务书写明由 worker 合并时，"
                "门禁、评审与意见都解决后自行合并 PR（合并命令单独运行，不与其他命令连接），"
                "再 submit。需要用户决定的事用 ask_user "
                "在本会话直接问用户（Claude app 中再用 wrap_up 的 ask 写问题、actions 给每个选项"
                "一个按钮并设 blocked: true；用户点按钮等于在本会话发出按钮文字），"
                "用户回复后用 user_answer 登记；用户在本会话中修改或追加任务要求时（任务已提交"
                "或已完成、但用户尚未验收也可以），用 requirement_change 引用 hook 给出的消息编号"
                "登记需求变更，再继续、评审并重新提交；本任务不做、之后可能要做的事（未完成或延后"
                "的部分、范围外发现、用户要求记成 follow-up 的事）用 follow_up_record 记录，报告中"
                "引用其编号；跨任务的事（资源冲突、依赖、"
                "其他任务的范围）才用 question 交主会话。"
                "唤醒主会话：tsw 工具结果带 wake 且 send 为 true 时，用 "
                "mcp__ccd_session_mgmt__send_message 把 wake.message 原样发给 wake.session_id"
                '（当前持有调度租约的主会话，可能不是启动你的会话；不要发给 "parent"）；'
                "send 为 false 时不发送（回传已在账本中）。没有 wake 时（例如 [report]）发给 "
                "whoami 的 orchestrator_session，为 null 时不发送。",
            )
        lines = [f"你是 TaskSteward 主会话（数据目录 {self.home.root}）。"]
        with self.ledger.view() as conn:
            stood = lease.stood_down(conn, who.session_id)
            holder = lease.orchestrator_session(conn)
        if stood is not None:
            # A session that gave the lease up does not take it back by itself (DN001 §6.4).
            via = "被接管" if stood.get("via") == "takeover" else "让出"
            now_held = f"现在由 {holder} 持有" if holder else "现在没有主会话持有"
            lines.append(
                f"本会话已于 {stood.get('stood_down_at')} {via}调度租约（{now_held}），只读运行："
                "不派发、不验收、不处理回传，也不自动领回。只有用户在本会话中要求本会话重新担任"
                "主会话时才调用 lease_acquire。"
            )
            stale = session_hint(self.env)
            if stale:
                lines.append(stale)
            return context("SessionStart", "".join(lines))
        try:
            # Taken even with an invalid configuration: config_change needs the lease to repair
            # it (DN003 §7.1).
            self.ledger.commit(
                lease.acquire(
                    str(who.session_id),
                    machine=self.machine,
                    lease_minutes=lease_minutes(self.config),
                    actor={"role": "orchestrator", "session_id": who.session_id},
                )
            )
            lines.append("已持有调度租约。")
        except Exception as exc:  # another orchestrator holds it: read-only
            lines.append(f"未取得调度租约，本会话只读运行（{exc}）。")
        if not self.config.valid:
            lines.append(
                "配置无效，除 config_change / config_revert 外的写工具都会被拒绝："
                "用 config_show 查看问题，再用 config_change 修复。"
            )
        else:
            with self.ledger.view() as conn:
                items = inbox_mod.attention(
                    conn, self.ledger.storage, self.config, routine_mod.load_cache(self.home)
                )
            summary = "，".join(
                f"{k} {len(v) if k != 'inbox' else v[0]['count']}" for k, v in items.items()
            )
            lines.append(f"attention：{summary}。按 orchestrate skill 处理。")
        stale = session_hint(self.env)
        if stale:
            lines.append(stale)
        return context("SessionStart", "".join(lines))

    def on_PreToolUse(self, who: Identity) -> dict[str, Any] | None:
        tool = str(self.payload.get("tool_name") or "")
        tool_input: Mapping[str, Any] = self.payload.get("tool_input") or {}
        if who.role == "revoked":
            if tool in WRITE_TOOLS or tool in ("Bash", "PowerShell", START_SESSION):
                return deny(
                    "PreToolUse",
                    f"本会话绑定的 {_bound_name(who)} 已{who.phase}，不能再写入；请回到主会话",
                )
            return None
        if who.role == "routine" and (
            tool in WRITE_TOOLS or tool in ("Bash", "PowerShell", START_SESSION)
        ):
            # CC-B11 (CC008 §3): the routine session runs the routine board and nothing else.
            return deny(
                "PreToolUse",
                "CC-B11 例行会话只执行例行表（routine_board）中的例行项：不编辑文件、不运行命令、"
                "不建会话；其余事项发消息交主会话。确属需要时请用户自己执行",
            )
        if who.role == "worker" and not who.cwd_consistent and tool in (*WRITE_TOOLS, "Bash"):
            command = str(tool_input.get("command") or "") if tool == "Bash" else ""
            if not self._returns_to_root(command, who):
                return deny(
                    "PreToolUse",
                    "会话目录与 attempt 的执行目录不一致，先 cd 回执行目录："
                    f"{'、'.join(str(p) for p in who.writable_roots)}",
                )
        if tool in WRITE_TOOLS:
            target = Path(str(tool_input.get(WRITE_TOOLS[tool]) or ""))
            if not target.is_absolute():
                target = self.cwd / target
            top = owned_top(self.home, target)
            if top is not None:
                return deny("PreToolUse", owned_reason(top))
            if who.role == "worker" and not any(
                within(target, root) for root in who.writable_roots
            ):
                if is_memory_file(target):
                    return deny("PreToolUse", MEMORY_REASON)
                return deny(
                    "PreToolUse",
                    f"只能写入本任务的可写路径：{'、'.join(str(p) for p in who.writable_roots)}",
                )
            return None
        if tool == "Bash":
            push_allowed = pr_allowed = merge_allowed = False
            merge_repository: str | None = None
            merge_host: str | None = None
            if who.role == "worker" and who.task_id:
                push_allowed, pr_allowed, merge_allowed, merge_repository, merge_host = (
                    self._delivery(who.task_id)
                )
            problem = bash_problem(
                str(tool_input.get("command") or ""),
                home=self.home,
                push_allowed=push_allowed,
                pr_allowed=pr_allowed,
                merge_allowed=merge_allowed,
                merge_repository=merge_repository,
                merge_host=merge_host,
                worker=who.role == "worker",
                cwd=self.cwd,
                writable_roots=who.writable_roots if who.role == "worker" else None,
            )
            return None if problem is None else deny("PreToolUse", problem)
        if tool in SCHEDULERS and who.role in ("worker", "orchestrator", "routine"):
            # A scheduled prompt reaches the session like a user's message. Untagged, a worker's
            # would be recorded as the user's answer (found by the live host-e2e scenario,
            # DN006 §8.1a), and the orchestrator's would count as the user's turn, where the Stop
            # hook asks for prestarts that cannot be made (CC008 CC-S6).
            prompt = str(tool_input.get("prompt") or "")
            if not prompt.lstrip().startswith(SCHEDULED_TAG):
                return deny(
                    "PreToolUse",
                    f"定时提示必须以 {SCHEDULED_TAG}> 开头（以 </scheduled-task> 结束），"
                    "这样它不会被当成用户本人的消息",
                )
            return None
        if tool == START_SESSION and who.role == "orchestrator":
            name = str(tool_input.get("name") or "")
            with self.ledger.view() as conn:
                row = conn.execute(
                    "SELECT 1 FROM attempts WHERE phase = 'starting' AND generation_valid = 1 "
                    "AND session_name = ?",
                    (name,),
                ).fetchone()
                newest = routine_mod.latest(conn)
            routine = (
                newest is not None
                and newest["status"] == "starting"
                and newest["session_name"] == name
            )
            if row is None and not routine:
                return deny(
                    "PreToolUse",
                    "先用 attempt_start（例行会话用 routine_start）落账再创建会话；"
                    "会话名必须是当前有效代次的名称",
                )
        return None

    def on_UserPromptSubmit(self, who: Identity) -> dict[str, Any] | None:
        prompt = str(self.payload.get("prompt") or "")
        if who.role == "orchestrator":
            self._mark_turn(who, user=not is_automated(prompt))
            return None
        if who.role != "worker":
            return None
        notes: list[str] = []
        recorded = self._record_answer(who, prompt) if self.config.valid else None
        if recorded is not None and recorded.get("request_id"):
            notes.append(
                f"这条用户消息已记录为 {recorded['message_id']}（问题 {recorded['request_id']}）。"
                "它回答了问题时，用 user_answer 引用这个编号登记答复；它修改了任务的目标、范围或"
                "验收时，登记答复后再用 requirement_change 引用这个编号登记需求变更。"
            )
        elif recorded is not None:
            notes.append(
                f"这条用户消息已记录为 {recorded['message_id']}。它修改了任务的目标、范围或验收"
                "（包括任务已提交或已完成后用户追加的要求）时，用 requirement_change 引用这个"
                "编号登记需求变更，再继续工作、评审并重新提交；要求把某些事记成 follow-up（以后再"
                "做）时，用 follow_up_record 引用这个编号记录；只是讨论或任务内的澄清时不登记。"
            )
        if "[report]" in prompt:
            notes.append(
                "用户要求回传（[report]）：先用 progress_note 记录用户决定、已做动作与影响范围，"
                "再用 mcp__ccd_session_mgmt__send_message 通知主会话："
                f"{self._wake_hint()}"
            )
        return context("UserPromptSubmit", "".join(notes)) if notes else None

    def _wake_hint(self) -> str:
        """Where a worker's message to the orchestrator goes: the lease holder (DN001 §6.3)."""
        if self.config.valid and not self.config.get("scheduling.wake.worker_message_parent"):
            return "配置关闭了 worker 的唤醒消息，不发送（记录已在账本中）。"
        try:
            with self.ledger.view() as conn:
                target = lease.orchestrator_session(conn)
        except Exception:  # a hint never blocks the user's message
            target = None
        if target is None:
            return (
                "现在没有主会话持有调度租约（例如正在交接），不发送。progress_note 不进入主会话"
                "的 inbox：在回复中告诉用户这次回传还没有送达主会话，等新主会话领取租约后"
                "（whoami 的 orchestrator_session 不为 null）再发给它。"
            )
        return (
            f"session_id 为 {target}（当前持有调度租约的主会话，whoami 的 orchestrator_session；"
            '不要发给 "parent"）。'
        )

    def _record_answer(self, who: Identity, prompt: str) -> dict[str, Any] | None:
        """Record the user's words in the worker's session (DN006 §8.1a, DN002 §5.1a): an
        answer to its open question or the source of a requirement change."""
        if who.phase != "live" or not who.task_id or not who.session_id:
            return None
        task_id, session_id = who.task_id, who.session_id
        out: dict[str, Any] = {}

        def build(ctx: CommitContext) -> list[EventDraft]:
            draft = session_message(ctx.conn, task_id, session_id, prompt)
            if draft is not None:
                out.update(draft.data)
            return [] if draft is None else [draft]

        try:
            self.ledger.commit(
                Command(
                    actor={"role": "user", "session_id": session_id, "via": "UserPromptSubmit"},
                    input={"op": "session_message", "prompt": prompt},
                    build=build,
                )
            )
        except Exception as exc:  # recording must never block the user's message
            print(f"tsw guard: session message not recorded: {exc}", file=sys.stderr)
            return None
        return out or None

    def on_Stop(self, who: Identity) -> dict[str, Any] | None:
        again = bool(self.payload.get("stop_hook_active"))
        if who.role == "orchestrator":
            user_turn = self._take_turn(who)
            if again or not self.config.valid:
                return None
            reasons: list[str] = []
            with self.ledger.view() as conn:
                pending = inbox_mod.inbox(conn, self.ledger.storage, self.config)
                due = handoff_due(conn, utc_now())
                # Only the user's own turn can create sessions (G-WINDOW): elsewhere a reminder
                # could not be acted on. A session reading while another holds the lease neither.
                may_dispatch = self._may_dispatch(conn, who)
                prestart = (
                    inbox_mod.needs_prestart(conn, self.config)
                    if user_turn and may_dispatch
                    else []
                )
                routine = (
                    inbox_mod.routine_attention(
                        conn, self.config, routine_mod.load_cache(self.home)
                    )
                    if may_dispatch
                    else []
                )
            state = routine[0]["state"] if routine else None
            if pending and may_dispatch:
                reasons.append(
                    f"还有 {len(pending)} 条未处理的回传，先调用 inbox 处理并 inbox_ack。"
                )
            if due and may_dispatch:
                reasons.append(
                    f"有 {len(due)} 个 worker 会话中的问题已超时无人回复："
                    "运行 sync 转交，再按 inbox 处理。"
                )
            if state == "stale":
                reasons.append(
                    "例行会话已超过几个间隔没有运行（attention 的 routine 为 stale）：用 "
                    f"mcp__ccd_session_mgmt__send_message 给它（{routine[0].get('session_id')}）"
                    "发消息“运行 routine_board”，让它重新设定定时任务。"
                )
            if state == "attached":
                # Not limited to the user's turn: detaching creates no session (CC006 §6.1).
                reasons.append(
                    f"例行会话 routine-g{routine[0].get('generation')}"
                    f"（{routine[0].get('session_id')}）还挂在本会话名下（attention 的 routine 为 "
                    "attached），它每结束一轮都会唤醒你：调用 "
                    "mcp__ccd_session_mgmt__detach_session(session_id=它的会话 ID)，成功后 "
                    f"routine_detached（generation: {routine[0].get('generation')}）。"
                    "它已经分离或不是本会话启动的（get_session 的 detached 为 true，或 "
                    "parentSessionId 不是本会话）时，同样调用 routine_detached，在 note 中写明。"
                )
            if state == "missing" and user_turn:
                reasons.append(
                    "这是用户的 turn：例行会话还没有启动（attention 的 routine 为 missing）。"
                    "按 orchestrate skill 启动：routine_start（user_turn: true）→ start_session → "
                    "routine_bind。用户关闭了例行会话时说明原因后直接结束。"
                )
            if prestart:
                names = "、".join(str(item["task_id"]) for item in prestart)
                reasons.append(
                    f"这是用户的 turn，结束后就不能再创建会话：{names} 还没有会话（attention 的 "
                    "needs_prestart）。按 orchestrate skill 为它们预启动：attempt_start"
                    "（purpose: prestart，user_turn: true）→ start_session → attempt_reconcile。"
                    "确实不该现在预启动时（例如用户要求先不开始），说明原因后直接结束。"
                )
            return {"decision": "block", "reason": "".join(reasons)} if reasons else None
        if who.role == "routine":
            # Nothing to check; the end of the turn is kept as live evidence (T250).
            routine_mod.record_turn_end(self.home, utc_now())
            return None
        if who.role != "worker":
            return None
        with self.ledger.view() as conn:
            row = row_dict(
                conn.execute(
                    "SELECT status, substate FROM tasks WHERE task_id = ?", (who.task_id,)
                ).fetchone()
            )
        if self.config.valid and who.phase == "live":
            try:
                self.ledger.commit(_turn_ended(who))
            except Exception as exc:  # observation only; never block on it
                print(f"tsw guard: turn_ended not recorded: {exc}", file=sys.stderr)
        if row is not None and row["substate"] == "reviewing" and not again:
            return {
                "decision": "block",
                "reason": "评审进行中：再次调用 review_run 完成这一轮"
                "（或用 review_complete / review_fail 记录结果）后再结束。",
            }
        return None

    def _turn_file(self, who: Identity) -> Path:
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(who.session_id or "none"))
        return self.home.cache_dir / TURNS_DIR / f"{name}.json"

    def _mark_turn(self, who: Identity, *, user: bool) -> None:
        """Remember whether the orchestrator's current turn is the user's own (``UserPromptSubmit``
        sees every turn's prompt; the ``Stop`` hook does not). A cache file, not a ledger event:
        it only steers the reminder at the end of this turn."""
        path = self._turn_file(who)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, json.dumps({"user_turn": user, "at": utc_now()}).encode("utf-8"))
        except OSError as exc:  # a missed mark only skips a reminder
            print(f"tsw guard: turn not recorded: {exc}", file=sys.stderr)

    def _take_turn(self, who: Identity) -> bool:
        """Whether the turn that is ending was the user's; the mark is used up either way, so a
        later turn the hook did not see (no ``UserPromptSubmit``) never counts as the user's."""
        path = self._turn_file(who)
        try:
            user = bool(json.loads(path.read_text(encoding="utf-8")).get("user_turn"))
        except (OSError, ValueError, AttributeError):
            return False
        with contextlib.suppress(OSError):
            path.unlink()
        return user

    def _may_dispatch(self, conn: sqlite3.Connection, who: Identity) -> bool:
        """No other session holds a valid scheduling lease (G-LEASE would refuse a prestart),
        and this session has not given the lease up (it does not take it back by itself)."""
        if lease.stood_down(conn, who.session_id) is not None:
            return False
        held = lease.lease_row(conn)
        if held is None or lease.is_expired(held, utc_now()):
            return True
        return held["holder_session"] == who.session_id and held["machine"] == self.machine

    def _returns_to_root(self, command: str, who: Identity) -> bool:
        """A bare ``cd`` into a writable root is how a drifted worker gets back."""
        segments = shell.split(command)
        if len(segments) != 1 or segments[0].redirects:
            return False
        words = segments[0].words
        if len(words) != 2 or shell.program_name(words[0]) not in shell.CHDIR:
            return False
        target = shell.resolve(words[1], self.cwd)
        return target is not None and any(within(target, r) for r in who.writable_roots)

    def _delivery(self, task_id: str) -> tuple[bool, bool, bool, str | None, str | None]:
        """Push, PR and merge authorization of the task; its repository's ``owner/name`` and
        host."""
        with self.ledger.view() as conn:
            row = row_dict(
                conn.execute(
                    "SELECT repo, kind FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )
        if row is None:
            return False, False, False, None, None
        repo = resolve_repository(self.config, str(row["repo"]))
        delivery = self.config.get(f"repositories.{repo}.delivery", {}) if repo else {}
        slug = self.config.get(f"repositories.{repo}.repository") if repo else None
        host = self.config.get(f"repositories.{repo}.host") if repo else None
        return (
            bool(delivery.get("push")),
            bool(delivery.get("create_pr")),
            merge_authorized(self.config, row),
            slug if isinstance(slug, str) else None,
            host if isinstance(host, str) else None,
        )


def _bound_name(who: Identity) -> str:
    """How a revoked session's binding is named: an attempt or a routine generation."""
    return f"attempt {who.task_id}/{who.attempt}" if who.task_id else f"例行会话 {who.attempt}"


def _turn_ended(who: Identity) -> Command:
    def build(ctx: CommitContext) -> list[EventDraft]:
        return [
            EventDraft(
                type="worker.turn_ended",
                task_id=who.task_id,
                attempt=who.attempt,
                data={"attempt": who.attempt},
            )
        ]

    return Command(
        actor={"role": "worker", "session_id": who.session_id},
        input={"op": "turn_ended"},
        build=build,
    )


def run(event: str, home: Home, env: Mapping[str, str], stdin: str) -> str:
    """Run one hook; returns what to print on stdout (possibly empty)."""
    try:
        payload = json.loads(stdin) if stdin.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    try:
        output = Guard(home, env, payload).run(event)
    except Exception as exc:  # a broken hook must not break the session
        print(f"tsw guard {event}: {exc}", file=sys.stderr)
        return ""
    return "" if output is None else json.dumps(output, ensure_ascii=False)
