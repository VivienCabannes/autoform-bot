"""Canonical configuration contracts for hard candidate-gate providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
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
DOCKER_CREATE_ATTESTATION_SCHEMA = "autoform-docker-create-attestation/v1"
GATE_RESULT_FRAME_SCHEMA = "AUTOFORM-GATE-RESULT/1"

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_INSPECT_BYTES = 2 * 1024 * 1024
_SHM_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IMAGE = re.compile(r"[^\s@]+@sha256:(?P<digest>[0-9a-f]{64})")
_PLATFORM = re.compile(r"linux/[a-z0-9][a-z0-9_.-]*(?:/[a-z0-9][a-z0-9_.-]*)?")
_USER = re.compile(r"(?P<uid>[1-9][0-9]*):(?P<gid>[1-9][0-9]*)")
_RUNTIME_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_INVOCATION_ID = re.compile(r"[0-9a-f]{64}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_AUTOFORM_LABEL_PREFIX = "org.autoform."
_FORBIDDEN_CONTAINER_ENVIRONMENT = frozenset(
    {
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "GIT_ASKPASS",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "KUBECONFIG",
        "SSH_AGENT_PID",
        "SSH_AUTH_SOCK",
    }
)
_CREDENTIAL_ENVIRONMENT_FRAGMENT = re.compile(
    r"(?:^|_)(?:API_KEY|CREDENTIALS?|PASSWORD|SECRET|TOKEN)(?:_|$)"
)
_RESULT_FRAME_HEADER = re.compile(
    rb"AUTOFORM-GATE-RESULT/1 (?P<size>[1-9][0-9]*) (?P<sha256>[0-9a-f]{64})\n"
)


class GateProviderError(RuntimeError):
    """A hard gate provider configuration or lifecycle is unsafe."""


def encode_gate_result_frame(evidence: bytes) -> bytes:
    """Frame one canonical result so injected stdout can only force rejection."""

    if not isinstance(evidence, bytes) or not evidence:
        raise GateProviderError("gate result evidence must be nonempty bytes")
    digest = hashlib.sha256(evidence).hexdigest()
    header = f"{GATE_RESULT_FRAME_SCHEMA} {len(evidence)} {digest}\n".encode("ascii")
    return header + evidence


def parse_gate_result_frame(frame: bytes, *, maximum: int) -> bytes:
    """Return the sole framed payload, rejecting prefixes, suffixes, and truncation."""

    _positive_integer("gate result byte limit", maximum)
    if not isinstance(frame, bytes) or not frame or len(frame) > maximum + 128:
        raise GateProviderError("gate result frame has an invalid size")
    newline = frame.find(b"\n")
    if newline < 0 or newline > 127:
        raise GateProviderError("gate result frame has an invalid header")
    match = _RESULT_FRAME_HEADER.fullmatch(frame[: newline + 1])
    if match is None:
        raise GateProviderError("gate result frame has an invalid header")
    size = int(match.group("size"))
    if size > maximum:
        raise GateProviderError("gate result evidence exceeds its configured limit")
    evidence = frame[newline + 1 :]
    if len(evidence) != size:
        raise GateProviderError("gate result frame length does not match its evidence")
    expected = match.group("sha256").decode("ascii")
    if not hmac.compare_digest(hashlib.sha256(evidence).hexdigest(), expected):
        raise GateProviderError("gate result frame digest does not match its evidence")
    return evidence


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
    repository_pack_sha256: str
    repository_pack_bytes: int
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
            ("repository pack SHA-256", self.repository_pack_sha256),
            ("provider config SHA-256", self.provider_config_sha256),
        ):
            _sha256(label, value)
        _positive_integer("repository pack bytes", self.repository_pack_bytes)

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
            "repository_pack_bytes": self.repository_pack_bytes,
            "repository_pack_sha256": self.repository_pack_sha256,
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
            "repository_pack_bytes",
            "repository_pack_sha256",
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


@dataclass(frozen=True, slots=True)
class DockerCreateAttestation:
    """Canonical proof that Docker created, but did not start, the requested policy."""

    container_id: str
    request_sha256: str
    provider_config_sha256: str
    inspect_sha256: str
    normalized_policy_sha256: str
    schema: str = DOCKER_CREATE_ATTESTATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DOCKER_CREATE_ATTESTATION_SCHEMA:
            raise GateProviderError("unsupported Docker create attestation schema")
        if _INVOCATION_ID.fullmatch(self.container_id) is None:
            raise GateProviderError("container ID must be 256-bit lowercase hexadecimal")
        for label, value in (
            ("request SHA-256", self.request_sha256),
            ("provider config SHA-256", self.provider_config_sha256),
            ("inspect SHA-256", self.inspect_sha256),
            ("normalized policy SHA-256", self.normalized_policy_sha256),
        ):
            _sha256(label, value)

    def as_dict(self) -> dict[str, str]:
        return {
            "container_id": self.container_id,
            "inspect_sha256": self.inspect_sha256,
            "normalized_policy_sha256": self.normalized_policy_sha256,
            "provider_config_sha256": self.provider_config_sha256,
            "request_sha256": self.request_sha256,
            "schema": self.schema,
        }

    def evidence_bytes(self) -> bytes:
        return _json_bytes(self.as_dict())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.evidence_bytes()).hexdigest()

    @classmethod
    def from_bytes(cls, content: bytes) -> DockerCreateAttestation:
        value = _strict_json_bytes(
            content,
            label="Docker create attestation",
            maximum=_MAX_CONFIG_BYTES,
        )
        expected = {
            "container_id",
            "inspect_sha256",
            "normalized_policy_sha256",
            "provider_config_sha256",
            "request_sha256",
            "schema",
        }
        if set(value) != expected:
            raise GateProviderError("Docker create attestation fields do not match the schema")
        try:
            return cls(**value)
        except (TypeError, ValueError, GateProviderError) as error:
            if isinstance(error, GateProviderError):
                raise
            raise GateProviderError("Docker create attestation contains invalid values") from error


def attest_docker_create(
    config: DockerGateProviderConfig,
    request: GateInvocationRequest,
    repository_pack: str | Path,
    inspect_bytes: bytes,
) -> DockerCreateAttestation:
    """Reject a created container unless every admission-relevant field is exact."""

    create = docker_create_argv(config, request, repository_pack)
    pack = os.fspath(repository_pack)
    inspect = _strict_json_bytes(
        inspect_bytes,
        label="Docker create inspection",
        maximum=_MAX_INSPECT_BYTES,
    )
    container_id = _required_string(inspect, "Id", "Docker container ID")
    if _INVOCATION_ID.fullmatch(container_id) is None:
        raise GateProviderError("Docker inspection contains an invalid container ID")
    _require_equal(inspect, "Name", f"/{request.container_name}", "container name")
    _require_equal(inspect, "Image", config.image_id, "container image ID")
    _require_equal(inspect, "Path", config.evaluator_executable, "container executable")
    image_index = create.index(config.image_reference)
    evaluator_arguments = list(create[image_index + 1 :])
    _require_equal(inspect, "Args", evaluator_arguments, "container arguments")
    _require_equal(inspect, "RestartCount", 0, "container restart count")
    _require_equal(inspect, "Platform", "linux", "container platform")

    state = _required_object(inspect, "State", "container state")
    for key, expected in (
        ("Status", "created"),
        ("Running", False),
        ("Paused", False),
        ("Restarting", False),
        ("OOMKilled", False),
        ("Dead", False),
        ("Pid", 0),
        ("ExitCode", 0),
        ("Error", ""),
    ):
        _require_equal(state, key, expected, f"container state {key}")

    container_config = _required_object(inspect, "Config", "container config")
    _require_equal(container_config, "Hostname", "autoform-gate", "container hostname")
    _require_equal(container_config, "Domainname", "", "container domain")
    _require_equal(container_config, "User", config.user, "container user")
    _require_equal(container_config, "Tty", False, "container TTY")
    _require_equal(container_config, "OpenStdin", False, "container open stdin")
    _require_equal(container_config, "StdinOnce", False, "container stdin-once")
    _require_equal(container_config, "Cmd", evaluator_arguments, "container command")
    _require_equal(container_config, "Image", config.image_reference, "container image reference")
    _require_equal(container_config, "WorkingDir", "/autoform/work", "container working directory")
    _require_equal(container_config, "StopSignal", "SIGKILL", "container stop signal")
    _require_equal(container_config, "StopTimeout", 0, "container stop timeout")
    _require_equal(
        container_config,
        "Entrypoint",
        [config.evaluator_executable],
        "container entrypoint",
    )
    _require_empty_field(container_config, "ExposedPorts", "container exposed ports")
    _require_empty_field(container_config, "Volumes", "container image volumes")
    _require_equal(
        container_config,
        "Healthcheck",
        {"Test": ["NONE"]},
        "container healthcheck",
    )
    labels = _required_object(container_config, "Labels", "container labels")
    expected_labels = dict(label.split("=", 1) for label in request.ownership_labels())
    for key, value in expected_labels.items():
        _require_equal(labels, key, value, f"container ownership label {key}")
    extra_autoform_labels = sorted(
        key
        for key in labels
        if key.startswith(_AUTOFORM_LABEL_PREFIX) and key not in expected_labels
    )
    if extra_autoform_labels:
        raise GateProviderError("Docker inspection contains unexpected Autoform ownership labels")
    environment = _environment_map(container_config.get("Env"))
    for key, value in (
        ("HOME", "/nonexistent"),
        ("TMPDIR", "/autoform/work/tmp"),
    ):
        if environment.get(key) != value:
            raise GateProviderError(f"Docker inspection changed required environment variable {key}")
    forbidden_environment = sorted(
        name
        for name in environment
        if name in _FORBIDDEN_CONTAINER_ENVIRONMENT
        or _CREDENTIAL_ENVIRONMENT_FRAGMENT.search(name.upper()) is not None
    )
    if forbidden_environment:
        raise GateProviderError("Docker inspection exposed forbidden host environment names")

    host = _required_object(inspect, "HostConfig", "Docker host config")
    _validate_host_config(config, pack, host)
    _validate_mounts(pack, inspect.get("Mounts"))
    _validate_network_settings(inspect.get("NetworkSettings"))

    normalized = {
        "arguments": evaluator_arguments,
        "container_id": container_id,
        "environment": environment,
        "host": _normalized_host_policy(config, request, pack),
        "image_id": config.image_id,
        "image_reference": config.image_reference,
        "labels": expected_labels,
        "name": request.container_name,
        "platform": config.platform,
        "user": config.user,
    }
    return DockerCreateAttestation(
        container_id=container_id,
        request_sha256=request.sha256,
        provider_config_sha256=config.sha256,
        inspect_sha256=hashlib.sha256(inspect_bytes).hexdigest(),
        normalized_policy_sha256=hashlib.sha256(_json_bytes(normalized)).hexdigest(),
    )


def docker_create_argv(
    config: DockerGateProviderConfig,
    request: GateInvocationRequest,
    repository_pack: str | Path,
) -> tuple[str, ...]:
    """Return one canonical create command for an inspect-before-start container."""

    if request.provider_config_sha256 != config.sha256:
        raise GateProviderError("gate invocation does not bind the Docker provider config")
    pack = os.fspath(repository_pack)
    _canonical_absolute_path("repository pack", pack)
    if "," in pack:
        raise GateProviderError("repository pack cannot be represented as a Docker bind mount")
    if request.repository_pack_bytes > config.limits.scratch_bytes:
        raise GateProviderError("repository pack does not fit in the Docker scratch limit")
    encoded_request = base64.urlsafe_b64encode(request.evidence_bytes()).decode("ascii")
    limits = config.limits
    mount = (
        f"type=bind,source={pack},target=/autoform/input/repository.pack,"
        "readonly,bind-recursive=disabled"
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
            "--oom-kill-disable=false",
            "--shm-size",
            str(_SHM_BYTES),
            "--cpus",
            _docker_cpus(limits.cpu_nanos),
            "--ulimit",
            f"nofile={limits.nofile_limit}:{limits.nofile_limit}",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            f"/autoform/work:rw,nosuid,nodev,size={limits.scratch_bytes}",
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


def _required_object(
    value: dict[str, Any],
    key: str,
    label: str,
) -> dict[str, Any]:
    if key not in value or not isinstance(value[key], dict):
        raise GateProviderError(f"{label} must be one object")
    return value[key]


def _required_string(value: dict[str, Any], key: str, label: str) -> str:
    if key not in value or not isinstance(value[key], str):
        raise GateProviderError(f"{label} must be text")
    return value[key]


def _require_equal(
    value: dict[str, Any],
    key: str,
    expected: object,
    label: str,
) -> None:
    if key not in value or type(value[key]) is not type(expected) or value[key] != expected:
        raise GateProviderError(f"Docker inspection changed {label}")


def _require_empty(value: object, label: str) -> None:
    if value is None or value == "":
        return
    if isinstance(value, (dict, list)) and not value:
        return
    raise GateProviderError(f"Docker inspection populated {label}")


def _require_empty_field(value: dict[str, Any], key: str, label: str) -> None:
    if key not in value:
        raise GateProviderError(f"Docker inspection omitted {label}")
    _require_empty(value[key], label)


def _environment_map(value: object) -> dict[str, str]:
    if not isinstance(value, list) or len(value) > 256:
        raise GateProviderError("Docker inspection contains an invalid environment")
    environment: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, str) or len(entry.encode("utf-8")) > 16 * 1024:
            raise GateProviderError("Docker inspection contains an invalid environment entry")
        name, separator, content = entry.partition("=")
        if separator != "=" or _ENVIRONMENT_NAME.fullmatch(name) is None:
            raise GateProviderError("Docker inspection contains an invalid environment entry")
        if name in environment:
            raise GateProviderError("Docker inspection contains duplicate environment names")
        environment[name] = content
    return environment


def _validate_host_config(
    config: DockerGateProviderConfig,
    repository_pack: str,
    host: dict[str, Any],
) -> None:
    limits = config.limits
    for key, expected, label in (
        ("NetworkMode", "none", "network mode"),
        ("RestartPolicy", {"Name": "no", "MaximumRetryCount": 0}, "restart policy"),
        ("AutoRemove", False, "automatic removal"),
        ("CapDrop", ["ALL"], "dropped capabilities"),
        ("CgroupnsMode", "private", "cgroup namespace"),
        ("IpcMode", "none", "IPC namespace"),
        ("PidMode", "private", "PID namespace"),
        ("UTSMode", "private", "UTS namespace"),
        ("Privileged", False, "privileged mode"),
        ("PublishAllPorts", False, "published ports"),
        ("ReadonlyRootfs", True, "read-only root filesystem"),
        ("Runtime", config.container_runtime, "OCI runtime"),
        ("Memory", limits.memory_bytes, "memory limit"),
        ("MemorySwap", limits.memory_swap_bytes, "memory plus swap limit"),
        ("MemorySwappiness", 0, "memory swappiness"),
        ("NanoCpus", limits.cpu_nanos, "CPU limit"),
        ("PidsLimit", limits.pids_limit, "PID limit"),
        ("OomKillDisable", False, "OOM killer policy"),
        ("Init", True, "init policy"),
        ("ShmSize", _SHM_BYTES, "shared-memory limit"),
        ("LogConfig", {"Type": "none", "Config": {}}, "logging policy"),
        (
            "Tmpfs",
            {
                "/autoform/work": f"rw,nosuid,nodev,size={limits.scratch_bytes}",
            },
            "tmpfs policy",
        ),
    ):
        _require_equal(host, key, expected, label)

    for key, label in (
        ("Binds", "legacy bind mounts"),
        ("PortBindings", "port bindings"),
        ("Links", "container links"),
        ("Dns", "custom DNS servers"),
        ("DnsOptions", "custom DNS options"),
        ("DnsSearch", "custom DNS search domains"),
        ("ExtraHosts", "extra host mappings"),
        ("VolumesFrom", "inherited volumes"),
        ("CapAdd", "added capabilities"),
        ("GroupAdd", "supplementary groups"),
        ("Devices", "host devices"),
        ("DeviceCgroupRules", "device cgroup rules"),
        ("DeviceRequests", "device requests"),
        ("Sysctls", "custom sysctls"),
        ("StorageOpt", "storage options"),
    ):
        _require_empty_field(host, key, label)

    _validate_security_options(config, host)
    _validate_ulimits(limits, host)
    _validate_host_mount(repository_pack, host)


def _validate_security_options(
    config: DockerGateProviderConfig,
    host: dict[str, Any],
) -> None:
    value = host.get("SecurityOpt")
    if not isinstance(value, list) or len(value) != 2 or any(
        not isinstance(option, str) for option in value
    ):
        raise GateProviderError("Docker inspection contains invalid security options")
    if value.count("no-new-privileges=true") != 1:
        raise GateProviderError("Docker inspection changed no-new-privileges")
    seccomp = [option.removeprefix("seccomp=") for option in value if option.startswith("seccomp=")]
    if len(seccomp) != 1:
        raise GateProviderError("Docker inspection changed the seccomp policy")
    policy = _strict_json_bytes(
        seccomp[0].encode("utf-8"),
        label="Docker seccomp inspection",
        maximum=_MAX_CONFIG_BYTES,
    )
    if hashlib.sha256(_json_bytes(policy)).hexdigest() != config.seccomp_profile_sha256:
        raise GateProviderError("Docker inspection changed the seccomp policy")


def _validate_ulimits(limits: DockerSandboxLimits, host: dict[str, Any]) -> None:
    value = host.get("Ulimits")
    if not isinstance(value, list) or len(value) != 2:
        raise GateProviderError("Docker inspection contains invalid ulimits")
    observed: dict[str, tuple[int, int]] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"Name", "Soft", "Hard"}:
            raise GateProviderError("Docker inspection contains invalid ulimits")
        name = entry["Name"]
        soft = entry["Soft"]
        hard = entry["Hard"]
        if (
            not isinstance(name, str)
            or type(soft) is not int
            or type(hard) is not int
            or name in observed
        ):
            raise GateProviderError("Docker inspection contains invalid ulimits")
        observed[name] = (soft, hard)
    expected = {
        "core": (0, 0),
        "nofile": (limits.nofile_limit, limits.nofile_limit),
    }
    if observed != expected:
        raise GateProviderError("Docker inspection changed the ulimits")


def _validate_host_mount(repository_pack: str, host: dict[str, Any]) -> None:
    mounts = host.get("Mounts")
    if not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
        raise GateProviderError("Docker inspection contains an invalid host mount policy")
    mount = mounts[0]
    for key, expected, label in (
        ("Type", "bind", "host mount type"),
        ("Source", repository_pack, "host mount source"),
        ("Target", "/autoform/input/repository.pack", "host mount target"),
        ("ReadOnly", True, "host mount read-only policy"),
        ("Consistency", "", "host mount consistency"),
    ):
        _require_equal(mount, key, expected, label)
    bind_options = _required_object(mount, "BindOptions", "host bind options")
    expected_options = {
        "CreateMountpoint": False,
        "NonRecursive": True,
        "Propagation": "rprivate",
        "ReadOnlyForceRecursive": False,
        "ReadOnlyNonRecursive": False,
    }
    if bind_options != expected_options:
        raise GateProviderError("Docker inspection changed recursive read-only bind options")
    for key in ("VolumeOptions", "TmpfsOptions", "ImageOptions", "ClusterOptions"):
        if key in mount:
            _require_empty(mount[key], f"host mount {key}")


def _validate_mounts(repository_pack: str, value: object) -> None:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise GateProviderError("Docker inspection contains invalid realized mounts")
    mount = value[0]
    for key, expected, label in (
        ("Type", "bind", "realized mount type"),
        ("Source", repository_pack, "realized mount source"),
        ("Destination", "/autoform/input/repository.pack", "realized mount destination"),
        ("Mode", "ro", "realized mount mode"),
        ("RW", False, "realized mount write policy"),
        ("Propagation", "rprivate", "realized mount propagation"),
    ):
        _require_equal(mount, key, expected, label)


def _validate_network_settings(value: object) -> None:
    if not isinstance(value, dict):
        raise GateProviderError("Docker inspection contains invalid network settings")
    for key, expected, label in (
        ("Bridge", "", "network bridge"),
        ("SandboxID", "", "network sandbox ID"),
        ("SandboxKey", "", "network sandbox key"),
        ("HairpinMode", False, "network hairpin mode"),
        ("LinkLocalIPv6Address", "", "link-local IPv6 address"),
        ("LinkLocalIPv6PrefixLen", 0, "link-local IPv6 prefix"),
    ):
        _require_equal(value, key, expected, label)
    for key, label in (
        ("Ports", "network ports"),
        ("SecondaryIPAddresses", "secondary IP addresses"),
        ("SecondaryIPv6Addresses", "secondary IPv6 addresses"),
    ):
        _require_empty_field(value, key, label)
    networks = _required_object(value, "Networks", "container networks")
    if set(networks) != {"none"} or not isinstance(networks["none"], dict):
        raise GateProviderError("Docker inspection attached an unexpected network")
    network = networks["none"]
    for key, expected, label in (
        ("EndpointID", "", "network endpoint ID"),
        ("Gateway", "", "network gateway"),
        ("IPAddress", "", "network IP address"),
        ("IPPrefixLen", 0, "network IP prefix"),
        ("IPv6Gateway", "", "network IPv6 gateway"),
        ("GlobalIPv6Address", "", "global IPv6 address"),
        ("GlobalIPv6PrefixLen", 0, "global IPv6 prefix"),
        ("MacAddress", "", "network MAC address"),
    ):
        _require_equal(network, key, expected, label)
    for key, label in (
        ("IPAMConfig", "network IPAM configuration"),
        ("Links", "network links"),
        ("Aliases", "network aliases"),
        ("DriverOpts", "network driver options"),
        ("DNSNames", "network DNS names"),
    ):
        _require_empty_field(network, key, label)


def _normalized_host_policy(
    config: DockerGateProviderConfig,
    request: GateInvocationRequest,
    repository_pack: str,
) -> dict[str, object]:
    limits = config.limits
    return {
        "capabilities": {"add": [], "drop": ["ALL"]},
        "container_runtime": config.container_runtime,
        "filesystem": {
            "input": {
                "bytes": request.repository_pack_bytes,
                "destination": "/autoform/input/repository.pack",
                "kind": "git-pack",
                "sha256": request.repository_pack_sha256,
                "source": repository_pack,
                "read_only": True,
            },
            "root_read_only": True,
            "shm_bytes": _SHM_BYTES,
            "tmpfs": {
                "/autoform/work": limits.scratch_bytes,
            },
        },
        "limits": limits.as_dict(),
        "logging": "none",
        "namespaces": {
            "cgroup": "private",
            "ipc": "none",
            "network": "none",
            "pid": "private",
            "uts": "private",
        },
        "no_new_privileges": True,
        "nonroot_user": config.user,
        "seccomp_profile_sha256": config.seccomp_profile_sha256,
    }


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
    "DOCKER_CREATE_ATTESTATION_SCHEMA",
    "DOCKER_GATE_PROVIDER_SCHEMA",
    "DOCKER_SANDBOX_POLICY",
    "GATE_EVALUATOR_SCHEMA",
    "GATE_INVOCATION_SCHEMA",
    "GATE_RESULT_FRAME_SCHEMA",
    "DockerCreateAttestation",
    "DockerGateProviderConfig",
    "DockerSandboxLimits",
    "GateInvocationRequest",
    "GateProviderError",
    "attest_docker_create",
    "docker_create_argv",
    "encode_gate_result_frame",
    "parse_gate_result_frame",
]
