"""Markdown in the Dashboard app's document dialog (UC-DIRECT-005, DN006 §8.10, T278)."""

from __future__ import annotations

import re

import pytest

from tsw.host.markdown import inline, render_markdown, safe_href

mark = pytest.mark


def md(source: str) -> str:
    out = render_markdown(source)
    assert out.startswith('<article class="md">') and out.endswith("</article>")
    return out[len('<article class="md">') : -len("</article>")]


@mark.use_case("UC-DIRECT-005")
def test_headings_paragraphs_and_breaks() -> None:
    assert md("# 任务书 T278 a1\n\n## 目标\n###### six ##") == (
        "<h1>任务书 T278 a1</h1><h2>目标</h2><h6>six</h6>"
    )
    assert md("第一行\n第二行") == "<p>第一行\n第二行</p>"
    assert md("hard  \nbreak") == "<p>hard<br>\nbreak</p>"
    assert md("Title\n=====\n\nSub\n---") == "<h1>Title</h1><h2>Sub</h2>"
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
    assert out.startswith("<h1>任务书 T278 a1</h1><ul><li>标题：[tsw dashboard] 引用</li>")
    assert "<h2>目标</h2>" in out and "<ol><li>引用</li><li>卡片</li></ol>" in out
    assert "\r" not in out and "\ufeff" not in out
