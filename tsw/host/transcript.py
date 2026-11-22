"""Where a message came from, as Claude Code wrote it in the session's transcript.

A hook's payload carries ``transcript_path`` and ``prompt_id``. The message that starts a turn is
a ``user`` entry with the same ``promptId`` and the host's ``origin``; a message injected while
the turn runs is a ``queued_command`` attachment with its ``origin`` and text (``prompt``) but no
``promptId`` (T334 probe C1). The entries are written only after ``UserPromptSubmit`` has run, so
they are read at ``Stop`` (CC001 §6.1, T338). Reading is best effort: any failure means "not
known", and callers keep their old behavior.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

WORKDECK = "workdeck"
# A turn's own entries are near the end; a turn with a lot of tool output pushes them back.
# Reading stops here rather than walk a long session's whole transcript at every turn's end.
TAIL_LIMIT = 8 * 1024 * 1024
CHUNK = 256 * 1024
QUEUED = b'"queued_command"'


def prompt_origin(path: Any, prompt_id: Any, *, limit: int = TAIL_LIMIT) -> dict[str, Any] | None:
    """The ``origin`` of the ``user`` entry that started the turn ``prompt_id``; None when there
    is no such entry within the last ``limit`` bytes or the transcript cannot be read."""
    if not isinstance(prompt_id, str) or not prompt_id:
        return None
    needle = json.dumps(prompt_id).encode("utf-8")

    def match(line: bytes) -> dict[str, Any] | None:
        entry = _entry(line) if needle in line else None
        if entry is None or entry.get("type") != "user" or entry.get("promptId") != prompt_id:
            return None
        return _dict(entry.get("origin"))

    return _search(path, match, limit)


def message_origin(
    path: Any, prompt_id: Any, text: Any, *, limit: int = TAIL_LIMIT
) -> dict[str, Any] | None:
    """The ``origin`` of a message the hook recorded with ``text`` in the turn ``prompt_id``: a
    ``queued_command`` attachment holding the text (injected while the turn ran), or the ``user``
    entry that started the turn if its content holds the text; the latest one found."""
    if not isinstance(text, str) or not text.strip():
        return None
    words = text.strip()
    needle = json.dumps(prompt_id).encode("utf-8") if isinstance(prompt_id, str) else None

    def match(line: bytes) -> dict[str, Any] | None:
        if QUEUED not in line and (needle is None or needle not in line):
            return None
        entry = _entry(line)
        if entry is None:
            return None
        attachment = _dict(entry.get("attachment"))
        if entry.get("type") == "attachment" and attachment is not None:
            queued = attachment.get("type") == "queued_command"
            if queued and str(attachment.get("prompt") or "").strip() == words:
                return _dict(attachment.get("origin"))
            return None
        if entry.get("type") != "user" or needle is None or entry.get("promptId") != prompt_id:
            return None
        message = _dict(entry.get("message")) or {}
        return _dict(entry.get("origin")) if words in _text(message.get("content")) else None

    return _search(path, match, limit)


def via(origin: dict[str, Any] | None) -> str | None:
    """``workdeck`` when the host recorded the message as delivered by the workdeck daemon
    (``origin.kind: peer``, ``origin.from: workdeck``; CC001 §6.1); None otherwise."""
    if origin is None:
        return None
    if origin.get("kind") == "peer" and origin.get("from") == WORKDECK:
        return WORKDECK
    return None


def _search(
    path: Any, match: Callable[[bytes], dict[str, Any] | None], limit: int
) -> dict[str, Any] | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        for line in _lines_backwards(Path(path), limit):
            found = match(line)
            if found is not None:
                return found
    except OSError:
        return None
    return None


def _lines_backwards(path: Path, limit: int) -> Iterator[bytes]:
    """The transcript's lines, last first, within its last ``limit`` bytes; a line cut by the
    limit is never given."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        pos = end
        carry = b""  # a line whose beginning is in the chunk before this one
        while pos > 0 and end - pos < limit:
            size = min(CHUNK, pos, limit - (end - pos))
            pos -= size
            fh.seek(pos)
            lines = (fh.read(size) + carry).split(b"\n")
            # The first piece may be cut, unless the chunk starts the file: keep it for the next.
            carry = lines.pop(0) if pos > 0 else b""
            yield from reversed(lines)


def _entry(line: bytes) -> dict[str, Any] | None:
    try:
        parsed: Any = json.loads(line)
    except ValueError:
        return None
    return _dict(parsed)


def _dict(value: Any) -> dict[str, Any] | None:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else None


def _text(content: Any) -> str:
    """A message's text: a string, or the text blocks of a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = [_dict(b) for b in cast("list[Any]", content)]
        return "\n".join(str(b.get("text") or "") for b in blocks if b and b.get("type") == "text")
    return ""
