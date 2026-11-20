"""The Dashboard app's view of one task: ``/tasks/<task>`` (DN006 §8.10).

A read-only page with more than a card holds: the task's card in the retrieval index (DN007
§5.1, read without touching the index), the requirement and its revisions, every attempt with its
session, the submission and the review rounds, the outputs, dependencies, references
(:mod:`tsw.host.relations`) and resources, the questions and judgments, the follow-ups (DN002
§5.7), the progress notes and the task's ledger events. Its links are
relative (``../`` back to the board, ``<task>`` to a dependency, ``../runs/...`` to a report), so
the page keeps working under a path prefix (DN006 §8.10). It shares the board's head: the same
Content-Security-Policy, token and scripts, so the accept, reject and cancel buttons behave as on
the card.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

from tsw.context.index import IndexedCard, outputs_digest, task_facts
from tsw.context.service import fallback_card
from tsw.events import Event
from tsw.host.relations import label, references
from tsw.host.status import (
    OUTPUT_TYPES,
    PR_TYPES,
    TITLE,
    AppView,
    Revision,
    actions_html,
    brief_html,
    card_notes,
    escape,
    follow_up_anchor,
    link_for,
    page_head,
    pending_asks,
    repo_hue,
    session_link,
)
from tsw.machine.guards import invalidated
from tsw.machine.model import loads
from tsw.tasks import KINDS, STATUS_LABELS, follow_up_of, follow_ups_of

# The newest events shown on the timeline; older ones stay in the ledger.
TIMELINE_LIMIT = 200
NOTES_LIMIT = 50
FOOTER = (
    "本页由 Dashboard 应用（tsw status serve）按账本实时生成；按钮直接写入账本，记为你本人的操作。"
)
REQUEST_STATUS = {"pending": "待答", "resolved": "已答", "superseded": "已取代"}
FOLLOW_UP_STATUS = {"open": "待处理", "converted": "已转为任务", "dismissed": "已驳回"}
FOLLOW_UP_KINDS = {"remaining": "未完成", "out_of_scope": "范围外发现", "idea": "建议"}
SEVERITY = {"high": "高", "medium": "中", "low": "低"}
KEYWORD_SOURCES = {
    "recorded": "主会话记录",
    "legacy": "导入的旧关键词记录",
    "generated": "由标题与任务书生成的初版",
    "missing": "缺失",
}
SUMMARY_SOURCES = {"recorded": "主会话记录", "goal": "以任务目标开头代替", "missing": "缺失"}
INDEX_PROBLEMS = {
    "missing": "检索索引尚未建立",
    "version": "检索索引是其他版本，需要重建",
    "unreadable": "检索索引暂时无法读取（损坏或正在更新）",
    "foreign": "检索索引与当前账本的历史不符，需要重建",
}
REBUILD_HINT = (
    "下次检索（context_catalog 等工具）时会自动建立或更新，也可以在终端运行 tsw context rebuild。"
)


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


def _mapping(value: Any) -> dict[str, Any]:
    """A JSON object read from the projection, or an empty one when the value is anything else."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _rounds(value: Any) -> str:
    """The submission's review rounds: a list of round numbers, or a count in older records."""
    if isinstance(value, list):
        return "、".join(str(n) for n in cast("list[Any]", value)) or "—"
    return "" if value is None else str(value)


def _reviewer(value: Any) -> str:
    """A round's reviewer, recorded as ``{"session": …, "model": …}``."""
    try:
        found = _mapping(loads(value, None))
    except ValueError:  # not JSON: a bare name
        found = {}
    if not found:
        return str(value or "—")
    return " · ".join(str(found[k]) for k in ("session", "model") if found.get(k)) or "—"


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


