# Ledger data and L1 rows are untyped JSON; unknown-type reports carry no signal.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
# pyright: reportUnknownVariableType=false
"""L2 retrieval index ``cache/context.sqlite`` (DN007 §5, §12.2).

The index is a cache: it is derived from the ledger (L0), the projection (L1) and the source
material in the data directory, and is rebuilt whenever it is missing, damaged, of another
version, or does not belong to the ledger's history. It is never a basis for a decision.

Updating it: the events after ``indexed_seq`` tell which tasks changed and which user decisions
were made. Their L1 rows are read under the commit lock (briefly); files are read and the index
written outside it, serialized by the ``context`` lock. ``indexed_seq`` then moves to the
projection's sequence, with that event's checksum so that another history is detected.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections import Counter
from collections.abc import Generator, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tsw.events import Event, digest
from tsw.faults import fault
from tsw.home import Home
from tsw.ledger import Ledger
from tsw.projection import projected_tail
from tsw.storage.base import LedgerTail, StorageBackend

from .text import TAG_PATTERN, generated_keywords, read_text

SCHEMA_VERSION = 1
CONTEXT_LOCK = "context"
LEGACY_SYSTEM = "orchestrator-agent"
KEYWORD_INDEX = f"legacy/{LEGACY_SYSTEM}/metadata/task-keywords/index.json"
SUMMARY_FALLBACK_CHARS = 200
TIMELINE_TEXT_CHARS = 300
# Event fields that say what happened, in the order they are tried for the timeline.
_DETAIL_FIELDS = ("text", "summary", "answer", "reason", "decision", "rationale", "question")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards(
    task_id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    registered_seq INTEGER NOT NULL,
    last_activity TEXT NOT NULL,
    stale INTEGER NOT NULL,
    card TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aliases(
    alias TEXT NOT NULL,
    canonical TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY(alias, canonical)
);
CREATE TABLE IF NOT EXISTS docs(
    doc_id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    chars INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_task ON docs(task_id);
CREATE TABLE IF NOT EXISTS decisions(
    ref TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    task_id TEXT,
    revision INTEGER,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    message_ref TEXT NOT NULL,
    text TEXT NOT NULL,
    detail TEXT NOT NULL,
    target TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS timeline(
    sequence INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    time TEXT NOT NULL,
    type TEXT NOT NULL,
    attempt TEXT,
    revision INTEGER,
    actor TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS timeline_task ON timeline(task_id, sequence);
"""


@dataclass
class Freshness:
    """How current the index is for a query (DN007 §5.4)."""

    indexed_seq: int
    projected_seq: int
    fts: bool
    lagging: list[str] = field(default_factory=lambda: [])
    error: str | None = None

    @property
    def lag(self) -> bool:
        return self.indexed_seq < self.projected_seq

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "indexed_seq": self.indexed_seq,
            "projected_seq": self.projected_seq,
            "full_text": self.fts,
        }
        if self.lag:
            out["index_lag"] = True
            out["lagging_tasks"] = self.lagging
            out["note"] = (
                "索引落后：落后任务按 L1 的标题、仓库与依赖参与结果（标为 index_lag），"
                "它们的全文片段在补齐前不可检索"
            )
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Facts:
    """What the index needs about one task, read from L1 under the commit lock."""

    task: dict[str, Any]
    dependencies: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    summary: dict[str, Any] | None
    imported: dict[str, Any] | None
    decisions: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    follow_ups: list[dict[str, Any]]
    requests: list[dict[str, Any]]
    notes: list[dict[str, Any]]


@dataclass
class Scan:
    """What the events after ``indexed_seq`` changed."""

    end: LedgerTail
    touched: set[str] = field(default_factory=set[str])
    decisions: list[dict[str, Any]] = field(default_factory=lambda: [])
    timeline: list[tuple[Any, ...]] = field(default_factory=lambda: [])
    # (sequence, old id, new id) of each task.renumbered: what the old id did before belongs to
    # the new id; a task that takes the old id later is another task (DN005 §4.4).
    renames: list[tuple[int, str, str]] = field(default_factory=lambda: [])


def outputs_digest(outputs: Iterable[Mapping[str, Any]]) -> str:
    """The same digest as :func:`tsw.machine.context_tools.outputs_digest`, from rows."""
    rows = sorted(outputs, key=lambda r: str(r["output_id"]))
    return digest([[r["output_id"], r["type"], r["ref"], r["delivery"]] for r in rows])


