"""The Dashboard page (``views/STATUS.html``), generated from the projection after every commit.

The page shows the recorded times of the ledger, never the time it was rendered, so generating
it again does not change any business time. It is one offline file: a card per task, grouped by
status, with the user's pending items called out on the card. The inlined script
(:mod:`tsw.host.status_assets`) adds filtering, search, sorting, regrouping, folding and the task
flow view (:mod:`tsw.host.flow`); without it the page still shows every card.

The page is called TaskSteward Dashboard (DN006 §8.10, T273); the command, the file and the
configuration key keep their ``status`` names. The Dashboard app (:mod:`tsw.host.status_app`)
renders the same page with an
:class:`AppView`: every card then offers the user's direct actions as buttons and links to the
task's own view (:mod:`tsw.host.task_view`). A card with a session links to it in its host, with
the ``data-tsw-*`` attributes pages that embed this one rely on (DN006 §8.10).
"""

from __future__ import annotations

import hashlib
import html
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from tsw.commands import CommitContext
from tsw.host.flow import FlowTask, render_flow
from tsw.host.status_assets import (
    APP_CSP,
    APP_SCRIPT,
    CSP,
    CSS,
    KIND_COLORS,
    REPO_HUES,
    SCRIPT,
    STATUS_COLORS,
)
from tsw.machine.follow_up import open_count
from tsw.machine.guards import invalidated
from tsw.machine.model import OCCUPYING, loads
from tsw.tasks import KINDS, STATUS_LABELS

ORDER = (
    "waiting_user",
    "verifying",
    "in_progress",
    "dispatching",
    "paused",
    "ready",
    "waiting_dependency",
    "intake",
    "done",
    "accepted",
    "cancelled",
)
# The name the user sees (DN006 §8.10, T273).
TITLE = "TaskSteward Dashboard"
# Groups folded until the user opens them: finished work stays out of the way.
FOLDED = ("accepted", "cancelled")
PR_TYPES = ("pr", "pull_request")
OUTPUT_TYPES = {
    "pr": "PR",
    "pull_request": "PR",
    "report": "报告",
    "document": "文档",
    "dataset": "数据集",
    "email": "邮件",
    "presentation": "演示文稿",
    "commit": "提交",
    "teams_message": "Teams 消息",
    "files": "文件",
    "other": "其他",
}


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


_e = escape


def link_for(ref: str) -> str | None:
    """A link for an output reference, relative to ``views/``; None when it is not a location."""
    if ref.startswith(("https://", "http://")):
        return ref
    if ref.startswith("legacy://"):
        return "../legacy/" + quote(ref[len("legacy://") :], safe="/")
    if ref.startswith("runs/"):
        return "../" + quote(ref, safe="/")
    return None


# The address of a session in its host, by the attempt's session adapter (DN006 §8.10). An attempt
# with no adapter recorded (imported, or recorded before adapters were) is a ``claude-app`` one.
SESSION_URLS = {"claude-app": "claude://claude.ai/epitaxy/{ref}"}
DEFAULT_ADAPTER = "claude-app"


def session_href(adapter: str | None, session_ref: str) -> str | None:
    """The host's address of a session, or None when its adapter has none (CC006 §3)."""
    template = SESSION_URLS.get(adapter or DEFAULT_ADAPTER)
    return template.format(ref=quote(session_ref, safe="")) if template else None


def session_link(task_id: str, attempt: dict[str, Any] | None) -> str:
    """A link to the attempt's session with the attributes embedders rely on (DN006 §8.10).

    ``data-tsw-session``, ``data-tsw-task``, ``data-tsw-attempt`` and ``data-tsw-adapter`` are a
    contract with pages that embed this one: they only grow, never change.
    """
    ref = attempt.get("session_ref") if attempt else None
    if not attempt or not ref:
        return ""
    adapter = str(attempt.get("adapter") or DEFAULT_ADAPTER)
    label = attempt.get("session_name") or ref
    attrs = (
        f'class="session" data-tsw-session="{_e(ref)}" data-tsw-task="{_e(task_id)}" '
        f'data-tsw-attempt="{_e(attempt["attempt"])}" data-tsw-adapter="{_e(adapter)}"'
    )
    href = session_href(adapter, str(ref))
    if href is None:
        return f"<span {attrs}>{_e(label)}</span>"
    return f'<a {attrs} href="{_e(href)}" title="在宿主中打开会话 {_e(ref)}">{_e(label)}</a>'


