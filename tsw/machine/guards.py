"""Guards of DN002 §4. Each returns nothing or raises ``REJECTED`` with a reason code."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any, cast

from tsw.config import EffectiveConfig, resolve_repository
from tsw.errors import rejected
from tsw.lease import stood_down
from tsw.machine.model import (
    KINDS_ALLOWED,
    LIVE_PHASES,
    OCCUPYING,
    PAUSE_CATEGORIES,
    loads,
    review_cycle,
    row_dict,
)
from tsw.plugins.catalog import SESSIONS

USER_CHANNELS = ("main-session", "in-session", "teams-chat", "hub-portal")
# The user's own operations outside any session (DN006 §8.10): only the ``user`` caller, which
# only the terminal commands and the STATUS app take, records them; sessions cannot use them.
DIRECT_CHANNELS = ("user-cli", "status-app")
SOURCE_FIELDS = ("channel", "principal", "message_ref", "text")


def g_plan(row: Mapping[str, Any], config: EffectiveConfig) -> str:
    """G-PLAN: complete specification, a known kind and a configured repository.

    Returns the canonical repository name.
    """
    acceptance = loads(row["acceptance"], [])
    missing = [name for name in ("title", "goal") if not str(row[name]).strip()]
    if not acceptance:
        missing.append("acceptance")
    if missing:
        raise rejected("任务书字段不完整", reason="g_plan", missing=missing)
    if row["kind"] not in KINDS_ALLOWED:
        raise rejected("任务类型必须是研究、设计或代码", reason="g_plan", kind=row["kind"])
    repo = resolve_repository(config, str(row["repo"]))
    if repo is None:
        raise rejected("仓库未在配置中登记", reason="g_plan", repo=row["repo"])
    return repo


def dependency_problems(
    conn: sqlite3.Connection,
    task_id: str,
    pending_invalid: Mapping[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """G-DEP for every dependency of ``task_id``; an empty list means all are satisfied.

    ``pending_invalid`` maps upstream tasks whose results no longer hold to the root changes
    that caused it (see :func:`invalidated`), so a single pass reaches every transitive dependent
    (DN002 §5.2) and an acknowledgement only covers the root changes it actually saw.
    """
    pending = pending_invalid or {}
    problems: list[dict[str, Any]] = []
    for dep in conn.execute("SELECT * FROM dependencies WHERE task_id = ?", (task_id,)):
        upstream = row_dict(
            conn.execute("SELECT * FROM tasks WHERE task_id = ?", (dep["dep_id"],)).fetchone()
        )
        reason = None
        if upstream is None:
            reason = "missing"
        elif upstream["status"] in ("cancelled",) or (
            upstream["status"] == "archived" and upstream["previous_status"] == "cancelled"
        ):
            reason = "cancelled"
        elif upstream["status"] not in ("done", "accepted", "archived") or (
            # Only work that was finished before it was archived satisfies a dependency.
            upstream["status"] == "archived"
            and upstream["previous_status"] not in ("done", "accepted")
        ):
            reason = "not_done"
        elif int(upstream["revision"]) != int(dep["bound_revision"]):
            reason = "revision_changed"
        elif int(upstream["stale"]) or upstream["task_id"] in pending:
            reason = "stale"
        if reason is not None:
            problem: dict[str, Any] = {
                "dep_id": dep["dep_id"],
                "reason": reason,
                "upstream_revision": None if upstream is None else int(upstream["revision"]),
                "upstream_status": None if upstream is None else upstream["status"],
            }
            if reason == "stale":
                problem["cause"] = pending.get(str(dep["dep_id"]), [])
            problems.append(problem)
    return problems


def root_causes(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The root changes behind ``problems``: direct facts, or the causes carried by a stale
    upstream. Sorted and de-duplicated so equal causes compare equal."""
    facts: dict[str, dict[str, Any]] = {}
    for problem in problems:
        found = problem.get("cause")
        for fact in found if found else [{k: v for k, v in problem.items() if k != "cause"}]:
            facts[json.dumps(fact, sort_keys=True, ensure_ascii=False)] = fact
    return [facts[key] for key in sorted(facts)]


