"""The Dashboard's standing-preferences page rendered from an in-memory projection (UC-DIRECT-007,
DN006 §8.10, DN007 §6.2, T290)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from tsw.host.preference_view import (
    OTHER,
    Filter,
    conflicts,
    parse_filter,
    render_preferences,
    replace_choices,
    scope_choices,
)
from tsw.host.status import AppView, render
from tsw.host.status_assets import APP_CSP, APP_SCRIPT
from tsw.projection import SCHEMA_SQL

mark = pytest.mark
APP = AppView(token="tok", actions={})


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def _source(channel: str, ref: str, text: str, target: list[Any]) -> str:
    return json.dumps(
        {
            "channel": channel,
            "principal": "owner@box",
            "message_ref": ref,
            "text": text,
            "target": target,
        },
        ensure_ascii=False,
    )


def _pref(
    db: sqlite3.Connection,
    n: int,
    scope: str,
    topic: str,
    text: str,
    *,
    supersedes: list[str] | None = None,
    superseded_by: str | None = None,
    channel: str = "main-session",
    words: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO user_decisions(ref, task_id, kind, topic, text, scope, verified_source, "
        "supersedes, superseded_by, recorded_at, source_seq) "
        "VALUES(?, NULL, 'standing', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"P-{n}",
            topic,
            text,
            scope,
            _source(
                channel,
                f"m-{n}",
                text if words is None else words,
                ["preference", scope, "standing"],
            ),
            json.dumps(supersedes or []),
            superseded_by,
            f"2026-11-15T0{n}:00:00Z",
            100 + n,
        ),
    )


def _revoke(db: sqlite3.Connection, ref: str, reason: str) -> None:
    db.execute(
        "INSERT INTO preference_revocations(ref, reason, verified_source, revoked_at, source_seq) "
        "VALUES(?, ?, ?, ?, ?)",
        (
            ref,
            reason,
            _source("status-app", "status-app:abcd:1", reason, ["preference", ref, "revoke"]),
            "2026-11-16T01:00:00Z",
            200,
        ),
    )


def _item(page: str, ref: str) -> str:
    match = re.search(rf'<article class="pref [^"]*" id="pref-{ref}".*?</article>', page, re.S)
    assert match, ref
    return match.group(0)


def _groups(page: str) -> list[str]:
    return re.findall(r'<section class="panel pref-group" data-scope="([^"]+)"', page)


@pytest.fixture
def prefs(conn: sqlite3.Connection) -> sqlite3.Connection:
    """P-1 global replaced by P-2; P-3 global overridden in repo:demo by P-4; P-5 revoked;
    P-6 and P-7 two unrelated preferences in force on one topic of repo:demo."""
    conn.execute(
        "INSERT INTO tasks(task_id, title, repo, kind, status, revision, version, spec_path, "
        "spec_sha256, registered_seq, last_activity, source_seq) VALUES('T001', 't', 'other', "
        "'code', 'intake', 1, 1, 'x', 'x', 1, '2026-11-15T00:00:00Z', 1)"
    )
    _pref(conn, 1, "global", "画图", "旧的画图规则", superseded_by="P-2")
    _pref(conn, 2, "global", "画图", "用 diagram skill\n第二行", supersedes=["P-1"], words="原话 A")
    _pref(conn, 3, "global", "PR", "PR 默认 ready")
    _pref(conn, 4, "repo:demo", "PR", "demo 的 PR 先开 draft", supersedes=["P-3"])
    _pref(conn, 5, "project:x", "邮件", "<b>只存草稿</b>", channel="user-cli")
    _revoke(conn, "P-5", "不再需要")
    _pref(conn, 6, "repo:demo", "合并", "squash", channel="status-app")
    _pref(conn, 7, "repo:demo", "合并", "rebase")
    return conn


@mark.use_case("UC-DIRECT-007")
def test_the_page_lists_preferences_by_scope_and_topic_with_their_state(
    prefs: sqlite3.Connection,
) -> None:
    page = render_preferences(prefs, APP, Filter(all=True))
    assert _groups(page) == ["global", "repo:demo", "project:x"]
    demo = page.split('data-scope="repo:demo"')[1].split("</section>")[0]
    # Within a scope, by topic (PR, 合并) and then by number.
    assert re.findall(r'id="pref-(P-\d+)"', demo) == ["P-4", "P-6", "P-7"]
    # The words as recorded, line break kept, escaped.
    assert "用 diagram skill\n第二行" in _item(page, "P-2")
    assert "&lt;b&gt;只存草稿&lt;/b&gt;" in _item(page, "P-5") and "<b>只存草稿" not in page
    # States and relations, with in-page links.
    p1 = _item(page, "P-1")
    assert 'class="pref replaced"' in p1 and '已被 <a href="#pref-P-2">P-2</a> 替代' in p1
    assert '替代 <a href="#pref-P-1">P-1</a>' in _item(page, "P-2")
    p3 = _item(page, "P-3")
    assert 'class="pref current"' in p3
    assert '在仓库 demo中被更窄范围的 <a href="#pref-P-4">P-4</a> 覆盖' in p3
    assert '在本范围内覆盖 <a href="#pref-P-3">P-3</a>（全局）' in _item(page, "P-4")
    p5 = _item(page, "P-5")
    assert 'class="pref revoked"' in p5 and "已撤销（2026-11-16T01:00:00Z）" in p5
    assert "<b>撤销理由</b>：不再需要" in p5
    # Sources: channel, recorder, message, time and sequence; the words when they differ.
    p2 = _item(page, "P-2")
    assert "记录：主会话 · owner@box · <code>m-2</code>" in p2 and "账本序号 102" in p2
    assert "<summary>来源原话</summary>" in p2 and "原话 A" in p2
    assert "来源原话" not in p3
    assert "记录：终端（user-cli）" in p5 and "撤销：Dashboard（status-app）" in p5
    assert "<code>status-app:abcd:1</code>" in p5 and "账本序号 200" in p5
    assert "记录：Dashboard（status-app）" in _item(page, "P-6")


@mark.use_case("UC-DIRECT-007")
def test_only_preferences_in_force_offer_replace_and_revoke(prefs: sqlite3.Connection) -> None:
    page = render_preferences(prefs, APP, Filter(all=True))
    for ref in ("P-2", "P-3", "P-4", "P-6", "P-7"):
        item = _item(page, ref)
        assert f'data-pref-ref="{ref}"' in item, ref
        assert 'data-pref-open="replace"' in item and 'data-pref-open="revoke"' in item, ref
        assert '<div class="pref-form" data-form="replace" hidden>' in item, ref
    for ref in ("P-1", "P-5"):
        assert "data-pref-open" not in _item(page, ref), ref
    # A replacement starts from the words, topic and scope it replaces.
    p2 = _item(page, "P-2")
    assert '<input name="topic" value="画图"' in p2
    assert '<textarea name="text" rows="3">用 diagram skill\n第二行</textarea>' in p2
    # A global one may move to a narrower scope; a repository one only stays in its own.
    assert '<option value="repo:demo">仓库 demo（repo:demo）</option>' in p2
    assert f'<option value="{OTHER}">' in p2
    p4 = _item(page, "P-4")
    assert '<option value="repo:demo" selected>' in p4
    assert (
        "<option" in p4 and "global" not in p4.split('data-form="replace"')[1].split("</select>")[0]
    )
    assert OTHER not in p4
    # The record form offers every scope there is, every repository with tasks, and "other".
    record = page.split('id="record"')[1].split("</section>")[0]
    for scope in ("global", "repo:demo", "repo:other", "project:x"):
        assert f'<option value="{scope}"' in record, scope
    assert 'data-pref-send="record"' in record and 'name="scope_other"' in record


@mark.use_case("UC-DIRECT-007")
def test_one_topic_with_two_preferences_in_force_is_flagged(prefs: sqlite3.Connection) -> None:
    page = render_preferences(prefs, APP)
    demo = page.split('data-scope="repo:demo"')[1].split("</section>")[0]
    assert "同主题有 2 条现行偏好" in demo and "「合并」" in demo
    assert '<a href="#pref-P-6">P-6</a>、<a href="#pref-P-7">P-7</a>' in demo
    # A replaced or revoked one is not a conflict.
    assert "同主题有" not in page.split('data-scope="global"')[1].split("</section>")[0]
    rows: list[dict[str, Any]] = [
        {"ref": "P-1", "topic": "a", "superseded_by": "P-2"},
        {"ref": "P-2", "topic": " A ", "superseded_by": None},
        {"ref": "P-3", "topic": "a", "revoked": {"time": "t", "reason": "r"}},
    ]
    assert conflicts(rows) == []
    assert conflicts([*rows, {"ref": "P-4", "topic": "a"}]) == [(" A ", ["P-2", "P-4"])]


@mark.use_case("UC-DIRECT-007")
def test_the_filter_is_in_the_address_and_applied_on_the_server(prefs: sqlite3.Connection) -> None:
    assert parse_filter("") == Filter()
    assert parse_filter("scope=repo%3Ademo&all=1&x=y") == Filter("repo:demo", True)
    assert parse_filter("all=0") == Filter()
    assert Filter().query(scope="repo:demo") == "preferences?scope=repo%3Ademo"
    assert Filter("global").query(all=True) == "preferences?scope=global&all=1"
    assert Filter("global", True).query(scope="") == "preferences?all=1"
    # By default only those in force, like tsw preference list.
    page = render_preferences(prefs, APP)
    shown = re.findall(r'<article class="pref [^"]*" id="pref-(P-\d+)"', page)
    assert sorted(shown) == ["P-2", "P-3", "P-4", "P-6", "P-7"]
    assert "现行 5 条 · 共 7 条（含已替代、已撤销） · 本页列出 5 条" in page
    assert _groups(page) == ["global", "repo:demo"]
    # One scope, with or without the history.
    page = render_preferences(prefs, APP, Filter("global", True))
    assert re.findall(r'id="pref-(P-\d+)"', page) == ["P-3", "P-1", "P-2"]
    assert '<a class="ghost" href="preferences?scope=global&amp;all=1" aria-current="true">' in page
    assert (
        '<a class="ghost" href="preferences?scope=global" aria-pressed="true">只看现行</a>' in page
    )
    page = render_preferences(prefs, APP, Filter("project:x"))
    assert "没有符合筛选条件的偏好。" in page and "pref-P-5" not in page


@mark.use_case("UC-DIRECT-007")
def test_the_page_shares_the_apps_head_and_finds_its_way_back(conn: sqlite3.Connection) -> None:
    page = render_preferences(conn, APP)
    assert "还没有长期偏好。" in page and "现行 0 条" in page
    assert '<meta name="tsw-token" content="tok">' in page
    assert f'content="{APP_CSP}"'.replace("'", "&#x27;") in page
    assert '<a class="back" href="./">← 返回看板</a>' in page
    assert '<main class="task-view pref-view" data-tsw-page="preferences">' in page
    assert "<title>长期偏好 · TaskSteward Dashboard</title>" in page
    # The script posts to the app's own preference endpoints and confirms first.
    assert "/api/preferences/record" in APP_SCRIPT and "/revoke`" in APP_SCRIPT
    assert APP_SCRIPT.count("window.confirm(") >= 2
    assert "</script" not in APP_SCRIPT.lower()


@mark.use_case("UC-DIRECT-007")
def test_the_board_links_the_page_in_the_app_only(prefs: sqlite3.Connection) -> None:
    board = render(prefs, AppView(token="tok", actions={}))
    assert '<a class="ghost" id="prefs" href="preferences"' in board and "长期偏好 5</a>" in board
    static = render(prefs)
    assert 'id="prefs"' not in static and "tsw preference list" in static


def test_the_scope_choices() -> None:
    choices = scope_choices(["project:x", "repo:b", "global"], ["a", "", "b"])
    assert choices == ["global", "repo:a", "repo:b", "project:x"]
    assert replace_choices("global", choices) == choices
    assert replace_choices("repo:a", choices) == ["repo:a"]
    assert replace_choices("project:x", choices) == ["project:x"]


@mark.use_case("UC-DIRECT-007")
def test_links_to_preferences_left_out_by_the_filter_open_the_full_list(
    prefs: sqlite3.Connection,
) -> None:
    # By default P-1 (replaced) is not listed: its link opens the list of every preference.
    page = render_preferences(prefs, APP)
    assert "pref-P-1" not in re.findall(r'<article [^>]*id="(pref-P-\d+)"', page)
    p2 = _item(page, "P-2")
    assert '替代 <a href="preferences?all=1#pref-P-1">P-1</a>' in p2
    # With every one listed, the same link stays on the page.
    assert '替代 <a href="#pref-P-1">P-1</a>' in _item(
        render_preferences(prefs, APP, Filter(all=True)), "P-2"
    )
    # Filtered to one scope, a link to another scope's preference leaves the filter too.
    page = render_preferences(prefs, APP, Filter("repo:demo"))
    assert '在本范围内覆盖 <a href="preferences?all=1#pref-P-3">P-3</a>' in _item(page, "P-4")
    page = render_preferences(prefs, APP, Filter("global"))
    assert '被更窄范围的 <a href="preferences?all=1#pref-P-4">P-4</a> 覆盖' in _item(page, "P-3")
    # Links within the listed ones (the conflict note) stay on the page.
    demo = render_preferences(prefs, APP, Filter("repo:demo"))
    assert '<a href="#pref-P-6">P-6</a>、<a href="#pref-P-7">P-7</a>' in demo


@mark.use_case("UC-DIRECT-007")
def test_a_lone_global_preference_can_still_be_overridden_in_a_scope_typed_in(
    conn: sqlite3.Connection,
) -> None:
    # No tasks and no other scope yet: the replace form still offers a scope of the user's own.
    _pref(conn, 1, "global", "PR", "PR 默认 ready")
    item = _item(render_preferences(conn, APP), "P-1")
    replace = item.split('data-form="replace"')[1].split('data-form="revoke"')[0]
    assert f'<option value="{OTHER}">其他（手填）</option>' in replace
    assert 'name="scope_other"' in replace and "也可以改为更窄的仓库或项目范围" in replace
    # A repository preference stays in its own scope.
    _pref(conn, 2, "repo:demo", "PR", "draft")
    item = _item(render_preferences(conn, APP), "P-2")
    replace = item.split('data-form="replace"')[1].split('data-form="revoke"')[0]
    assert OTHER not in replace and "scope_other" not in replace and "更窄" not in replace
