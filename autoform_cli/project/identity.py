"""Canonical material identities shared by project inspection and its catalog."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit


class MathlibGitError(ValueError):
    """A Mathlib Git URL cannot participate in a trusted material identity."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def canonical_mathlib_git(value: object) -> str:
    """Return the credential-free HTTPS identity for a raw Mathlib Git URL."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\\" in value
        or any(character.isspace() for character in value)
        or _has_unsafe_unicode(value)
    ):
        raise MathlibGitError("invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise MathlibGitError("invalid") from error
    if parsed.username is not None or parsed.password is not None:
        raise MathlibGitError("credentialed")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.lower() != parsed.hostname.lower()
    ):
        raise MathlibGitError("transport")
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path:
        raise MathlibGitError("repository")
    return f"https://{parsed.hostname.lower()}{path}"


def canonical_package_path(
    value: object,
    *,
    root_is_none: bool = False,
) -> str | None:
    """Validate the canonical relative spelling of a Lake package path."""

    if type(value) is not str or not value or _has_unsafe_unicode(value):
        raise ValueError("invalid package path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or ".." in posix.parts
    ):
        raise ValueError("invalid package path")
    normalized = posix.as_posix()
    if normalized != value:
        raise ValueError("noncanonical package path")
    if normalized == ".":
        if root_is_none:
            return None
        raise ValueError("invalid package path")
    return normalized


def material_identity_key(
    lean_toolchain: str,
    name: str,
    package_type: str,
    git: str | None,
    resolved_revision: str | None,
    subdirectory: str | None,
    config_file: str,
    manifest_file: str | None,
) -> tuple[str, str, str, str | None, str | None, str | None, str, str | None]:
    """Return every field that changes the Lean/Mathlib material source."""

    return (
        lean_toolchain,
        name,
        package_type,
        git,
        resolved_revision,
        subdirectory,
        config_file,
        manifest_file,
    )


def _has_unsafe_unicode(value: str) -> bool:
    return any(not character.isprintable() for character in value)
