"""Compile an Autoform dependency graph from its Markdown blueprint.

Markdown is both the human wiki and the sole authored graph representation:
node paths are stable ids, frontmatter carries checked facts, and links under
the two dependency headings are typed edges. ``Graph`` is only a validated
in-memory projection. It rejects broken links and cycles instead of persisting
a second graph file that could drift from the book.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+).*?\1")
_FRONTMATTER_KEYS = frozenset(
    {
        "kind",
        "declaration",
        "lean",
        "statement",
        "proof",
        "mathlib",
        "not_ready",
        "discussion",
        "status",
    }
)
_FORMALIZED = "formalized"
_TRUE = frozenset({"true", "yes"})
_FALSE = frozenset({"false", "no"})

#: ``## Depends on`` carries the prerequisites needed to *state* a node;
#: ``## Proof depends on`` carries the extra prerequisites its *proof* needs.
#: Both are graph edges, mirroring where leanblueprint places ``\uses``.
_STATEMENT_SECTION = "depends on"
_PROOF_SECTION = "proof depends on"


class GraphValidationError(ValueError):
    """A blueprint could not be interpreted as a valid dependency graph."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


class LegacyNodesDirectoryWarning(UserWarning):
    """A blueprint still uses the deprecated top-level ``nodes/`` directory."""


class LegacyStatusWarning(UserWarning):
    """A node still asserts the deprecated flat ``status`` field."""


@dataclass(frozen=True, slots=True)
class Node:
    """One Markdown node in a blueprint.

    Only the ``statement``/``proof``/``mathlib``/``not_ready`` assertions are
    recorded here. Everything a reader thinks of as progress -- ready to state,
    ready to prove, fully proved -- is derived from the graph by
    :mod:`autoform_cli.status`, so it can never go stale.
    """

    id: str
    title: str
    path: Path
    dependencies: tuple[str, ...]
    statement_dependencies: tuple[str, ...] = ()
    proof_dependencies: tuple[str, ...] = ()
    kind: str = "node"
    lean: str | None = None
    declaration: str | None = None
    statement_formalized: bool = False
    proof_formalized: bool = False
    mathlib: bool = False
    not_ready: bool = False
    discussion: str | None = None


@dataclass(frozen=True, slots=True)
class Graph:
    """A validated blueprint graph, keyed by stable node id."""

    blueprint_dir: Path
    nodes: dict[str, Node]

    @property
    def edge_count(self) -> int:
        return sum(len(node.dependencies) for node in self.nodes.values())


@dataclass(frozen=True, slots=True)
class _ParsedNode:
    id: str
    title: str
    path: Path
    statement_targets: tuple[str, ...]
    proof_targets: tuple[str, ...]
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class _NodeSource:
    id: str
    path: Path
    text: str
    legacy: bool = False


