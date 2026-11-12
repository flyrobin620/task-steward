# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Orchestrator tools: planning, dispatch, attempts, pause and resume, judgment, verification,
acceptance and cancellation (DN002 §3.2, §5; DN006 §8.3)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig, resolve_repository
from tsw.errors import rejected
from tsw.events import EventDraft, digest
from tsw.home import Home, same_dir, workdir_path
from tsw.importer.resume import resume_package
from tsw.machine import guards
from tsw.machine import session_titles as _titles
from tsw.machine.base import (
    ToolEnv,
    flag,
    integer,
    items,
    mapping,
    opt_text,
    strings,
    text,
    tool,
    worker_attempt,
)
from tsw.machine.model import (
    attempt,
    current_attempt,
    dumps,
    loads,
    next_item_id,
    require_status,
    review_cycle,
    row_dict,
    task,
)
from tsw.roles import record_given, worker_instructions, worker_prompt
from tsw.tasks import TaskSpec, register, render_spec, validate_spec
from tsw.workspace import brief_section, ensure, home_skills, plan


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def _next_request_id(conn: sqlite3.Connection, task_id: str, prefix: str = "Q") -> str:
    return next_item_id(conn, "input_requests", "request_id", prefix, task_id)


def _decision_id(conn: sqlite3.Connection, task_id: str) -> str:
    return next_item_id(conn, "decisions", "decision_id", "D", task_id)


# ---------------------------------------------------------------------- registration and planning


@tool("task_register", "orchestrator", description="登记任务（T01）")
def task_register(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    spec = TaskSpec(
        task_id=opt_text(inp, "task_id"),
        title=text(inp, "title"),
        repo=text(inp, "repo"),
        kind=text(inp, "kind"),
        goal=text(inp, "goal"),
        acceptance=strings(items(inp, "acceptance"), "acceptance"),
    )
    return register(spec, actor=env.caller.actor(), command_id=opt_text(inp, "command_id"))


def _would_cycle(conn: sqlite3.Connection, task_id: str, deps: list[str]) -> bool:
    graph: dict[str, list[str]] = {}
    for row in conn.execute("SELECT task_id, dep_id FROM dependencies"):
        graph.setdefault(str(row["task_id"]), []).append(str(row["dep_id"]))
    graph[task_id] = deps
    seen: set[str] = set()
    stack = list(deps)
    while stack:
        node = stack.pop()
        if node == task_id:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, []))
    return False


@tool("task_plan", "orchestrator", description="规划任务（T03 / T04）")
def task_plan(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    deps_in = items(inp, "dependencies", required=False)
    resources = strings(items(inp, "resources", required=False), "resources")
    reason = text(inp, "reason")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "intake")
        repo = guards.g_plan(row, env.config)
        pending = ctx.conn.execute(
            "SELECT request_id FROM input_requests WHERE task_id = ? AND status = 'pending'",
            (task_id,),
        ).fetchall()
        if pending:
            raise rejected(
                "仍有待澄清的问题", reason="pending_questions", requests=[r[0] for r in pending]
            )
        deps: list[dict[str, Any]] = []
        for dep in deps_in:
            if not isinstance(dep, dict) or not isinstance(dep.get("task_id"), str):
                raise rejected(
                    "依赖格式应为 {task_id, reason}", reason="invalid_input", field="dependencies"
                )
            dep_id = str(dep["task_id"])
            if dep_id == task_id:
                raise rejected("任务不能依赖自己", reason="dependency_self")
            if any(existing["task_id"] == dep_id for existing in deps):
                raise rejected("依赖重复", reason="dependency_duplicate", dep_id=dep_id)
            upstream = task(ctx.conn, dep_id)
            deps.append(
                {
                    "task_id": dep_id,
                    "bound_revision": int(upstream["revision"]),
                    "kind": str(dep.get("kind") or "explicit"),
                    "reason": str(dep.get("reason") or ""),
                }
            )
        if _would_cycle(ctx.conn, task_id, [d["task_id"] for d in deps]):
            raise rejected("依赖形成环", reason="dependency_cycle")
        # Evaluate G-DEP against the new dependency set.
        satisfied = True
        for dep in deps:
            upstream = task(ctx.conn, dep["task_id"])
            if not (
                upstream["status"] in ("done", "accepted")
                or (upstream["status"] == "archived" and upstream["previous_status"] == "accepted")
            ) or int(upstream["stale"]):
                satisfied = False
        return [
            EventDraft(
                type="task.planned",
                task_id=task_id,
                revision=int(row["revision"]),
                data={
                    "transition": "T04" if satisfied else "T03",
                    "dependencies": deps,
                    "resources": resources,
                    "repo": repo,
                    "reason": reason,
                },
            )
        ]

    return _cmd(env, "task_plan", inp, build)


@tool("intake_question", "orchestrator", description="受理时向用户提问（T02）")
def intake_question(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    question = text(inp, "question")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "intake")
        return [
            EventDraft(
                type="input.requested",
                task_id=task_id,
                revision=int(row["revision"]),
                data={
                    "transition": "T02",
                    "request_id": _next_request_id(ctx.conn, task_id),
                    "kind": "intake",
                    "question": question,
                    "binding": {"revision": int(row["revision"])},
                },
            )
        ]

    return _cmd(env, "intake_question", inp, build)


def _pending_request(conn: sqlite3.Connection, task_id: str, request_id: str) -> dict[str, Any]:
    found = row_dict(
        conn.execute(
            "SELECT * FROM input_requests WHERE request_id = ? AND task_id = ?",
            (request_id, task_id),
        ).fetchone()
    )
    if found is None:
        raise rejected("输入请求不存在", reason="request_not_found", request_id=request_id)
    return found


