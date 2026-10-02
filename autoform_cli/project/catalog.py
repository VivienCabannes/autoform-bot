"""Load Autoform's bundled known-good Lean and Mathlib releases."""

from __future__ import annotations

import json
import re
import unicodedata
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from .model import (
    RELEASE_CATALOG_SCHEMA,
    LeanRelease,
    MathlibRelease,
    ReleaseCatalog,
    SupportedRelease,
)


class ProjectCatalogError(ValueError):
    """The bundled release catalog is missing or invalid."""


_LEAN_TOOLCHAIN = re.compile(r"leanprover/lean4:(v[0-9]+\.[0-9]+\.[0-9]+)")
_GIT_REVISION = re.compile(r"[0-9a-f]{40}")


def load_release_catalog() -> ReleaseCatalog:
    try:
        text = files("autoform_cli.project").joinpath("releases.json").read_text(encoding="utf-8")
    except (OSError, TypeError, UnicodeError):
        raise ProjectCatalogError("bundled project release catalog is unavailable") from None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, RecursionError, MemoryError):
        raise ProjectCatalogError("bundled project release catalog is invalid") from None
    return parse_release_catalog(payload)


def parse_release_catalog(payload: Any) -> ReleaseCatalog:
    if not isinstance(payload, dict) or set(payload) != {"schema", "releases"}:
        raise ProjectCatalogError("release catalog has invalid fields")
    if payload["schema"] != RELEASE_CATALOG_SCHEMA or not isinstance(payload["releases"], list):
        raise ProjectCatalogError("release catalog has an invalid schema")

    releases: list[SupportedRelease] = []
    for entry in payload["releases"]:
        releases.append(_parse_release(entry))
    if not releases:
        raise ProjectCatalogError("release catalog is empty")
    if tuple(release.id for release in releases) != tuple(sorted(release.id for release in releases)):
        raise ProjectCatalogError("release catalog is not canonically ordered")
    if len({release.id for release in releases}) != len(releases):
        raise ProjectCatalogError("release catalog has duplicate release ids")
    if sum(release.recommended for release in releases) != 1:
        raise ProjectCatalogError("release catalog must have exactly one recommended release")
    return ReleaseCatalog(RELEASE_CATALOG_SCHEMA, tuple(releases))


def _parse_release(entry: Any) -> SupportedRelease:
    expected = {"id", "channel", "recommended", "lean", "mathlib"}
    if not isinstance(entry, dict) or set(entry) != expected:
        raise ProjectCatalogError("release entry has invalid fields")
    release_id = _string(entry["id"])
    channel = _string(entry["channel"])
    recommended = entry["recommended"]
    if not isinstance(recommended, bool):
        raise ProjectCatalogError("release recommendation must be boolean")
    lean = _object(entry["lean"], {"toolchain", "version"}, "Lean release")
    mathlib = _object(
        entry["mathlib"],
        {
            "config_file",
            "git",
            "input_revision",
            "manifest_file",
            "name",
            "package_type",
            "resolved_revision",
            "scope",
            "subdirectory",
        },
        "Mathlib release",
    )
    lean_toolchain = _string(lean["toolchain"])
    lean_version = _string(lean["version"])
    match = _LEAN_TOOLCHAIN.fullmatch(lean_toolchain)
    if match is None or match.group(1) != lean_version:
        raise ProjectCatalogError("Lean release toolchain and version disagree")
    return SupportedRelease(
        id=release_id,
        channel=channel,
        recommended=recommended,
        lean=LeanRelease(toolchain=lean_toolchain, version=lean_version),
        mathlib=MathlibRelease(
            name=_mathlib_name(mathlib["name"]),
            scope=_string(mathlib["scope"]),
            package_type=_mathlib_package_type(mathlib["package_type"]),
            git=_mathlib_git(mathlib["git"]),
            input_revision=_string(mathlib["input_revision"]),
            resolved_revision=_resolved_revision(mathlib["resolved_revision"]),
            subdirectory=_optional_string(mathlib["subdirectory"]),
            config_file=_string(mathlib["config_file"]),
            manifest_file=_string(mathlib["manifest_file"]),
        ),
    )


def _object(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectCatalogError(f"{name} has invalid fields")
    return value


def _string(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
    ):
        raise ProjectCatalogError("release catalog strings must be nonempty and trimmed")
    return value


def _mathlib_name(value: Any) -> str:
    name = _string(value)
    if name != "mathlib":
        raise ProjectCatalogError("Mathlib release name must be mathlib")
    return name


def _mathlib_package_type(value: Any) -> str:
    package_type = _string(value)
    if package_type != "git":
        raise ProjectCatalogError("Mathlib release package type must be git")
    return package_type


def _optional_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def _resolved_revision(value: Any) -> str:
    revision = _string(value)
    if _GIT_REVISION.fullmatch(revision) is None:
        raise ProjectCatalogError("Mathlib resolved revision must be a full lowercase Git SHA")
    return revision


def _mathlib_git(value: Any) -> str:
    raw = _string(value)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise ProjectCatalogError("Mathlib release Git source is invalid") from error
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not path
    ):
        raise ProjectCatalogError("Mathlib release Git source is invalid")
    return f"https://{parsed.hostname.lower()}{path}"
