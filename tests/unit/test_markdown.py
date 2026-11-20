"""Markdown in the Dashboard app's document dialog (UC-DIRECT-005, DN006 §8.10, T278)."""

from __future__ import annotations

import re

import pytest

from tsw.host.markdown import anchor_id, inline, render_markdown, safe_href
from tsw.host.status_assets import APP_SCRIPT

mark = pytest.mark


def md(source: str) -> str:
    out = render_markdown(source)
    assert out.startswith('<article class="md">') and out.endswith("</article>")
    return out[len('<article class="md">') : -len("</article>")]


@mark.use_case("UC-DIRECT-005")
def test_headings_paragraphs_and_breaks() -> None:
    assert md("# 任务书 T278 a1\n\n## 目标\n###### six ##") == (
        '<h1 id="md-任务书-t278-a1">任务书 T278 a1</h1><h2 id="md-目标">目标</h2>'
        '<h6 id="md-six">six</h6>'
    )
    assert md("第一行\n第二行") == "<p>第一行\n第二行</p>"
    assert md("hard  \nbreak") == "<p>hard<br>\nbreak</p>"
    assert md("Title\n=====\n\nSub\n---") == '<h1 id="md-title">Title</h1><h2 id="md-sub">Sub</h2>'
    assert md("#not a heading") == "<p>#not a heading</p>"
    assert md("---") == "<hr>" and md("* * *") == "<hr>"


@mark.use_case("UC-DIRECT-005")
def test_emphasis_code_and_escapes() -> None:
    assert inline("**粗** 与 *斜* 与 _斜_ 与 ~~删~~") == (
        "<strong>粗</strong> 与 <em>斜</em> 与 <em>斜</em> 与 <del>删</del>"
    )
    # Names with underscores stay as they are; a lone star is text.
    assert inline("snake_case_name 与 a * b") == "snake_case_name 与 a * b"
    assert inline("`tsw context rebuild` 与 `` a`b ``") == (
        "<code>tsw context rebuild</code> 与 <code>a`b</code>"
    )
    assert inline("`<b>` 里不解析 **x**") == "<code>&lt;b&gt;</code> 里不解析 <strong>x</strong>"
    assert inline(r"\*不是强调\* 与 \[x\]") == "*不是强调* 与 [x]"


@mark.use_case("UC-DIRECT-005")
def test_lists_nest_and_carry_task_boxes() -> None:
    assert md("- a\n- b\n  - c\n  - d\n- e") == (
        "<ul><li>a</li><li>b<ul><li>c</li><li>d</li></ul></li><li>e</li></ul>"
    )
    assert md("1. 一\n2. 二\n\n3) 另一个列表") == (
        '<ol><li>一</li><li>二</li></ol><ol start="3"><li>另一个列表</li></ol>'
    )
    assert md("- [x] 完成\n- [ ] 未完成") == (
        '<ul><li class="task"><input type="checkbox" disabled checked> 完成</li>'
        '<li class="task"><input type="checkbox" disabled> 未完成</li></ul>'
    )
    # Two paragraphs in an item make the list loose; a lazy line continues its item.
    assert md("- a\n\n  more\n- b") == "<ul><li><p>a</p><p>more</p></li><li><p>b</p></li></ul>"
    assert md("- a\ncontinued") == "<ul><li>a\ncontinued</li></ul>"
    # A list interrupts a paragraph (an ordered one only from 1).
    assert md("段落\n- x") == "<p>段落</p><ul><li>x</li></ul>"
    assert md("year\n2026. was") == "<p>year\n2026. was</p>"


@mark.use_case("UC-DIRECT-005")
def test_tables_with_alignment_and_escaped_pipes() -> None:
    out = md("| 工作 | 先读 | n |\n| --- | :-: | --: |\n| a `x|y` | b \\| c | 1 |\n| short |")
    assert out == (
        '<div class="tbl"><table><thead><tr><th>工作</th>'
        '<th style="text-align:center">先读</th><th style="text-align:right">n</th></tr></thead>'
        '<tbody><tr><td>a <code>x|y</code></td><td style="text-align:center">b | c</td>'
        '<td style="text-align:right">1</td></tr>'
        '<tr><td>short</td><td style="text-align:center"></td>'
        '<td style="text-align:right"></td></tr></tbody></table></div>'
    )
    # A table interrupts a paragraph; a pipe alone does not make one.
    assert md("text\n| a |\n|---|\n| 1 |").startswith('<p>text</p><div class="tbl">')
    assert md("a | b") == "<p>a | b</p>"


