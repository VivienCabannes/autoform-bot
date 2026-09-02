"""Write Autoform vault files deterministically instead of describing them.

The legacy project scaffold creates ``blueprint/`` with a landing page,
``roadmap/``, ``coverage/``, and ``sources/`` rather than asking an agent to
imitate the bundled example. Agents improvise: a real project came back with
chapter pages as siblings of their directories rather than as
``<chapter>/README.md``, which parses cleanly and publishes a book with no
chapters at all. The internal vault structure is fixed, so both legacy and
workspace scaffolds write it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .provenance import normalize_git_source

_TEMPLATES = Path(__file__).resolve().parent / "templates"

#: Template paths whose leading dot is dropped on disk so packaging tools and
#: ignore rules do not swallow them.
_DOTTED = {
    "gitignore": ".gitignore",
    "blueprint/gitignore": "blueprint/.gitignore",
    "github": ".github",
}

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_TEMPLATE_PLACEHOLDER = re.compile(r"\{\{(?P<name>[A-Z][A-Z0-9_]*)\}\}")
_WORKSPACE_MANIFEST = ".autoform.toml"


def _normalize_autoform_source(source: str, *, allow_github_scp: bool = False) -> str | None:
    """Compatibility wrapper for explicit workflow-source validation."""

    return normalize_git_source(source, allow_github_scp=allow_github_scp)


def plugin_pin() -> tuple[str, str]:
    """Return verified all-or-nothing provenance for legacy callers."""

    # Import at call time so direct imports of ``autoform_cli.scaffold`` remain
    # independent of the project package's initialization order.
    from .provenance import plugin_pin as verified_plugin_pin

    return verified_plugin_pin()


class ScaffoldError(ValueError):
    """The project could not be scaffolded safely."""

    def __init__(self, issues: list[str] | tuple[str, ...]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class ScaffoldResult:
    """What a scaffold run wrote, and what it left alone."""

    project: str
    written: tuple[str, ...]
    skipped: tuple[str, ...]
    unpinned: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "written": list(self.written),
            "skipped": list(self.skipped),
            "unpinned": self.unpinned,
        }


def _destination(relative: str) -> str:
    for template_prefix, real_prefix in _DOTTED.items():
        if relative == template_prefix:
            return real_prefix
        if relative.startswith(f"{template_prefix}/"):
            return real_prefix + relative[len(template_prefix) :]
    return relative


def _yaml_scalar(value: str) -> str:
    """Serialize *value* as a quoted YAML scalar.

    JSON strings are valid YAML double-quoted scalars. Using the standard JSON
    serializer preserves the value while escaping line breaks, tabs, nulls,
    quotes, backslashes, and every other control character that could otherwise
    alter the generated document.
    """
    return json.dumps(value, ensure_ascii=False)


def _render(text: str, substitutions: dict[str, str]) -> str:
    """Substitute tokens from the original template exactly once.

    Replacement values are user-controlled in several templates. A sequential
    series of ``str.replace`` calls can reinterpret token-shaped text inside an
    earlier value, corrupting YAML and Markdown or exposing another generated
    value. A single regex pass never scans replacement content again.
    """

    return _TEMPLATE_PLACEHOLDER.sub(
        lambda match: substitutions.get(match.group("name"), match.group(0)),
        text,
    )


def _atomic_write(destination: Path, content: bytes, *, mode: int) -> None:
    """Replace *destination* from a same-directory temporary file.

    Replacing rather than truncating is essential when an existing destination
    has hard links: ``--force`` must not modify another path to the old inode.
    """

    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _within(path: Path, root: Path) -> bool:
    """True when *path* resolves to somewhere at or beneath *root*.

    Resolution follows symlinks, so this is what confines the scaffold: it is
    not enough to reject a symlinked project root, because a link one level
    down -- `project/blueprint` pointing elsewhere -- redirects the whole vault
    out of the project, and `--force` would then overwrite files there.
    """
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _require_exclusive_scaffold_support() -> None:
    required = (
        hasattr(os, "O_DIRECTORY"),
        hasattr(os, "O_NOFOLLOW"),
        os.open in os.supports_dir_fd,
        os.mkdir in os.supports_dir_fd,
        os.listdir in os.supports_fd,
    )
    if not all(required):
        raise ScaffoldError(
            ["this platform cannot scaffold a blueprint with the required path safety"]
        )


def _open_directory_chain(path: Path) -> int:
    """Open an absolute directory path without following any component link."""

    _require_exclusive_scaffold_support()
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, flags)
        for part in absolute.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise ScaffoldError([f"cannot open blueprint directory safely: {path}"]) from None


def _open_or_create_directory(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        os.mkdir(name, mode=0o755, dir_fd=parent_descriptor)
    except FileExistsError:
        pass
    except OSError:
        raise ScaffoldError([f"cannot create blueprint directory: {name}"]) from None
    try:
        return os.open(name, flags, dir_fd=parent_descriptor)
    except OSError:
        raise ScaffoldError([f"cannot open blueprint directory safely: {name}"]) from None


def _exclusive_write_at(parent_descriptor: int, name: str, content: bytes, *, mode: int) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(name, flags, mode, dir_fd=parent_descriptor)
    except FileExistsError:
        raise ScaffoldError([f"blueprint destination already exists: {name}"]) from None
    except OSError:
        raise ScaffoldError([f"cannot create blueprint file safely: {name}"]) from None
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        raise ScaffoldError([f"cannot write blueprint file safely: {name}"]) from None


def scaffold_project(
    target: str | Path,
    *,
    title: str,
    repository_url: str = "",
    autoform_source: str = "",
    autoform_ref: str = "",
    force: bool = False,
    discover_plugin_pin: bool = True,
) -> ScaffoldResult:
    """Write the blueprint vault, site config, and CI into *target*.

    Existing files are never overwritten unless *force* is set; they come back
    in ``skipped`` so a repair run reports exactly what it left in place.
    """

    requested = Path(target).expanduser()
    issues: list[str] = []
    if not title.strip():
        issues.append("project title must not be empty")
    # Checked before resolve(), which would collapse the link and hide it.
    if requested.is_symlink():
        issues.append(f"refusing to scaffold into a symlink: {requested}")
    root = requested.resolve()
    if root.exists() and not root.is_dir():
        issues.append(f"target exists and is not a directory: {root}")
    if (root / _WORKSPACE_MANIFEST).exists() or (root / _WORKSPACE_MANIFEST).is_symlink():
        issues.append(
            "refusing the legacy single-vault scaffold in a manifest-managed workspace; "
            "use 'autoform blueprint new'"
        )
    # A branch name or an abbreviated sha is the same silent failure this
    # gate exists to prevent, just supplied by hand: CI would reinstall a
    # different Autoform later and break a project that was passing.
    # Git treats a sha case-insensitively and always prints lowercase, so an
    # uppercase one pasted from a web UI is valid input, not a mistake.
    given_ref = autoform_ref.strip().lower()
    if autoform_ref and not _FULL_SHA.fullmatch(given_ref):
        issues.append(
            f"--autoform-ref must be a full 40-character commit sha, not {given_ref!r}; "
            "branches and abbreviated shas do not stay put"
        )
    given_source = ""
    if autoform_source:
        normalized = _normalize_autoform_source(autoform_source)
        if normalized is None:
            issues.append(
                "--autoform-source must be a safe credential-free HTTPS Git URL ending in .git"
            )
        else:
            given_source = normalized
    if issues:
        raise ScaffoldError(issues)

    if bool(autoform_source) != bool(autoform_ref):
        issues.append("--autoform-source and --autoform-ref must be provided together")
    if issues:
        raise ScaffoldError(issues)

    # Explicit provenance is already a complete caller choice. Discovery is a
    # network verification step and must not run merely to be discarded.
    pinned_source, pinned_ref = (
        plugin_pin()
        if discover_plugin_pin and not (given_source and given_ref)
        else ("", "")
    )
    safe_pinned_source = _normalize_autoform_source(pinned_source, allow_github_scp=True)
    if safe_pinned_source is None or not _FULL_SHA.fullmatch(pinned_ref.lower()):
        pinned_source, pinned_ref = "", ""
    else:
        pinned_source, pinned_ref = safe_pinned_source, pinned_ref.lower()
    source = given_source or pinned_source
    ref = given_ref or pinned_ref
    unpinned = not source or not ref
    substitutions = {
        "PROJECT_TITLE_YAML": _yaml_scalar(title.strip()),
        "REPO_URL_YAML": _yaml_scalar(repository_url.strip()),
        "PROJECT_TITLE": title.strip(),
        "REPO_URL": repository_url.strip(),
        "AUTOFORM_SOURCE": source,
        "AUTOFORM_REF": ref,
        "AUTOFORM_SOURCE_YAML": _yaml_scalar(source),
        "AUTOFORM_REF_YAML": _yaml_scalar(ref),
    }

    written: list[str] = []
    skipped: list[str] = []
    for template in sorted(_TEMPLATES.rglob("*")):
        relative_path = template.relative_to(_TEMPLATES)
        if (
            not template.is_file()
            or "__pycache__" in relative_path.parts
            or template.suffix == ".pyc"
        ):
            continue
        relative = relative_path.as_posix()
        if unpinned and relative.startswith("github/"):
            skipped.append(_destination(relative))
            continue
        destination = root / _destination(relative)
        # Confine every write, not just the root. Reject links outright before
        # checking whether the destination should be skipped: `exists()` is
        # false for a dangling symlink, but opening that path still follows the
        # link and can create a file outside the project.
        probe = root
        for part in Path(_destination(relative)).parts:
            probe = probe / part
            if probe.is_symlink() or (probe.exists() and not _within(probe, root)):
                raise ScaffoldError(
                    [f"refusing to write outside the project through a link: {probe}"]
                )
        if destination.exists() and not force:
            skipped.append(_destination(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if template.suffix in {".js", ".html"} or relative.endswith("gitignore"):
            content = template.read_bytes()
        else:
            rendered = _render(template.read_text(encoding="utf-8"), substitutions)
            content = rendered.encode("utf-8")
        _atomic_write(destination, content, mode=stat.S_IMODE(template.stat().st_mode))
        written.append(_destination(relative))

    return ScaffoldResult(title.strip(), tuple(written), tuple(skipped), unpinned)


def scaffold_blueprint(
    target: str | Path,
    *,
    title: str,
    _directory_descriptor: int | None = None,
    _directory_identity: tuple[int, int] | None = None,
) -> tuple[str, ...]:
    """Write only the vault files beneath ``templates/blueprint`` into *target*.

    This is the workspace counterpart to :func:`scaffold_project`: shared
    repository files stay at the workspace root, while each registered project
    receives an independent roadmap, coverage contract, and source notes.
    """

    _require_exclusive_scaffold_support()
    requested = Path(target).expanduser().absolute()
    issues: list[str] = []
    if not title.strip():
        issues.append("project title must not be empty")
    if requested.is_symlink():
        issues.append(f"refusing to scaffold into a symlink: {requested}")
    if not requested.exists():
        issues.append(f"target directory does not exist: {requested}")
    elif not requested.is_dir():
        issues.append(f"target exists and is not a directory: {requested}")
    if issues:
        raise ScaffoldError(issues)

    root_descriptor = (
        _open_directory_chain(requested)
        if _directory_descriptor is None
        else os.dup(_directory_descriptor)
    )
    try:
        opened = os.fstat(root_descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ScaffoldError([f"blueprint directory binding is no longer a directory: {requested}"])
        if _directory_identity is not None and (opened.st_dev, opened.st_ino) != _directory_identity:
            raise ScaffoldError([f"blueprint directory binding changed: {requested}"])
        try:
            if os.listdir(root_descriptor):
                raise ScaffoldError([f"target directory is not empty: {requested}"])
        except OSError:
            raise ScaffoldError([f"cannot inspect blueprint directory: {requested}"]) from None

        substitutions = {"PROJECT_TITLE": title.strip()}
        source_root = _TEMPLATES / "blueprint"
        written: list[str] = []
        for template in sorted(source_root.rglob("*")):
            relative_path = template.relative_to(source_root)
            if (
                not template.is_file()
                or "__pycache__" in relative_path.parts
                or template.suffix == ".pyc"
            ):
                continue
            relative = relative_path.as_posix()
            destination_relative = ".gitignore" if relative == "gitignore" else relative
            if template.suffix in {".js", ".html"} or relative.endswith("gitignore"):
                content = template.read_bytes()
            else:
                content = _render(template.read_text(encoding="utf-8"), substitutions).encode(
                    "utf-8"
                )

            parent_descriptor = os.dup(root_descriptor)
            try:
                for part in Path(destination_relative).parts[:-1]:
                    next_descriptor = _open_or_create_directory(parent_descriptor, part)
                    os.close(parent_descriptor)
                    parent_descriptor = next_descriptor
                _exclusive_write_at(
                    parent_descriptor,
                    Path(destination_relative).name,
                    content,
                    mode=stat.S_IMODE(template.stat().st_mode),
                )
            finally:
                os.close(parent_descriptor)
            written.append(destination_relative)
        return tuple(written)
    finally:
        os.close(root_descriptor)


__all__ = [
    "ScaffoldError",
    "ScaffoldResult",
    "plugin_pin",
    "scaffold_blueprint",
    "scaffold_project",
]