def task_href(task_id: str) -> str:
    """The task view's address relative to the board (the STATUS app only, DN006 §8.10)."""
    return "tasks/" + quote(task_id, safe="")


def follow_up_anchor(follow_up_id: str) -> str:
    """The anchor of a follow-up on its task's view (DN006 §8.10)."""
    return "follow-up-" + quote(follow_up_id, safe="")


def follow_up_facts(
    card: Card, app: AppView | None, on_board: frozenset[str] = frozenset()
) -> list[tuple[str, str]]:
    """The card's follow-up lines: how many are open, and where the task came from.

    In the app a source links to its task view, which archived tasks keep too; the static page
    links to the source's card only while the board shows it (``on_board``).
    """
    task_id = str(card.row["task_id"])
    facts: list[tuple[str, str]] = []
    if card.follow_ups:
        label = f"{card.follow_ups} 条待处理"
        facts.append(
            (
                "follow-up",
                f'<a class="fu" href="{_e(task_href(task_id))}#follow-ups">{_e(label)}</a>'
                if app
                else f'<span class="fu">{_e(label)}</span>',
            )
        )
    if card.follow_up_of:
        links = [
            f'<a href="{_e(task_href(src))}#{_e(follow_up_anchor(fid))}">{_e(fid)}</a>'
            if app
            else f'<a href="#task-{_e(src)}">{_e(fid)}</a>'
            if src in on_board
            else _e(fid)
            for src, fid in card.follow_up_of
        ]
        facts.append(("来自", " ".join(links)))
    return facts


def repo_hue(repo: str) -> int:
    digest = hashlib.sha256(repo.encode("utf-8")).digest()
    return REPO_HUES[digest[0] % len(REPO_HUES)]


@dataclass
class Output:
    kind: str
    label: str
    ref: str
    delivery: str | None

    def html(self, cls: str) -> str:
        href = link_for(self.ref)
        link = f'<a href="{_e(href)}">{_e(self.label)}</a>' if href else _e(self.label)
        extra = f"<small>{_e(self.delivery)}</small>" if self.delivery else ""
        return f'<span class="out {cls}">{link}{extra}</span>'


def _no_notes() -> list[tuple[str, str, str]]:
    return []


def _no_flags() -> set[str]:
    return set()


def _no_sources() -> list[tuple[str, str]]:
    return []


@dataclass
class Card:
    row: dict[str, Any]
    notes: list[tuple[str, str, str]] = field(default_factory=_no_notes)
    flags: set[str] = field(default_factory=_no_flags)
    # Open follow-ups of the task, and the follow-ups it was made from (DN002 §5.7).
    follow_ups: int = 0
    follow_up_of: list[tuple[str, str]] = field(default_factory=_no_sources)


ACTION_LABELS = {"accept": "验收", "reject": "退回", "cancel": "取消"}


@dataclass(frozen=True)
class Revision:
    """One earlier revision of a task's requirement, from the ledger (the STATUS app only)."""

    revision: int
    time: str
    how: str  # what made it: registration, a requirement change, a rejection
    goal: str
    acceptance: tuple[str, ...]
    words: str = ""  # the user's words recorded with the change


def _no_history() -> dict[str, list[Revision]]:
    return {}


@dataclass(frozen=True)
class AppView:
    """The STATUS app's additions: the request token, each task's direct actions and history.

    ``actions`` maps a task to the actions it offers and, when cancel is not offered for a
    task that could otherwise be cancelled, the reason shown instead. ``history`` maps a task
    to its earlier revisions, oldest first; the offline page has only the current one.
    """

    token: str
    actions: dict[str, tuple[tuple[str, ...], str | None]]
    history: dict[str, list[Revision]] = field(default_factory=_no_history)


def _requirement(goal: str, acceptance: list[Any] | tuple[str, ...]) -> str:
    out = f'<h4>目标</h4><p class="pre">{_e(goal)}</p>' if goal else ""
    if acceptance:
        out += "<h4>验收</h4><ul>" + "".join(f"<li>{_e(a)}</li>" for a in acceptance) + "</ul>"
    return out


