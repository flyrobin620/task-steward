"""A stand-in for ``copilot -p --output-format json``, for tests of the ``copilot-cli`` reviewer.

Prints the JSON Lines events the real CLI prints (shape taken from the recorded sample
``tests/fixtures/copilot/final-answer.jsonl``). Each run takes the next entry of the JSON list in
``FAKE_COPILOT_ANSWERS``: ``"echo-pass"`` reads the request file named in the prompt and passes
the head it asks for; an object may set ``content``, ``model`` (the model the answer claims),
``exit`` and ``error`` (a ``session.error`` message). Every command line is appended to
``FAKE_COPILOT_LOG`` as one JSON line, with the working directory.
"""

import json
import os
import re
import sys
from pathlib import Path


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")


def option(argv: list, name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]  # the CLI writes UTF-8
    argv = sys.argv[1:]
    with Path(os.environ["FAKE_COPILOT_LOG"]).open("a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": str(Path.cwd())}, ensure_ascii=False) + "\n")
    path = Path(os.environ["FAKE_COPILOT_ANSWERS"])
    answers = json.loads(path.read_text(encoding="utf-8"))
    answer = answers.pop(0) if answers else "echo-pass"
    path.write_text(json.dumps(answers), encoding="utf-8")
    if answer == "echo-pass":
        answer = {}
    prompt = option(argv, "-p") or ""
    session = option(argv, "--resume") or option(argv, "--session-id")
    model = answer.get("model") or option(argv, "--model")
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
            "type": "session.resume" if "--resume" in argv else "session.start",
            "data": {"sessionId": session},
        }
    )
    emit({"type": "session.model_change", "data": {"source": "startup", "newModel": model}})
    if answer.get("error"):
        emit({"type": "session.error", "data": {"errorType": "query", "message": answer["error"]}})
    else:
        emit(
            {
                "type": "assistant.message",
                "data": {"model": model, "content": "", "toolRequests": [{"name": "view"}]},
            }
        )
        emit(
            {
                "type": "assistant.message",
                "data": {
                    "model": model,
                    "content": content,
                    "toolRequests": [],
                    "phase": "final_answer",
                },
            }
        )
    code = int(answer.get("exit", 1 if answer.get("error") else 0))
    emit({"type": "result", "sessionId": session, "exitCode": code})
    return code


if __name__ == "__main__":
    sys.exit(main())