def topological(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Tasks with every dependency before its dependents (registration order breaks ties)."""
    rows = conn.execute("SELECT * FROM tasks ORDER BY registered_seq").fetchall()
    deps: dict[str, list[str]] = {}
    for dep in conn.execute("SELECT task_id, dep_id FROM dependencies"):
        deps.setdefault(str(dep["task_id"]), []).append(str(dep["dep_id"]))
    by_id = {str(row["task_id"]): row for row in rows}
    ordered: list[sqlite3.Row] = []
    seen: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in seen or task_id not in by_id:
            return
        seen.add(task_id)
        for upstream in deps.get(task_id, []):
            visit(upstream)
        ordered.append(by_id[task_id])

    for row in rows:
        visit(str(row["task_id"]))
    return ordered


def invalidated(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Tasks whose results no longer hold, followed through the whole dependency chain.

    A ready or occupying task with an unsatisfied dependency is invalid; a done task is invalid
    when it is marked stale or has a dependency problem the user has not acknowledged. A kept
    (acknowledged) result stops the propagation (DN002 §5.2). Guards use this instead of the
    direct dependencies only, so nothing downstream can be accepted between an upstream change
    and the ``sync`` that records it.
    """
    invalid: dict[str, list[dict[str, Any]]] = {}
    for row in topological(conn):
        task_id, status = str(row["task_id"]), str(row["status"])
        problems = dependency_problems(conn, task_id, invalid)
        if status == "done":
            if int(row["stale"]) or (problems and problems != loads(row["stale_ack"], None)):
                invalid[task_id] = root_causes(problems)
        elif problems and status in ("ready", *OCCUPYING):
            invalid[task_id] = root_causes(problems)
    return invalid


def g_dep(conn: sqlite3.Connection, task_id: str) -> None:
    problems = dependency_problems(conn, task_id, invalidated(conn))
    if problems:
        raise rejected("依赖未满足", reason="g_dep", dependencies=problems)


def g_slot(conn: sqlite3.Connection, config: EffectiveConfig) -> None:
    placeholders = ", ".join("?" for _ in OCCUPYING)
    used = conn.execute(
        f"SELECT COUNT(*) AS n FROM tasks WHERE status IN ({placeholders})",
        tuple(OCCUPYING),
    ).fetchone()["n"]
    limit = int(config.get("scheduling.max_workers"))
    if used >= limit:
        raise rejected("没有空闲名额", reason="g_slot", used=used, max_workers=limit)


def g_res(conn: sqlite3.Connection, task_id: str, resources: Iterable[str]) -> None:
    for resource in resources:
        holder = conn.execute(
            "SELECT owner_task FROM resource_claims WHERE resource = ? AND status = 'held' "
            "AND owner_task != ?",
            (resource, task_id),
        ).fetchone()
        if holder is not None:
            raise rejected(
                "独占资源被其他任务占用",
                reason="g_res",
                resource=resource,
                holder=holder["owner_task"],
            )


def g_window(config: EffectiveConfig, user_turn: bool) -> None:
    """G-WINDOW: creating a session needs the user's own turn when the adapter says so."""
    adapter = str(config.get("roles.worker.session"))
    caps = SESSIONS.get(adapter)
    if caps is not None and caps.dispatch_window and not user_turn:
        raise rejected(
            "只有用户本人的 turn 能创建 worker 会话；任务保持就绪并标记待启动",
            reason="g_window",
            adapter=adapter,
        )


def g_one_attempt(conn: sqlite3.Connection, task_id: str, allowed: str | None = None) -> None:
    for row in conn.execute("SELECT attempt, phase FROM attempts WHERE task_id = ?", (task_id,)):
        if row["phase"] in LIVE_PHASES and row["attempt"] != allowed:
            raise rejected(
                "该任务已有可控的 attempt",
                reason="g_one_attempt",
                attempt=row["attempt"],
                phase=row["phase"],
            )


def g_pause(pause: Mapping[str, Any]) -> None:
    missing = [
        key
        for key in ("owner", "next_action", "resume_condition", "category")
        if not isinstance(pause.get(key), str) or not str(pause[key]).strip()
    ]
    if missing:
        raise rejected(
            "暂停必须写明负责人、下一步、恢复条件和类别", reason="g_pause", missing=missing
        )
    if pause["category"] not in PAUSE_CATEGORIES:
        raise rejected("未知的暂停类别", reason="g_pause", category=pause["category"])


def g_user(
    source: Mapping[str, Any] | None,
    *,
    task_id: str,
    revision: int,
    kind: str,
    role: str = "orchestrator",
) -> dict[str, Any]:
    """G-USER: a verified source naming the user, the message, the words and the target.

    The ``user`` caller (the user's own terminal command or STATUS app, DN006 §8.10) records the
    direct channels and only those; every other caller records a session or channel source.
    """
    return verified_source(
        source,
        [task_id, revision, kind],
        channels=DIRECT_CHANNELS if role == "user" else USER_CHANNELS,
        mismatch="用户来源的决定对象与当前任务不一致",
    )


def verified_source(
    source: Mapping[str, Any] | None,
    expected: list[Any],
    *,
    channels: tuple[str, ...] = USER_CHANNELS,
    mismatch: str = "用户来源的决定对象与本次操作不一致",
) -> dict[str, Any]:
    """A ``VerifiedSource`` (DN006 §8.5) whose target is exactly ``expected``."""
    if not isinstance(source, Mapping):
        raise rejected("需要用户的真实来源（VerifiedSource）", reason="g_user")
    missing = [
        key
        for key in SOURCE_FIELDS
        if not isinstance(source.get(key), str) or not str(source[key]).strip()
    ]
    if missing:
        raise rejected("用户来源字段不完整", reason="g_user", missing=missing)
    if source["channel"] not in channels:
        raise rejected("用户来源的渠道无效", reason="g_user", channel=source["channel"])
    target = source.get("target")
    if not isinstance(target, list) or list(target) != expected:  # pyright: ignore[reportUnknownArgumentType]
        raise rejected(mismatch, reason="g_user", expected=expected, target=target)
    return dict(source)


def effective_budget(config: EffectiveConfig, cycle: Mapping[str, Any]) -> int:
    return (
        int(config.get("review.max_rounds"))
        + int(cycle["orchestrator_extension"])
        + int(cycle["user_extension"])
    )


def blocking_unresolved(
    conn: sqlite3.Connection, task_id: str, attempt_id: str, cycle: int
) -> list[dict[str, Any]]:
    """Blocking findings of the cycle not yet disposed as G-SUBMIT requires.

    A ``fix`` counts as verified when the worker recorded a verification, or when a later round
    of the same cycle completed: the fix was then reviewed incrementally (DN004 §2).
    """
    latest = conn.execute(
        "SELECT MAX(round) AS r FROM review_rounds WHERE task_id = ? AND attempt = ? AND cycle = ? "
        "AND verdict IN ('pass', 'changes_requested')",
        (task_id, attempt_id, cycle),
    ).fetchone()["r"]
    unresolved: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM findings WHERE task_id = ? AND attempt = ? AND cycle = ? AND blocking = 1",
        (task_id, attempt_id, cycle),
    ):
        disposition = row["disposition"]
        reviewed_later = latest is not None and int(row["round"]) < int(latest)
        ok = (
            disposition in ("overruled", "deferred_approved")
            or (row["action"] == "fix" and (bool(row["verification"]) or reviewed_later))
            or (row["action"] == "reject" and bool(row["reason"]))
        )
        if not ok:
            unresolved.append(
                {"round": row["round"], "finding": row["finding_id"], "action": row["action"]}
            )
    return unresolved