def load_graph(blueprint_dir: str | Path) -> Graph:
    """Load and validate Markdown nodes beneath *blueprint_dir*."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    if not blueprint.is_dir():
        raise GraphValidationError([f"blueprint directory does not exist: {blueprint}"])

    issues: list[str] = []
    parsed: list[_ParsedNode] = []
    canonical_ids: dict[Path, str] = {}
    node_ids: dict[str, Path] = {}
    sources, discovery_issues, uses_legacy_nodes = _discover_nodes(blueprint)
    issues.extend(discovery_issues)
    if uses_legacy_nodes:
        warnings.warn(
            "blueprint/nodes/ is deprecated; move node files under blueprint/roadmap/ and set kind: node",
            LegacyNodesDirectoryWarning,
            stacklevel=2,
        )

    for source in sources:
        canonical = source.path.resolve()
        if canonical in canonical_ids:
            issues.append(f"{source.id}: duplicates node {canonical_ids[canonical]!r}")
            continue
        if source.id in node_ids:
            issues.append(f"{source.id}: duplicate node id also used by {node_ids[source.id]}")
            continue
        canonical_ids[canonical] = source.id
        node_ids[source.id] = canonical
        node, node_issues = _parse_node(source.id, canonical, source.text)
        issues.extend(node_issues)
        if node is not None:
            if source.legacy:
                legacy_declaration = node.metadata.get("kind")
                explicit_declaration = node.metadata.get("declaration")
                if legacy_declaration in {None, "node"}:
                    legacy_declaration = None
                if (
                    legacy_declaration is not None
                    and explicit_declaration is not None
                    and legacy_declaration != explicit_declaration
                ):
                    issues.append(
                        f"{source.id}: conflicting legacy kind {legacy_declaration!r} "
                        f"and declaration {explicit_declaration!r}"
                    )
                elif legacy_declaration is not None:
                    node.metadata["declaration"] = legacy_declaration
                node.metadata["kind"] = "node"
            parsed.append(node)

    if issues:
        raise GraphValidationError(issues)

    nodes: dict[str, Node] = {}
    for parsed_node in parsed:

        def resolve(targets: tuple[str, ...], node: _ParsedNode = parsed_node) -> list[str]:
            resolved: list[str] = []
            for target in targets:
                dependency, issue = _resolve_target(node, target, blueprint, canonical_ids)
                if issue:
                    issues.append(issue)
                elif dependency == node.id:
                    issues.append(f"{node.id}: dependency on itself")
                elif dependency not in resolved:
                    resolved.append(dependency)
            return resolved

        statement_dependencies = resolve(parsed_node.statement_targets)
        proof_dependencies = resolve(parsed_node.proof_targets)
        dependencies = list(statement_dependencies)
        dependencies.extend(
            dependency for dependency in proof_dependencies if dependency not in dependencies
        )
        metadata = parsed_node.metadata
        nodes[parsed_node.id] = Node(
            id=parsed_node.id,
            title=parsed_node.title,
            path=parsed_node.path,
            dependencies=tuple(dependencies),
            statement_dependencies=tuple(statement_dependencies),
            proof_dependencies=tuple(proof_dependencies),
            kind="node",
            declaration=metadata.get("declaration"),
            lean=metadata.get("lean"),
            statement_formalized=metadata.get("statement") == _FORMALIZED,
            proof_formalized=metadata.get("proof") == _FORMALIZED,
            mathlib=metadata.get("mathlib") in _TRUE,
            not_ready=metadata.get("not_ready") in _TRUE,
            discussion=metadata.get("discussion"),
        )

    if not issues:
        issues.extend(_find_cycles(nodes))
    if issues:
        raise GraphValidationError(issues)
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def _discover_nodes(blueprint: Path) -> tuple[list[_NodeSource], list[str], bool]:
    roadmap_root = blueprint / "roadmap"
    legacy_root = blueprint / "nodes"
    if not roadmap_root.is_dir() and not legacy_root.is_dir():
        return [], [f"roadmap directory does not exist: {roadmap_root}"], False

    issues: list[str] = []
    sources: list[_NodeSource] = []
    if roadmap_root.is_dir():
        roadmap_root = roadmap_root.resolve()
        for path in sorted(roadmap_root.rglob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                relative = path.relative_to(roadmap_root).as_posix()
                issues.append(f"{relative}: cannot read roadmap page: {exc}")
                continue
            if not _declares_node(text):
                continue
            node_id = path.relative_to(roadmap_root).with_suffix("").as_posix()
            canonical = path.resolve()
            if not _is_within(canonical, roadmap_root):
                issues.append(f"{node_id}: node file escapes the roadmap directory")
                continue
            sources.append(_NodeSource(node_id, canonical, text))

    uses_legacy_nodes = False
    if legacy_root.is_dir():
        legacy_root = legacy_root.resolve()
        for path in sorted(legacy_root.rglob("*.md")):
            uses_legacy_nodes = True
            node_id = path.relative_to(legacy_root).with_suffix("").as_posix()
            canonical = path.resolve()
            if not _is_within(canonical, legacy_root):
                issues.append(f"{node_id}: node file escapes the legacy nodes directory")
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                issues.append(f"{node_id}: cannot read node: {exc}")
                continue
            sources.append(_NodeSource(node_id, canonical, text, legacy=True))

    return sources, issues, uses_legacy_nodes


def _declares_node(text: str) -> bool:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for raw in lines[1:]:
        stripped = raw.strip()
        if stripped == "---":
            break
        if ":" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        if key == "kind" and _unquote_scalar(value) == "node":
            return True
    return False


def _parse_node(node_id: str, path: Path, text: str) -> tuple[_ParsedNode | None, list[str]]:
    lines = text.splitlines()
    metadata, body_start, issues = _parse_frontmatter(node_id, lines)
    title: str | None = None
    title_count = 0
    targets: dict[str, list[str]] = {_STATEMENT_SECTION: [], _PROOF_SECTION: []}
    section: str | None = None
    fence: tuple[str, int] | None = None
    body = _HTML_COMMENT.sub("", "\n".join(lines[body_start:]))

    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            marker_kind = marker[0]
            if fence is None:
                fence = (marker_kind, len(marker))
            elif marker_kind == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue

        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if level == 1:
                title_count += 1
                if title is None:
                    title = heading_text
            if level <= 2:
                heading_key = heading_text.casefold()
                section = heading_key if level == 2 and heading_key in targets else None
            continue
        if section is not None:
            for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
                target = match.group(1)
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                targets[section].append(target)

    if title is None:
        issues.append(f"{node_id}: missing H1 title")
    elif title_count > 1:
        issues.append(f"{node_id}: multiple H1 titles")
    if issues:
        return None, issues
    parsed = _ParsedNode(
        node_id,
        title,
        path,
        tuple(targets[_STATEMENT_SECTION]),
        tuple(targets[_PROOF_SECTION]),
        metadata,
    )
    return parsed, []


def _parse_frontmatter(node_id: str, lines: list[str]) -> tuple[dict[str, str], int, list[str]]:
    if not lines or lines[0].strip() != "---":
        return {}, 0, []

    issues: list[str] = []
    metadata: dict[str, str] = {}
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration:
        return {}, len(lines), [f"{node_id}: unterminated frontmatter"]

    for line_number, raw in enumerate(lines[1:end], start=2):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            issues.append(f"{node_id}:{line_number}: expected 'key: value' in frontmatter")
            continue
        key, value = (part.strip() for part in stripped.split(":", 1))
        if key not in _FRONTMATTER_KEYS:
            issues.append(f"{node_id}:{line_number}: unsupported frontmatter key {key!r}")
            continue
        if key in metadata:
            issues.append(f"{node_id}:{line_number}: duplicate frontmatter key {key!r}")
            continue
        value = _unquote_scalar(value)
        if not value:
            issues.append(f"{node_id}:{line_number}: empty frontmatter value for {key!r}")
            continue
        value, issue = _normalize_value(node_id, line_number, key, value)
        if issue:
            issues.append(issue)
            continue
        metadata[key] = value

    _absorb_legacy_status(node_id, metadata)
    return metadata, end + 1, issues


def _normalize_value(node_id: str, line_number: int, key: str, value: str) -> tuple[str, str | None]:
    """Canonicalize an assertion value, or explain why it is not one."""
    location = f"{node_id}:{line_number}"
    folded = value.casefold()
    if key in {"statement", "proof"}:
        if folded != _FORMALIZED:
            return value, f"{location}: {key!r} accepts only {_FORMALIZED!r}; omit the key otherwise"
        return folded, None
    if key in {"mathlib", "not_ready"}:
        if folded not in _TRUE | _FALSE:
            return value, f"{location}: {key!r} accepts only true or false"
        return folded, None
    return value, None


def _absorb_legacy_status(node_id: str, metadata: dict[str, str]) -> None:
    """Map the deprecated flat ``status`` field onto explicit assertions.

    ``ready`` and ``planned`` carry no information the graph cannot derive, so
    they are simply dropped.
    """
    status = metadata.pop("status", None)
    if status is None:
        return
    warnings.warn(
        f"{node_id}: 'status' is deprecated; assert 'statement: formalized', "
        "'proof: formalized', 'mathlib: true', or 'not_ready: true' instead",
        LegacyStatusWarning,
        stacklevel=2,
    )
    folded = status.casefold()
    if folded == "proved":
        metadata.setdefault("statement", _FORMALIZED)
        metadata.setdefault("proof", _FORMALIZED)
    elif folded == "blocked":
        metadata.setdefault("not_ready", "true")


def _unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _resolve_target(
    node: _ParsedNode,
    target: str,
    blueprint: Path,
    canonical_ids: dict[Path, str],
) -> tuple[str | None, str | None]:
    split = urlsplit(target)
    if split.scheme or split.netloc or split.query:
        return None, f"{node.id}: dependency target must be a relative Markdown path: {target!r}"
    raw_path = unquote(split.path)
    if not raw_path:
        return None, f"{node.id}: dependency target must name a Markdown file: {target!r}"
    relative = Path(raw_path)
    if relative.is_absolute() or relative.suffix != ".md":
        return None, f"{node.id}: dependency target must be a relative .md file: {target!r}"

    resolved = (node.path.parent / relative).resolve()
    if not _is_within(resolved, blueprint):
        return None, f"{node.id}: dependency target escapes the blueprint directory: {target!r}"
    if not resolved.is_file():
        return None, f"{node.id}: dependency target does not exist: {target!r}"
    dependency = canonical_ids.get(resolved)
    if dependency is None:
        return None, f"{node.id}: dependency target is not a node: {target!r}"
    return dependency, None


def _find_cycles(nodes: dict[str, Node]) -> list[str]:
    state: dict[str, int] = {}
    stack: list[str] = []
    issues: list[str] = []

    def visit(node_id: str) -> None:
        state[node_id] = 1
        stack.append(node_id)
        for dependency in nodes[node_id].dependencies:
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycle = stack[start:] + [dependency]
                message = f"dependency cycle: {' -> '.join(cycle)}"
                if message not in issues:
                    issues.append(message)
        stack.pop()
        state[node_id] = 2

    for node_id in sorted(nodes):
        if state.get(node_id, 0) == 0:
            visit(node_id)
    return issues


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


__all__ = [
    "Graph",
    "GraphValidationError",
    "LegacyNodesDirectoryWarning",
    "LegacyStatusWarning",
    "Node",
    "load_graph",
]
