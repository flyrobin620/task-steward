"""The Dashboard app's standing-preferences page: ``/preferences`` (DN006 §8.10, DN007 §6.2).

Every standing preference, read from the projection on each request (the same reading as
``tsw preference list --all``), grouped by scope and listed by topic within a scope: the words as
recorded, whether it is in force, replaced, revoked or overridden in a narrower scope, what it
replaces and what replaced or overrides it, and where it came from. A filter in the address
(``?scope=<scope>``, ``all=1``) is applied here, so it needs no script and can be bookmarked.

The page also holds the user's own management: a form to record a preference, and on every
preference in force a form to replace it and one to revoke it. The forms are rendered here and
hidden until opened; the app's script (``status_app.js``) asks the user to confirm and posts them to
``/api/preferences/...``, where the app runs ``preference_record`` / ``preference_revoke`` as the
``user`` caller on the ``status-app`` channel (:meth:`tsw.host.status_app.StatusApp.preference`).
Every check is the tool's; the page only offers what the rules allow (DN007 §6.2).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode

from tsw import preferences
from tsw.host.status import TITLE, AppView, escape, page_head

PATH = "preferences"
CHANNELS = {
    "main-session": "主会话",
    "in-session": "会话内",
    "teams-chat": "Teams",
    "hub-portal": "Hub Portal",
    "user-cli": "终端（user-cli）",
    "status-app": "Dashboard（status-app）",
}
# The choice of the scope menus that lets the user type a scope of their own.
OTHER = "__other__"
FOOTER = (
    "本页由 Dashboard 应用（tsw status serve）按账本实时生成；记录、替代、撤销直接写入账本，"
    "记为你本人的操作（渠道 status-app），历史只追加、不改写。"
)


@dataclass(frozen=True)
class Filter:
    """What the address asks for: one scope (empty for all) and whether to list every one."""

    scope: str = ""
    all: bool = False

    def query(self, **change: Any) -> str:
        """The relative address of this page with ``change`` applied."""
        scope = change.get("scope", self.scope)
        every = change.get("all", self.all)
        params = {k: v for k, v in (("scope", scope), ("all", "1" if every else "")) if v}
        return PATH + ("?" + urlencode(params) if params else "")


def parse_filter(query: str) -> Filter:
    """The filter of a request's query string; anything else in it is ignored."""
    params = parse_qs(query)
    return Filter(
        scope=(params.get("scope") or [""])[0].strip(),
        all=(params.get("all") or [""])[0] in ("1", "true", "yes"),
    )


def scope_key(scope: str) -> tuple[int, str]:
    """Scopes in page order: global, then repositories, then projects, then anything else."""
    kind, _, name = scope.partition(":")
    rank = {"global": 0, "repo": 1, "project": 2}.get(kind, 3)
    return (rank, name if rank else "")


def scope_label(scope: str) -> str:
    kind, _, name = scope.partition(":")
    if scope == "global":
        return "全局"
    if kind == "repo":
        return f"仓库 {name}"
    if kind == "project":
        return f"项目 {name}"
    return scope


def _topic_key(item: Mapping[str, Any]) -> tuple[str, int]:
    ref = str(item["ref"])
    number = ref[2:] if ref.startswith("P-") else ""
    return (" ".join(str(item["topic"]).split()).casefold(), int(number) if number.isdigit() else 0)


def actionable(item: Mapping[str, Any]) -> bool:
    """Whether a preference can still be replaced or revoked: in force in its own scope."""
    return not item.get("superseded_by") and not item.get("revoked")


def conflicts(items: Iterable[Mapping[str, Any]]) -> list[tuple[str, list[str]]]:
    """Topics of one scope with two or more preferences in force (DN007 §6: the user decides).

    Two preferences in force in the same scope never replace one another (a replacement marks
    the old one replaced), so every such pair is unresolved.
    """
    by_topic: dict[str, list[Mapping[str, Any]]] = {}
    for item in items:
        if actionable(item):
            by_topic.setdefault(" ".join(str(item["topic"]).split()).casefold(), []).append(item)
    return [
        (str(found[0]["topic"]), [str(i["ref"]) for i in found])
        for found in by_topic.values()
        if len(found) > 1
    ]