def brief_html(
    row: dict[str, Any], attempt: dict[str, Any] | None, history: list[Revision] | None
) -> str:
    """The task's brief at its current revision, and, in the app, the earlier revisions."""
    acceptance: list[Any] = loads(row["acceptance"], []) or []
    path = attempt.get("brief_path") if attempt else None
    link = (
        f'<p><a href="{_e(link_for(str(path)))}">打开 worker 的任务书（brief.md）</a></p>'
        if path and link_for(str(path))
        else ""
    )
    body = _requirement(str(row["goal"] or ""), acceptance) + link
    if not body:
        return ""
    out = (
        f'<details class="brief"><summary>任务书 · revision {_e(row["revision"])}</summary>'
        f'<div class="body scroll">{body}</div></details>'
    )
    earlier = [r for r in history or [] if r.revision < int(row["revision"])]
    if earlier:
        sections = "".join(
            f'<section class="rev"><h4>revision {r.revision} · {_e(r.how)} · '
            f'<time datetime="{_e(r.time)}">{_e(r.time)}</time></h4>'
            + (f'<p class="pre words">{_e(r.words)}</p>' if r.words else "")
            + _requirement(r.goal, r.acceptance)
            + "</section>"
            for r in reversed(earlier)
        )
        out += (
            f'<details class="brief history"><summary>历史任务书（{len(earlier)}）</summary>'
            f'<div class="body scroll">{sections}</div></details>'
        )
    return out


def actions_html(task_id: str, revision: Any, app: AppView) -> str:
    offered, blocked = app.actions.get(task_id, ((), None))
    if not offered and not blocked:
        return ""
    buttons = "".join(
        f'<button type="button" class="act act-{_e(a)}" data-act="{_e(a)}">'
        f"{_e(ACTION_LABELS.get(a, a))}</button>"
        for a in offered
    )
    why = f'<small class="act-why">{_e(blocked)}</small>' if blocked else ""
    return (
        f'<div class="acts" data-task="{_e(task_id)}" data-revision="{_e(revision)}">'
        f'{buttons}{why}<span class="act-result" role="status" aria-live="polite"></span></div>'
    )


def outputs_by_task(conn: sqlite3.Connection) -> dict[str, list[Output]]:
    """PR links and registered outputs per task, PRs first."""
    out: dict[str, list[Output]] = {}
    for r in conn.execute(
        "SELECT task_id, output_id, type, ref, delivery FROM outputs "
        "ORDER BY task_id, type NOT IN ('pr', 'pull_request'), output_id"
    ):
        ref = str(r["ref"])
        is_pr = r["type"] in PR_TYPES
        label = ref.rsplit("/", 2)[-1] if is_pr and "/pull/" in ref else str(r["output_id"])
        label = f"PR #{label}" if is_pr and label.isdigit() else label
        out.setdefault(str(r["task_id"]), []).append(
            Output(str(r["type"]), label, ref, r["delivery"])
        )
    return out


def _ask_note(ask: dict[str, Any]) -> str:
    if ask["relayed_to"]:
        return f"已转交主会话，并转达到 {ask['relayed_to']}"
    if ask["handed_off_at"]:
        return f"已于 {ask['handed_off_at']} 转交主会话"
    if ask["responded_at"]:
        return "用户已在 worker 会话中回复"
    if ask["handoff_after"]:
        return f"请在 worker 会话中回答；{ask['handoff_after']} 前无回复则转交主会话"
    return "请在主会话中回答"


