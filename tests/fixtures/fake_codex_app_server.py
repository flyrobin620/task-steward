"""A stand-in for ``codex app-server`` over stdio, for surface tests of the reviewer adapter.

Answers the requests ``tsw.review.codex`` sends. The review answer comes from the file named by
``FAKE_CODEX_ANSWERS`` (a JSON list, one entry per turn; ``"echo-pass"`` passes the head named
in the prompt); every request is appended to ``FAKE_CODEX_LOG`` as one JSON line.
"""

import json
import os
import re
import sys
from pathlib import Path


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def next_answer(prompt: str) -> str:
    path = Path(os.environ["FAKE_CODEX_ANSWERS"])
    answers = json.loads(path.read_text(encoding="utf-8"))
    answer = answers.pop(0) if answers else "echo-pass"
    path.write_text(json.dumps(answers), encoding="utf-8")
    if answer == "echo-pass":
        head = re.search(r'"reviewed_head":"([^"]+)"', prompt)
        return json.dumps(
            {
                "verdict": "pass",
                "summary": "no findings",
                "reviewed_head": head.group(1) if head else "?",
                "findings": [],
            }
        )
    return answer if isinstance(answer, str) else json.dumps(answer)


def main() -> None:
    log = Path(os.environ["FAKE_CODEX_LOG"])
    turns = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        with log.open("a", encoding="utf-8") as out:
            out.write(json.dumps(message) + "\n")
        if "id" not in message:
            continue
        method, params, mid = message["method"], message.get("params") or {}, message["id"]
        if method in ("thread/start", "thread/resume"):
            thread = params.get("threadId", "thread-1")
            send({"id": mid, "result": {"thread": {"id": thread}, "model": params["model"]}})
        elif method == "turn/start":
            turns += 1
            turn = f"turn-{turns}"
            send({"id": mid, "result": {"turn": {"id": turn}}})
            text = next_answer(params["input"][0]["text"])
            thread = params["threadId"]
            item = {"type": "agentMessage", "phase": "final", "text": text}
            send(
                {
                    "method": "item/completed",
                    "params": {"threadId": thread, "turnId": turn, "item": item},
                }
            )
            done = {"id": turn, "status": "completed"}
            send({"method": "turn/completed", "params": {"threadId": thread, "turn": done}})
        else:  # initialize, thread/unarchive, thread/archive, turn/interrupt
            send({"id": mid, "result": {}})


if __name__ == "__main__":
    main()