def _anchor(ref: Any) -> str:
    return f"pref-{ref}"


def _ref_link(ref: Any) -> str:
    return f'<a href="#{escape(_anchor(ref))}">{escape(ref)}</a>'


def _state(item: Mapping[str, Any]) -> tuple[str, str]:
    """The state's class and words."""
    revoked = item.get("revoked")
    if revoked:
        return "revoked", f"已撤销（{escape(revoked.get('time') or '')}）"
    if item.get("superseded_by"):
        return "replaced", f"已被 {_ref_link(item['superseded_by'])} 替代"
    return "current", "现行"


def _relations(item: Mapping[str, Any], scopes: Mapping[str, str]) -> list[str]:
    out: list[str] = []
    replaced: list[Any] = list(item.get("supersedes") or [])
    for old in replaced:
        old_scope = scopes.get(str(old), "")
        if old_scope == item["scope"]:
            out.append(f"替代 {_ref_link(old)}")
        else:
            out.append(
                f"在本范围内覆盖 {_ref_link(old)}（{escape(scope_label(old_scope) or '?')}）"
            )
    if item.get("superseded_by"):
        out.append(f"被 {_ref_link(item['superseded_by'])} 替代")
    overrides: list[Mapping[str, Any]] = list(item.get("overridden_in") or [])
    for over in overrides:
        out.append(
            f"在{escape(scope_label(str(over['scope'])))}中"
            f"被更窄范围的 {_ref_link(over['ref'])} 覆盖"
        )
    return out


def _source(source: Mapping[str, Any], text: str, when: Any, seq: Any, head: str) -> str:
    channel = str(source.get("channel") or "")
    parts = [
        f"{escape(head)}：{escape(CHANNELS.get(channel, channel) or '—')}",
        escape(source.get("principal") or ""),
        f"<code>{escape(source.get('message_ref'))}</code>" if source.get("message_ref") else "",
        f'<time datetime="{escape(when)}">{escape(when)}</time>' if when else "",
        f"账本序号 {escape(seq)}" if seq is not None else "",
    ]
    line = " · ".join(p for p in parts if p)
    words = str(source.get("text") or "")
    if words and words.strip() != text.strip():
        line += (
            '<details class="more"><summary>来源原话</summary>'
            f'<p class="pref-words">{escape(words)}</p></details>'
        )
    return f'<div class="pref-src">{line}</div>'


def scope_choices(scopes: Iterable[str], repos: Iterable[str]) -> list[str]:
    """The scopes the record form offers: global, every repository with tasks and every scope
    that already has a preference."""
    found = {"global", *scopes, *(f"repo:{r}" for r in repos if r)}
    return sorted(found, key=scope_key)


def _options(choices: Sequence[str], chosen: str, other: bool = True) -> str:
    out = "".join(
        f'<option value="{escape(c)}"{" selected" if c == chosen else ""}>{escape(scope_label(c))}'
        + (f"（{escape(c)}）" if c != "global" else "")
        + "</option>"
        for c in choices
    )
    if other:
        out += f'<option value="{OTHER}">其他（手填）</option>'
    return out


def _scope_fields(choices: Sequence[str], chosen: str, other: bool = True) -> str:
    fields = f'<label>范围 <select name="scope">{_options(choices, chosen, other)}</select></label>'
    if other:
        fields += (
            '<label>其他范围 <input name="scope_other" placeholder="repo:<仓库> 或 project:<项目>" '
            'autocomplete="off"></label>'
        )
    return fields


def replace_choices(scope: str, choices: Sequence[str]) -> list[str]:
    """Where a replacement may go: the same scope, or (for a global one) a narrower one."""
    if scope != "global":
        return [scope]
    return [scope, *(c for c in choices if c != "global")]


