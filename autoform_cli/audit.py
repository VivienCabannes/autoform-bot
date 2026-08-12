"""Deterministically audit a Markdown-native Autoform roadmap.

The audit is deliberately local and read-only. It derives its answer from the
blueprint Markdown and, when supplied, a local Lean source tree. It never
contacts a network service or writes generated state back into the blueprint.
"""

from __future__ import annotations

import json
import re
import statistics
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import status
from .graph import Graph, GraphValidationError, Node, load_graph
from .lean import SourceIndex, declaration_names, index_project

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
_INLINE_CODE = re.compile(r"(`+).*?\1")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_COVERAGE_GAP = re.compile(
    r"(?:"
    r"`(PARTIAL|PENDING|INCOMPLETE|TODO|TBD|GAP)`"
    r"|\|\s*(PARTIAL|PENDING|INCOMPLETE|TODO|TBD|GAP)\s*\|"
    r"|^\s*#{1,6}\s+(PARTIAL|PENDING|INCOMPLETE|TODO|TBD|GAP)\b"
    r"|^\s*(?:[-*+]\s+)?(?:status|coverage)\s*:\s*"
    r"(PARTIAL|PENDING|INCOMPLETE|TODO|TBD|GAP)\b"
    r"|^\s*[-*+]\s+(PARTIAL|PENDING|INCOMPLETE|TODO|TBD|GAP)\b"
    r")",
    re.I,
)
_EXTERNAL_SCHEMES = frozenset({"http", "https"})
_MARKDOWN_ANCHOR_PUNCTUATION = re.compile(r"[^\w\- ]", re.UNICODE)

#: More siblings than this at one level is a table of contents, not a chapter.
_MAX_DIRECT_CHILDREN = 24

#: A node is reported as oversized only once its finished Lean work clears both
#: an absolute floor and a large multiple of this project's own median. The
#: multiple self-calibrates -- a project whose nodes are routinely long is
#: measured against itself, and a project with too few finished nodes to have a
#: meaningful median cannot clear the multiple at all.
_NODE_SIZE_FLOOR = 200
_NODE_SIZE_MULTIPLE = 4

_DECLARATION_KEYWORDS = {
    "abbrev": frozenset({"abbrev"}),
    "axiom": frozenset({"axiom"}),
    "class": frozenset({"class"}),
    "corollary": frozenset({"lemma", "theorem"}),
    "def": frozenset({"def"}),
    "definition": frozenset({"def"}),
    "inductive": frozenset({"inductive"}),
    "instance": frozenset({"instance"}),
    "lemma": frozenset({"lemma", "theorem"}),
    "opaque": frozenset({"opaque"}),
    "proposition": frozenset({"lemma", "theorem"}),
    "structure": frozenset({"structure"}),
    "theorem": frozenset({"lemma", "theorem"}),
}