def _legacy_ref(imported: Mapping[str, Any], ref: str) -> str:
    """Where an imported task's report is now (DN005 §4.2 step 6): read-only history was copied
    under ``legacy/<system>/``; a resumable task's ``runs/`` were copied as they were."""
    if not imported["resumable"] and ref.startswith("runs/"):
        return f"legacy://{imported['source']}/{ref}"
    return ref


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
        legacy = _mapping(loads(imported["legacy"], None))
        for key, label in (
            ("worker_report", "旧系统交付报告"),
            ("review_report", "旧系统验收结论"),
        ):
            ref = legacy.get(key)
            if isinstance(ref, str) and ref:
                items.append((label, _link(_legacy_ref(imported, ref))))
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
        ("评审轮次", escape(_rounds(sub.get("rounds")))),
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
                escape(_reviewer(r["reviewer"])),
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
    found = references(conn)
    titles = {
        str(r["task_id"]): (str(r["title"]), str(r["status"]))
        for r in conn.execute("SELECT task_id, title, status FROM tasks")
    }

    def ref_rows(pairs: list[tuple[str, list[str]]]) -> list[list[str]]:
        rows: list[list[str]] = []
        for other, via in pairs:
            title, status = titles.get(other, ("", ""))
            rows.append(
                [
                    _task_link(other),
                    escape(title),
                    escape(STATUS_LABELS.get(status, status)),
                    escape("、".join(label(v) for v in via)),
                ]
            )
        return rows

    cited = sorted(found.get(task_id, {}).items())
    citing = sorted((a, refs[task_id]) for a, refs in found.items() if task_id in refs)
    head = ("任务", "标题", "状态", "来源")
    return (
        "<h3>依赖</h3>"
        + _table(("任务", "类型", "绑定的 revision", "理由"), deps)
        + "<h3>依赖本任务的任务</h3>"
        + _table(("任务", "类型", "绑定的 revision"), users)
        + "<h3>本任务引用的任务</h3>"
        + _table(head, ref_rows(cited))
        + "<h3>引用本任务的任务</h3>"
        + _table(head, ref_rows(citing))
        + "<h3>资源</h3>"
        + _table(("资源", "占用"), resources)
        + "<h3>集成</h3>"
        + _table(("仓库", "状态"), integrations)
    )


@dataclass(frozen=True)
class CardState:
    """The task's index card as the view shows it, and how current it is (DN006 §8.10, T278)."""

    card: dict[str, Any]
    problem: str | None  # why the index cannot be used (INDEX_PROBLEMS); None when it can
    lagging: bool
    indexed_seq: int
    task_seq: int


def card_state(
    conn: sqlite3.Connection,
    task_id: str,
    found: IndexedCard,
    checksums: Mapping[int, str],
    task_seq: int,
) -> CardState | None:
    """The card to show for the task, or None when the projection has no such task.

    The index is used when it belongs to this ledger: the event at its position has the checksum
    it recorded (so the position is not past the ledger either). When the task has events after
    that position, or no card yet, the structured fields come from the projection and the
    keywords and summary stay as indexed, as the retrieval tools do (DN007 §5.4); the card is
    then stale when the revision or the outputs it was bound to are no longer the task's.
    """
    facts = task_facts(conn, task_id)
    if facts is None:
        return None
    problem = found.problem
    indexed = found.indexed.sequence
    if problem is None and checksums.get(indexed) != found.indexed.checksum:
        problem = "foreign"
    if problem is not None:
        return CardState(fallback_card(facts, None), problem, False, indexed, task_seq)
    if found.card is None or task_seq > indexed:
        card = fallback_card(facts, found.card)
        bound = _mapping(card.get("card_revision"))
        if bound and not bound.get("stale"):
            outputs = outputs_digest(facts.outputs)[: len(str(bound.get("outputs") or ""))]
            if bound.get("task_revision") != facts.task["revision"] or (
                bound.get("outputs") is not None and bound.get("outputs") != outputs
            ):
                card["card_revision"] = {**bound, "stale": True}
        return CardState(card, None, True, indexed, task_seq)
    return CardState(found.card, None, False, indexed, task_seq)


def _stale_reason(card: Mapping[str, Any], revision: int) -> str:
    bound = _mapping(card.get("card_revision"))
    if card.get("keyword_source") == "generated":
        return "关键词是由标题与任务书生成的初版，等主会话记录"
    if bound.get("task_revision") not in (None, revision):
        return f"卡片记录于 revision {bound['task_revision']}，任务现在是 revision {revision}"
    return "卡片记录后任务的 revision 或产出有变化"


def _strings(value: Any) -> list[str]:
    return [str(x) for x in cast("list[Any]", value)] if isinstance(value, list) else []


