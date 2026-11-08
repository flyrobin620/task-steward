#!/usr/bin/env python
"""A stand-in for the GitHub CLI in a live run (docs/development/test-environments.md).

A live run never pushes to a real remote. Its target repository has a bare ``origin.git`` in the
run directory, and this script, installed as ``<run>/bin/gh``, plays GitHub for it: pull
requests are records in ``<run>/gh-state.json`` and a merge is a squash commit on ``main`` of
the bare repository. Everything else in the run is real; what this script does is simulated
evidence and is reported as such.

Supported: ``gh --version``, ``gh pr create``, ``gh pr view``, ``gh pr list``, ``gh pr checks``
and ``gh pr merge`` (``--squash``), each with ``-R [HOST/]OWNER/NAME`` or a PR link. Every
invocation is appended to ``<run>/gh-log.jsonl``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RUN = Path(os.environ.get("TSW_LIVE_RUN") or Path(__file__).resolve().parent.parent)
STATE = RUN / "gh-state.json"
LOG = RUN / "gh-log.jsonl"
ORIGIN = RUN / "origin.git"
VALUE_OPTIONS = {
    "-R",
    "--repo",
    "-B",
    "--base",
    "-H",
    "--head",
    "-t",
    "--title",
    "-b",
    "--body",
    "-F",
    "--body-file",
    "--json",
    "-q",
    "--jq",
    "--subject",
    "--match-head-commit",
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"gh (live stub): git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def load() -> dict[str, Any]:
    if STATE.is_file():
        data: dict[str, Any] = json.loads(STATE.read_text(encoding="utf-8"))
        return data
    return {"repository": None, "pulls": []}


def save(state: dict[str, Any]) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE)


def parse(args: list[str]) -> tuple[list[str], dict[str, str], set[str]]:
    words: list[str] = []
    values: dict[str, str] = {}
    flags: set[str] = set()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and "=" in arg:
            key, value = arg.split("=", 1)
            values[key] = value
        elif arg in VALUE_OPTIONS and i + 1 < len(args):
            values[arg] = args[i + 1]
            i += 1
        elif arg.startswith("-"):
            flags.add(arg)
        else:
            words.append(arg)
        i += 1
    return words, values, flags


def target(values: dict[str, str], words: list[str], state: dict[str, Any]) -> tuple[str, int]:
    """The repository (owner/name) and PR number a command names."""
    repo = values.get("-R") or values.get("--repo")
    number = 0
    for word in words:
        if "/pull/" in word:
            path = word.split("://", 1)[-1]
            parts = path.split("/")
            repo, number = f"{parts[1]}/{parts[2]}", int(parts[4])
        elif word.isdigit():
            number = int(word)
    if repo is None:
        raise SystemExit("gh (live stub): name the repository with -R or a PR link")
    repo = "/".join(repo.split("/")[-2:])
    expected = state.get("repository")
    if expected and repo.lower() != str(expected).lower():
        raise SystemExit(f"gh (live stub): unknown repository {repo}")
    return repo, number


def pull(state: dict[str, Any], number: int) -> dict[str, Any]:
    for pr in state["pulls"]:
        if pr["number"] == number:
            found: dict[str, Any] = pr
            return found
    raise SystemExit(f"gh (live stub): no pull request #{number}")


def url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def create(values: dict[str, str], words: list[str], state: dict[str, Any]) -> int:
    repo, _ = target(values, words, state)
    head = values.get("-H") or values.get("--head")
    if not head:
        raise SystemExit("gh (live stub): pr create needs --head <branch> (push the branch first)")
    base = values.get("-B") or values.get("--base") or "main"
    sha = git("rev-parse", f"refs/heads/{head}", cwd=ORIGIN)
    number = len(state["pulls"]) + 1
    state["pulls"].append(
        {
            "number": number,
            "title": values.get("-t") or values.get("--title") or "",
            "base": base,
            "head": head,
            "head_sha": sha,
            "state": "OPEN",
            "created_at": now(),
            "merge_commit": None,
            "merged_head": None,
        }
    )
    save(state)
    print(url(repo, number))
    return 0


def refresh(pr: dict[str, Any]) -> None:
    if pr["state"] == "OPEN":
        pr["head_sha"] = git("rev-parse", f"refs/heads/{pr['head']}", cwd=ORIGIN)


def view(values: dict[str, str], words: list[str], state: dict[str, Any]) -> int:
    repo, number = target(values, words, state)
    pr = pull(state, number)
    refresh(pr)
    save(state)
    out = {
        "number": number,
        "url": url(repo, number),
        "state": pr["state"],
        "title": pr["title"],
        "baseRefName": pr["base"],
        "headRefName": pr["head"],
        "headRefOid": pr["merged_head"] or pr["head_sha"],
        "mergeCommit": {"oid": pr["merge_commit"]} if pr["merge_commit"] else None,
        "mergedAt": pr.get("merged_at"),
    }
    print(json.dumps(out, indent=2))
    return 0


def checks(values: dict[str, str], words: list[str], state: dict[str, Any]) -> int:
    _, number = target(values, words, state)
    pull(state, number)
    print("Gate\tpass\t0s\t(live stub: no CI in a live run)")
    return 0


def merge(values: dict[str, str], words: list[str], flags: set[str], state: dict[str, Any]) -> int:
    repo, number = target(values, words, state)
    pr = pull(state, number)
    if pr["state"] != "OPEN":
        raise SystemExit(f"gh (live stub): pull request #{number} is {pr['state']}")
    if "--merge" in flags or "--rebase" in flags:
        raise SystemExit("gh (live stub): only --squash merges are supported")
    refresh(pr)
    expected = values.get("--match-head-commit")
    if expected and not pr["head_sha"].startswith(expected):
        raise SystemExit("gh (live stub): head commit does not match --match-head-commit")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        git("clone", "-q", "--branch", pr["base"], str(ORIGIN), str(work))
        git("fetch", "-q", "origin", pr["head"], cwd=work)
        git("merge", "-q", "--squash", "FETCH_HEAD", cwd=work)
        git(
            "-c",
            "user.name=live-stub",
            "-c",
            "user.email=live-stub@example.com",
            "commit",
            "-q",
            "-m",
            f"{pr['title']} (#{number})",
            cwd=work,
        )
        commit = git("rev-parse", "HEAD", cwd=work)
        git("push", "-q", "origin", f"HEAD:{pr['base']}", cwd=work)
    pr.update(state="MERGED", merge_commit=commit, merged_head=pr["head_sha"], merged_at=now())
    save(state)
    print(f"✓ Squashed and merged pull request {repo}#{number} ({commit})")
    return 0


@contextmanager
def locked(timeout: float = 120.0) -> Generator[None, None, None]:
    """One stand-in invocation at a time per run: two workers share ``gh-state.json`` and the
    bare repository, and each command reads, changes and writes them as a whole."""
    lock = RUN / "gh-state.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                message = f"gh (live stub): {lock} is held; remove it if no gh is running"
                raise SystemExit(message) from None
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(fd)
        lock.unlink()


def main(argv: list[str]) -> int:
    with locked():
        return run_command(argv)


def run_command(argv: list[str]) -> int:
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps({"time": now(), "args": argv}, ensure_ascii=False) + "\n")
    if argv[:1] == ["--version"]:
        print("gh version 0.0.0 (live stub: simulated GitHub for a live run)")
        return 0
    words, values, flags = parse(argv)
    state = load()
    if words[:1] != ["pr"] or len(words) < 2:
        raise SystemExit(f"gh (live stub): unsupported command: gh {' '.join(argv)}")
    command, rest = words[1], words[2:]
    if command == "create":
        return create(values, rest, state)
    if command == "view":
        return view(values, rest, state)
    if command == "checks":
        return checks(values, rest, state)
    if command == "merge":
        return merge(values, rest, flags, state)
    if command == "list":
        print(json.dumps(state["pulls"], indent=2))
        return 0
    raise SystemExit(f"gh (live stub): unsupported command: gh pr {command}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
