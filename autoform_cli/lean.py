"""Resolve blueprint ``lean:`` declarations to their source location.

Scanning the project's own Lean files keeps two promises at once: a proved node
can link to the line that proves it, and a ``lean:`` name that resolves to
nothing is a validation error rather than a broken link -- the job
``leanblueprint checkdecls`` does for LaTeX blueprints.

The scanner is a lexical pass, not an elaborator. It tracks ``namespace`` and
comment nesting, which is enough for declarations written in the ordinary way,
and deliberately reports nothing it cannot see rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_LINE_COMMENT = re.compile(r"--.*$")
_NAMESPACE = re.compile(r"^\s*namespace\s+(\S+)")
_SECTION = re.compile(r"^\s*section\b\s*(\S*)")
_END = re.compile(r"^\s*end\b\s*(\S*)")
_DECLARATION = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)*"
    r"(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive|opaque|axiom)\s+"
    r"([^\s:(){}\[\]⦃⦄,]+)"
)
_IGNORED_DIRECTORIES = frozenset({".lake", ".git", "lake-packages", "build"})
_IGNORED_DIRECTORY_PREFIXES = (".autoform-publication-",)
_PUBLICATION_MANIFEST = "publication.json"
_PUBLICATION_SCHEMAS = frozenset({"autoform-publication/v1", "autoform-publication/v2"})


@dataclass(frozen=True, slots=True)
class Declaration:
    """One Lean declaration found in the project's sources."""

    name: str
    path: Path
    line: int
    keyword: str


@dataclass(frozen=True, slots=True)
class SourceIndex:
    """Every declaration the scanner found, keyed by fully qualified name."""

    root: Path
    declarations: dict[str, Declaration]

    def find(self, name: str) -> Declaration | None:
        return self.declarations.get(name)


@dataclass(frozen=True, slots=True)
class IndexedSourceSnapshot:
    """One source generation used for both declaration links and its digest."""

    index: SourceIndex
    revision: str


