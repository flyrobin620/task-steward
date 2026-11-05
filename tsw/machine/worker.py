# Tool inputs are untyped JSON checked field by field; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""Worker tools: progress, questions, the self-review loop (E01–E07, DN004) and submission (T13)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import SEVERITIES
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.machine import guards
from tsw.machine.base import (
    ToolEnv,
    flag,
    integer,
    items,
    mapping,
    opt_text,
    task_and_attempt,
    text,
    tool,
)
from tsw.machine.model import last_round_number, require_substate, review_cycle, task

CATEGORIES = (
    "correctness",
    "security",
    "requirement",
    "test",
    "docs",
    "maintainability",
    "performance",
)
VERDICTS = ("pass", "changes_requested", "blocked")


def _cmd(env: ToolEnv, op: str, inp: Mapping[str, Any], build: Any) -> Command:
    return Command(
        actor=env.caller.actor(),
        input={"op": op, **inp},
        build=build,
        command_id=opt_text(inp, "command_id"),
    )


def _item_id(ctx: CommitContext, task_id: str, prefix: str) -> str:
    count = ctx.conn.execute(
        "SELECT COUNT(*) AS n FROM judgments WHERE task_id = ?", (task_id,)
    ).fetchone()["n"]
    return f"{prefix}-{task_id}-{int(count) + 1}"


