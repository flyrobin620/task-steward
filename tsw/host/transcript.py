"""Where the message that started a turn came from, as Claude Code wrote it in the transcript.

A hook's payload carries ``transcript_path`` and ``prompt_id``; the transcript's ``user`` entry
that started the turn carries the same ``promptId`` and the host's ``origin``. The entry is
written only after ``UserPromptSubmit`` has run, so it is read at ``Stop`` (CC001 §6.1, T338).
Reading is best effort: any failure means "not known", and callers keep their old behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

WORKDECK = "workdeck"
# A turn's own entry is near the end; a turn with a lot of tool output pushes it back. Reading
# stops here rather than walk a long session's whole transcript at every turn's end.
TAIL_LIMIT = 8 * 1024 * 1024
CHUNK = 256 * 1024


def prompt_origin(path: Any, prompt_id: Any, *, limit: int = TAIL_LIMIT) -> dict[str, Any] | None:
    """The ``origin`` of the ``user`` entry with this ``promptId``, searched from the end of the
    transcript; None when there is no such entry within ``limit`` bytes or it cannot be read."""
    if not isinstance(path, str) or not path or not isinstance(prompt_id, str) or not prompt_id:
        return None
    needle = json.dumps(prompt_id).encode("utf-8")
    try:
        with Path(path).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            pos = end
            carry = b""  # a line whose beginning is in the chunk before this one
            while pos > 0 and end - pos < limit:
                size = min(CHUNK, pos, limit - (end - pos))
                pos -= size
                fh.seek(pos)
                lines = (fh.read(size) + carry).split(b"\n")
                # The first piece may be cut, unless the chunk starts the file: keep it for the
                # next chunk. A line cut by the limit is never read.
                carry = lines.pop(0) if pos > 0 else b""
                for line in reversed(lines):
                    found = _origin_of(line, prompt_id, needle)
                    if found is not None:
                        return found
    except OSError:
        return None
    return None


def _origin_of(line: bytes, prompt_id: str, needle: bytes) -> dict[str, Any] | None:
    if needle not in line:
        return None
    try:
        parsed: Any = json.loads(line)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    entry = cast("dict[str, Any]", parsed)
    if entry.get("type") != "user" or entry.get("promptId") != prompt_id:
        return None
    origin = entry.get("origin")
    return cast("dict[str, Any]", origin) if isinstance(origin, dict) else None


def via(origin: dict[str, Any] | None) -> str | None:
    """``workdeck`` when the host recorded the message as delivered by the workdeck daemon
    (``origin.kind: peer``, ``origin.from: workdeck``; CC001 §6.1); None otherwise."""
    if origin is None:
        return None
    if origin.get("kind") == "peer" and origin.get("from") == WORKDECK:
        return WORKDECK
    return None