def _card(
    card: Card,
    attempt: dict[str, Any] | None,
    outputs: list[Output],
    deps: list[str],
    note: str | None,
    *,
    app: AppView | None = None,
    on_board: frozenset[str] = frozenset(),
) -> str:
    row = card.row
    status = str(row["status"])
    task_id = str(row["task_id"])
    kind = str(row["kind"])
    phase = attempt["phase"] if attempt else None
    submission: dict[str, Any] = loads(row["submission"], None) or {}
    report_info: dict[str, Any] = submission.get("report") or {}
    report: str | None = report_info.get("html_path")
    prs = [o for o in outputs if o.kind in PR_TYPES]
    others = [o for o in outputs if o.kind not in PR_TYPES]
    links = [o.html("pr") for o in prs]
    if report and not any(o.ref == report for o in outputs):
        links.append(Output("report", "报告", str(report), None).html("rep"))
    face = [o for o in others if o.kind == "report"][:2]
    links += [o.html("rep") for o in face]
    rest = [o for o in others if o not in face]

    sub = f" <small>· {_e(row['substate'])}</small>" if row["substate"] else ""
    flag = '<span class="flag">待用户处理</span>' if "user" in card.flags else ""
    run = " · ".join(_e(v) for v in (row["current_attempt"], phase) if v)
    link = session_link(task_id, attempt)
    if link:
        run = f"{run} · {link}" if run else link
    facts = [
        ("attempt", run or "—"),
        ("revision", _e(row["revision"])),
        (
            "最近记录",
            f'<time datetime="{_e(row["last_activity"])}">{_e(row["last_activity"])}</time>',
        ),
    ]
    if deps:
        facts.append(("依赖", " ".join(f'<a href="#task-{_e(d)}">{_e(d)}</a>' for d in deps)))
    facts += follow_up_facts(card, app, on_board)
    # A long text (a question, a pause) scrolls inside its note, so the card keeps its height.
    notes = "".join(
        f'<div class="note {cls}"><b>{_e(head)}</b>'
        + (f'：<span class="clip">{_e(text)}</span>' if text else "")
        + "</div>"
        for cls, head, text in card.notes
    )
    brief = brief_html(row, attempt, app.history.get(task_id, []) if app else None)

    body: list[str] = []
    if note:
        body.append(f'<h4>最近进展</h4><p class="pre">{_e(note)}</p>')
    times = [
        (label, row[key])
        for label, key in (("完成", "done_at"), ("验收", "accepted_at"))
        if row[key]
    ]
    if attempt and attempt.get("branch"):
        times.append(("分支", attempt["branch"]))
    if times:
        body.append(
            '<dl class="facts">'
            + "".join(f"<div><dt>{_e(k)}</dt><dd>{_e(v)}</dd></div>" for k, v in times)
            + "</dl>"
        )
    if rest:
        items = "".join(
            f"<li>{_e(OUTPUT_TYPES.get(o.kind, o.kind))} · {o.html('')}</li>" for o in rest
        )
        body.append(f"<h4>其他产出（{len(rest)}）</h4><ul>{items}</ul>")
    more = (
        f'<details class="more"><summary>详情</summary><div class="body">{"".join(body)}</div></details>'
        if body
        else ""
    )
    classes = f"card st-{_e(status)} k-{_e(kind)}" + (" user" if "user" in card.flags else "")
    return (
        f'<article class="{classes}" id="task-{_e(task_id)}" data-status="{_e(status)}" '
        f'data-repo="{_e(row["repo"])}" data-kind="{_e(kind)}" data-seq="{_e(row["registered_seq"])}" '
        f'data-activity="{_e(row["last_activity"])}" data-flags="{_e(" ".join(sorted(card.flags)))}">'
        f'<div class="top"><a class="tid" href="#task-{_e(task_id)}">{_e(task_id)}</a>'
        f'<span class="badge">{_e(STATUS_LABELS.get(status, status))}{sub}</span>'
        f'<span class="chip kind">{_e(KINDS.get(kind, kind))}</span>'
        f'<span class="chip repo" style="--h:{repo_hue(str(row["repo"]))}">{_e(row["repo"])}</span>'
        f"{flag}"
        + (f'<a class="detail" href="{_e(task_href(task_id))}">详情</a>' if app else "")
        + "</div>"
        f"<h3>{_e(row['title'])}</h3>"
        + (f'<div class="notes">{notes}</div>' if notes else "")
        + '<dl class="facts">'
        + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
        + "</dl>"
        + (f'<div class="links">{"".join(links)}</div>' if links else "")
        + (actions_html(task_id, row["revision"], app) if app else "")
        + (f'<div class="foot">{brief}{more}</div>' if brief or more else "")
        + "</article>"
    )


def _colors() -> str:
    rules = [f".st-{k}{{--c:{v}}}" for k, v in STATUS_COLORS.items()]
    rules += [
        f".k-{k} .chip.kind,.pick.k-{k},.group.k-{k}{{--c:{v}}}" for k, v in KIND_COLORS.items()
    ]
    return "\n".join(rules)


