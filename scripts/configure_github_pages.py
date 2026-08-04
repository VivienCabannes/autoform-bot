#!/usr/bin/env python3
"""Inspect or install an opt-in GitHub Pages configuration for Autoform."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve()
AUTOFORM_ROOT = SCRIPT.parents[1]
TEMPLATE = AUTOFORM_ROOT / "templates" / "github" / "autoform-pages.yml"
WORKFLOW_PATH = Path(".github/workflows/autoform-pages.yml")
CONFIG_PATH = Path(".autoform/pages.json")
PUBLISHED_CATEGORIES = (
    "graph structure",
    "theorem content",
    "proof status",
    "review verdicts",
    "kernel evidence",
)
EXCLUDED_CATEGORIES = (
    "agent activity",
    "task queues",
    "dispatcher logs",
    "backend configuration",
    "credentials",
    "local filesystem paths",
)
_GITHUB_REMOTE = re.compile(r"github\.com(?::|/)([^/]+)/([^/]+?)(?:\.git)?$")
_SAFE_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")
_CANONICAL_AUTOFORM_REPOSITORY = "facebookresearch/autoform-bot"


class PagesConfigError(RuntimeError):
    """A repository visibility, approval, or configuration error."""


def _run(args: list[str], *, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _github_slug(remote: str) -> str | None:
    match = _GITHUB_REMOTE.search(remote.strip())
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _git_remote(repo_root: Path) -> tuple[str | None, str | None]:
    try:
        remote = _run(["git", "remote", "get-url", "origin"], cwd=repo_root)
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return remote, _github_slug(remote)


def _github_visibility(repo_root: Path, repository: str) -> tuple[str, str | None, str | None]:
    try:
        payload = json.loads(
            _run(
                [
                    "gh",
                    "repo",
                    "view",
                    repository,
                    "--json",
                    "visibility,url,defaultBranchRef",
                ],
                cwd=repo_root,
            )
        )
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return "unclear", None, None
    visibility = str(payload.get("visibility") or "unclear").lower()
    if visibility not in {"public", "private", "internal"}:
        visibility = "unclear"
    branch = payload.get("defaultBranchRef")
    branch_name = branch.get("name") if isinstance(branch, dict) else None
    return visibility, payload.get("url"), branch_name


def inspect_repository(repo_root: Path, *, repository: str | None = None) -> dict:
    repo_root = repo_root.resolve()
    remote, detected = _git_remote(repo_root)
    repository = repository or detected
    if repository is None:
        visibility, url, default_branch = "unclear", None, None
    else:
        visibility, url, default_branch = _github_visibility(repo_root, repository)
    return {
        "repository": repository,
        "github_remote_detected": remote is not None and detected is not None,
        "url": url,
        "visibility": visibility,
        "default_branch": default_branch,
        "published_categories": list(PUBLISHED_CATEGORIES),
        "excluded_categories": list(EXCLUDED_CATEGORIES),
        "writes": [str(CONFIG_PATH), str(WORKFLOW_PATH)],
    }


def _safe_relative(repo_root: Path, path: Path | str, *, label: str) -> Path:
    repo_root = repo_root.resolve()
    path = Path(path)
    target = path.resolve() if path.is_absolute() else (repo_root / path).resolve()
    try:
        return target.relative_to(repo_root)
    except ValueError as error:
        raise PagesConfigError(f"{label} must stay inside the repository") from error


def _autoform_source(autoform_root: Path) -> tuple[str, str]:
    autoform_root = autoform_root.resolve()
    try:
        remote = _run(["git", "remote", "get-url", "origin"], cwd=autoform_root)
        revision = _run(["git", "rev-parse", "HEAD"], cwd=autoform_root)
        _run(
            ["git", "cat-file", "-e", f"{revision}:scripts/export_github_dashboard.py"],
            cwd=autoform_root,
        )
        repository = _github_slug(remote)
        if repository is not None and _SAFE_REVISION.fullmatch(revision):
            return repository, revision
    except (OSError, subprocess.CalledProcessError):
        pass

    repository = os.environ.get(
        "AUTOFORM_PAGES_EXPORTER_REPOSITORY",
        _CANONICAL_AUTOFORM_REPOSITORY,
    )
    try:
        revision = _run(
            [
                "gh",
                "api",
                f"repos/{repository}/commits/main",
                "--jq",
                ".sha",
            ],
            cwd=autoform_root,
        )
        _run(
            [
                "gh",
                "api",
                f"repos/{repository}/contents/scripts/export_github_dashboard.py?ref={revision}",
                "--jq",
                ".sha",
            ],
            cwd=autoform_root,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise PagesConfigError(
            "cannot resolve a public, immutable Autoform exporter revision; provide repository and revision"
        ) from error
    if not _SAFE_REVISION.fullmatch(revision):
        raise PagesConfigError("resolved Autoform exporter revision is not a full Git commit")
    return repository, revision


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _workflow(
    *,
    graph: Path,
    site: Path,
    default_branch: str,
    autoform_repository: str,
    autoform_revision: str,
) -> str:
    if not TEMPLATE.is_file():
        raise PagesConfigError(f"workflow template missing: {TEMPLATE}")
    graph_dir = graph.parent
    watch = (
        graph,
        graph_dir / "informal_content/**",
        graph_dir / "kernel/**",
        graph_dir / "review_status.json",
        Path("blueprint/web/**"),
        Path("**/*.lean"),
        CONFIG_PATH,
        WORKFLOW_PATH,
    )
    watch_lines = "\n".join(f"      - {json.dumps(str(path))}" for path in watch)
    return (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__WATCH_PATHS__", watch_lines)
        .replace("__DEFAULT_BRANCH__", json.dumps(default_branch))
        .replace("__AUTOFORM_REPOSITORY__", autoform_repository)
        .replace("__AUTOFORM_REVISION__", autoform_revision)
        .replace("__GRAPH_PATH__", json.dumps(str(graph)))
        .replace("__SITE_PATH__", json.dumps(str(site)))
    )


def install_configuration(
    repo_root: Path,
    *,
    repository: str,
    visibility: str,
    graph: Path | str,
    site: Path | str,
    autoform_repository: str,
    autoform_revision: str,
    default_branch: str = "main",
    approved: bool,
    private_pages_verified: bool = False,
    force: bool = False,
) -> tuple[Path, Path]:
    repo_root = repo_root.resolve()
    if not approved:
        raise PagesConfigError("explicit publication approval is required")
    if visibility not in {"public", "private", "internal", "unclear"}:
        raise PagesConfigError("repository visibility is invalid; refusing automatic publication")
    if visibility == "unclear":
        raise PagesConfigError("repository visibility is unclear; refusing automatic publication")
    if visibility in {"private", "internal"} and not private_pages_verified:
        raise PagesConfigError(
            "private/internal Pages availability and access control must be verified for this plan"
        )
    if not _SAFE_REVISION.fullmatch(autoform_revision):
        raise PagesConfigError("Autoform revision must be a full 40-character Git commit")
    if not _REPOSITORY_SLUG.fullmatch(repository):
        raise PagesConfigError("repository must be an owner/name GitHub slug")
    if not _REPOSITORY_SLUG.fullmatch(autoform_repository):
        raise PagesConfigError("Autoform repository must be an owner/name GitHub slug")
    if (
        not _BRANCH_NAME.fullmatch(default_branch)
        or default_branch.startswith("/")
        or default_branch.endswith("/")
        or ".." in default_branch
    ):
        raise PagesConfigError("default branch name is invalid")
    graph = _safe_relative(repo_root, graph, label="graph")
    site = _safe_relative(repo_root, site, label="site output")
    if site == Path(".") or graph == site or site in graph.parents:
        raise PagesConfigError("site output must be a dedicated directory separate from the graph")
    config_path = repo_root / CONFIG_PATH
    workflow_path = repo_root / WORKFLOW_PATH
    for path in (config_path, workflow_path):
        if path.is_symlink():
            raise PagesConfigError(f"refusing to replace symlink: {path}")
        if path.exists() and not force:
            raise PagesConfigError(f"configuration already exists: {path}; pass --force to replace")

    config: dict[str, Any] = {
        "version": 1,
        "repository": repository,
        "repository_visibility": visibility,
        "default_branch": default_branch,
        "private_pages_verified": bool(private_pages_verified),
        "graph": str(graph),
        "site": str(site),
        "autoform_exporter": {
            "repository": autoform_repository,
            "revision": autoform_revision,
        },
        "published_categories": list(PUBLISHED_CATEGORIES),
        "excluded_categories": list(EXCLUDED_CATEGORIES),
    }
    config_payload = json.dumps(config, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    workflow_payload = _workflow(
        graph=graph,
        site=site,
        default_branch=default_branch,
        autoform_repository=autoform_repository,
        autoform_revision=autoform_revision,
    )
    _atomic_write(config_path, config_payload)
    _atomic_write(workflow_path, workflow_payload)
    return config_path, workflow_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--repository")

    install = subparsers.add_parser("install")
    install.add_argument("--repository")
    install.add_argument("--graph", type=Path, default=Path(".autoform/graph.json"))
    install.add_argument("--site", type=Path, default=Path(".autoform/site"))
    install.add_argument("--autoform-root", type=Path, default=AUTOFORM_ROOT)
    install.add_argument("--autoform-repository")
    install.add_argument("--autoform-revision")
    install.add_argument("--approve-publication", action="store_true")
    install.add_argument("--private-pages-verified", action="store_true")
    install.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    repo_root = args.repo_root.resolve()
    inspection = inspect_repository(
        repo_root,
        repository=getattr(args, "repository", None),
    )
    if args.command == "inspect":
        print(json.dumps(inspection, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    try:
        repository = args.repository or inspection["repository"]
        visibility = inspection["visibility"]
        default_branch = inspection["default_branch"]
        if repository is None:
            raise PagesConfigError("no GitHub repository could be identified")
        if default_branch is None:
            raise PagesConfigError("default branch is unclear; refusing to generate a deployment workflow")
        if args.autoform_repository and args.autoform_revision:
            autoform_repository = args.autoform_repository
            autoform_revision = args.autoform_revision
        elif args.autoform_repository or args.autoform_revision:
            raise PagesConfigError("provide both Autoform repository and revision")
        else:
            autoform_repository, autoform_revision = _autoform_source(args.autoform_root)
        paths = install_configuration(
            repo_root,
            repository=repository,
            visibility=visibility,
            graph=args.graph,
            site=args.site,
            autoform_repository=autoform_repository,
            autoform_revision=autoform_revision,
            default_branch=default_branch,
            approved=args.approve_publication,
            private_pages_verified=args.private_pages_verified,
            force=args.force,
        )
    except PagesConfigError as error:
        parser.error(str(error))
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
