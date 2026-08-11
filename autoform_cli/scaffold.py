"""Write a blueprint vault deterministically instead of describing one.

Setup used to instruct an agent, in prose, to create ``blueprint/`` with a
landing page, ``roadmap/``, ``coverage/``, and ``sources/``, and to imitate the
bundled example. Agents improvise: a real project came back with chapter pages
as siblings of their directories rather than as ``<chapter>/README.md``, which
parses cleanly and publishes a book with no chapters at all. The structure is
fixed, so the tool writes it.
"""

from __future__ import annotations

import json
import re
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
_FULL_SHA = re.compile(r"[0-9a-f]{40}")

#: Where `claude plugin install` records the marketplace each plugin came from.
_PLUGIN_REGISTRY = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"


def _here() -> Path:
    """The Autoform directory this CLI is running out of."""

    return Path(__file__).resolve().parent.parent


def _git(*args: str, root: Path | None = None) -> str | None:
    """Read a value from an Autoform checkout, defaulting to this one."""

    try:
        done = subprocess.run(
            ["git", "-C", str(root or _here()), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = done.stdout.strip()
    return value if done.returncode == 0 and value else None


def _marketplace_checkout() -> Path | None:
    """The checkout an installed plugin copy was made from, if it is on disk.

    `claude plugin install` copies into
    ``~/.claude/plugins/cache/<marketplace>/<plugin>/<version>`` from a
    marketplace it keeps as a real Git checkout, and records where in
    ``known_marketplaces.json``. That checkout is this code's actual provenance,
    so reading it is not the guess :func:`plugin_pin` refuses to make.

    Returns ``None`` on anything unexpected: not running from a plugin cache, no
    registry, no such marketplace, or a location that is not a checkout of
    Autoform. A wrong answer here is worse than no answer.
    """

    parts = _here().parts
    try:
        cache = len(parts) - 1 - parts[::-1].index("cache")
    except ValueError:
        return None
    if cache + 1 >= len(parts):
        return None
    try:
        registry = json.loads(_PLUGIN_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = registry.get(parts[cache + 1]) if isinstance(registry, dict) else None
    location = entry.get("installLocation") if isinstance(entry, dict) else None
    if not isinstance(location, str) or not location:
        return None
    checkout = Path(location).expanduser()
    # Insist it is a checkout of *this* project, not merely a checkout.
    if not (checkout / ".git").exists():
        return None
    if not (checkout / "autoform_cli" / "scaffold.py").is_file():
        return None
    return checkout


def plugin_pin() -> tuple[str, str]:
    """The Autoform source and commit generated CI should install, if knowable.

    Read from the Autoform checkout this CLI runs out of, or, when there is none
    because `claude plugin install` copied the directory without its `.git`,
    from the marketplace checkout that copy was made from. Both are records of
    where this code came from rather than assumptions about it.

    Returns empty strings when neither is available. An earlier version fell
    back to `facebookresearch/autoform-bot@main` instead. That commit predates
    `autoform_cli` entirely, so every project scaffolded through the plugin got
    CI that installed a build with no `autoform` command and failed at the first
    step, with nothing in the workflow to explain why. A wrong pin is worse than
    no pin: guessing here is what made the failure silent.
    """

    root = None if (_here() / ".git").exists() else _marketplace_checkout()
    source = _git("remote", "get-url", "origin", root=root)
    ref = _git("rev-parse", "HEAD", root=root)
    if not source or not ref or not _FULL_SHA.fullmatch(ref):
        return "", ""
    if source.startswith("git@github.com:"):
        source = "https://github.com/" + source[len("git@github.com:") :]
    if not source.endswith(".git"):
        source += ".git"
    return source, ref


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
    # A branch name or an abbreviated sha is the same silent failure this
    # gate exists to prevent, just supplied by hand: CI would reinstall a
    # different Autoform later and break a project that was passing.
    given_ref = autoform_ref.strip()
    if given_ref and not _FULL_SHA.fullmatch(given_ref):
        issues.append(
            f"--autoform-ref must be a full 40-character commit sha, not {given_ref!r}; "
            "branches and abbreviated shas do not stay put"
        )
    if issues:
        raise ScaffoldError(issues)

    pinned_source, pinned_ref = plugin_pin()
    source = autoform_source.strip() or pinned_source or DEFAULT_AUTOFORM_SOURCE
    ref = autoform_ref.strip() or pinned_ref
    # CI installs Autoform from a Git ref. Where Autoform lives is a fixed fact
    # worth defaulting; which commit is not, and a guessed one publishes a
    # project whose first CI step fails for a reason no file in it explains. So
    # the ref alone decides: without one the workflows are skipped and reported.
    unpinned = not ref
    substitutions = {
        "PROJECT_TITLE": title.strip(),
        "REPO_URL": repository_url.strip(),
        "AUTOFORM_SOURCE": source,
        "AUTOFORM_REF": ref,
    }

    written: list[str] = []
    skipped: list[str] = []
    for template in sorted(_TEMPLATES.rglob("*")):
        if not template.is_file():
            continue
        relative = template.relative_to(_TEMPLATES).as_posix()
        if unpinned and relative.startswith("github/workflows/"):
            skipped.append(_destination(relative))
            continue
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

    return ScaffoldResult(title.strip(), tuple(written), tuple(skipped), unpinned)


__all__ = [
    "DEFAULT_AUTOFORM_SOURCE",
    "ScaffoldError",
    "ScaffoldResult",
    "plugin_pin",
    "scaffold_project",
]
