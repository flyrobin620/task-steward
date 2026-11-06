"""A small orchestrator-agent control root and the snapshot its exporter would write."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

PR = "https://github.com/example/demo/pull/7"


def _definition(task_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"旧任务 {task_id}",
        "repo": "demo",
        "kind": "code",
        "goal": "目标",
        "acceptance": "第一条\n第二条",
        "dependencies": [],
        "resources": [],
        "priority": 0,
        "paused": False,
        **extra,
    }


def tasks() -> dict[str, dict[str, Any]]:
    accepted = {"revision": 1, "definition_hash": "h", "accepted_at": "2026-10-01T00:00:00+00:00"}
    return {
        "T001": {
            "status": "done",
            "retirement": {
                "archived_at": "2026-10-09T00:00:00+00:00",
                "reason": "用户归档",
                "retained_resources": ["res-kept"],
            },
            "user_acceptance": accepted,
            "outputs": [{"id": "report", "type": "report", "ref": "runs/T001/a1/result.html"}],
            "worker_report": "runs/T001/a1/result.html",
        },
        "T002": {"status": "cancelled"},
        "T003": {"status": "done", "user_acceptance": accepted},
        "T004": {
            "status": "done",
            "completed_at": "2026-10-20T00:00:00+00:00",
            "worker_report": "runs/T004/a1/result.html",
            "pull_requests": [{"url": PR, "role": "current", "verified_at": "2026-10-20"}],
            "outputs": [{"id": "report", "type": "report", "ref": "runs/T004/a1/result.html"}],
            "attempts": [{"number": 1, "worker_channel": "codex-native", "branch": "codex/t004"}],
        },
        "T005": {"status": "pending", "dependencies": ["T004"]},
        "T006": {
            "status": "paused",
            "dependencies": ["T003"],
            "follow_up": {"owner": "主控", "next_action": "等外部", "resume_condition": "外部完成"},
            "input_request": {"id": "req-1", "question": "要继续吗？", "status": "pending"},
            "resources": ["res-paused"],
        },
        "T007": {
            "status": "running",
            "attempts": [
                {"number": 1},
                {"number": 2, "worktree": "wt/t007", "branch": "codex/t007", "base_commit": "abc"},
            ],
            "worker_report": "runs/T007/a2/result.html",
            "last_feedback": "补测试后再交",
            "dependencies": ["T004", "T001"],
        },
        "T008": {"status": "failed", "dependencies": ["T001"]},
        "T010": {
            "status": "paused",
            "retirement": {"archived_at": "2026-10-11T00:00:00+00:00", "reason": "用户关闭"},
        },
    }


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(root: Path, *, tail_events: int = 2) -> tuple[Path, Path]:
    """Returns (source root, snapshot directory)."""
    source = root / "control"
    runtime = source / "runtime"
    runtime.mkdir(parents=True)
    archive = runtime / "events-1-2.jsonl.gz"
    with gzip.open(archive, "wb") as stream:
        for seq in (1, 2):
            stream.write(json.dumps({"sequence": seq, "action": "x"}).encode() + b"\n")
    tail = runtime / "events.jsonl"
    tail.write_bytes(
        b"".join(
            json.dumps({"sequence": 3 + i, "action": "y"}).encode() + b"\n"
            for i in range(tail_events)
        )
    )
    last = 2 + tail_events
    records = tasks()
    (source / "todo").mkdir()
    for task_id in [*records, "T009"]:
        (source / "todo" / f"{task_id}.md").write_text(f"### {task_id}：旧任务\n", encoding="utf-8")
    (source / "TODO.md").write_text("- 索引\n", encoding="utf-8")
    for task_id in ("T001", "T004"):
        report = source / "runs" / task_id / "a1" / "result.html"
        report.parent.mkdir(parents=True)
        report.write_text("<p>报告</p>", encoding="utf-8")
    (source / "config").mkdir()
    (source / "config" / "repositories.json").write_text("{}", encoding="utf-8")
    keywords = source / "metadata" / "task-keywords" / "records"
    keywords.mkdir(parents=True)
    (keywords / "k.json").write_text("{}", encoding="utf-8")
    full = {
        tid: {
            "id": tid,
            "revision": 1,
            "definition": _definition(tid, resources=rec.pop("resources", [])),
            **rec,
        }
        for tid, rec in records.items()
    }
    derived = {
        tid: {
            "phase": "archived" if rec.get("retirement") else rec["status"],
            "user_acceptance_valid": tid in ("T001", "T003"),
            "activity_time": f"2026-10-{int(tid[1:]):02d}T12:00:00+00:00",
        }
        for tid, rec in records.items()
    }
    snapshot = {
        "schema": 1,
        "source": {
            "system": "orchestrator-agent",
            "root": str(source),
            "git_head": "abc",
            "exported_at": "2026-11-06T00:00:00+00:00",
            "last_sequence": last,
        },
        "controller": {
            "holder": "codex:old",
            "session": "old",
            "expires_at": "2026-11-04T15:27:24+00:00",
        },
        "tasks": full,
        "derived": derived,
        "volumes": [
            {
                "path": "runtime/events-1-2.jsonl.gz",
                "compressed": True,
                "first": 1,
                "last": 2,
                "bytes": archive.stat().st_size,
                "sha256": _sha(archive),
            },
            {
                "path": "runtime/events.jsonl",
                "compressed": False,
                "first": 3,
                "last": last,
                "bytes": tail.stat().st_size,
                "sha256": _sha(tail),
            },
        ],
        "files": {
            "index": "TODO.md",
            "todo": [f"todo/T{i:03d}.md" for i in range(1, 11)],
            "runs": ["T001", "T004"],
            "config": ["config/repositories.json"],
            "keywords": "metadata/task-keywords",
        },
    }
    snapshot_dir = root / f"snapshot-{last}"
    snapshot_dir.mkdir()
    (snapshot_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
    )
    return source, snapshot_dir
