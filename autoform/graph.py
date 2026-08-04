"""Load an Autoform blueprint from a small Markdown wiki."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[^)]*)?\)")
_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"(`+).*?\1")
_FRONTMATTER_KEYS = frozenset({"kind", "status", "lean"})


class GraphValidationError(ValueError):
    """A blueprint could not be interpreted as a valid dependency graph."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class Node:
    """One Markdown node in a blueprint."""

    id: str
    title: str
    path: Path
    dependencies: tuple[str, ...]
    kind: str | None = None
    status: str | None = None
    lean: str | None = None


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
    targets: tuple[str, ...]
    metadata: dict[str, str]


def load_graph(blueprint_dir: str | Path) -> Graph:
    """Load and validate ``blueprint/nodes/**/*.md`` beneath *blueprint_dir*."""

    blueprint = Path(blueprint_dir).expanduser().resolve()
    if not blueprint.is_dir():
        raise GraphValidationError([f"blueprint directory does not exist: {blueprint}"])

    nodes_root = blueprint / "nodes"
    if not nodes_root.is_dir():
        raise GraphValidationError([f"nodes directory does not exist: {nodes_root}"])
    nodes_root = nodes_root.resolve()

    issues: list[str] = []
    parsed: list[_ParsedNode] = []
    canonical_ids: dict[Path, str] = {}
    for path in sorted(nodes_root.rglob("*.md")):
        relative = path.relative_to(nodes_root)
        node_id = relative.with_suffix("").as_posix()
        canonical = path.resolve()
        if not _is_within(canonical, nodes_root):
            issues.append(f"{node_id}: node file escapes the nodes directory")
            continue
        if canonical in canonical_ids:
            issues.append(f"{node_id}: duplicates node {canonical_ids[canonical]!r}")
            continue
        canonical_ids[canonical] = node_id
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            issues.append(f"{node_id}: cannot read node: {exc}")
            continue
        node, node_issues = _parse_node(node_id, canonical, text)
        issues.extend(node_issues)
        if node is not None:
            parsed.append(node)

    if issues:
        raise GraphValidationError(issues)

    nodes: dict[str, Node] = {}
    for parsed_node in parsed:
        dependencies: list[str] = []
        for target in parsed_node.targets:
            dependency, issue = _resolve_target(parsed_node, target, nodes_root, canonical_ids)
            if issue:
                issues.append(issue)
            elif dependency == parsed_node.id:
                issues.append(f"{parsed_node.id}: dependency on itself")
            elif dependency not in dependencies:
                dependencies.append(dependency)
        nodes[parsed_node.id] = Node(
            id=parsed_node.id,
            title=parsed_node.title,
            path=parsed_node.path,
            dependencies=tuple(dependencies),
            kind=parsed_node.metadata.get("kind"),
            status=parsed_node.metadata.get("status"),
            lean=parsed_node.metadata.get("lean"),
        )

    if not issues:
        issues.extend(_find_cycles(nodes))
    if issues:
        raise GraphValidationError(issues)
    return Graph(blueprint_dir=blueprint, nodes=nodes)


def _parse_node(node_id: str, path: Path, text: str) -> tuple[_ParsedNode | None, list[str]]:
    lines = text.splitlines()
    metadata, body_start, issues = _parse_frontmatter(node_id, lines)
    title: str | None = None
    title_count = 0
    targets: list[str] = []
    in_dependencies = False
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
                in_dependencies = level == 2 and heading_text.casefold() == "depends on"
            continue
        if in_dependencies:
            for match in _LINK.finditer(_INLINE_CODE.sub("", line)):
                target = match.group(1)
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                targets.append(target)

    if title is None:
        issues.append(f"{node_id}: missing H1 title")
    elif title_count > 1:
        issues.append(f"{node_id}: multiple H1 titles")
    if issues:
        return None, issues
    return _ParsedNode(node_id, title, path, tuple(targets), metadata), []


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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if not value:
            issues.append(f"{node_id}:{line_number}: empty frontmatter value for {key!r}")
            continue
        metadata[key] = value
    return metadata, end + 1, issues


def _resolve_target(
    node: _ParsedNode,
    target: str,
    nodes_root: Path,
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
    if not _is_within(resolved, nodes_root):
        return None, f"{node.id}: dependency target escapes the nodes directory: {target!r}"
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


__all__ = ["Graph", "GraphValidationError", "Node", "load_graph"]
