"""Ledger event format (DN001 §8.2).

Every event is stored as one line of canonical JSON. ``checksum`` is the SHA-256 of the canonical
JSON of the event without its ``checksum`` field; ``prev`` is the previous event's checksum, so
the ledger forms a digest chain over its JSON text (never over executables).

``command_digest`` is an optional addition to the DN001 format: the SHA-256 of the canonical
command input, so that a repeated ``command_id`` with different content is rejected even after
the projection has been rebuilt.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from tsw.errors import rejected

SCHEMA = 1
GENESIS = "0" * 64

_REQUIRED = ("schema", "sequence", "time", "type", "actor", "prev", "checksum")


def canonical_bytes(value: Any) -> bytes:
    """Canonical JSON: sorted keys, no whitespace, UTF-8, no NaN."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EventDraft:
    """An event before it receives a sequence number and its place in the chain."""

    type: str
    task_id: str | None = None
    revision: int | None = None
    attempt: str | None = None
    data: Mapping[str, Any] = field(default_factory=lambda: {})


@dataclass(frozen=True)
class Event:
    schema: int
    sequence: int
    time: str
    type: str
    actor: Mapping[str, Any]
    prev: str
    checksum: str
    task_id: str | None = None
    revision: int | None = None
    attempt: str | None = None
    command_id: str | None = None
    command_digest: str | None = None
    data: Mapping[str, Any] = field(default_factory=lambda: {})

    def body(self) -> dict[str, Any]:
        """The event as a dict without ``checksum`` and without empty optional fields."""
        out: dict[str, Any] = {
            "schema": self.schema,
            "sequence": self.sequence,
            "time": self.time,
            "type": self.type,
            "actor": dict(self.actor),
            "prev": self.prev,
            "data": dict(self.data),
        }
        for key in ("task_id", "revision", "attempt", "command_id", "command_digest"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out

    def to_dict(self) -> dict[str, Any]:
        out = self.body()
        out["checksum"] = self.checksum
        return out

    def to_line(self) -> bytes:
        return canonical_bytes(self.to_dict()) + b"\n"

    def computed_checksum(self) -> str:
        return digest(self.body())

    @staticmethod
    def from_line(line: bytes) -> Event:
        try:
            raw = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise rejected("账本行不是合法的 JSON", reason="ledger_corrupt") from exc
        if not isinstance(raw, dict):
            raise rejected("账本行不是 JSON 对象", reason="ledger_corrupt")
        return Event.from_dict(raw)  # pyright: ignore[reportUnknownArgumentType]

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> Event:
        missing = [key for key in _REQUIRED if key not in raw]
        if missing:
            raise rejected("事件缺少字段", reason="ledger_corrupt", missing=missing)
        event = Event(
            schema=int(raw["schema"]),
            sequence=int(raw["sequence"]),
            time=str(raw["time"]),
            type=str(raw["type"]),
            actor=dict(raw["actor"]),
            prev=str(raw["prev"]),
            checksum=str(raw["checksum"]),
            task_id=raw.get("task_id"),
            revision=raw.get("revision"),
            attempt=raw.get("attempt"),
            command_id=raw.get("command_id"),
            command_digest=raw.get("command_digest"),
            data=dict(raw.get("data") or {}),
        )
        if event.schema != SCHEMA:
            raise rejected(
                "不支持的事件 schema，请升级 tsw", reason="unsupported_schema", schema=event.schema
            )
        if event.computed_checksum() != event.checksum:
            raise rejected("事件摘要不匹配", reason="ledger_corrupt", sequence=event.sequence)
        return event


def seal(
    draft: EventDraft,
    *,
    sequence: int,
    prev: str,
    time: str,
    actor: Mapping[str, Any],
    command_id: str | None,
    command_digest: str | None,
) -> Event:
    """Give a draft its sequence and chain position and compute its checksum."""
    unsealed = Event(
        schema=SCHEMA,
        sequence=sequence,
        time=time,
        type=draft.type,
        actor=dict(actor),
        prev=prev,
        checksum="",
        task_id=draft.task_id,
        revision=draft.revision,
        attempt=draft.attempt,
        command_id=command_id,
        command_digest=command_digest,
        data=dict(draft.data),
    )
    return replace(unsealed, checksum=unsealed.computed_checksum())