@tool("input_resolve", "orchestrator", description="记录用户答复（T02 / T12）")
def input_resolve(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    request_id = text(inp, "request_id")
    answer = text(inp, "answer")
    source_in = mapping(inp, "user_source")
    extend = int(inp.get("extend_rounds") or 0)

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        request = _pending_request(ctx.conn, task_id, request_id)
        source = guards.g_user(
            source_in, task_id=task_id, revision=int(row["revision"]), kind="answer"
        )
        if source.get("request_id") != request_id:
            raise rejected(
                "用户答复的来源指向别的请求",
                reason="g_user",
                request_id=request_id,
                source_request=source.get("request_id"),
            )
        if request["kind"] == "in_session" and source["channel"] == "in-session":
            # Answers in the worker's session are recorded by the worker with user_answer,
            # citing the message the hook recorded (DN006 §8.1a), whatever the request's state.
            raise rejected("worker 会话中的答复由 worker 用 user_answer 登记", reason="g_user")
        if request["status"] != "pending":
            return [
                EventDraft(
                    type="input.late_response",
                    task_id=task_id,
                    revision=int(row["revision"]),
                    data={"request_id": request_id, "source": source, "answer": answer},
                )
            ]
        binding = loads(request["binding"], {})
        data: dict[str, Any] = {"request_id": request_id, "source": source, "answer": answer}
        drafts: list[EventDraft] = []
        if request["kind"] == "intake":
            require_status(row, "intake")
            data["transition"] = "T02"
        elif request["kind"] in ("escalation", "in_session"):
            require_status(row, "waiting_user")
            attempt_row = current_attempt(ctx.conn, task_id)
            if (
                binding.get("revision") != int(row["revision"])
                or attempt_row is None
                or binding.get("attempt") != attempt_row["attempt"]
                or binding.get("generation") != int(attempt_row["generation"])
            ):
                return [
                    EventDraft(
                        type="input.late_response",
                        task_id=task_id,
                        revision=int(row["revision"]),
                        data={"request_id": request_id, "source": source, "answer": answer},
                    )
                ]
            data.update(
                transition="T12",
                # The requirement changed while waiting: old reviews are void, start a new cycle.
                new_cycle=row["waiting_revision"] is not None
                and int(row["waiting_revision"]) != int(row["revision"]),
                resume_substate=binding.get("substate") or "executing",
                resolves=binding.get("judgments", []),
            )
            if extend:
                # T12 across revisions opens a new cycle; the extension belongs to that cycle.
                cycle = int(row["cycle"]) + (1 if data["new_cycle"] else 0)
                drafts.append(
                    EventDraft(
                        type="review.budget_extended",
                        task_id=task_id,
                        revision=int(row["revision"]),
                        data={
                            "attempt": attempt_row["attempt"],
                            "cycle": cycle,
                            "rounds": extend,
                            "source_kind": "user",
                            "user_source": source,
                        },
                    )
                )
        elif request["kind"] == "imported":
            # A question the source system was still waiting on: record the answer; the task
            # moves on through its own transitions (resume, dispatch) afterwards.
            data["imported"] = True
        else:
            raise rejected("该请求需要专门的工具处理", reason="request_kind", kind=request["kind"])
        return [
            EventDraft(
                type="input.resolved", task_id=task_id, revision=int(row["revision"]), data=data
            ),
            *drafts,
        ]

    return _cmd(env, "input_resolve", inp, build)


def requirement_change_event(
    ctx: CommitContext,
    config: EffectiveConfig,
    row: Mapping[str, Any],
    *,
    goal: str,
    acceptance: list[str],
    source: Mapping[str, Any],
    transition: str | None,
    extra: Mapping[str, Any] | None = None,
    tell_session: bool = True,
) -> EventDraft:
    """``task.requirement_changed`` at revision+1 (DN002 §5.1): the task card and the brief of
    the task's current session are rewritten in the same event. Used by the orchestrator's
    ``task_update`` and by the worker's ``requirement_change`` (§5.1a)."""
    task_id = str(row["task_id"])
    revision = int(row["revision"]) + 1
    spec = TaskSpec(
        title=str(row["title"]),
        repo=str(row["repo"]),
        kind=str(row["kind"]),
        goal=goal,
        acceptance=acceptance,
        task_id=task_id,
    )
    validate_spec(spec)
    body = render_spec(task_id, spec) + f"\n（revision {revision}）\n"
    spec_sha = ctx.storage.put_artifact(str(row["spec_path"]), body.encode("utf-8"))
    data: dict[str, Any] = {
        "revision": revision,
        "goal": goal,
        "acceptance": acceptance,
        "spec_sha256": spec_sha,
        "user_source": dict(source),
        **(extra or {}),
    }
    current = current_attempt(ctx.conn, task_id)
    # A session prestarted before briefs were written at prestart has none yet: it gets one.
    unwritten = (
        current is not None
        and not current["brief_path"]
        and current["purpose"] == "prestart"
        and current["phase"] in ("starting", "standby")
    )
    if current is not None and (
        unwritten or (current["brief_path"] and current["phase"] not in ("replaced", "closed"))
    ):
        # DN002 §5.1: the brief of the task's session (prestarted, being dispatched or at
        # work) is brought to the new revision; the orchestrator tells a session that has
        # one, and dispatch rewrites it again from the latest revision.
        attempt_id = str(current["attempt"])
        path = str(current["brief_path"] or _brief_path(task_id, attempt_id))
        read_set = _read_set(ctx.conn, task_id) if unwritten else loads(current["read_set"], [])
        workdir = str(current["workdir"] or f"work/{task_id}/{attempt_id}")
        updated = {**row, "revision": revision, "goal": goal, "acceptance": dumps(acceptance)}
        brief = _brief(ctx, updated, attempt_id, workdir, read_set, config)
        ctx.storage.put_artifact(path, brief.encode("utf-8"))
        data["brief"] = {
            "attempt": current["attempt"],
            "revision": revision,
            "path": path,
            **({"read_set": read_set} if unwritten else {}),
            # The session the orchestrator tells about the new brief, if it has one; a worker
            # recording the change itself is that session (§5.1a).
            **(
                {"session_ref": current["session_ref"]}
                if tell_session
                and current["session_ref"]
                and current["phase"] in ("standby", "live", "stopped")
                else {}
            ),
        }
    if transition:
        data["transition"] = transition
    return EventDraft(
        type="task.requirement_changed", task_id=task_id, revision=revision, data=data
    )


@tool("task_update", "orchestrator", sync_after=True, description="用户修改需求（revision+1）")
def task_update(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    goal = text(inp, "goal")
    acceptance = strings(items(inp, "acceptance"), "acceptance")
    source_in = mapping(inp, "user_source")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        if row["status"] in ("accepted", "cancelled", "archived"):
            raise rejected(
                "已验收、取消或归档的任务不接受修改，请新建关联任务", reason="invalid_state"
            )
        source = guards.g_user(
            source_in, task_id=task_id, revision=int(row["revision"]), kind="change"
        )
        transition = {"verifying": "T17", "done": "T23"}.get(str(row["status"]))
        return [
            requirement_change_event(
                ctx,
                env.config,
                row,
                goal=goal,
                acceptance=acceptance,
                source=source,
                transition=transition,
            )
        ]

    return _cmd(env, "task_update", inp, build)


@tool("dependency_rebind", "orchestrator", description="依赖重新绑定到上游新 revision")
def dependency_rebind(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    dep_id = text(inp, "dep_id")
    scope = text(inp, "revalidate_scope")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        dep = ctx.conn.execute(
            "SELECT * FROM dependencies WHERE task_id = ? AND dep_id = ?", (task_id, dep_id)
        ).fetchone()
        if dep is None:
            raise rejected("没有这个依赖", reason="dependency_not_found")
        upstream = task(ctx.conn, dep_id)
        if int(upstream["revision"]) == int(dep["bound_revision"]):
            raise rejected("依赖已绑定到当前 revision", reason="already_bound")
        return [
            EventDraft(
                type="dependency.rebound",
                task_id=task_id,
                revision=int(row["revision"]),
                data={
                    "dep_id": dep_id,
                    "revision": int(upstream["revision"]),
                    "revalidate_scope": scope,
                },
            )
        ]

    return _cmd(env, "dependency_rebind", inp, build)


@tool("stale_keep", "orchestrator", sync_after=True, description="用户决定保留 stale 成果")
def stale_keep(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    source_in = mapping(inp, "user_source")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        if not int(row["stale"]):
            raise rejected("任务没有 stale 标记", reason="not_stale")
        source = guards.g_user(
            source_in, task_id=task_id, revision=int(row["revision"]), kind="keep"
        )
        return [
            EventDraft(
                type="dependency.stale_cleared",
                task_id=task_id,
                revision=int(row["revision"]),
                data={
                    "user_source": source,
                    "acknowledged": guards.dependency_problems(
                        ctx.conn, task_id, guards.invalidated(ctx.conn)
                    ),
                },
            )
        ]

    return _cmd(env, "stale_keep", inp, build)


# ---------------------------------------------------------------------- dispatch and attempts


def _delivery_line(config: EffectiveConfig, row: Mapping[str, Any]) -> str:
    """How the work leaves the worker (DN002 §3.5, G-SUBMIT; decision 0003)."""
    repo = resolve_repository(config, str(row["repo"]))
    delivery = config.get(f"repositories.{repo}.delivery", {}) if repo else {}
    yes = {True: "是", False: "否"}
    merge = (
        "worker 在门禁、评审与意见都解决后自行合并；等主分支上合并提交的 CI 通过（失败时在本任务内"
        "修复或 revert），提交时附 CI 运行链接（任务书或仓库 AGENTS.md 另有规定时从其规定，提交时"
        "说明）"
        if guards.merge_authorized(config, row)
        else "worker 不合并"
    )
    return (
        f"- 交付：推送 {yes[bool(delivery.get('push'))]}；"
        f"开 PR {yes[bool(delivery.get('create_pr'))]}；合并：{merge}\n"
        "- live：仓库规则要求的 live 场景在最终 head 上运行；需要在合并后的主分支上才能验证的"
        "（例如要更新插件或重新生成宿主文件才生效的改动），合并后运行；不能运行的在提交与报告中"
        "写明原因\n"
    )


def _brief(  # noqa: PLR0917 - the brief is rendered from exactly these inputs
    ctx: CommitContext,
    row: Mapping[str, Any],
    attempt_id: str,
    workdir: str,
    read_set: list[str],
    config: EffectiveConfig,
) -> str:
    acceptance = "\n".join(f"- {item}" for item in loads(row["acceptance"], []))
    ws = plan(ctx.home, config, row, attempt_id, workdir)
    repository = (
        brief_section(ws, home_skills(ctx.home) if ws.worktree else [], workdir) if ws else ""
    )
    return (
        f"# 任务书 {row['task_id']} {attempt_id}\n\n"
        f"- 标题：{row['title']}\n- 仓库：{row['repo']}\n- revision：{row['revision']}\n"
        f"- 执行目录：{workdir}\n- 授权读取集合：{', '.join(read_set)}\n"
        + _delivery_line(config, row)
        + "\n"
        + repository
        + f"## 目标\n\n{row['goal']}\n\n## 验收\n\n{acceptance}\n"
    ) + resume_package(ctx.conn, str(row["task_id"]))


def _brief_path(task_id: str, attempt_id: str) -> str:
    return f"runs/{task_id}/{attempt_id}/brief.md"


def _read_set(conn: sqlite3.Connection, task_id: str, extra: Sequence[str] = ()) -> list[str]:
    """The task itself, its dependencies and any extra tasks the orchestrator adds."""
    deps = [
        str(r["dep_id"])
        for r in conn.execute("SELECT dep_id FROM dependencies WHERE task_id = ?", (task_id,))
    ]
    return sorted({task_id, *deps, *extra})


@tool("task_dispatch", "orchestrator", description="派发任务（T07，R-ATTEMPT）")
def task_dispatch(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    workdir = text(inp, "workdir")
    branch = opt_text(inp, "branch")
    user_turn = flag(inp, "user_turn")
    extra_reads = strings(items(inp, "read_set", required=False), "read_set")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "ready")
        guards.g_slot(ctx.conn, env.config)
        resources = loads(row["resources"], [])
        guards.g_res(ctx.conn, task_id, resources)
        guards.g_dep(ctx.conn, task_id)
        previous = current_attempt(ctx.conn, task_id)
        caller = env.caller.session_id
        if previous is None or previous["phase"] in ("replaced", "closed"):
            number = int(row["attempt_count"]) + 1
            attempt_id, mode = f"a{number}", "new"
        elif previous["phase"] == "planned":
            attempt_id, mode = str(previous["attempt"]), "reuse_planned"
        elif previous["phase"] == "starting":
            raise rejected("会话创建结果未知，先按会话名对账", reason="reconcile_first")
        elif previous["phase"] in ("standby", "live", "stopped"):
            if not guards.controls(ctx.conn, previous, caller):
                raise rejected(
                    "原会话由其他主会话启动，核实停止后记为 replaced 再派发",
                    reason="attempt_uncontrollable",
                    attempt=previous["attempt"],
                )
            attempt_id = str(previous["attempt"])
            mode = "standby" if previous["phase"] == "standby" else "continue"
            if previous["workdir"] and not same_dir(ctx.home, workdir, str(previous["workdir"])):
                raise rejected(
                    "已有会话在另一个执行目录中，派发要沿用该目录",
                    reason="workdir_mismatch",
                    workdir=previous["workdir"],
                )
        else:
            raise rejected("未知的 attempt 阶段", reason="invalid_attempt", phase=previous["phase"])
        if mode in ("new", "reuse_planned"):
            guards.g_window(env.config, user_turn)
        read_set = _read_set(ctx.conn, task_id, extra_reads)
        brief_path = _brief_path(task_id, attempt_id)
        ctx.storage.put_artifact(
            brief_path,
            _brief(ctx, row, attempt_id, workdir, read_set, env.config).encode("utf-8"),
        )
        return [
            EventDraft(
                type="task.dispatch_started",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={
                    "transition": "T07",
                    "attempt": attempt_id,
                    "attempt_mode": mode,
                    "workdir": workdir,
                    "branch": branch,
                    "brief_path": brief_path,
                    "brief_revision": int(row["revision"]),
                    "read_set": read_set,
                    "resources": resources,
                    "config_digest": digest(env.config.values),
                },
            )
        ]

    return _cmd(env, "task_dispatch", inp, build)


def _require_current_brief(row: Mapping[str, Any], attempt_row: Mapping[str, Any]) -> None:
    if attempt_row["brief_revision"] is not None and int(attempt_row["brief_revision"]) != int(
        row["revision"]
    ):
        raise rejected(
            "任务书不是当前 revision，先更新任务书",
            reason="brief_outdated",
            brief_revision=attempt_row["brief_revision"],
            revision=row["revision"],
        )


def _session_name(env: ToolEnv, task_id: str, attempt_id: str, generation: int) -> str:
    pattern = str(env.config.get("sessions.name_pattern"))
    return pattern.format(
        task_id_lower=task_id.lower(), attempt=attempt_id, generation=generation, task_id=task_id
    )


def _ensure_dir(home: Home, recorded: str) -> None:
    """Create the execution directory a session is about to be created in, if it is missing."""
    path = workdir_path(home, recorded)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise rejected(
            "无法创建执行目录", reason="workdir_unavailable", workdir=recorded, error=str(exc)
        ) from exc


@tool(
    "attempt_start",
    "orchestrator",
    description=(
        "请求创建会话（attempt.start_requested，含预启动）；返回 start_session 用的 cwd 与 "
        "prompt（worker 说明加短指令或待命说明），原样传入"
    ),
)
def attempt_start(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    purpose = text(inp, "purpose")
    user_turn = flag(inp, "user_turn")
    given_workdir = opt_text(inp, "workdir")  # where the session is created
    given_title = opt_text(inp, "session_title")  # the main session's short title (DN002 §5.8)
    if purpose not in ("dispatch", "prestart"):
        raise rejected(
            "purpose 必须是 dispatch 或 prestart", reason="invalid_input", field="purpose"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        guards.g_window(env.config, user_turn)
        current = current_attempt(ctx.conn, task_id)
        if purpose == "dispatch":
            require_status(row, "dispatching")
            if current is None or current["phase"] != "planned":
                raise rejected("派发中的 attempt 必须处于 planned", reason="invalid_attempt")
            attempt_id = str(current["attempt"])
            if (
                given_workdir
                and current["workdir"]
                and not same_dir(ctx.home, given_workdir, str(current["workdir"]))
            ):
                raise rejected(
                    "会话目录必须是派发时记录的执行目录",
                    reason="workdir_mismatch",
                    workdir=current["workdir"],
                )
        else:
            require_status(row, "ready", "waiting_dependency")
            if not env.config.get("scheduling.prestart.enabled"):
                raise rejected("配置关闭了预启动", reason="prestart_disabled")
            standby = ctx.conn.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE phase = 'standby' "
                "OR (phase = 'starting' AND purpose = 'prestart')"
            ).fetchone()["n"]
            limit = int(env.config.get("scheduling.prestart.max_standby"))
            if int(standby) >= limit:
                raise rejected("待命会话已达上限", reason="max_standby", limit=limit)
            if current is None or current["phase"] in ("replaced", "closed"):
                attempt_id = f"a{int(row['attempt_count']) + 1}"
            elif current["phase"] == "planned":
                attempt_id = str(current["attempt"])
            else:
                raise rejected(
                    "已有待命或可续用的会话", reason="g_one_attempt", phase=current["phase"]
                )
        guards.g_one_attempt(ctx.conn, task_id, allowed=None)
        # The session is created with the worker instructions at the top of its prompt
        # (DN006 §6.2): refuse before the directory and the worktree are made when the data
        # directory's copy is missing or not the current one.
        instructions, digest = worker_instructions(ctx.home.root)
        previous = attempt(ctx.conn, task_id, attempt_id)
        # Every prestart generation records its directory, the default included, so a retry
        # never inherits the directory of a generation that was not created.
        workdir = given_workdir or f"work/{task_id}/{attempt_id}"
        # The host creates the session in this directory and fails when it does not exist.
        recorded = current["workdir"] if purpose == "dispatch" and current is not None else None
        _ensure_dir(ctx.home, str(recorded or workdir))
        # A repository task works in a worktree of the repository inside that directory: the
        # session is created there (CC006 §8). It is made before anything is recorded; a
        # failure refuses the start like a directory that cannot be created.
        ws = plan(ctx.home, env.config, row, attempt_id, str(recorded or workdir))
        made = ensure(ws) if ws is not None and ws.worktree is not None else None
        generation = (int(previous["generation"]) if previous else 0) + 1
        # The title the session is created with, and the base the later titles are made of
        # (DN002 §5.8): tsw renames a session only while it still has the title tsw gave it.
        base = _titles.base_title(task_id, str(row["title"]), given_title)
        session_title = _titles.creation_title(base, purpose)
        data: dict[str, Any] = {
            "attempt": attempt_id,
            "generation": generation,
            "session_name": _session_name(env, task_id, attempt_id, generation),
            "purpose": purpose,
            "controller": env.caller.session_id,
            "adapter": str(env.config.get("roles.worker.session")),
            **({"worktree": made} if made is not None else {}),
            "worker_instructions_sha256": digest,
            "title_base": base,
            "session_title": session_title,
        }
        # What the main session passes to start_session unchanged, and which version of the
        # instructions this session gets (the worker's hooks tell it of a newer one).
        output["prompt"] = worker_prompt(instructions, task_id, attempt_id, purpose)
        output["session_title"] = session_title
        record_given(ctx.home.root, str(data["session_name"]), digest)
        if purpose == "prestart":
            # The brief is written now, at the current revision, so what was decided for the
            # task is on disk before the context that decided it is gone; dispatch and every
            # requirement change rewrite it (DN002 §5.6).
            read_set = _read_set(ctx.conn, task_id)
            brief_path = _brief_path(task_id, attempt_id)
            ctx.storage.put_artifact(
                brief_path,
                _brief(ctx, row, attempt_id, workdir, read_set, env.config).encode("utf-8"),
            )
            data |= {
                "workdir": workdir,
                "brief_path": brief_path,
                "brief_revision": int(row["revision"]),
                "read_set": read_set,
            }
        return [
            EventDraft(
                type="attempt.start_requested",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data=data,
            )
        ]

    output: dict[str, Any] = {}
    command = _cmd(env, "attempt_start", inp, build)
    command.output = output
    return command


@tool("attempt_reconcile", "orchestrator", description="会话创建结果对账（T08 / T09 / 迟到会话）")
def attempt_reconcile(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    attempt_id = text(inp, "attempt")
    generation = integer(inp, "generation", minimum=1)
    outcome = text(inp, "outcome")
    session_ref = opt_text(inp, "session_ref")
    receipt = mapping(inp, "receipt", required=False)
    # The title the session was actually created with, when it is not the one attempt_start
    # returned (DN002 §5.8).
    used_title = opt_text(inp, "session_title")
    titled = {"session_title": used_title} if used_title else {}
    if outcome not in ("created", "not_created"):
        raise rejected(
            "outcome 必须是 created 或 not_created", reason="invalid_input", field="outcome"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        found = attempt(ctx.conn, task_id, attempt_id)
        if found is None:
            raise rejected("attempt 不存在", reason="invalid_attempt")
        revision = int(row["revision"])
        current_gen = int(found["generation"]) == generation and bool(found["generation_valid"])
        if outcome == "created" and not (current_gen and found["phase"] == "starting"):
            if not session_ref:
                raise rejected("迟到会话需要会话引用", reason="invalid_input", field="session_ref")
            return [
                EventDraft(
                    type="attempt.late_session",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt_id,
                    data={
                        "attempt": attempt_id,
                        "generation": generation,
                        "session_ref": session_ref,
                    },
                )
            ]
        if found["phase"] != "starting" or not current_gen:
            raise rejected("只能对当前有效代次的 starting 阶段对账", reason="invalid_attempt")
        if outcome == "not_created":
            drafts = [
                EventDraft(
                    type="attempt.not_created",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt_id,
                    data={"attempt": attempt_id, "generation": generation},
                )
            ]
            if found["purpose"] == "dispatch" and row["status"] == "dispatching":
                drafts.append(
                    EventDraft(
                        type="attempt.dispatch_abandoned",
                        task_id=task_id,
                        revision=revision,
                        attempt=attempt_id,
                        data={"transition": "T09", "attempt": attempt_id, "generation": generation},
                    )
                )
            return drafts
        if not session_ref:
            raise rejected("创建成功需要会话引用", reason="invalid_input", field="session_ref")
        if row["status"] in ("cancelled", "archived", "accepted", "done"):
            # The task no longer wants this session: record it so the caller stops it.
            return [
                EventDraft(
                    type="attempt.late_session",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt_id,
                    data={
                        "attempt": attempt_id,
                        "generation": generation,
                        "session_ref": session_ref,
                    },
                )
            ]
        if found["purpose"] == "prestart":
            return [
                EventDraft(
                    type="attempt.prestarted",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt_id,
                    data={
                        "attempt": attempt_id,
                        "session_ref": session_ref,
                        "generation": generation,
                        **titled,
                    },
                )
            ]
        require_status(row, "dispatching")
        _require_current_brief(row, found)
        if not flag(inp, "brief_delivered"):
            raise rejected("新建会话须确认任务书已送达", reason="brief_not_delivered")
        guards.g_one_attempt(ctx.conn, task_id, allowed=attempt_id)
        return [
            EventDraft(
                type="attempt.bound",
                task_id=task_id,
                revision=revision,
                attempt=attempt_id,
                data={
                    "transition": "T08",
                    "attempt": attempt_id,
                    "session_ref": session_ref,
                    "generation": generation,
                    "receipt": receipt or {},
                    **titled,
                },
            )
        ]

    return _cmd(env, "attempt_reconcile", inp, build)


@tool("attempt_deliver", "orchestrator", description="向待命会话投递任务书（T08）")
def attempt_deliver(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "dispatching")
        current = current_attempt(ctx.conn, task_id)
        if (
            current is None
            or current["phase"] != "standby"
            or not guards.controllable(current, env.caller.session_id, ctx.conn)
        ):
            raise rejected("没有由当前主会话控制的待命会话", reason="attempt_uncontrollable")
        _require_current_brief(row, current)
        return [
            EventDraft(
                type="attempt.brief_delivered",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=str(current["attempt"]),
                data={"transition": "T08", "attempt": current["attempt"]},
            )
        ]

    return _cmd(env, "attempt_deliver", inp, build)


@tool("attempt_continue", "orchestrator", description="续用原会话（T08）或扩大授权读取集合")
def attempt_continue(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    extra_reads = strings(items(inp, "read_set", required=False), "read_set")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        current = current_attempt(ctx.conn, task_id)
        if (
            current is None
            or current["phase"] not in ("live", "stopped")
            or not guards.controllable(current, env.caller.session_id, ctx.conn)
        ):
            raise rejected("没有由当前主会话控制的原会话", reason="attempt_uncontrollable")
        read_set = sorted({*loads(current["read_set"], []), *extra_reads})
        data: dict[str, Any] = {"attempt": current["attempt"], "read_set": read_set}
        if row["status"] == "dispatching":
            _require_current_brief(row, current)
            data["transition"] = "T08"
        else:
            require_status(row, "in_progress", "waiting_user", "verifying")
        return [
            EventDraft(
                type="attempt.continued",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=str(current["attempt"]),
                data=data,
            )
        ]

    return _cmd(env, "attempt_continue", inp, build)


@tool(
    "attempt_update",
    "orchestrator",
    description="记录 attempt 的平台事实（stopped / replaced / closed / lost）",
)
def attempt_update(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    attempt_id = text(inp, "attempt")
    event = text(inp, "event")
    evidence = text(inp, "evidence")
    if event not in ("stopped", "replaced", "closed", "lost"):
        raise rejected(
            "event 必须是 stopped、replaced、closed 或 lost", reason="invalid_input", field="event"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        found = attempt(ctx.conn, task_id, attempt_id)
        if found is None:
            raise rejected("attempt 不存在", reason="invalid_attempt")
        data: dict[str, Any] = {"attempt": attempt_id, "evidence": evidence}
        if event == "lost":
            if row["status"] == "in_progress" and row["current_attempt"] == attempt_id:
                data["transition"] = "T15"
                return [
                    EventDraft(
                        type="attempt.lost",
                        task_id=task_id,
                        revision=int(row["revision"]),
                        attempt=attempt_id,
                        data=data,
                    )
                ]
            event_type = "attempt.replaced"  # a lost standby or stopped session
        elif event == "replaced":
            if (
                row["status"] in ("dispatching", "in_progress", "waiting_user")
                and row["current_attempt"] == attempt_id
            ):
                raise rejected(
                    "占用名额的 attempt 不能直接替换，先暂停或经 T15", reason="invalid_state"
                )
            if found["phase"] == "starting":
                raise rejected("starting 阶段须先对账", reason="reconcile_first")
            event_type = "attempt.replaced"
        else:
            event_type = f"attempt.{event}"
        return [
            EventDraft(
                type=event_type,
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data=data,
            )
        ]

    return _cmd(env, "attempt_update", inp, build)


# ---------------------------------------------------------------------- pause and resume


@tool("task_pause", "orchestrator", "worker", description="暂停（T10 / T14 / T19）")
def task_pause(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    pause = {
        "category": text(inp, "category"),
        "owner": text(inp, "owner"),
        "next_action": text(inp, "next_action"),
        "resume_condition": text(inp, "resume_condition"),
    }
    worker_stopped = flag(inp, "worker_stopped")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        guards.g_pause(pause)
        current = current_attempt(ctx.conn, task_id)
        data: dict[str, Any] = {"pause": dict(pause)}
        if env.caller.role == "worker":
            worker_attempt(ctx.conn, env, task_id)
            if row["status"] != "in_progress":
                raise rejected("worker 只能暂停自己执行中的任务（T14）", reason="role_denied")
        status = row["status"]
        if status == "dispatching":
            if pause["category"] != "workdir_conflict":
                raise rejected("派发中只能因执行目录冲突暂停", reason="invalid_state")
            data["transition"] = "T10"
        elif status == "in_progress":
            if not worker_stopped:
                raise rejected("暂停前 worker 必须已停止", reason="worker_not_stopped")
            data["transition"] = "T14"
            if current is not None:
                data["attempt_stopped"] = current["attempt"]
        elif status == "verifying":
            if pause["category"] not in ("integration_blocked", "failure"):
                raise rejected("核验中只能因集成受阻或失败暂停", reason="invalid_state")
            data["transition"] = "T19"
            data["pause"]["resume_target"] = "verifying"
        else:
            raise rejected("当前状态不能暂停", reason="invalid_state", status=status)
        return [
            EventDraft(
                type="task.paused", task_id=task_id, revision=int(row["revision"]), data=data
            )
        ]

    return _cmd(env, "task_pause", inp, build)


def _submission_valid(conn: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
    submission = loads(row["submission"], None)
    if not submission or int(submission.get("revision", -1)) != int(row["revision"]):
        return False
    bindings = {
        str(r["dep_id"]): int(r["bound_revision"])
        for r in conn.execute(
            "SELECT dep_id, bound_revision FROM dependencies WHERE task_id = ?", (row["task_id"],)
        )
    }
    return bindings == submission.get("dependencies", {})


@tool("task_resume", "orchestrator", description="恢复（T20 / T21 / T27，按 §5.3 选择）")
def task_resume(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    if not flag(inp, "condition_met"):
        raise rejected("恢复条件尚未满足", reason="resume_condition")
    note = text(inp, "note")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "paused")
        pause = loads(row["pause"], {})
        resources = loads(row["resources"], [])
        current = current_attempt(ctx.conn, task_id)

        def admissible() -> bool:
            try:
                guards.g_slot(ctx.conn, env.config)
                guards.g_res(ctx.conn, task_id, resources)
                guards.g_dep(ctx.conn, task_id)
            except Exception:
                return False
            return True

        data: dict[str, Any] = {"note": note, "resources": resources}
        if pause.get("resume_target") == "verifying" and _submission_valid(ctx.conn, row):
            # T27 only; when it cannot be admitted now the task stays paused with its evidence.
            guards.g_slot(ctx.conn, env.config)
            guards.g_res(ctx.conn, task_id, resources)
            guards.g_dep(ctx.conn, task_id)
            data["transition"] = "T27"
        elif (
            current is not None
            and current["phase"] in ("live", "stopped")
            and guards.controllable(current, env.caller.session_id, ctx.conn)
            and admissible()
        ):
            data.update(transition="T20", attempt=current["attempt"])
        else:
            data["transition"] = "T21"
        return [
            EventDraft(
                type="task.resumed", task_id=task_id, revision=int(row["revision"]), data=data
            )
        ]

    return _cmd(env, "task_resume", inp, build)


# ---------------------------------------------------------------------- judgment (DN006 §8.3)


@tool(
    "judge",
    "orchestrator",
    description="主会话判断：回答、追加轮次、认定不成立、批准延后、调整方向或交给人类",
)
def judge(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    decision = text(inp, "decision")
    rationale = text(inp, "rationale")
    basis = strings(items(inp, "basis"), "basis")
    confidence = text(inp, "confidence")
    item_ids = strings(items(inp, "items"), "items")
    if decision not in ("answer", "extend", "overrule", "defer", "feedback", "escalate"):
        raise rejected("未知的决定", reason="invalid_input", field="decision")
    if confidence not in ("high", "medium", "low"):
        raise rejected(
            "confidence 必须是 high、medium 或 low", reason="invalid_input", field="confidence"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        reask = row["status"] == "waiting_user"
        if reask:
            # DN002 §5.1: requests superseded by a requirement change are asked again for the
            # new revision; the task stays waiting for the user.
            pending = ctx.conn.execute(
                "SELECT 1 FROM input_requests WHERE task_id = ? AND status = 'pending'", (task_id,)
            ).fetchone()
            if pending is not None or decision != "escalate":
                raise rejected(
                    "等待用户时只能在原请求作废后按新 revision 重新提问",
                    reason="invalid_state",
                )
        else:
            require_status(row, "in_progress")
        revision, cycle = int(row["revision"]), int(row["cycle"])
        current = current_attempt(ctx.conn, task_id)
        assert current is not None
        triggers = []
        for item_id in item_ids:
            found = row_dict(
                ctx.conn.execute(
                    "SELECT * FROM judgments WHERE item_id = ? AND task_id = ?", (item_id, task_id)
                ).fetchone()
            )
            if found is None or found["status"] != "open":
                raise rejected("没有待判断的事项", reason="judgment_not_open", item=item_id)
            triggers.append(str(found["trigger"]))
        decision_id = _decision_id(ctx.conn, task_id)
        record = EventDraft(
            type="orchestrator.decision",
            task_id=task_id,
            revision=revision,
            data={
                "decision_id": decision_id,
                "trigger": ",".join(triggers),
                "decision": decision,
                "rationale": rationale,
                "basis": basis,
                "confidence": confidence,
                "answer": inp.get("answer"),
                "resolves": item_ids if decision in ("answer",) else [],
            },
        )
        base = {
            "attempt": current["attempt"],
            "cycle": cycle,
            "decision_id": decision_id,
            "resolves": item_ids,
        }
        if decision == "answer":
            text(inp, "answer")
            return [record]
        if decision == "extend":
            rounds = integer(inp, "rounds", minimum=1)
            if "review.budget_exhausted" not in triggers:
                raise rejected("只有评审预算用完时才能追加轮次", reason="invalid_trigger")
            cyc = review_cycle(ctx.conn, task_id, str(current["attempt"]), cycle)
            limit = int(env.config.get("review.orchestrator_extension_limit"))
            if int(cyc["orchestrator_extension"]) + rounds > limit:
                raise rejected(
                    "主会话累计追加超过上限，只能交给人类", reason="extension_limit", limit=limit
                )
            return [
                record,
                EventDraft(
                    type="review.budget_extended",
                    task_id=task_id,
                    revision=revision,
                    data={**base, "rounds": rounds, "source_kind": "orchestrator"},
                ),
            ]
        if decision in ("overrule", "defer"):
            refs = items(inp, "findings")
            checked = []
            for ref in refs:
                if not isinstance(ref, dict):
                    raise rejected("findings 元素应为 {round, finding}", reason="invalid_input")
                found = ctx.conn.execute(
                    "SELECT 1 FROM findings WHERE task_id = ? AND attempt = ? AND cycle = ? AND "
                    "round = ? AND finding_id = ? AND blocking = 1",
                    (task_id, current["attempt"], cycle, ref.get("round"), ref.get("finding")),
                ).fetchone()
                if found is None:
                    raise rejected("本周期没有这个阻断项", reason="finding_not_found", ref=ref)
                checked.append({"round": ref["round"], "finding": ref["finding"]})
            event = (
                "review.finding_overruled" if decision == "overrule" else "review.defer_approved"
            )
            return [
                record,
                EventDraft(
                    type=event,
                    task_id=task_id,
                    revision=revision,
                    data={**base, "findings": checked},
                ),
            ]
        if decision == "feedback":
            return [
                record,
                EventDraft(
                    type="orchestrator.feedback",
                    task_id=task_id,
                    revision=revision,
                    data={**base, "feedback": text(inp, "feedback")},
                ),
            ]
        # escalate: T11
        request_id = _next_request_id(ctx.conn, task_id)
        return [
            record,
            EventDraft(
                type="input.escalated",
                task_id=task_id,
                revision=revision,
                data={
                    **({} if reask else {"transition": "T11"}),
                    "request_id": request_id,
                    "kind": "escalation",
                    "question": text(inp, "question"),
                    "binding": {
                        "revision": revision,
                        "attempt": current["attempt"],
                        "generation": int(current["generation"]),
                        "cycle": cycle,
                        "substate": row["substate"] if not reask else "executing",
                        "judgments": item_ids,
                        "decision_id": decision_id,
                    },
                    "resolves": item_ids,
                },
            ),
        ]

    return _cmd(env, "judge", inp, build)


# ---------------------------------------------------------------------- verification and acceptance


@tool("feedback", "orchestrator", description="核验后给出剩余工作（T17）")
def feedback(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    work = strings(items(inp, "items"), "items")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "verifying")
        if not work:
            raise rejected("反馈必须列出具体剩余工作", reason="invalid_input", field="items")
        return [
            EventDraft(
                type="orchestrator.feedback",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"transition": "T17", "items": work},
            )
        ]

    return _cmd(env, "feedback", inp, build)


@tool("integrate", "orchestrator", description="集成进展（verifying 内部）")
def integrate(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    repo = text(inp, "repo")
    status = text(inp, "status")
    if status not in ("started", "conflict", "completed", "withdrawn"):
        raise rejected(
            "status 必须是 started、conflict、completed 或 withdrawn", reason="invalid_input"
        )

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        if status != "withdrawn":
            require_status(row, "verifying")
        return [
            EventDraft(
                type=f"integration.{status}",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"repo": repo, "detail": inp.get("detail")},
            )
        ]

    return _cmd(env, "integrate", inp, build)


@tool(
    "output_link",
    "orchestrator",
    "worker",
    description="登记 PR 或产出（worker 登记草稿，主会话核验）",
)
def output_link(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    kind = text(inp, "type")
    data = {
        "output_id": text(inp, "output_id"),
        "type": kind,
        "ref": text(inp, "ref"),
        "role": opt_text(inp, "role"),
        "delivery": opt_text(inp, "delivery"),
        "evidence": strings(items(inp, "evidence"), "evidence"),
        "verified_at": None,
    }

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        if env.caller.role == "worker":
            worker_attempt(ctx.conn, env, task_id)
        else:
            data["verified_at"] = text(inp, "verified_at")
        event = "pr.registered" if kind == "pr" else "output.registered"
        return [
            EventDraft(type=event, task_id=task_id, revision=int(row["revision"]), data=dict(data))
        ]

    return _cmd(env, "output_link", inp, build)


@tool("task_accept", "orchestrator", sync_after=True, description="主会话技术验收（T18，G-ACCEPT）")
def task_accept(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    verified_head = text(inp, "verified_head")
    results = items(inp, "acceptance")
    summary = text(inp, "summary")
    post_merge = opt_text(inp, "post_merge")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "verifying")
        revision = int(row["revision"])
        submission = loads(row["submission"], None)
        if (
            not submission
            or int(submission["revision"]) != revision
            or not _submission_valid(ctx.conn, row)
        ):
            raise rejected(
                "提交证据不属于当前 revision 或依赖绑定已变", reason="g_accept", check="revision"
            )
        if verified_head != submission["head"]:
            raise rejected("核对的 HEAD 与提交 HEAD 不一致", reason="g_accept", check="head")
        checked = guards.g_accept_items(
            loads(row["acceptance"], []), results, task_id=task_id, revision=revision
        )
        current = current_attempt(ctx.conn, task_id)
        assert current is not None
        recheck = guards.g_submit(
            ctx.conn,
            env.config,
            {**row, "cycle": submission["cycle"]},
            str(current["attempt"]),
            str(submission["head"]),
        )
        guards.g_dep(ctx.conn, task_id)
        verified = ctx.conn.execute(
            "SELECT output_id FROM outputs WHERE task_id = ? AND verified_at IS NOT NULL",
            (task_id,),
        ).fetchall()
        if not verified:
            raise rejected("没有已核验登记的 PR 或产出", reason="g_accept", check="outputs")
        merge = submission.get("merge")
        if merge:
            # The worker merged and waited for the merge commit's CI before submitting: the
            # orchestrator checks the evidence it attached (DN002 G-ACCEPT, decision 0003).
            if not ctx.conn.execute(
                "SELECT 1 FROM outputs WHERE task_id = ? AND type = 'pr' AND ref = ? "
                "AND verified_at IS NOT NULL",
                (task_id, merge["pr"]),
            ).fetchone():
                raise rejected(
                    "worker 合并的 PR 尚未核验登记",
                    reason="g_accept",
                    check="merged_pr",
                    pr=merge["pr"],
                )
            if not (post_merge and post_merge.strip()):
                raise rejected(
                    "worker 已合并：须写明对 worker 所附合并证据（合并提交、CI 链接、live）的核对",
                    reason="g_accept",
                    check="post_merge",
                )
        open_integrations = ctx.conn.execute(
            "SELECT repo FROM integrations WHERE task_id = ? AND status IN ('started', 'conflict')",
            (task_id,),
        ).fetchall()
        if open_integrations:
            raise rejected(
                "集成尚未完成",
                reason="g_accept",
                check="integration",
                repos=[r[0] for r in open_integrations],
            )
        if ctx.conn.execute(
            "SELECT 1 FROM input_requests WHERE task_id = ? AND status = 'pending'", (task_id,)
        ).fetchone():
            raise rejected("仍有待答输入请求", reason="g_accept", check="pending_input")
        review_path = f"runs/{task_id}/{current['attempt']}/review.md"
        lines = [f"# 主控验收 {task_id} revision {revision}", "", summary, "", "## 验收逐项", ""]
        lines += [
            f"- {c['item']}：{'已完成' if c['status'] == 'done' else '用户明确替代'}"
            for c in checked
        ]
        if merge:
            lines += [
                "",
                "## 合并后核对",
                "",
                f"- worker 合并：{merge['pr']}（合并提交 {merge['merge_commit']}）",
                *(
                    [f"- 合并提交的 CI：{merge['ci']['conclusion']}（{merge['ci']['url']}）"]
                    if merge.get("ci")
                    else []
                ),
                *([f"- live：{merge['live']}"] if merge.get("live") else []),
                f"- 主会话核对：{post_merge}",
            ]
        decisions = ctx.conn.execute(
            "SELECT decision_id, decision, rationale FROM decisions WHERE task_id = ? ORDER BY "
            "sequence",
            (task_id,),
        ).fetchall()
        if decisions:
            lines += ["", "## 主会话决定", ""] + [
                f"- {d['decision_id']} {d['decision']}：{d['rationale']}" for d in decisions
            ]
        ctx.storage.put_artifact(review_path, ("\n".join(lines) + "\n").encode("utf-8"))
        return [
            EventDraft(
                type="task.accepted",
                task_id=task_id,
                revision=revision,
                data={
                    "transition": "T18",
                    "acceptance": checked,
                    "head": verified_head,
                    "review_path": review_path,
                    "reviews": recheck["rounds"],
                    **({"post_merge": post_merge} if merge else {}),
                },
            )
        ]

    return _cmd(env, "task_accept", inp, build)


@tool("user_accept", "orchestrator", "user", sync_after=True, description="用户验收（T22，G-USER）")
def user_accept(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    source_in = mapping(inp, "user_source")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "done")
        source = guards.g_user(
            source_in,
            task_id=task_id,
            revision=int(row["revision"]),
            kind="accept",
            role=env.caller.role,
        )
        if int(row["stale"]):
            raise rejected("任务有未解决的 stale 标记", reason="stale")
        if task_id in guards.invalidated(ctx.conn):
            # An upstream change anywhere in the chain, even before sync records it, blocks it.
            raise rejected(
                "上游依赖已变化，先运行 sync",
                reason="stale",
                dependencies=guards.dependency_problems(
                    ctx.conn, task_id, guards.invalidated(ctx.conn)
                ),
            )
        if ctx.conn.execute(
            "SELECT 1 FROM input_requests WHERE task_id = ? AND status = 'pending'", (task_id,)
        ).fetchone():
            raise rejected("仍有待答输入请求", reason="pending_input")
        return [
            EventDraft(
                type="task.user_accepted",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"transition": "T22", "user_source": source},
            )
        ]

    return _cmd(env, "user_accept", inp, build)


@tool(
    "user_reject",
    "orchestrator",
    "user",
    sync_after=True,
    description="用户退回（T23，revision+1）",
)
def user_reject(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    source_in = mapping(inp, "user_source")
    reason = text(inp, "reason")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "done")
        source = guards.g_user(
            source_in,
            task_id=task_id,
            revision=int(row["revision"]),
            kind="reject",
            role=env.caller.role,
        )
        return [
            EventDraft(
                type="task.user_rejected",
                task_id=task_id,
                revision=int(row["revision"]) + 1,
                data={
                    "transition": "T23",
                    "revision": int(row["revision"]) + 1,
                    "reason": reason,
                    "user_source": source,
                },
            )
        ]

    return _cmd(env, "user_reject", inp, build)


@tool("task_cancel", "orchestrator", "user", sync_after=True, description="用户取消（T25）")
def task_cancel(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    source_in = mapping(inp, "user_source")
    worker_stopped = flag(inp, "worker_stopped")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        if row["status"] not in (
            "intake",
            "waiting_dependency",
            "ready",
            "dispatching",
            "in_progress",
            "waiting_user",
            "paused",
            "verifying",
            "done",
        ):
            raise rejected("当前状态不能取消", reason="invalid_state", status=row["status"])
        source = guards.g_user(
            source_in,
            task_id=task_id,
            revision=int(row["revision"]),
            kind="cancel",
            role=env.caller.role,
        )
        phases = {
            str(r["attempt"]): str(r["phase"])
            for r in ctx.conn.execute(
                "SELECT attempt, phase FROM attempts WHERE task_id = ?", (task_id,)
            )
        }
        unsettled = [a for a, p in phases.items() if p in ("starting", "standby")]
        if unsettled:
            raise rejected(
                "创建中的会话须先对账，待命会话须先处置（attempt_update closed / replaced）",
                reason="attempt_unsettled",
                attempts=unsettled,
            )
        live = [a for a, p in phases.items() if p == "live"]
        if live and not worker_stopped:
            raise rejected("活动的 worker 须先停止", reason="worker_not_stopped", attempts=live)
        if ctx.conn.execute(
            "SELECT 1 FROM integrations WHERE task_id = ? AND status IN ('started', 'conflict')",
            (task_id,),
        ).fetchone():
            raise rejected("进行中的集成须先撤回", reason="integration_active")
        released = loads(row["resources"], []) if worker_stopped or not live else []
        return [
            EventDraft(
                type="task.cancelled",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"transition": "T25", "user_source": source, "released_resources": released},
            )
        ]

    return _cmd(env, "task_cancel", inp, build)


@tool("task_archive", "orchestrator", description="归档已取消的任务（T26）")
def task_archive(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    source_in = mapping(inp, "user_source")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "cancelled")
        if ctx.conn.execute(
            "SELECT 1 FROM follow_ups WHERE task_id = ? AND status = 'open' LIMIT 1", (task_id,)
        ).fetchone():
            # An archived task does not change (DN002 §8): its follow-ups are decided first.
            raise rejected(
                "任务还有 open 的 follow-up：先转为任务或驳回，再归档（DN002 §5.7）",
                reason="follow_ups_open",
            )
        source = guards.g_user(
            source_in, task_id=task_id, revision=int(row["revision"]), kind="archive"
        )
        return [
            EventDraft(
                type="task.archived",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"transition": "T26", "user_source": source},
            )
        ]

    return _cmd(env, "task_archive", inp, build)


@tool("dependency_invalidate", "orchestrator", description="依赖失效且 worker 已停止（T16）")
def dependency_invalidate(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    worker_stopped = flag(inp, "worker_stopped")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        require_status(row, "dispatching", "in_progress", "waiting_user", "verifying")
        problems = guards.dependency_problems(ctx.conn, task_id, guards.invalidated(ctx.conn))
        if not problems:
            raise rejected("依赖仍然满足", reason="dependencies_ok")
        current = current_attempt(ctx.conn, task_id)
        if current is not None and current["phase"] == "starting":
            raise rejected("会话创建结果未知，先对账", reason="reconcile_first")
        if current is not None and current["phase"] == "live" and not worker_stopped:
            raise rejected("worker 须先收到通知并停止", reason="worker_not_stopped")
        released = (
            loads(row["resources"], [])
            if worker_stopped or current is None or current["phase"] != "live"
            else []
        )
        return [
            EventDraft(
                type="dependency.invalidated",
                task_id=task_id,
                revision=int(row["revision"]),
                data={
                    "transition": "T16",
                    "dependencies": problems,
                    "released_resources": released,
                },
            )
        ]

    return _cmd(env, "dependency_invalidate", inp, build)


@tool("resource_release", "orchestrator", description="核实停止后释放独占资源")
def resource_release(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    resources = strings(items(inp, "resources"), "resources")
    evidence = text(inp, "evidence")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row = task(ctx.conn, task_id)
        if row["status"] in ("dispatching", "in_progress", "waiting_user", "verifying"):
            raise rejected("占用名额的任务不能单独释放资源", reason="invalid_state")
        held = {
            r["resource"]
            for r in ctx.conn.execute(
                "SELECT resource FROM resource_claims WHERE owner_task = ? AND status = 'held'",
                (task_id,),
            )
        }
        missing = [r for r in resources if r not in held]
        if missing:
            raise rejected("任务没有占用这些资源", reason="not_held", resources=missing)
        return [
            EventDraft(
                type="resource.released",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"resources": resources, "evidence": evidence},
            )
        ]

    return _cmd(env, "resource_release", inp, build)
