"""The STATUS page (``views/STATUS.html``), generated from the projection after every commit.

The page shows the recorded times of the ledger, never the time it was rendered, so generating
it again does not change any business time. It is one offline file: a card per task, grouped by
status, with the user's pending items called out on the card. The inlined script
(:mod:`tsw.host.status_assets`) adds filtering, search, sorting, regrouping and folding; without
it the page still shows every card.
"""

from __future__ import annotations

import hashlib
import html
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from tsw.commands import CommitContext
from tsw.host.status_assets import CSP, CSS, KIND_COLORS, REPO_HUES, SCRIPT, STATUS_COLORS
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


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _href(ref: str) -> str | None:
    """A link for an output reference, relative to ``views/``; None when it is not a location."""
    if ref.startswith(("https://", "http://")):
        return ref
    if ref.startswith("legacy://"):
        return "../legacy/" + quote(ref[len("legacy://") :], safe="/")
    if ref.startswith("runs/"):
        return "../" + quote(ref, safe="/")
    return None


def _hue(repo: str) -> int:
    digest = hashlib.sha256(repo.encode("utf-8")).digest()
    return REPO_HUES[digest[0] % len(REPO_HUES)]


@dataclass
class Output:
    kind: str
    label: str
    ref: str
    delivery: str | None

    def html(self, cls: str) -> str:
        href = _href(self.ref)
        link = f'<a href="{_e(href)}">{_e(self.label)}</a>' if href else _e(self.label)
        extra = f"<small>{_e(self.delivery)}</small>" if self.delivery else ""
        return f'<span class="out {cls}">{link}{extra}</span>'


def _no_notes() -> list[tuple[str, str, str]]:
    return []


def _no_flags() -> set[str]:
    return set()


@dataclass
class Card:
    row: dict[str, Any]
    notes: list[tuple[str, str, str]] = field(default_factory=_no_notes)
    flags: set[str] = field(default_factory=_no_flags)


def _outputs(conn: sqlite3.Connection) -> dict[str, list[Output]]:
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
    session = attempt.get("session_name") if attempt else None
    run = " · ".join(_e(v) for v in (row["current_attempt"], phase, session) if v)
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
    notes = "".join(
        f'<div class="note {cls}"><b>{_e(head)}</b>{"：" + _e(text) if text else ""}</div>'
        for cls, head, text in card.notes
    )

    body: list[str] = []
    if row["goal"]:
        body.append(f'<h4>目标</h4><p class="pre">{_e(row["goal"])}</p>')
    acceptance: list[Any] = loads(row["acceptance"], []) or []
    if acceptance:
        items = "".join(f"<li>{_e(a)}</li>" for a in acceptance)
        body.append(f"<h4>验收</h4><ul>{items}</ul>")
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
        f'<span class="chip repo" style="--h:{_hue(str(row["repo"]))}">{_e(row["repo"])}</span>'
        f"{flag}</div>"
        f"<h3>{_e(row['title'])}</h3>"
        + (f'<div class="notes">{notes}</div>' if notes else "")
        + '<dl class="facts">'
        + "".join(f"<div><dt>{k}</dt><dd>{v}</dd></div>" for k, v in facts)
        + "</dl>"
        + (f'<div class="links">{"".join(links)}</div>' if links else "")
        + more
        + "</article>"
    )


def _colors() -> str:
    rules = [f".st-{k}{{--c:{v}}}" for k, v in STATUS_COLORS.items()]
    rules += [
        f".k-{k} .chip.kind,.pick.k-{k},.group.k-{k}{{--c:{v}}}" for k, v in KIND_COLORS.items()
    ]
    return "\n".join(rules)