def card_notes(
    row: dict[str, Any],
    attempt: dict[str, Any] | None,
    ask: dict[str, Any] | None,
    invalid: bool,
    app: AppView | None,
) -> Card:
    """What the card calls out for the task (a question, an acceptance, a pause, an alert)."""
    card = Card(row)
    phase = attempt["phase"] if attempt else None
    if row["status"] == "ready" and phase in (None, "planned", "replaced", "closed"):
        card.notes.append(("start", "待启动", ""))
        card.flags.add("tostart")
    if ask is not None:
        # The page is for the user: the question itself is shown so it can be answered.
        card.notes.append(
            (
                "ask",
                f"等待用户（{ask['request_id']}，{_ask_note(ask)}）",
                str(ask["question"]),
            )
        )
        card.flags |= {"asking", "user"}
    if row["status"] == "waiting_user":
        card.flags.add("user")
    if row["status"] == "done":
        card.notes.append(
            (
                "accept",
                "待用户验收",
                "查看报告与产出后，在本页点「验收」或「退回」"
                if app
                else f"查看报告与产出后验收：在终端运行 tsw task accept {row['task_id']}，"
                "或在 Dashboard 应用（tsw status serve）中点「验收」；也可以在主会话中验收",
            )
        )
        card.flags.add("user")
    pause = loads(row["pause"], None)
    if pause:
        owner = str(pause.get("owner") or "")
        card.notes.append(
            (
                "pause",
                f"暂停（{pause.get('category')}）负责人 {owner}",
                f"{pause.get('next_action')}；恢复条件：{pause.get('resume_condition')}",
            )
        )
        card.flags.add("paused")
        if owner == "用户":
            card.flags.add("user")
    if row["stale"]:
        card.notes.append(("stale", "stale：上游已变化", ""))
        card.flags.add("alert")
    if invalid and row["status"] in OCCUPYING:
        card.notes.append(("invalid", "依赖失效，需停止 worker", ""))
        card.flags.add("alert")
    return card


# Attention tiles: (label, colour, filter on click: statuses, flag).
ATTENTION = (
    ("占用名额", "#2563eb", ",".join(s for s in ORDER if s in OCCUPYING), ""),
    ("待启动", "#0d9488", "", "tostart"),
    ("待判断", "#7c3aed", "", ""),
    ("待用户答复", "#e11d48", "", "asking"),
    ("待处理 follow-up", "#0891b2", "", "followups"),
)


def _tile(label: str, n: int, color: str, *, st: str = "", flag: str = "", extra: str = "") -> str:
    inner = f"<b>{n}</b><span>{_e(label)}</span>"
    if not (st or flag or extra):
        return f'<div class="tile" style="--c:{color}">{inner}</div>'
    title = "在任务流中查看已归档任务" if extra else "只看这些任务"
    return (
        f'<button type="button" class="tile" style="--c:{color}" data-tile-st="{_e(st)}" '
        f'data-tile-flag="{_e(flag)}"{extra} title="{title}">{inner}</button>'
    )


def tiles(
    *,
    occupying: int,
    to_start: int,
    judgments: int,
    pending: int,
    follow_ups: int,
    statuses: list[tuple[str, int]],
    archived: int,
) -> str:
    """The count tiles: what needs attention, then each status on the board (DN006 §8.10).

    A tile whose count is zero is not shown. The status tiles are the statuses actually on the
    board, with the counts of the bar and the picks below; the last one counts the archived
    tasks and opens them in the task flow.
    """
    attention = [
        _tile(label, n, color, st=st, flag=flag)
        for (label, color, st, flag), n in zip(
            ATTENTION, (occupying, to_start, judgments, pending, follow_ups), strict=True
        )
        if n
    ]
    states = [
        _tile(STATUS_LABELS.get(s, s), n, STATUS_COLORS.get(s, "#64748b"), st=s)
        for s, n in statuses
        if n
    ]
    if archived:
        states.append(
            _tile(
                STATUS_LABELS["archived"],
                archived,
                STATUS_COLORS["archived"],
                extra=' data-tile-archived="1"',
            )
        )
    out = ""
    if attention:
        out += f'<div class="tile-group" aria-label="关注">{"".join(attention)}</div>'
    if states:
        out += f'<div class="tile-group states" aria-label="状态">{"".join(states)}</div>'
    return out


