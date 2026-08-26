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

from .markdown import INLINE_CODE, content_lines, link_targets, local_target_issue

COVERAGE_SCHEMA = "autoform-coverage/v1"
COVERAGE_DISPOSITIONS = ("MAPPED", "DECOMPOSED", "DEFERRED", "OUT")
_EXPECTED_HEADER = ("Area", "Coverage", "Evidence")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
#: Words that name the absence of a decision. Evidence that opens with one of
#: these is a promise to decide later, not a disposition.
_PLACEHOLDER_EVIDENCE = frozenset({"pending", "placeholder", "todo", "tbd", "unknown"})


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
    lines = content_lines(text)
    header_indexes: list[int] = []
    for index in range(len(lines) - 1):
        header = _cells(lines[index])
        separator = _cells(lines[index + 1])
        if header == _EXPECTED_HEADER and len(separator) == 3 and all(
            _SEPARATOR.fullmatch(cell) for cell in separator
        ):
            header_indexes.append(index)

    if not header_indexes:
        return [], [CoverageIssue(0, "coverage contract has no 'Area | Coverage | Evidence' table")]
    if len(header_indexes) > 1:
        return [], [CoverageIssue(header_indexes[1] + 1, "coverage contract has multiple coverage tables")]

    header_index = header_indexes[0]
    entries: list[CoverageEntry] = []
    issues: list[CoverageIssue] = []
    seen_areas: dict[str, int] = {}
    for index in range(header_index + 2, len(lines)):
        raw = lines[index]
        if not raw.strip():
            break
        cells = _cells(raw)
        line_number = index + 1
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
    """Return evidence with inline code removed.

    HTML comments are already masked before the table is parsed. Inline code is
    illustration rather than justification, so a cell whose only content is a
    code span states no reason at all.
    """

    return INLINE_CODE.sub("", value)


def _has_substance(visible: str) -> bool:
    """Whether anything a reader could act on survives emphasis and punctuation."""

    return bool(re.search(r"\w", re.sub(r"[*_~\\]", "", visible)))


def _is_placeholder(visible: str) -> bool:
    """Whether the evidence only announces that a decision is still outstanding.

    A cell is rejected when its first word is a placeholder, however decorated:
    ``TBD``, ``**TODO.**``, and ``TODO: choose a milestone`` all lead with a
    marker that declares the row unfinished, so nothing after it is a reason.

    A placeholder word later in the sentence is deliberately allowed. Evidence
    such as "Listed in the roadmap; source audit pending" tells a reader
    something they can check, and rejecting it would fail closed on ordinary
    authoring -- the bundled thesis example writes exactly that.
    """

    words = re.findall(r"\w+", re.sub(r"[*_~\\]", "", visible).casefold())
    return bool(words) and words[0] in _PLACEHOLDER_EVIDENCE


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
