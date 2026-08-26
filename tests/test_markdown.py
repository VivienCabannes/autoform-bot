from __future__ import annotations

import re
from pathlib import Path

import pytest

from autoform_cli.markdown import (
    content,
    content_lines,
    link_targets,
    local_target_issue,
    markdown_anchors,
    markdown_links,
    visible_text,
)

#: Heading forms whose published anchors are easy to get subtly wrong.
ANCHOR_CORPUS = [
    "# Depends on",
    "# Café",
    "# Naïve Bayes — dashes",
    "# [Linked result](other.md)",
    "# `Code` heading",
    "# *Emphasised* result",
    "# Heading with &amp; entity",
    "# Title {.class}",
    "# Result {#custom-id}",
    "# Trailing hashes ###",
    "# 1. Numbered",
    "# Depends on\n\n## Depends on\n\n### Depends on",
    "# A {#dup}\n\n# B {#dup}",
    "# Depends on\n\n# B {#depends-on}",
    "# A {#depends-on}\n\n# Depends on",
    "# ***",
    "Setext one\n===",
    "Setext two\n---",
    "# Sources\n\nParagraph.\n\n## Sources",
]


def _renderer_anchors(text: str) -> set[str]:
    """The anchors Python-Markdown itself publishes, for differential testing."""

    markdown = pytest.importorskip("markdown")
    # The extensions the generated mkdocs.yml enables that can affect heading IDs.
    html = markdown.Markdown(extensions=["attr_list", "toc", "tables", "md_in_html"]).convert(text)
    return set(re.findall(r'<h[1-6][^>]*\bid="([^"]+)"', html))


@pytest.mark.parametrize("source", ANCHOR_CORPUS)
def test_anchors_match_the_configured_renderer(tmp_path: Path, source: str) -> None:
    # An anchor rule that only approximates the renderer fails in both
    # directions: it rejects fragments that resolve and accepts fragments that
    # never appear on the page. Pin it to the renderer itself.
    article = tmp_path / "article.md"
    article.write_text(source + "\n", encoding="utf-8")

    assert markdown_anchors(article) == _renderer_anchors(source + "\n")


def test_frontmatter_cannot_contribute_anchors(tmp_path: Path) -> None:
    # MkDocs strips frontmatter before Markdown sees it, so a setext-looking
    # closing delimiter must not turn a YAML key into a heading.
    article = tmp_path / "article.md"
    article.write_text("---\nkind: blueprint\n---\n\n# Real heading\n", encoding="utf-8")

    assert markdown_anchors(article) == {"real-heading"}


def test_visible_text_keeps_labels_and_drops_destinations() -> None:
    assert visible_text("[Node](../roadmap/node.md)").strip() == "Node"
    assert visible_text("[ ](missing.md)").strip() == ""
    assert visible_text("<span></span>").strip() == ""
    assert visible_text("&amp; &nbsp;").strip() == "&"
    assert visible_text("`code`").strip() == "code"
    assert visible_text("![alt](image.png)").strip() == ""


def test_masking_blanks_code_blocks_and_comments_without_moving_lines() -> None:
    text = (
        "# Title\n"
        "\n"
        "```markdown\n"
        "| Area | Coverage | Evidence |\n"
        "```\n"
        "\n"
        "<!-- hidden\n"
        "still hidden\n"
        "-->\n"
        "\n"
        "    indented code\n"
        "\n"
        "visible tail\n"
    )

    lines = content_lines(text)

    assert len(lines) == len(text.splitlines())
    assert lines[0] == "# Title"
    assert [line for line in lines if line.strip()] == ["# Title", "visible tail"]
    # The tail keeps its own line number, which is what diagnostics report.
    assert lines.index("visible tail") == 12


def test_a_comment_opener_inside_a_fence_is_literal_text() -> None:
    text = "```\n<!--\n```\n\nvisible\n"

    assert [line for line in content_lines(text) if line.strip()] == ["visible"]


def test_a_fence_inside_a_comment_does_not_open_a_code_block() -> None:
    text = "<!--\n```\n-->\n\nvisible\n"

    assert [line for line in content_lines(text) if line.strip()] == ["visible"]


def test_text_beside_a_comment_on_one_line_survives() -> None:
    assert content_lines("| OUT | real <!-- aside --> reason |\n") == [
        "| OUT | real  reason |"
    ]


