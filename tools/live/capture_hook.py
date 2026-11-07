"""Record the payload of a Claude Code hook in a live run (``<run>/hook-payloads.jsonl``).

``prepare`` installs it next to the plugin's own hooks, through the project settings of the
run's folders, so the payloads the real app sends can be checked after the run and kept as
recorded samples for the surface tests (docs/development/test-environments.md).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    run = Path(os.environ.get("TSW_LIVE_RUN") or Path(__file__).resolve().parent.parent)
    raw = sys.stdin.read()
    try:
        payload: object = json.loads(raw)
    except ValueError:
        payload = raw
    record = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": sys.argv[1] if len(sys.argv) > 1 else None,
        "session": os.environ.get("CLAUDE_CODE_HOST_SESSION_ID"),
        "payload": payload,
    }
    with (run / "hook-payloads.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
    return 0  # a recorder never changes what the session does


if __name__ == "__main__":
    sys.exit(main())