@tool(
    "progress_note", "worker", "orchestrator", lease=False, description="进展记录（不唤醒主会话）"
)
def progress_note(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    note = text(inp, "text")

    def build(ctx: CommitContext) -> list[EventDraft]:
        if env.caller.role == "worker":
            row, _ = task_and_attempt(ctx.conn, env, task_id)
        else:
            row = task(ctx.conn, task_id)
        return [
            EventDraft(
                type="worker.progress",
                task_id=task_id,
                revision=int(row["revision"]),
                data={"text": note},
            )
        ]

    return _cmd(env, "progress_note", inp, build)


@tool("question", "worker", description="worker 提问，交主会话判断")
def question(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    body = text(inp, "text")
    wants_human = flag(inp, "wants_human")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        if row["status"] != "in_progress":
            raise rejected("只有执行中才能提问", reason="invalid_state")
        return [
            EventDraft(
                type="worker.question",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=str(current["attempt"]),
                data={
                    "item_id": _item_id(ctx, task_id, "J"),
                    "text": body,
                    "wants_human": wants_human,
                },
            )
        ]

    return _cmd(env, "question", inp, build)


@tool("review_start", "worker", description="开始一轮评审（E01 / E05，R-ROUND）")
def review_start(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    head = text(inp, "head")
    reviewer = mapping(inp, "reviewer")
    assert reviewer is not None

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "executing", "revising")
        attempt_id, cycle_no = str(current["attempt"]), int(row["cycle"])
        cycle = review_cycle(ctx.conn, task_id, attempt_id, cycle_no)
        budget = guards.effective_budget(env.config, cycle)
        if int(cycle["rounds"]) >= budget:
            raise rejected(
                "本周期评审预算已用完，交主会话判断",
                reason="budget_exhausted",
                rounds=cycle["rounds"],
                budget=budget,
            )
        allowed = {
            str(env.config.get("roles.reviewer.session")),
            str(env.config.get("roles.reviewer.fallback_session") or ""),
        } - {""}
        if reviewer.get("session") not in allowed:
            raise rejected(
                "评审器必须是配置中的评审会话适配器",
                reason="reviewer_not_configured",
                allowed=sorted(allowed),
            )
        return [
            EventDraft(
                type="review.started",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={
                    "worker_transition": "E01" if row["substate"] == "executing" else "E05",
                    "attempt": attempt_id,
                    "cycle": cycle_no,
                    "round": last_round_number(ctx.conn, task_id, attempt_id) + 1,
                    "head": head,
                    "base": opt_text(inp, "base"),
                    "reviewer": {
                        "session": reviewer.get("session"),
                        "model": reviewer.get("model"),
                    },
                },
            )
        ]

    return _cmd(env, "review_start", inp, build)


@tool(
    "review_exhausted", "worker", description="评审预算用完，交主会话判断（executing / revising）"
)
def review_exhausted(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    suggestion = text(inp, "suggestion")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "executing", "revising")
        attempt_id, cycle_no = str(current["attempt"]), int(row["cycle"])
        cycle = review_cycle(ctx.conn, task_id, attempt_id, cycle_no)
        if int(cycle["rounds"]) < guards.effective_budget(env.config, cycle):
            raise rejected("预算尚未用完", reason="budget_available")
        return [
            EventDraft(
                type="review.budget_exhausted",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={
                    "item_id": _item_id(ctx, task_id, "J"),
                    "attempt": attempt_id,
                    "cycle": cycle_no,
                    "round": cycle["last_round"],
                    "unresolved": guards.blocking_unresolved(
                        ctx.conn, task_id, attempt_id, cycle_no
                    ),
                    "suggestion": suggestion,
                },
            )
        ]

    return _cmd(env, "review_exhausted", inp, build)


def _current_round(
    ctx: CommitContext, row: Mapping[str, Any], attempt_id: str, round_no: int
) -> None:
    cycle = review_cycle(ctx.conn, str(row["task_id"]), attempt_id, int(row["cycle"]))
    if cycle["last_round"] != round_no:
        raise rejected(
            "只能处理本周期的最后一轮", reason="stale_round", last_round=cycle["last_round"]
        )


@tool("review_fail", "worker", description="评审调用失败（E02 重试或 review.unavailable）")
def review_fail(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    round_no = integer(inp, "round", minimum=1)
    error = text(inp, "error")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "reviewing")
        attempt_id = str(current["attempt"])
        _current_round(ctx, row, attempt_id, round_no)
        cycle = review_cycle(ctx.conn, task_id, attempt_id, int(row["cycle"]))
        data = {
            "attempt": attempt_id,
            "cycle": int(row["cycle"]),
            "round": round_no,
            "error": error,
        }
        if int(cycle["retries"]) < int(env.config.get("review.retry_limit")):
            return [
                EventDraft(
                    type="review.failed",
                    task_id=task_id,
                    revision=int(row["revision"]),
                    attempt=attempt_id,
                    data=data,
                )
            ]
        return [
            EventDraft(
                type="review.unavailable",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={**data, "item_id": _item_id(ctx, task_id, "J")},
            )
        ]

    return _cmd(env, "review_fail", inp, build)


def validate_result(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The reviewer output contract (DN004 §4)."""
    if result.get("verdict") not in VERDICTS:
        raise rejected("评审结果的 verdict 无效", reason="review_schema", field="verdict")
    for key in ("summary", "reviewed_head"):
        if not isinstance(result.get(key), str) or not str(result[key]).strip():
            raise rejected("评审结果缺少字段", reason="review_schema", field=key)
    findings = result.get("findings")
    if not isinstance(findings, list):
        raise rejected("评审结果的 findings 应为数组", reason="review_schema", field="findings")
    seen: set[str] = set()
    checked: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise rejected("评审意见应为对象", reason="review_schema")
        fid, severity = finding.get("id"), finding.get("severity")
        if not isinstance(fid, str) or fid in seen:
            raise rejected("评审意见 id 缺失或重复", reason="review_schema", id=fid)
        if severity not in SEVERITIES or finding.get("category") not in CATEGORIES:
            raise rejected("评审意见的严重级别或类别无效", reason="review_schema", id=fid)
        for key in ("claim", "evidence"):
            if not isinstance(finding.get(key), str):
                raise rejected("评审意见缺少字段", reason="review_schema", id=fid, field=key)
        seen.add(fid)
        checked.append({"id": fid, "severity": severity})
    return checked


@tool("review_complete", "worker", description="评审完成（E03），结果须满足输出契约")
def review_complete(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    round_no = integer(inp, "round", minimum=1)
    result = mapping(inp, "result")
    assert result is not None
    result_path = text(inp, "result_path")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "reviewing")
        attempt_id = str(current["attempt"])
        _current_round(ctx, row, attempt_id, round_no)
        findings = validate_result(result)
        block_on = set(env.config.get("review.block_on") or [])
        return [
            EventDraft(
                type="review.completed",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={
                    "attempt": attempt_id,
                    "cycle": int(row["cycle"]),
                    "round": round_no,
                    "verdict": result["verdict"],
                    "reviewed_head": result["reviewed_head"],
                    "findings": [{**f, "blocking": f["severity"] in block_on} for f in findings],
                    "result_path": result_path,
                },
            )
        ]

    return _cmd(env, "review_complete", inp, build)


@tool("review_triage", "worker", description="逐条处置评审意见")
def review_triage(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    round_no = integer(inp, "round", minimum=1)
    entries = items(inp, "items")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "triaging")
        attempt_id = str(current["attempt"])
        _current_round(ctx, row, attempt_id, round_no)
        findings = {
            str(r["finding_id"]): bool(r["blocking"])
            for r in ctx.conn.execute(
                "SELECT finding_id, blocking FROM findings WHERE task_id = ? AND attempt = ? AND "
                "round = ?",
                (task_id, attempt_id, round_no),
            )
        }
        handled: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise rejected("处置应为对象", reason="invalid_input", field="items")
            fid, action = entry.get("finding"), entry.get("action")
            if fid not in findings:
                raise rejected("本轮没有这条意见", reason="finding_not_found", finding=fid)
            allowed = ("fix", "reject") if findings[str(fid)] else ("fix", "reject", "defer")
            if action not in allowed:
                raise rejected(
                    "阻断项只能 fix 或 reject，延后须提交 defer_requested",
                    reason="invalid_action",
                    finding=fid,
                )
            reason = entry.get("reason")
            if action in ("reject", "defer") and not (isinstance(reason, str) and reason.strip()):
                raise rejected("reject 与 defer 必须写理由", reason="reason_required", finding=fid)
            handled.append(
                {
                    "finding": fid,
                    "action": action,
                    "reason": reason,
                    "verification": entry.get("verification"),
                }
            )
        missing = sorted(set(findings) - {str(h["finding"]) for h in handled})
        if missing:
            raise rejected("必须处置本轮全部意见", reason="triage_incomplete", missing=missing)
        return [
            EventDraft(
                type="review.triaged",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={"attempt": attempt_id, "round": round_no, "items": handled},
            )
        ]

    return _cmd(env, "review_triage", inp, build)


@tool("review_decide", "worker", description="评审决定：iterate（E04）/ finalize / escalate")
def review_decide(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    round_no = integer(inp, "round", minimum=1)
    decision = text(inp, "decision")
    head = text(inp, "head")
    if decision not in ("iterate", "finalize", "escalate"):
        raise rejected("decision 必须是 iterate、finalize 或 escalate", reason="invalid_input")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "triaging")
        attempt_id, cycle_no = str(current["attempt"]), int(row["cycle"])
        _current_round(ctx, row, attempt_id, round_no)
        cycle = review_cycle(ctx.conn, task_id, attempt_id, cycle_no)
        unresolved = guards.blocking_unresolved(ctx.conn, task_id, attempt_id, cycle_no)
        needs_fix = ctx.conn.execute(
            "SELECT finding_id FROM findings WHERE task_id = ? AND attempt = ? AND round = ? "
            "AND blocking = 1 AND action = 'fix'",
            (task_id, attempt_id, round_no),
        ).fetchall()
        revision = int(row["revision"])
        base = {"attempt": attempt_id, "cycle": cycle_no, "round": round_no, "decision": decision}
        if decision == "iterate":
            if not needs_fix:
                raise rejected("没有需要修复的阻断项", reason="nothing_to_fix")
            if int(cycle["rounds"]) >= guards.effective_budget(env.config, cycle):
                raise rejected("本周期评审预算已用完，只能 escalate", reason="budget_exhausted")
            return [
                EventDraft(
                    type="review.decision",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt_id,
                    data=base,
                )
            ]
        if decision == "finalize":
            if unresolved:
                raise rejected(
                    "仍有未处置的阻断项", reason="blocking_unresolved", findings=unresolved
                )
            last = ctx.conn.execute(
                "SELECT verdict FROM review_rounds WHERE task_id = ? AND attempt = ? AND round = ?",
                (task_id, attempt_id, round_no),
            ).fetchone()
            if last is not None and last["verdict"] == "blocked":
                raise rejected(
                    "评审器因缺材料无法评审（blocked），补齐后用 self_check_failed 重跑",
                    reason="blocked_review",
                )
            if cycle["last_reviewed_head"] != head:
                raise rejected(
                    "当前 HEAD 晚于最后评审的 HEAD，须对差异再评审（E06）",
                    reason="head_not_reviewed",
                    reviewed=cycle["last_reviewed_head"],
                    head=head,
                )
            return [
                EventDraft(
                    type="review.decision",
                    task_id=task_id,
                    revision=revision,
                    attempt=attempt_id,
                    data=base,
                )
            ]
        kind = text(inp, "escalate_kind")
        if kind not in ("budget_exhausted", "defer_requested"):
            raise rejected(
                "escalate_kind 必须是 budget_exhausted 或 defer_requested", reason="invalid_input"
            )
        if kind == "budget_exhausted" and int(cycle["rounds"]) < guards.effective_budget(
            env.config, cycle
        ):
            raise rejected("预算尚未用完", reason="budget_available")
        payload = {
            "item_id": _item_id(ctx, task_id, "J"),
            "attempt": attempt_id,
            "cycle": cycle_no,
            "round": round_no,
            "unresolved": unresolved,
            "suggestion": text(inp, "suggestion"),
        }
        return [
            EventDraft(
                type="review.decision",
                task_id=task_id,
                revision=revision,
                attempt=attempt_id,
                data=base,
            ),
            EventDraft(
                type=f"review.{kind}",
                task_id=task_id,
                revision=revision,
                attempt=attempt_id,
                data=payload,
            ),
        ]

    return _cmd(env, "review_decide", inp, build)


@tool("self_check_failed", "worker", description="自查发现遗漏或定稿后 HEAD 变化（E06）")
def self_check_failed(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    reason = text(inp, "reason")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "triaging")
        return [
            EventDraft(
                type="worker.self_check_failed",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=str(current["attempt"]),
                data={"reason": reason},
            )
        ]

    return _cmd(env, "self_check_failed", inp, build)


def _draft_path(task_id: str, attempt_id: str, rel: str) -> str:
    path = PurePosixPath(rel.replace("\\", "/"))
    prefix = PurePosixPath("work", task_id, attempt_id)
    if path.is_absolute() or ".." in path.parts or path.parts[: len(prefix.parts)] != prefix.parts:
        raise rejected(
            "报告草稿必须在本 attempt 的 work 目录中", reason="invalid_report_path", path=rel
        )
    return str(path)


@tool("submit", "worker", description="提交终版（T13，G-SUBMIT）")
def submit(inp: Mapping[str, Any], env: ToolEnv) -> Command:
    task_id = text(inp, "task_id")
    head = text(inp, "head")
    report_md = text(inp, "report_md")
    report_html = text(inp, "report_html")
    worktree_clean = flag(inp, "worktree_clean")
    diff_note = opt_text(inp, "diff_note")

    def build(ctx: CommitContext) -> list[EventDraft]:
        row, current = task_and_attempt(ctx.conn, env, task_id)
        require_substate(row, "triaging")
        attempt_id = str(current["attempt"])
        if not worktree_clean and not (diff_note and diff_note.strip()):
            raise rejected("工作树不干净时必须说明差异", reason="g_submit", check="worktree")
        evidence = guards.g_submit(ctx.conn, env.config, row, attempt_id, head)
        copied: dict[str, str] = {}
        for key, rel, name in (
            ("md", report_md, "result.md"),
            ("html", report_html, "result.html"),
        ):
            source = ctx.home.root / _draft_path(task_id, attempt_id, rel)
            if not source.is_file():
                raise rejected("报告文件不存在", reason="g_submit", check=f"report_{key}", path=rel)
            target = f"runs/{task_id}/{attempt_id}/{name}"
            copied[key] = ctx.storage.put_artifact(target, source.read_bytes())
            copied[f"{key}_path"] = target
        dependencies = {
            str(r["dep_id"]): int(r["bound_revision"])
            for r in ctx.conn.execute(
                "SELECT dep_id, bound_revision FROM dependencies WHERE task_id = ?", (task_id,)
            )
        }
        submission = {
            "revision": int(row["revision"]),
            "cycle": evidence["cycle"],
            "head": head,
            "rounds": evidence["rounds"],
            "report": copied,
            "dependencies": dependencies,
            "worktree_clean": worktree_clean,
            "diff_note": diff_note,
        }
        return [
            EventDraft(
                type="worker.submitted",
                task_id=task_id,
                revision=int(row["revision"]),
                attempt=attempt_id,
                data={"transition": "T13", "submission": submission},
            )
        ]

    return _cmd(env, "submit", inp, build)
