"""CI's scope: which stages a change needs (docs/development/ci.md).

    python tools/scope.py --base <rev> [--head <rev>] [--all <reason>] [--out <plan.json>]

The changed paths are ``git diff --name-only --no-renames <base> <head>``, so a rename counts its
old and its new path. Every path must match a component of ``tools/scope.json``; the stages of
every matched component are selected, together with the map's ``always`` stages. A path that
matches no component fails the scope (exit code 1), and so does a map that is not valid. ``--all``
selects every stage (pushes to ``main`` and the label ``ci:all``). The plan is printed, written to
``--out`` and, under GitHub Actions, given to the workflow as the step output ``stages`` (a JSON
array).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = "tools/scope.json"
RESOLVER_PATH = "tools/scope.py"
WORKFLOW_PATH = ".github/workflows/ci.yml"


def glob_regex(glob: str) -> re.Pattern[str]:
    """A path glob as a regular expression: ``**`` any number of folders, ``*`` and ``?`` within
    one name."""
    out = ""
    index = 0
    while index < len(glob):
        char = glob[index]
        if glob.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
            continue
        if glob.startswith("**", index):
            out += ".*"
            index += 2
            continue
        out += "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
        index += 1
    return re.compile(f"^{out}$")


def load_map(root: Path = ROOT) -> dict[str, Any]:
    return json.loads((root / MAP_PATH).read_text(encoding="utf-8"))


def validate_map(scope_map: Mapping[str, Any]) -> list[str]:
    """What is wrong with a component map: its stages, component names and paths."""
    problems: list[str] = []
    stages = list(scope_map.get("stages") or {})
    if not stages:
        problems.append("没有定义任何阶段")
    problems += [
        f"always: 未知阶段 {stage}" for stage in scope_map.get("always", []) if stage not in stages
    ]
    names: set[str] = set()
    components = list(scope_map.get("components") or [])
    for component in components:
        name = str(component.get("name") or "")
        if not name or name in names:
            problems.append(f"组件没有名称或名称重复：{name!r}")
        names.add(name)
        if not component.get("paths"):
            problems.append(f"{name}: 没有路径")
        if not component.get("stages"):
            problems.append(f"{name}: 没有阶段")
        problems += [
            f"{name}: 未知阶段 {stage}"
            for stage in component.get("stages", [])
            if stage not in stages
        ]
    for path in (MAP_PATH, RESOLVER_PATH, WORKFLOW_PATH):
        owners = [
            c for c in components if any(glob_regex(g).match(path) for g in c.get("paths", []))
        ]
        if not any(set(stages) <= set(c.get("stages", [])) for c in owners):
            problems.append(f"改动 {path} 必须选中全部阶段")
    return problems


def resolve(
    scope_map: Mapping[str, Any], changed: Iterable[str], all_reason: str | None = None
) -> dict[str, Any]:
    """The plan for a change: the selected stages in the map's order, the matched components and
    the paths that match no component."""
    order = list(scope_map["stages"])
    compiled = [
        (component, [glob_regex(glob) for glob in component["paths"]])
        for component in scope_map["components"]
    ]
    selected = set(scope_map.get("always", []))
    components: list[str] = []
    unmapped: list[str] = []
    paths = list(changed)
    for path in paths:
        hits = [c for c, patterns in compiled if any(p.match(path) for p in patterns)]
        if not hits:
            unmapped.append(path)
        for component in hits:
            if component["name"] not in components:
                components.append(component["name"])
            selected.update(component["stages"])
    reason = f"改动：{'、'.join(components)}" if components else "没有改动任何组件"
    if all_reason is not None:
        selected.update(order)
        reason = f"全部阶段：{all_reason}"
    return {
        "stages": [stage for stage in order if stage in selected],
        "reason": reason,
        "components": components,
        "unmapped": unmapped,
        "changed": len(paths),
    }


def changed_paths(base: str, head: str, root: Path = ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", base, head],
        cwd=root,
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8")
    return [path for path in out.split("\0") if path]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="CI scope")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--all", dest="all_reason", metavar="REASON")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    scope_map = load_map()
    invalid = validate_map(scope_map)
    if invalid:
        for problem in invalid:
            print(f"scope: {MAP_PATH}: {problem}", file=sys.stderr)
        return 1
    plan = resolve(scope_map, changed_paths(args.base, args.head), args.all_reason)
    plan["head"] = os.environ.get("GITHUB_SHA") or args.head
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if plan["unmapped"]:
        for path in plan["unmapped"]:
            print(
                f"scope: {path} 不属于 {MAP_PATH} 的任何组件，请把它加入一个组件", file=sys.stderr
            )
        return 1
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"stages={json.dumps(plan['stages'])}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
