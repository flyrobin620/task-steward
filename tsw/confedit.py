# Configuration is untyped JSON walked generically; unknown-type reports carry no signal here.
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
"""Changing the unified configuration through tsw (DN003 §7).

The orchestrator changes ``config/task-steward.json`` with ``config_change`` and ``config_revert``.
A change is validated as a whole (the effective configuration after the change must pass
DN003 §4), recorded in the ledger as ``config.changed`` with the file text before and after, and
only then written. The event comes first: when the file write is interrupted, the next commit
writes it (:func:`apply_pending`), but only while the file still holds the recorded "before"
text, so a later hand edit is never overwritten.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tsw.commands import CommitContext
from tsw.config import CONFIG_FILE
from tsw.errors import rejected
from tsw.events import Event, sha256_hex
from tsw.home import Home
from tsw.projection import get_meta, set_meta
from tsw.storage.base import StorageBackend
from tsw.storage.fs import atomic_write

CHANGE_EVENT = "config.changed"
LAST_CHANGE = "config:last_change"  # projection meta: the latest change (sequence, id)
COUNT = "config:count"  # projection meta: number of changes, for the next change id
# The latest change known to be in the file (or deliberately left out because the file was
# edited by hand). Kept outside the projection, so a ``ledger rebuild`` does not forget it and
# write an old change over a later hand edit.
APPLIED_FILE = "config-applied.json"
NO_FILE = "none"  # the digest recorded when the file does not exist

# Keys that grant or withdraw authority (DN003 §7.2): changing any of them, in either direction,
# needs the user's own words. ``*`` matches one map key; a key matches a pattern when one is a
# prefix of the other, so replacing a whole parent object is caught too.
PERMISSION_KEYS = (
    "repositories.*.delivery",
    "repositories.*.auth_profile",
    "repositories.*.commit_identity",
    "accounts",
    "policies",
    "roles.*.permission_mode",
    "roles.*.session",
    "roles.reviewer",
    "roles.worker.allow_in_task_subagents",
    "plugins",
    "input.triage",
    "input.channels",
    "review.block_on",
    "review.max_rounds",
    "review.orchestrator_extension_limit",
)


# ---------------------------------------------------------------------- the file


def config_path(home: Home) -> Path:
    return home.path(CONFIG_FILE)


def read_text(home: Home) -> str | None:
    path = config_path(home)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def text_sha(text: str | None) -> str:
    return NO_FILE if text is None else sha256_hex(text.encode("utf-8"))


def parse(text: str | None) -> dict[str, Any] | None:
    """The file's object; ``{}`` when there is no file; None when it is not a JSON object."""
    if text is None:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def render(values: Mapping[str, Any]) -> str:
    return json.dumps(values, ensure_ascii=False, indent=2) + "\n"


def write_text(home: Home, text: str | None) -> None:
    path = config_path(home)
    if text is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, text.encode("utf-8"))


# ---------------------------------------------------------------------- operations


def split_path(path: Any) -> list[str]:
    """A dotted path, or a list of segments for map keys that contain dots."""
    if isinstance(path, list) and all(isinstance(p, str) and p for p in path):
        return [str(p) for p in path]
    if isinstance(path, str):
        return [] if path == "" else path.split(".")
    raise rejected("path 应为点号路径或字符串数组", reason="invalid_input", field="path")


def apply_ops(values: Mapping[str, Any], ops: Sequence[Any]) -> dict[str, Any]:
    """``set`` (an empty path replaces the whole file) and ``unset``; applied in order."""
    out: dict[str, Any] = copy.deepcopy(dict(values))
    if not ops:
        raise rejected("ops 不能为空", reason="invalid_input", field="ops")
    for index, op in enumerate(ops):
        if not isinstance(op, Mapping) or op.get("op") not in ("set", "unset"):
            raise rejected(
                "每个操作应为 {op: set/unset, path, value}", reason="invalid_input", index=index
            )
        parts = split_path(op.get("path"))
        if op["op"] == "set" and "value" not in op:
            raise rejected("set 需要 value", reason="invalid_input", index=index)
        if not parts:
            if op["op"] == "unset" or not isinstance(op["value"], dict):
                raise rejected(
                    "空路径只能用 set 替换整个文件，且 value 应为对象",
                    reason="invalid_input",
                    index=index,
                )
            out = copy.deepcopy(dict(op["value"]))
            continue
        parent: Any = out
        for part in parts[:-1]:
            child = parent.get(part)
            if not isinstance(child, dict):
                if op["op"] == "unset":
                    raise rejected("要删除的键不存在", reason="no_such_key", path=".".join(parts))
                child = {}
                parent[part] = child
            parent = child
        if op["op"] == "set":
            parent[parts[-1]] = copy.deepcopy(op["value"])
        elif parts[-1] in parent:
            del parent[parts[-1]]
        else:
            raise rejected("要删除的键不存在", reason="no_such_key", path=".".join(parts))
    return out


