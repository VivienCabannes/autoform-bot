"""Load Autoform's bundled known-good Lean and Mathlib releases."""

from __future__ import annotations

import json
import re
import unicodedata
from importlib.resources import files
from typing import Any

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
    mathlib = _object(entry["mathlib"], {"git", "revision"}, "Mathlib release")
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
        mathlib=MathlibRelease(git=_string(mathlib["git"]), revision=_string(mathlib["revision"])),
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
