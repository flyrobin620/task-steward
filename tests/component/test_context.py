"""Task context retrieval (DN007 §5–§8, §12): the L2 index on a real data directory, ledger and
SQLite, and the read tools the orchestrator and workers use."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tests.import_fixture import build
from tests.machine_harness import ORCH, Harness
from tsw.commands import Command
from tsw.config import load_config
from tsw.context import ContextService, Reader
from tsw.errors import TswError
from tsw.events import EventDraft
from tsw.home import Home
from tsw.importer.orchestrator_agent import run_import
from tsw.ledger import Ledger
from tsw.machine.base import Caller

ORCH_READER = Reader("orchestrator", "orch-1")


def service(h: Harness) -> ContextService:
    return ContextService(h.home, h.ledger, h.config)


def write_spec(h: Harness, task_id: str, body: str) -> None:
    """The task's spec file gets extra text (as tsw writes it, then a test adds to it)."""
    path = h.home.root / "todo" / f"{task_id}.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + body + "\n", encoding="utf-8")


def reason_of(call: Any) -> str:
    with pytest.raises(TswError) as exc:
        call()
    return str(exc.value.details.get("reason"))


def context_reads(h: Harness) -> list[dict[str, Any]]:
    return [dict(e.data) for e in h.ledger.storage.read(since=0) if e.type == "context.read"]


def seeded(home: Home) -> Harness:
    h = Harness(home)
    h.register("T001", title="[ex] CSV 导出：流式写出与分页", goal="报表页支持 CSV 导出")
    write_spec(h, "T001", "实现 `StreamingCsvWriter`，下载链接 24 小时过期。")
    h.register("T002", title="[ex] 导出权限检查", goal="只有管理员能导出")
    h.register("T003", title="[ex] 登录页改版", goal="新的登录页")
    h.register("T004", title="其他仓库的任务", repo="other", goal="与导出无关")
    h.register("T005", title="上游：报表数据接口", goal="报表接口")
    h.plan("T005")
    h.plan("T002", deps=["T005"])
    return h


@pytest.mark.use_case("UC-CONTEXT-001")
def test_cards_are_built_from_the_ledger_and_marked_by_their_keyword_source(home: Home) -> None:
    h = seeded(home)
    svc = service(h)
    out = svc.catalog(ORCH_READER, {"scope": "repo", "repo": "demo", "limit": 20})
    cards = {c["task_id"]: c for c in out["cards"]}
    assert set(cards) == {"T001", "T002", "T003", "T005"}  # T004 is in another repository
    first = cards["T001"]
    assert first["keyword_source"] == "generated" and first["stale"] is True
    assert "StreamingCsvWriter" in first["keywords"]
    assert "ex" in first["keywords"]
    assert first["summary_source"] == "goal" and first["summary"] == "报表页支持 CSV 导出"
    assert cards["T002"]["dependencies"] == ["T005"]
    assert out["index"]["indexed_seq"] == out["index"]["projected_seq"]
    assert (home.cache_dir / "context.sqlite").is_file()
    # Reading the catalog in one repository is not audited; a global one is.
    assert context_reads(h) == []


