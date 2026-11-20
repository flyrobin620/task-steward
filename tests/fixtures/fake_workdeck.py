"""A stand-in for workdeck's plugin commands (``workdeck plugin …``, workdeck DN005 §11.4):
workdeck is outside this repository.

It keeps the ``plugins`` of a ``workdeck.json`` at ``FAKE_WORKDECK_CONFIG`` the way workdeck does
(the manifest's entry without ``workdeckPlugin``; an identical entry is ``unchanged``, a different
one under the same id needs ``--replace``) and appends every call's arguments to
``FAKE_WORKDECK_LOG``. ``FAKE_WORKDECK_FAIL=<verb>`` makes that verb fail (exit 1) before it
changes anything; ``FAKE_WORKDECK_FAIL=restart`` makes ``add`` fail after writing the
configuration, as workdeck does when its daemon does not come back. ``FAKE_WORKDECK_RUNNING=1``:
``list`` reports the daemon running every plugin (``running: <id> ready``).

    plugin add <manifest file> [--replace]
    plugin list            <id> <name> <kind> <upstream>, tab-separated (describeUpstream)
    plugin remove <id>
"""

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_WORKDECK_LOG"]).open("a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
config = Path(os.environ["FAKE_WORKDECK_CONFIG"])
data = json.loads(config.read_text(encoding="utf-8")) if config.is_file() else {}
plugins = data.setdefault("plugins", [])
verb = args[1] if len(args) > 1 else ""
if args[:1] != ["plugin"] or verb not in ("add", "list", "remove"):
    print(f"unknown command {' '.join(args)}", file=sys.stderr)
    sys.exit(2)
if os.environ.get("FAKE_WORKDECK_FAIL") == verb:
    print(f"workdeck plugin {verb}: failed (stand-in)", file=sys.stderr)
    sys.exit(1)


def save() -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2), encoding="utf-8")


if verb == "list":
    if not plugins:
        print("no plugins (add one with workdeck plugin add)")
    for p in plugins:
        spawn = p.get("spawn") or {}
        started = " ".join([spawn.get("command", ""), *spawn.get("args", [])])
        where = f" (in {spawn['cwd']})" if spawn.get("cwd") else ""
        print(f"{p['id']}\t{p.get('name', p['id'])}\tspawn\tstarts: {started}{where}")
    if os.environ.get("FAKE_WORKDECK_RUNNING"):
        for p in plugins:
            print(f"running: {p['id']} ready")
    else:
        print("the daemon is not running (stand-in)")
elif verb == "add":
    raw = Path(args[2]).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        print("manifest is not JSON (BOM)", file=sys.stderr)
        sys.exit(1)
    manifest = json.loads(raw.decode("utf-8"))
    if manifest.pop("workdeckPlugin", None) != 1:
        print("not a workdeck plugin manifest", file=sys.stderr)
        sys.exit(1)
    at = next((i for i, p in enumerate(plugins) if p["id"] == manifest["id"]), None)
    if at is not None and plugins[at] == manifest:
        print(f"[config] unchanged: {config}")
        sys.exit(0)
    if at is not None and "--replace" not in args:
        print(f'a different plugin "{manifest["id"]}" is configured already', file=sys.stderr)
        sys.exit(1)
    if at is None:
        plugins.append(manifest)
    else:
        plugins[at] = manifest
    save()
    print(f"[config] {'added' if at is None else 'replaced'}: {config}")
    if os.environ.get("FAKE_WORKDECK_FAIL") == "restart":
        print("[restart] FAILED: the daemon did not come back (stand-in)", file=sys.stderr)
        sys.exit(1)
else:
    before = len(plugins)
    data["plugins"] = [p for p in plugins if p["id"] != args[2]]
    if len(data["plugins"]) == before:
        print(f'no plugin "{args[2]}"')
        sys.exit(1)
    save()
    print(f"[config] removed {args[2]}: {config}")
