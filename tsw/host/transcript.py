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
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

from tsw.machine.ask_user import user_words

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


def turn_origins(
    path: Any, prompt_id: Any, prompts: Sequence[str], *, limit: int = TAIL_LIMIT
) -> list[dict[str, Any] | None]:
    """The ``origin`` of each prompt the hook saw in the turn ``prompt_id``, in the order seen
    (``user_words`` of each; None where not known).

    Only the turn's own entries count: the ``user`` entry that started it (``promptId``, with an
    origin) and the ``queued_command`` attachments after it. The first prompt is the one that
    started the turn when that entry holds its words; each later one takes the first attachment
    not yet taken whose words are the same, so equal words sent twice are told apart by order.
    Without the turn's start entry (not written, or beyond ``limit``) nothing is known.
    """
    unknown: list[dict[str, Any] | None] = [None] * len(prompts)
    if not prompts or not isinstance(prompt_id, str) or not prompt_id:
        return unknown
    needle = json.dumps(prompt_id).encode("utf-8")
    start: dict[str, Any] | None = None
    queued: list[tuple[str, dict[str, Any] | None]] = []  # the turn's attachments, last first
    if not isinstance(path, str) or not path:
        return unknown
    try:
        for line in _lines_backwards(Path(path), limit):
            if QUEUED not in line and needle not in line:
                continue
            entry = _entry(line)
            if entry is None:
                continue
            attachment = _dict(entry.get("attachment"))
            if entry.get("type") == "attachment" and attachment is not None:
                if attachment.get("type") == "queued_command":
                    words = user_words(str(attachment.get("prompt") or ""))
                    queued.append((words, _dict(attachment.get("origin"))))
                continue
            origin = _dict(entry.get("origin"))
            if entry.get("type") == "user" and entry.get("promptId") == prompt_id and origin:
                start = entry
                break
    except OSError:
        return unknown
    if start is None:
        return unknown
    out = list(unknown)
    message = _dict(start.get("message")) or {}
    first = user_words(prompts[0])
    if first and first in _text(message.get("content")):
        out[0] = _dict(start.get("origin"))
    taken = [False] * len(queued)
    order = list(range(len(queued) - 1, -1, -1))  # oldest first
    for n, prompt in enumerate(prompts[1:], start=1):
        words = user_words(prompt)
        for i in order:
            if not taken[i] and words and queued[i][0] == words:
                taken[i] = True
                out[n] = queued[i][1]
                break
    return out


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
