"""Parse the machine-checkable source coverage contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

COVERAGE_SCHEMA = "autoform-coverage/v1"
COVERAGE_DISPOSITIONS = ("MAPPED", "DECOMPOSED", "DEFERRED", "OUT")
_EXPECTED_HEADER = ("Area", "Coverage", "Evidence")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[[^\]]+\]\(\s*(?P<target><[^>\r\n]+>|[^)\s]+)"
)
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
    """Canonical coverage rows, counts, and source binding."""

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
    lines = text.splitlines()
    tables: list[tuple[int, tuple[str, ...]]] = []
    fenced = _fenced_lines(lines)
    for index in range(len(lines) - 1):
        if index in fenced or index + 1 in fenced:
            continue
        header = _cells(lines[index])
        separator = _cells(lines[index + 1])
        if header == _EXPECTED_HEADER and len(separator) == 3 and all(
            _SEPARATOR.fullmatch(cell) for cell in separator
        ):
            tables.append((index, header))

    if not tables:
        return [], [CoverageIssue(0, "coverage contract has no 'Area | Coverage | Evidence' table")]
    if len(tables) > 1:
        return [], [CoverageIssue(tables[1][0] + 1, "coverage contract has multiple coverage tables")]

    header_index, _header = tables[0]
    entries: list[CoverageEntry] = []
    issues: list[CoverageIssue] = []
    seen_areas: dict[str, int] = {}
    for index in range(header_index + 2, len(lines)):
        raw = lines[index]
        if index in fenced or not raw.strip():
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
        normalized_evidence = re.sub(r"[*_~`]", "", visible_evidence).strip(" \t\r\n.!?:;").casefold()
        if normalized_evidence in _PLACEHOLDER_EVIDENCE:
            issues.append(CoverageIssue(entry.line, "coverage evidence is a placeholder"))
            continue
        if entry.disposition != "DECOMPOSED":
            continue

        targets = tuple(match.group("target") for match in _MARKDOWN_LINK.finditer(visible_evidence))
        if not targets:
            issues.append(
                CoverageIssue(
                    entry.line,
                    "DECOMPOSED coverage evidence must link to at least one roadmap article",
                )
            )
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
    without_comments = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    return re.sub(r"(`+)[^`]*?\1", "", without_comments)


def _is_roadmap_article(target: str, *, coverage_path: Path, roadmap: Path) -> bool:
    normalized = target[1:-1] if target.startswith("<") and target.endswith(">") else target
    parsed = urlsplit(normalized)
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


def _fenced_lines(lines: list[str]) -> set[int]:
    fenced: set[int] = set()
    marker: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match is not None:
            token = match.group(1)
            fenced.add(index)
            if marker is None:
                marker = (token[0], len(token))
            elif token[0] == marker[0] and len(token) >= marker[1]:
                marker = None
            continue
        if marker is not None:
            fenced.add(index)
    return fenced


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
