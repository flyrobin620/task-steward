"""JSON Schemas of the tool inputs, published by the MCP server (``tools/list``).

The tools validate their input themselves; these schemas tell the model what to send.
"""

from __future__ import annotations

from typing import Any

S: dict[str, Any] = {"type": "string"}
INT: dict[str, Any] = {"type": "integer"}
B: dict[str, Any] = {"type": "boolean"}
OBJ: dict[str, Any] = {"type": "object"}
STRS: dict[str, Any] = {"type": "array", "items": S}
SOURCE: dict[str, Any] = {
    "type": "object",
    "description": "VerifiedSource：用户本人的来源（DN006 §8.5）",
    "properties": {
        "channel": S,
        "principal": S,
        "message_ref": S,
        "text": S,
        "request_id": S,
        "target": {"type": "array", "description": "[任务 ID, revision, 决定类别]"},
    },
    "required": ["channel", "principal", "message_ref", "text", "target"],
}
COMMON: dict[str, Any] = {
    "command_id": {**S, "description": "幂等键：同一 ID 重复调用返回原结果"},
    "expected_version": {**INT, "description": "乐观锁：任务版本不一致时拒绝"},
}


def schema(required: list[str], **properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {**properties, **COMMON},
        "required": required,
    }


SCHEMAS: dict[str, dict[str, Any]] = {
    "task_register": schema(
        ["title", "repo", "kind", "goal", "acceptance"],
        task_id=S,
        title=S,
        repo=S,
        kind={"enum": ["research", "design", "code"]},
        goal=S,
        acceptance=STRS,
    ),
    "task_plan": schema(
        ["task_id", "reason"],
        task_id=S,
        dependencies={
            "type": "array",
            "items": {"type": "object", "properties": {"task_id": S, "reason": S, "kind": S}},
        },
        resources=STRS,
        reason=S,
    ),
    "intake_question": schema(["task_id", "question"], task_id=S, question=S),
    "input_resolve": schema(
        ["task_id", "request_id", "answer", "user_source"],
        task_id=S,
        request_id=S,
        answer=S,
        user_source=SOURCE,
        extend_rounds=INT,
    ),
    "task_update": schema(
        ["task_id", "goal", "acceptance", "user_source"],
        task_id=S,
        goal=S,
        acceptance=STRS,
        user_source=SOURCE,
    ),
    "dependency_rebind": schema(
        ["task_id", "dep_id", "revalidate_scope"], task_id=S, dep_id=S, revalidate_scope=S
    ),
    "stale_keep": schema(["task_id", "user_source"], task_id=S, user_source=SOURCE),
    "task_dispatch": schema(
        ["task_id", "workdir", "user_turn"],
        task_id=S,
        workdir=S,
        branch=S,
        user_turn=B,
        read_set=STRS,
    ),
    "attempt_start": schema(
        ["task_id", "purpose", "user_turn"],
        task_id=S,
        purpose={"enum": ["dispatch", "prestart"]},
        user_turn=B,
    ),
    "attempt_reconcile": schema(
        ["task_id", "attempt", "generation", "outcome"],
        task_id=S,
        attempt=S,
        generation=INT,
        outcome={"enum": ["created", "not_created"]},
        session_ref=S,
        receipt=OBJ,
        brief_delivered=B,
    ),
    "attempt_deliver": schema(["task_id"], task_id=S),
    "attempt_continue": schema(["task_id"], task_id=S, read_set=STRS),
    "attempt_update": schema(
        ["task_id", "attempt", "event", "evidence"],
        task_id=S,
        attempt=S,
        event={"enum": ["stopped", "replaced", "closed", "lost"]},
        evidence=S,
    ),
    "task_pause": schema(
        ["task_id", "category", "owner", "next_action", "resume_condition"],
        task_id=S,
        category=S,
        owner=S,
        next_action=S,
        resume_condition=S,
        worker_stopped=B,
    ),
    "task_resume": schema(["task_id", "condition_met", "note"], task_id=S, condition_met=B, note=S),
    "judge": schema(
        ["task_id", "decision", "rationale", "basis", "confidence", "items"],
        task_id=S,
        decision={"enum": ["answer", "extend", "overrule", "defer", "feedback", "escalate"]},
        rationale=S,
        basis=STRS,
        confidence={"enum": ["high", "medium", "low"]},
        items=STRS,
        answer=S,
        rounds=INT,
        feedback=S,
        question=S,
        findings={
            "type": "array",
            "items": {"type": "object", "properties": {"round": INT, "finding": S}},
        },
    ),
    "feedback": schema(["task_id", "items"], task_id=S, items=STRS),
    "integrate": schema(
        ["task_id", "repo", "status"],
        task_id=S,
        repo=S,
        status={"enum": ["started", "conflict", "completed", "withdrawn"]},
        detail=S,
    ),
    "output_link": schema(
        ["task_id", "output_id", "type", "ref", "evidence"],
        task_id=S,
        output_id=S,
        type=S,
        ref=S,
        role=S,
        delivery=S,
        evidence=STRS,
        verified_at=S,
    ),
    "task_accept": schema(
        ["task_id", "verified_head", "summary", "acceptance"],
        task_id=S,
        verified_head=S,
        summary=S,
        acceptance={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": S,
                    "status": {"enum": ["done", "replaced_by_user"]},
                    "evidence": S,
                    "user_source": SOURCE,
                },
            },
        },
    ),
    "user_accept": schema(["task_id", "user_source"], task_id=S, user_source=SOURCE),
    "user_reject": schema(
        ["task_id", "user_source", "reason"], task_id=S, user_source=SOURCE, reason=S
    ),
    "task_cancel": schema(
        ["task_id", "user_source"], task_id=S, user_source=SOURCE, worker_stopped=B
    ),
    "task_archive": schema(["task_id", "user_source"], task_id=S, user_source=SOURCE),
    "dependency_invalidate": schema(["task_id"], task_id=S, worker_stopped=B),
    "resource_release": schema(
        ["task_id", "resources", "evidence"], task_id=S, resources=STRS, evidence=S
    ),
    "progress_note": schema(["task_id", "text"], task_id=S, text=S),
    "question": schema(["task_id", "text"], task_id=S, text=S, wants_human=B),
    "review_start": schema(
        ["task_id", "head", "reviewer"],
        task_id=S,
        head=S,
        base=S,
        reviewer={"type": "object", "properties": {"session": S, "model": S}},
    ),
    "review_fail": schema(["task_id", "round", "error"], task_id=S, round=INT, error=S),
    "review_complete": schema(
        ["task_id", "round", "result", "result_path"],
        task_id=S,
        round=INT,
        result=OBJ,
        result_path=S,
    ),
    "review_triage": schema(
        ["task_id", "round", "items"],
        task_id=S,
        round=INT,
        items={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": S,
                    "action": {"enum": ["fix", "reject", "defer"]},
                    "reason": S,
                    "verification": S,
                },
            },
        },
    ),
    "review_decide": schema(
        ["task_id", "round", "decision", "head"],
        task_id=S,
        round=INT,
        decision={"enum": ["iterate", "finalize", "escalate"]},
        head=S,
        escalate_kind={"enum": ["budget_exhausted", "defer_requested"]},
        suggestion=S,
    ),
    "review_exhausted": schema(["task_id", "suggestion"], task_id=S, suggestion=S),
    "self_check_failed": schema(["task_id", "reason"], task_id=S, reason=S),
    "submit": schema(
        ["task_id", "head", "report_md", "report_html", "worktree_clean"],
        task_id=S,
        head=S,
        report_md=S,
        report_html=S,
        worktree_clean=B,
        diff_note=S,
    ),
    "sync": schema([]),
}
