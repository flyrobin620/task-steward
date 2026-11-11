"""The STATUS app's view of one task: ``/tasks/<task>`` (DN006 §8.10).

A read-only page with more than a card holds: the requirement and its revisions, every attempt
with its session, the submission and the review rounds, the outputs, dependencies and resources,
the questions and judgments, the progress notes and the task's ledger events. Its links are
relative (``../`` back to the board, ``<task>`` to a dependency, ``../runs/...`` to a report), so
the page keeps working under a path prefix (DN006 §8.10). It shares the board's head: the same
Content-Security-Policy, token and scripts, so the accept, reject and cancel buttons behave as on
the card.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from tsw.events import Event
from tsw.host.status import (
    OUTPUT_TYPES,
    PR_TYPES,
    AppView,
    Revision,
    actions_html,
    brief_html,
    card_notes,
    escape,
    link_for,
    page_head,
    pending_asks,
    repo_hue,
    session_link,
)
from tsw.machine.guards import invalidated
from tsw.machine.model import loads
from tsw.tasks import KINDS, STATUS_LABELS

# The newest events shown on the timeline; older ones stay in the ledger.
TIMELINE_LIMIT = 200
NOTES_LIMIT = 50
FOOTER = (
    "本页由 STATUS 应用（tsw status serve）按账本实时生成；按钮直接写入账本，记为你本人的操作。"
)
REQUEST_STATUS = {"pending": "待答", "resolved": "已答", "superseded": "已取代"}


@dataclass(frozen=True)
class Step:
    """One ledger event of a task, as the timeline shows it."""

    sequence: int
    time: str
    type: str
    attempt: str | None
    transition: str | None


def timeline(events: Iterable[Event]) -> dict[str, list[Step]]:
    """Every task's ledger events, oldest first; a renumbered task keeps its earlier events."""
    out: dict[str, list[Step]] = {}
    for event in events:
        task_id = event.task_id
        if task_id is None:
            continue
        if event.type == "task.renumbered":
            moved = out.pop(str(event.data["from"]), [])
            out[str(event.data["to"])] = moved
            task_id = str(event.data["to"])
        transition = event.data.get("transition")
        out.setdefault(task_id, []).append(
            Step(
                event.sequence,
                event.time,
                event.type,
                event.attempt,
                str(transition) if transition else None,
            )
        )
    return out


def _attempt_key(attempt: str) -> tuple[int, str]:
    digits = attempt[1:]
    return (int(digits) if digits.isdigit() else 0, attempt)


def _time(value: Any) -> str:
    if not value:
        return "—"
    return f'<time datetime="{escape(value)}">{escape(value)}</time>'


def _link(ref: Any, label: Any = None) -> str:
    """A link for a reference when it is a location, else the text itself."""
    if not ref:
        return "—"
    href = link_for(str(ref))
    text = escape(label if label is not None else ref)
    return f'<a href="{escape(href)}">{text}</a>' if href else text


def _facts(items: Iterable[tuple[str, str]]) -> str:
    rows = "".join(f"<div><dt>{escape(k)}</dt><dd>{v}</dd></div>" for k, v in items if v)
    return f'<dl class="facts wide">{rows}</dl>'