def _record_form(choices: Sequence[str]) -> str:
    return (
        '<section class="panel pref-new" id="record"><h2>记录新偏好</h2>'
        '<div class="pref-acts" data-pref-ref="">'
        '<div class="pref-form" data-form="record">'
        f"{_scope_fields(choices, 'global')}"
        '<label>主题 <input name="topic" placeholder="如：画图、邮件发送、PR 默认状态" '
        'autocomplete="off"></label>'
        '<label class="wide">原文 <textarea name="text" rows="3" '
        'placeholder="按你的原意写清楚：以后在什么情况下怎样做"></textarea></label>'
        '<div class="pref-buttons"><button type="button" class="act act-accept" '
        'data-pref-send="record">记录</button>'
        '<span class="act-result" role="status" aria-live="polite"></span></div>'
        "</div></div>"
        '<p class="meta">先弹出确认框；记录后立即生效，下一次受理、派发或生成任务书即使用'
        "（DN007 §6.1）。要修改一条已有的偏好，用它的「替代」。</p></section>"
    )


def _item_forms(item: Mapping[str, Any], choices: Sequence[str]) -> str:
    scope = str(item["scope"])
    where = replace_choices(scope, choices)
    narrower = (
        "；也可以改为更窄的仓库或项目范围：新偏好只在该范围内优先，原偏好在其他范围仍然有效"
        if len(where) > 1
        else ""
    )
    return (
        f'<div class="acts pref-acts" data-pref-ref="{escape(item["ref"])}" '
        f'data-pref-scope="{escape(scope)}">'
        '<button type="button" class="act act-replace" data-pref-open="replace">替代</button>'
        '<button type="button" class="act act-cancel" data-pref-open="revoke">撤销</button>'
        '<span class="act-result" role="status" aria-live="polite"></span>'
        '<div class="pref-form" data-form="replace" hidden>'
        f'<p class="meta">新偏好得到新编号；范围不变时 {escape(item["ref"])} 标为已替代'
        f"{escape(narrower)}。</p>"
        f"{_scope_fields(where, scope, other=len(where) > 1)}"
        f'<label>主题 <input name="topic" value="{escape(item["topic"])}" '
        'autocomplete="off"></label>'
        f'<label class="wide">原文 <textarea name="text" rows="3">{escape(item["text"])}</textarea>'
        "</label>"
        '<div class="pref-buttons"><button type="button" class="act act-accept" '
        'data-pref-send="replace">提交替代</button>'
        '<button type="button" class="act act-cancel" data-pref-close="">收起</button></div></div>'
        '<div class="pref-form" data-form="revoke" hidden>'
        '<label class="wide">撤销理由 <textarea name="reason" rows="2" '
        'placeholder="为什么不再适用；会原样记入账本"></textarea></label>'
        '<div class="pref-buttons"><button type="button" class="act act-reject-send" '
        'data-pref-send="revoke">提交撤销</button>'
        '<button type="button" class="act act-cancel" data-pref-close="">收起</button></div></div>'
        "</div>"
    )


def _item(
    item: Mapping[str, Any],
    scopes: Mapping[str, str],
    revocations: Mapping[str, Mapping[str, Any]],
    choices: Sequence[str],
) -> str:
    ref = str(item["ref"])
    cls, state = _state(item)
    revoked = item.get("revoked")
    notes = ""
    if revoked:
        notes += f'<div class="note revoked"><b>撤销理由</b>：{escape(revoked.get("reason"))}</div>'
    relations = _relations(item, scopes)
    rel = f'<p class="pref-rel">{"；".join(relations)}</p>' if relations else ""
    sources = _source(
        item.get("source") or {},
        str(item["text"]),
        item.get("time"),
        item.get("source_seq"),
        "记录",
    )
    gone = revocations.get(ref)
    if gone:
        sources += _source(
            gone.get("source") or {}, "", gone.get("time"), gone.get("source_seq"), "撤销"
        )
    forms = _item_forms(item, choices) if actionable(item) else ""
    return (
        f'<article class="pref {cls}" id="{escape(_anchor(ref))}" data-pref="{escape(ref)}">'
        f'<div class="top"><a class="tid" href="#{escape(_anchor(ref))}">{escape(ref)}</a>'
        f'<span class="chip topic">{escape(item["topic"])}</span>'
        f'<span class="pref-state {cls}">{state}</span></div>'
        f'<p class="pref-text">{escape(item["text"])}</p>{notes}{rel}{sources}{forms}</article>'
    )


