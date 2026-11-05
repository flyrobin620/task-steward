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
from tsw.machine import orchestrator as _orchestrator
from tsw.machine import worker as _worker
from tsw.machine.base import REGISTRY, Caller, Tool, ToolEnv, check_caller
from tsw.machine.guards import dependency_problems
from tsw.machine.model import OCCUPYING, loads

# Importing the tool modules registers their tools.
TOOL_MODULES = (_orchestrator, _worker)

SYNC = Tool(
    "sync", ("orchestrator", "system"), lambda inp, env: sync_command(env), description="系统规则"
)


def sync_command(env: ToolEnv, report: dict[str, Any] | None = None) -> Command:
    """Dependency propagation and retention: one event per task whose rule applies."""
    out = report if report is not None else {}

    def build(ctx: CommitContext) -> list[EventDraft]:
        drafts: list[EventDraft] = []
        needs_stop: list[dict[str, Any]] = []
        archive_days = int(env.config.get("policies.auto_archive_days"))
        now = parse_time(ctx.now)
        for row in ctx.conn.execute("SELECT * FROM tasks ORDER BY registered_seq").fetchall():
            task_id, status, revision = (
                str(row["task_id"]),
                str(row["status"]),
                int(row["revision"]),
            )
            problems = dependency_problems(ctx.conn, task_id)
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
                count = ctx.conn.execute(
                    "SELECT COUNT(*) AS n FROM input_requests WHERE task_id = ?", (task_id,)
                ).fetchone()["n"]
                drafts.append(
                    EventDraft(
                        type="input.requested",
                        task_id=task_id,
                        revision=revision,
                        data={
                            "request_id": f"Q-{task_id}-{int(count) + 1}",
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
                if quiet and live is None and pending is None:
                    drafts.append(
                        EventDraft(
                            type="task.archived",
                            task_id=task_id,
                            revision=revision,
                            data={"transition": "T24", "reason": "auto_archive"},
                        )
                    )
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
        out["sync"] = run_tool(ledger, config, caller, "sync", {})
    return out


def tool_names() -> list[str]:
    return sorted([*REGISTRY.tools, "sync"])
