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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tsw import lease
from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig, load_config, resolve_repository
from tsw.events import EventDraft
from tsw.home import Home
from tsw.host import inbox as inbox_mod
from tsw.host.identity import Identity, resolve, within
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
_PUSH = re.compile(r"\bgit\b[^;&|]*\bpush\b")
_PR = re.compile(r"\bgh\s+pr\s+(create|merge)\b")
_HASH = re.compile(
    r"\b(sha\d*sum|md5sum|shasum|certutil\s+-hashfile|get-filehash)\b", re.IGNORECASE
)
_EXECUTABLE = re.compile(r"\.(exe|dll|msi|sys|so|dylib|bin|com|appx|msix)\b", re.IGNORECASE)
_WRITE_WORDS = re.compile(
    r"(>|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|set-content|out-file|remove-item|\bdel\b|\btouch\b)",
    re.IGNORECASE,
)


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


def tsw_owned_path(home: Home, path: Path) -> bool:
    return any(within(path, home.root / top) for top in TSW_OWNED)


def bash_problem(
    command: str, *, home: Home, push_allowed: bool, pr_allowed: bool, worker: bool
) -> str | None:
    """Why a shell command is refused, or None."""
    if _HASH.search(command) and _EXECUTABLE.search(command):
        return "不对可执行文件求哈希（policies.executable_hashing），除非账本中有用户批准记录"
    home_text = str(home.root).replace("\\", "/").lower()
    normalized = command.replace("\\", "/").lower()
    if home_text in normalized and _WRITE_WORDS.search(command):
        for top in TSW_OWNED:
            if f"{home_text}/{top}" in normalized:
                return f"数据目录的 {top}/ 只由 tsw 写入，请使用 tsw 工具"
    if worker:
        if _PUSH.search(command) and not push_allowed:
            return "该仓库没有推送授权（repositories.*.delivery.push）"
        match = _PR.search(command)
        if match and (match.group(1) == "merge" or not pr_allowed):
            return "worker 不能合并 PR；创建 PR 需要仓库的 delivery.create_pr 授权"
    return None


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
        handler = getattr(self, f"on_{event}", None)
        return None if handler is None else handler(who)

    # ------------------------------------------------------------------ events

    def on_SessionStart(self, who: Identity) -> dict[str, Any]:
        if who.role == "worker":
            roots = "、".join(str(p) for p in who.writable_roots)
            return context(
                "SessionStart",
                f"你是 TaskSteward 任务 {who.task_id} 的 worker（attempt {who.attempt}）。"
                f"任务书：{who.brief_path or '等待投递'}。只在可写路径内修改：{roots}。"
                "完成后用 review_start 等工具做自评审，再 submit；需要主会话判断时用 question，"
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
                "reason": "评审进行中：用 review_complete 或 review_fail "
                "记录这一轮的结果后再结束。",
            }
        return None

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