def g_submit(
    conn: sqlite3.Connection,
    config: EffectiveConfig,
    task_row: Mapping[str, Any],
    attempt_id: str,
    head: str,
) -> dict[str, Any]:
    """G-SUBMIT (DN002 §4) for the current revision and submit cycle; returns the evidence."""
    cycle_no = int(task_row["cycle"])
    cycle = review_cycle(conn, str(task_row["task_id"]), attempt_id, cycle_no)
    rounds = [
        row_dict(r)
        for r in conn.execute(
            "SELECT * FROM review_rounds WHERE task_id = ? AND attempt = ? AND cycle = ? "
            "ORDER BY round",
            (task_row["task_id"], attempt_id, cycle_no),
        )
    ]
    reviewers = {
        str(config.get("roles.reviewer.session")),
        str(config.get("roles.reviewer.fallback_session") or ""),
    } - {""}
    completed = [
        r
        for r in rounds
        if r is not None
        and r["verdict"] in ("pass", "changes_requested")
        and loads(r["reviewer"], {}).get("session") in reviewers
    ]
    if not completed:
        raise rejected(
            "本提交周期没有由配置的评审器完成的评审", reason="g_submit", missing="review"
        )
    last = completed[-1]
    latest = rounds[-1]
    if latest is None or latest["round"] != last["round"] or latest["decision"] != "finalize":
        raise rejected(
            "本周期最后一轮必须是有效评审并已 finalize",
            reason="g_submit",
            last_round=None if latest is None else latest["round"],
            decision=None if latest is None else latest["decision"],
        )
    if cycle["last_decision"] != "finalize":
        raise rejected(
            "最后的评审决定不是 finalize", reason="g_submit", decision=cycle["last_decision"]
        )
    if last["reviewed_head"] != head:
        raise rejected(
            "最后一轮评审的 HEAD 与提交 HEAD 不一致",
            reason="g_submit",
            reviewed_head=last["reviewed_head"],
            head=head,
        )
    unresolved = blocking_unresolved(conn, str(task_row["task_id"]), attempt_id, cycle_no)
    if unresolved:
        raise rejected("仍有未处置的阻断项", reason="g_submit", findings=unresolved)
    return {
        "cycle": cycle_no,
        "rounds": [int(r["round"]) for r in completed],
        "last_reviewed_head": last["reviewed_head"],
    }