def _rows(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in conn.execute(sql, params)]  # noqa: SIM118


def task_facts(conn: sqlite3.Connection, task_id: str) -> Facts | None:
    found = _rows(conn, "SELECT * FROM tasks WHERE task_id = ?", task_id)
    if not found:
        return None
    summaries = _rows(conn, "SELECT * FROM task_summaries WHERE task_id = ?", task_id)
    imported = _rows(conn, "SELECT * FROM imported_tasks WHERE task_id = ?", task_id)
    return Facts(
        task=found[0],
        dependencies=_rows(
            conn, "SELECT dep_id, kind, reason FROM dependencies WHERE task_id = ?", task_id
        ),
        outputs=_rows(
            conn,
            "SELECT output_id, type, ref, delivery FROM outputs WHERE task_id = ? "
            "ORDER BY output_id",
            task_id,
        ),
        summary=summaries[0] if summaries else None,
        imported=imported[0] if imported else None,
        decisions=_rows(
            conn,
            "SELECT decision_id, trigger, decision, rationale FROM decisions WHERE task_id = ? "
            "ORDER BY sequence",
            task_id,
        ),
        findings=_rows(
            conn,
            "SELECT round, finding_id, severity, action, reason, verification FROM findings "
            "WHERE task_id = ? ORDER BY attempt, round, finding_id",
            task_id,
        ),
        follow_ups=_rows(
            conn,
            "SELECT follow_up_id, kind, title, detail, status FROM follow_ups WHERE task_id = ? "
            "ORDER BY recorded_seq",
            task_id,
        ),
        requests=_rows(
            conn,
            "SELECT request_id, question, resolution FROM input_requests WHERE task_id = ?",
            task_id,
        ),
        notes=_rows(
            conn, "SELECT text FROM task_notes WHERE task_id = ? ORDER BY sequence", task_id
        ),
    )


# ---------------------------------------------------------------------- user decisions in events


def sources_in(value: Any) -> Generator[Mapping[str, Any], None, None]:
    """Every ``VerifiedSource`` in an event's data (DN006 §8.5)."""
    if isinstance(value, Mapping):
        if (
            isinstance(value.get("channel"), str)
            and isinstance(value.get("text"), str)
            and isinstance(value.get("target"), list)
        ):
            yield value
            return
        for item in value.values():
            yield from sources_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from sources_in(item)


