# JSON payloads from the host are walked generically; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Claude Code hooks (DN001 §7.3): ``tsw guard <Event>`` reads the hook payload on stdin.

Hooks are the enforced boundary; skills and CLAUDE.md only describe the workflow. A hook never
crashes the session: an internal failure allows the action and reports the error on stderr.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from tsw import lease
from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig, load_config, resolve_repository
from tsw.events import EventDraft
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host import shell
from tsw.host.identity import HOST_CLI_COMMANDS, Identity, resolve, within
from tsw.ledger import Ledger
from tsw.machine.model import row_dict

WRITE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
START_SESSION = "mcp__ccd_session__start_session"
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


def owned_top(home: Home, path: Path) -> str | None:
    """The tsw-owned top-level directory of the data directory that ``path`` is in, if any."""
    return next((top for top in TSW_OWNED if within(path, home.root / top)), None)


def tsw_owned_path(home: Home, path: Path) -> bool:
    return owned_top(home, path) is not None


def tsw_cli_command(program: shell.Program) -> str | None:
    """The ``tsw`` command a program invocation runs (``"tool run"``), or None if it is not tsw."""
    args = list(program.args)
    if program.name == "tsw":
        rest = args
    elif program.name in _PYTHONS:
        rest = None
        for i, arg in enumerate(args):
            if arg.replace("\\", "/").endswith("tsw_launch.py"):
                rest = args[i + 1 :]
                break
            if arg == "-m" and i + 1 < len(args) and args[i + 1] in ("tsw", "tsw.cli"):
                rest = args[i + 2 :]
                break
        if rest is None:
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
            return f"数据目录的 {top}/ 只由 tsw 写入，请使用 tsw 工具"
        if writable_roots is not None and not any(within(path, r) for r in writable_roots):
            return f"只能写入本任务的可写路径（{path} 不在其中）"
    home_text = str(home.root).replace("\\", "/").lower()
    normalized = command.replace("\\", "/").lower()
    if home_text in normalized and _WRITE_WORDS.search(command):
        for top in TSW_OWNED:
            if f"{home_text}/{top}" in normalized:
                return f"数据目录的 {top}/ 只由 tsw 写入，请使用 tsw 工具"
    for program in facts.programs:
        cli = tsw_cli_command(program)
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
            words = [w.lower() for w in words]
            if words[:2] == ["pr", "merge"] or (
                words[:1] == ["api"] and any("merge" in w.lower() for w in program.args)
            ):
                return "worker 不能合并 PR"
            if words[:2] == ["pr", "create"] and not pr_allowed:
                return "创建 PR 需要仓库的 delivery.create_pr 授权"
    return None