def render(conn: sqlite3.Connection) -> str:
    tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY registered_seq")]
    outputs = _outputs(conn)
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
    asking = {
        str(r["task_id"]): dict(r)
        for r in conn.execute(
            "SELECT r.task_id, r.request_id, r.question, s.handoff_after, s.handed_off_at, "
            "s.relayed_to, s.responded_at FROM input_requests r "
            "LEFT JOIN in_session_requests s ON s.request_id = r.request_id "
            "WHERE r.status = 'pending' ORDER BY r.request_id"
        )
    }
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
    cards: list[Card] = []
    for row in shown:
        card = Card(row)
        attempt = attempts.get((row["task_id"], row["current_attempt"]))
        phase = attempt["phase"] if attempt else None
        if row["status"] == "ready" and phase in (None, "planned", "replaced", "closed"):
            card.notes.append(("start", "待启动", ""))
            card.flags.add("tostart")
        ask = asking.get(row["task_id"])
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
            card.notes.append(("accept", "待用户验收", "查看报告与产出后在主会话验收"))
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
        if row["task_id"] in invalid and row["status"] in OCCUPYING:
            card.notes.append(("invalid", "依赖失效，需停止 worker", ""))
            card.flags.add("alert")
        cards.append(card)
    to_start = sum(1 for c in cards if "tostart" in c.flags)

    judgments = conn.execute(
        "SELECT COUNT(*) AS n FROM judgments WHERE status = 'open'"
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM input_requests WHERE status = 'pending'"
    ).fetchone()["n"]
    archived = counts.get("archived", 0)
    # (label, value, colour, filter on click: statuses, flag)
    tiles: list[tuple[str, int, str, str, str]] = [
        ("占用名额", occupying, "#2563eb", ",".join(s for s in ORDER if s in OCCUPYING), ""),
        ("待启动", to_start, "#0d9488", "", "tostart"),
        ("待判断", judgments, "#7c3aed", "", ""),
        ("待用户答复", pending, "#e11d48", "", "asking"),
        ("待用户验收", counts.get("done", 0), "#ea580c", "done", ""),
        ("已归档", archived, "#64748b", "", ""),
    ]
    tile_html: list[str] = []
    for label, n, color, st, flag in tiles:
        inner = f"<b>{n}</b><span>{_e(label)}</span>"
        if st or flag:
            tile_html.append(
                f'<button type="button" class="tile" style="--c:{color}" data-tile-st="{st}" '
                f'data-tile-flag="{flag}" title="只看这些任务">{inner}</button>'
            )
        else:
            tile_html.append(f'<div class="tile" style="--c:{color}">{inner}</div>')
    present = [s for s in ORDER if counts.get(s)] + [
        s for s in counts if s not in ORDER and s != "archived"
    ]
    dist = "".join(
        f'<i class="st-{_e(s)}" style="flex:{counts[s]}" '
        f'title="{_e(STATUS_LABELS.get(s, s))} {counts[s]}"></i>'
        for s in present
    )
    status_picks = "".join(
        f'<button type="button" class="pick st-{_e(s)}" data-st="{_e(s)}" '
        f'data-label="{_e(STATUS_LABELS.get(s, s))}" aria-pressed="false">'
        f"{_e(STATUS_LABELS.get(s, s))}<small>{counts[s]}</small></button>"
        for s in present
    )
    kinds_present = [k for k in KINDS if any(c.row["kind"] == k for c in cards)] + sorted(
        {str(c.row["kind"]) for c in cards} - set(KINDS)
    )
    kind_picks = "".join(
        f'<button type="button" class="pick k-{_e(k)}" data-kind="{_e(k)}" '
        f'data-label="{_e(KINDS.get(k, k))}" aria-pressed="false">{_e(KINDS.get(k, k))}'
        f"<small>{sum(1 for c in cards if c.row['kind'] == k)}</small></button>"
        for k in kinds_present
    )
    repos = sorted({str(c.row["repo"]) for c in cards})
    repo_options = "".join(f'<option value="{_e(r)}">{_e(r)}</option>' for r in repos)

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
            )
            for c in members
        )
        groups.append(
            f'<details class="group st-{_e(status)}"{"" if status in FOLDED else " open"}>'
            f"<summary><span>{_e(STATUS_LABELS.get(status, status))}</span>"
            f'<span class="n">{len(members)}</span></summary><div class="grid">{body}</div></details>'
        )

    lease_text = (
        f"调度租约：{_e(lease['holder_session'])}（{_e(lease['machine'])}，到期 {_e(lease['expires_at'])}）"
        if lease is not None and lease["holder_session"]
        else "调度租约：无人持有"
    )
    last = max((t["last_activity"] for t in tasks), default="—")
    order = " ".join(ORDER + tuple(s for s in present if s not in ORDER))
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_e(CSP)}">
<title>TaskSteward 状态</title>
<style>{CSS}{_colors()}</style>
<script>{SCRIPT}</script></head>
<body><main>
<header class="hero"><div><div class="eyebrow">TASKSTEWARD · STATUS</div><h1>TaskSteward 状态</h1></div>
<p class="meta">账本序号 {_e(meta.get("projected_seq", "0"))} · 最近记录 {_e(last)} · {lease_text}</p>
<button type="button" class="ghost" id="refresh" hidden aria-pressed="false" title="每 30 秒重新载入本页">自动刷新</button>
<button type="button" class="ghost" id="style" hidden title="朴素或鲜艳">风格：朴素</button>
<button type="button" class="ghost" id="theme" hidden>主题：跟随系统</button></header>
<section class="tiles">{"".join(tile_html)}</section>
<div class="dist" aria-hidden="true">{dist}</div>
<section class="tools" id="tools" role="search" aria-label="筛选任务" hidden>
<div class="row"><input id="q" type="search" placeholder="搜索编号、标题、仓库、问题、产出…（按 / 聚焦）" aria-label="搜索">
<button type="button" class="ghost" id="mine" aria-pressed="false">只看待用户处理</button>
<label>仓库 <select id="repo"><option value="">全部仓库</option>{repo_options}</select></label>
<label>关注 <select id="flag"><option value="">全部</option><option value="user">待用户处理</option>
<option value="asking">待用户答复</option><option value="tostart">待启动</option>
<option value="paused">暂停</option><option value="alert">需要处理的异常</option></select></label>
<span class="sep"></span>
<label>排序 <select id="sort"><option value="status">状态优先</option><option value="activity">最近活动</option>
<option value="stalest">最久未动</option><option value="newest">编号：新→旧</option>
<option value="oldest">编号：旧→新</option><option value="repo">仓库</option></select></label>
<label>分组 <select id="group"><option value="status">按状态</option><option value="repo">按仓库</option>
<option value="kind">按类型</option><option value="none">不分组</option></select></label></div>
<div class="row"><span class="lbl">状态</span>{status_picks}<span class="lbl">类型</span>{kind_picks}
<span class="sep"></span><span class="count" id="count" role="status" aria-live="polite"></span>
<button type="button" class="ghost" id="open-all">展开分组</button>
<button type="button" class="ghost" id="close-all">折叠分组</button>
<button type="button" class="ghost" id="cards-open">展开详情</button>
<button type="button" class="ghost" id="cards-close">收起详情</button>
<button type="button" class="ghost" id="clear">清除筛选</button></div></section>
<div id="board" data-order="{_e(order)}" data-kinds="{_e(" ".join(KINDS))}">{"".join(groups)}</div>
<p class="empty" id="empty" hidden>没有符合筛选条件的任务。<button type="button" class="ghost" id="clear-empty">清除筛选</button></p>
{"" if cards else '<p class="empty">没有任务</p>'}
<footer class="meta">本页由 tsw 根据账本生成，请勿手工编辑；已归档任务不在此列出（{archived} 个）。</footer>
</main></body>
</html>
"""


def write_status(ctx: CommitContext) -> None:
    ctx.storage.put_artifact("views/STATUS.html", render(ctx.conn).encode("utf-8"))
