"""Shared Markdown primitives for the deterministic blueprint checks.

The audit, the coverage contract, and the renderer must agree on what counts as
published Markdown. When each one carried its own regular expressions they
disagreed in ways that failed open: a table hidden inside an HTML comment was
treated as authoritative, and a link missing its closing parenthesis satisfied a
check even though it never renders as a link. This module is the single place
those rules live.

Two ideas run through everything here:

* Only *visible* Markdown carries meaning. Fenced code blocks, indented code
  blocks, and HTML comments are documentation about a contract, never the
  contract itself.
* Line numbers are part of the diagnostic. Masking never changes how many lines
  a document has, so a caller can always report the author's own line number.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

#: Link schemes that are resolved by the reader's browser, not by this checker.
EXTERNAL_SCHEMES = frozenset({"http", "https"})

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+).*?\1")
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)

#: A complete inline link. The closing parenthesis is required: a target such as
#: ``[Node](../roadmap/node.md`` renders as literal text, so accepting it would
#: let unrendered evidence satisfy a coverage disposition.
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")

ANCHOR_PUNCTUATION = re.compile(r"[^\w\- ]", re.UNICODE)
#: The trailing attr-list block and an explicit `#id` within it. MkDocs allows
#: the ID alongside classes and attributes, for example `{#result .highlight}`.
ATTR_LIST = re.compile(r"\{(?P<attributes>[^{}]*)\}\s*$")
ATTR_LIST_ID = re.compile(r"(?:^|\s)#(?P<id>[^\s#.={}]+)(?=\s|$)")

#: An unordered or ordered list marker. Content indented under a list item is a
#: continuation of that item, not an indented code block.
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")

#: CommonMark's indentation threshold for an indented code block.
_CODE_INDENT = 4


def strip_line_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Remove HTML comment spans from one line, carrying state across lines.

    Returns the visible remainder of ``line`` and whether a comment is still
    open when the line ends. Text on the same line as a comment's start or end
    is preserved, so ``| OUT | real <!-- aside --> |`` keeps its cell layout.
    """

    output: list[str] = []
    index = 0
    while index < len(line):
        if in_comment:
            end = line.find("-->", index)
            if end < 0:
                return "".join(output), True
            index = end + 3
            in_comment = False
            continue
        start = line.find("<!--", index)
        if start < 0:
            output.append(line[index:])
            break
        output.append(line[index:start])
        index = start + 4
        in_comment = True
    return "".join(output), in_comment


def content_lines(text: str) -> list[str]:
    """Return ``text`` line by line with everything unpublished masked out.

    Fenced code blocks, indented code blocks, and HTML comments are blanked.
    The result always has exactly as many entries as ``text`` has lines, so
    index ``i`` still describes line ``i + 1`` of the source document.
    """

    return _mask_indented_code(_mask_fences_and_comments(text.splitlines()))


def link_targets(value: str) -> tuple[str, ...]:
    """Return the targets of every complete inline link in ``value``.

    Inline code is ignored, so a link shown as an example inside backticks does
    not count as a real reference. Angle-bracket targets are unwrapped.
    """

    return tuple(
        _unwrap_target(match.group(1)) for match in LINK.finditer(INLINE_CODE.sub("", value))
    )


def markdown_links(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, target)`` for every visible link in ``text``."""

    links: list[tuple[int, str]] = []
    for line_number, line in enumerate(content_lines(text), start=1):
        links.extend((line_number, target) for target in link_targets(line))
    return links


def markdown_anchors(path: Path) -> set[str]:
    """Return the heading anchors MkDocs will generate for ``path``."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()

    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in content_lines(text):
        heading = HEADING.match(line)
        if heading is None:
            continue
        title = heading.group(2).strip()
        # `attr_list` is enabled in the generated mkdocs.yml, so an explicit
        # `#id` in the trailing attribute block is what MkDocs renders and what
        # a link must match. The ID may appear beside classes or attributes.
        attributes = ATTR_LIST.search(title)
        explicit = (
            ATTR_LIST_ID.search(attributes.group("attributes")) if attributes is not None else None
        )
        if explicit is not None:
            anchors.add(explicit.group("id"))
            continue
        base = ANCHOR_PUNCTUATION.sub("", title.casefold())
        base = re.sub(r"\s+", "-", base).strip("-")
        if not base:
            continue
        index = counts.get(base, 0)
        counts[base] = index + 1
        anchors.add(base if index == 0 else f"{base}_{index}")
    return anchors


