"""The best-effort command-line reader used by the PreToolUse hook."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsw.host import shell

pytestmark = pytest.mark.use_case("UC-HOST-002")


def test_split_honours_quotes_operators_and_redirections() -> None:
    segments = shell.split("echo 'a; b' > \"out file.txt\" 2>&1 && cat < in.txt | tee -a log")
    assert [s.words for s in segments] == [["echo", "a; b"], ["cat"], ["tee", "-a", "log"]]
    assert segments[0].redirects == ["out file.txt"]
    assert shell.split("ls &> all.log")[0].redirects == ["all.log"]
    assert shell.split("echo x >>notes.md")[0].redirects == ["notes.md"]


@pytest.mark.parametrize(
    ("command", "written"),
    [
        ("echo x > a.txt", ["a.txt"]),
        ("cd sub && touch b.txt", ["sub/b.txt"]),
        ("cp -r src/ dest/", ["dest"]),
        ("rm -f one two", ["one", "two"]),
        ("sed -i 's/a/b/' f.py", ["f.py"]),
        ("dd if=/dev/zero of=img.bin", ["img.bin"]),
        ("git clone https://example.invalid/r.git clone-dir", ["clone-dir"]),
        ("git worktree add ../wt2 main", ["../wt2"]),
        ('powershell -Command "Out-File -FilePath o.txt"', ["o.txt"]),
        ("cmd /c copy a.txt b.txt", ["b.txt"]),
        ("echo x > /dev/null", []),
        ("git status", []),
        # A move removes its sources; option values are not operands.
        ("mv ../other/file.txt local.txt", ["local.txt", "../other/file.txt"]),
        ("cp -t ../other local.txt", ["../other"]),
        ("cp --target-directory=out a b", ["out"]),
        ("git worktree add -b feature ../outside main", ["../outside"]),
        ("git -C ../other clone https://example.invalid/repo.git local", ["../other/local"]),
        ("git clone -b dev https://example.invalid/repo.git", ["repo"]),
        ("git init -b main fresh", ["fresh"]),
        ("sed -i -e '/^#/d' notes.txt", ["notes.txt"]),
        ("sed -i -f ../scripts/cleanup.sed notes.txt", ["notes.txt"]),
        ("sed -Ei.bak 's/a/b/' f.py", ["f.py"]),
        ("sed 's/a/b/' f.py", []),
        ('powershell -Command "Set-Content paths.txt C:/sdk/bin"', ["paths.txt"]),
        ('powershell -Command "Set-Content -Value C:/sdk -Path p.txt -Force"', ["p.txt"]),
        ('powershell -Command "Copy-Item C:/src/a.txt b.txt"', ["b.txt"]),
        ('powershell -Command "Move-Item ../a.txt -Destination b.txt"', ["../a.txt", "b.txt"]),
        ("robocopy src dst /MOV /R:1", ["dst", "src"]),
        # Heredoc bodies and here-strings are data, not commands.
        ("python - <<'EOF'\nimport os\nrm -rf /\nEOF\ntouch after.txt", ["after.txt"]),
        ("cat <<-END > out.txt\n\trm x\n\tEND", ["out.txt"]),
        ("grep x <<< 'rm -rf y'", []),
        ("cat <<\\EOF\nrm body\nEOF\ntouch real.txt", ["real.txt"]),
        ("cat <<\"E\"O'F'\nrm body\nEOF\ntouch real.txt", ["real.txt"]),
        # A comment is not a heredoc; an unterminated heredoc hides nothing.
        ("echo hi # <<EOF\ntouch real.txt", ["real.txt"]),
        ("cat <<EOF\ntouch real.txt", ["real.txt"]),
        ("echo a#b > x.txt", ["x.txt"]),
        ('cmd /d /c "echo # step & copy a.txt b.txt"', ["b.txt"]),  # cmd has no comments
        # PowerShell positional arguments fill the parameters not given by name.
        ('powershell -Command "Copy-Item -Path local.txt ../outside.txt"', ["../outside.txt"]),
        (
            'powershell -Command "Move-Item -Path local.txt ../outside.txt"',
            ["local.txt", "../outside.txt"],
        ),
        ('powershell -Command "Copy-Item -Destination d.txt s.txt"', ["d.txt"]),
    ],
)
def test_write_targets_are_resolved_against_the_tracked_directory(
    tmp_path: Path, command: str, written: list[str]
) -> None:
    facts = shell.analyze(command, tmp_path)
    assert facts.writes == [Path(str(tmp_path / w).rstrip("/\\")).resolve() for w in written]


def test_git_bash_drive_paths_and_variables(tmp_path: Path) -> None:
    assert shell.resolve("/c/Work/x", tmp_path) == Path("C:/Work/x")
    assert shell.resolve("$HOME/x", tmp_path) is None
    facts = shell.analyze("echo x > $OUT", tmp_path)
    assert (facts.writes, facts.unresolved) == ([], ["$OUT"])


def test_program_names_drop_paths_suffixes_and_wrappers(tmp_path: Path) -> None:
    facts = shell.analyze("env -u X FOO=1 C:/tools/GH.EXE pr list", tmp_path)
    assert [(p.name, p.args) for p in facts.programs] == [("gh", ("pr", "list"))]
    assert shell.git_subcommand(["-C", "r", "--no-pager", "push"]) == ("push", [])