def pending_asks(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Each task's pending question with its in-session state (the last by number)."""
    return {
        str(r["task_id"]): dict(r)
        for r in conn.execute(
            "SELECT r.task_id, r.request_id, r.question, s.handoff_after, s.handed_off_at, "
            "s.relayed_to, s.responded_at FROM input_requests r "
            "LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
            "WHERE r.status = 'pending' ORDER BY r.request_id"
        )
    }


def page_head(title: str, app: AppView | None) -> str:
    """The page's ``<head>``: its policy, styles and scripts, and in the app the request token.

    The board and the task view share it, so both keep the same Content-Security-Policy.
    """
    if app:
        policy = (
            f'<meta http-equiv="Content-Security-Policy" content="{_e(APP_CSP)}">\n'
            f'<meta name="tsw-token" content="{_e(app.token)}">'
        )
        app_script = f"<script>{APP_SCRIPT}</script>"
    else:
        policy = f'<meta http-equiv="Content-Security-Policy" content="{_e(CSP)}">'
        app_script = ""
    return (
        '<head><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width, initial-scale=1">\n'
        f"{policy}\n<title>{_e(title)}</title>\n<style>{CSS}{_colors()}</style>\n"
        f"<script>{SCRIPT}</script>{app_script}</head>"
    )


def render(conn: sqlite3.Connection, app: AppView | None = None) -> str:
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY registered_seq")]
    outputs = outputs_by_task(conn)
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    lease = conn.execute("SELECT * FROM leases WHERE name = 'scheduler'").fetchone()
    attempts = {
        (r["task_id"], r["attempt"]): dict(r) for r in conn.execute("SELECT * FROM attempts")
    }
    deps: dict[str, list[str]] = {}
    for r in conn.execute("SELECT task_id, dep_id FROM dependencies ORDER BY task_id, dep_id"):
        deps.setdefault(str(r["task_id"]), []).append(str(r["dep_id"]))
    latest_note = {
        str(r["task_id"]): str(r["text"])
        for r in conn.execute(
            "SELECT task_id, text FROM task_notes n WHERE sequence = "
            "(SELECT MAX(sequence) FROM task_notes m WHERE m.task_id = n.task_id)"
        )
    }
    invalid = invalidated(conn)
    asking = pending_asks(conn)
    counts: dict[str, int] = {}
    for row in tasks:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    occupying = sum(counts.get(s, 0) for s in OCCUPYING)
    shown = sorted(
        (t for t in tasks if t["status"] != "archived"),
        key=lambda t: (
            ORDER.index(t["status"]) if t["status"] in ORDER else 99,
            t["registered_seq"],
        ),
    )
    cards = [
        card_notes(
            row,
            attempts.get((row["task_id"], row["current_attempt"])),
            asking.get(row["task_id"]),
            row["task_id"] in invalid,
            app,
        )
        for row in shown
    ]
    to_start = sum(1 for c in cards if "tostart" in c.flags)
    # Follow-ups (DN002 §5.7): open ones on their task's card, the source on the task made.
    open_follow_ups = open_count(conn)
    sources: dict[str, list[tuple[str, str]]] = {}
    for r in conn.execute(
        "SELECT task_id, follow_up_id, converted_to FROM follow_ups "
        "WHERE converted_to IS NOT NULL ORDER BY task_id, recorded_seq, follow_up_id"
    ):
        sources.setdefault(str(r["converted_to"]), []).append(
            (str(r["task_id"]), str(r["follow_up_id"]))
        )
    for c in cards:
        c.follow_ups = open_follow_ups.get(str(c.row["task_id"]), 0)
        c.follow_up_of = sources.get(str(c.row["task_id"]), [])
        if c.follow_ups:
            c.flags.add("followups")

    judgments = conn.execute(
        "SELECT COUNT(*) AS n FROM judgments WHERE status = 'open'"
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM input_requests WHERE status = 'pending'"
    ).fetchone()["n"]
    archived = counts.get("archived", 0)
    # The statuses on the board, in board order: the tiles, the bar, the picks and the groups
    # all come from this one count (DN006 §8.10, T273).
    present = [s for s in ORDER if counts.get(s)] + sorted(
        s for s in counts if s not in ORDER and s != "archived"
    )
    tiles_html = tiles(
        occupying=occupying,
        to_start=to_start,
        judgments=judgments,
        pending=pending,
        follow_ups=sum(c.follow_ups for c in cards),
        statuses=[(s, counts[s]) for s in present],
        archived=archived,
    )
    dist = "".join(
        f'<i class="st-{_e(s)}" style="flex:{counts[s]}" '
        f'title="{_e(STATUS_LABELS.get(s, s))} {counts[s]}"></i>'
        for s in present
    )
    # Every status and kind has a pick, so a choice survives a reload while nothing has it;
    # the empty ones stay hidden unless chosen (DN006 §8.10, T273).
    pick_statuses = list(ORDER) + [s for s in present if s not in ORDER]
    status_picks = "".join(
        f'<button type="button" class="pick st-{_e(s)}" data-st="{_e(s)}" '
        f'data-label="{_e(STATUS_LABELS.get(s, s))}" data-n="{counts.get(s, 0)}" '
        f'aria-pressed="false"{"" if counts.get(s) else " hidden"}>'
        f"{_e(STATUS_LABELS.get(s, s))}<small>{counts.get(s, 0)}</small></button>"
        for s in pick_statuses
    )
    # Kinds and repositories count the board's tasks (data-n) and, for the task flow with the
    # archived tasks, every task (data-na); the script shows those with tasks in the view.
    kinds_present = list(KINDS) + sorted({str(t["kind"]) for t in tasks} - set(KINDS))
    kind_count = {k: sum(1 for c in cards if c.row["kind"] == k) for k in kinds_present}
    kind_all = {k: sum(1 for t in tasks if t["kind"] == k) for k in kinds_present}
    kind_picks = "".join(
        f'<button type="button" class="pick k-{_e(k)}" data-kind="{_e(k)}" '
        f'data-label="{_e(KINDS.get(k, k))}" data-n="{kind_count[k]}" data-na="{kind_all[k]}" '
        f'aria-pressed="false"{"" if kind_count[k] else " hidden"}>{_e(KINDS.get(k, k))}'
        f"<small>{kind_count[k]}</small></button>"
        for k in kinds_present
    )
    repo_count: dict[str, int] = {}
    for c in cards:
        repo_count[str(c.row["repo"])] = repo_count.get(str(c.row["repo"]), 0) + 1
    repo_all: dict[str, int] = {}
    for t in tasks:
        repo_all[str(t["repo"])] = repo_all.get(str(t["repo"]), 0) + 1
    repo_options = "".join(
        f'<option value="{_e(r)}" data-n="{repo_count.get(r, 0)}" data-na="{n}"'
        f"{'' if repo_count.get(r) else ' hidden'}>{_e(r)}</option>"
        for r, n in sorted(repo_all.items())
    )

    on_board = frozenset(str(c.row["task_id"]) for c in cards)
    groups: list[str] = []
    for status in present:
        members = [c for c in cards if c.row["status"] == status]
        body = "".join(
            _card(
                c,
                attempts.get((c.row["task_id"], c.row["current_attempt"])),
                outputs.get(c.row["task_id"], []),
                deps.get(c.row["task_id"], []),
                latest_note.get(c.row["task_id"]),
                app=app,
                on_board=on_board,
            )
            for c in members
        )
        groups.append(
            f'<details class="group st-{_e(status)}"{"" if status in FOLDED else " open"}>'
            f"<summary><span>{_e(STATUS_LABELS.get(status, status))}</span>"
            f'<span class="n">{len(members)}</span></summary><div class="grid">{body}</div></details>'
        )

    flow = render_flow(
        [
            FlowTask(
                str(t["task_id"]),
                str(t["title"]),
                str(t["repo"]),
                str(t["kind"]),
                str(t["status"]),
                int(t["registered_seq"]),
                str(t["last_activity"]),
            )
            for t in tasks
        ],
        deps,
        {new: [src for src, _ in found] for new, found in sources.items()},
        flags={str(c.row["task_id"]): " ".join(sorted(c.flags)) for c in cards},
        app=app is not None,
        on_board=on_board,
        repo_hue=repo_hue,
    )
    lease_text = (
        f"调度租约：{_e(lease['holder_session'])}（{_e(lease['machine'])}，到期 {_e(lease['expires_at'])}）"
        if lease is not None and lease["holder_session"]
        else "调度租约：无人持有"
    )
    last = max((t["last_activity"] for t in tasks), default="—")
    order = " ".join(ORDER + tuple(s for s in present if s not in ORDER))
    if app:
        footer = (
            "本页由 Dashboard 应用（tsw status serve）按账本实时生成；"
            "按钮直接写入账本，记为你本人的操作"
        )
    else:
        footer = (
            "本页由 tsw 根据账本生成，请勿手工编辑；"
            "验收、退回、取消可运行 tsw status serve 在页面上操作"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
{page_head(TITLE, app)}
<body><main>
<header class="hero"><div><div class="eyebrow">TASKSTEWARD · DASHBOARD</div><h1>{TITLE}</h1></div>
<p class="meta">账本序号 {_e(meta.get("projected_seq", "0"))} · 最近记录 {_e(last)} · {
        lease_text
    }</p>
<button type="button" class="ghost" id="refresh" hidden aria-pressed="false" title="每 30 秒重新载入本页">自动刷新</button>
<button type="button" class="ghost" id="style" hidden title="朴素或鲜艳">风格：朴素</button>
<button type="button" class="ghost" id="theme" hidden>主题：跟随系统</button></header>
<section class="tiles">{tiles_html}</section>
<div class="dist" aria-hidden="true">{dist}</div>
<section class="tools" id="tools" role="search" aria-label="筛选任务" hidden>
<div class="row"><input id="q" type="search" placeholder="搜索编号、标题、仓库、问题、产出…（按 / 聚焦）" aria-label="搜索">
<button type="button" class="ghost" id="mine" aria-pressed="false">只看待用户处理</button>
<label>仓库 <select id="repo"><option value="">全部仓库</option>{repo_options}</select></label>
<label>关注 <select id="flag"><option value="">全部</option><option value="user">待用户处理</option>
<option value="asking">待用户答复</option><option value="tostart">待启动</option>
<option value="paused">暂停</option><option value="alert">需要处理的异常</option>
<option value="followups">有待处理的 follow-up</option></select></label>
<span class="sep"></span>
<span class="views" role="group" aria-label="视图"><button type="button" class="ghost" id="view-status" aria-pressed="false" title="按状态分组">按状态</button><button type="button" class="ghost" id="timeline" aria-pressed="false" title="不分组，按最近一次修改时间倒序排列">按时间顺序</button><button type="button" class="ghost" id="view-flow" aria-pressed="false" title="按依赖关系画出任务的拓扑图">任务流</button></span>
<label class="board-only">排序 <select id="sort"><option value="status">状态优先</option><option value="activity">最近活动</option>
<option value="stalest">最久未动</option><option value="newest">编号：新→旧</option>
<option value="oldest">编号：旧→新</option><option value="repo">仓库</option></select></label>
<label class="board-only">分组 <select id="group"><option value="status">按状态</option><option value="repo">按仓库</option>
<option value="kind">按类型</option><option value="none">不分组</option></select></label></div>
<div class="row"><span class="lbl">状态</span>{status_picks}<span class="lbl">类型</span>{
        kind_picks
    }
<span class="sep"></span><span class="count" id="count" role="status" aria-live="polite"></span>
<button type="button" class="ghost board-only" id="open-all">展开分组</button>
<button type="button" class="ghost board-only" id="close-all">折叠分组</button>
<button type="button" class="ghost board-only" id="cards-open">展开详情</button>
<button type="button" class="ghost board-only" id="cards-close">收起详情</button>
<button type="button" class="ghost" id="clear">清除筛选</button></div></section>
<div id="board" data-order="{_e(order)}" data-kinds="{_e(" ".join(KINDS))}">{"".join(groups)}</div>
{flow}
<p class="empty" id="empty" hidden>没有符合筛选条件的任务。<button type="button" class="ghost" id="clear-empty">清除筛选</button></p>
{"" if cards else '<p class="empty">没有任务</p>'}
<footer class="meta">{footer}；看板不列出已归档任务（{
        archived
    } 个），可在任务流中勾选「含已归档」查看。</footer>
</main></body>
</html>
"""


def write_status(ctx: CommitContext) -> None:
    ctx.storage.put_artifact("views/STATUS.html", render(ctx.conn).encode("utf-8"))