def merge_authorized(config: EffectiveConfig, task_row: Mapping[str, Any]) -> bool:
    """A task whose repository lets the worker merge, whatever its kind (DN002 §3.5, 0003)."""
    repo = resolve_repository(config, str(task_row["repo"]))
    return bool(repo and config.get(f"repositories.{repo}.delivery.merge"))


# GitHub Actions run conclusions and statuses: a run that has not finished yet is waited for,
# anything else that is not ``success`` failed (DN002 §3.5).
CI_PENDING = frozenset(
    {"", "queued", "in_progress", "pending", "requested", "waiting", "expected", "running"}
)
MERGE_KEYS = ("pr", "merge_commit", "merged_head", "live")


def _merge_ci(merge: Mapping[str, Any]) -> dict[str, str]:
    """The CI evidence of the merge commit on the default branch (DN002 G-SUBMIT, 0003).

    A structured declaration (DN008 §8 item 3): tsw checks that it is complete, names the merge
    commit and says the run passed; the orchestrator opens the run link when it verifies.
    """
    found: Any = merge.get("ci")
    if not isinstance(found, Mapping):
        raise rejected(
            "须附主分支上合并提交的 CI 结果：等 CI 完成，再在 merge.ci 中写明 commit、"
            "conclusion 与运行链接 url",
            reason="g_submit",
            check="merge_ci",
        )
    given = cast("Mapping[str, Any]", found)
    ci = {k: str(given.get(k) or "").strip() for k in ("commit", "conclusion", "url")}
    missing = [k for k in ("commit", "url") if not ci[k]]
    if missing:
        raise rejected(
            "CI 结果不完整：写明运行的提交与链接",
            reason="g_submit",
            check="merge_ci",
            missing=missing,
        )
    if ci["commit"] != str(merge["merge_commit"]):
        raise rejected(
            "CI 结果不是合并提交的：附主分支上合并提交的那次运行",
            reason="g_submit",
            check="ci_commit",
            ci_commit=ci["commit"],
            merge_commit=str(merge["merge_commit"]),
        )
    conclusion = ci["conclusion"].lower()
    if conclusion in CI_PENDING:
        raise rejected(
            "合并提交的 CI 尚未完成：等它完成后再提交",
            reason="g_submit",
            check="ci_pending",
            conclusion=ci["conclusion"],
            url=ci["url"],
        )
    if conclusion != "success":
        raise rejected(
            "合并提交的 CI 没有通过：在本任务内修复（新的 PR，经评审后合并）或 revert，"
            "等新的合并提交 CI 通过后再提交，不能带着失败提交",
            reason="g_submit",
            check="ci_failed",
            conclusion=ci["conclusion"],
            url=ci["url"],
        )
    return {**ci, "conclusion": "success"}


