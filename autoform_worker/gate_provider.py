"""Canonical configuration contracts for hard candidate-gate providers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DOCKER_GATE_PROVIDER_SCHEMA = "autoform-docker-gate-provider/v1"
DOCKER_SANDBOX_POLICY = "autoform-docker-gate-sandbox/v1"
GATE_EVALUATOR_SCHEMA = "autoform-gate-evaluator/v1"

_MAX_CONFIG_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"[^\s@]+@sha256:(?P<digest>[0-9a-f]{64})")
_PLATFORM = re.compile(r"linux/[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)?")
_USER = re.compile(r"(?P<uid>[1-9][0-9]*):(?P<gid>[1-9][0-9]*)")


class GateProviderError(RuntimeError):
    """A hard gate provider configuration or lifecycle is unsafe."""


@dataclass(frozen=True, slots=True)
class DockerSandboxLimits:
    """Resource ceilings enforced by Docker and bound into run identity."""

    wall_timeout_seconds: float
    memory_bytes: int
    memory_swap_bytes: int
    cpu_nanos: int
    pids_limit: int
    scratch_bytes: int
    output_bytes: int
    nofile_limit: int

    def __post_init__(self) -> None:
        timeout = _positive_finite("wall timeout", self.wall_timeout_seconds)
        object.__setattr__(self, "wall_timeout_seconds", timeout)
        for label, value in (
            ("memory bytes", self.memory_bytes),
            ("memory swap bytes", self.memory_swap_bytes),
            ("CPU nanos", self.cpu_nanos),
            ("PID limit", self.pids_limit),
            ("scratch bytes", self.scratch_bytes),
            ("output bytes", self.output_bytes),
            ("nofile limit", self.nofile_limit),
        ):
            _positive_integer(label, value)
        if self.memory_swap_bytes != self.memory_bytes:
            raise GateProviderError("memory swap bytes must equal memory bytes")
        if self.output_bytes > self.scratch_bytes:
            raise GateProviderError("output bytes must not exceed scratch bytes")

    def as_dict(self) -> dict[str, int | float]:
        return {
            "cpu_nanos": self.cpu_nanos,
            "memory_bytes": self.memory_bytes,
            "memory_swap_bytes": self.memory_swap_bytes,
            "nofile_limit": self.nofile_limit,
            "output_bytes": self.output_bytes,
            "pids_limit": self.pids_limit,
            "scratch_bytes": self.scratch_bytes,
            "wall_timeout_seconds": self.wall_timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class DockerGateProviderConfig:
    """Every host, image, policy, and resource input needed to resume safely."""

    runtime_path: str
    runtime_executable_sha256: str
    runtime_fingerprint_sha256: str
    image_reference: str
    image_id: str
    platform: str
    runtime_bundle_sha256: str
    seccomp_profile_path: str
    seccomp_profile_sha256: str
    user: str
    limits: DockerSandboxLimits
    schema: str = DOCKER_GATE_PROVIDER_SCHEMA
    policy: str = DOCKER_SANDBOX_POLICY
    evaluator_schema: str = GATE_EVALUATOR_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DOCKER_GATE_PROVIDER_SCHEMA:
            raise GateProviderError("unsupported Docker gate provider schema")
        if self.policy != DOCKER_SANDBOX_POLICY:
            raise GateProviderError("unsupported Docker sandbox policy")
        if self.evaluator_schema != GATE_EVALUATOR_SCHEMA:
            raise GateProviderError("unsupported gate evaluator schema")
        _canonical_absolute_path("runtime path", self.runtime_path)
        _canonical_absolute_path("seccomp profile path", self.seccomp_profile_path)
        for label, value in (
            ("runtime executable SHA-256", self.runtime_executable_sha256),
            ("runtime fingerprint SHA-256", self.runtime_fingerprint_sha256),
            ("runtime bundle SHA-256", self.runtime_bundle_sha256),
            ("seccomp profile SHA-256", self.seccomp_profile_sha256),
        ):
            _sha256(label, value)
        image = _IMAGE.fullmatch(self.image_reference)
        if image is None:
            raise GateProviderError("image reference must contain an immutable sha256 digest")
        if not self.image_id.startswith("sha256:"):
            raise GateProviderError("image ID must use the sha256 algorithm")
        _sha256("image ID", self.image_id.removeprefix("sha256:"))
        if _PLATFORM.fullmatch(self.platform) is None:
            raise GateProviderError("platform must be an exact Linux OCI platform")
        user = _USER.fullmatch(self.user)
        if user is None:
            raise GateProviderError("container user must be an explicit non-root numeric uid:gid")

    def as_dict(self) -> dict[str, object]:
        return {
            "evaluator_schema": self.evaluator_schema,
            "image_id": self.image_id,
            "image_reference": self.image_reference,
            "limits": self.limits.as_dict(),
            "platform": self.platform,
            "policy": self.policy,
            "runtime_bundle_sha256": self.runtime_bundle_sha256,
            "runtime_executable_sha256": self.runtime_executable_sha256,
            "runtime_fingerprint_sha256": self.runtime_fingerprint_sha256,
            "runtime_path": self.runtime_path,
            "schema": self.schema,
            "seccomp_profile_path": self.seccomp_profile_path,
            "seccomp_profile_sha256": self.seccomp_profile_sha256,
            "user": self.user,
        }

    def evidence_bytes(self) -> bytes:
        return _json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, content: bytes) -> DockerGateProviderConfig:
        if not isinstance(content, bytes) or not content or len(content) > _MAX_CONFIG_BYTES:
            raise GateProviderError("Docker gate provider config has an invalid size")
        try:
            value = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=lambda constant: (_raise_json_constant(constant)),
            )
        except (UnicodeError, ValueError, TypeError) as error:
            raise GateProviderError("Docker gate provider config is not strict JSON") from error
        if not isinstance(value, dict):
            raise GateProviderError("Docker gate provider config must contain one object")
        expected = {
            "evaluator_schema",
            "image_id",
            "image_reference",
            "limits",
            "platform",
            "policy",
            "runtime_bundle_sha256",
            "runtime_executable_sha256",
            "runtime_fingerprint_sha256",
            "runtime_path",
            "schema",
            "seccomp_profile_path",
            "seccomp_profile_sha256",
            "user",
        }
        if set(value) != expected:
            raise GateProviderError("Docker gate provider config fields do not match the schema")
        limits_value = value.pop("limits")
        if not isinstance(limits_value, dict):
            raise GateProviderError("Docker gate provider limits must contain one object")
        limit_fields = {
            "cpu_nanos",
            "memory_bytes",
            "memory_swap_bytes",
            "nofile_limit",
            "output_bytes",
            "pids_limit",
            "scratch_bytes",
            "wall_timeout_seconds",
        }
        if set(limits_value) != limit_fields:
            raise GateProviderError("Docker gate provider limit fields do not match the schema")
        try:
            limits = DockerSandboxLimits(**limits_value)
            return cls(limits=limits, **value)
        except (TypeError, ValueError, GateProviderError) as error:
            if isinstance(error, GateProviderError):
                raise
            raise GateProviderError("Docker gate provider config contains invalid values") from error


def _canonical_absolute_path(label: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GateProviderError(f"{label} must be a nonempty canonical absolute path")
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise GateProviderError(f"{label} must be a nonempty canonical absolute path")


def _sha256(label: str, value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise GateProviderError(f"{label} must be lowercase hexadecimal")


def _positive_integer(label: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise GateProviderError(f"{label} must be a positive integer")


def _positive_finite(label: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GateProviderError(f"{label} must be a finite positive number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise GateProviderError(f"{label} must be a finite positive number") from error
    if not math.isfinite(normalized) or normalized <= 0:
        raise GateProviderError(f"{label} must be a finite positive number")
    return normalized


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _raise_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise GateProviderError("Docker gate provider config is not canonical JSON") from error


__all__ = [
    "DOCKER_GATE_PROVIDER_SCHEMA",
    "DOCKER_SANDBOX_POLICY",
    "GATE_EVALUATOR_SCHEMA",
    "DockerGateProviderConfig",
    "DockerSandboxLimits",
    "GateProviderError",
]
