"""A stand-in for ``claude -p --output-format stream-json``, for tests of the ``claude-headless``
reviewer.

Prints the stream-json events the real CLI prints (shape taken from the recorded sample
``tests/fixtures/claude/final-answer.jsonl``). Each run takes the next entry of the JSON list in
``FAKE_REVIEWER_ANSWERS``: ``"echo-pass"`` reads the request file named in the prompt and passes
the head it asks for; an object may set ``content``, ``model`` (the model the answer claims),
``init_model``, ``session`` (the session ID the result reports), ``is_error``, ``exit`` and
``sleep`` (seconds to hang after the first event). Every
command line is appended to ``FAKE_REVIEWER_LOG`` as one JSON line, with the working directory
and the names of the inherited variables that start with ``CLAUDE``, ``ANTHROPIC`` or ``TSW``.
"""

import json
import os
import re
import sys
import time
from pathlib import Path


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")


def option(argv: list, name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]  # the CLI writes UTF-8
    argv = sys.argv[1:]
    inherited = sorted(
        k for k in os.environ if k.upper().startswith(("CLAUDE", "ANTHROPIC", "TSW"))
    )
    with Path(os.environ["FAKE_REVIEWER_LOG"]).open("a", encoding="utf-8") as log:
        entry = {"argv": argv, "cwd": str(Path.cwd()), "inherited": inherited}
        log.write(json.dumps(entry, ensure_ascii=False) + "\n")
    path = Path(os.environ["FAKE_REVIEWER_ANSWERS"])
    answers = json.loads(path.read_text(encoding="utf-8"))
    answer = answers.pop(0) if answers else "echo-pass"
    path.write_text(json.dumps(answers), encoding="utf-8")
    if answer == "echo-pass":
        answer = {}
    prompt = option(argv, "-p") or ""
    session = option(argv, "--resume") or option(argv, "--session-id")
    requested = option(argv, "--model") or "?"
    # The host reports an alias as the model it stands for, with the context suffix.
    full = (
        f"claude-{requested}-5-5"
        if requested in ("sonnet", "opus", "haiku", "fable")
        else requested
    )
    init_model = answer.get("init_model") or f"{full}[1m]"
    model = answer.get("model") or full.replace("-5-5", "-5.5")
    content = answer.get("content")
    if content is None:
        found = re.search(r"([A-Za-z]:\\[^ ，（]+?-request\.md|/[^ ，（]+?-request\.md)", prompt)
        request = Path(found.group(1)).read_text(encoding="utf-8") if found else ""
        head = re.search(r'"reviewed_head":"([^"]+)"', request)
        content = json.dumps(
            {
                "verdict": "pass",
                "summary": "no findings",
                "reviewed_head": head.group(1) if head else "?",
                "findings": [],
            }
        )
    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session,
            "tools": (option(argv, "--tools") or "").split(","),
            "mcp_servers": [],
            "model": init_model,
            "permissionMode": option(argv, "--permission-mode"),
        }
    )
    if answer.get("sleep"):  # print the start, then hang: the run times out
        sys.stdout.flush()
        time.sleep(float(answer["sleep"]))
    is_error = bool(answer.get("is_error"))
    if not is_error:
        emit(
            {
                "type": "assistant",
                "message": {"model": model, "content": [{"type": "tool_use", "name": "Read"}]},
                "session_id": session,
            }
        )
    emit(
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>" if is_error else model,
                "content": [{"type": "text", "text": content}],
            },
            "session_id": session,
        }
    )
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": is_error,
            "session_id": answer.get("session") or session,
            "result": content,
            "modelUsage": {} if is_error else {init_model: {}},
        }
    )
    return int(answer.get("exit", 1 if is_error else 0))


if __name__ == "__main__":
    sys.exit(main())
