"""Canonical configuration contracts for hard candidate-gate providers."""

from __future__ import annotations

import hashlib
import base64
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autoform_cli.graph import ARTICLE_ID_PATTERN


DOCKER_GATE_PROVIDER_SCHEMA = "autoform-docker-gate-provider/v1"
DOCKER_SANDBOX_POLICY = "autoform-docker-gate-sandbox/v1"
GATE_EVALUATOR_SCHEMA = "autoform-gate-evaluator/v1"
GATE_INVOCATION_SCHEMA = "autoform-gate-invocation/v1"

_MAX_CONFIG_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"[^\s@]+@sha256:(?P<digest>[0-9a-f]{64})")
_PLATFORM = re.compile(r"linux/[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)?")
_USER = re.compile(r"(?P<uid>[1-9][0-9]*):(?P<gid>[1-9][0-9]*)")
_RUNTIME_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_INVOCATION_ID = re.compile(r"[0-9a-f]{64}")


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
    evaluator_executable: str
    container_runtime: str
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
        _canonical_absolute_path("evaluator executable", self.evaluator_executable)
        if _RUNTIME_NAME.fullmatch(self.container_runtime) is None:
            raise GateProviderError("container runtime must be a canonical Docker runtime name")
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
        if any(int(user.group(field)) > 2**31 - 1 for field in ("uid", "gid")):
            raise GateProviderError("container user uid and gid must fit signed 32-bit values")

    def as_dict(self) -> dict[str, object]:
        return {
            "evaluator_schema": self.evaluator_schema,
            "evaluator_executable": self.evaluator_executable,
            "container_runtime": self.container_runtime,
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
        value = _strict_json_bytes(
            content,
            label="Docker gate provider config",
            maximum=_MAX_CONFIG_BYTES,
        )
        expected = {
            "evaluator_schema",
            "evaluator_executable",
            "container_runtime",
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


@dataclass(frozen=True, slots=True)
class GateInvocationRequest:
    """Immutable evaluator request persisted before a container is created."""

    invocation_id: str
    run_id: str
    attempt_id: str
    base_oid: str
    candidate_oid: str
    node_id: str
    article_id: str
    phase: str
    attempt: int
    source_revision: str
    source_contract_sha256: str
    protected_roadmap_sha256: str
    work_item_sha256: str
    provider_config_sha256: str
    schema: str = GATE_INVOCATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != GATE_INVOCATION_SCHEMA:
            raise GateProviderError("unsupported gate invocation schema")
        if _INVOCATION_ID.fullmatch(self.invocation_id) is None:
            raise GateProviderError("gate invocation ID must be 256-bit lowercase hexadecimal")
        for label, value in (
            ("run ID", self.run_id),
            ("attempt ID", self.attempt_id),
            ("node ID", self.node_id),
        ):
            _canonical_text(label, value)
        if not isinstance(self.article_id, str) or ARTICLE_ID_PATTERN.fullmatch(self.article_id) is None:
            raise GateProviderError("article ID must use the durable Autoform format")
        _sha256("source revision", self.source_revision)
        for label, value in (("base OID", self.base_oid), ("candidate OID", self.candidate_oid)):
            if not isinstance(value, str) or _OID.fullmatch(value) is None:
                raise GateProviderError(f"{label} must be a lowercase SHA-1 or SHA-256 object ID")
        if len(self.base_oid) != len(self.candidate_oid):
            raise GateProviderError("base and candidate OIDs must use the same object format")
        if self.base_oid == self.candidate_oid:
            raise GateProviderError("candidate OID must differ from base OID")
        if self.phase not in {"statement", "proof"}:
            raise GateProviderError("gate invocation phase must be statement or proof")
        _positive_integer("attempt number", self.attempt)
        for label, value in (
            ("source contract SHA-256", self.source_contract_sha256),
            ("protected roadmap SHA-256", self.protected_roadmap_sha256),
            ("work item SHA-256", self.work_item_sha256),
            ("provider config SHA-256", self.provider_config_sha256),
        ):
            _sha256(label, value)

    @property
    def container_name(self) -> str:
        return f"autoform-gate-{self.invocation_id}"

    def ownership_labels(self) -> tuple[str, ...]:
        return (
            "org.autoform.gate=1",
            f"org.autoform.invocation={self.invocation_id}",
            f"org.autoform.request-sha256={self.sha256}",
            f"org.autoform.provider-sha256={self.provider_config_sha256}",
        )

    def evaluator_dict(self) -> dict[str, object]:
        """Return only the fields the in-container evaluator must trust."""

        return {
            "article_id": self.article_id,
            "attempt": self.attempt,
            "base_oid": self.base_oid,
            "candidate_oid": self.candidate_oid,
            "node_id": self.node_id,
            "phase": self.phase,
            "protected_roadmap_sha256": self.protected_roadmap_sha256,
            "source_contract_sha256": self.source_contract_sha256,
            "source_revision": self.source_revision,
            "work_item_sha256": self.work_item_sha256,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.evaluator_dict(),
            "attempt_id": self.attempt_id,
            "invocation_id": self.invocation_id,
            "provider_config_sha256": self.provider_config_sha256,
            "run_id": self.run_id,
            "schema": self.schema,
        }

    def evidence_bytes(self) -> bytes:
        return _json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, content: bytes) -> GateInvocationRequest:
        value = _strict_json_bytes(content, label="gate invocation", maximum=_MAX_CONFIG_BYTES)
        expected = {
            "article_id",
            "attempt",
            "attempt_id",
            "base_oid",
            "candidate_oid",
            "invocation_id",
            "node_id",
            "phase",
            "protected_roadmap_sha256",
            "provider_config_sha256",
            "run_id",
            "schema",
            "source_contract_sha256",
            "source_revision",
            "work_item_sha256",
        }
        if set(value) != expected:
            raise GateProviderError("gate invocation fields do not match the schema")
        try:
            return cls(**value)
        except (TypeError, ValueError, GateProviderError) as error:
            if isinstance(error, GateProviderError):
                raise
            raise GateProviderError("gate invocation contains invalid values") from error


def docker_create_argv(
    config: DockerGateProviderConfig,
    request: GateInvocationRequest,
    repository_root: str | Path,
) -> tuple[str, ...]:
    """Return one canonical create command for an inspect-before-start container."""

    if request.provider_config_sha256 != config.sha256:
        raise GateProviderError("gate invocation does not bind the Docker provider config")
    root = os.fspath(repository_root)
    _canonical_absolute_path("repository root", root)
    if "," in root:
        raise GateProviderError("repository root cannot be represented as a Docker bind mount")
    encoded_request = base64.urlsafe_b64encode(request.evidence_bytes()).decode("ascii")
    limits = config.limits
    mount = (
        f"type=bind,source={root},target=/autoform/input/repository,"
        "readonly,bind-recursive=readonly"
    )
    arguments = [
        config.runtime_path,
        "create",
        "--name",
        request.container_name,
    ]
    for label in request.ownership_labels():
        arguments.extend(("--label", label))
    arguments.extend(
        (
            "--pull",
            "never",
            "--platform",
            config.platform,
            "--network",
            "none",
            "--pid",
            "private",
            "--ipc",
            "none",
            "--uts",
            "private",
            "--cgroupns",
            "private",
            "--runtime",
            config.container_runtime,
            "--read-only",
            "--init",
            "--user",
            config.user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--security-opt",
            f"seccomp={config.seccomp_profile_path}",
            "--pids-limit",
            str(limits.pids_limit),
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_swap_bytes),
            "--memory-swappiness",
            "0",
            "--cpus",
            _docker_cpus(limits.cpu_nanos),
            "--ulimit",
            f"nofile={limits.nofile_limit}:{limits.nofile_limit}",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            f"/autoform/work:rw,nosuid,nodev,size={limits.scratch_bytes}",
            "--tmpfs",
            f"/autoform/result:rw,nosuid,nodev,noexec,size={limits.output_bytes}",
            "--mount",
            mount,
            "--log-driver",
            "none",
            "--restart",
            "no",
            "--stop-signal",
            "SIGKILL",
            "--stop-timeout",
            "0",
            "--no-healthcheck",
            "--hostname",
            "autoform-gate",
            "--workdir",
            "/autoform/work",
            "--env",
            "HOME=/nonexistent",
            "--env",
            "TMPDIR=/autoform/work/tmp",
            "--env",
            "AUTOFORM_GATE_RESULT=/autoform/result/result.json",
            "--entrypoint",
            config.evaluator_executable,
            config.image_reference,
            "-I",
            "-m",
            "autoform_worker.gate_evaluator",
            "--request-base64",
            encoded_request,
        )
    )
    return tuple(arguments)


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


def _canonical_text(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise GateProviderError(f"{label} must be nonempty canonical text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise GateProviderError(f"{label} must be nonempty canonical text") from error
    if (
        not value
        or len(encoded) > 4096
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise GateProviderError(f"{label} must be nonempty canonical text")


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


def _docker_cpus(value: int) -> str:
    whole, remainder = divmod(value, 1_000_000_000)
    return f"{whole}.{remainder:09d}"


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_bytes(content: bytes, *, label: str, maximum: int) -> dict[str, Any]:
    if not isinstance(content, bytes) or not content or len(content) > maximum:
        raise GateProviderError(f"{label} has an invalid size")
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda constant: (_raise_json_constant(constant)),
        )
    except (UnicodeError, ValueError, TypeError) as error:
        raise GateProviderError(f"{label} is not strict JSON") from error
    if not isinstance(value, dict):
        raise GateProviderError(f"{label} must contain one object")
    return value


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
    "GATE_INVOCATION_SCHEMA",
    "DockerGateProviderConfig",
    "DockerSandboxLimits",
    "GateInvocationRequest",
    "GateProviderError",
    "docker_create_argv",
]