def _table(head: Iterable[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="none">无</p>'
    cols = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return (
        f'<div class="tbl"><table><thead><tr>{cols}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def _section(key: str, title: str, body: str, count: int | None = None) -> str:
    n = f'<span class="n">{count}</span>' if count is not None else ""
    return f'<section class="panel" id="{escape(key)}"><h2>{escape(title)}{n}</h2>{body}</section>'


def _code(value: Any) -> str:
    return f"<code>{escape(value)}</code>" if value else "—"


def _task_link(task_id: str) -> str:
    """A dependency's view, relative to this one (``/tasks/<this>`` → ``/tasks/<other>``)."""
    return f'<a href="{escape(quote(task_id, safe=""))}">{escape(task_id)}</a>'


def _overview(row: Mapping[str, Any], imported: Mapping[str, Any] | None) -> str:
    status = str(row["status"])
    items = [
        ("状态", escape(STATUS_LABELS.get(status, status))),
        ("子状态", escape(row["substate"] or "")),
        ("之前的状态", escape(STATUS_LABELS.get(row["previous_status"] or "", "") or "")),
        ("revision", escape(row["revision"])),
        ("等待的 revision", escape(row["waiting_revision"] or "")),
        ("当前 attempt", escape(row["current_attempt"] or "—")),
        ("attempt 数", escape(row["attempt_count"])),
        ("评审周期", escape(row["cycle"])),
        ("登记序号", escape(row["registered_seq"])),
        ("最近记录", _time(row["last_activity"])),
        ("完成", _time(row["done_at"]) if row["done_at"] else ""),
        ("验收", _time(row["accepted_at"]) if row["accepted_at"] else ""),
        ("任务文件", _code(row["spec_path"])),
    ]
    if imported:
        items.append(("导入自", escape(f"{imported['source']}（原状态 {imported['old_status']}）")))
        legacy = str(imported["legacy"] or "")
        if legacy:
            items.append(("旧系统成果", _link(legacy)))
    return _facts(items)


def _revisions(row: Mapping[str, Any], past: list[Revision]) -> str:
    rows = [
        [
            escape(r.revision) + (" <small>当前</small>" if r.revision == row["revision"] else ""),
            escape(r.how),
            _time(r.time),
            f'<span class="clip">{escape(r.words)}</span>' if r.words else "—",
        ]
        for r in reversed(past)
    ]
    return _table(("revision", "来源", "时间", "用户原话 / 理由"), rows)


def _attempts(task_id: str, attempts: list[dict[str, Any]]) -> str:
    rows: list[list[str]] = []
    for a in attempts:
        brief = a.get("brief_path")
        brief_cell = _link(brief, f"brief.md（revision {a.get('brief_revision') or '—'}）")
        rows.append(
            [
                escape(a["attempt"]),
                escape(a["phase"]),
                session_link(task_id, a) or "—",
                escape(a.get("adapter") or "—"),
                _code(a.get("workdir")),
                _code(a.get("branch")),
                brief_cell if brief else "—",
                escape(a.get("purpose") or "—"),
            ]
        )
    return _table(("attempt", "阶段", "会话", "适配器", "执行目录", "分支", "任务书", "用途"), rows)


def _submission(row: Mapping[str, Any]) -> str:
    sub: dict[str, Any] = loads(row["submission"], None) or {}
    if not sub:
        return '<p class="none">当前没有提交</p>'
    report: dict[str, Any] = sub.get("report") or {}
    links = " ".join(
        _link(report[key], label)
        for key, label in (("html_path", "报告"), ("md_path", "报告（md）"))
        if report.get(key)
    )
    items = [
        ("revision", escape(sub.get("revision", ""))),
        ("评审周期", escape(sub.get("cycle", ""))),
        ("head", _code(sub.get("head"))),
        ("评审轮数", escape(sub.get("rounds", ""))),
        ("报告", links),
        ("工作树干净", "是" if sub.get("worktree_clean") else "否"),
        ("差异说明", escape(sub.get("diff_note") or "")),
    ]
    merge: dict[str, Any] = sub.get("merge") or {}
    if merge:
        ci: dict[str, Any] = merge.get("ci") or {}
        ci_text = escape(ci.get("conclusion") or "")
        if ci.get("url"):
            ci_text = _link(ci["url"], ci.get("conclusion") or "CI")
        items += [
            ("PR", _link(merge.get("pr")) if merge.get("pr") else ""),
            ("合并提交", _code(merge.get("merge_commit"))),
            ("主分支 CI", ci_text),
            ("live", escape(merge.get("live") or "")),
        ]
    if sub.get("merge_skipped"):
        items.append(("未合并的原因", escape(sub["merge_skipped"])))
    return _facts(items)


def _reviews(conn: sqlite3.Connection, task_id: str) -> tuple[str, int]:
    cycles = [
        [
            escape(r["attempt"]),
            escape(r["cycle"]),
            escape(r["rounds"]),
            escape(f"主会话 {r['orchestrator_extension']} · 用户 {r['user_extension']}"),
            escape(r["last_decision"] or "—"),
            _code(r["last_reviewed_head"]),
        ]
        for r in conn.execute(
            "SELECT * FROM review_cycles WHERE task_id = ? ORDER BY attempt, cycle", (task_id,)
        )
    ]
    counts = {
        (str(r["attempt"]), int(r["round"])): (int(r["n"]), int(r["blocking"] or 0))
        for r in conn.execute(
            "SELECT attempt, round, COUNT(*) AS n, SUM(blocking) AS blocking FROM findings "
            "WHERE task_id = ? GROUP BY attempt, round",
            (task_id,),
        )
    }
    rounds: list[list[str]] = []
    for r in conn.execute(
        "SELECT * FROM review_rounds WHERE task_id = ? ORDER BY attempt, round", (task_id,)
    ):
        n, blocking = counts.get((str(r["attempt"]), int(r["round"])), (0, 0))
        rounds.append(
            [
                escape(r["attempt"]),
                escape(r["round"]),
                escape(r["cycle"]),
                escape(r["reviewer"]),
                _code(r["head"]),
                escape(r["verdict"] or "—"),
                escape(r["decision"] or "—"),
                escape(f"{n}（阻塞 {blocking}）"),
            ]
        )
    body = (
        "<h3>评审周期</h3>"
        + _table(("attempt", "周期", "轮数", "延长", "最后的决定", "最后评审的 head"), cycles)
        + "<h3>评审轮次</h3>"
        + _table(("attempt", "轮次", "周期", "评审器", "head", "结论", "决定", "意见"), rounds)
    )
    return body, len(rounds)


def _outputs(conn: sqlite3.Connection, task_id: str) -> tuple[str, int]:
    rows = [
        [
            escape(OUTPUT_TYPES.get(str(r["type"]), r["type"])),
            _link(r["ref"], r["output_id"] if r["type"] not in PR_TYPES else r["ref"]),
            escape(r["role"] or "—"),
            escape(r["delivery"] or "—"),
            _time(r["verified_at"]),
        ]
        for r in conn.execute(
            "SELECT * FROM outputs WHERE task_id = ? "
            "ORDER BY type NOT IN ('pr', 'pull_request'), output_id",
            (task_id,),
        )
    ]
    return _table(("类型", "产出", "角色", "交付", "核实时间"), rows), len(rows)


def _relations(conn: sqlite3.Connection, row: Mapping[str, Any]) -> str:
    task_id = str(row["task_id"])
    deps = [
        [
            _task_link(str(r["dep_id"])),
            escape(r["kind"]),
            escape(r["bound_revision"]),
            escape(r["reason"]),
        ]
        for r in conn.execute(
            "SELECT * FROM dependencies WHERE task_id = ? ORDER BY dep_id", (task_id,)
        )
    ]
    users = [
        [_task_link(str(r["task_id"])), escape(r["kind"]), escape(r["bound_revision"])]
        for r in conn.execute(
            "SELECT * FROM dependencies WHERE dep_id = ? ORDER BY task_id", (task_id,)
        )
    ]
    declared: list[Any] = loads(row["resources"], []) or []
    claims = {
        str(r["resource"]): str(r["status"])
        for r in conn.execute(
            "SELECT resource, status FROM resource_claims WHERE owner_task = ?", (task_id,)
        )
    }
    names = [str(x) for x in declared] + sorted(set(claims) - {str(x) for x in declared})
    resources = [[_code(n), escape(claims.get(n, "未占用"))] for n in names]
    integrations = [
        [escape(r["repo"]), escape(r["status"])]
        for r in conn.execute(
            "SELECT repo, status FROM integrations WHERE task_id = ? ORDER BY repo", (task_id,)
        )
    ]
    return (
        "<h3>依赖</h3>"
        + _table(("任务", "类型", "绑定的 revision", "理由"), deps)
        + "<h3>依赖本任务的任务</h3>"
        + _table(("任务", "类型", "绑定的 revision"), users)
        + "<h3>资源</h3>"
        + _table(("资源", "占用"), resources)
        + "<h3>集成</h3>"
        + _table(("仓库", "状态"), integrations)
    )


def _answer(resolution: str | None) -> str:
    found: dict[str, Any] = loads(resolution, None) or {}
    if not found:
        return "—"
    source: dict[str, Any] = found.get("source") or {}
    parts = [escape(found.get("answer") or "")]
    words = source.get("text")
    if words:
        parts.append(
            f"<small>{escape(source.get('channel') or '')} · 原话：{escape(words)}</small>"
        )
    return '<span class="clip">' + "<br>".join(p for p in parts if p) + "</span>"


def _session_state(r: Mapping[str, Any]) -> str:
    if not r["session_ref"]:
        return "—"
    facts = [
        ("提问", r["asked_at"]),
        ("转交期限", r["handoff_after"]),
        ("用户回复", r["responded_at"]),
        ("已转交", r["handed_off_at"]),
        ("转达到", r["relayed_to"]),
    ]
    return "<br>".join(f"{escape(k)} {escape(v)}" for k, v in facts if v) or "—"


def _questions(conn: sqlite3.Connection, task_id: str) -> tuple[str, int, int]:
    rows: list[list[str]] = []
    pending = 0
    for r in conn.execute(
        "SELECT r.request_id, r.kind, r.status, r.question, r.resolution, s.session_ref, "
        "s.asked_at, s.handoff_after, s.handed_off_at, s.relayed_to, s.responded_at "
        "FROM input_requests r LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
        "WHERE r.task_id = ? ORDER BY r.status != 'pending', r.request_id DESC",
        (task_id,),
    ):
        pending += r["status"] == "pending"
        rows.append(
            [
                escape(r["request_id"]),
                escape(r["kind"]),
                escape(REQUEST_STATUS.get(str(r["status"]), r["status"])),
                f'<span class="clip">{escape(r["question"])}</span>',
                _answer(r["resolution"]),
                _session_state(r),
            ]
        )
    body = _table(("编号", "类型", "状态", "问题", "处理结果", "会话内提问"), rows)
    judgments = [
        [
            escape(r["item_id"]),
            escape(r["trigger"]),
            escape(r["status"]),
            escape(r["decision_ref"] or "—"),
        ]
        for r in conn.execute(
            "SELECT * FROM judgments WHERE task_id = ? ORDER BY item_id", (task_id,)
        )
    ]
    decisions = [
        [
            escape(r["decision_id"]),
            escape(r["trigger"]),
            f'<span class="clip">{escape(r["decision"])}</span>',
            f'<span class="clip">{escape(r["rationale"])}</span>',
            escape(r["confidence"]),
            escape(r["sequence"]),
        ]
        for r in conn.execute(
            "SELECT * FROM decisions WHERE task_id = ? ORDER BY sequence DESC", (task_id,)
        )
    ]
    body += (
        "<h3>待判断事项</h3>"
        + _table(("编号", "触发", "状态", "决定"), judgments)
        + "<h3>主会话的决定</h3>"
        + _table(("编号", "触发", "决定", "理由", "把握", "账本序号"), decisions)
    )
    return body, len(rows), pending


def _pause(row: Mapping[str, Any]) -> str:
    pause: dict[str, Any] = loads(row["pause"], None) or {}
    if not pause:
        return '<p class="none">未暂停</p>'
    return _facts(
        [
            ("类别", escape(pause.get("category") or "")),
            ("负责人", escape(pause.get("owner") or "")),
            ("下一步", escape(pause.get("next_action") or "")),
            ("恢复条件", escape(pause.get("resume_condition") or "")),
        ]
    )


def _notes(conn: sqlite3.Connection, task_id: str, times: Mapping[int, str]) -> tuple[str, int]:
    rows = [
        [
            escape(r["sequence"]),
            _time(times.get(int(r["sequence"]))),
            f'<p class="pre">{escape(r["text"])}</p>',
        ]
        for r in conn.execute(
            "SELECT sequence, text FROM task_notes WHERE task_id = ? "
            "ORDER BY sequence DESC LIMIT ?",
            (task_id, NOTES_LIMIT),
        )
    ]
    return _table(("账本序号", "时间", "进展"), rows), len(rows)


def _timeline(steps: list[Step]) -> str:
    shown = steps[-TIMELINE_LIMIT:][::-1]
    rows = [
        [
            _time(s.time),
            escape(s.sequence),
            _code(s.type),
            escape(s.transition or ""),
            escape(s.attempt or ""),
        ]
        for s in shown
    ]
    more = (
        f'<p class="meta">只列出最近 {TIMELINE_LIMIT} 条（共 {len(steps)} 条）。</p>'
        if len(steps) > TIMELINE_LIMIT
        else ""
    )
    return _table(("时间", "账本序号", "事件", "迁移", "attempt"), rows) + more


def render_task(
    conn: sqlite3.Connection, task_id: str, app: AppView, steps: list[Step]
) -> str | None:
    """The task's own page, or None when the projection has no such task."""
    found = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if found is None:
        return None
    row = dict(found)
    status = str(row["status"])
    kind = str(row["kind"])
    attempts = sorted(
        (dict(r) for r in conn.execute("SELECT * FROM attempts WHERE task_id = ?", (task_id,))),
        key=lambda a: _attempt_key(str(a["attempt"])),
    )
    current = next((a for a in attempts if a["attempt"] == row["current_attempt"]), None)
    card = card_notes(
        row,
        current,
        pending_asks(conn).get(task_id),
        task_id in invalidated(conn),
        app,
    )
    notes = "".join(
        f'<div class="note {cls}"><b>{escape(head)}</b>'
        + (f'：<span class="clip">{escape(text)}</span>' if text else "")
        + "</div>"
        for cls, head, text in card.notes
    )
    imported = conn.execute("SELECT * FROM imported_tasks WHERE task_id = ?", (task_id,)).fetchone()
    times = {s.sequence: s.time for s in steps}
    past = app.history.get(task_id, [])
    requirement = brief_html(row, current, None)
    reviews, n_rounds = _reviews(conn, task_id)
    outputs, n_outputs = _outputs(conn, task_id)
    questions, n_questions, n_pending = _questions(conn, task_id)
    progress, n_notes = _notes(conn, task_id, times)
    sub = f" <small>· {escape(row['substate'])}</small>" if row["substate"] else ""
    session = session_link(task_id, current)
    sections = [
        _section("overview", "概况", _overview(row, dict(imported) if imported else None)),
        _section(
            "requirement",
            "需求",
            requirement.replace('<details class="brief">', '<details class="brief" open>', 1)
            + "<h3>revision 历史</h3>"
            + _revisions(row, past),
        ),
        _section("attempts", "attempt", _attempts(task_id, attempts), len(attempts)),
        _section("submission", "提交与评审", _submission(row) + reviews, n_rounds),
        _section("outputs", "产出", outputs, n_outputs),
        _section("relations", "依赖与资源", _relations(conn, row)),
        _section(
            "questions",
            "问题与判断" + (f"（待答 {n_pending}）" if n_pending else ""),
            questions,
            n_questions,
        ),
        _section("pause", "暂停", _pause(row)),
        _section("notes", "进展记录", progress, n_notes),
        _section("timeline", "时间线", _timeline(steps), len(steps)),
    ]
    back = "../#task-" + quote(task_id, safe="")
    classes = f"view st-{escape(status)} k-{escape(kind)}" + (
        " user" if "user" in card.flags else ""
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
{page_head(f"{task_id} · TaskSteward", app)}
<body><main class="task-view" data-tsw-task="{escape(task_id)}">
<nav class="crumbs"><a class="back" href="{escape(back)}">← 返回看板</a></nav>
<header class="{classes}"><div class="top"><span class="tid">{escape(task_id)}</span>
<span class="badge">{escape(STATUS_LABELS.get(status, status))}{sub}</span>
<span class="chip kind">{escape(KINDS.get(kind, kind))}</span>
<span class="chip repo" style="--h:{repo_hue(str(row["repo"]))}">{escape(row["repo"])}</span>
{'<span class="flag">待用户处理</span>' if "user" in card.flags else ""}</div>
<h1>{escape(row["title"])}</h1>
{f'<div class="notes">{notes}</div>' if notes else ""}
{f'<p class="sess">会话：{session}</p>' if session else ""}
{actions_html(task_id, row["revision"], app)}</header>
{"".join(sections)}
<footer class="meta">{FOOTER}</footer>
</main></body>
</html>
"""


def not_found(task_id: str, app: AppView) -> str:
    """The page for a task the projection does not have, with the way back to the board."""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
{page_head("没有这个任务 · TaskSteward", app)}
<body><main class="task-view">
<nav class="crumbs"><a class="back" href="../">← 返回看板</a></nav>
<p class="empty">没有任务 {escape(task_id)}。</p>
</main></body>
</html>
"""
