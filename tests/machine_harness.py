"""A harness that drives the state machine through its tools, as the orchestrator and workers do."""

from __future__ import annotations

import json
from typing import Any

from tsw import lease
from tsw.config import load_config
from tsw.home import Home
from tsw.ledger import Ledger
from tsw.machine.base import Caller
from tsw.machine.model import loads, task
from tsw.machine.runner import run_tool

MACHINE = "m1"
ORCH = Caller(role="orchestrator", session_id="orch-1", machine=MACHINE)


class Clock:
    def __init__(self, now: str = "2026-11-05T00:00:00Z") -> None:
        self.now = now

    def __call__(self) -> str:
        return self.now


class Harness:
    def __init__(self, home: Home, config: dict[str, Any] | None = None) -> None:
        self.home = home
        settings: dict[str, Any] = {
            "version": 1,
            "repositories": {"demo": {"path": "~/src/demo", "aliases": ["d"]}},
        }
        for key, value in (config or {}).items():
            settings[key] = value
        home.path("config/task-steward.json").write_text(json.dumps(settings), encoding="utf-8")
        self.config = load_config(home).require_valid()
        self.clock = Clock()
        self.ledger = Ledger(home, clock=self.clock, machine=MACHINE)
        self.ledger.commit(
            lease.acquire("orch-1", machine=MACHINE, lease_minutes=1_000_000, actor=ORCH.actor())
        )

    def run(self, name: str, caller: Caller = ORCH, /, **inp: Any) -> dict[str, Any]:
        return run_tool(self.ledger, self.config, caller, name, inp)

    def task(self, task_id: str) -> dict[str, Any]:
        with self.ledger.view() as conn:
            return task(conn, task_id)

    def status(self, task_id: str) -> str:
        return str(self.task(task_id)["status"])

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with self.ledger.view() as conn:
            return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]  # noqa: SIM118

    @staticmethod
    def source(task_id: str, revision: int, kind: str, text: str = "同意") -> dict[str, Any]:
        return {
            "channel": "main-session",
            "principal": "owner",
            "message_ref": f"msg-{task_id}-{kind}",
            "text": text,
            "target": [task_id, revision, kind],
        }

    # ------------------------------------------------------------------ flows

    def register(self, task_id: str, **overrides: Any) -> None:
        spec: dict[str, Any] = {
            "task_id": task_id,
            "title": f"任务 {task_id}",
            "repo": "demo",
            "kind": "code",
            "goal": "实现功能",
            "acceptance": ["功能可用", "测试通过"],
        }
        spec.update(overrides)
        self.run("task_register", **spec)

    def plan(
        self, task_id: str, deps: list[str] | None = None, resources: list[str] | None = None
    ) -> None:
        self.run(
            "task_plan",
            task_id=task_id,
            dependencies=[{"task_id": d, "reason": "需要上游成果"} for d in deps or []],
            resources=resources or [],
            reason="规划",
        )

    def dispatch(self, task_id: str, session: str | None = None) -> Caller:
        """T07 → start → bound (T08); returns the worker caller."""
        self.run("task_dispatch", task_id=task_id, workdir=f"~/wt/{task_id}", user_turn=True)
        started = self.run("attempt_start", task_id=task_id, purpose="dispatch", user_turn=True)
        data = started["events"][0]["data"]
        worker = Caller(
            role="worker",
            session_id=session or f"w-{task_id}-{data['generation']}",
            machine=MACHINE,
        )
        self.run(
            "attempt_reconcile",
            task_id=task_id,
            attempt=data["attempt"],
            generation=data["generation"],
            outcome="created",
            session_ref=worker.session_id,
            brief_delivered=True,
            receipt={"model": "claude-opus-5-5[1m]"},
        )
        return worker

    def review_round(
        self,
        worker: Caller,
        task_id: str,
        head: str,
        findings: list[dict[str, Any]] | None = None,
    ) -> int:
        started = self.run(
            "review_start",
            worker,
            task_id=task_id,
            head=head,
            reviewer={"session": "codex-app-server", "model": "gpt-6-astra"},
        )
        round_no = int(started["events"][0]["data"]["round"])
        self.run(
            "review_complete",
            worker,
            task_id=task_id,
            round=round_no,
            result_path=f"runs/{task_id}/review/r{round_no}.json",
            result={
                "verdict": "changes_requested" if findings else "pass",
                "summary": "评审",
                "reviewed_head": head,
                "findings": findings or [],
            },
        )
        return round_no

    def finalize(self, worker: Caller, task_id: str, head: str = "h1") -> None:
        round_no = self.review_round(worker, task_id, head)
        self.run("review_triage", worker, task_id=task_id, round=round_no, items=[])
        self.run(
            "review_decide", worker, task_id=task_id, round=round_no, decision="finalize", head=head
        )

    def submit(self, worker: Caller, task_id: str, head: str = "h1") -> None:
        attempt = self.task(task_id)["current_attempt"]
        draft = self.home.root / "work" / task_id / attempt
        draft.mkdir(parents=True, exist_ok=True)
        (draft / "result.md").write_text("# 报告", encoding="utf-8")
        (draft / "result.html").write_text("<h1>报告</h1>", encoding="utf-8")
        self.run(
            "submit",
            worker,
            task_id=task_id,
            head=head,
            report_md=f"work/{task_id}/{attempt}/result.md",
            report_html=f"work/{task_id}/{attempt}/result.html",
            worktree_clean=True,
        )

    def accept(self, task_id: str, head: str = "h1") -> dict[str, Any]:
        self.run(
            "output_link",
            task_id=task_id,
            output_id="pr-1",
            type="pr",
            ref="https://example.com/pr/1",
            role="current",
            evidence=["pr.json"],
            verified_at="2026-11-05T00:00:00Z",
        )
        acceptance = loads(self.task(task_id)["acceptance"], [])
        return self.run(
            "task_accept",
            task_id=task_id,
            verified_head=head,
            summary="验收通过",
            acceptance=[
                {"item": item, "status": "done", "evidence": "测试结果"} for item in acceptance
            ],
        )

    def complete(self, task_id: str, deps: list[str] | None = None) -> Caller:
        """Register, plan, dispatch, review, submit and accept: the task ends in ``done``."""
        self.register(task_id)
        self.plan(task_id, deps)
        worker = self.dispatch(task_id)
        self.finalize(worker, task_id)
        self.submit(worker, task_id)
        self.accept(task_id)
        return worker
