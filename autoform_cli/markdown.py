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

Where a rule has to predict what a reader sees, it follows the configured
renderer rather than an approximation of it. Anchor generation in particular
reproduces Python-Markdown's ``toc`` slugging and unique-ID behaviour, because a
checker that guesses at anchors both rejects valid fragments and accepts
fragments that never appear on the published page. ``tests/test_markdown.py``
holds a differential test that compares this module against Python-Markdown
itself, so the two cannot drift apart unnoticed.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

#: Link schemes that are resolved by the reader's browser, not by this checker.
EXTERNAL_SCHEMES = frozenset({"http", "https"})

HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+).*?\1")
HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)

#: A closing fence carries nothing but its marker. ``pymdownx.superfences`` keeps
#: ````` trailing`` inside the code block, so treating it as a closer would expose
#: content that is still fenced when the page renders.
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

#: A complete inline link. The closing parenthesis is required: a target such as
#: ``[Node](../roadmap/node.md`` renders as literal text, so accepting it would
#: let unrendered evidence satisfy a coverage disposition.
LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")

#: The trailing attr-list block and an explicit `#id` within it. MkDocs allows
#: the ID alongside classes and attributes, for example `{#result .highlight}`.
ATTR_LIST = re.compile(r"\{(?P<attributes>[^{}]*)\}\s*$")
ATTR_LIST_ID = re.compile(r"(?:^|\s)#(?P<id>[^\s#.={}]+)(?=\s|$)")

#: A setext underline. Python-Markdown gives these headings IDs too, so ignoring
#: them would leave valid fragments unresolvable.
SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")

#: An unordered or ordered list marker. Content indented under a list item is a
#: continuation of that item, not an indented code block.
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)")

#: CommonMark's indentation threshold for an indented code block.
_CODE_INDENT = 4

#: Inline constructs, in the order they have to be reduced to visible text.
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_AUTOLINK = re.compile(r"<((?:https?|ftp|mailto):[^>\s]+)>")
_INLINE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REFERENCE_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_CODE_SPAN = re.compile(r"(`+)(.*?)\1", re.DOTALL)
_HTML_TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][^>]*>", re.DOTALL)
_EMPHASIS = re.compile(r"[*_~]")
_BACKSLASH_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

#: Python-Markdown appends ``_1``, ``_2`` and so on to make an ID unique.
_ID_COUNT = re.compile(r"^(.*_)(\d+)$")


@dataclass(frozen=True, slots=True)
class Content:
    """A line-preserving view of the publishable Markdown in a document.

    ``lines`` holds one entry per source line with unpublished spans blanked.
    ``hidden`` holds the indexes of lines that belong to an unpublished
    construct: a fenced block, an indented code block, or an HTML comment,
    *including the blank lines inside them*.

    That last detail is what makes the view safe to scan. A caller that ends a
    construct at a blank line -- a table body, say -- needs to distinguish a
    blank the author typed from a blank that merely sits inside a comment. Treat
    them alike and a comment containing an empty line silently swallows every
    row beneath it.
    """

    lines: tuple[str, ...]
    hidden: frozenset[int]

    def is_hidden(self, index: int) -> bool:
        """Whether line ``index`` belongs to an unpublished construct."""

        return index in self.hidden

    def ends_block(self, index: int) -> bool:
        """Whether line ``index`` is a blank line the author actually typed.

        Only these end a table or a paragraph. A blank line inside a comment or
        a fence is part of that construct and carries on through it.
        """

        return not self.lines[index].strip() and index not in self.hidden


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


def content(text: str) -> Content:
    """Return the publishable view of ``text``, recording what is hidden.

    Fenced code blocks, indented code blocks, and HTML comments are blanked.
    The result always has exactly as many lines as ``text``, so index ``i``
    still describes line ``i + 1`` of the source document.
    """

    lines = text.splitlines()
    hidden: set[int] = set()
    masked = _mask_fences_and_comments(lines, hidden)
    masked = _mask_indented_code(masked, hidden)
    return Content(tuple(masked), frozenset(hidden))


def content_lines(text: str) -> list[str]:
    """Return ``text`` line by line with everything unpublished masked out."""

    return list(content(text).lines)


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


def visible_text(value: str) -> str:
    """Reduce inline Markdown to the text a reader actually sees.

    Link labels replace their destinations, code spans become their contents,
    HTML tags are dropped, and entities are decoded. A URL is not prose and a
    tag is not evidence, so anything deciding whether a fragment of Markdown
    *says* something has to look at this rather than at the source.
    """

    text = _IMAGE.sub("", value)
    text = _AUTOLINK.sub(r"\1", text)
    text = _INLINE_LINK.sub(r"\1", text)
    text = _REFERENCE_LINK.sub(r"\1", text)
    text = _CODE_SPAN.sub(lambda match: match.group(2), text)
    text = _HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = _EMPHASIS.sub("", text)
    return _BACKSLASH_ESCAPE.sub(r"\1", text)


def frontmatter_end(lines: list[str]) -> int:
    """Return the index of the first line after any YAML frontmatter block."""

    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return len(lines)


