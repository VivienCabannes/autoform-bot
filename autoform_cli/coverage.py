"""Parse the machine-checkable source coverage contract.

The contract is one Markdown table in ``coverage/README.md``. Because it is the
only place a project states what its roadmap is supposed to cover, every rule
here is written to fail closed: anything that does not visibly render, and any
evidence that says nothing, is rejected rather than quietly accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .markdown import (
    INLINE_CODE,
    Content,
    content,
    link_targets,
    local_target_issue,
    site_converter,
    visible_text,
)

COVERAGE_SCHEMA = "autoform-coverage/v1"
COVERAGE_DISPOSITIONS = ("MAPPED", "DECOMPOSED", "DEFERRED", "OUT")
_EXPECTED_HEADER = ("Area", "Coverage", "Evidence")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
#: Words that name the absence of a decision.
_PLACEHOLDER_EVIDENCE = frozenset({"pending", "placeholder", "todo", "tbd", "unknown"})
#: Punctuation that turns a leading placeholder into a marker, as in ``TODO:``.
_MARKER_PUNCTUATION = re.compile(r"^[\s]*[:\-\u2013\u2014]")


@dataclass(frozen=True, order=True, slots=True)
class CoverageIssue:
    """One structural problem in a coverage contract."""

    line: int
    reason: str


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """One source area and its explicit roadmap disposition."""

    area: str
    disposition: str
    evidence: str
    line: int

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoverageSummary:
    """Canonical coverage rows, counts, and source binding.

    The summary describes what the author *declared*. It is deliberately not a
    measurement of the source tree: nothing here reads the Lean project or
    counts proved declarations.
    """

    schema: str
    source_path: str
    source_sha256: str
    entries: tuple[CoverageEntry, ...]

    @property
    def counts(self) -> dict[str, int]:
        totals = Counter(entry.disposition for entry in self.entries)
        return {disposition: totals[disposition] for disposition in COVERAGE_DISPOSITIONS}

    @property
    def complete(self) -> bool:
        """Whether every author-declared row reached a terminal disposition.

        Terminal means the row is no longer ``MAPPED`` -- the author has either
        decomposed it into roadmap articles, deferred it to a named milestone,
        or excluded it with a reason.

        This is a claim about the *contract*, not about the project. It does not
        establish that the declared rows cover the source exhaustively, and it
        says nothing about whether the linked roadmap articles are formalized or
        proved. A project that declares one narrow area and disposes of it
        reports ``complete`` while most of its source remains undeclared.
        """

        return bool(self.entries) and not self.counts["MAPPED"]

    def as_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "counts": self.counts,
            "entries": [entry.as_dict() for entry in self.entries],
            "schema": self.schema,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def load_coverage(blueprint_dir: str | Path) -> tuple[CoverageSummary | None, tuple[CoverageIssue, ...]]:
    """Read and validate ``coverage/README.md`` without modifying it."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    path = blueprint / "coverage" / "README.md"
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except FileNotFoundError:
        return None, (CoverageIssue(0, "coverage contract is missing"),)
    except UnicodeError:
        return None, (CoverageIssue(0, "coverage contract cannot be read as UTF-8"),)
    except OSError:
        return None, (CoverageIssue(0, "coverage contract cannot be read"),)

    rows, issues = _parse_table(text)
    issues.extend(_validate_evidence(rows, blueprint=blueprint, coverage_path=path))
    if issues:
        return None, tuple(issues)
    return (
        CoverageSummary(
            schema=COVERAGE_SCHEMA,
            source_path="coverage/README.md",
            source_sha256=hashlib.sha256(content).hexdigest(),
            entries=tuple(rows),
        ),
        (),
    )