def _leaves(value: Any, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    if isinstance(value, dict) and value:
        out: dict[tuple[str, ...], Any] = {}
        for key, item in value.items():
            out.update(_leaves(item, (*prefix, str(key))))
        return out
    return {prefix: value}


Key = tuple[str, ...]


def changed_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Key]:
    """Every leaf added, removed or changed, as key segments (map keys may contain dots)."""
    old, new = _leaves(dict(before)), _leaves(dict(after))
    keys = {k for k in old.keys() | new.keys() if k}
    return sorted(
        k for k in keys if k not in old or k not in new or _canon(old[k]) != _canon(new[k])
    )


def dotted(keys: Iterable[Key]) -> list[str]:
    """Keys for display; segments are joined with dots."""
    return [".".join(k) for k in keys]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _matches(parts: Key, pattern: str) -> bool:
    wanted = pattern.split(".")
    shared = min(len(parts), len(wanted))
    return all(w in ("*", p) for p, w in zip(parts[:shared], wanted[:shared], strict=True))


def permission_keys(
    before: Mapping[str, Any], after: Mapping[str, Any], changed: Iterable[Key]
) -> list[Key]:
    """The changed keys that need the user's own words, plus added or removed repositories.

    Matching works on key segments, never on joined text, so a map key containing dots
    (``repositories["my.repo"]``) cannot shift the segments and slip past a pattern.
    """
    out = {key for key in changed if any(_matches(key, p) for p in PERMISSION_KEYS)}
    old, new = _keys(before.get("repositories")), _keys(after.get("repositories"))
    out |= {("repositories", name) for name in old ^ new}
    return sorted(out)


def _keys(value: Any) -> set[str]:
    return {str(k) for k in value} if isinstance(value, dict) else set()


def source_ref(source: Any) -> tuple[str, str] | None:
    """The user message a ``VerifiedSource`` refers to: (channel, message reference)."""
    if not isinstance(source, Mapping):
        return None
    channel, ref = source.get("channel"), source.get("message_ref")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return (channel, ref) if isinstance(channel, str) and isinstance(ref, str) else None


# ---------------------------------------------------------------------- the ledger


def change_events(storage: StorageBackend) -> list[Event]:
    return [event for event in storage.read(since=0) if event.type == CHANGE_EVENT]


def find_change(storage: StorageBackend, change_id: str) -> Event:
    for event in change_events(storage):
        if event.data.get("change_id") == change_id:
            return event
    raise rejected("没有这次配置修改", reason="no_such_change", change_id=change_id)


def next_change_id(ctx: CommitContext) -> str:
    return f"C{int(get_meta(ctx.conn, COUNT) or 0) + 1:03d}"


def summary(event: Event) -> dict[str, Any]:
    data = event.data
    source = data.get("user_source")
    return {
        "change_id": data.get("change_id"),
        "sequence": event.sequence,
        "time": event.time,
        "kind": data.get("kind"),
        "reverts": data.get("reverts"),
        "changed": data.get("changed"),
        "permission": data.get("permission"),
        "reason": data.get("reason"),
        "user_words": source.get("text") if isinstance(source, Mapping) else None,
        "before_sha256": data.get("before_sha256"),
        "after_sha256": data.get("after_sha256"),
    }


def reduce_changed(conn: Any, event: Event) -> None:
    set_meta(
        conn,
        LAST_CHANGE,
        json.dumps({"sequence": event.sequence, "change_id": event.data.get("change_id")}),
    )
    set_meta(conn, COUNT, str(int(get_meta(conn, COUNT) or 0) + 1))


def apply_pending(ctx: CommitContext) -> None:
    """Write the latest recorded change into the file if an interruption left it unwritten.

    The file is written only while it still holds the change's "before" text; any other content
    is a hand edit made since, and hand edits are left alone (DN003 §7.3).
    """
    raw = get_meta(ctx.conn, LAST_CHANGE)
    if raw is None:
        return
    sequence = int(json.loads(raw)["sequence"])
    if _applied(ctx.home) >= sequence:
        return
    event = next(e for e in ctx.storage.read(since=sequence - 1) if e.sequence == sequence)
    current = text_sha(read_text(ctx.home))
    if current != event.data["after_sha256"] and current == event.data["before_sha256"]:
        write_text(ctx.home, event.data.get("after"))
    atomic_write(
        ctx.home.cache_dir / APPLIED_FILE, json.dumps({"sequence": sequence}).encode("utf-8")
    )


def _applied(home: Home) -> int:
    """The latest change already settled; 0 when unknown (for example after a restore)."""
    try:
        return int(
            json.loads((home.cache_dir / APPLIED_FILE).read_text(encoding="utf-8"))["sequence"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return 0