def _card(state: CardState | None, revision: int) -> str:
    if state is None:
        return '<p class="none">没有读取检索索引</p>'
    card = state.card
    if state.problem:
        status = (
            f'<p class="note warn"><b>{escape(INDEX_PROBLEMS.get(state.problem, state.problem))}'
            f"</b>：{escape(REBUILD_HINT)}以下只有由账本当前状态生成的结构字段。</p>"
        )
    elif state.lagging:
        status = (
            f'<p class="note warn"><b>索引落后</b>：索引到账本序号 {state.indexed_seq}，'
            f"本任务最近记录序号 {state.task_seq}。结构字段取账本当前状态，关键词与摘要沿用索引中的"
            f"版本；{escape(REBUILD_HINT)}</p>"
        )
    else:
        status = f'<p class="meta">索引到账本序号 {state.indexed_seq}，含本任务的最新记录。</p>'
    keywords = _strings(card.get("keywords"))
    chips = "".join(f'<span class="chip kw">{escape(k)}</span>' for k in keywords) or "—"
    source = str(card.get("keyword_source") or "missing")
    summary_source = str(card.get("summary_source") or "missing")
    bound = _mapping(card.get("card_revision"))
    stale = bool(bound.get("stale"))
    outcomes = [
        _link(o.get("ref"), o.get("label") or o.get("ref"))
        for o in (_mapping(x) for x in _list(card.get("outcomes")))
    ]
    coverage = card.get("keyword_coverage")
    if isinstance(coverage, dict):
        coverage = "；".join(f"{k}：{v}" for k, v in _mapping(coverage).items())
    deps = _strings(card.get("dependencies"))
    hard = set(_strings(card.get("hard_dependencies")))
    summary = str(card.get("summary") or "")
    items = [
        ("关键词", f'<span class="kws">{chips}</span>'),
        ("关键词来源", escape(KEYWORD_SOURCES.get(source, source))),
        ("关键词覆盖", escape(coverage or "")),
        ("摘要", f'<span class="clip">{escape(summary)}</span>' if summary else "—"),
        ("摘要来源", escape(SUMMARY_SOURCES.get(summary_source, summary_source))),
        ("标签", escape("、".join(_strings(card.get("tags"))))),
        ("项目", escape("、".join(_strings(card.get("project_ids"))))),
        (
            "stale",
            f"是：{escape(_stale_reason(card, revision))}"
            if stale
            else "否"
            if bound
            else "未知（索引中还没有这张卡片）",
        ),
        (
            "卡片绑定",
            escape(
                f"revision {bound.get('task_revision')} · 产出摘要 {bound.get('outputs') or '—'}"
            )
            if bound
            else "",
        ),
        (
            "依赖",
            " ".join(
                _task_link(d) + ("" if d in hard else " <small>（推断）</small>") for d in deps
            ),
        ),
        ("产出", "<br>".join(outcomes)),
        ("只读的导入任务", "是" if card.get("imported") and not card.get("resumable") else ""),
    ]
    return status + _facts(items)


def _list(value: Any) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def _follow_up_source(item: Mapping[str, Any]) -> str:
    by = _mapping(item["recorded_by"])
    who = "worker" if by.get("role") == "worker" else "主会话"
    if by.get("attempt"):
        who += f" {by['attempt']}"
    parts = [f"revision {escape(item['revision'])} · {escape(who)}", _time(item["recorded_at"])]
    source = _mapping(item["source"])
    if source:
        parts.append(
            f"用户原话（{escape(source.get('message_ref') or source.get('channel') or '')}）："
            f'<span class="clip">{escape(source.get("text") or "")}</span>'
        )
    return "<br>".join(parts)


def _follow_up_state(item: Mapping[str, Any]) -> str:
    status = str(item["status"])
    out = escape(FOLLOW_UP_STATUS.get(status, status))
    resolution = _mapping(item["resolution"])
    if status == "converted" and item["converted_to"]:
        mode = "并入" if resolution.get("mode") == "existing" else "转为"
        out += f"<br>{mode} {_task_link(str(item['converted_to']))}"
    if resolution.get("reason"):
        out += f"<br>理由：{escape(resolution['reason'])}"
    if status == "open" and item["recheck_after"]:
        out += f"<br>暂不处理，复查日期 {escape(item['recheck_after'])}"
    if resolution.get("at"):
        out += f"<br>{_time(resolution['at'])}"
    return out