def _parse_table(text: str) -> tuple[list[CoverageEntry], list[CoverageIssue]]:
    # Only published Markdown can carry the contract. Commented-out and
    # code-block tables are masked to blank lines first, which keeps every
    # surviving index aligned with the author's own line numbering.
    view = content(text)
    lines = view.lines
    source_lines = text.splitlines()
    header_indexes: list[int] = []
    layout_issues: list[CoverageIssue] = []
    for index in range(len(lines) - 1):
        if view.is_hidden(index) or view.is_hidden(index + 1):
            # The table is commented out or fenced, so it publishes nothing.
            continue
        if _cells(lines[index]) != _EXPECTED_HEADER:
            continue
        separator = _cells(lines[index + 1])
        if len(separator) != 3 or not all(_SEPARATOR.fullmatch(cell) for cell in separator):
            continue
        # Masking shows what the cells *say*; only the renderer knows whether
        # these two lines make a table at all. The `tables` extension decides
        # that from the source, before inline processing removes comments, so a
        # comment can leave the column count intact and still break the
        # delimiter row -- and then the page shows a paragraph, not a contract.
        if not _renders_as_table(source_lines[index], source_lines[index + 1]):
            layout_issues.append(
                CoverageIssue(
                    index + 1,
                    "coverage table header and separator do not render as a table; "
                    "keep HTML comments out of them",
                )
            )
            continue
        header_indexes.append(index)

    if not header_indexes:
        if layout_issues:
            return [], layout_issues
        return [], [CoverageIssue(0, "coverage contract has no 'Area | Coverage | Evidence' table")]
    if len(header_indexes) > 1:
        return [], [CoverageIssue(header_indexes[1] + 1, "coverage contract has multiple coverage tables")]

    header_index = header_indexes[0]
    entries: list[CoverageEntry] = []
    issues: list[CoverageIssue] = list(layout_issues)
    seen_areas: dict[str, int] = {}
    for index in range(header_index + 2, len(lines)):
        raw = lines[index]
        if view.ends_block(index):
            # A blank line the author typed ends the table. Hidden content also
            # ends it, for every renderer as well as for us, so any row written
            # below it is published by nobody and must be reported.
            break
        if not raw.strip():
            issues.extend(_unpublished_row_issues(view, index))
            break
        cells = _cells(raw)
        line_number = index + 1
        # A renderer splits cells before it strips comments, so a comment that
        # contains a pipe changes the column layout a reader sees. Reject the
        # disagreement rather than parse a different table than the one shown.
        if len(_cells(source_lines[index])) != len(cells):
            issues.append(
                CoverageIssue(
                    line_number,
                    "an HTML comment changes this coverage row's column layout",
                )
            )
            continue
        if len(cells) != 3:
            issues.append(CoverageIssue(line_number, "coverage row must have exactly three columns"))
            continue
        area, disposition_text, evidence = cells
        disposition = _inline_code(disposition_text).upper()
        if not area:
            issues.append(CoverageIssue(line_number, "coverage area is empty"))
        if disposition not in COVERAGE_DISPOSITIONS:
            allowed = ", ".join(COVERAGE_DISPOSITIONS)
            issues.append(
                CoverageIssue(line_number, f"unknown coverage disposition {disposition_text!r}; expected {allowed}")
            )
        if not evidence:
            issues.append(CoverageIssue(line_number, "coverage evidence is empty"))
        normalized_area = area.casefold()
        if normalized_area in seen_areas:
            issues.append(
                CoverageIssue(
                    line_number,
                    f"duplicate coverage area {area!r}; first declared at line {seen_areas[normalized_area]}",
                )
            )
        else:
            seen_areas[normalized_area] = line_number
        if area and disposition in COVERAGE_DISPOSITIONS and evidence:
            entries.append(CoverageEntry(area, disposition, evidence, line_number))

    if not entries and not issues:
        issues.append(CoverageIssue(header_index + 1, "coverage table has no rows"))
    return entries, issues


def _renders_as_table(header_line: str, separator_line: str) -> bool:
    """Whether the site's renderer turns these two lines into a table."""

    try:
        rendered = site_converter().convert(f"{header_line}\n{separator_line}\n")
    except Exception:
        return False
    return "<table>" in rendered


def _looks_like_row(line: str) -> bool:
    """Whether ``line`` is the shape of a table row the renderer would accept.

    Deliberately looser than :func:`_cells`, which demands both outer pipes.
    Python-Markdown accepts ``A | OUT | reason`` and ``| A | OUT | reason`` as
    rows just as readily, so a stranded row written either way has to be
    reported rather than passed over for failing the canonical form.
    """

    bare = INLINE_CODE.sub("", line).strip()
    if not bare:
        return False
    return bare.startswith("|") or bare.count("|") >= 2


def _unpublished_row_issues(view: Content, start: int) -> list[CoverageIssue]:
    """Report rows written after hidden content inside the table body.

    The table has already ended at ``start``. Anything shaped like a table row
    between there and the next blank line the author actually typed looks like a
    declaration they expected to count, so name each one rather than let the
    contract shrink in silence. Row shape is judged loosely, by
    :func:`_looks_like_row`: a row with the wrong column count, or written
    without its outer pipes, is still a row somebody meant to declare.
    Trailing notes with no rows after them are left alone.
    """

    issues: list[CoverageIssue] = []
    for index in range(start, len(view.lines)):
        if view.ends_block(index):
            break
        if _looks_like_row(view.lines[index]):
            issues.append(
                CoverageIssue(
                    index + 1,
                    "coverage row follows hidden content and would not be published; "
                    "move the comment or code block below the table",
                )
            )
    return issues