@mark.use_case("UC-DIRECT-005")
def test_code_blocks_and_quotes() -> None:
    assert md("```python\nprint('<x>')\n\n  indented\n```") == (
        '<pre><code class="language-python">print(&#x27;&lt;x&gt;&#x27;)\n\n  indented</code></pre>'
    )
    assert md("~~~\nunclosed") == "<pre><code>unclosed</code></pre>"
    assert md("    code\n    more") == "<pre><code>code\nmore</code></pre>"
    assert md("> 引用 **重点**\n> - 列表\n\nafter") == (
        "<blockquote><p>引用 <strong>重点</strong></p><ul><li>列表</li></ul></blockquote>"
        "<p>after</p>"
    )


@mark.use_case("UC-DIRECT-005")
def test_links_keep_only_safe_targets() -> None:
    assert inline("[设计](docs/design/DN006.md#810) 与 [站点](https://example.test/a?b=1&c=2)") == (
        '<a href="docs/design/DN006.md#810">设计</a> 与 '
        '<a href="https://example.test/a?b=1&amp;c=2">站点</a>'
    )
    for bad in (
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\tscript:alert(1)",
        "&#106;avascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:x",
        "file:///etc/passwd",
    ):
        assert safe_href(bad) is None, bad
        out = inline(f"[点我]({bad})")
        assert "href" not in out and "点我" in out, bad
    assert safe_href("mailto:a@example.com") == "mailto:a@example.com"
    assert safe_href("#anchor") == "#anchor" and safe_href("../x.md") == "../x.md"
    assert inline("<https://example.test/x> <a@example.com>") == (
        '<a href="https://example.test/x">https://example.test/x</a> '
        '<a href="mailto:a@example.com">a@example.com</a>'
    )
    # A link's own text holds no other link.
    assert inline("[see https://example.test](https://example.test/b)") == (
        '<a href="https://example.test/b">see https://example.test</a>'
    )


@mark.use_case("UC-DIRECT-005")
def test_bare_urls_stop_before_trailing_punctuation() -> None:
    assert inline("见 https://example.test/a。下一句") == (
        '见 <a href="https://example.test/a">https://example.test/a</a>。下一句'
    )
    assert inline("(https://example.test/b).") == (
        '(<a href="https://example.test/b">https://example.test/b</a>).'
    )
    assert inline("https://example.test/wiki/A_(b)") == (
        '<a href="https://example.test/wiki/A_(b)">https://example.test/wiki/A_(b)</a>'
    )


@mark.use_case("UC-DIRECT-005")
def test_raw_html_and_images_never_become_markup() -> None:
    out = md(
        '<script>alert(1)</script>\n\n<img src=x onerror="alert(1)">\n\n'
        '![图 <1>](https://example.test/p.png "t") and [x](https://e.test "<b>")\n\n'
        "[a](javascript:alert(1)) **<i>**"
    )
    assert "<script" not in out and "<img" not in out and "<i>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert 'onerror="' not in out  # only as escaped text
    assert '<a href="https://example.test/p.png" class="img">图片：图 &lt;1&gt;</a>' in out
    # Only the elements and attributes the renderer writes appear.
    tags = set(re.findall(r"<([a-z0-9]+)", out))
    assert tags <= {"p", "a", "strong", "br"}
    attrs = set(re.findall(r'\s([a-z-]+)="', out))
    assert attrs <= {"href", "class"}


@mark.use_case("UC-DIRECT-005")
def test_a_whole_brief_renders() -> None:
    source = (
        "\ufeff# 任务书 T278 a1\r\n\r\n- 标题：[tsw dashboard] 引用\r\n"
        "- 授权读取集合：T266, T273\r\n"
        "\r\n## 目标\r\n\r\n用户的原话：“新task”。\r\n\r\n要做的：\r\n1. 引用\r\n2. 卡片\r\n"
    )
    out = md(source)
    assert out.startswith('<h1 id="md-任务书-t278-a1">任务书 T278 a1</h1><ul><li>标题：')
    assert '<h2 id="md-目标">目标</h2>' in out and "<ol><li>引用</li><li>卡片</li></ol>" in out
    assert "\r" not in out and "\ufeff" not in out


