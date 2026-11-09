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
FINDINGS: dict[str, Any] = {
    "type": "array",
    "items": {"type": "object", "properties": {"round": INT, "finding": S}},
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
        workdir=S,
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
        post_merge={
            **S,
            "description": "worker 已合并时必填：对 worker 所附合并证据（合并提交、CI 链接、live）"
            "的核对结论",
        },
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
        merge={
            "type": "object",
            "description": "worker 已合并 PR（仓库授权 delivery.merge）：合并记录、主分支上合并提交"
            "的 CI 结果与 live 说明",
            "properties": {
                "pr": S,
                "merge_commit": S,
                "merged_head": S,
                "ci": {
                    "type": "object",
                    "description": "主分支上合并提交的 CI 运行：commit 为合并提交，conclusion 为"
                    " success，url 为运行链接",
                    "properties": {"commit": S, "conclusion": S, "url": S},
                },
                "live": {
                    **S,
                    "description": "在最终 head 与（需要时）合并后的主分支上运行的 live 场景"
                    "与结果，或不需要、不能运行的原因",
                },
            },
        },
        merge_skipped={**S, "description": "授权合并但没有合并时的原因（任务书或仓库规则）"},
    ),
    "sync": schema([]),
    "ask_user": schema(
        ["task_id", "question"],
        task_id=S,
        question={**S, "description": "问题全文；随后在本会话的回复里原样写给用户"},
        items={**STRS, "description": "同时交给用户的待处理事项（review_exhausted 等记录的编号）"},
        reserved={**B, "description": "只有用户本人能决定（凭据、验收等）：转交后主会话也不能代答"},
    ),
    "user_answer": schema(
        ["task_id", "request_id", "message_id", "answer"],
        task_id=S,
        request_id=S,
        message_id={**S, "description": "hook 记录的用户消息编号（M-…），提示里会给出"},
        answer={**S, "description": "按用户原话整理的决定"},
        extend_rounds={**INT, "description": "用户批准追加的评审轮次"},
        defer_findings=FINDINGS,
        overrule_findings=FINDINGS,
    ),
    "input_handoff": schema(["task_id", "request_id"], task_id=S, request_id=S),
    "handoff_resolve": schema(
        ["task_id", "request_id", "decision", "answer", "rationale", "basis", "confidence"],
        task_id=S,
        request_id=S,
        decision={"enum": ["answer", "extend", "overrule", "defer"]},
        answer=S,
        rationale=S,
        basis=STRS,
        confidence={"enum": ["high", "medium", "low"]},
        extend_rounds=INT,
        defer_findings=FINDINGS,
        overrule_findings=FINDINGS,
    ),
    "input_relay": schema(
        ["task_id", "request_id", "channel"],
        task_id=S,
        request_id=S,
        channel={**S, "description": "main-session 或已加载的其他人类渠道（如 teams-chat）"},
        note=S,
    ),
    "routine_start": schema(
        ["user_turn"],
        user_turn={**B, "description": "这一轮是不是用户本人的 turn（只有它能创建会话）"},
    ),
    "routine_bind": schema(
        ["generation", "session_id"],
        generation={**INT, "description": "routine_start 返回的代次"},
        session_id={**S, "description": "start_session 返回的会话 ID"},
    ),
    "routine_armed": schema(
        ["job_ref", "cron", "interval_minutes"],
        job_ref={**S, "description": "宿主定时工具返回的任务引用（如 CronCreate 的 job ID）"},
        cron=S,
        interval_minutes=INT,
    ),
    "routine_run": schema(
        ["job_id"],
        job_id={**S, "description": "routine_board 列出的例行项"},
        chat_id={**S, "description": "Teams 例行项：取自该行 run 中的 chat_id"},
        fetched={"description": "这一行 fetch 要求取的原始数据（宿主工具的返回值，原样）"},
    ),
    "channel_sent": schema(
        ["task_id", "request_id", "chat_id", "message_id"],
        task_id=S,
        request_id=S,
        chat_id={**S, "description": "卡片所在的 Teams chat ID（发送结果中的 chatId）"},
        message_id={**S, "description": "卡片的 Teams 消息 ID"},
        sender_id={**S, "description": "发送者（用户本人）在 Teams 中的 ID，发送结果中有时给出"},
    ),
    "config_change": schema(
        ["ops", "reason"],
        ops={
            "type": "array",
            "description": "按顺序应用：set（空路径表示替换整个文件）/ unset；路径用点号，"
            "键名含点号时用字符串数组",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"enum": ["set", "unset"]},
                    "path": {"description": "如 repositories.app.delivery.push"},
                    "value": {"description": "set 的新值（任意 JSON）"},
                },
                "required": ["op", "path"],
            },
        },
        reason=S,
        user_source={
            **SOURCE,
            "description": "改动权限类键时必填：用户原话；target 为 "
            '["config", 修改前文件的 sha256（config_show 的 file_sha256）, "authorize"]',
        },
        expected_sha256={**S, "description": "修改前文件的摘要；不一致时拒绝"},
    ),
    "config_revert": schema(
        ["to_change", "reason"],
        to_change={**S, "description": "恢复到这次修改（如 C003）之前的内容"},
        reason=S,
        user_source={**SOURCE, "description": "恢复涉及权限类键时必填，target 同 config_change"},
        expected_sha256=S,
    ),
}