def _follow_ups(conn: sqlite3.Connection, task_id: str) -> tuple[str, int, int]:
    """The task's follow-ups, open ones first, and the follow-ups it was made from (DN002 §5.7)."""
    found = follow_ups_of(conn, task_id)
    found.sort(key=lambda f: (f["status"] != "open", f["recorded_seq"]))
    rows = "".join(
        f'<tr id="{escape(follow_up_anchor(str(f["follow_up_id"])))}" '
        f'data-status="{escape(f["status"])}">'
        f"<td>{escape(f['follow_up_id'])}</td>"
        f"<td>{escape(FOLLOW_UP_KINDS.get(str(f['kind']), f['kind']))}"
        + (
            f"<br>严重度 {escape(SEVERITY.get(str(f['severity']), f['severity']))}"
            if f["severity"]
            else ""
        )
        + f"</td><td><b>{escape(f['title'])}</b>"
        f'<p class="pre">{escape(f["detail"])}</p></td>'
        f"<td>{_follow_up_source(f)}</td><td>{_follow_up_state(f)}</td></tr>"
        for f in found
    )
    heads = "".join(f"<th>{h}</th>" for h in ("编号", "类别", "事项", "来源", "状态与去向"))
    table = (
        '<div class="tbl"><table><thead><tr>'
        f"{heads}</tr></thead><tbody>{rows}</tbody></table></div>"
        if found
        else '<p class="none">无</p>'
    )
    made = follow_up_of(conn, task_id)
    origin = ""
    if made:
        links = "".join(
            f'<li><a href="{escape(quote(str(m["task_id"]), safe=""))}#'
            f'{escape(follow_up_anchor(str(m["follow_up_id"])))}">{escape(m["follow_up_id"])}</a>'
            f" {escape(m['title'])}</li>"
            for m in made
        )
        origin = f"<h3>本任务来自</h3><ul>{links}</ul>"
    pending = sum(1 for f in found if f["status"] == "open")
    return origin + table, len(found), pending


def _answer(resolution: str | None) -> str:
    """The answer and where it came from: a verified source, or the system it was imported from."""
    found = _mapping(loads(resolution, None))
    if not found:
        return "—"
    parts = [escape(found.get("answer") or "")]
    source = found.get("source")
    if isinstance(source, dict):
        verified = _mapping(source)
        words = verified.get("text")
        if words:
            channel = escape(verified.get("channel") or "")
            parts.append(f"<small>{channel} · 原话：{escape(words)}</small>")
    elif source:
        parts.append(f"<small>来自 {escape(source)}</small>")
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
    conn: sqlite3.Connection,
    task_id: str,
    app: AppView,
    steps: list[Step],
    indexed: CardState | None = None,
) -> str | None:
    """The task's own page, or None when the projection has no such task.

    ``indexed`` is the task's index card (:func:`card_state`); without it the section says that
    the index was not read.
    """
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
    follow_ups, n_follow_ups, n_open = _follow_ups(conn, task_id)
    sub = f" <small>· {escape(row['substate'])}</small>" if row["substate"] else ""
    session = session_link(task_id, current)
    sections = [
        _section("overview", "概况", _overview(row, dict(imported) if imported else None)),
        _section("card", "检索卡片", _card(indexed, int(row["revision"]))),
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
        _section("relations", "关系与资源", _relations(conn, row)),
        _section(
            "questions",
            "问题与判断" + (f"（待答 {n_pending}）" if n_pending else ""),
            questions,
            n_questions,
        ),
        _section(
            "follow-ups",
            "follow-up" + (f"（待处理 {n_open}）" if n_open else ""),
            follow_ups,
            n_follow_ups,
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
{page_head(f"{task_id} · {TITLE}", app)}
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
{page_head(f"没有这个任务 · {TITLE}", app)}
<body><main class="task-view">
<nav class="crumbs"><a class="back" href="../">← 返回看板</a></nav>
<p class="empty">没有任务 {escape(task_id)}。</p>
</main></body>
</html>
"""