@pytest.mark.use_case("UC-CONTEXT-001")
def test_recorded_keywords_win_and_go_stale_with_a_new_revision_or_outputs(home: Home) -> None:
    h = seeded(home)
    before = h.task("T001")["last_activity"]
    h.clock.now = "2026-11-06T00:00:00Z"
    h.run(
        "task_summary",
        task_id="T001",
        keywords=["CSV 导出", "流式写出", "下载链接过期"],
        stage="intake",
    )
    # Describing a task is not activity in it.
    assert h.task("T001")["last_activity"] == before
    svc = service(h)
    card = svc.catalog(ORCH_READER, {"task_ids": ["T001"]})["pinned"][0]
    assert card["keywords"] == ["CSV 导出", "流式写出", "下载链接过期"]
    assert card["keyword_source"] == "recorded" and card["stale"] is False

    h.run(
        "output_link",
        task_id="T001",
        output_id="pr-1",
        type="pr",
        ref="https://example.com/pr/1",
        role="current",
        evidence=["pr.json"],
        verified_at="2026-11-06T00:00:00Z",
    )
    card = svc.catalog(ORCH_READER, {"task_ids": ["T001"]})["pinned"][0]
    assert card["stale"] is True
    assert card["outcomes"] == ["pr-1 https://example.com/pr/1"]

    h.run(
        "task_summary",
        task_id="T001",
        keywords=["CSV 导出"],
        summary="服务端流式写出 CSV，链接 24 小时过期；PR 已合并。",
        stage="acceptance",
    )
    card = svc.catalog(ORCH_READER, {"task_ids": ["T001"]})["pinned"][0]
    assert card["stale"] is False and "summary_source" not in card
    h.run(
        "task_update",
        task_id="T001",
        goal="报表页支持 CSV 与 Excel 导出",
        acceptance=["两种格式都能导出"],
        user_source=Harness.source("T001", 1, "change"),
    )
    card = svc.catalog(ORCH_READER, {"task_ids": ["T001"]})["pinned"][0]
    assert card["revision"] == 2 and card["stale"] is True


@pytest.mark.use_case("UC-CONTEXT-001")
def test_task_summary_checks_its_input_and_belongs_to_the_orchestrator(home: Home) -> None:
    h = seeded(home)
    assert reason_of(
        lambda: h.run("task_summary", task_id="T001", keywords=[], stage="intake")
    ) == ("invalid_input")
    assert (
        reason_of(lambda: h.run("task_summary", task_id="T001", keywords=["a"], stage="acceptance"))
        == "invalid_input"
    )
    assert (
        reason_of(lambda: h.run("task_summary", task_id="T001", keywords=["a"], stage="later"))
        == "invalid_input"
    )
    assert reason_of(
        lambda: h.run("task_summary", task_id="T999", keywords=["a"], stage="intake")
    ) == ("task_not_found")
    h.plan("T003")
    worker = h.dispatch("T003")
    assert (
        reason_of(
            lambda: h.run("task_summary", worker, task_id="T003", keywords=["a"], stage="intake")
        )
        == "role_denied"
    )
    other = Caller(role="orchestrator", session_id="orch-2", machine="m1")
    assert (
        reason_of(
            lambda: h.run("task_summary", other, task_id="T001", keywords=["a"], stage="intake")
        )
        == "lease_held"
    )


@pytest.mark.use_case("UC-CONTEXT-002")
def test_catalog_ranks_keyword_hits_pins_named_tasks_and_their_dependencies(home: Home) -> None:
    h = seeded(home)
    h.run("task_summary", task_id="T002", keywords=["导出权限", "管理员"], stage="intake")
    svc = service(h)
    out = svc.catalog(ORCH_READER, {"query": "导出", "repo": "demo"})
    ids = [c["task_id"] for c in out["cards"]]
    assert ids[0] == "T002"  # a keyword hit outranks a title hit
    assert set(ids) == {"T001", "T002"}
    assert out["cards"][0]["match"]["keywords"] == ["导出权限"]

    # Named tasks and their hard dependencies are always there, whatever the scope and page.
    out = svc.catalog(ORCH_READER, {"query": "登录 T002", "repo": "other", "limit": 1})
    pinned = {c["task_id"]: c["pinned"] for c in out["pinned"]}
    assert pinned == {"T002": "named", "T005": "dependency_of:T002"}
    assert [c["task_id"] for c in out["cards"]] == []  # 登录 is not in repository other

    # Pages: one card at a time, next_offset until the end.
    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = svc.catalog(ORCH_READER, {"repo": "demo", "limit": 1, "offset": offset})
        seen += [c["task_id"] for c in page["cards"]]
        offset = page["next_offset"]
    assert sorted(seen) == ["T001", "T002", "T003", "T005"]