@dataclass(frozen=True, order=True, slots=True)
class AuditFinding:
    """One actionable roadmap problem at a stable blueprint-relative path."""

    article_path: str
    code: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The complete, canonically ordered result of one audit."""

    findings: tuple[AuditFinding, ...] = ()

    @property
    def clean(self) -> bool:
        """Whether the audit found no roadmap problems."""

        return not self.findings

    def as_dict(self) -> dict[str, object]:
        """Return a stable, machine-readable representation without host paths."""

        return {
            "clean": self.clean,
            "findings": [asdict(finding) for finding in self.findings],
        }

    def to_json(self) -> str:
        """Serialize the result canonically for snapshots and automation."""

        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def audit_blueprint(
    blueprint_dir: str | Path,
    *,
    lean_root: str | Path | None = None,
) -> AuditResult:
    """Audit *blueprint_dir* using only local, committed-style source files.

    Graph syntax errors are returned as structured findings rather than raised.
    Semantic checks run only after :func:`load_graph` has produced a valid graph.
    """

    blueprint = Path(blueprint_dir).expanduser().resolve()
    try:
        graph = load_graph(blueprint)
    except GraphValidationError as error:
        findings = [
            AuditFinding(
                _validation_article_path(blueprint, issue),
                "invalid-graph",
                _stable_validation_reason(blueprint, issue),
            )
            for issue in error.issues
        ]
        return _result(findings)
    return audit_graph(graph, lean_root=lean_root)


def audit_graph(graph: Graph, *, lean_root: str | Path | None = None) -> AuditResult:
    """Audit an already loaded graph without modifying it or its source files."""

    findings: list[AuditFinding] = []
    derived = status.derive(graph)

    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        article_path = _relative_path(node.path, graph.blueprint_dir)
        children = graph.children(node_id)
        article = _read_article(node.path)

        if node.formalizable:
            if children:
                findings.append(
                    AuditFinding(
                        article_path,
                        "formalizable-container",
                        "formalizable article has contained articles; declaration-sized articles must be leaves",
                    )
                )
            if not article.statement_text:
                findings.append(
                    AuditFinding(
                        article_path,
                        "missing-statement-text",
                        "formalizable article has no statement text between its H1 and first H2 section",
                    )
                )
            if not article.has_depends_section:
                findings.append(
                    AuditFinding(
                        article_path,
                        "missing-depends-section",
                        "formalizable article has no explicit '## Depends on' section",
                    )
                )
            if node.origin == "cited" and not node.sources:
                findings.append(
                    AuditFinding(
                        article_path,
                        "missing-source-link",
                        "cited formalizable article has no link under '## Sources'",
                    )
                )

        if len(children) > _MAX_DIRECT_CHILDREN:
            findings.append(
                AuditFinding(
                    article_path,
                    "overfull-container",
                    f"article directly contains {len(children)} articles, more than the "
                    f"{_MAX_DIRECT_CHILDREN}-article limit; group them into chapters",
                )
            )

        if node.proof_formalized and not node.statement_formalized:
            findings.append(
                AuditFinding(
                    article_path,
                    "proof-without-statement",
                    "proof is marked formalized but the statement is not marked formalized",
                )
            )

        if node.mathlib and not declaration_names(node.mathlib_declaration or ""):
            findings.append(
                AuditFinding(
                    article_path,
                    "mathlib-without-declaration",
                    "mathlib is true but mathlib_declaration metadata is missing",
                )
            )

        formalization_evidence = (
            bool(node.lean)
            or node.statement_formalized
            or node.proof_formalized
            or node.mathlib
            or bool(node.mathlib_declaration)
            or bool(node.mathlib_file)
            or derived[node_id].proved
        )
        if not children and formalization_evidence and not node.declaration:
            findings.append(
                AuditFinding(
                    article_path,
                    "missing-declaration-intent",
                    "formalization-bearing leaf has no declaration intent metadata",
                )
            )

        findings.extend(_source_findings(graph, node, article_path))

    findings.extend(_coverage_findings(graph.blueprint_dir))
    if lean_root is not None:
        findings.extend(_lean_findings(graph, lean_root))
    return _result(findings)


@dataclass(frozen=True, slots=True)
class _ArticleShape:
    statement_text: bool
    has_depends_section: bool


def _read_article(path: Path) -> _ArticleShape:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return _ArticleShape(False, False)

    lines = text.splitlines()
    start = _frontmatter_end(lines)
    body = _HTML_COMMENT.sub("", "\n".join(lines[start:]))
    seen_h1 = False
    before_first_h2 = True
    statement_text = False
    has_depends_section = False
    fence: tuple[str, int] | None = None

    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip().casefold()
            if level == 1:
                seen_h1 = True
            elif level == 2:
                before_first_h2 = False
                if title == "depends on":
                    has_depends_section = True
            continue
        if seen_h1 and before_first_h2 and line.strip():
            statement_text = True

    return _ArticleShape(statement_text, has_depends_section)


def _frontmatter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return index + 1
    return len(lines)


def _source_findings(graph: Graph, node: Node, article_path: str) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for target in node.sources:
        issue = _local_target_issue(node.path, target, graph.blueprint_dir, label="source")
        if issue is not None:
            code, reason = issue
            findings.append(AuditFinding(article_path, code, reason))
    return findings


def _local_target_issue(
    source_path: Path,
    target: str,
    boundary: Path,
    *,
    label: str,
) -> tuple[str, str] | None:
    split = urlsplit(target)
    scheme = split.scheme.casefold()
    if scheme in _EXTERNAL_SCHEMES:
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
        if fragment not in _markdown_anchors(candidate):
            return f"{label}-anchor-not-found", f"{label} link fragment does not resolve: {target!r}"
    return None


def _markdown_anchors(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()
    counts: dict[str, int] = {}
    anchors: set[str] = set()
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING.match(line)
        if heading is None:
            continue
        base = _MARKDOWN_ANCHOR_PUNCTUATION.sub("", heading.group(2).strip().casefold())
        base = re.sub(r"\s+", "-", base).strip("-")
        if not base:
            continue
        index = counts.get(base, 0)
        counts[base] = index + 1
        anchors.add(base if index == 0 else f"{base}_{index}")
    return anchors


def _coverage_findings(blueprint: Path) -> list[AuditFinding]:
    coverage_root = blueprint / "coverage"
    contract = coverage_root / "README.md"
    if not contract.is_file():
        return [
            AuditFinding(
                "coverage/README.md",
                "missing-coverage-contract",
                "coverage contract is missing",
            )
        ]

    findings: list[AuditFinding] = []
    coverage_files = sorted(path for path in coverage_root.rglob("*.md") if path.is_file())
    for path in coverage_files:
        article_path = _relative_path(path, blueprint)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            findings.append(
                AuditFinding(article_path, "unreadable-coverage-file", "coverage file cannot be read as UTF-8")
            )
            continue

        if path == contract and not _has_substantive_markdown(text):
            findings.append(
                AuditFinding(
                    article_path,
                    "empty-coverage-contract",
                    "coverage contract has no substantive content",
                )
            )

        for line_number, marker in _coverage_gap_markers(text):
            findings.append(
                AuditFinding(
                    article_path,
                    "declared-coverage-gap",
                    f"coverage contract declares {marker} at line {line_number}",
                )
            )

        for line_number, target in _markdown_links(text):
            issue = _local_target_issue(path, target, blueprint, label="coverage")
            if issue is not None:
                code, reason = issue
                findings.append(AuditFinding(article_path, code, f"{reason} (line {line_number})"))
    return findings


def _has_substantive_markdown(text: str) -> bool:
    lines = text.splitlines()
    body = _HTML_COMMENT.sub("", "\n".join(lines[_frontmatter_end(lines) :]))
    for line in body.splitlines():
        if _HEADING.match(line):
            continue
        if line.strip():
            return True
    return False


def _coverage_gap_markers(text: str) -> list[tuple[int, str]]:
    markers: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line, in_comment = _strip_line_comments(raw, in_comment)
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        seen: set[str] = set()
        for match in _COVERAGE_GAP.finditer(line):
            marker = next(group for group in match.groups() if group is not None).upper()
            if marker not in seen:
                markers.append((line_number, marker))
                seen.add(marker)
    return markers


def _markdown_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    fence: tuple[str, int] | None = None
    in_comment = False
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line, in_comment = _strip_line_comments(raw, in_comment)
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
            target = match.group(1)
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            links.append((line_number, target))
    return links


def _strip_line_comments(line: str, in_comment: bool) -> tuple[str, bool]:
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


def _lean_findings(graph: Graph, lean_root: str | Path) -> list[AuditFinding]:
    root = Path(lean_root).expanduser().resolve()
    if not root.is_dir():
        return [
            AuditFinding(
                ".",
                "invalid-lean-root",
                "Lean root does not exist or is not a directory",
            )
        ]

    findings: list[AuditFinding] = []
    index = index_project(root)
    spans = _source_spans(index)
    sizes: dict[str, int] = {}
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        article_path = _relative_path(node.path, graph.blueprint_dir)
        names = declaration_names(node.lean or "")
        if (node.statement_formalized or node.proof_formalized) and not names:
            findings.append(
                AuditFinding(
                    article_path,
                    "missing-lean-target",
                    "formalized local work has no lean declaration target",
                )
            )
            continue

        resolved = []
        for name in names:
            declaration = index.find(name)
            if declaration is None:
                findings.append(
                    AuditFinding(
                        article_path,
                        "lean-target-not-found",
                        f"Lean declaration target was not found: {name}",
                    )
                )
            else:
                resolved.append(declaration)

        expected = _DECLARATION_KEYWORDS.get((node.declaration or "").casefold())
        if expected and resolved and not any(declaration.keyword in expected for declaration in resolved):
            actual = ", ".join(sorted({declaration.keyword for declaration in resolved}))
            findings.append(
                AuditFinding(
                    article_path,
                    "lean-target-kind-mismatch",
                    f"Lean target kind {actual} does not match declaration intent {node.declaration}",
                )
            )

        if resolved:
            sizes[node_id] = sum(spans[declaration.name] for declaration in resolved)

    findings.extend(_size_findings(graph, sizes))
    return findings


def _source_spans(index: SourceIndex) -> dict[str, int]:
    """Measure each declaration's source span, up to the next declaration.

    This is the retrospective size signal: unlike anything authored in the
    article, it is what the node actually cost once it was formalized.
    """

    starts: dict[Path, list[int]] = {}
    for declaration in index.declarations.values():
        starts.setdefault(declaration.path, []).append(declaration.line)
    tails: dict[Path, int] = {}
    for path, lines in starts.items():
        lines.sort()
        tails[path] = _line_count(index.root / path)

    spans: dict[str, int] = {}
    for declaration in index.declarations.values():
        lines = starts[declaration.path]
        following = bisect_right(lines, declaration.line)
        end = lines[following] - 1 if following < len(lines) else tails[declaration.path]
        spans[declaration.name] = max(1, end - declaration.line + 1)
    return spans


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeError):
        return 0


def _size_findings(graph: Graph, sizes: dict[str, int]) -> list[AuditFinding]:
    """Flag finished nodes that are large outliers against this project's own work."""

    if not sizes:
        return []
    median = statistics.median(sizes.values())
    limit = max(_NODE_SIZE_FLOOR, _NODE_SIZE_MULTIPLE * median)
    return [
        AuditFinding(
            _relative_path(graph.nodes[node_id].path, graph.blueprint_dir),
            "node-too-large",
            f"node's Lean declarations span {size} lines against this project's "
            f"{median:g}-line median; split it into pull-request-sized nodes",
        )
        for node_id, size in sorted(sizes.items())
        if size >= limit
    ]


def _stable_validation_reason(blueprint: Path, issue: str) -> str:
    blueprint_text = str(blueprint.resolve())
    return issue.replace(blueprint_text, ".")


def _validation_article_path(blueprint: Path, issue: str) -> str:
    node_id = issue.split(":", 1)[0]
    if not node_id or " " in node_id or node_id in {"dependency cycle", "rolled-up dependency cycle"}:
        return "."

    roadmap = blueprint / "roadmap"
    if node_id == "roadmap":
        candidate = roadmap / "README.md"
    else:
        readme = roadmap / node_id / "README.md"
        candidate = readme if readme.exists() else roadmap / f"{node_id}.md"
    if candidate.exists() or roadmap.exists():
        return _relative_path(candidate, blueprint)
    return "."


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return "."


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _result(findings: list[AuditFinding]) -> AuditResult:
    return AuditResult(tuple(sorted(set(findings))))


__all__ = ["AuditFinding", "AuditResult", "audit_blueprint", "audit_graph"]