def python_code(program: shell.Program, command: str) -> str:
    """The code Python runs inline: ``-c CODE``, or stdin (a heredoc) when there is no script.

    Options are read as the interpreter does: they end at ``-c``, ``-m``, ``-`` or the script.
    """
    args = list(program.args)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-c":
            return args[i + 1] if i + 1 < len(args) else ""
        if arg.startswith("-c") and not arg.startswith("--"):
            return arg[2:]
        if arg == "-m" or (arg.startswith("-m") and not arg.startswith("--")):
            return ""  # a module; ``-m tsw`` is caught as a tsw command
        if arg == "-":
            return command
        if arg in _PYTHON_VALUE_OPTIONS:
            i += 2
            continue
        if not arg.startswith("-"):
            return ""  # a script file
        i += 1
    return command


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
                f"本会话绑定的 TaskSteward attempt {who.task_id}/{who.attempt} 已{who.phase}，"
                "只能阅读，不能再写入或调用写工具。",
            )
        if who.role == "worker":
            roots = "、".join(str(p) for p in who.writable_roots)
            return context(
                "SessionStart",
                f"你是 TaskSteward 任务 {who.task_id} 的 worker（attempt {who.attempt}）。"
                f"任务书：{who.brief_path or '等待投递'}。只在可写路径内修改：{roots}。"
                "完成后用 review_run 做自评审并逐条处置，再 submit；需要主会话判断时用 question，"
                '并用 mcp__ccd_session_mgmt__send_message(session_id="parent") 唤醒主会话。',
            )
        lines = [f"你是 TaskSteward 主会话（数据目录 {self.home.root}）。"]
        if not self.config.valid:
            lines.append("配置无效，所有写工具都会被拒绝：先运行 tsw config validate。")
        else:
            try:
                self.ledger.commit(
                    lease.acquire(
                        str(who.session_id),
                        machine=self.machine,
                        lease_minutes=int(self.config.get("scheduling.lease_minutes")),
                        actor={"role": "orchestrator", "session_id": who.session_id},
                    )
                )
                lines.append("已持有调度租约。")
            except Exception as exc:  # another orchestrator holds it: read-only
                lines.append(f"未取得调度租约，本会话只读运行（{exc}）。")
            with self.ledger.view() as conn:
                items = inbox_mod.attention(conn, self.ledger.storage, self.config)
            summary = "，".join(
                f"{k} {len(v) if k != 'inbox' else v[0]['count']}" for k, v in items.items()
            )
            lines.append(f"attention：{summary}。按 orchestrate skill 处理。")
        return context("SessionStart", "".join(lines))

    def on_PreToolUse(self, who: Identity) -> dict[str, Any] | None:
        tool = str(self.payload.get("tool_name") or "")
        tool_input: Mapping[str, Any] = self.payload.get("tool_input") or {}
        if who.role == "revoked":
            if tool in WRITE_TOOLS or tool in ("Bash", "PowerShell", START_SESSION):
                return deny(
                    "PreToolUse",
                    f"本会话绑定的 attempt {who.task_id}/{who.attempt} 已{who.phase}，不能再写入；"
                    "请回到主会话",
                )
            return None
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
            if tsw_owned_path(self.home, target):
                return deny(
                    "PreToolUse",
                    "数据目录中的账本、任务书、报告与视图只由 tsw 写入，请使用 tsw 工具",
                )
            if who.role == "worker" and not any(
                within(target, root) for root in who.writable_roots
            ):
                return deny(
                    "PreToolUse",
                    f"只能写入本任务的可写路径：{'、'.join(str(p) for p in who.writable_roots)}",
                )
            return None
        if tool == "Bash":
            push_allowed = pr_allowed = False
            if who.role == "worker" and who.task_id:
                push_allowed, pr_allowed = self._delivery(who.task_id)
            problem = bash_problem(
                str(tool_input.get("command") or ""),
                home=self.home,
                push_allowed=push_allowed,
                pr_allowed=pr_allowed,
                worker=who.role == "worker",
                cwd=self.cwd,
                writable_roots=who.writable_roots if who.role == "worker" else None,
            )
            return None if problem is None else deny("PreToolUse", problem)
        if tool == START_SESSION and who.role == "orchestrator":
            name = str(tool_input.get("name") or "")
            with self.ledger.view() as conn:
                row = conn.execute(
                    "SELECT 1 FROM attempts WHERE phase = 'starting' AND generation_valid = 1 "
                    "AND session_name = ?",
                    (name,),
                ).fetchone()
            if row is None:
                return deny(
                    "PreToolUse",
                    "先用 attempt_start 落账再创建会话；会话名必须是当前有效代次的名称",
                )
        return None

    def on_UserPromptSubmit(self, who: Identity) -> dict[str, Any] | None:
        prompt = str(self.payload.get("prompt") or "")
        if who.role == "worker" and "[report]" in prompt:
            return context(
                "UserPromptSubmit",
                "用户要求回传（[report]）：先用 progress_note 记录用户决定、已做动作与影响范围，"
                '再用 mcp__ccd_session_mgmt__send_message(session_id="parent") 通知主会话。',
            )
        return None

    def on_Stop(self, who: Identity) -> dict[str, Any] | None:
        again = bool(self.payload.get("stop_hook_active"))
        if who.role == "orchestrator":
            if again or not self.config.valid:
                return None
            with self.ledger.view() as conn:
                pending = inbox_mod.inbox(conn, self.ledger.storage, self.config)
            if pending:
                return {
                    "decision": "block",
                    "reason": f"还有 {len(pending)} 条未处理的回传，"
                    "先调用 inbox 处理并 inbox_ack。",
                }
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

    def _delivery(self, task_id: str) -> tuple[bool, bool]:
        with self.ledger.view() as conn:
            row = row_dict(
                conn.execute("SELECT repo FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            )
        repo = resolve_repository(self.config, str(row["repo"])) if row else None
        delivery = self.config.get(f"repositories.{repo}.delivery", {}) if repo else {}
        return bool(delivery.get("push")), bool(delivery.get("create_pr"))


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
