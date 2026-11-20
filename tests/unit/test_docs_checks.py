"""The documentation checks on synthetic repositories: views (tools/docs.py), links, the rule
documents' automation (tools/checks/automation.py), the privacy scan and the standard-library
check. Each rule fails on an input made to break it."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import docs
from tools.checks import automation, privacy, stdlib_only


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _doc_tree(root: Path) -> list[Path]:
    _write(root / "README.md", "# 项目\n\n见 [DN001](docs/design/DN001-a.md)。\n")
    _write(root / "docs" / "design" / "DN001-a.md", "# DN001\n\n## 背景\n\n正文。\n")
    _write(root / "docs" / "design" / "claude-code" / "CC001-a.md", "# CC001\n\n正文。\n")
    return docs.render_all(root)


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------- views


def test_fresh_views_pass(tmp_path: Path) -> None:
    written = _doc_tree(tmp_path)
    assert len(written) == 3
    assert docs.check_views(tmp_path, _files(tmp_path)) == []


def test_a_stale_view_fails(tmp_path: Path) -> None:
    _doc_tree(tmp_path)
    _write(tmp_path / "docs" / "design" / "DN001-a.md", "# DN001\n\n改过了。\n")
    assert docs.check_views(tmp_path, _files(tmp_path)) == [
        "docs/design/DN001-a.html: 已过期，运行 python tools/docs.py render"
    ]


def test_a_missing_view_fails(tmp_path: Path) -> None:
    _doc_tree(tmp_path)
    (tmp_path / "docs" / "design" / "claude-code" / "CC001-a.html").unlink()
    assert docs.check_views(tmp_path, _files(tmp_path)) == [
        "docs/design/claude-code/CC001-a.html: 缺少 HTML，运行 python tools/docs.py render"
    ]


def test_a_view_left_without_its_source_fails(tmp_path: Path) -> None:
    _doc_tree(tmp_path)
    (tmp_path / "docs" / "design" / "DN001-a.md").rename(
        tmp_path / "docs" / "design" / "DN001-b.md"
    )
    _write(tmp_path / "docs" / "development" / "old.html", "<p>旧</p>")
    _write(tmp_path / "tsw" / "host" / "page.html", "<p>产品资源，不是文档视图</p>")
    problems = docs.check_views(tmp_path, _files(tmp_path))
    assert (
        "docs/design/DN001-a.html: 孤立的 HTML，没有对应的 Markdown 来源；删除它或补上来源"
        in problems
    )
    assert (
        "docs/development/old.html: 孤立的 HTML，没有对应的 Markdown 来源；删除它或补上来源"
        in problems
    )
    assert "docs/design/DN001-b.html: 缺少 HTML，运行 python tools/docs.py render" in problems
    assert not any(p.startswith("tsw/") for p in problems)


# ---------------------------------------------------------------------- links


def test_broken_links_and_anchors_fail(tmp_path: Path) -> None:
    _doc_tree(tmp_path)
    readme = _write(
        tmp_path / "README.md",
        "# 项目\n\n[有](docs/design/DN001-a.md) [没有](docs/nope.md) `[代码](不算.md)`\n",
    )
    page = _write(
        tmp_path / "docs" / "design" / "x.html",
        '<a href="DN001-a.html#背景">好</a><a href="DN001-a.html#nope">坏</a>',
    )
    assert docs.check_links([readme, page], tmp_path) == [
        "README.md: 链接目标不存在 docs/nope.md",
        "docs/design/x.html: 锚点不存在 DN001-a.html#nope",
    ]


# ---------------------------------------------------------------------- automation


def _rules(root: Path, text: str) -> None:
    _write(root / "AGENTS.md", "# AGENTS\n")
    _write(root / "docs" / "development" / "ci.md", text)
    _write(root / "tools" / "gate.py", 'STEPS = ("lint",)\nTIERS = ("unit",)\nARG = "--tier"\n')
    _write(root / ".github" / "workflows" / "ci.yml", "if: contains(labels, 'ci:all')\n")


def test_named_commands_and_labels_that_exist_pass(tmp_path: Path) -> None:
    _rules(
        tmp_path,
        "运行 `python tools/gate.py --tier unit <路径>`，加标签 `ci:all`，`python <路径>`。\n",
    )
    assert automation.check(tmp_path) == []


def test_a_missing_script_an_unknown_word_and_an_unhandled_label_fail(tmp_path: Path) -> None:
    _rules(
        tmp_path,
        "`python tools/nope.py`\n`python -X utf8 tools/gate.py --tier smoke`\n标签 `ci:fast`\n",
    )
    assert automation.check(tmp_path) == [
        "docs/development/ci.md:1: 命令的脚本不存在 tools/nope.py",
        "docs/development/ci.md:2: tools/gate.py 不认识 smoke",
        "docs/development/ci.md:3: 标签 ci:fast 没有被 .github/workflows 处理",
    ]


@pytest.mark.parametrize(
    "workflow",
    [
        "# the label 'ci:all' selects every stage\nrun: echo\n",
        "run: echo  # 'ci:all' is handled elsewhere\n",
        "if: contains(labels, 'ci:allx')\n",
        "if: contains(labels, 'ci:all-stages')\n",
    ],
)
def test_a_label_named_only_in_a_comment_or_inside_a_longer_label_fails(
    tmp_path: Path, workflow: str
) -> None:
    _rules(tmp_path, "标签 `ci:all`\n")
    _write(tmp_path / ".github" / "workflows" / "ci.yml", workflow)
    assert automation.check(tmp_path) == [
        "docs/development/ci.md:1: 标签 ci:all 没有被 .github/workflows 处理"
    ]


def test_the_repository_rule_documents_pass() -> None:
    assert automation.check(docs.ROOT) == []


# ---------------------------------------------------------------------- privacy


def test_privacy_scan_finds_user_paths_emails_and_private_terms(tmp_path: Path) -> None:
    # Built from pieces so that this file itself passes the scan.
    windows_path = "C:" + "\\Users\\someone\\repo"
    home_path = "/home/" + "someone/repo"
    email = "a.person" + "@corp.example.io"
    _write(tmp_path / ".local" / "privacy-terms.txt", "# 注释\nZzPrivateTerm\n")
    text = _write(
        tmp_path / "notes.md",
        "\n".join(
            [
                windows_path,
                home_path,
                f"mail {email}",
                "ok 123+name@users.noreply.github.com noreply@anthropic.com x@example.com",
                "built at zzprivateterm",
            ]
        ),
    )
    binary = _write(tmp_path / "logo.png", windows_path)
    local = _write(tmp_path / ".local" / "x.md", windows_path)
    assert privacy.scan([text, binary, local], tmp_path) == [
        "notes.md:1: 本机用户路径",
        "notes.md:2: 本机用户路径",
        "notes.md:3: 邮箱地址",
        "notes.md:5: 私有词表命中",
    ]


# ---------------------------------------------------------------------- standard library only


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import json\nfrom pathlib import Path\nfrom tsw import ledger\nfrom . import x\n", []),
        ("import requests\n", ["tsw/m.py:1: 运行时导入了非标准库模块 requests"]),
        ("from yaml.loader import Loader\n", ["tsw/m.py:1: 运行时导入了非标准库模块 yaml.loader"]),
        ("import tomllib\n", ["tsw/m.py:1: 运行时导入了非标准库模块 tomllib"]),
    ],
)
def test_runtime_imports_only_the_standard_library_of_python_310(
    tmp_path: Path, source: str, expected: list[str]
) -> None:
    _write(tmp_path / "tsw" / "m.py", source)
    _write(tmp_path / "tests" / "t.py", "import pytest\n")
    assert stdlib_only.check(tmp_path) == expected


def test_plugin_code_is_runtime_too(tmp_path: Path) -> None:
    _write(tmp_path / "plugin" / "bin" / "launch.py", "import psutil\n")
    assert stdlib_only.check(tmp_path) == [
        "plugin/bin/launch.py:1: 运行时导入了非标准库模块 psutil"
    ]