def index_project(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> SourceIndex:
    """Scan ``*.lean`` beneath *root* and index declarations by full name."""
    root_path = Path(root).expanduser().resolve()
    excluded: tuple[Path, ...] = tuple(
        candidate
        for value in exclude_roots
        if (candidate := _relative_exclusion(root_path, value)) is not None
    )
    declarations: dict[str, Declaration] = {}
    if not root_path.is_dir():
        return SourceIndex(root=root_path, declarations=declarations)

    for path, relative in _project_source_paths(root_path, excluded):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for declaration in _scan(text, relative):
            # First definition wins, so an earlier file is not masked by a later
            # one when a name is genuinely duplicated across namespaces.
            declarations.setdefault(declaration.name, declaration)
    return SourceIndex(root=root_path, declarations=declarations)


def snapshot_project_sources(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> IndexedSourceSnapshot:
    """Read each Lean source once and derive its index and revision together."""

    root_path = Path(root).expanduser().resolve()
    excluded: tuple[Path, ...] = tuple(
        candidate
        for value in exclude_roots
        if (candidate := _relative_exclusion(root_path, value)) is not None
    )
    declarations: dict[str, Declaration] = {}
    digest = hashlib.sha256(b"autoform-lean-source-index/v1\0")
    if root_path.is_dir():
        for path, relative in _project_source_paths(root_path, excluded):
            data = _stable_source_bytes(path)
            _update_source_digest(digest, relative, data)
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                continue
            for declaration in _scan(text, relative):
                declarations.setdefault(declaration.name, declaration)
    return IndexedSourceSnapshot(
        SourceIndex(root=root_path, declarations=declarations), digest.hexdigest()
    )


def project_source_revision(
    root: str | Path, *, exclude_roots: Iterable[str | Path] = ()
) -> str:
    """Hash the exact Lean source set consumed by :func:`index_project`."""
    root_path = Path(root).expanduser().resolve()
    excluded: tuple[Path, ...] = tuple(
        candidate
        for value in exclude_roots
        if (candidate := _relative_exclusion(root_path, value)) is not None
    )
    digest = hashlib.sha256(b"autoform-lean-source-index/v1\0")
    if not root_path.is_dir():
        return digest.hexdigest()
    for path, relative in _project_source_paths(root_path, excluded):
        data = _stable_source_bytes(path)
        _update_source_digest(digest, relative, data)
    return digest.hexdigest()


def _update_source_digest(digest, relative: Path, data: bytes) -> None:
    encoded = relative.as_posix().encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def _project_source_paths(
    root: Path, excluded: tuple[Path, ...]
) -> Iterable[tuple[Path, Path]]:
    publication_cache: dict[Path, bool] = {}
    for path in sorted(root.rglob("*.lean")):
        relative = path.relative_to(root)
        if (
            _IGNORED_DIRECTORIES.intersection(relative.parts)
            or any(part.startswith(_IGNORED_DIRECTORY_PREFIXES) for part in relative.parts)
            or any(relative == prefix or relative.is_relative_to(prefix) for prefix in excluded)
            or _inside_publication(path.parent, root, publication_cache)
        ):
            continue
        yield path, relative


def _inside_publication(
    directory: Path, root: Path, cache: dict[Path, bool]
) -> bool:
    if directory in cache:
        return cache[directory]
    marker = directory / _PUBLICATION_MANIFEST
    generated = _is_publication_manifest(marker)
    if not generated and directory != root:
        generated = _inside_publication(directory.parent, root, cache)
    cache[directory] = generated
    return generated


def _is_publication_manifest(path: Path) -> bool:
    try:
        if path.stat().st_size > 1024 * 1024:
            return False
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("schema") in _PUBLICATION_SCHEMAS


def _stable_source_bytes(path: Path) -> bytes:
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()
    before_signature = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_signature = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_signature != after_signature:
        raise OSError("Lean source changed while it was read")
    return data


def _relative_exclusion(root: Path, value: str | Path) -> Path | None:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None


def _scan(text: str, relative: Path) -> list[Declaration]:
    found: list[Declaration] = []
    namespaces: list[str] = []
    scopes: list[str | None] = []
    comment_depth = 0

    for number, raw in enumerate(text.splitlines(), start=1):
        line, comment_depth = _strip_comments(raw, comment_depth)
        if not line.strip():
            continue

        namespace_match = _NAMESPACE.match(line)
        if namespace_match:
            name = namespace_match.group(1)
            namespaces.append(name)
            scopes.append(name)
            continue

        section_match = _SECTION.match(line)
        if section_match:
            scopes.append(None)
            continue

        end_match = _END.match(line)
        if end_match:
            if scopes:
                closed = scopes.pop()
                if closed is not None and namespaces:
                    namespaces.pop()
            continue

        declaration_match = _DECLARATION.match(line)
        if declaration_match:
            keyword, name = declaration_match.group(1), declaration_match.group(2)
            qualified = ".".join([*namespaces, name])
            found.append(Declaration(qualified, relative, number, keyword))
    return found


def _strip_comments(line: str, depth: int) -> tuple[str, int]:
    """Remove Lean comments from *line*, carrying block-comment depth across."""
    out: list[str] = []
    index = 0
    while index < len(line):
        pair = line[index : index + 2]
        if depth:
            if pair == "-/":
                depth -= 1
                index += 2
                continue
            if pair == "/-":
                depth += 1
                index += 2
                continue
            index += 1
            continue
        if pair == "/-":
            depth += 1
            index += 2
            continue
        out.append(line[index])
        index += 1
    return _LINE_COMMENT.sub("", "".join(out)), depth


def declaration_names(lean: str) -> list[str]:
    """Split a ``lean:`` frontmatter value into individual declaration names."""
    return [name.strip() for name in lean.replace(",", " ").split() if name.strip()]


@dataclass(frozen=True, slots=True)
class SourceLinker:
    """Build permalinks into the project's Lean sources."""

    index: SourceIndex
    repository_url: str | None = None
    ref: str | None = None

    def location(self, name: str) -> Declaration | None:
        return self.index.find(name)

    def url(self, name: str) -> str | None:
        """Return a permanent link to *name*, or ``None`` if it cannot be built."""
        declaration = self.index.find(name)
        if declaration is None or not self.repository_url or not self.ref:
            return None
        path = declaration.path.as_posix()
        return f"{self.repository_url}/blob/{self.ref}/{path}#L{declaration.line}"


def build_linker(
    lean_root: str | Path,
    *,
    repository_url: str | None = None,
    ref: str | None = None,
    exclude_roots: Iterable[str | Path] = (),
    source_index: SourceIndex | None = None,
) -> SourceLinker:
    """Index *lean_root* and resolve the repository coordinates to link against."""
    return SourceLinker(
        index=(
            source_index
            if source_index is not None
            else index_project(lean_root, exclude_roots=exclude_roots)
        ),
        repository_url=repository_url or detect_repository_url(lean_root),
        ref=ref or detect_ref(lean_root),
    )


def detect_repository_url(root: str | Path) -> str | None:
    """Find the project's web URL from the CI environment or the git remote."""
    repository = os.environ.get("GITHUB_REPOSITORY")
    if repository:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        return f"{server.rstrip('/')}/{repository}"
    remote = _git(root, "config", "--get", "remote.origin.url")
    return _normalize_remote(remote) if remote else None


def detect_ref(root: str | Path) -> str | None:
    """Prefer the exact commit so links keep pointing at the reviewed code."""
    return os.environ.get("GITHUB_SHA") or _git(root, "rev-parse", "HEAD")


def _normalize_remote(remote: str) -> str | None:
    remote = remote.strip()
    if remote.startswith("git@"):
        host, _, path = remote[4:].partition(":")
        if not path:
            return None
        remote = f"https://{host}/{path}"
    elif remote.startswith("ssh://git@"):
        remote = "https://" + remote[len("ssh://git@") :]
    if not remote.startswith(("http://", "https://")):
        return None
    return remote[: -len(".git")] if remote.endswith(".git") else remote.rstrip("/")


def _git(root: str | Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


__all__ = [
    "IndexedSourceSnapshot",
    "Declaration",
    "SourceIndex",
    "SourceLinker",
    "build_linker",
    "declaration_names",
    "detect_ref",
    "detect_repository_url",
    "index_project",
    "project_source_revision",
    "snapshot_project_sources",
]