def markdown_anchors(path: Path) -> set[str]:
    """Return the heading anchors MkDocs will publish for ``path``.

    This mirrors Python-Markdown's ``toc`` extension, which the generated
    ``mkdocs.yml`` configures: the heading's *rendered* text is folded to ASCII
    and slugged, an ``attr_list`` block supplies an explicit ID when it carries
    one, and a collision gains a ``_1`` suffix. Reproducing the renderer matters
    in both directions, since an approximation rejects fragments that do resolve
    and accepts fragments that never appear on the page.
    """

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    lines = list(content(text).lines)
    # MkDocs strips YAML frontmatter before Markdown ever sees it, so those
    # lines cannot contribute headings.
    body = lines[frontmatter_end(lines) :]
    # Python-Markdown reserves every explicit ID in the document before it slugs
    # a single heading. That ordering is observable: given `# Depends on` above
    # `# B {#depends-on}`, the explicit one keeps `depends-on` and the heading
    # above it becomes `depends-on_1`.
    published = {found for found in (_explicit_id(line) for line in body) if found}
    for title in _heading_titles(body):
        explicit, heading = _split_attr_list(title)
        if explicit is not None:
            # Already reserved above, and emitted verbatim: an explicit ID is
            # never uniquified, so duplicates render as duplicates.
            continue
        _add_unique(_slug(visible_text(heading)), published)
    return published


def _explicit_id(line: str) -> str | None:
    attributes = ATTR_LIST.search(line)
    if attributes is None:
        return None
    explicit = ATTR_LIST_ID.search(attributes.group("attributes"))
    return explicit.group("id") if explicit is not None else None


def _heading_titles(lines: list[str]) -> list[str]:
    titles: list[str] = []
    for index, line in enumerate(lines):
        atx = HEADING.match(line)
        if atx is not None:
            titles.append(atx.group(2).strip())
            continue
        # A setext underline turns the paragraph line above it into a heading.
        # Table rows and existing headings cannot be underlined this way.
        if index == 0 or SETEXT.match(line) is None:
            continue
        previous = lines[index - 1].strip()
        if previous and not previous.startswith("|") and HEADING.match(previous) is None:
            titles.append(previous)
    return titles


def _split_attr_list(title: str) -> tuple[str | None, str]:
    attributes = ATTR_LIST.search(title)
    if attributes is None:
        return None, title
    # The block is consumed by `attr_list` whether or not it carries an ID, so it
    # never reaches the slug. Leaving it in produced anchors such as
    # `title-class`.
    return _explicit_id(title), title[: attributes.start()].strip()


def _slug(text: str) -> str:
    """Slug ``text`` the way Python-Markdown's default ``slugify`` does."""

    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    stripped = re.sub(r"[^\w\s-]", "", folded).strip().lower()
    return re.sub(r"[-\s]+", "-", stripped)


def _add_unique(candidate: str, published: set[str]) -> None:
    """Record ``candidate``, resolving collisions as Python-Markdown does."""

    while candidate in published or not candidate:
        match = _ID_COUNT.match(candidate)
        candidate = (
            f"{match.group(1)}{int(match.group(2)) + 1}" if match is not None else f"{candidate}_1"
        )
    published.add(candidate)


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


def _mask_fences_and_comments(lines: list[str], hidden: set[int]) -> list[str]:
    masked: list[str] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for index, raw in enumerate(lines):
        if fence is not None:
            # A fence closes on the raw line: `<!--` inside a code block is
            # literal text, not the start of a comment. The closing delimiter
            # belongs to the block, so it is hidden along with the body.
            match = FENCE_CLOSE.match(raw)
            if match is not None:
                marker = match.group(1)
                if marker[0] == fence[0] and len(marker) >= fence[1]:
                    fence = None
            hidden.add(index)
            masked.append("")
            continue
        opened_in_comment = in_comment
        line, in_comment = strip_line_comments(raw, in_comment)
        match = FENCE.match(line)
        if match is not None:
            marker = match.group(1)
            fence = (marker[0], len(marker))
            hidden.add(index)
            masked.append("")
            continue
        # A line shows nothing either because the author left it empty or
        # because a comment covers it. Only the second belongs to a construct.
        if not line.strip() and (opened_in_comment or raw.strip()):
            hidden.add(index)
        masked.append(line)
    return masked


def _mask_indented_code(lines: list[str], hidden: set[int]) -> list[str]:
    masked = list(lines)
    in_list = False
    index = 0
    while index < len(masked):
        line = masked[index]
        if not line.strip():
            index += 1
            continue
        if _indent(line) < _CODE_INDENT:
            # This line sets the context every following indented line is read
            # against, and it stays set across the blank lines that separate a
            # list item from its continuation paragraphs.
            in_list = _LIST_ITEM.match(line) is not None
            index += 1
            continue
        # Indented content is a code block only outside a list, and only where
        # it does not continue the paragraph directly above it.
        if in_list or (index > 0 and masked[index - 1].strip()):
            index += 1
            continue
        end = index
        while end < len(masked) and (
            not masked[end].strip() or _indent(masked[end]) >= _CODE_INDENT
        ):
            end += 1
        # Blank lines trailing the block separate it from whatever follows, so
        # they end a table or paragraph as any other blank line does.
        body = end
        while body > index and not masked[body - 1].strip():
            body -= 1
        for inside in range(index, body):
            hidden.add(inside)
            masked[inside] = ""
        index = end
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
    "ATTR_LIST",
    "ATTR_LIST_ID",
    "EXTERNAL_SCHEMES",
    "FENCE",
    "FENCE_CLOSE",
    "HEADING",
    "HTML_COMMENT",
    "INLINE_CODE",
    "LINK",
    "SETEXT",
    "Content",
    "content",
    "content_lines",
    "frontmatter_end",
    "link_targets",
    "local_target_issue",
    "markdown_anchors",
    "markdown_links",
    "strip_line_comments",
    "visible_text",
]