@mark.use_case("UC-DIRECT-005")
def test_headings_have_unique_ids_and_anchors_point_at_them() -> None:
    # GitHub's slugs behind a prefix, numbered when repeated, also inside lists and quotes.
    out = md(
        "[目录](#验收) [另一处](#%E7%9B%AE%E6%A0%87) [文件](other.md#Section-2)\n\n"
        "## 目标\n\n## 验收 `T278`!\n\n> ## 目标\n\n- ## 目标"
    )
    assert re.findall(r'<h2 id="([^"]+)"', out) == [
        "md-目标",
        "md-验收-t278",
        "md-目标-1",
        "md-目标-2",
    ]
    assert '<a href="#md-验收">目录</a>' in out and '<a href="#md-目标">另一处</a>' in out
    # Another file's fragment is the dialog's to resolve against that file.
    assert '<a href="other.md#Section-2">文件</a>' in out
    assert anchor_id("Hello, World <x>") == "md-hello-world-x"
    # A heading named like a numbered repeat never shares an id, in either order.
    for source, ids in (
        ("# A\n\n# A\n\n# A-1\n\n# A", ["md-a", "md-a-1", "md-a-1-1", "md-a-2"]),
        ("# A-1\n\n# A\n\n# A", ["md-a-1", "md-a", "md-a-2"]),
    ):
        found = re.findall(r'<h1 id="([^"]+)"', md(source))
        assert found == ids and len(set(found)) == len(found), source
    assert '<a href="#md-a-1-1">x</a>' in md("[x](#a-1-1)\n\n# A\n\n# A\n\n# A-1")
    # The dialog keeps the fragment, scrolls to it, and only shows the latest request.
    assert "function reveal(hash)" in APP_SCRIPT and 'if (href.startsWith("#"))' in APP_SCRIPT
    assert (
        "if (mine !== latest || !dialog.open) return;" in APP_SCRIPT
        and 'dialog.addEventListener("close", released)' in APP_SCRIPT
    )


@mark.use_case("UC-DIRECT-005")
def test_link_targets_keep_spaces_and_labels_skip_code() -> None:
    # The scheme check ignores spaces; the target keeps them for the URL parser to encode.
    assert inline("[报告](<other report.md>)") == '<a href="other report.md">报告</a>'
    assert safe_href(" a b.md ") == "a b.md"
    assert safe_href(" java script:x ") is None  # read as browsers might: refused
    assert safe_href("java\nscript:alert(1)") is None
    # Balanced parentheses belong to the target; the unsafe one is refused whole.
    assert inline("[w](https://example.com/A_(b)) x") == (
        '<a href="https://example.com/A_(b)">w</a> x'
    )
    assert inline("[链接](javascript:alert(1))") == "链接"
    # A bracket inside a code span does not end the link's text.
    assert inline("[use `]` here](other.md)") == '<a href="other.md">use <code>]</code> here</a>'
    # No link inside a link's text: the inner one stays text.
    assert inline("[a [b](x) c](y)") == '<a href="y">a [b](x) c</a>'
    # A backslash does not escape inside code, but an escaped backtick opens nothing.
    assert inline(r"`a\`") == r"<code>a\</code>"
    assert inline(r"\` `b`") == "` <code>b</code>"
    # Parity of backslashes, and a backtick consumed by a link target, as the parser reads them.
    assert inline(r"\\`code`") == r"\<code>code</code>"
    assert inline("[x](url`x) and `code`") == '<a href="url`x">x</a> and <code>code</code>'
    assert inline("[x](url`x) and [y](other.md) and `code`") == (
        '<a href="url`x">x</a> and <a href="other.md">y</a> and <code>code</code>'
    )


@mark.use_case("UC-DIRECT-005")
@pytest.mark.parametrize(
    "piece",
    [
        "[",
        "![",
        "[a](",
        "[a](<",
        "[a](x '",
        "[a](((",
        "[a](b)))",
        "a`",
        "a`` `",
        "snake_case_name ",
        "*a ",
        "_a ",
        "**a _b ",
    ],
)
def test_unmatched_markup_renders_in_one_pass(piece: str) -> None:
    # Each opener is looked up, not searched for, so a long paragraph of unmatched markup
    # renders in about linear time (a quadratic scan of 300 000 characters would not finish).
    source = piece * (300_000 // len(piece))
    out = md(source)
    assert out.startswith("<p>") and len(out) >= len(source) // 2
