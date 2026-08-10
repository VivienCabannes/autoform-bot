"""Write a blueprint vault deterministically instead of describing one.

Setup used to instruct an agent, in prose, to create ``blueprint/`` with a
landing page, ``roadmap/``, ``coverage/``, and ``sources/``, and to imitate the
bundled example. Agents improvise: a real project came back with chapter pages
as siblings of their directories rather than as ``<chapter>/README.md``, which
parses cleanly and publishes a book with no chapters at all. The structure is
fixed, so the tool writes it.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parent / "templates"

#: Template paths whose leading dot is dropped on disk so packaging tools and
#: ignore rules do not swallow them.
_DOTTED = {
    "gitignore": ".gitignore",
    "blueprint/gitignore": "blueprint/.gitignore",
    "github": ".github",
}

DEFAULT_AUTOFORM_SOURCE = "https://github.com/facebookresearch/autoform-bot.git"
DEFAULT_AUTOFORM_REF = "main"


def _git(*args: str) -> str | None:
    """Read a value from the Autoform checkout this CLI is running out of."""

    try:
        done = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent.parent), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None


def plugin_pin() -> tuple[str, str]:
    """The Autoform source and commit that generated CI should install.

    A floating ref is a trap: `facebookresearch/autoform-bot@main` predates the
    CLI entirely, so a scaffolded project's first CI run installs a build with
    no `autoform` command. Pinning the checkout that scaffolded the project
    means the workflow runs the same Autoform the author ran, and the pin is
    immutable by construction.
    """

    source = _git("remote", "get-url", "origin") or DEFAULT_AUTOFORM_SOURCE
    if source.startswith("git@github.com:"):
        source = "https://github.com/" + source[len("git@github.com:") :]
    if not source.endswith(".git"):
        source += ".git"
    return source, _git("rev-parse", "HEAD") or DEFAULT_AUTOFORM_REF


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

    def as_dict(self) -> dict[str, object]:
        return {
            "project": self.project,
            "written": list(self.written),
            "skipped": list(self.skipped),
        }


def _destination(relative: str) -> str:
    for template_prefix, real_prefix in _DOTTED.items():
        if relative == template_prefix:
            return real_prefix
        if relative.startswith(f"{template_prefix}/"):
            return real_prefix + relative[len(template_prefix) :]
    return relative


def _render(text: str, substitutions: dict[str, str]) -> str:
    for key, value in substitutions.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold_project(
    target: str | Path,
    *,
    title: str,
    repository_url: str = "",
    autoform_source: str = "",
    autoform_ref: str = "",
    force: bool = False,
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
    if issues:
        raise ScaffoldError(issues)

    pinned_source, pinned_ref = plugin_pin()
    substitutions = {
        "PROJECT_TITLE": title.strip(),
        "REPO_URL": repository_url.strip(),
        "AUTOFORM_SOURCE": autoform_source.strip() or pinned_source,
        "AUTOFORM_REF": autoform_ref.strip() or pinned_ref,
    }

    written: list[str] = []
    skipped: list[str] = []
    for template in sorted(_TEMPLATES.rglob("*")):
        if not template.is_file():
            continue
        relative = template.relative_to(_TEMPLATES).as_posix()
        destination = root / _destination(relative)
        if destination.exists() and not force:
            skipped.append(_destination(relative))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if template.suffix in {".js", ".html"} or relative.endswith("gitignore"):
            shutil.copyfile(template, destination)
        else:
            destination.write_text(
                _render(template.read_text(encoding="utf-8"), substitutions),
                encoding="utf-8",
            )
        written.append(_destination(relative))

    return ScaffoldResult(title.strip(), tuple(written), tuple(skipped))


__all__ = [
    "DEFAULT_AUTOFORM_REF",
    "DEFAULT_AUTOFORM_SOURCE",
    "ScaffoldError",
    "ScaffoldResult",
    "plugin_pin",
    "scaffold_project",
]