def local_target_issue(
    source_path: Path,
    target: str,
    boundary: Path,
    *,
    label: str,
) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when a local link does not resolve.

    ``source_path`` is the file containing the link, ``boundary`` the directory
    the link may not escape. External schemes are the reader's problem and are
    reported as fine. A fragment on a Markdown target must name a real heading.
    """

    split = urlsplit(target)
    scheme = split.scheme.casefold()
    if scheme in EXTERNAL_SCHEMES:
        return None
    if scheme:
        return f"unsupported-{label}-link", f"{label} link uses unsupported scheme: {target!r}"
    if split.netloc:
        return f"unsupported-{label}-link", f"{label} link uses a network location: {target!r}"

    raw_path = unquote(split.path)
    if "\x00" in raw_path:
        return f"malformed-{label}-link", f"{label} link contains an invalid path: {target!r}"
    if not raw_path:
        candidate = source_path.resolve()
    else:
        relative = Path(raw_path)
        if relative.is_absolute():
            return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
        candidate = (source_path.parent / relative).resolve()

    boundary = boundary.resolve()
    if not _is_within(candidate, boundary):
        return f"{label}-escapes-blueprint", f"{label} link escapes the blueprint: {target!r}"
    try:
        is_file = candidate.is_file()
    except (OSError, ValueError):
        return f"malformed-{label}-link", f"{label} link contains an invalid path: {target!r}"
    if not is_file:
        return f"{label}-not-found", f"{label} link does not resolve to a file: {target!r}"
    if split.fragment and candidate.suffix.casefold() == ".md":
        fragment = unquote(split.fragment)
        if fragment not in markdown_anchors(candidate):
            return f"{label}-anchor-not-found", f"{label} link fragment does not resolve: {target!r}"
    return None


def _mask_fences_and_comments(lines: list[str]) -> list[str]:
    masked: list[str] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for raw in lines:
        if fence is not None:
            # A fence closes on the raw line: `<!--` inside a code block is
            # literal text, not the start of a comment.
            match = FENCE.match(raw)
            if match is not None:
                marker = match.group(1)
                if marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
            masked.append("")
            continue
        line, in_comment = strip_line_comments(raw, in_comment)
        match = FENCE.match(line)
        if match is not None:
            marker = match.group(1)
            fence = (marker[0], len(marker))
            masked.append("")
            continue
        masked.append(line)
    return masked


def _mask_indented_code(lines: list[str]) -> list[str]:
    masked = list(lines)
    previous: str | None = None
    index = 0
    while index < len(masked):
        line = masked[index]
        if not line.strip():
            index += 1
            continue
        # An indented code block can only begin after a blank line, and never
        # inside a list, where the same indentation continues the list item.
        starts_block = (
            _indent(line) >= _CODE_INDENT
            and (index == 0 or not masked[index - 1].strip())
            and (previous is None or _LIST_ITEM.match(previous) is None)
        )
        if not starts_block:
            previous = line
            index += 1
            continue
        while index < len(masked) and (
            not masked[index].strip() or _indent(masked[index]) >= _CODE_INDENT
        ):
            masked[index] = ""
            index += 1
        previous = None
    return masked


def _indent(line: str) -> int:
    expanded = line.expandtabs(_CODE_INDENT)
    return len(expanded) - len(expanded.lstrip(" "))


def _unwrap_target(target: str) -> str:
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    return target


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


__all__ = [
    "ANCHOR_PUNCTUATION",
    "ATTR_LIST",
    "ATTR_LIST_ID",
    "EXTERNAL_SCHEMES",
    "FENCE",
    "HEADING",
    "HTML_COMMENT",
    "INLINE_CODE",
    "LINK",
    "content_lines",
    "link_targets",
    "local_target_issue",
    "markdown_anchors",
    "markdown_links",
    "strip_line_comments",
]
