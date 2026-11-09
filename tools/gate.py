"""The local gate, also run by CI (docs/development/ci.md).

    python tools/gate.py                         every step and every CI tier
    python tools/gate.py --tier unit             only some tiers (plus the other steps)
    python tools/gate.py --steps lint,stdlib     only some steps
    python tools/gate.py --verify-ci DIR ...     the aggregate CI ``Gate`` check

Steps: ``lint`` (ruff, ruff format, pyright, actionlint), ``stdlib`` (runtime imports only the
standard library), ``docs`` (tools/docs.py check), ``tests`` (each tier on its own, its test
files split into ``--jobs`` parallel pytest processes whose results are merged),
``catalog`` (use-case and transition coverage of this run). Results go to ``--results-dir``
(default ``.local/gate/<run id>/``) together with ``summary.json``.

A tier that collects no tests, or does not write a fresh result file, fails the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.checks import catalog as catalog_check  # noqa: E402
from tools.checks import stdlib_only  # noqa: E402
from tools.checks.repo import utf8_stdio  # noqa: E402
from tsw.cli import iter_commands  # noqa: E402
from tsw.host.mcp import tool_names as mcp_tool_names  # noqa: E402
from tsw.machine.runner import tool_names  # noqa: E402

STEPS = ("lint", "stdlib", "docs", "tests", "catalog")
CI_TIERS = ("unit", "component", "surface", "conformance")
MAX_JOBS = 8


def _tool(name: str) -> str:
    """A console script installed next to the running interpreter, else one on PATH."""
    scripts = Path(sys.executable).parent
    for candidate in (scripts / name, scripts / f"{name}.exe", scripts / "Scripts" / f"{name}.exe"):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found is None:
        raise FileNotFoundError(f"找不到 {name}，请先 pip install -r requirements-dev.txt")
    return found


def default_jobs() -> int:
    """Parallel pytest processes per tier: the CPU count, at most ``MAX_JOBS``."""
    return max(1, min(os.cpu_count() or 1, MAX_JOBS))


def _shards(tier_dir: Path, jobs: int) -> list[list[Path]]:
    """Split a tier's test files into at most ``jobs`` sets of similar size.

    A file's size in bytes stands in for its run time: the largest file goes to the lightest
    set first. Every test file is in exactly one set; the split depends only on the files.
    """
    files = sorted(
        {*tier_dir.glob("test_*.py"), *tier_dir.glob("*_test.py")},
        key=lambda path: (-path.stat().st_size, path.name),
    )
    if not files:
        return [[tier_dir]]
    shards: list[list[Path]] = [[] for _ in range(min(jobs, len(files)))]
    loads = [0] * len(shards)
    for path in files:
        lightest = loads.index(min(loads))
        shards[lightest].append(path)
        loads[lightest] += path.stat().st_size
    return [sorted(shard) for shard in shards]


def _run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


class Gate:
    def __init__(self, results_dir: Path) -> None:
        self.results_dir = results_dir
        self.steps: dict[str, dict[str, Any]] = {}
        self.tiers: dict[str, dict[str, Any]] = {}
        self.test_results: list[dict[str, Any]] = []

    def record(self, step: str, ok: bool, detail: list[str] | str) -> None:
        lines = detail if isinstance(detail, list) else [detail] if detail else []
        self.steps[step] = {"status": "passed" if ok else "failed", "detail": lines}
        print(f"[{'PASS' if ok else 'FAIL'}] {step}")
        for line in lines[:40]:
            print(f"    {line}")
        if len(lines) > 40:
            print(f"    … 另有 {len(lines) - 40} 行，见 {self.results_dir / 'summary.json'}")

    # ------------------------------------------------------------------ steps

    def lint(self) -> None:
        problems: list[str] = []
        commands = [
            [_tool("ruff"), "check", "."],
            [_tool("ruff"), "format", "--check", "."],
            [_tool("pyright"), "--pythonpath", sys.executable],
            [_tool("actionlint"), "-color=false"],
        ]
        for cmd in commands:
            code, output = _run(cmd)
            if code != 0:
                problems.append(
                    f"$ {' '.join(Path(cmd[0]).stem if i == 0 else c for i, c in enumerate(cmd))}"
                )
                problems.extend(output.splitlines()[-30:])
        self.record("lint", not problems, problems)

    def stdlib(self) -> None:
        problems = stdlib_only.check(ROOT)
        self.record("stdlib", not problems, problems)

    def docs(self) -> None:
        code, output = _run([sys.executable, "-X", "utf8", "tools/docs.py", "check"])
        self.record("docs", code == 0, output.splitlines())

    def tests(self, tiers: list[str], jobs: int) -> None:
        problems: list[str] = []
        with tempfile.TemporaryDirectory(prefix="tsw-gate-", ignore_cleanup_errors=True) as tmp:
            for tier in tiers:
                problems += self._tier(tier, jobs, Path(tmp))
        problems += self._coverage(tiers)
        self.record("tests", not problems, problems)

    def _tier(self, tier: str, jobs: int, tmp: Path) -> list[str]:
        """Run one tier as parallel pytest processes over disjoint sets of its test files.

        Each process (shard) has its own result file, coverage data file, junit file and
        ``--basetemp``; the shards are merged into ``<tier>.json``, ``<tier>.xml`` and
        ``<tier>.log``, the same evidence as one run of the whole tier.
        """
        problems: list[str] = []
        for pattern in (f"{tier}.*", f".coverage.{tier}*"):
            for stale in self.results_dir.glob(pattern):
                stale.unlink()
        shards = _shards(ROOT / "tests" / tier, jobs)
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PYTHONUTF8"] = "1"
        started = time.time()
        procs: list[tuple[int, subprocess.Popen[str], Path]] = []
        for index, files in enumerate(shards, start=1):
            shard_env = dict(env)
            shard_env["TSW_RESULTS_FILE"] = str(self.results_dir / f"{tier}.{index}.json")
            log = self.results_dir / f"{tier}.{index}.out"
            with log.open("w", encoding="utf-8") as out:
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "coverage",
                        "run",
                        f"--data-file={self.results_dir / f'.coverage.{tier}.{index}'}",
                        "-m",
                        "pytest",
                        *(path.relative_to(ROOT).as_posix() for path in files),
                        "-q",
                        "-p",
                        "no:cacheprovider",
                        f"--basetemp={tmp / f'{tier}-{index}'}",
                        f"--junitxml={self.results_dir / f'{tier}.{index}.xml'}",
                    ],
                    cwd=ROOT,
                    env=shard_env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            procs.append((index, proc, log))
        summary: dict[str, Any] = {"collected": 0, "passed": 0, "failed": 0, "skipped": 0}
        summary.update({"exit": 0, "processes": len(shards)})
        merged: dict[str, Any] = {"started": started, "finished": started, "exitstatus": 0}
        merged.update({"collected": 0, "tests": []})
        logs: list[str] = []
        suites = ET.Element("testsuites")
        for index, proc, log in procs:
            code = proc.wait()
            output = log.read_text(encoding="utf-8", errors="replace").strip()
            log.unlink()
            logs.append(f"===== {tier} {index}/{len(shards)}（退出码 {code}）=====\n{output}")
            summary["exit"] = summary["exit"] or code
            junit = self.results_dir / f"{tier}.{index}.xml"
            if junit.is_file():
                suites.extend(ET.parse(junit).getroot())
                junit.unlink()
            result_file = self.results_dir / f"{tier}.{index}.json"
            if not result_file.is_file():
                problems.append(f"{tier}[{index}]: 没有生成结果文件（{output.splitlines()[-1:]}）")
                continue
            data = json.loads(result_file.read_text(encoding="utf-8"))
            result_file.unlink()
            if data["started"] < started - 1:
                problems.append(f"{tier}[{index}]: 结果文件不是本次运行生成的")
            merged["finished"] = max(merged["finished"], data["finished"])
            merged["collected"] += int(data["collected"])
            merged["tests"] += data["tests"]
            if code != 0:
                problems.append(f"{tier}[{index}]: pytest 退出码 {code}，详见 {tier}.log")
                problems.extend(line for line in output.splitlines() if "FAILED" in line)
        merged["exitstatus"] = summary["exit"]
        (self.results_dir / f"{tier}.log").write_text("\n\n".join(logs) + "\n", encoding="utf-8")
        ET.ElementTree(suites).write(self.results_dir / f"{tier}.xml", encoding="utf-8")
        if not any(f"{tier}[" in problem and "结果文件" in problem for problem in problems):
            (self.results_dir / f"{tier}.json").write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for test in merged["tests"]:
            outcome = str(test["outcome"])
            key = outcome if outcome in ("passed", "failed", "skipped") else "failed"
            summary[key] += 1
            self.test_results.append(test)
        summary["collected"] = merged["collected"]
        summary["seconds"] = round(time.time() - started, 1)
        if summary["collected"] == 0:
            problems.append(f"{tier}: 没有发现任何测试")
        self.tiers[tier] = summary
        print(
            f"    {tier}: {summary['collected']} collected, {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['skipped']} skipped "
            f"（{len(shards)} 个进程，{summary['seconds']}s）"
        )
        return problems

    def _coverage(self, tiers: list[str]) -> list[str]:
        """Combine and report line coverage. The number is not a gate; missing evidence is."""
        data_files = sorted(
            path for tier in tiers for path in self.results_dir.glob(f".coverage.{tier}.*")
        )
        missing = [t for t in tiers if not any(self.results_dir.glob(f".coverage.{t}.*"))]
        if missing:
            return [f"缺少覆盖数据：{missing}"]
        combined = self.results_dir / ".coverage"
        combined.unlink(missing_ok=True)
        code, output = _run(
            [
                sys.executable,
                "-m",
                "coverage",
                "combine",
                "--keep",
                f"--data-file={combined}",
                *map(str, data_files),
            ]
        )
        if code != 0:
            return [f"coverage combine 失败：{output.splitlines()[-1:]}"]
        code, report = _run([sys.executable, "-m", "coverage", "report", f"--data-file={combined}"])
        totals = [line for line in report.splitlines() if line.startswith("TOTAL")]
        if code != 0 or not totals:
            return [f"coverage report 失败：{report.splitlines()[-1:]}"]
        (self.results_dir / "coverage.txt").write_text(report, encoding="utf-8")
        print(f"    行覆盖率（只报告，不作门禁）：{totals[0]}")
        return []

    def catalog(self, tiers_run: set[str]) -> None:
        problems, not_verified = catalog_check.check(
            ROOT,
            self.test_results,
            tiers_run,
            iter_commands(),
            tools=tool_names(),
            mcp_tools=mcp_tool_names(),
        )
        self.record("catalog", not problems, problems + [f"未验证：{n}" for n in not_verified])

    # ------------------------------------------------------------------ summary

    def write_summary(self) -> bool:
        ok = all(step["status"] == "passed" for step in self.steps.values())
        summary = {
            "ok": ok,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "github_run": {
                "id": os.environ.get("GITHUB_RUN_ID"),
                "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "sha": os.environ.get("GITHUB_SHA"),
            },
            "steps": self.steps,
            "tiers": self.tiers,
        }
        (self.results_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"gate: {'OK' if ok else 'FAILED'}  ({self.results_dir / 'summary.json'})")
        return ok


def verify_ci(artifacts: Path, needs_json: str, expected: list[str]) -> int:
    """The aggregate ``Gate`` job: every needed job succeeded and left a fresh, passing summary."""
    problems: list[str] = []
    needs: dict[str, Any] = json.loads(needs_json or "{}")
    problems.extend(
        f"任务 {job} 的结果为 {info.get('result')}"
        for job, info in needs.items()
        if info.get("result") != "success"
    )
    run_id = os.environ.get("GITHUB_RUN_ID")
    for name in expected:
        path = artifacts / name / "summary.json"
        if not path.is_file():
            problems.append(f"缺少证据：{name}/summary.json")
            continue
        summary = json.loads(path.read_text(encoding="utf-8"))
        if not summary.get("ok"):
            problems.append(f"{name}: 门禁未通过")
        if run_id and summary.get("github_run", {}).get("id") != run_id:
            problems.append(f"{name}: 证据不是本次运行生成的")
        for tier, counts in summary.get("tiers", {}).items():
            if counts.get("collected", 0) == 0:
                problems.append(f"{name}: {tier} 层级没有发现测试")
        if name.startswith("test-"):
            if set(summary.get("tiers", {})) != set(CI_TIERS):
                problems.append(f"{name}: 测试层级不完整 {sorted(summary.get('tiers', {}))}")
            evidence = [f"{tier}.json" for tier in CI_TIERS] + ["coverage.txt"]
            problems.extend(
                f"{name}: 缺少证据文件 {file}"
                for file in evidence
                if not (artifacts / name / file).is_file()
            )
            if summary.get("steps", {}).get("catalog", {}).get("status") != "passed":
                problems.append(f"{name}: 目录检查没有通过")
    for problem in problems:
        print(problem)
    print("Gate: " + ("OK" if not problems else "FAILED"))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    parser = argparse.ArgumentParser(description="TaskSteward gate")
    parser.add_argument("--steps", default=",".join(STEPS))
    parser.add_argument("--tier", action="append", choices=CI_TIERS)
    parser.add_argument(
        "--jobs",
        type=int,
        default=default_jobs(),
        help=f"每个层级并行的 pytest 进程数（默认为 CPU 数，至多 {MAX_JOBS}；1 为不并行）",
    )
    parser.add_argument("--results-dir")
    parser.add_argument("--verify-ci", metavar="ARTIFACTS")
    parser.add_argument(
        "--needs-json", default=os.environ.get("NEEDS", "{}"), help="默认读取环境变量 NEEDS"
    )
    parser.add_argument("--expect", default="lint,docs,test-3.10,test-3.13")
    args = parser.parse_args(argv)
    if args.verify_ci:
        return verify_ci(Path(args.verify_ci), args.needs_json, args.expect.split(","))

    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    unknown = [step for step in steps if step not in STEPS]
    if unknown:
        parser.error(f"未知步骤 {unknown}")
    if args.jobs < 1:
        parser.error("--jobs 至少为 1")
    tiers: list[str] = list(args.tier or CI_TIERS)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_dir = Path(args.results_dir) if args.results_dir else ROOT / ".local" / "gate" / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    gate = Gate(results_dir.resolve())
    if "lint" in steps:
        gate.lint()
    if "stdlib" in steps:
        gate.stdlib()
    if "docs" in steps:
        gate.docs()
    if "tests" in steps:
        gate.tests(tiers, args.jobs)
    if "catalog" in steps:
        if "tests" not in steps:
            gate.record("catalog", False, "catalog 步骤需要同一次运行中的 tests 步骤")
        else:
            gate.catalog(set(tiers))
    return 0 if gate.write_summary() else 1


if __name__ == "__main__":
    sys.exit(main())