@pytest.mark.use_case("UC-CONTEXT-002")
def test_aliases_expand_repositories_and_queries(home: Home) -> None:
    h = seeded(home)
    svc = service(h)
    # The configured alias d stands for demo; the title tag ex mostly names demo.
    assert {
        c["task_id"] for c in svc.catalog(ORCH_READER, {"repo": "d", "limit": 20})["cards"]
    } == {
        "T001",
        "T002",
        "T003",
        "T005",
    }
    out = svc.catalog(ORCH_READER, {"query": "demo", "scope": "global", "reason": "跨仓库查重"})
    assert {c["task_id"] for c in out["cards"]} >= {"T001", "T002"}
    assert "ex" in out["terms"]["demo"]
    audit = context_reads(h)
    assert audit[-1]["tool"] == "context_catalog" and audit[-1]["reason"] == "跨仓库查重"
    assert audit[-1]["reader"] == {"role": "orchestrator", "task_id": None, "attempt": None}


@pytest.mark.use_case("UC-CONTEXT-002")
def test_full_text_finds_material_beyond_the_card_and_short_words_only_match_cards(
    home: Home,
) -> None:
    h = seeded(home)
    svc = service(h)
    out = svc.search(ORCH_READER, {"query": "下载链接", "repo": "demo"})
    assert [(x["task_id"], x["kind"]) for x in out["hits"]] == [("T001", "spec")]
    assert "«下载链接»" in out["hits"][0]["snippet"]
    assert out["hits"][0]["ref"] == "todo/T001.md"
    # A catalog query reaches the task through its spec as well.
    found = svc.catalog(ORCH_READER, {"query": "下载链接", "repo": "demo"})["cards"]
    assert [c["task_id"] for c in found] == ["T001"] and found[0]["match"]["full_text"] == 1
    # Two characters: no full text, only the cards.
    out = svc.search(ORCH_READER, {"query": "登录", "repo": "demo"})
    assert out["hits"] == [] and out["short_terms"] == ["登录"]
    assert [c["task_id"] for c in out["card_matches"]] == ["T003"]
    assert context_reads(h)[-1]["tool"] == "context_search"


@pytest.mark.use_case("UC-CONTEXT-003")
def test_the_index_follows_the_ledger_incrementally_and_prunes_the_queue(home: Home) -> None:
    h = seeded(home)
    svc = service(h)
    first = svc.index.refresh()
    assert first.indexed_seq == first.projected_seq
    assert h.query("SELECT COUNT(*) AS n FROM index_queue")[0]["n"] == 0
    h.register("T006", title="CSV 导入", goal="导入 CSV")
    fresh = svc.index.refresh()
    assert fresh.indexed_seq > first.indexed_seq and not fresh.lag
    with svc.index.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 6
    assert h.query("SELECT COUNT(*) AS n FROM index_queue")[0]["n"] == 0
    # Nothing new: nothing is rewritten.
    assert svc.index.refresh().indexed_seq == fresh.indexed_seq


