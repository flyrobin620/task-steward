"""Running tools, and the system rules (DN002 §5.2, T05 / T06 / T24, stale marking)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.events import EventDraft
from tsw.lease import parse_time
from tsw.ledger import Ledger
from tsw.machine import ask_user as _ask_user
from tsw.machine import config_tools as _config_tools
from tsw.machine import follow_up as _follow_up
from tsw.machine import orchestrator as _orchestrator
from tsw.machine import routine as _routine
from tsw.machine import worker as _worker
from tsw.machine.ask_user import handed_off_event, handoff_due
from tsw.machine.base import REGISTRY, Caller, Tool, ToolEnv, check_caller, check_version
from tsw.machine.guards import dependency_problems, invalidated, topological
from tsw.machine.model import OCCUPYING, loads, next_item_id

# Importing the tool modules registers their tools.
TOOL_MODULES = (_orchestrator, _worker, _config_tools, _ask_user, _routine, _follow_up)

SYNC = Tool(
    "sync",
    ("orchestrator", "system", "user"),
    lambda inp, env: sync_command(env),
    description="系统规则",
)


def sync_command(env: ToolEnv, report: dict[str, Any] | None = None) -> Command:
    """Dependency propagation and retention: one event per task whose rule applies."""
    out = report if report is not None else {}

    def build(ctx: CommitContext) -> list[EventDraft]:
        drafts: list[EventDraft] = []
        needs_stop: list[dict[str, Any]] = []
        archive_days = int(env.config.get("policies.auto_archive_days"))
        now = parse_time(ctx.now)
        invalid = invalidated(ctx.conn)
        for row in topological(ctx.conn):
            task_id, status, revision = (
                str(row["task_id"]),
                str(row["status"]),
                int(row["revision"]),
            )
            problems = dependency_problems(ctx.conn, task_id, invalid)
            if (
                status == "waiting_dependency"
                and not problems
                and ctx.conn.execute(
                    "SELECT 1 FROM dependencies WHERE task_id = ?", (task_id,)
                ).fetchone()
            ):
                drafts.append(
                    EventDraft(
                        type="dependency.satisfied",
                        task_id=task_id,
                        revision=revision,
                        data={"transition": "T05"},
                    )
                )
            elif status == "ready" and problems:
                drafts.append(
                    EventDraft(
                        type="dependency.invalidated",
                        task_id=task_id,
                        revision=revision,
                        data={"transition": "T06", "dependencies": problems},
                    )
                )
            elif status in OCCUPYING and problems:
                needs_stop.append({"task_id": task_id, "dependencies": problems})
            elif (
                status == "done"
                and problems
                and not int(row["stale"])
                and problems != loads(row["stale_ack"], None)
            ):
                request_id = next_item_id(ctx.conn, "input_requests", "request_id", "Q", task_id)
                drafts.append(
                    EventDraft(
                        type="input.requested",
                        task_id=task_id,
                        revision=revision,
                        data={
                            "request_id": request_id,
                            "kind": "stale",
                            "question": "上游任务已变化，该任务的成果需要保持还是重新验证？",
                            "binding": {"revision": revision, "dependencies": problems},
                        },
                    )
                )
            elif status == "accepted" and row["business_changed_at"]:
                quiet = now - parse_time(str(row["business_changed_at"])) >= timedelta(
                    days=archive_days
                )
                live = ctx.conn.execute(
                    "SELECT 1 FROM attempts WHERE task_id = ? AND phase = 'starting'",
                    (task_id,),
                ).fetchone()
                pending = ctx.conn.execute(
                    "SELECT 1 FROM input_requests WHERE task_id = ? AND status = 'pending'",
                    (task_id,),
                ).fetchone()
                # Follow-ups still to be decided keep the task out of the archive (DN002 §5.7).
                if (
                    quiet
                    and live is None
                    and pending is None
                    and not _follow_up.has_open(ctx.conn, task_id)
                ):
                    drafts.append(
                        EventDraft(
                            type="task.archived",
                            task_id=task_id,
                            revision=revision,
                            data={"transition": "T24", "reason": "auto_archive"},
                        )
                    )
        # In-session questions with no reply past their time go to the orchestrator (DN006 §8.3).
        drafts.extend(handed_off_event(due, "system") for due in handoff_due(ctx.conn, ctx.now))
        out["needs_stop"] = needs_stop
        return drafts

    return Command(actor=env.caller.actor(), input={"op": "sync", "at": "now"}, build=build)


def run_tool(
    ledger: Ledger,
    config: EffectiveConfig,
    caller: Caller,
    name: str,
    inp: Mapping[str, Any],
) -> dict[str, Any]:
    spec = SYNC if name == "sync" else REGISTRY.get(name)
    env = ToolEnv(config=config, caller=caller)
    report: dict[str, Any] = {}
    command = sync_command(env, report) if name == "sync" else spec.handler(inp, env)
    original = command.build

    def guarded(ctx: CommitContext) -> Any:
        check_caller(ctx, env, spec)
        check_version(ctx, inp)
        return original(ctx)

    command.build = guarded
    result = ledger.commit(command)
    out: dict[str, Any] = {
        "tool": name,
        "duplicate": result.duplicate,
        "events": [
            {"sequence": e.sequence, "type": e.type, "task_id": e.task_id, "data": dict(e.data)}
            for e in result.events
        ],
    }
    if name == "sync":
        out["needs_stop"] = report.get("needs_stop", [])
    elif spec.sync_after and not result.duplicate:
        # A worker's change runs the system rules as the system (DN002 §5.1a).
        runs_sync = caller if caller.role in SYNC.roles else Caller("system", None, caller.machine)
        out["sync"] = run_tool(ledger, config, runs_sync, "sync", {})
    return out


def tool_names() -> list[str]:
    return sorted([*REGISTRY.tools, "sync"])
