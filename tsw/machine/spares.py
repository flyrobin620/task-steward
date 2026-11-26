# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""The spare-session pool (DN002 §5.9, T363).

A spare session is made in the user's own turn, bound to no task, and waits in an empty folder of
its own (``spares/S<n>`` in the data directory). Where no session can be created (a turn begun by
a workdeck message, another session or the app) the orchestrator claims one for a task: the
session makes the task's worktree (or folder) itself and moves there with the host's
``change_directory``, which the host allows for a folder the session made (T363 probes), and then
works like a worker created there. The Dashboard claims one as a new conversation of the user's
(DN006 §8.10), and a message the user sends straight to a spare session claims it the same way.

The pool is not a task: its events belong to no task and change no task's state.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.home import Home, workdir_path
from tsw.lease import parse_time
from tsw.machine import guards
from tsw.machine.base import ToolEnv, flag, opt_text, text, tool, worker_attempt
from tsw.machine.model import row_dict
from tsw.plugins.catalog import PERMISSION_NAMES, SESSIONS

SPARE_DIR = "spares"  # relative to the data directory; one empty folder per spare session
CLAIM_TAG = "[TaskSteward 派发]"
START_TAG = "[TaskSteward 开始]"
# A claimed session's turn is the first of a task: its folder is made by the session itself.
STATUSES_IN_POOL = ("starting", "available")
CLOSE_OUTCOMES = ("archived", "missing", "failed")
MOVE_OUTCOMES = ("moved", "failed")
BACKGROUND = (
    "TaskSteward 的备用会话：在用户的一轮中预先建好，不绑定任务，在自己的空目录中等待。"
    "之后的第一条消息决定它的用途：主会话派它做一个任务（它自己建好任务的目录并移过去），"
    "或者用户经 Dashboard、workdeck 直接开始一段新对话。"
)


def spare_id(number: int) -> str:
    return f"S{number}"


def spare_title(number: int, config: EffectiveConfig) -> str:
    prefix = str(config.get("sessions.title_on_spare") or "").strip()
    return f"{prefix} {spare_id(number)}" if prefix else spare_id(number)


def spare_prompt(sid: str) -> str:
    """The first prompt of a spare session; it decides nothing until the next message."""
    return (
        f"你是 TaskSteward 的备用会话 {sid}，现在不绑定任何任务。现在只回复“备用会话就绪”，然后结束"
        "这一轮，不做任何其他事。\n"
        "之后收到的第一条消息决定你的用途：\n"
        f"- 主会话发来的、以 `{CLAIM_TAG}` 开头的消息派你做一个任务：严格按消息中的步骤做"
        "（建目录、移动、登记），然后结束那一轮；之后主会话会发开始的消息。\n"
        "- 其他消息就是用户要和你对话（经 workdeck、Dashboard 或在 app 中直接输入）：先用 "
        "mcp__ccd_session_mgmt__set_session_title 把本会话的标题改成这条消息的简短概括（至多 30 "
        f"字，session_id 为你自己的会话 ID），再照常回答。你不是 TaskSteward 的 worker，"
        "不按任务书工作。"
    )


def by_session(conn: sqlite3.Connection, session_ref: str | None) -> dict[str, Any] | None:
    if not session_ref:
        return None
    return row_dict(
        conn.execute(
            "SELECT * FROM spares WHERE session_ref = ? ORDER BY number DESC LIMIT 1",
            (session_ref,),
        ).fetchone()
    )


def get(conn: sqlite3.Connection, sid: str) -> dict[str, Any]:
    row = row_dict(conn.execute("SELECT * FROM spares WHERE spare_id = ?", (sid,)).fetchone())
    if row is None:
        raise rejected("没有这个备用会话", reason="spare_not_found", spare_id=sid)
    return row