def g_merge(
    config: EffectiveConfig,
    task_row: Mapping[str, Any],
    head: str,
    merge: Mapping[str, Any] | None,
    skipped: str | None,
) -> dict[str, Any]:
    """The merge part of G-SUBMIT: a task in a repository that lets the worker merge is
    submitted after its PR is merged and the merge commit's CI passed, unless the brief or the
    repository says otherwise."""
    allowed = merge_authorized(config, task_row)
    if merge is not None:
        if not allowed:
            raise rejected(
                "该仓库没有合并授权（repositories.*.delivery.merge）",
                reason="g_submit",
                check="merge_authorized",
            )
        missing = [k for k in MERGE_KEYS if not str(merge.get(k) or "").strip()]
        if missing:
            raise rejected(
                "合并记录不完整（live 写明在最终 head 与合并后的主分支上运行的 live 场景与结果，"
                "或不需要、不能运行的原因）",
                reason="g_submit",
                check="merge",
                missing=missing,
            )
        if merge["merged_head"] != head:
            raise rejected(
                "合并的 PR head 与提交 HEAD（最后评审的 HEAD）不一致",
                reason="g_submit",
                check="merged_head",
                merged_head=merge["merged_head"],
                head=head,
            )
        record = {k: str(merge[k]).strip() for k in MERGE_KEYS}
        return {"merge": {**record, "ci": _merge_ci(merge)}}
    if allowed:
        if not (skipped and skipped.strip()):
            raise rejected(
                "该仓库由 worker 合并：先合并 PR 再提交，或用 merge_skipped 说明任务书或仓库规则"
                "要求由他人合并的原因",
                reason="g_submit",
                check="merge",
            )
        return {"merge_skipped": skipped.strip()}
    return {}


def controls(
    conn: sqlite3.Connection | None, attempt_row: Mapping[str, Any], caller_session: str | None
) -> bool:
    """The caller controls the attempt's session: it started it, or the orchestrator that did
    handed the lease over (released it, DN001 §6.4) and the caller, holding the lease now (every
    orchestrator write needs it, G-LEASE), took over its sessions. A session whose lease was taken
    over hands nothing on: its sessions are verified stopped and replaced (DN002 §5.5)."""
    controller = attempt_row["controller"]
    if controller is None or caller_session is None:
        return False
    if controller == caller_session:
        return True
    if conn is None:
        return False
    gave_up = stood_down(conn, str(controller))
    return gave_up is not None and gave_up.get("via") == "release"


def controllable(
    attempt_row: Mapping[str, Any] | None,
    caller_session: str | None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    return (
        attempt_row is not None
        and attempt_row["phase"] in ("live", "stopped", "standby")
        and controls(conn, attempt_row, caller_session)
    )


def g_accept_items(
    expected: list[str], results: list[Any], *, task_id: str, revision: int
) -> list[dict[str, Any]]:
    """G-ACCEPT, item by item: every acceptance item is done with evidence or explicitly
    replaced by the user (with a verified source); nothing is missing or extra."""
    seen: list[str] = []
    checked: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise rejected("验收结论元素应为对象", reason="invalid_input", field="acceptance")
        entry: Mapping[str, Any] = result  # pyright: ignore[reportUnknownVariableType]
        item = str(entry.get("item"))
        status = entry.get("status")
        if status == "done":
            evidence = entry.get("evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                raise rejected("已完成的验收项必须附证据", reason="g_accept", item=item)
            checked.append({"item": item, "status": "done", "evidence": evidence})
        elif status == "replaced_by_user":
            source = g_user(
                entry.get("user_source"), task_id=task_id, revision=revision, kind="replace"
            )
            checked.append({"item": item, "status": status, "user_source": source})
        else:
            raise rejected("验收项只能是已完成或用户明确替代", reason="g_accept", item=item)
        seen.append(item)
    if sorted(seen) != sorted(expected):
        raise rejected(
            "验收结论必须逐项覆盖任务书的验收要求",
            reason="g_accept",
            expected=expected,
            given=seen,
        )
    return checked
