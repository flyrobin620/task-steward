"""A stand-in for the three reviewer command lines' model lists (DN004 §3.4), for tests of
``tsw.review.models``: the hosts are outside this repository.

The mode comes from the command line, as tsw starts each host:

    app-server                                  ``codex app-server``: ``initialize``, ``model/list``
    --acp ...                                   ``copilot --acp``: ``initialize``, ``session/new``,
                                                ``session/close``
    -p --input-format stream-json ...           ``claude -p``: the ``initialize`` control request

The answers are the recorded samples in ``tests/fixtures/models/``. ``FAKE_MODELS_MODE`` changes
the behaviour: ``hang`` (never answers), ``exit`` (exits before answering), ``error`` (the list
request fails), ``empty`` (an empty list). Every call is appended to ``FAKE_MODELS_LOG`` (when
set) as one JSON line: the arguments, the working directory, the requests received and the names
of the inherited variables that start with ``CLAUDE``, ``ANTHROPIC`` or ``TSW``.
"""

import json
import os
import sys
import time
from pathlib import Path

SAMPLES = Path(__file__).resolve().parent / "models"
MODE = os.environ.get("FAKE_MODELS_MODE", "")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def sample(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


def log(entry: dict) -> None:
    target = os.environ.get("FAKE_MODELS_LOG")
    if target:
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")


def requests():
    for line in sys.stdin:
        if line.strip():
            yield json.loads(line)


def codex(entry: dict) -> None:
    for message in requests():
        entry["requests"].append(message.get("method"))
        if "id" not in message:
            continue
        mid, method = message["id"], message["method"]
        if method == "model/list":
            if MODE == "hang":
                time.sleep(3600)
            if MODE == "exit":
                return
            if MODE == "error":
                send({"id": mid, "error": {"code": -32000, "message": "not logged in"}})
                continue
            listed = sample("codex-model-list.json")
            data = [] if MODE == "empty" else listed["data"]
            # Two pages, so the client follows nextCursor.
            cursor = (message.get("params") or {}).get("cursor")
            half = len(data) // 2
            page = data[half:] if cursor else data[:half]
            send({"id": mid, "result": {"data": page, "nextCursor": None if cursor else "p2"}})
        else:
            send({"id": mid, "result": {}})


def copilot(entry: dict) -> None:
    for message in requests():
        entry["requests"].append(message.get("method"))
        if "id" not in message:
            continue
        mid, method = message["id"], message["method"]
        if method == "session/new":
            entry["session_cwd"] = (message.get("params") or {}).get("cwd")
            if MODE == "hang":
                time.sleep(3600)
            if MODE == "exit":
                return
            if MODE == "error":
                send({"id": mid, "error": {"code": -32000, "message": "Authentication required"}})
                continue
            result = sample("copilot-session-new.json")["result"]
            if MODE == "empty":
                result["models"]["availableModels"] = []
            send({"id": mid, "result": result})
        elif method == "initialize":
            send({"id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {}}})
        else:
            send({"id": mid, "result": {}})


def claude(entry: dict) -> None:
    for message in requests():
        entry["requests"].append(message.get("type"))
        if message.get("type") != "control_request":
            continue
        if MODE == "hang":
            time.sleep(3600)
        if MODE == "exit":
            return
        rid = message["request_id"]
        if MODE == "error":
            response = {"subtype": "error", "request_id": rid, "error": "not logged in"}
        else:
            recorded = sample("claude-initialize.json")["response"]
            if MODE == "empty":
                recorded["response"]["models"] = []
            response = {**recorded, "request_id": rid}
        send({"type": "control_response", "response": response})


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    argv = sys.argv[1:]
    inherited = sorted(
        k for k in os.environ if k.upper().startswith(("CLAUDE", "ANTHROPIC", "TSW"))
    )
    entry = {"argv": argv, "cwd": str(Path.cwd()), "inherited": inherited, "requests": []}
    try:
        if argv[:1] == ["app-server"]:
            codex(entry)
        elif "--acp" in argv:
            copilot(entry)
        elif "--input-format" in argv:
            claude(entry)
        else:
            print(f"unknown: {argv}", file=sys.stderr)
            return 2
    finally:
        log(entry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
