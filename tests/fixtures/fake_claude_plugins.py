"""A stand-in for Claude Code's plugin commands (``claude plugin …``): Claude Code is outside this
repository.

It keeps Claude Code's own records under ``$CLAUDE_CONFIG_DIR/plugins`` the way T305 observed them
(``known_marketplaces.json``, ``installed_plugins.json`` with ``installPath``, ``version`` and
``gitCommitSha``, a cached copy of the marketplace's ``plugin/``) and appends every call's
arguments to ``FAKE_CLAUDE_LOG``. ``FAKE_CLAUDE_FAIL=<verb>`` makes that verb fail (exit 1).

    plugin marketplace add <dir>       a directory marketplace; re-points one of the same name
    plugin marketplace remove <name>
    plugin install <name> --marketplace <dir>    a "git" marketplace from <dir>, then install
    plugin install task-steward@task-steward [--scope user]
    plugin update task-steward@task-steward
    plugin uninstall task-steward@task-steward
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ID = "task-steward@task-steward"
args = sys.argv[1:]
with Path(os.environ["FAKE_CLAUDE_LOG"]).open("a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
root = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins"
root.mkdir(parents=True, exist_ok=True)
known_file = root / "known_marketplaces.json"
installed_file = root / "installed_plugins.json"


def load(path: Path, default: dict) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def head(repo: str) -> str:
    out = subprocess.run(
        ["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def source_dir(known: dict) -> str:
    source = known["task-steward"]["source"]
    return source["path"] if source["source"] == "directory" else source["url"]


def install(known: dict) -> None:
    repo = source_dir(known)
    sha = head(repo)
    copy = root / "cache" / "task-steward" / "task-steward" / sha[:12]
    if not copy.exists():
        shutil.copytree(Path(repo) / "plugin", copy)
    records = load(installed_file, {"version": 2, "plugins": {}})
    others = [r for r in records["plugins"].get(PLUGIN_ID, []) if r.get("scope") != "user"]
    records["plugins"][PLUGIN_ID] = [
        *others,
        {"scope": "user", "installPath": str(copy), "version": sha[:12], "gitCommitSha": sha},
    ]
    save(installed_file, records)


verb = " ".join(args[:3]) if args[:2] == ["plugin", "marketplace"] else " ".join(args[:2])
if os.environ.get("FAKE_CLAUDE_FAIL") and os.environ["FAKE_CLAUDE_FAIL"] in verb:
    print(f"error: {verb} failed")
    sys.exit(1)
known = load(known_file, {})
if verb == "plugin marketplace add":
    path = args[3]
    before = known.get("task-steward")
    known["task-steward"] = {
        "source": {"source": "directory", "path": path},
        "installLocation": path,
    }
    save(known_file, known)
    print("Marketplace 'task-steward' now points at dir:" + path if before else "added")
elif verb == "plugin marketplace remove":
    # As Claude Code does: removing a marketplace also uninstalls its (user-scope) plugin.
    known.pop(args[3], None)
    save(known_file, known)
    records = load(installed_file, {"version": 2, "plugins": {}})
    left = [r for r in records["plugins"].get(PLUGIN_ID, []) if r.get("scope") != "user"]
    if left:
        records["plugins"][PLUGIN_ID] = left
    else:
        records["plugins"].pop(PLUGIN_ID, None)
    save(installed_file, records)
elif verb == "plugin install" and "--marketplace" in args:
    url = args[args.index("--marketplace") + 1]
    known["task-steward"] = {"source": {"source": "git", "url": url}, "installLocation": url}
    save(known_file, known)
    install(known)
elif verb in ("plugin install", "plugin update"):
    install(known)
elif verb == "plugin uninstall":
    records = load(installed_file, {"version": 2, "plugins": {}})
    left = [r for r in records["plugins"].get(PLUGIN_ID, []) if r.get("scope") != "user"]
    if left:
        records["plugins"][PLUGIN_ID] = left
    else:
        records["plugins"].pop(PLUGIN_ID, None)
    save(installed_file, records)
else:
    print(f"unknown: {args}")
    sys.exit(2)