def test_indented_content_under_a_list_item_is_not_code() -> None:
    text = "- item\n\n    continuation of the item\n"

    assert [line.strip() for line in content_lines(text) if line.strip()] == [
        "- item",
        "continuation of the item",
    ]


def test_every_continuation_paragraph_in_a_list_stays_visible() -> None:
    # List context has to survive the blank lines between paragraphs, or the
    # second one is mistaken for a code block and its links go unchecked.
    text = "- item\n\n    first continuation\n\n    second continuation\n\ntail\n"

    assert [line.strip() for line in content_lines(text) if line.strip()] == [
        "- item",
        "first continuation",
        "second continuation",
        "tail",
    ]


def test_indented_code_at_the_start_of_a_document_is_masked() -> None:
    assert content_lines("    indented code\n\nvisible\n") == ["", "", "visible"]


def test_content_distinguishes_hidden_lines_from_blank_ones() -> None:
    view = content("visible\n\n<!-- hidden -->\n")

    assert view.lines == ("visible", "", "")
    assert view.hidden == frozenset({2})
    assert not view.is_hidden(1)
    # Line 1 is a blank the author typed and ends a block; line 2 only looks
    # blank because a comment covers it.
    assert view.ends_block(1)
    assert not view.ends_block(2)


def test_blank_lines_inside_a_comment_belong_to_the_comment() -> None:
    view = content("visible\n<!-- note\n\nmore note -->\nafter\n")

    assert view.hidden == frozenset({1, 2, 3})
    assert not view.ends_block(2)


def test_blank_lines_inside_a_fence_belong_to_the_fence() -> None:
    view = content("visible\n```\n\nexample\n```\nafter\n")

    assert view.hidden == frozenset({1, 2, 3, 4})
    assert not view.ends_block(2)


def test_a_closing_fence_may_not_carry_trailing_text() -> None:
    # pymdownx.superfences keeps this inside the code block, so anything after
    # it is still fenced and must stay masked.
    view = content("```\nfenced\n``` trailing\nstill fenced\n")

    assert [line for line in view.lines if line.strip()] == []


def test_a_bare_closing_fence_ends_the_block() -> None:
    view = content("```\nfenced\n```\npublished\n")

    assert [line for line in view.lines if line.strip()] == ["published"]


def test_link_extraction_requires_a_closing_parenthesis() -> None:
    assert link_targets("[Node](../roadmap/node.md)") == ("../roadmap/node.md",)
    assert link_targets("[Node](<../roadmap/a b.md>)") == ("../roadmap/a b.md",)
    assert link_targets("[Node](../roadmap/node.md") == ()
    assert link_targets("`[Node](../roadmap/node.md)`") == ()
    assert link_targets("![Figure](../image.png)") == ()


def test_markdown_links_report_visible_links_with_line_numbers() -> None:
    text = "# Title\n\n[One](a.md)\n\n```\n[Two](b.md)\n```\n\n[Three](c.md)\n"

    assert markdown_links(text) == [(3, "a.md"), (9, "c.md")]


def test_anchors_follow_headings_and_explicit_ids(tmp_path: Path) -> None:
    path = tmp_path / "article.md"
    path.write_text(
        "# Roadmap article\n\n## Depends on\n\n## Depends on\n\n## Result {#custom-id}\n",
        encoding="utf-8",
    )

    assert markdown_anchors(path) == {
        "roadmap-article",
        "depends-on",
        "depends-on_1",
        "custom-id",
    }


def test_local_targets_are_resolved_against_the_boundary(tmp_path: Path) -> None:
    article = tmp_path / "roadmap" / "node.md"
    article.parent.mkdir(parents=True)
    article.write_text("# Node\n\n## Results\n", encoding="utf-8")
    source = tmp_path / "coverage" / "README.md"
    source.parent.mkdir()
    source.write_text("# Coverage\n", encoding="utf-8")

    def issue(target: str) -> str | None:
        problem = local_target_issue(source, target, tmp_path, label="coverage")
        return None if problem is None else problem[0]

    assert issue("../roadmap/node.md") is None
    assert issue("../roadmap/node.md#results") is None
    assert issue("https://example.invalid/page") is None
    assert issue("../roadmap/node.md#absent") == "coverage-anchor-not-found"
    assert issue("../roadmap/absent.md") == "coverage-not-found"
    assert issue("../../outside.md") == "coverage-escapes-blueprint"
    assert issue("mailto:someone@example.invalid") == "unsupported-coverage-link"
    assert issue("//example.invalid/page") == "unsupported-coverage-link"
