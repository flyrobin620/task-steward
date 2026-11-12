# L1 and L2 rows are untyped JSON; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""The read tools on top of the context index (DN007 §5.3, §6, §7): ``context_catalog``,
``context_read``, ``context_search``, ``context_decisions`` and ``context_task``.

All of them only read; ``context_read``, ``context_search`` and a global ``context_catalog``
append a ``context.read`` audit event (DN007 §7.1) that belongs to no task. Results carry their
sources (task, file, event sequence) so that a decision can cite them.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tsw.commands import Command, CommitContext
from tsw.config import EffectiveConfig
from tsw.errors import rejected
from tsw.events import EventDraft
from tsw.home import Home
from tsw.ledger import Ledger

from .index import ContextIndex, Facts, Freshness, report_refs, resolve_material, task_facts
from .text import contains, is_short, pieces, read_text, terms

PARTS = ("spec", "brief", "report", "reviews", "decisions", "outputs", "follow_ups", "requests")
SCOPES = ("repo", "project", "global")
MAX_LIMIT = 50
MIN_REASON_CHARS = 2
TIMELINE_LIMIT = 200
SUMMARY_CHARS = 160
OUTCOMES = 3
TASK_ID = re.compile(r"^T\d{3,}$", re.IGNORECASE)
# Generated keywords mostly repeat the title: a hit on them counts like a summary hit.
WEIGHTS = {"keywords": 3, "generated": 1, "title": 2, "summary": 1}