@pytest.mark.use_case("UC-CONTEXT-003")
def test_a_lagging_index_falls_back_to_the_projection(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = seeded(home)
    svc = service(h)
    svc.index.refresh()
    h.register("T006", title="新任务：导出模板", goal="导出模板")
    monkeypatch.setenv("TSW_TEST_FAULTS", "context.refresh")
    out = svc.catalog(ORCH_READER, {"query": "导出模板", "repo": "demo"})
    assert out["index"]["index_lag"] is True
    assert "T006" in out["index"]["lagging_tasks"]
    lagging = [c for c in out["cards"] if c["task_id"] == "T006"]
    assert lagging and lagging[0]["index_lag"] is True
    # A named task the index has never seen is still there.
    assert [c["task_id"] for c in svc.catalog(ORCH_READER, {"task_ids": ["T006"]})["pinned"]] == [
        "T006"
    ]
    monkeypatch.delenv("TSW_TEST_FAULTS")
    out = svc.catalog(ORCH_READER, {"query": "导出模板", "repo": "demo"})
    assert "index_lag" not in out["index"]


@pytest.mark.use_case("UC-CONTEXT-003")
def test_a_lost_damaged_or_foreign_index_is_rebuilt(home: Home) -> None:
    h = seeded(home)
    svc = service(h)
    built = svc.rebuild()
    assert built["cards"] == 5 and built["full_text"] is True
    assert built["keyword_sources"] == {"generated": 5}
    path = home.cache_dir / "context.sqlite"
    path.write_bytes(b"not a database")
    assert svc.index.refresh().indexed_seq == built["indexed_seq"]
    with svc.index.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 5
        # Another history: the recorded checksum no longer matches the ledger.
        conn.execute("UPDATE meta SET value = 'x' WHERE key = 'indexed_checksum'")
        conn.execute("DELETE FROM cards")
    svc.index.refresh()
    with svc.index.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 5
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'version'")
    svc.index.refresh()
    with svc.index.connection() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()[0] == "1"


@pytest.mark.use_case("UC-CONTEXT-004")
def test_context_read_needs_a_reason_records_it_and_pages(home: Home) -> None:
    h = seeded(home)
    write_spec(h, "T001", "长段落。" * 200)
    svc = ContextService(h.home, h.ledger, h.config)
    assert reason_of(lambda: svc.read(ORCH_READER, {"task_ids": ["T001"], "part": "spec"})) == (
        "invalid_input"
    )
    assert (
        reason_of(
            lambda: svc.read(ORCH_READER, {"task_ids": ["T001"], "part": "x", "reason": "核对范围"})
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            lambda: svc.read(
                ORCH_READER, {"task_ids": ["T404"], "part": "spec", "reason": "核对范围"}
            )
        )
        == "task_not_found"
    )
    svc.max_chars = 300
    first = svc.read(
        ORCH_READER, {"task_ids": ["T001"], "part": "spec", "reason": "核对 T001 的导出方案"}
    )
    assert first["text"].startswith("## T001 · spec")
    assert len(first["text"]) == 300 and first["next_offset"] == 300
    assert first["sources"] == [{"task_id": "T001", "refs": ["todo/T001.md"]}]
    rest = svc.read(
        ORCH_READER,
        {"task_ids": ["T001"], "part": "spec", "reason": "继续读", "offset": first["next_offset"]},
    )
    assert rest["offset"] == 300 and first["total_chars"] == rest["total_chars"]
    audit = context_reads(h)
    assert [(a["tool"], a["chars"], a["reason"]) for a in audit] == [
        ("context_read", 300, "核对 T001 的导出方案"),
        ("context_read", len(rest["text"]), "继续读"),
    ]
    assert first["recorded"] is not None and rest["recorded"] > first["recorded"]


@pytest.mark.use_case("UC-CONTEXT-004")
def test_context_read_parts_and_the_task_history(home: Home) -> None:
    h = Harness(home)
    h.complete("T001")
    svc = service(h)
    reader = ORCH_READER
    report = svc.read(reader, {"task_ids": ["T001"], "part": "report", "reason": "看交付"})
    assert "# 报告" in report["text"] and report["sources"][0]["refs"] == ["runs/T001/a1/result.md"]
    reviews = svc.read(reader, {"task_ids": ["T001"], "part": "reviews", "reason": "看评审"})
    assert "R1" in reviews["text"] and "pass" in reviews["text"]
    outputs = svc.read(reader, {"task_ids": ["T001"], "part": "outputs", "reason": "看产出"})
    assert "https://example.com/pr/1" in outputs["text"]
    brief = svc.read(reader, {"task_ids": ["T001"], "part": "brief", "reason": "看任务书"})
    assert "任务书 T001" in brief["text"]
    history = svc.task(reader, {"task_id": "T001"})
    assert history["task"]["status"] == "done"
    assert [a["attempt"] for a in history["attempts"]] == ["a1"]
    types = [t["type"] for t in history["timeline"]]
    assert types[0] == "task.registered" and "worker.submitted" in types
    assert "task.accepted" in types


@pytest.mark.use_case("UC-CONTEXT-005")
def test_decisions_come_from_the_users_words_and_say_how_far_they_reach(home: Home) -> None:
    h = seeded(home)
    asked = h.run("intake_question", task_id="T001", question="导出要不要包含隐藏列？")
    request_id = asked["events"][0]["data"]["request_id"]
    h.run(
        "input_resolve",
        task_id="T001",
        request_id=request_id,
        answer="不包含隐藏列",
        user_source=Harness.source("T001", 1, "answer", "导出不要包含隐藏列", request_id),
    )
    source = {
        "channel": "main-session",
        "principal": "owner",
        "message_ref": "msg-pref-1",
        "text": "以后导出文件一律用 UTF-8 BOM",
        "target": ["preference", "repo:demo", "standing"],
    }
    first = h.run(
        "preference_record",
        topic="导出编码",
        text="导出文件一律用 UTF-8 BOM",
        scope="repo:demo",
        user_source=source,
    )
    assert first["events"][0]["data"]["ref"] == "P-1"
    assert first["events"][0]["task_id"] is None
    svc = service(h)
    out = svc.decisions(ORCH_READER, {"query": "导出", "scope": "repo", "repo": "demo"})
    by_ref = {d["ref"]: d for d in out["decisions"]}
    assert by_ref["P-1"]["kind"] == "standing" and by_ref["P-1"]["usable_as_basis"] == "in_scope"
    answer = next(d for d in out["decisions"] if d["kind"] == "task")
    assert answer["task_id"] == "T001" and answer["text"] == "导出不要包含隐藏列"
    assert answer["usable_as_basis"] == "this_task_only"
    assert out["decisions"][0]["ref"] == "P-1"  # a standing preference first

    # Superseding: same scope or narrower, with the user's words for the new scope.
    second = {
        **source,
        "message_ref": "msg-pref-2",
        "text": "导出改用不带 BOM 的 UTF-8",
    }
    h.run(
        "preference_record",
        topic="导出编码",
        text="导出用不带 BOM 的 UTF-8",
        scope="repo:demo",
        supersedes=["P-1"],
        user_source=second,
    )
    out = svc.decisions(ORCH_READER, {"query": "导出编码"})
    by_ref = {d["ref"]: d for d in out["decisions"]}
    assert by_ref["P-1"]["superseded_by"] == "P-2" and by_ref["P-1"]["usable_as_basis"] is False
    assert by_ref["P-2"]["supersedes"] == ["P-1"]
    wider = {**source, "target": ["preference", "global", "standing"], "message_ref": "m3"}
    assert (
        reason_of(
            lambda: h.run(
                "preference_record",
                topic="导出编码",
                text="全局",
                scope="global",
                supersedes=["P-2"],
                user_source=wider,
            )
        )
        == "invalid_input"
    )
    assert (
        reason_of(
            lambda: h.run(
                "preference_record", topic="x", text="y", scope="global", user_source=source
            )
        )
        == "g_user"
    )
    # Task scope: the task's own decisions; another repository sees none of T001's.
    assert {
        d["ref"]
        for d in svc.decisions(ORCH_READER, {"scope": "task", "task_id": "T001"})["decisions"]
    } == {answer["ref"], "P-1", "P-2"}
    assert svc.decisions(ORCH_READER, {"scope": "repo", "repo": "other"})["decisions"] == []


@pytest.mark.use_case("UC-CONTEXT-002")
def test_a_worker_sees_every_task_with_its_own_read_set_first(home: Home) -> None:
    h = seeded(home)
    h.plan("T001")
    worker = h.dispatch("T001")
    reader = Reader("worker", worker.session_id, "T001", "a1")
    svc = service(h)
    out = svc.catalog(reader, {"scope": "repo", "limit": 20})
    ids = [c["task_id"] for c in out["cards"]]
    assert ids[0] == "T001"  # its own task (the recommended read set) first
    # Workers read any task, and the audit says who read.
    svc.read(reader, {"task_ids": ["T004"], "part": "spec", "reason": "参考别的仓库的做法"})
    assert context_reads(h)[-1]["reader"] == {"role": "worker", "task_id": "T001", "attempt": "a1"}


@pytest.mark.use_case("UC-CONTEXT-003")
def test_events_written_before_these_tables_still_reduce(home: Home) -> None:
    """A projection made before the new tables gets them without a rebuild (DN007 §12.2)."""
    h = seeded(home)
    state = home.cache_dir / "state.sqlite"
    with sqlite3.connect(state) as conn:
        conn.execute("DROP TABLE task_summaries")
        conn.execute("DROP TABLE user_decisions")
    h.run("task_summary", task_id="T001", keywords=["CSV"], stage="intake")
    assert h.query("SELECT keywords FROM task_summaries")[0]["keywords"] == '["CSV"]'
    h.ledger.commit(
        Command(
            actor=ORCH.actor(),
            input={"op": "audit"},
            build=lambda ctx: [EventDraft(type="context.read", data={"tool": "context_read"})],
        )
    )
    assert context_reads(h)[-1]["tool"] == "context_read"


@pytest.mark.use_case("UC-CONTEXT-001")
def test_imported_tasks_take_their_keywords_from_the_legacy_records(
    home: Home, tmp_path: Path
) -> None:
    _, snapshot = build(tmp_path)
    ledger = Ledger(home)
    run_import(home, ledger, snapshot)
    base = home.root / "legacy" / "orchestrator-agent" / "metadata" / "task-keywords"
    (base / "records").mkdir(parents=True, exist_ok=True)
    record = {
        "id": "T001",
        "keywords": ["邮箱标签", "mailbox tags"],
        "project_ids": ["spqe"],
        "coverage": {"intent": "reviewed", "outputs": "partial"},
        "source": {"revision": 1},
    }
    (base / "records" / "r1.json").write_text(json.dumps(record), encoding="utf-8")
    (base / "index.json").write_text(
        json.dumps({"records": {"T001": {"ref": "metadata/task-keywords/records/r1.json"}}}),
        encoding="utf-8",
    )
    for found in home.root.glob("**/T001/a1/result.html"):
        found.write_text("<p>报告：迁移了 mailbox tags 的写入路径</p>", encoding="utf-8")
    config = load_config(home)
    svc = ContextService(home, ledger, config)
    built = svc.rebuild()
    assert built["keyword_sources"]["legacy"] == 1
    cards = {
        c["task_id"]: c for c in svc.catalog(ORCH_READER, {"task_ids": ["T001", "T004"]})["pinned"]
    }
    assert cards["T001"]["keywords"] == ["邮箱标签", "mailbox tags"]
    assert cards["T001"]["keyword_source"] == "legacy" and cards["T001"]["stale"] is False
    assert cards["T001"]["project_ids"] == ["spqe"]
    assert cards["T001"]["keyword_coverage"] == {"intent": "reviewed", "outputs": "partial"}
    assert cards["T001"]["imported"] is True
    assert cards["T004"]["keyword_source"] == "generated" and cards["T004"]["stale"] is True
    # The project is an alias of the repository; the imported report is in the full text.
    assert [c["task_id"] for c in svc.catalog(ORCH_READER, {"project": "spqe"})["cards"]] == [
        "T001"
    ]
    hits = svc.search(ORCH_READER, {"query": "写入路径"})["hits"]
    assert [(h["task_id"], h["kind"]) for h in hits] == [("T001", "report")]