def _detail(data: Mapping[str, Any]) -> str:
    for key in ("answer", "summary", "reason", "decision", "note"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def decision_docs(event: Event) -> list[dict[str, Any]]:
    """User decisions an event carries (DN007 §12.2). Standing preferences are read from L1."""
    if event.type == "user.preference_recorded":
        return []
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for source in sources_in(dict(event.data)):
        key = (str(source.get("message_ref") or ""), str(source.get("text")))
        if key in seen:
            continue
        seen.add(key)
        target = list(source.get("target") or [])
        what = str(target[2]) if len(target) > 2 else ""
        kind = "one_time" if event.task_id is None or what == "authorize" else "task"
        ref = f"E{event.sequence}" if not found else f"E{event.sequence}-{len(found) + 1}"
        found.append(
            {
                "ref": ref,
                "sequence": event.sequence,
                "time": event.time,
                "event_type": event.type,
                "task_id": event.task_id,
                "revision": event.revision,
                "kind": kind,
                "channel": str(source.get("channel")),
                "message_ref": str(source.get("message_ref") or ""),
                "text": str(source.get("text")),
                "detail": _detail(event.data),
                "target": json.dumps(target, ensure_ascii=False),
            }
        )
    return found


def timeline_row(event: Event) -> tuple[Any, ...] | None:
    if event.task_id is None:
        return None
    data = dict(event.data)
    text = ""
    for key in _DETAIL_FIELDS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            break
    if not text and data.get("transition"):
        text = f"{data['transition']}"
    return (
        event.sequence,
        event.task_id,
        event.time,
        event.type,
        event.attempt,
        event.revision,
        str(event.actor.get("role") or ""),
        text[:TIMELINE_TEXT_CHARS],
    )


# ---------------------------------------------------------------------- the index


class ContextIndex:
    def __init__(self, home: Home, ledger: Ledger, *, path: Path | None = None) -> None:
        self.home = home
        self.ledger = ledger
        self.storage: StorageBackend = ledger.storage
        self.path = path or home.cache_dir / "context.sqlite"
        self.tokenizer = "trigram"
        self.repo_aliases: dict[str, list[str]] = {}
        self._legacy: dict[str, Any] | None = None

    # ------------------------------------------------------------------ open

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """The index, created or recreated when missing, damaged or of another version."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._try_open()
        if conn is None:
            self.discard()
            conn = self._try_open()
            if conn is None:
                raise OSError(f"cannot open {self.path}")
        try:
            yield conn
        finally:
            conn.close()

    def _try_open(self) -> sqlite3.Connection | None:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(SCHEMA_SQL)
            version = _meta(conn, "version")
            if version is None:
                _set_meta(conn, "version", str(SCHEMA_VERSION))
                _set_meta(conn, "fts", "1" if self._create_fts(conn) else "0")
            elif version != str(SCHEMA_VERSION) or (
                _meta(conn, "fts") == "1" and not _has_table(conn, "fts_docs")
            ):
                conn.close()
                return None
        except sqlite3.DatabaseError:
            conn.close()
            return None
        return conn

    def _create_fts(self, conn: sqlite3.Connection) -> bool:
        """FTS5 with the configured tokenizer (DN007 §5.2, C1); without it, no full text."""
        try:
            conn.execute(
                f"CREATE VIRTUAL TABLE fts_docs USING fts5(body, tokenize='{self.tokenizer}')"
            )
        except sqlite3.OperationalError:
            return False
        return True

    def discard(self) -> None:
        for suffix in ("", "-journal", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def fts(self, conn: sqlite3.Connection) -> bool:
        return _meta(conn, "fts") == "1"

    @staticmethod
    def indexed(conn: sqlite3.Connection) -> LedgerTail:
        seq, checksum = _meta(conn, "indexed_seq"), _meta(conn, "indexed_checksum")
        if seq is None or checksum is None:
            return LedgerTail.empty()
        return LedgerTail(int(seq), checksum)

    # ------------------------------------------------------------------ update

    def refresh(self, *, full: bool = False) -> Freshness:
        """Bring the index up to the projection; returns how current it is afterwards."""
        with self.storage.lock(CONTEXT_LOCK, ttl=600), self.connection() as conn:
            start = LedgerTail.empty() if full else self.indexed(conn)
            if start.sequence and not self._same_history(start):
                start = LedgerTail.empty()
            scan = self.scan(start.sequence)
            with self.ledger.view() as l1:
                projected = projected_tail(l1)
                if projected.sequence > scan.end.sequence:
                    more = self.scan(scan.end.sequence, upto=projected.sequence)
                    scan.touched |= more.touched
                    scan.decisions += more.decisions
                    scan.timeline += more.timeline
                    scan.renames += more.renames
                    scan.end = more.end
                rebuild = start.sequence == 0
                present = {r["task_id"] for r in l1.execute("SELECT task_id FROM tasks")}
                wanted = present if rebuild else scan.touched & present
                facts = {t: f for t in sorted(wanted) if (f := task_facts(l1, t)) is not None}
                titles = _rows(l1, "SELECT task_id, title, repo FROM tasks")
            if scan.end.sequence == start.sequence and not rebuild:
                return Freshness(start.sequence, projected.sequence, self.fts(conn))
            fault("context.refresh")
            self._write(conn, facts, present, titles, scan, rebuild=rebuild)
            fts = self.fts(conn)
        self._prune_queue(scan.end.sequence)
        return Freshness(scan.end.sequence, scan.end.sequence, fts)

    def rebuild(self) -> dict[str, Any]:
        """Discard the index and build it again from L0, L1 and the source material."""
        with self.storage.lock(CONTEXT_LOCK, ttl=600):
            self.discard()
        fresh = self.refresh(full=True)
        with self.connection() as conn:
            sources = Counter(
                json.loads(r["card"]).get("keyword_source")
                for r in conn.execute("SELECT card FROM cards")
            )
            return {
                "indexed_seq": fresh.indexed_seq,
                "cards": sum(sources.values()),
                "keyword_sources": dict(sorted(sources.items())),
                "stale_cards": conn.execute("SELECT COUNT(*) FROM cards WHERE stale").fetchone()[0],
                "docs": conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0],
                "decisions": conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                "full_text": fresh.fts,
            }

    def _same_history(self, start: LedgerTail) -> bool:
        for event in self.storage.read(since=start.sequence - 1):
            return event.sequence == start.sequence and event.checksum == start.checksum
        return False

    def scan(self, since: int, *, upto: int | None = None) -> Scan:
        scan = Scan(end=LedgerTail.empty())
        last: LedgerTail | None = None
        if since:
            for event in self.storage.read(since=since - 1):
                last = LedgerTail(event.sequence, event.checksum)
                break
        for event in self.storage.read(since=since):
            if upto is not None and event.sequence > upto:
                break
            if event.type == "task.renumbered" and event.task_id is not None:
                old = str(event.data.get("from") or "")
                if old:
                    scan.renames.append((event.sequence, old, event.task_id))
                    scan.touched.add(old)
            if event.task_id is not None:
                scan.touched.add(event.task_id)
                row = timeline_row(event)
                if row is not None:
                    scan.timeline.append(row)
            scan.decisions += decision_docs(event)
            last = LedgerTail(event.sequence, event.checksum)
        scan.end = last or LedgerTail.empty()
        return scan

    def _prune_queue(self, upto: int) -> None:
        """The L1 queue entries the index has consumed (DN007 §5.4)."""
        with contextlib.suppress(Exception), self.ledger.view() as l1:
            l1.execute("DELETE FROM index_queue WHERE sequence <= ?", (upto,))

    # ------------------------------------------------------------------ write

    def _write(
        self,
        conn: sqlite3.Connection,
        facts: Mapping[str, Facts],
        present: set[str],
        titles: list[dict[str, Any]],
        scan: Scan,
        *,
        rebuild: bool,
    ) -> None:
        fts = self.fts(conn)
        built = {task_id: self._card(f) for task_id, f in facts.items()}
        documents = {task_id: self._docs(f) for task_id, f in facts.items()}
        conn.execute("BEGIN IMMEDIATE")
        try:
            if rebuild:
                for table in ("cards", "docs", "decisions", "timeline", "aliases"):
                    conn.execute(f"DELETE FROM {table}")
                if fts:
                    conn.execute("DELETE FROM fts_docs")
            gone = [r["task_id"] for r in conn.execute("SELECT task_id FROM cards")]
            for task_id in [t for t in gone if t not in present]:
                self._drop_task(conn, task_id, fts)
            for task_id, card in built.items():
                if not rebuild:
                    self._drop_task(conn, task_id, fts)
                conn.execute(
                    "INSERT INTO cards(task_id, repo, status, title, registered_seq, "
                    "last_activity, stale, card) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        card["repo"],
                        card["status"],
                        card["title"],
                        int(facts[task_id].task["registered_seq"]),
                        card["last_activity"],
                        1 if card["card_revision"]["stale"] else 0,
                        json.dumps(card, ensure_ascii=False),
                    ),
                )
                for kind, ref, body in documents[task_id]:
                    cur = conn.execute(
                        "INSERT INTO docs(task_id, kind, ref, chars) VALUES(?, ?, ?, ?)",
                        (task_id, kind, ref, len(body)),
                    )
                    if fts:
                        conn.execute(
                            "INSERT INTO fts_docs(rowid, body) VALUES(?, ?)", (cur.lastrowid, body)
                        )
            for doc in scan.decisions:
                conn.execute(
                    "INSERT OR REPLACE INTO decisions(ref, sequence, time, event_type, task_id, "
                    "revision, kind, channel, message_ref, text, detail, target) "
                    "VALUES(:ref, :sequence, :time, :event_type, :task_id, :revision, :kind, "
                    ":channel, :message_ref, :text, :detail, :target)",
                    doc,
                )
            conn.executemany(
                "INSERT OR REPLACE INTO timeline(sequence, task_id, time, type, attempt, "
                "revision, actor, text) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                scan.timeline,
            )
            for sequence, old, new in scan.renames:
                for table in ("timeline", "decisions"):
                    conn.execute(
                        f"UPDATE {table} SET task_id = ? WHERE task_id = ? AND sequence < ?",
                        (new, old, sequence),
                    )
            self._write_aliases(conn, titles)
            _set_meta(conn, "indexed_seq", str(scan.end.sequence))
            _set_meta(conn, "indexed_checksum", scan.end.checksum)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _drop_task(conn: sqlite3.Connection, task_id: str, fts: bool) -> None:
        if fts:
            conn.execute(
                "DELETE FROM fts_docs WHERE rowid IN (SELECT doc_id FROM docs WHERE task_id = ?)",
                (task_id,),
            )
        conn.execute("DELETE FROM docs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM cards WHERE task_id = ?", (task_id,))

    def _write_aliases(self, conn: sqlite3.Connection, titles: list[dict[str, Any]]) -> None:
        """Configured repository aliases and title tags that mostly name one repository
        (``[tsw]`` → ``task-steward``), DN007 §12.2. Projects are matched on the cards."""
        conn.execute("DELETE FROM aliases")
        pairs: set[tuple[str, str, str]] = set()
        for repo, names in self.repo_aliases.items():
            for name in names:
                if name.lower() != repo.lower():
                    pairs.add((name.lower(), repo, "config"))
        tags: dict[str, Counter[str]] = {}
        for row in titles:
            for tag in _tags(str(row["title"])):
                tags.setdefault(tag.lower(), Counter())[str(row["repo"])] += 1
        for tag, counts in tags.items():
            repo, hits = counts.most_common(1)[0]
            if hits >= 2 and hits * 10 >= sum(counts.values()) * 6 and tag != repo.lower():
                pairs.add((tag, repo, "title_tag"))
        conn.executemany(
            "INSERT OR IGNORE INTO aliases(alias, canonical, kind) VALUES(?, ?, ?)", sorted(pairs)
        )

    # ------------------------------------------------------------------ cards

    def legacy_records(self) -> dict[str, Any]:
        """Imported tasks' keyword records, by task (DN007 §8): only data directory files."""
        if self._legacy is None:
            self._legacy = {}
            index = self.home.root / KEYWORD_INDEX
            try:
                records = json.loads(index.read_text(encoding="utf-8")).get("records") or {}
            except (OSError, ValueError):
                records = {}
            base = index.parent.parent.parent  # legacy/<system>/
            for task_id, entry in records.items():
                ref = str((entry or {}).get("ref") or "")
                path = base / ref
                if not ref or ".." in Path(ref).parts:
                    continue
                with contextlib.suppress(OSError, ValueError):
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(record, dict):
                        record["_ref"] = f"legacy/{LEGACY_SYSTEM}/{ref}"
                        self._legacy[str(task_id)] = record
        return self._legacy

    def _spec_text(self, facts: Facts) -> str | None:
        spec_path = str(facts.task.get("spec_path") or "")
        return read_text(self.home.root / spec_path) if spec_path else None

    def _card(self, facts: Facts) -> dict[str, Any]:
        row = facts.task
        task_id = str(row["task_id"])
        revision = int(row["revision"])
        outputs_now = outputs_digest(facts.outputs)
        legacy = self.legacy_records().get(task_id) if facts.imported else None
        keywords: list[str]
        coverage: Any = None
        if facts.summary is not None:
            keywords = list(json.loads(facts.summary["keywords"]))
            source = "recorded"
            stale = (
                int(facts.summary["bound_revision"]) != revision
                or facts.summary["bound_outputs"] != outputs_now
            )
        elif legacy is not None:
            keywords = [str(k) for k in legacy.get("keywords") or []]
            source = "legacy"
            recorded_revision = (legacy.get("source") or {}).get("revision")
            stale = recorded_revision is not None and int(recorded_revision) != revision
            coverage = legacy.get("coverage")
        else:
            keywords = generated_keywords(str(row["title"]), self._spec_text(facts))
            source = "generated"
            stale = True
        recorded = str((facts.summary or {}).get("summary") or "")
        goal = " ".join(str(row.get("goal") or "").split())
        summary = recorded or goal[:SUMMARY_FALLBACK_CHARS]
        projects = [str(p) for p in (legacy or {}).get("project_ids") or []] or [str(row["repo"])]
        card: dict[str, Any] = {
            "task_id": task_id,
            "title": row["title"],
            "repo": row["repo"],
            "project_ids": projects,
            "kind": row["kind"],
            "status": row["status"],
            "revision": revision,
            "keywords": keywords,
            "keyword_source": source,
            "summary": summary,
            "summary_source": "recorded" if recorded else "goal",
            "tags": _tags(str(row["title"])),
            "outcomes": [
                {
                    "type": o["type"],
                    "ref": o["ref"],
                    "label": f"{o['output_id']} · {o['delivery']}"
                    if o["delivery"]
                    else o["output_id"],
                }
                for o in facts.outputs[:6]
            ],
            "dependencies": [d["dep_id"] for d in facts.dependencies],
            "hard_dependencies": [
                d["dep_id"] for d in facts.dependencies if d["kind"] != "inferred"
            ],
            "last_activity": row["last_activity"],
            "imported": facts.imported is not None,
            "resumable": bool(facts.imported["resumable"]) if facts.imported else True,
            "card_revision": {
                "task_revision": revision,
                "outputs": outputs_now[:12],
                "stale": stale,
            },
        }
        if coverage is not None:
            card["keyword_coverage"] = coverage
        return card

    def _docs(self, facts: Facts) -> list[tuple[str, str, str]]:
        """Full-text entries of a task: (kind, ref, text)."""
        row = facts.task
        out: list[tuple[str, str, str]] = []
        spec_path = str(row.get("spec_path") or "")
        spec = self._spec_text(facts)
        if spec:
            out.append(("spec", spec_path, spec))
        for ref in report_refs(facts):
            path = resolve_material(self.home, ref)
            text = read_text(path) if path is not None else None
            if text:
                out.append(("report", ref, text))
        lines = [
            f"{d['decision_id']} {d['trigger']} → {d['decision']}：{d['rationale']}"
            for d in facts.decisions
        ]
        if lines:
            out.append(("decisions", "decisions", "\n".join(lines)))
        lines = [
            f"R{f['round']} {f['finding_id']} [{f['severity']}] {f['action'] or ''}："
            f"{f['reason'] or ''} {f['verification'] or ''}"
            for f in facts.findings
        ]
        if lines:
            out.append(("reviews", "findings", "\n".join(lines)))
        lines = [f"{f['follow_up_id']} {f['title']}：{f['detail']}" for f in facts.follow_ups]
        if lines:
            out.append(("follow_ups", "follow_ups", "\n".join(lines)))
        lines = []
        for r in facts.requests:
            answer = (json.loads(r["resolution"]) if r["resolution"] else {}) or {}
            lines.append(f"{r['request_id']} 问：{r['question']}\n答：{answer.get('answer') or ''}")
        if lines:
            out.append(("requests", "input_requests", "\n".join(lines)))
        if facts.notes:
            out.append(("notes", "task_notes", "\n".join(str(n["text"]) for n in facts.notes)))
        return out


# ---------------------------------------------------------------------- materials


def report_refs(facts: Facts) -> list[str]:
    """Where the task's reports are: the last submission, or the imported task's reports."""
    refs: list[str] = []
    submission = json.loads(facts.task["submission"]) if facts.task.get("submission") else None
    report = (submission or {}).get("report") or {}
    for key in ("md_path", "html_path"):
        if report.get(key):
            refs.append(str(report[key]))
            break
    if facts.imported:
        legacy = json.loads(facts.imported["legacy"] or "{}")
        for key in ("worker_report", "review_report"):
            if isinstance(legacy.get(key), str) and legacy[key]:
                refs.append(str(legacy[key]))
    return list(dict.fromkeys(refs))


def resolve_material(home: Home, ref: str) -> Path | None:
    """A ``runs/``, ``todo/`` or ``legacy://`` reference inside the data directory."""
    text = ref.replace("\\", "/")
    if text.startswith("legacy://"):
        system, _, rest = text[len("legacy://") :].partition("/")
        candidates = [home.root / "legacy" / system / rest]
    elif text.startswith(("runs/", "todo/", "legacy/")):
        candidates = [home.root / text, home.root / "legacy" / LEGACY_SYSTEM / text]
    else:
        return None
    if ".." in Path(text).parts:
        return None
    for path in candidates:
        if path.is_file():
            return path
    return None


def _tags(title: str) -> list[str]:
    return [t.strip() for t in TAG_PATTERN.findall(title) if t.strip()]


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row is not None