@dataclass(frozen=True)
class Reader:
    """Who is reading: the orchestrator or a worker of ``task_id`` / ``attempt``."""

    role: str
    session_id: str | None = None
    task_id: str | None = None
    attempt: str | None = None

    def actor(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.session_id:
            out["session_id"] = self.session_id
        return out

    def describe(self) -> dict[str, Any]:
        return {"role": self.role, "task_id": self.task_id, "attempt": self.attempt}


class ContextService:
    def __init__(
        self, home: Home, ledger: Ledger, config: EffectiveConfig, *, path: Path | None = None
    ) -> None:
        self.home = home
        self.ledger = ledger
        self.index = ContextIndex(home, ledger, path=path)
        self.index.tokenizer = str(config.get("context.fts_tokenizer") or "trigram")
        repos = config.get("repositories") or {}
        self.index.repo_aliases = {
            str(name): [str(a) for a in (repo or {}).get("aliases") or []]
            for name, repo in repos.items()
            if isinstance(repo, dict)
        }
        self.page_size = int(config.get("context.catalog_page_size") or 8)
        self.max_chars = int(config.get("context.max_chars") or 20000)
        self.require_reason = bool(config.get("context.require_read_reason"))

    # ------------------------------------------------------------------ freshness

    def freshness(self) -> Freshness:
        """Catch the index up; when that fails, say which tasks are behind (DN007 §5.4)."""
        try:
            return self.index.refresh()
        except Exception as exc:  # the index is a cache: fall back to L1, never fail the read
            error = f"{type(exc).__name__}: {exc}"
        with self.ledger.view() as l1:
            projected = int(
                (
                    l1.execute("SELECT value FROM meta WHERE key = 'projected_seq'").fetchone()
                    or [0]
                )[0]
            )
            present = [r["task_id"] for r in l1.execute("SELECT task_id FROM tasks")]
        indexed, fts, known = 0, False, set[str]()
        try:
            with self.index.connection() as conn:
                indexed = self.index.indexed(conn).sequence
                fts = self.index.fts(conn)
                known = {r["task_id"] for r in conn.execute("SELECT task_id FROM cards")}
            touched = self.index.scan(indexed).touched if indexed else set(present)
        except Exception:  # an unreadable index: every task is behind
            indexed, touched = 0, set(present)
        lagging = sorted({t for t in present if t in touched or t not in known})
        return Freshness(indexed, projected, fts, lagging, error)

    # ------------------------------------------------------------------ catalog

    def catalog(self, reader: Reader, args: Mapping[str, Any]) -> dict[str, Any]:
        query = _opt(args, "query")
        words = terms(query)
        named = [w.upper() for w in words if TASK_ID.match(w)]
        words = [w for w in words if not TASK_ID.match(w)]
        named = list(dict.fromkeys([*(_ids(args, "task_ids")), *named]))
        scope, repo, project = self._scope(reader, args)
        limit = _limit(args, self.page_size)
        offset = _offset(args)
        fresh = self.freshness()
        with self.index.connection() as c2, self.ledger.view() as l1:
            cards = self._cards(c2, l1, fresh)
            aliases = _aliases(c2)
            variants = {w: _expand(w, aliases) | pieces(w) for w in words}
            fts_hits = self._fts_tasks(c2, variants) if fts_ok(c2, self.index) else {}
            pinned = self._pinned(l1, cards, named)
            preferred = self._preferred(l1, reader)
        in_scope = [c for c in cards.values() if _in_scope(c, scope, repo, project, aliases)]
        evaluated = [
            (card, *_score(card, variants, fts_hits))
            for card in in_scope
            if card["task_id"] not in pinned
        ]
        weight = rarity([per for _, _, per in evaluated], list(variants))
        scored: list[tuple[dict[str, Any], dict[str, Any], float]] = []
        for card, match, per in evaluated:
            score = sum(v * weight[w] for w, v in per.items())
            if words and not per:
                continue
            scored.append((card, match, round(score, 3)))
        scored.sort(
            key=lambda item: (
                item[0]["task_id"] in preferred,
                item[2],
                str(item[0]["last_activity"]),
            ),
            reverse=True,
        )
        page = scored[offset : offset + limit]
        more = offset + limit < len(scored)
        out = {
            "scope": scope,
            "repo": repo,
            "project": project,
            "query": query,
            "terms": {w: sorted(v) for w, v in variants.items()},
            "pinned": [
                {**_compact(cards[t]), "pinned": why} for t, why in pinned.items() if t in cards
            ],
            "cards": [{**_compact(card), "match": match} for card, match, _ in page],
            "total": len(scored),
            "next_offset": offset + limit if more else None,
            "index": fresh.to_dict(),
            "note": "点名任务与硬依赖总在 pinned 中；看 next_offset 判断是否还有更多；"
            "需要细节时用 context_read 读取入围任务的指定部分并写明理由",
        }
        if scope == "global":
            self._record(
                reader,
                {
                    "tool": "context_catalog",
                    "scope": scope,
                    "query": query,
                    "tasks": [c["task_id"] for c, _, _ in page],
                    "reason": _opt(args, "reason"),
                },
            )
        return out

    def _scope(self, reader: Reader, args: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
        repo, project = _opt(args, "repo") or None, _opt(args, "project") or None
        # Without a scope: the given repository or project; a worker's own repository; global.
        scope = _opt(args, "scope") or (
            "repo" if repo else "project" if project else "repo" if reader.task_id else "global"
        )
        if scope not in SCOPES:
            raise rejected(
                "scope 无效", reason="invalid_input", field="scope", allowed=list(SCOPES)
            )
        if scope in ("repo", "project") and not (repo or project) and reader.task_id:
            with self.ledger.view() as l1:
                row = l1.execute(
                    "SELECT repo FROM tasks WHERE task_id = ?", (reader.task_id,)
                ).fetchone()
            repo = str(row["repo"]) if row else None
        if scope == "repo" and not repo:
            raise rejected("scope=repo 需要 repo", reason="invalid_input", field="repo")
        if scope == "project" and not (project or repo):
            raise rejected("scope=project 需要 project", reason="invalid_input", field="project")
        return scope, repo, project or (repo if scope == "project" else None)

    def _cards(
        self, c2: sqlite3.Connection, l1: sqlite3.Connection, fresh: Freshness
    ) -> dict[str, dict[str, Any]]:
        cards = {r["task_id"]: json.loads(r["card"]) for r in c2.execute("SELECT * FROM cards")}
        for task_id in fresh.lagging:
            facts = task_facts(l1, task_id)
            if facts is None:
                cards.pop(task_id, None)
                continue
            cards[task_id] = fallback_card(facts, cards.get(task_id))
        present = {r["task_id"] for r in l1.execute("SELECT task_id FROM tasks")}
        return {t: c for t, c in cards.items() if t in present}

    @staticmethod
    def _fts_tasks(
        c2: sqlite3.Connection, variants: Mapping[str, set[str]]
    ) -> dict[str, dict[str, int]]:
        """Per term, the tasks whose full text contains it and how many documents do."""
        out: dict[str, dict[str, int]] = {}
        for word, forms in variants.items():
            longs = [f for f in forms if not is_short(f)]
            if not longs:
                continue
            hits: dict[str, int] = {}
            for row in c2.execute(
                "SELECT d.task_id AS task_id, COUNT(*) AS n FROM fts_docs JOIN docs d "
                "ON d.doc_id = fts_docs.rowid WHERE fts_docs MATCH ? GROUP BY d.task_id",
                (_match(longs),),
            ):
                hits[row["task_id"]] = int(row["n"])
            out[word] = hits
        return out

    @staticmethod
    def _pinned(
        l1: sqlite3.Connection, cards: Mapping[str, Any], named: Sequence[str]
    ) -> dict[str, str]:
        pinned: dict[str, str] = {}
        for task_id in named:
            if task_id not in cards:
                continue
            pinned.setdefault(task_id, "named")
            for row in l1.execute(
                "SELECT dep_id FROM dependencies WHERE task_id = ? AND kind != 'inferred' "
                "ORDER BY dep_id",
                (task_id,),
            ):
                pinned.setdefault(str(row["dep_id"]), f"dependency_of:{task_id}")
        return pinned

    @staticmethod
    def _preferred(l1: sqlite3.Connection, reader: Reader) -> set[str]:
        """A worker's recommended read set: its task, the brief's read set and dependencies."""
        if reader.role != "worker" or not reader.task_id:
            return set()
        out = {reader.task_id}
        row = l1.execute(
            "SELECT read_set FROM attempts WHERE task_id = ? AND attempt = ?",
            (reader.task_id, reader.attempt),
        ).fetchone()
        if row and row["read_set"]:
            out |= {str(t) for t in json.loads(row["read_set"])}
        out |= {
            str(r["dep_id"])
            for r in l1.execute(
                "SELECT dep_id FROM dependencies WHERE task_id = ?", (reader.task_id,)
            )
        }
        return out

    # ------------------------------------------------------------------ search

    def search(self, reader: Reader, args: Mapping[str, Any]) -> dict[str, Any]:
        query = _opt(args, "query")
        words = terms(query)
        if not words:
            raise rejected("需要查询词", reason="invalid_input", field="query")
        scope, repo, project = self._scope(reader, args)
        limit = _limit(args, 10)
        fresh = self.freshness()
        with self.index.connection() as c2, self.ledger.view() as l1:
            cards = self._cards(c2, l1, fresh)
            aliases = _aliases(c2)
            variants = {w: _expand(w, aliases) | pieces(w) for w in words}
            fts = fts_ok(c2, self.index)
            longs = sorted({f for forms in variants.values() for f in forms if not is_short(f)})
            hits: list[dict[str, Any]] = []
            if fts and longs:
                for row in c2.execute(
                    "SELECT d.task_id AS task_id, d.kind AS kind, d.ref AS ref, "
                    "snippet(fts_docs, 0, '«', '»', '…', 24) AS snippet FROM fts_docs "
                    "JOIN docs d ON d.doc_id = fts_docs.rowid WHERE fts_docs MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (_match(longs), MAX_LIMIT * 4),
                ):
                    card = cards.get(row["task_id"])
                    if card is None or not _in_scope(card, scope, repo, project, aliases):
                        continue
                    hits.append(
                        {
                            "task_id": row["task_id"],
                            "title": card["title"],
                            "kind": row["kind"],
                            "ref": row["ref"],
                            "snippet": " ".join(str(row["snippet"]).split()),
                        }
                    )
                    if len(hits) >= limit:
                        break
        shorts = sorted({f for forms in variants.values() for f in forms if is_short(f)})
        card_matches = [
            {"task_id": c["task_id"], "title": c["title"], "terms": found}
            for c in cards.values()
            if _in_scope(c, scope, repo, project, aliases)
            and (found := [s for s in shorts if contains(_card_text(c), s)])
        ][:limit]
        self._record(
            reader,
            {
                "tool": "context_search",
                "scope": scope,
                "query": query,
                "tasks": sorted({h["task_id"] for h in hits}),
                "reason": _opt(args, "reason"),
            },
        )
        out: dict[str, Any] = {
            "scope": scope,
            "repo": repo,
            "project": project,
            "query": query,
            "hits": hits,
            "index": fresh.to_dict(),
        }
        if shorts:
            out["short_terms"] = shorts
            out["card_matches"] = card_matches
            out["note"] = "少于 3 个字符的词不进全文索引，只在卡片与别名上做子串匹配（DN007 §5.2）"
        if not fts:
            out["note"] = "本机 SQLite 不支持全文索引，只能在卡片上匹配（DN007 §11 C1）"
        return out

    # ------------------------------------------------------------------ decisions

    def decisions(self, reader: Reader, args: Mapping[str, Any]) -> dict[str, Any]:
        query = _opt(args, "query")
        words = terms(query)
        scope = _opt(args, "scope") or "global"
        if scope not in ("global", "repo", "task"):
            raise rejected("scope 应为 global、repo 或 task", reason="invalid_input", field="scope")
        repo = _opt(args, "repo") or None
        task_id = _opt(args, "task_id") or (reader.task_id if scope == "task" else None)
        limit = _limit(args, 20)
        offset = _offset(args)
        fresh = self.freshness()
        with self.index.connection() as c2, self.ledger.view() as l1:
            repos = {
                r["task_id"]: str(r["repo"]) for r in l1.execute("SELECT task_id, repo FROM tasks")
            }
            if scope == "task":
                if not task_id or task_id not in repos:
                    raise rejected("任务不存在", reason="task_not_found", task_id=task_id)
                repo = repos[task_id]
            if scope == "repo" and not repo:
                raise rejected("scope=repo 需要 repo", reason="invalid_input", field="repo")
            items = [_standing(r) for r in l1.execute("SELECT * FROM user_decisions")]
            items += [_historic(r) for r in c2.execute("SELECT * FROM decisions")]
        chosen: list[tuple[dict[str, Any], int]] = []
        for item in items:
            if not _decision_in_scope(item, scope, repo, task_id, repos):
                continue
            haystack = " ".join(str(item.get(k) or "") for k in ("topic", "text", "detail"))
            score = sum(1 for w in words if contains(haystack, w))
            if words and score == 0:
                continue
            chosen.append((item, score))
        mine = reader.task_id if reader.role == "worker" else task_id
        chosen.sort(
            key=lambda pair: (
                pair[0].get("task_id") == mine and mine is not None,
                pair[0]["kind"] == "standing" and not pair[0].get("superseded_by"),
                pair[1],
                int(pair[0]["source_seq"]),
            ),
            reverse=True,
        )
        page = chosen[offset : offset + limit]
        return {
            "scope": scope,
            "repo": repo,
            "task_id": task_id,
            "query": query,
            "decisions": [item for item, _ in page],
            "total": len(chosen),
            "next_offset": offset + limit if offset + limit < len(chosen) else None,
            "index": fresh.to_dict(),
            "note": "usable_as_basis：in_scope 为范围内的长期偏好；"
            "this_task_only 只在该任务内作为依据，其他任务只能作为先例参考；"
            "this_object_only 为一次性授权；false（已被替代、候选约定）不能作为依据（DN007 §6）",
        }

    # ------------------------------------------------------------------ read

    def read(self, reader: Reader, args: Mapping[str, Any]) -> dict[str, Any]:
        task_ids = _ids(args, "task_ids") or (
            [_opt(args, "task_id")] if _opt(args, "task_id") else []
        )
        if not task_ids:
            raise rejected("需要 task_ids", reason="invalid_input", field="task_ids")
        part = _opt(args, "part")
        if part not in PARTS:
            raise rejected("part 无效", reason="invalid_input", field="part", allowed=list(PARTS))
        reason = " ".join(_opt(args, "reason").split())
        if self.require_reason and len(reason) < MIN_REASON_CHARS:
            raise rejected(
                "context_read 必须写明读取理由（DN007 §7.1）",
                reason="invalid_input",
                field="reason",
            )
        offset = _offset(args)
        sections: list[str] = []
        sources: list[dict[str, Any]] = []
        with self.ledger.view() as l1:
            gathered = {}
            for task_id in task_ids:
                facts = task_facts(l1, task_id)
                if facts is None:
                    raise rejected("任务不存在", reason="task_not_found", task_id=task_id)
                gathered[task_id] = (facts, self._l1_part(l1, facts, part))
        for task_id, (facts, l1_text) in gathered.items():
            text, refs = self._part(facts, part, l1_text)
            sections.append(f"## {task_id} · {part}\n\n{text or '（没有内容）'}")
            sources.append({"task_id": task_id, "refs": refs})
        whole = "\n\n".join(sections)
        chunk = whole[offset : offset + self.max_chars]
        next_offset = offset + len(chunk) if offset + len(chunk) < len(whole) else None
        sequence = self._record(
            reader,
            {
                "tool": "context_read",
                "tasks": task_ids,
                "part": part,
                "reason": reason,
                "offset": offset,
                "chars": len(chunk),
            },
        )
        return {
            "part": part,
            "tasks": task_ids,
            "text": chunk,
            "offset": offset,
            "next_offset": next_offset,
            "total_chars": len(whole),
            "sources": sources,
            "recorded": sequence,
        }

    def _l1_part(self, l1: sqlite3.Connection, facts: Facts, part: str) -> str:
        task_id = facts.task["task_id"]
        if part == "reviews":
            lines = []
            for r in _all(
                l1,
                "SELECT * FROM review_rounds WHERE task_id = ? ORDER BY attempt, round",
                task_id,
            ):
                lines.append(
                    f"- {r['attempt']} R{r['round']}（周期 {r['cycle']}）head {r['head'][:12]} "
                    f"结论 {r['verdict'] or '—'} 决定 {r['decision'] or '—'}"
                )
            for f in _all(
                l1,
                "SELECT * FROM findings WHERE task_id = ? ORDER BY attempt, round, finding_id",
                task_id,
            ):
                lines.append(
                    f"  - R{f['round']} {f['finding_id']} [{f['severity']}"
                    f"{'，阻断' if f['blocking'] else ''}] "
                    f"处置 {f['disposition'] or f['action'] or '—'}："
                    f"{f['reason'] or ''} {f['verification'] or ''}".rstrip()
                )
            return "\n".join(lines)
        if part == "decisions":
            lines = [
                f"- {d['decision_id']}（事件 {d['sequence']}）{d['trigger']} → {d['decision']}："
                f"{d['rationale']} 依据 {d['basis']}"
                for d in _all(
                    l1, "SELECT * FROM decisions WHERE task_id = ? ORDER BY sequence", task_id
                )
            ]
            lines += [
                f"- 判断 {j['item_id']} {j['trigger']}（{j['status']}）：{_short(j['data'])}"
                for j in _all(l1, "SELECT * FROM judgments WHERE task_id = ?", task_id)
            ]
            with self.index.connection() as c2:
                lines += [
                    f"- 用户（{r['channel']} {r['message_ref']}，事件 {r['sequence']}，"
                    f"{r['event_type']}）：{r['text']}"
                    + (f"\n  记录：{r['detail']}" if r["detail"] else "")
                    for r in _all(
                        c2, "SELECT * FROM decisions WHERE task_id = ? ORDER BY sequence", task_id
                    )
                ]
            return "\n".join(lines)
        if part == "outputs":
            return "\n".join(
                f"- {o['output_id']} {o['type']} {o['ref']} 交付 {o['delivery'] or '—'} "
                f"核实 {o['verified_at'] or '—'} 证据 {o['evidence']}"
                for o in _all(l1, "SELECT * FROM outputs WHERE task_id = ?", task_id)
            )
        if part == "follow_ups":
            return "\n".join(
                f"- {f['follow_up_id']} [{f['kind']}/{f['status']}] {f['title']}\n  {f['detail']}"
                for f in _all(
                    l1, "SELECT * FROM follow_ups WHERE task_id = ? ORDER BY recorded_seq", task_id
                )
            )
        if part == "requests":
            out = []
            for r in _all(l1, "SELECT * FROM input_requests WHERE task_id = ?", task_id):
                answer = (json.loads(r["resolution"]) if r["resolution"] else {}) or {}
                out.append(
                    f"- {r['request_id']}（{r['status']}）问：{r['question']}\n"
                    f"  答：{answer.get('answer') or '—'}"
                )
            return "\n".join(out)
        if part == "brief":
            row = l1.execute(
                "SELECT brief_path FROM attempts WHERE task_id = ? AND brief_path IS NOT NULL "
                "ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            return str(row["brief_path"]) if row else ""
        return ""

    def _part(self, facts: Facts, part: str, l1_text: str) -> tuple[str, list[str]]:
        if part == "spec":
            ref = str(facts.task.get("spec_path") or "")
            return (read_text(self.home.root / ref) or "") if ref else "", [ref]
        if part == "brief":
            path = resolve_material(self.home, l1_text) if l1_text else None
            return (read_text(path) or "") if path else "", [l1_text] if l1_text else []
        if part == "report":
            texts, refs = [], []
            for ref in report_refs(facts):
                path = resolve_material(self.home, ref)
                text = read_text(path) if path is not None else None
                if text:
                    texts.append(f"### {ref}\n\n{text}")
                    refs.append(ref)
            return "\n\n".join(texts), refs
        return l1_text, [f"L1:{part}"]

    # ------------------------------------------------------------------ task history

    def task(self, reader: Reader, args: Mapping[str, Any]) -> dict[str, Any]:
        task_id = _opt(args, "task_id") or reader.task_id
        if not task_id:
            raise rejected("需要 task_id", reason="invalid_input", field="task_id")
        fresh = self.freshness()
        with self.ledger.view() as l1:
            rows = _all(l1, "SELECT * FROM tasks WHERE task_id = ?", task_id)
            if not rows:
                raise rejected("任务不存在", reason="task_not_found", task_id=task_id)
            row = rows[0]
            out: dict[str, Any] = {
                "task": {
                    k: row[k]
                    for k in (
                        "task_id",
                        "title",
                        "repo",
                        "kind",
                        "status",
                        "substate",
                        "revision",
                        "cycle",
                        "current_attempt",
                        "goal",
                        "last_activity",
                        "done_at",
                        "accepted_at",
                    )
                },
                "acceptance": json.loads(row["acceptance"] or "[]"),
                "submission": json.loads(row["submission"]) if row["submission"] else None,
                "attempts": [
                    {
                        k: a[k]
                        for k in (
                            "attempt",
                            "phase",
                            "session_ref",
                            "generation",
                            "workdir",
                            "branch",
                            "brief_path",
                            "brief_revision",
                        )
                    }
                    for a in _all(l1, "SELECT * FROM attempts WHERE task_id = ?", task_id)
                ],
                "review_cycles": _all(
                    l1,
                    "SELECT * FROM review_cycles WHERE task_id = ? ORDER BY attempt, cycle",
                    task_id,
                ),
                "review_rounds": _all(
                    l1,
                    "SELECT * FROM review_rounds WHERE task_id = ? ORDER BY attempt, round",
                    task_id,
                ),
                "findings": _all(
                    l1,
                    "SELECT * FROM findings WHERE task_id = ? ORDER BY attempt, round, finding_id",
                    task_id,
                ),
                "decisions": _all(
                    l1, "SELECT * FROM decisions WHERE task_id = ? ORDER BY sequence", task_id
                ),
                "judgments": _all(l1, "SELECT * FROM judgments WHERE task_id = ?", task_id),
                "input_requests": _all(
                    l1, "SELECT * FROM input_requests WHERE task_id = ?", task_id
                ),
                "follow_ups": _all(
                    l1, "SELECT * FROM follow_ups WHERE task_id = ? ORDER BY recorded_seq", task_id
                ),
                "outputs": _all(l1, "SELECT * FROM outputs WHERE task_id = ?", task_id),
                "dependencies": _all(l1, "SELECT * FROM dependencies WHERE task_id = ?", task_id),
            }
        with self.index.connection() as c2:
            timeline = _all(
                c2,
                "SELECT * FROM timeline WHERE task_id = ? ORDER BY sequence DESC LIMIT ?",
                task_id,
                TIMELINE_LIMIT + 1,
            )
        out["timeline"] = list(reversed(timeline[:TIMELINE_LIMIT]))
        out["timeline_truncated"] = len(timeline) > TIMELINE_LIMIT
        out["index"] = fresh.to_dict()
        return out

    # ------------------------------------------------------------------ maintenance

    def rebuild(self) -> dict[str, Any]:
        return self.index.rebuild()

    # ------------------------------------------------------------------ audit

    def _record(self, reader: Reader, data: dict[str, Any]) -> int | None:
        """``context.read``: what a session read and why (DN007 §7.1); belongs to no task."""
        payload = {**data, "reader": reader.describe()}

        def build(ctx: CommitContext) -> list[EventDraft]:
            return [EventDraft(type="context.read", data=payload)]

        result = self.ledger.commit(
            Command(actor=reader.actor(), input={"op": "context.read", **payload}, build=build)
        )
        return result.last_sequence


# ---------------------------------------------------------------------- helpers


def fts_ok(conn: sqlite3.Connection, index: ContextIndex) -> bool:
    return index.fts(conn)


def fallback_card(facts: Facts, old: Mapping[str, Any] | None) -> dict[str, Any]:
    """A card from L1 alone for a task the index has not caught up with (DN007 §5.4)."""
    row = facts.task
    card = dict(old or {})
    card.update(
        {
            "task_id": row["task_id"],
            "title": row["title"],
            "repo": row["repo"],
            "kind": row["kind"],
            "status": row["status"],
            "revision": row["revision"],
            "dependencies": [d["dep_id"] for d in facts.dependencies],
            "hard_dependencies": [
                d["dep_id"] for d in facts.dependencies if d["kind"] != "inferred"
            ],
            "last_activity": row["last_activity"],
            "index_lag": True,
        }
    )
    card.setdefault("project_ids", [row["repo"]])
    card.setdefault("keywords", [])
    card.setdefault("keyword_source", "missing")
    card.setdefault("summary", "")
    card.setdefault("summary_source", "missing")
    card.setdefault("tags", [])
    card.setdefault("outcomes", [])
    return card


def _compact(card: Mapping[str, Any]) -> dict[str, Any]:
    """A card as the catalog returns it: a few hundred characters (DN007 §5.1)."""
    out: dict[str, Any] = {
        k: card[k]
        for k in ("task_id", "title", "repo", "status", "revision", "keywords", "keyword_source")
        if k in card
    }
    out["stale"] = bool((card.get("card_revision") or {}).get("stale"))
    if card.get("keyword_coverage"):
        out["keyword_coverage"] = card["keyword_coverage"]
    summary = str(card.get("summary") or "")
    out["summary"] = summary if len(summary) <= SUMMARY_CHARS else summary[:SUMMARY_CHARS] + "…"
    if card.get("summary_source") != "recorded":
        out["summary_source"] = card.get("summary_source")
    if card.get("project_ids") and card["project_ids"] != [card.get("repo")]:
        out["project_ids"] = card["project_ids"]
    if card.get("dependencies"):
        out["dependencies"] = card["dependencies"]
    outcomes = card.get("outcomes") or []
    if outcomes:
        out["outcomes"] = [o["label"] + " " + o["ref"] for o in outcomes[:OUTCOMES]]
    if card.get("imported"):
        out["imported"] = True
        out["resumable"] = bool(card.get("resumable"))
    if card.get("index_lag"):
        out["index_lag"] = True
    out["last_activity"] = card.get("last_activity")
    return out


def _card_text(card: Mapping[str, Any]) -> str:
    return " ".join(
        [
            str(card.get("task_id") or ""),
            str(card.get("title") or ""),
            str(card.get("repo") or ""),
            " ".join(card.get("keywords") or []),
            " ".join(card.get("project_ids") or []),
            str(card.get("summary") or ""),
        ]
    )


def _score(
    card: Mapping[str, Any],
    variants: Mapping[str, set[str]],
    fts_hits: Mapping[str, Mapping[str, int]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Which fields each query word hit, and its weight before the word's rarity counts."""
    keywords = [str(k) for k in card.get("keywords") or []]
    title = " ".join(
        [
            str(card.get("title") or ""),
            str(card.get("repo") or ""),
            " ".join(card.get("project_ids") or []),
            " ".join(card.get("tags") or []),
        ]
    )
    summary = str(card.get("summary") or "")
    match: dict[str, Any] = {"keywords": [], "title": [], "summary": [], "full_text": 0}
    per_word: dict[str, int] = {}
    for word, forms in variants.items():
        score = 0
        hit_keywords = [k for k in keywords if any(contains(k, f) for f in forms)]
        if hit_keywords:
            match["keywords"] += hit_keywords
            generated = card.get("keyword_source") in ("generated", "missing")
            score += WEIGHTS["generated" if generated else "keywords"]
        if any(contains(title, f) for f in forms):
            match["title"].append(word)
            score += WEIGHTS["title"]
        elif any(contains(summary, f) for f in forms):
            match["summary"].append(word)
            score += WEIGHTS["summary"]
        docs = (fts_hits.get(word) or {}).get(str(card["task_id"]), 0)
        if docs:
            match["full_text"] += docs
            score += 1
        if score:
            per_word[word] = score
    match["keywords"] = list(dict.fromkeys(match["keywords"]))
    return {k: v for k, v in match.items() if v}, per_word


def rarity(evaluated: Sequence[Mapping[str, int]], words: Sequence[str]) -> dict[str, float]:
    """A word that hits most cards says little (inverse document frequency over the scope)."""
    total = len(evaluated)
    found = {w: sum(1 for per in evaluated if per.get(w)) for w in words}
    return {w: math.log((total + 1) / (found[w] + 1)) + 0.1 for w in words}


def _in_scope(
    card: Mapping[str, Any],
    scope: str,
    repo: str | None,
    project: str | None,
    aliases: Mapping[str, set[str]],
) -> bool:
    if scope == "global":
        return True
    if scope == "repo":
        wanted = {f.lower() for f in _expand(str(repo), aliases)}
        return str(card.get("repo") or "").lower() in wanted
    wanted = {f.lower() for f in _expand(str(project), aliases)}
    names = [str(card.get("repo") or ""), *[str(p) for p in card.get("project_ids") or []]]
    return any(n.lower() in wanted for n in names)


def _aliases(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """alias → names it stands for, and each canonical name → its aliases (both directions)."""
    out: dict[str, set[str]] = {}
    for row in conn.execute("SELECT alias, canonical FROM aliases"):
        alias, canonical = str(row["alias"]).lower(), str(row["canonical"])
        out.setdefault(alias, set()).add(canonical)
        out.setdefault(canonical.lower(), set()).add(alias)
    return out


def _expand(word: str, aliases: Mapping[str, set[str]]) -> set[str]:
    return {word, *aliases.get(word.lower(), set())}


def _match(forms: Sequence[str]) -> str:
    """An FTS5 query: any of the forms, each as a phrase."""
    return " OR ".join('"' + f.replace('"', '""') + '"' for f in forms)


def _standing(row: sqlite3.Row) -> dict[str, Any]:
    superseded = row["superseded_by"]
    return {
        "ref": row["ref"],
        "kind": row["kind"],
        "scope": row["scope"],
        "task_id": row["task_id"],
        "topic": row["topic"],
        "text": row["text"],
        "source": json.loads(row["verified_source"] or "{}"),
        "supersedes": json.loads(row["supersedes"] or "[]"),
        "superseded_by": superseded,
        "time": row["recorded_at"],
        "source_seq": row["source_seq"],
        "usable_as_basis": False if superseded else "in_scope",
    }


def _historic(row: sqlite3.Row) -> dict[str, Any]:
    kind = row["kind"]
    return {
        "ref": row["ref"],
        "kind": kind,
        "scope": f"task:{row['task_id']}" if row["task_id"] else "object",
        "task_id": row["task_id"],
        "revision": row["revision"],
        "event_type": row["event_type"],
        "channel": row["channel"],
        "message_ref": row["message_ref"],
        "text": row["text"],
        "detail": row["detail"],
        "target": json.loads(row["target"] or "[]"),
        "time": row["time"],
        "source_seq": row["sequence"],
        "usable_as_basis": "this_task_only" if kind == "task" else "this_object_only",
    }


def _decision_in_scope(
    item: Mapping[str, Any],
    scope: str,
    repo: str | None,
    task_id: str | None,
    repos: Mapping[str, str],
) -> bool:
    if scope == "global":
        return True
    if item["kind"] == "standing":
        kind, _, name = str(item["scope"]).partition(":")
        return item["scope"] == "global" or (kind == "repo" and name == repo) or kind == "project"
    owner = item.get("task_id")
    if scope == "task":
        return owner == task_id
    return owner is not None and repos.get(str(owner)) == repo


def _all(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]  # noqa: SIM118


def _short(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    try:
        parsed = json.loads(text)
        text = str(parsed.get("text") or text) if isinstance(parsed, dict) else text
    except ValueError:
        pass
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _opt(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise rejected(f"{key} 应为字符串", reason="invalid_input", field=key)
    return value.strip()


def _ids(args: Mapping[str, Any], key: str) -> list[str]:
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise rejected(f"{key} 应为任务 ID 数组", reason="invalid_input", field=key)
    return list(dict.fromkeys(str(v).strip().upper() for v in value))


def _limit(args: Mapping[str, Any], default: int) -> int:
    value = args.get("limit", default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise rejected("limit 应为正整数", reason="invalid_input", field="limit")
    return min(value, MAX_LIMIT)


def _offset(args: Mapping[str, Any]) -> int:
    value = args.get("offset", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise rejected("offset 应为非负整数", reason="invalid_input", field="offset")
    return value
