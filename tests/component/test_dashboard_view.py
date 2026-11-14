"""References and the index card on a real ledger and index (UC-DIRECT-005, DN006 §8.10, T278)."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing

import pytest

from tests.machine_harness import Harness
from tsw.context import ContextService
from tsw.context.index import index_path, read_card
from tsw.home import Home
from tsw.host.relations import references
from tsw.host.status import AppView, render
from tsw.host.task_view import card_state, render_task, timeline

mark = pytest.mark
APP = AppView(token="t", actions={})


def archived(h: Harness, task_id: str, **spec: str) -> None:
    h.register(task_id, **spec)
    h.run("task_cancel", task_id=task_id, user_source=h.source(task_id, 1, "cancel"))
    h.run("task_archive", task_id=task_id, user_source=h.source(task_id, 1, "archive"))


def dispatch_reading(h: Harness, task_id: str, read_set: list[str]) -> None:
    """T07 with the orchestrator's extra reads, as the main session dispatches T277."""
    h.run(
        "task_dispatch",
        task_id=task_id,
        workdir=f"~/wt/{task_id}",
        user_turn=True,
        read_set=read_set,
    )


@pytest.fixture
def h(home: Home) -> Harness:
    harness = Harness(home)
    archived(harness, "T001", title="旧研究 A")
    archived(harness, "T002", title="旧研究 B")
    archived(harness, "T003", title="无关的旧任务")
    harness.register("T004", title="新研究", goal="参考 T002 的结论；迁移 T23 不是任务")
    harness.plan("T004")
    dispatch_reading(harness, "T004", ["T001"])
    return harness


@mark.use_case("UC-DIRECT-005")
def test_the_dispatch_read_set_and_the_goal_give_references_on_the_board(h: Harness) -> None:
    with h.ledger.view() as conn:
        assert references(conn) == {
            "T004": {"T001": ["read_set:a1"], "T002": ["goal"]},
        }
        page = render(conn)
    flow = page.split('id="flow"')[1]
    node = re.search(r'<span class="fnode[^"]*" data-id="T004"[^>]*>', flow) or re.search(
        r'<a class="fnode[^"]*" data-id="T004"[^>]*>', flow
    )
    assert node and 'data-refs="T001 T002"' in node.group(0)
    # The archived tasks it references are context without the archived ones; T003 is not.
    for tid, live in (("T001", True), ("T002", True), ("T003", False)):
        found = re.search(rf'class="fnode([^"]*)" data-id="{tid}"', flow)
        assert found and (("no-live" not in found.group(1)) is live), tid


@mark.use_case("UC-DIRECT-005")
def test_the_task_view_reads_the_index_card_without_writing_it(h: Harness) -> None:
    path = index_path(h.home)
    # No index yet: the view says so and creates nothing.
    found = read_card(path, "T004")
    assert found.problem == "missing" and not path.exists()

    def state(found_now: object = None) -> tuple[object, str]:
        events = list(h.ledger.storage.read(since=0))
        steps = timeline(events).get("T004", [])
        with h.ledger.view() as conn:
            card = card_state(
                conn,
                "T004",
                read_card(path, "T004"),
                {e.sequence: e.checksum for e in events},
                max(s.sequence for s in steps),
            )
            page = render_task(conn, "T004", APP, steps, card)
        assert page is not None
        section = re.search(r'<section class="panel" id="card">.*?</section>', page, re.S)
        assert section
        return card, section.group(0)

    card, section = state()
    assert "检索索引尚未建立" in section and not path.exists()

    # A retrieval builds the index; the view now shows the card as indexed.
    ContextService(h.home, h.ledger, h.config).freshness()
    stamp = path.stat().st_mtime_ns
    card, section = state()
    assert card.problem is None and not card.lagging  # type: ignore[attr-defined]
    assert card.card["keyword_source"] == "generated"  # type: ignore[attr-defined]
    assert "由标题与任务书生成的初版" in section and "<dt>stale</dt><dd>是" in section

    # The task moves on: the index is behind for it until the next retrieval.
    h.run("progress_note", task_id="T004", text="进展")
    card, section = state()
    assert card.lagging and "索引落后" in section  # type: ignore[attr-defined]
    assert path.stat().st_mtime_ns == stamp  # reading never refreshed it

    # Another schema version, or a damaged file: said so, and left as it is.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE meta SET value = '999' WHERE key = 'version'")
    card, section = state()
    assert card.problem == "version" and "其他版本" in section  # type: ignore[attr-defined]
    path.write_bytes(b"not a database")
    card, section = state()
    assert card.problem == "unreadable" and "暂时无法读取" in section  # type: ignore[attr-defined]
    assert path.read_bytes() == b"not a database"
    # The next retrieval rebuilds it, and the card is current again.
    ContextService(h.home, h.ledger, h.config).freshness()
    card, section = state()
    assert card.problem is None and not card.lagging  # type: ignore[attr-defined]