def render_preferences(conn: sqlite3.Connection, app: AppView, chosen: Filter | None = None) -> str:
    """The standing-preferences page for ``chosen`` (default: every scope, those in force)."""
    chosen = chosen or Filter()
    every = preferences.load(conn, None)
    revocations = preferences.revocations(conn)
    scopes = {str(i["ref"]): str(i["scope"]) for i in every}
    repos = [str(r["repo"]) for r in conn.execute("SELECT DISTINCT repo FROM tasks")]
    choices = scope_choices(scopes.values(), repos)
    in_force = [i for i in every if preferences.in_force(i)]
    shown = [
        i
        for i in (every if chosen.all else in_force)
        if not chosen.scope or i["scope"] == chosen.scope
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in shown:
        groups.setdefault(str(item["scope"]), []).append(item)
    sections: list[str] = []
    for scope in sorted(groups, key=scope_key):
        items = sorted(groups[scope], key=_topic_key)
        warn = "".join(
            f'<div class="note conflict"><b>同主题有 {len(refs)} 条现行偏好</b>：'
            f"「{escape(topic)}」"
            f"（{'、'.join(_ref_link(r) for r in refs)}）彼此没有替代关系，"
            "请用「替代」或「撤销」只留下一条（DN007 §6）。</div>"
            for topic, refs in conflicts(i for i in every if i["scope"] == scope)
        )
        body = "".join(_item(i, scopes, revocations, choices) for i in items)
        sections.append(
            f'<section class="panel pref-group" data-scope="{escape(scope)}">'
            f"<h2>{escape(scope_label(scope))}"
            f"{f' <code>{escape(scope)}</code>' if scope != 'global' else ''}"
            f'<span class="n">{len(items)}</span></h2>{warn}{body}</section>'
        )
    known = sorted({str(i["scope"]) for i in every}, key=scope_key)
    picks = "".join(
        f'<a class="ghost" href="{escape(chosen.query(scope=s))}"'
        + (' aria-current="true"' if s == chosen.scope else "")
        + f">{escape(scope_label(s) if s else '全部范围')}</a>"
        for s in ["", *known]
    )
    toggle = (
        f'<a class="ghost" href="{escape(chosen.query(all=not chosen.all))}"'
        f' aria-pressed="{str(chosen.all).lower()}">'
        f"{'只看现行' if chosen.all else '含已替代、已撤销'}</a>"
    )
    counts = (
        f"现行 {len(in_force)} 条 · 共 {len(every)} 条（含已替代、已撤销）"
        f" · 本页列出 {len(shown)} 条"
    )
    if shown:
        listing = "".join(sections)
    elif every:
        listing = '<p class="empty">没有符合筛选条件的偏好。</p>'
    else:
        listing = '<p class="empty">还没有长期偏好。</p>'
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
{page_head(f"长期偏好 · {TITLE}", app)}
<body><main class="task-view pref-view" data-tsw-page="preferences">
<nav class="crumbs"><a class="back" href="./">← 返回看板</a></nav>
<header class="hero"><div><div class="eyebrow">TASKSTEWARD · DASHBOARD</div><h1>长期偏好</h1></div>
<p class="meta">{escape(counts)}。偏好是账本中的数据，规则见 DN007 §6：修改即替代（新编号），
删除即撤销，历史都保留；更窄范围的偏好只在该范围内优先。</p></header>
<nav class="row pref-filter" aria-label="筛选偏好">{picks}<span class="sep"></span>{toggle}</nav>
{_record_form(choices)}
{listing}
<footer class="meta">{FOOTER}</footer>
</main></body>
</html>
"""