def _validate_evidence(
    entries: list[CoverageEntry],
    *,
    blueprint: Path,
    coverage_path: Path,
) -> list[CoverageIssue]:
    issues: list[CoverageIssue] = []
    roadmap = (blueprint / "roadmap").resolve()
    for entry in entries:
        visible_evidence = _visible_markdown(entry.evidence)
        if not _has_substance(visible_evidence):
            issues.append(CoverageIssue(entry.line, "coverage evidence has no substantive content"))
            continue
        if _is_placeholder(visible_evidence):
            issues.append(CoverageIssue(entry.line, "coverage evidence is a placeholder"))
            continue
        if entry.disposition != "DECOMPOSED":
            continue

        targets = link_targets(entry.evidence)
        if not targets:
            issues.append(
                CoverageIssue(
                    entry.line,
                    "DECOMPOSED coverage evidence must link to at least one roadmap article",
                )
            )
            continue
        # Every link offered as proof of decomposition must resolve, using the
        # audit's rules so `render` cannot publish evidence the audit rejects.
        # One good link beside a broken one is a broken claim.
        broken: list[str] = []
        for target in targets:
            problem = local_target_issue(coverage_path, target, blueprint, label="coverage")
            if problem is not None:
                broken.append(problem[1])
        if broken:
            issues.extend(CoverageIssue(entry.line, reason) for reason in broken)
            continue
        if not any(_is_roadmap_article(target, coverage_path=coverage_path, roadmap=roadmap) for target in targets):
            issues.append(
                CoverageIssue(
                    entry.line,
                    "DECOMPOSED coverage evidence has no link to an existing roadmap article",
                )
            )
    return issues


def _visible_markdown(value: str) -> str:
    """Return the text of ``value`` a reader would actually see as evidence.

    Inline code is removed outright rather than unwrapped: it is illustration
    rather than justification, so a cell whose only content is a code span
    states no reason at all. Everything else is reduced the way a renderer
    reduces it, which matters because a URL, an HTML tag, and an entity all
    carry word characters while showing the reader nothing. ``[ ](missing.md)``
    publishes an empty link, not evidence.
    """

    return visible_text(INLINE_CODE.sub("", value))


def _has_substance(visible: str) -> bool:
    """Whether anything a reader could act on survives emphasis and punctuation."""

    return bool(re.search(r"\w", re.sub(r"[*_~\\]", "", visible)))


def _is_placeholder(visible: str) -> bool:
    """Whether the evidence only announces that a decision is still outstanding.

    Two shapes are rejected. A cell whose every word is a placeholder, however
    decorated -- ``TBD``, ``**TODO.**`` -- and a cell that opens with one used as
    a marker, where punctuation separates it from the rest: ``TODO: choose a
    milestone``.

    A status word that merely begins a sentence is left alone, because it is
    usually carrying real information: "Pending Mathlib PR 1234" and "Unknown
    provenance, excluded by agreement" both name something a reader can check.
    Rejecting those pushed authors toward vaguer wording to satisfy the checker.

    The gap this leaves is a marker written without punctuation, as in "TODO
    choose a milestone". That reads as prose to any rule cheap enough to trust,
    so it is left to human review rather than guessed at.
    """

    stripped = re.sub(r"[*_~\\]", "", visible)
    words = re.findall(r"\w+", stripped.casefold())
    if not words:
        return False
    if all(word in _PLACEHOLDER_EVIDENCE for word in words):
        return True
    if words[0] not in _PLACEHOLDER_EVIDENCE:
        return False
    _, _, remainder = stripped.casefold().partition(words[0])
    return _MARKER_PUNCTUATION.match(remainder) is not None


def _is_roadmap_article(target: str, *, coverage_path: Path, roadmap: Path) -> bool:
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return False
    try:
        raw_path = unquote(parsed.path)
        if not raw_path or "\x00" in raw_path:
            return False
        candidate = (coverage_path.parent / raw_path).resolve()
        candidate.relative_to(roadmap)
        return candidate.is_file() and candidate.suffix.casefold() == ".md"
    except (OSError, RuntimeError, ValueError):
        return False


def _cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in stripped[1:-1]:
        if escaped:
            current.append(character if character == "|" else f"\\{character}")
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return tuple(cells)


def _inline_code(value: str) -> str:
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        return value[1:-1].strip()
    return value.strip()


__all__ = [
    "COVERAGE_DISPOSITIONS",
    "COVERAGE_SCHEMA",
    "CoverageEntry",
    "CoverageIssue",
    "CoverageSummary",
    "load_coverage",
]