def pool(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """The sessions still in the pool (being made or waiting), the newest first."""
    rows = conn.execute(
        "SELECT * FROM spares WHERE status IN ('starting', 'available') ORDER BY number DESC"
    )
    return [r for r in (row_dict(x) for x in rows) if r is not None]


def available(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [r for r in pool(conn) if r["status"] == "available"]


# The spare sessions the host says it runs (the MCP server reads its session registry and sets
# this around the call, DN002 §5.9). Read inside the command's build, after the ledger has
# answered a retried command_id from its record (review r2 F5); None: the host said nothing.
HOST_RUNNING: ContextVar[frozenset[str] | None] = ContextVar("tsw_spares_running", default=None)


def usable(conn: sqlite3.Connection, config: EffectiveConfig, now: str) -> list[dict[str, Any]]:
    """Available spare sessions not yet too old (``scheduling.spare.max_age_hours``) and, when
    the host said which run, running; the newest first: the ones a claim may take."""
    running = HOST_RUNNING.get()
    return [
        r
        for r in available(conn)
        if not expired(r, config, now) and (running is None or r["spare_id"] in running)
    ]


def ever(conn: sqlite3.Connection) -> bool:
    """Whether the pool ever had a session (a spare was asked for at least once)."""
    return conn.execute("SELECT 1 FROM spares LIMIT 1").fetchone() is not None


def claimed_by_page(conn: sqlite3.Connection, page_command: str) -> dict[str, Any] | None:
    """The spare session a Dashboard submission claimed (its page command_id), if any."""
    return row_dict(
        conn.execute(
            "SELECT * FROM spares WHERE status = 'claimed' AND claim_command = ? "
            "ORDER BY number DESC LIMIT 1",
            (page_command,),
        ).fetchone()
    )


def claimed_for(conn: sqlite3.Connection, task_id: str, attempt_id: str) -> dict[str, Any] | None:
    """The spare session claimed for this attempt, if it was one."""
    return row_dict(
        conn.execute(
            "SELECT * FROM spares WHERE status = 'claimed' AND claimed_as = 'task' "
            "AND claimed_task = ? AND claimed_attempt = ? ORDER BY number DESC LIMIT 1",
            (task_id, attempt_id),
        ).fetchone()
    )


def expired(row: Mapping[str, Any], config: EffectiveConfig, now: str) -> bool:
    """Older than ``scheduling.spare.max_age_hours`` (DN002 §5.9)."""
    since = row.get("created_at") or row.get("requested_at")
    if not since:
        return False
    hours = int(config.get("scheduling.spare.max_age_hours"))
    return parse_time(now) - parse_time(str(since)) > timedelta(hours=hours)


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def _session_taken(conn: sqlite3.Connection, session_ref: str) -> bool:
    if conn.execute("SELECT 1 FROM attempts WHERE session_ref = ?", (session_ref,)).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM routine_sessions WHERE session_ref = ?", (session_ref,)
    ).fetchone():
        return True
    return by_session(conn, session_ref) is not None


def remove_folder(home: Home, recorded: str) -> bool:
    """Remove a spare's own folder when it is empty; True when it is gone."""
    path = workdir_path(home, recorded)
    if not path.exists():
        return True
    with contextlib.suppress(OSError):
        path.rmdir()  # only an empty folder: anything in it is kept
    return not path.exists()


# ---------------------------------------------------------------------- tools


@tool(
    "spare_start",
    "orchestrator",
    description=(
        "补充一个备用会话（spare.start_requested，DN002 §5.9）：只在用户本人的 turn；返回 "
        "start_session 用的 cwd、name、title、prompt 与 background，原样传入"
    ),
)
def spare_start(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    user_turn = flag(inp, "user_turn")

    def build(ctx: CommitContext) -> list[EventDraft]:
        if not env.config.get("scheduling.spare.enabled"):
            raise rejected(
                "配置关闭了备用会话池（scheduling.spare.enabled）", reason="spare_disabled"
            )
        guards.g_window(env.config, user_turn)
        target = int(env.config.get("scheduling.spare.count"))
        if len(pool(ctx.conn)) >= target:
            raise rejected(
                "备用会话已达目标数量（scheduling.spare.count）",
                reason="spare_pool_full",
                count=target,
            )
        newest = ctx.conn.execute("SELECT MAX(number) AS n FROM spares").fetchone()["n"]
        number = int(newest or 0) + 1
        sid = spare_id(number)
        workdir = f"{SPARE_DIR}/{sid}"
        path = ctx.home.root / workdir
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise rejected("备用会话的目录已有内容", reason="spare_dir_exists", path=str(path))
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise rejected(
                "无法创建备用会话的目录", reason="workdir_unavailable", error=str(exc)
            ) from exc
        adapter = str(env.config.get("roles.worker.session"))
        caps = SESSIONS.get(adapter)
        mode = str(env.config.get("roles.worker.permission_mode"))
        title = spare_title(number, env.config)
        output.update(
            {
                "spare_id": sid,
                "session_name": f"spare-s{number}",
                "title": title,
                "prompt": spare_prompt(sid),
                "background": BACKGROUND,
                "model": env.config.get("roles.worker.model"),
                "effort": env.config.get("roles.worker.effort"),
                # What the host's session tool takes (start_session's permission_mode).
                "permission_mode": PERMISSION_NAMES.get(
                    caps.host if caps is not None else "", {}
                ).get(mode, mode),
            }
        )
        return [
            EventDraft(
                type="spare.start_requested",
                data={
                    "spare_id": sid,
                    "number": number,
                    "session_name": output["session_name"],
                    "workdir": workdir,
                    "title": title,
                    "prompt": output["prompt"],
                    "adapter": adapter,
                    "controller": env.caller.session_id,
                    # What start_session takes, so a retried call gives the same back (PR #95).
                    "background": BACKGROUND,
                    "model": output["model"],
                    "effort": output["effort"],
                    "permission_mode": output["permission_mode"],
                },
            )
        ]

    output: dict[str, Any] = {}
    command = _cmd(env, "spare_start", inp, build)
    command.output = output
    return command


@tool(
    "spare_reconcile",
    "orchestrator",
    description="备用会话的创建结果对账（spare.created / spare.not_created）",
)
def spare_reconcile(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    sid = text(inp, "spare_id")
    outcome = text(inp, "outcome")
    session_ref = opt_text(inp, "session_ref")
    if outcome not in ("created", "not_created"):
        raise rejected(
            "outcome 必须是 created 或 not_created", reason="invalid_input", field="outcome"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = get(ctx.conn, sid)
        if row["status"] != "starting":
            raise rejected(
                "只能对正在建立的备用会话对账", reason="invalid_spare", status=row["status"]
            )
        if outcome == "not_created":
            remove_folder(ctx.home, str(row["workdir"]))
            return [EventDraft(type="spare.not_created", data={"spare_id": sid})]
        if not session_ref:
            raise rejected("创建成功需要会话引用", reason="invalid_input", field="session_ref")
        if _session_taken(ctx.conn, session_ref):
            raise rejected("该会话已绑定为其他角色", reason="session_in_use")
        return [
            EventDraft(type="spare.created", data={"spare_id": sid, "session_ref": session_ref})
        ]

    return _cmd(env, "spare_reconcile", inp, build)


@tool(
    "spare_close",
    "orchestrator",
    description=(
        "记录退役备用会话的归档回执（spare.closed）：archived、missing（会话已不存在或已归档）或 "
        "failed（宿主拒绝归档）"
    ),
)
def spare_close(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    sid = text(inp, "spare_id")
    outcome = text(inp, "outcome")
    reason = opt_text(inp, "reason") or "retire"
    result = opt_text(inp, "result")
    if outcome not in CLOSE_OUTCOMES:
        raise rejected(
            "outcome 必须是 archived、missing 或 failed", reason="invalid_input", field="outcome"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = get(ctx.conn, sid)
        if row["status"] not in ("available", "retire_failed"):
            raise rejected(
                "只能关闭还在池中的备用会话", reason="invalid_spare", status=row["status"]
            )
        data: dict[str, Any] = {"spare_id": sid, "outcome": outcome, "reason": reason}
        if result:
            data["result"] = result
        if outcome != "failed":
            data["folder_removed"] = remove_folder(ctx.home, str(row["workdir"]))
        return [EventDraft(type="spare.closed", data=data)]

    return _cmd(env, "spare_close", inp, build)


@tool(
    "spare_claim",
    "user",
    description="认领一个备用会话作为用户的新对话（spare.claimed，Dashboard「新建会话」）",
)
def spare_claim(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    """The Dashboard claims a spare session for a conversation of the user's (DN006 §8.10).
    ``page_command`` is the page's own command_id for this submission: kept with the claim so a
    retry, even after the Dashboard restarted, finds it instead of claiming another (PR #95)."""
    sid = text(inp, "spare_id")
    page_command = opt_text(inp, "page_command")
    source = inp.get("user_source")
    if not isinstance(source, Mapping):
        raise rejected("需要来源", reason="invalid_input", field="user_source")

    def build(ctx: CommitContext) -> list[EventDraft]:
        if page_command:
            # Checked under the ledger's commit lock: two Dashboards handling the same submission
            # claim one spare between them, never two (review r4 F7).
            earlier = claimed_by_page(ctx.conn, page_command)
            if earlier is not None:
                raise rejected(
                    "这次提交已认领了备用会话",
                    reason="page_command_claimed",
                    spare_id=earlier["spare_id"],
                    session_ref=earlier["session_ref"],
                    text=earlier["claim_text"],
                )
        row = get(ctx.conn, sid)
        if row["status"] != "available":
            raise rejected("这个备用会话已不可用", reason="spare_unavailable", status=row["status"])
        return [
            EventDraft(
                type="spare.claimed",
                data={
                    "spare_id": sid,
                    "session_ref": row["session_ref"],
                    "as": "chat",
                    "via": str(source.get("channel") or "status-app"),
                    "source": dict(source),
                    **({"page_command": page_command} if page_command else {}),
                },
            )
        ]

    return _cmd(env, "spare_claim", inp, build)


def direct_claim(conn: sqlite3.Connection, session_ref: str) -> list[EventDraft]:
    """A message the user sent straight to an unclaimed spare session (workdeck, the app):
    the session is the user's conversation from now on. The words are not recorded."""
    row = by_session(conn, session_ref)
    if row is None or row["status"] != "available":
        return []
    return [
        EventDraft(
            type="spare.claimed",
            data={
                "spare_id": row["spare_id"],
                "session_ref": session_ref,
                "as": "chat",
                "via": "direct",
            },
        )
    ]


def task_claim(  # noqa: PLR0917 - a claim is made of exactly these
    conn: sqlite3.Connection,
    sid: str,
    task_id: str,
    attempt_id: str,
    target: Path,
    branch: str | None,
    config: EffectiveConfig | None = None,
    now: str | None = None,
) -> tuple[dict[str, Any], EventDraft]:
    """The spare session ``sid`` claimed for a task's attempt (``attempt_start`` with ``spare``)."""
    row = get(conn, sid)
    if row["status"] != "available":
        raise rejected("这个备用会话已不可用", reason="spare_unavailable", status=row["status"])
    if now is not None and config is not None and expired(row, config, now):
        raise rejected("这个备用会话已过期，等主会话退役它", reason="spare_expired", spare_id=sid)
    return row, EventDraft(
        type="spare.claimed",
        task_id=task_id,
        attempt=attempt_id,
        data={
            "spare_id": sid,
            "session_ref": row["session_ref"],
            "as": "task",
            "task_id": task_id,
            "attempt": attempt_id,
            "via": "orchestrator",
            "target": str(target),
            **({"target_branch": branch} if branch else {}),
        },
    )


def claim_message(task_id: str, attempt_id: str, sid: str, command: str, target: Path) -> str:
    """What the orchestrator sends the claimed session, unchanged (DN002 §5.9 step 3)."""
    return (
        f"{CLAIM_TAG} 你是 TaskSteward 任务 {task_id} 的 worker（attempt {attempt_id}），"
        f"由备用会话 {sid} 认领。这一轮只做三步，不做别的：\n"
        f"1. 用 Bash 运行：`{command}`\n"
        "2. 调用 `mcp__ccd_directory__change_directory`，"
        f"path 为 `{target}`（第 1 步刚建的目录）。\n"
        "3. 调用 tsw 的 `spare_moved`（outcome: moved；第 1 或第 2 步失败或被拒时 outcome: "
        "failed，detail 写结果原文），按返回的 wake 把 message 原样发给 session_id，然后结束"
        "这一轮。\n"
        "目录在这一轮结束后才移过去。之后主会话会发以 "
        f"`{START_TAG}` 开头的消息：那时你已在任务目录中，按那条消息开始工作。"
    )


def start_message(home: Home, task_id: str, attempt_id: str) -> str:
    """The orchestrator's message once the session moved (DN002 §5.9 step 5)."""
    worker_md = home.root / "roles" / "worker.md"
    return (
        f"{START_TAG} 先完整阅读 {worker_md}（TaskSteward worker 说明，本会话的提示语里没有它），"
        f"再调用 whoami，按任务书 runs/{task_id}/{attempt_id}/brief.md 执行。"
    )


@tool(
    "spare_moved",
    "worker",
    description=(
        "由备用会话认领的 worker 登记已建好任务目录并请求移动（spare.moved，DN002 §5.9）；"
        "按返回的 wake 唤醒主会话"
    ),
)
def spare_moved(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    outcome = text(inp, "outcome")
    detail = opt_text(inp, "detail")
    if outcome not in MOVE_OUTCOMES:
        raise rejected("outcome 必须是 moved 或 failed", reason="invalid_input", field="outcome")

    def build(ctx: CommitContext) -> list[EventDraft]:
        found = worker_attempt(ctx.conn, env, task_id)
        attempt_id = str(found["attempt"])
        row = claimed_for(ctx.conn, task_id, attempt_id)
        if row is None or row["session_ref"] != env.caller.session_id:
            raise rejected("本会话不是由备用会话认领的", reason="not_spare")
        target = target_dir(ctx.conn, task_id, attempt_id)
        if outcome == "moved":
            problem = target_problem(row, target)
            if problem:
                raise rejected(problem, reason="spare_target_missing", path=str(target))
        data: dict[str, Any] = {
            "spare_id": row["spare_id"],
            "task_id": task_id,
            "attempt": attempt_id,
            "outcome": outcome,
            "path": str(target) if target is not None else None,
            # Kept with the event: the orchestrator may read it from its inbox later (PR #95).
            "start_message": start_message(ctx.home, task_id, attempt_id),
        }
        if detail:
            data["detail"] = detail
        output["start_message"] = data["start_message"]
        return [EventDraft(type="spare.moved", task_id=task_id, attempt=attempt_id, data=data)]

    output: dict[str, Any] = {}
    command = _cmd(env, "spare_moved", inp, build)
    command.output = output
    return command


def target_dir(conn: sqlite3.Connection, task_id: str, attempt_id: str) -> Path | None:
    """Where the claimed session moves: recorded when the claim was made."""
    row = claimed_for(conn, task_id, attempt_id)
    value = row["target"] if row is not None else None
    return Path(str(value)) if value else None


def target_problem(row: Mapping[str, Any] | None, target: Path | None) -> str | None:
    """Why the folder the session was to make is not there as expected (None when it is)."""
    if target is None or row is None:
        return "这个 attempt 没有记录备用会话要进入的目录"
    if not target.is_dir():
        return f"目录 {target} 不存在：先按认领消息第 1 步建好它"
    branch = row.get("target_branch")
    if branch:
        head = _git_head(target)
        if head != branch:
            return f"{target} 不是在分支 {branch} 上的 worktree（当前：{head or '不是 git 仓库'}）"
    return None


def _git_head(path: Path) -> str | None:
    import subprocess  # noqa: PLC0415 - only this check runs git

    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None
