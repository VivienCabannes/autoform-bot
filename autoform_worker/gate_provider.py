"""Canonical configuration contracts for hard candidate-gate providers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from autoform_cli.graph import ARTICLE_ID_PATTERN


# v3 deliberately rejects v2 evidence: v2 had no external daemon anchor and
# no separate canonical seccomp policy digest.
DOCKER_GATE_PROVIDER_SCHEMA = "autoform-docker-gate-provider/v3"
DOCKER_SANDBOX_POLICY = "autoform-docker-gate-sandbox/v1"
DOCKER_RUNTIME_BUNDLE_SCHEMA = "autoform-gate-runtime-bundle/v1"
DOCKER_RUNTIME_FINGERPRINT_SCHEMA = "autoform-docker-runtime-fingerprint/v1"
GATE_EVALUATOR_SCHEMA = "autoform-gate-evaluator/v1"
GATE_INVOCATION_SCHEMA = "autoform-gate-invocation/v1"
DOCKER_CREATE_ATTESTATION_SCHEMA = "autoform-docker-create-attestation/v1"
GATE_RESULT_FRAME_SCHEMA = "AUTOFORM-GATE-RESULT/1"

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_INSPECT_BYTES = 2 * 1024 * 1024
_MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_SECCOMP_BYTES = 1024 * 1024
_DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 15.0
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
_CREDENTIAL_ENVIRONMENT_FRAGMENT = re.compile(r"(?:^|_)(?:API_KEY|CREDENTIALS?|PASSWORD|SECRET|TOKEN)(?:_|$)")
_RESULT_FRAME_HEADER = re.compile(rb"AUTOFORM-GATE-RESULT/1 (?P<size>[1-9][0-9]*) (?P<sha256>[0-9a-f]{64})\n")
_DOCKER_API_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_MINIMUM_DOCKER_API_VERSION = (1, 48)
_RUNTIME_BUNDLE_SCHEMA_LABEL = "org.autoform.runtime-bundle-schema"
_RUNTIME_BUNDLE_SHA256_LABEL = "org.autoform.runtime-bundle-sha256"
_DOCKER_ARCHITECTURES = {
    "aarch64": "arm64",
    "amd64": "amd64",
    "arm64": "arm64",
    "ppc64le": "ppc64le",
    "riscv64": "riscv64",
    "s390x": "s390x",
    "x86_64": "amd64",
}
_SECCOMP_DENY_ACTIONS = frozenset(
    {
        "SCMP_ACT_ERRNO",
        "SCMP_ACT_KILL",
        "SCMP_ACT_KILL_PROCESS",
        "SCMP_ACT_KILL_THREAD",
        "SCMP_ACT_TRAP",
    }
)
_SECCOMP_RULE_ACTIONS = _SECCOMP_DENY_ACTIONS | {"SCMP_ACT_ALLOW"}
_SYSCALL_NAME = re.compile(r"[A-Za-z0-9_]+")


class GateProviderError(RuntimeError):
    """A hard gate provider configuration or lifecycle is unsafe."""


@dataclass(frozen=True, slots=True)
class DockerCommandResult:
    """Raw bounded output from one Docker discovery command."""

    stdout: bytes
    stderr: bytes
    returncode: int


class DockerDiscoveryRunner(Protocol):
    """Injectable command boundary used by Docker provider discovery."""

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
        output_bytes_limit: int,
    ) -> DockerCommandResult: ...


@dataclass(frozen=True, slots=True)
class _RegularFileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    owner_uid: int
    owner_gid: int
    links: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int
    owner_uid: int


@dataclass(frozen=True, slots=True)
class _SocketIdentity:
    device: int
    inode: int
    mode: int
    owner_uid: int
    owner_gid: int


@dataclass(frozen=True, slots=True)
class _DiscoveryBindings:
    runtime_path: str
    runtime: _RegularFileIdentity
    state_directory: str
    state: _DirectoryIdentity
    docker_config_directory: str
    docker_config: _DirectoryIdentity
    seccomp_profile_path: str
    seccomp: _RegularFileIdentity
    seccomp_policy_sha256: str
    docker_host: str | None = None
    socket: _SocketIdentity | None = None


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
    runtime_device: int
    runtime_inode: int
    runtime_mode: int
    runtime_size: int
    runtime_owner_uid: int
    runtime_executable_sha256: str
    runtime_fingerprint_sha256: str
    docker_host: str
    docker_daemon_id: str
    docker_socket_device: int
    docker_socket_inode: int
    docker_socket_mode: int
    docker_socket_owner_uid: int
    docker_socket_owner_gid: int
    state_directory: str
    state_directory_device: int
    state_directory_inode: int
    state_directory_owner_uid: int
    docker_config_directory: str
    docker_config_device: int
    docker_config_inode: int
    docker_config_owner_uid: int
    image_reference: str
    image_id: str
    platform: str
    runtime_bundle_sha256: str
    seccomp_profile_path: str
    seccomp_profile_device: int
    seccomp_profile_inode: int
    seccomp_profile_size: int
    seccomp_profile_owner_uid: int
    seccomp_profile_sha256: str
    seccomp_policy_sha256: str
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
        _docker_socket_path_text(self.docker_host)
        _canonical_ascii_text("Docker daemon ID", self.docker_daemon_id)
        _canonical_absolute_path("state directory", self.state_directory)
        _canonical_absolute_path("Docker config directory", self.docker_config_directory)
        _canonical_absolute_path("seccomp profile path", self.seccomp_profile_path)
        _canonical_absolute_path("evaluator executable", self.evaluator_executable)
        if Path(self.docker_config_directory).parent != Path(self.state_directory):
            raise GateProviderError("Docker config directory must be directly under the state directory")
        if Path(self.seccomp_profile_path).parent != Path(self.state_directory):
            raise GateProviderError("seccomp profile must be directly under the state directory")
        if _RUNTIME_NAME.fullmatch(self.container_runtime) is None:
            raise GateProviderError("container runtime must be a canonical Docker runtime name")
        for label, value in (
            ("runtime device", self.runtime_device),
            ("runtime inode", self.runtime_inode),
            ("runtime size", self.runtime_size),
            ("Docker socket device", self.docker_socket_device),
            ("Docker socket inode", self.docker_socket_inode),
            ("state directory device", self.state_directory_device),
            ("state directory inode", self.state_directory_inode),
            ("Docker config device", self.docker_config_device),
            ("Docker config inode", self.docker_config_inode),
            ("seccomp profile device", self.seccomp_profile_device),
            ("seccomp profile inode", self.seccomp_profile_inode),
            ("seccomp profile size", self.seccomp_profile_size),
        ):
            _positive_integer(label, value)
        for label, value in (
            ("runtime owner uid", self.runtime_owner_uid),
            ("Docker socket owner uid", self.docker_socket_owner_uid),
            ("Docker socket owner gid", self.docker_socket_owner_gid),
            ("state directory owner uid", self.state_directory_owner_uid),
            ("Docker config owner uid", self.docker_config_owner_uid),
            ("seccomp profile owner uid", self.seccomp_profile_owner_uid),
        ):
            _nonnegative_integer(label, value)
        _permission_mode("runtime mode", self.runtime_mode)
        if self.runtime_mode & 0o111 == 0 or self.runtime_mode & 0o022 or self.runtime_mode & 0o7000:
            raise GateProviderError("runtime mode must be executable and not group/world writable")
        _permission_mode("Docker socket mode", self.docker_socket_mode)
        if self.docker_socket_mode & 0o002:
            raise GateProviderError("Docker socket mode must not be world writable")
        for label, value in (
            ("runtime executable SHA-256", self.runtime_executable_sha256),
            ("runtime fingerprint SHA-256", self.runtime_fingerprint_sha256),
            ("runtime bundle SHA-256", self.runtime_bundle_sha256),
            ("seccomp profile SHA-256", self.seccomp_profile_sha256),
            ("seccomp policy SHA-256", self.seccomp_policy_sha256),
        ):
            _sha256(label, value)
        _canonical_text("image reference", self.image_reference)
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
            "docker_config_device": self.docker_config_device,
            "docker_config_directory": self.docker_config_directory,
            "docker_config_inode": self.docker_config_inode,
            "docker_config_owner_uid": self.docker_config_owner_uid,
            "docker_host": self.docker_host,
            "docker_daemon_id": self.docker_daemon_id,
            "docker_socket_device": self.docker_socket_device,
            "docker_socket_inode": self.docker_socket_inode,
            "docker_socket_mode": self.docker_socket_mode,
            "docker_socket_owner_gid": self.docker_socket_owner_gid,
            "docker_socket_owner_uid": self.docker_socket_owner_uid,
            "evaluator_schema": self.evaluator_schema,
            "evaluator_executable": self.evaluator_executable,
            "container_runtime": self.container_runtime,
            "image_id": self.image_id,
            "image_reference": self.image_reference,
            "limits": self.limits.as_dict(),
            "platform": self.platform,
            "policy": self.policy,
            "runtime_bundle_sha256": self.runtime_bundle_sha256,
            "runtime_device": self.runtime_device,
            "runtime_executable_sha256": self.runtime_executable_sha256,
            "runtime_fingerprint_sha256": self.runtime_fingerprint_sha256,
            "runtime_inode": self.runtime_inode,
            "runtime_mode": self.runtime_mode,
            "runtime_owner_uid": self.runtime_owner_uid,
            "runtime_path": self.runtime_path,
            "runtime_size": self.runtime_size,
            "schema": self.schema,
            "seccomp_profile_device": self.seccomp_profile_device,
            "seccomp_profile_inode": self.seccomp_profile_inode,
            "seccomp_profile_owner_uid": self.seccomp_profile_owner_uid,
            "seccomp_profile_path": self.seccomp_profile_path,
            "seccomp_profile_sha256": self.seccomp_profile_sha256,
            "seccomp_policy_sha256": self.seccomp_policy_sha256,
            "seccomp_profile_size": self.seccomp_profile_size,
            "state_directory": self.state_directory,
            "state_directory_device": self.state_directory_device,
            "state_directory_inode": self.state_directory_inode,
            "state_directory_owner_uid": self.state_directory_owner_uid,
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
            "docker_config_device",
            "docker_config_directory",
            "docker_config_inode",
            "docker_config_owner_uid",
            "docker_host",
            "docker_daemon_id",
            "docker_socket_device",
            "docker_socket_inode",
            "docker_socket_mode",
            "docker_socket_owner_gid",
            "docker_socket_owner_uid",
            "evaluator_schema",
            "evaluator_executable",
            "container_runtime",
            "image_id",
            "image_reference",
            "limits",
            "platform",
            "policy",
            "runtime_bundle_sha256",
            "runtime_device",
            "runtime_executable_sha256",
            "runtime_fingerprint_sha256",
            "runtime_inode",
            "runtime_mode",
            "runtime_owner_uid",
            "runtime_path",
            "runtime_size",
            "schema",
            "seccomp_profile_device",
            "seccomp_profile_inode",
            "seccomp_profile_owner_uid",
            "seccomp_profile_path",
            "seccomp_profile_sha256",
            "seccomp_policy_sha256",
            "seccomp_profile_size",
            "state_directory",
            "state_directory_device",
            "state_directory_inode",
            "state_directory_owner_uid",
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


def discover_docker_gate_provider(
    *,
    docker_executable: str | Path,
    expected_runtime_executable_sha256: str,
    expected_daemon_id: str,
    state_directory: str | Path,
    docker_config_directory: str | Path,
    image_reference: str,
    platform: str,
    runtime_bundle_sha256: str,
    seccomp_profile_path: str | Path,
    expected_seccomp_profile_sha256: str,
    evaluator_executable: str,
    container_runtime: str,
    user: str,
    limits: DockerSandboxLimits,
    command_timeout_seconds: float = _DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    command_output_bytes: int = _MAX_INSPECT_BYTES,
    runner: DockerDiscoveryRunner | None = None,
) -> DockerGateProviderConfig:
    """Discover a provider against caller-supplied, out-of-band trust anchors.

    The expected executable, daemon, and seccomp file identities must not
    be derived from the local subjects inspected by this function in production.
    """

    timeout = _positive_finite("Docker discovery timeout", command_timeout_seconds)
    _positive_integer("Docker discovery output byte limit", command_output_bytes)
    if command_output_bytes > _MAX_INSPECT_BYTES:
        raise GateProviderError("Docker discovery output byte limit exceeds the hard maximum")
    _sha256("trusted Docker executable SHA-256", expected_runtime_executable_sha256)
    _canonical_ascii_text("trusted Docker daemon ID", expected_daemon_id)
    _sha256("trusted seccomp profile SHA-256", expected_seccomp_profile_sha256)
    _sha256("runtime bundle SHA-256", runtime_bundle_sha256)
    _canonical_text("image reference", image_reference)
    if _IMAGE.fullmatch(image_reference) is None:
        raise GateProviderError("image reference must contain an immutable sha256 digest")
    if _PLATFORM.fullmatch(platform) is None:
        raise GateProviderError("platform must be an exact Linux OCI platform")
    if _RUNTIME_NAME.fullmatch(container_runtime) is None:
        raise GateProviderError("container runtime must be a canonical Docker runtime name")
    _canonical_absolute_path("evaluator executable", evaluator_executable)
    if not isinstance(limits, DockerSandboxLimits):
        raise GateProviderError("Docker sandbox limits must use DockerSandboxLimits")

    bindings = _bind_discovery_files(
        docker_executable=docker_executable,
        state_directory=state_directory,
        docker_config_directory=docker_config_directory,
        seccomp_profile_path=seccomp_profile_path,
    )
    if not hmac.compare_digest(bindings.runtime.sha256, expected_runtime_executable_sha256):
        raise GateProviderError("Docker executable does not match the trusted Docker executable SHA-256")
    if not hmac.compare_digest(bindings.seccomp.sha256, expected_seccomp_profile_sha256):
        raise GateProviderError("seccomp profile does not match the trusted seccomp profile SHA-256")
    selected_runner = _bounded_docker_command if runner is None else runner
    common = (
        bindings.runtime_path,
        f"--config={bindings.docker_config_directory}",
    )
    context = _docker_discovery_json(
        "Docker context inspection",
        (
            *common,
            "context",
            "inspect",
            "default",
            "--format",
            "{{json .Endpoints.docker.Host}}",
        ),
        bindings=bindings,
        runner=selected_runner,
        timeout_seconds=timeout,
        output_bytes_limit=command_output_bytes,
    )
    if not isinstance(context, str):
        raise GateProviderError("Docker context must return one Unix socket URL")
    socket_path = _docker_socket_path(context)
    socket_identity = _inspect_docker_socket(socket_path)
    bindings = replace(bindings, docker_host=context, socket=socket_identity)
    host = f"--host={context}"
    version = _docker_discovery_json(
        "Docker version inspection",
        (*common, host, "version", "--format", "{{json .}}"),
        bindings=bindings,
        runner=selected_runner,
        timeout_seconds=timeout,
        output_bytes_limit=command_output_bytes,
    )
    info = _docker_discovery_json(
        "Docker daemon inspection",
        (*common, host, "info", "--format", "{{json .}}"),
        bindings=bindings,
        runner=selected_runner,
        timeout_seconds=timeout,
        output_bytes_limit=command_output_bytes,
    )
    version_object = _json_object(version, "Docker version inspection")
    info_object = _json_object(info, "Docker daemon inspection")
    runtime_fingerprint = _docker_runtime_fingerprint(
        docker_host=context,
        expected_daemon_id=expected_daemon_id,
        platform=platform,
        container_runtime=container_runtime,
        version=version_object,
        info=info_object,
    )
    image = _docker_discovery_json(
        "Docker image inspection",
        (
            *common,
            host,
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            image_reference,
        ),
        bindings=bindings,
        runner=selected_runner,
        timeout_seconds=timeout,
        output_bytes_limit=command_output_bytes,
    )
    image_object = _json_object(image, "Docker image inspection")
    image_id = _validate_gate_image(
        image_object,
        image_reference=image_reference,
        platform=platform,
        runtime_bundle_sha256=runtime_bundle_sha256,
    )
    assert bindings.socket is not None
    return DockerGateProviderConfig(
        runtime_path=bindings.runtime_path,
        runtime_device=bindings.runtime.device,
        runtime_inode=bindings.runtime.inode,
        runtime_mode=bindings.runtime.mode,
        runtime_size=bindings.runtime.size,
        runtime_owner_uid=bindings.runtime.owner_uid,
        runtime_executable_sha256=bindings.runtime.sha256,
        runtime_fingerprint_sha256=runtime_fingerprint,
        docker_host=context,
        docker_daemon_id=expected_daemon_id,
        docker_socket_device=bindings.socket.device,
        docker_socket_inode=bindings.socket.inode,
        docker_socket_mode=bindings.socket.mode,
        docker_socket_owner_uid=bindings.socket.owner_uid,
        docker_socket_owner_gid=bindings.socket.owner_gid,
        state_directory=bindings.state_directory,
        state_directory_device=bindings.state.device,
        state_directory_inode=bindings.state.inode,
        state_directory_owner_uid=bindings.state.owner_uid,
        docker_config_directory=bindings.docker_config_directory,
        docker_config_device=bindings.docker_config.device,
        docker_config_inode=bindings.docker_config.inode,
        docker_config_owner_uid=bindings.docker_config.owner_uid,
        image_reference=image_reference,
        image_id=image_id,
        platform=platform,
        runtime_bundle_sha256=runtime_bundle_sha256,
        seccomp_profile_path=bindings.seccomp_profile_path,
        seccomp_profile_device=bindings.seccomp.device,
        seccomp_profile_inode=bindings.seccomp.inode,
        seccomp_profile_size=bindings.seccomp.size,
        seccomp_profile_owner_uid=bindings.seccomp.owner_uid,
        seccomp_profile_sha256=bindings.seccomp.sha256,
        seccomp_policy_sha256=bindings.seccomp_policy_sha256,
        evaluator_executable=evaluator_executable,
        container_runtime=container_runtime,
        user=user,
        limits=limits,
    )


def revalidate_docker_gate_provider(
    config: DockerGateProviderConfig,
    *,
    command_timeout_seconds: float = _DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
    command_output_bytes: int = _MAX_INSPECT_BYTES,
    runner: DockerDiscoveryRunner | None = None,
) -> DockerGateProviderConfig:
    """Reprobe a bound provider; only a semantically identical socket replacement is accepted."""

    if not isinstance(config, DockerGateProviderConfig):
        raise GateProviderError("Docker provider revalidation requires a provider config")
    _revalidate_static_provider_files(config)

    refreshed = discover_docker_gate_provider(
        docker_executable=config.runtime_path,
        expected_runtime_executable_sha256=config.runtime_executable_sha256,
        expected_daemon_id=config.docker_daemon_id,
        state_directory=config.state_directory,
        docker_config_directory=config.docker_config_directory,
        image_reference=config.image_reference,
        platform=config.platform,
        runtime_bundle_sha256=config.runtime_bundle_sha256,
        seccomp_profile_path=config.seccomp_profile_path,
        expected_seccomp_profile_sha256=config.seccomp_profile_sha256,
        evaluator_executable=config.evaluator_executable,
        container_runtime=config.container_runtime,
        user=config.user,
        limits=config.limits,
        command_timeout_seconds=command_timeout_seconds,
        command_output_bytes=command_output_bytes,
        runner=runner,
    )
    if refreshed.runtime_fingerprint_sha256 != config.runtime_fingerprint_sha256:
        raise GateProviderError("Docker runtime semantic fingerprint changed")
    if refreshed.image_id != config.image_id:
        raise GateProviderError("Docker gate image identity changed")
    old = config.as_dict()
    new = refreshed.as_dict()
    for field in ("docker_socket_device", "docker_socket_inode"):
        old.pop(field)
        new.pop(field)
    if old != new:
        raise GateProviderError("Docker gate provider identity changed during revalidation")
    return refreshed


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
    result_bytes_limit: int
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
            if value == "0" * len(value):
                raise GateProviderError(f"{label} must not be the all-zero object ID")
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
        _positive_integer("gate result byte limit", self.result_bytes_limit)

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
            "result_bytes_limit": self.result_bytes_limit,
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
            "result_bytes_limit",
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
        key for key in labels if key.startswith(_AUTOFORM_LABEL_PREFIX) and key not in expected_labels
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
        if name in _FORBIDDEN_CONTAINER_ENVIRONMENT or _CREDENTIAL_ENVIRONMENT_FRAGMENT.search(name.upper()) is not None
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
    if request.result_bytes_limit != config.limits.output_bytes:
        raise GateProviderError("gate invocation does not bind the configured result limit")
    encoded_request = base64.urlsafe_b64encode(request.evidence_bytes()).decode("ascii")
    limits = config.limits
    mount = f"type=bind,source={pack},target=/autoform/input/repository.pack,readonly,bind-recursive=disabled"
    arguments = [
        config.runtime_path,
        f"--config={config.docker_config_directory}",
        f"--host={config.docker_host}",
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
            "--ipc",
            "none",
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
            f"/autoform/work:{_work_tmpfs_options(config)}",
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


def _bind_discovery_files(
    *,
    docker_executable: str | Path,
    state_directory: str | Path,
    docker_config_directory: str | Path,
    seccomp_profile_path: str | Path,
) -> _DiscoveryBindings:
    runtime_path = _resolve_docker_executable(docker_executable)
    state_path = _canonical_real_path(state_directory, label="state directory")
    config_path = _canonical_real_path(
        docker_config_directory,
        label="Docker config directory",
    )
    seccomp_path = _canonical_real_path(seccomp_profile_path, label="seccomp profile")
    if config_path.parent != state_path:
        raise GateProviderError("Docker config directory must be directly under the state directory")
    if seccomp_path.parent != state_path:
        raise GateProviderError("seccomp profile must be directly under the state directory")
    state = _inspect_private_directory(state_path, label="state directory", empty=False)
    docker_config = _inspect_private_directory(
        config_path,
        label="Docker config directory",
        empty=True,
    )
    seccomp, seccomp_policy_sha256 = _inspect_seccomp_profile(
        seccomp_path,
    )
    runtime = _inspect_regular_file(
        Path(runtime_path),
        label="Docker executable",
        maximum=_MAX_EXECUTABLE_BYTES,
        exact_mode=None,
        single_link=False,
        executable=True,
    )
    return _DiscoveryBindings(
        runtime_path=runtime_path,
        runtime=runtime,
        state_directory=os.fspath(state_path),
        state=state,
        docker_config_directory=os.fspath(config_path),
        docker_config=docker_config,
        seccomp_profile_path=os.fspath(seccomp_path),
        seccomp=seccomp,
        seccomp_policy_sha256=seccomp_policy_sha256,
    )


def _resolve_docker_executable(value: str | Path) -> str:
    try:
        requested = os.fspath(value)
    except TypeError as error:
        raise GateProviderError("Docker executable must be an explicit absolute path") from error
    if not isinstance(requested, str) or not requested or requested != requested.strip():
        raise GateProviderError("Docker executable must be an explicit absolute path")
    if not os.path.isabs(requested):
        raise GateProviderError("Docker executable must be an explicit absolute path")
    _canonical_absolute_path("Docker executable", requested)
    try:
        resolved = os.path.realpath(requested, strict=True)
    except (OSError, ValueError) as error:
        raise GateProviderError("Docker executable cannot be resolved safely") from error
    _canonical_absolute_path("resolved Docker executable", resolved)
    return resolved


def _canonical_real_path(value: str | Path, *, label: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as error:
        raise GateProviderError(f"{label} must be one canonical non-symbolic absolute path") from error
    try:
        _canonical_absolute_path(label, raw)
        resolved = os.path.realpath(raw, strict=True)
    except (OSError, ValueError) as error:
        raise GateProviderError(f"{label} cannot be resolved safely") from error
    if resolved != raw:
        raise GateProviderError(f"{label} must be one canonical non-symbolic absolute path")
    return Path(raw)


def _inspect_regular_file(
    path: Path,
    *,
    label: str,
    maximum: int,
    exact_mode: int | None,
    single_link: bool,
    executable: bool,
) -> _RegularFileIdentity:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    permissions = stat.S_IMODE(before.st_mode)
    if not stat.S_ISREG(before.st_mode):
        raise GateProviderError(f"{label} must be one regular file")
    if before.st_uid not in {0, os.geteuid()}:
        raise GateProviderError(f"{label} must be owned by root or the current user")
    if before.st_size <= 0 or before.st_size > maximum:
        raise GateProviderError(f"{label} has an invalid size")
    if exact_mode is not None and permissions != exact_mode:
        raise GateProviderError(f"{label} must have mode {exact_mode:04o}")
    if executable and (permissions & 0o111 == 0 or permissions & 0o022 or permissions & 0o7000):
        raise GateProviderError(f"{label} must be executable and not group/world writable")
    if executable and not os.access(path, os.X_OK):
        raise GateProviderError(f"{label} is not executable by the current process")
    if single_link and before.st_nlink != 1:
        raise GateProviderError(f"{label} must have exactly one hard link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    digest = hashlib.sha256()
    read_failed = False
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stat_file_identity(opened) != _stat_file_identity(before):
            raise GateProviderError(f"{label} changed while it was opened")
        size = 0
        while chunk := os.read(descriptor, min(1024 * 1024, maximum + 1 - size)):
            size += len(chunk)
            if size > maximum:
                raise GateProviderError(f"{label} exceeds its configured byte limit")
            digest.update(chunk)
        if size != before.st_size:
            raise GateProviderError(f"{label} changed while it was read")
        if _stat_file_identity(os.fstat(descriptor)) != _stat_file_identity(before):
            raise GateProviderError(f"{label} changed while it was read")
    except GateProviderError:
        read_failed = True
        raise
    except OSError as error:
        read_failed = True
        raise GateProviderError(f"{label} cannot be read safely") from error
    except BaseException:
        read_failed = True
        raise
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                if not read_failed:
                    raise GateProviderError(f"{label} could not be closed") from error
    try:
        after = os.lstat(path)
    except OSError as error:
        raise GateProviderError(f"{label} changed while it was read") from error
    if _stat_file_identity(after) != _stat_file_identity(before):
        raise GateProviderError(f"{label} changed while it was read")
    return _RegularFileIdentity(
        device=before.st_dev,
        inode=before.st_ino,
        mode=permissions,
        size=before.st_size,
        owner_uid=before.st_uid,
        owner_gid=before.st_gid,
        links=before.st_nlink,
        sha256=digest.hexdigest(),
    )


def _inspect_seccomp_profile(path: Path) -> tuple[_RegularFileIdentity, str]:
    identity = _inspect_regular_file(
        path,
        label="seccomp profile",
        maximum=_MAX_SECCOMP_BYTES,
        exact_mode=0o444,
        single_link=True,
        executable=False,
    )
    content = _read_bound_regular_file(
        path,
        expected=identity,
        maximum=_MAX_SECCOMP_BYTES,
        label="seccomp profile",
    )
    return identity, _canonical_seccomp_policy_sha256(content)


def _read_bound_regular_file(
    path: Path,
    *,
    expected: _RegularFileIdentity,
    maximum: int,
    label: str,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    content = bytearray()
    read_failed = False
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not _matches_regular_file_identity(opened, expected):
            raise GateProviderError(f"{label} changed while it was opened")
        while chunk := os.read(descriptor, min(1024 * 1024, maximum + 1 - len(content))):
            content.extend(chunk)
            if len(content) > maximum:
                raise GateProviderError(f"{label} exceeds its configured byte limit")
        if not _matches_regular_file_identity(os.fstat(descriptor), expected):
            raise GateProviderError(f"{label} changed while it was read")
    except GateProviderError:
        read_failed = True
        raise
    except OSError as error:
        read_failed = True
        raise GateProviderError(f"{label} cannot be read safely") from error
    except BaseException:
        read_failed = True
        raise
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as error:
                if not read_failed:
                    raise GateProviderError(f"{label} could not be closed") from error
    result = bytes(content)
    if not hmac.compare_digest(hashlib.sha256(result).hexdigest(), expected.sha256):
        raise GateProviderError(f"{label} changed while it was read")
    observed = _inspect_regular_file(
        path,
        label=label,
        maximum=maximum,
        exact_mode=expected.mode,
        single_link=expected.links == 1,
        executable=False,
    )
    if observed != expected:
        raise GateProviderError(f"{label} changed while it was read")
    return result


def _matches_regular_file_identity(
    value: os.stat_result,
    expected: _RegularFileIdentity,
) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_dev == expected.device
        and value.st_ino == expected.inode
        and stat.S_IMODE(value.st_mode) == expected.mode
        and value.st_size == expected.size
        and value.st_uid == expected.owner_uid
        and value.st_gid == expected.owner_gid
        and value.st_nlink == expected.links
    )


def _canonical_seccomp_policy_sha256(content: bytes) -> str:
    policy = _strict_json_bytes(
        content,
        label="seccomp profile",
        maximum=_MAX_SECCOMP_BYTES,
    )
    default_action = policy.get("defaultAction")
    if not isinstance(default_action, str) or default_action not in _SECCOMP_DENY_ACTIONS:
        raise GateProviderError("seccomp profile must have a deny-by-default action")
    if "listenerPath" in policy or "listenerMetadata" in policy:
        raise GateProviderError("seccomp profile must not delegate decisions to a listener")
    rules = policy.get("syscalls")
    if not isinstance(rules, list):
        raise GateProviderError("seccomp profile must contain one syscall rule list")
    for rule in rules:
        action = rule.get("action") if isinstance(rule, dict) else None
        if not isinstance(action, str) or action not in _SECCOMP_RULE_ACTIONS:
            raise GateProviderError("seccomp profile contains an unsafe syscall action")
        names = rule.get("names")
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or _SYSCALL_NAME.fullmatch(name) is None for name in names)
            or len(set(names)) != len(names)
        ):
            raise GateProviderError("seccomp profile contains invalid syscall names")
    return hashlib.sha256(_json_bytes(policy)).hexdigest()


def _stat_file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _inspect_private_directory(
    path: Path,
    *,
    label: str,
    empty: bool,
) -> _DirectoryIdentity:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise GateProviderError(f"{label} cannot be inspected") from error
    if not stat.S_ISDIR(before.st_mode):
        raise GateProviderError(f"{label} must be one real directory")
    if before.st_uid != os.geteuid():
        raise GateProviderError(f"{label} must be owned by the current user")
    if stat.S_IMODE(before.st_mode) != 0o700:
        raise GateProviderError(f"{label} must have mode 0700")
    if empty:
        try:
            with os.scandir(path) as entries:
                if next(entries, None) is not None:
                    raise GateProviderError(f"{label} must be empty")
        except GateProviderError:
            raise
        except OSError as error:
            raise GateProviderError(f"{label} cannot be inspected") from error
    try:
        after = os.lstat(path)
    except OSError as error:
        raise GateProviderError(f"{label} changed while it was inspected") from error
    if _stat_directory_identity(after) != _stat_directory_identity(before):
        raise GateProviderError(f"{label} changed while it was inspected")
    return _DirectoryIdentity(
        device=before.st_dev,
        inode=before.st_ino,
        owner_uid=before.st_uid,
    )


def _stat_directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _docker_socket_path(host: object) -> Path:
    return Path(_docker_socket_path_text(host))


def _docker_socket_path_text(host: object) -> str:
    if not isinstance(host, str) or not host or host != host.strip() or "%" in host:
        raise GateProviderError("Docker host must name one canonical local Unix socket")
    try:
        parsed = urlsplit(host)
    except ValueError as error:
        raise GateProviderError("Docker host must name one canonical local Unix socket") from error
    if (
        parsed.scheme != "unix"
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or host != f"unix://{parsed.path}"
        or os.path.normpath(parsed.path) != parsed.path
    ):
        raise GateProviderError("Docker host must name one canonical local Unix socket")
    _canonical_absolute_path("Docker Unix socket", parsed.path)
    return parsed.path


def _inspect_docker_socket(path: Path) -> _SocketIdentity:
    try:
        value = os.lstat(path)
    except OSError as error:
        raise GateProviderError("Docker Unix socket cannot be inspected") from error
    if not stat.S_ISSOCK(value.st_mode):
        raise GateProviderError("Docker Unix socket path is not a socket")
    if value.st_uid not in {0, os.geteuid()}:
        raise GateProviderError("Docker Unix socket must be owned by root or the current user")
    if stat.S_IMODE(value.st_mode) & 0o002:
        raise GateProviderError("Docker Unix socket must not be world writable")
    return _SocketIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        mode=stat.S_IMODE(value.st_mode),
        owner_uid=value.st_uid,
        owner_gid=value.st_gid,
    )


def _docker_discovery_json(
    label: str,
    argv: tuple[str, ...],
    *,
    bindings: _DiscoveryBindings,
    runner: DockerDiscoveryRunner,
    timeout_seconds: float,
    output_bytes_limit: int,
) -> object:
    _revalidate_discovery_bindings(bindings)
    result: DockerCommandResult | None = None
    command_error: Exception | None = None
    started = time.monotonic()
    try:
        result = runner(
            argv,
            env=_docker_discovery_environment(bindings.docker_config_directory),
            timeout_seconds=timeout_seconds,
            output_bytes_limit=output_bytes_limit,
        )
    except Exception as error:
        command_error = error
    if command_error is None and time.monotonic() - started > timeout_seconds:
        command_error = subprocess.TimeoutExpired(list(argv), timeout_seconds)
    try:
        _revalidate_discovery_bindings(bindings)
    except GateProviderError as error:
        if command_error is not None:
            raise error from command_error
        raise
    if command_error is not None:
        if isinstance(command_error, subprocess.TimeoutExpired):
            raise GateProviderError(f"{label} timed out") from command_error
        if isinstance(command_error, GateProviderError):
            raise command_error
        raise GateProviderError(f"{label} could not be executed safely") from command_error
    if not isinstance(result, DockerCommandResult):
        raise GateProviderError(f"{label} returned an invalid command result")
    if (
        not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
        or type(result.returncode) is not int
    ):
        raise GateProviderError(f"{label} returned an invalid command result")
    if len(result.stdout) + len(result.stderr) > output_bytes_limit:
        raise GateProviderError(f"{label} exceeded its output limit")
    if result.returncode != 0:
        raise GateProviderError(f"{label} failed with exit {result.returncode}")
    if result.stderr:
        raise GateProviderError(f"{label} returned unexpected stderr")
    return _strict_discovery_json(result.stdout, label=label)


def _revalidate_discovery_bindings(bindings: _DiscoveryBindings) -> None:
    checks: tuple[tuple[str, Callable[[], object], object], ...] = (
        (
            "Docker executable",
            lambda: _inspect_regular_file(
                Path(bindings.runtime_path),
                label="Docker executable",
                maximum=_MAX_EXECUTABLE_BYTES,
                exact_mode=None,
                single_link=False,
                executable=True,
            ),
            bindings.runtime,
        ),
        (
            "state directory",
            lambda: _inspect_private_directory(
                Path(bindings.state_directory),
                label="state directory",
                empty=False,
            ),
            bindings.state,
        ),
        (
            "Docker config directory",
            lambda: _inspect_private_directory(
                Path(bindings.docker_config_directory),
                label="Docker config directory",
                empty=True,
            ),
            bindings.docker_config,
        ),
        (
            "seccomp profile",
            lambda: _inspect_seccomp_profile(Path(bindings.seccomp_profile_path)),
            (bindings.seccomp, bindings.seccomp_policy_sha256),
        ),
    )
    for label, inspect, expected in checks:
        try:
            actual = inspect()
        except GateProviderError as error:
            raise GateProviderError(f"{label} changed during Docker discovery") from error
        if actual != expected:
            raise GateProviderError(f"{label} changed during Docker discovery")
    if bindings.docker_host is not None:
        assert bindings.socket is not None
        try:
            actual_socket = _inspect_docker_socket(_docker_socket_path(bindings.docker_host))
        except GateProviderError as error:
            raise GateProviderError("Docker socket changed during Docker discovery") from error
        if actual_socket != bindings.socket:
            raise GateProviderError("Docker socket changed during Docker discovery")


def _docker_discovery_environment(docker_config_directory: str) -> dict[str, str]:
    return {
        "DOCKER_CONFIG": docker_config_directory,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }


def _strict_discovery_json(content: bytes, *, label: str) -> object:
    if not content or not content.endswith(b"\n") or content.endswith(b"\n\n"):
        raise GateProviderError(f"{label} did not return one JSON line")
    payload = content[:-1]
    if not payload or payload != payload.strip() or b"\n" in payload or b"\r" in payload:
        raise GateProviderError(f"{label} did not return one JSON line")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise GateProviderError(f"{label} returned non-UTF-8 output") from error
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda constant: _raise_json_constant(constant),
        )
    except (RecursionError, ValueError, TypeError) as error:
        raise GateProviderError(f"{label} did not return strict JSON") from error


def _json_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GateProviderError(f"{label} must return one object")
    return value


def _docker_runtime_fingerprint(
    *,
    docker_host: str,
    expected_daemon_id: str,
    platform: str,
    container_runtime: str,
    version: dict[str, Any],
    info: dict[str, Any],
) -> str:
    client = _required_object(version, "Client", "Docker client version")
    server = _required_object(version, "Server", "Docker server version")
    client_api = _required_string(client, "ApiVersion", "Docker client API version")
    server_api = _required_string(server, "ApiVersion", "Docker server API version")
    _minimum_api_version(client_api, label="Docker client API version")
    _minimum_api_version(server_api, label="Docker server API version")
    server_os = _required_canonical_string(
        server,
        "Os",
        "Docker server operating system",
    )
    server_arch = _required_canonical_string(
        server,
        "Arch",
        "Docker server architecture",
    )
    if server_os != "linux":
        raise GateProviderError("Docker provider requires a Linux server")
    expected_os, expected_arch, *_variant = platform.split("/")
    if server_os != expected_os or server_arch != expected_arch:
        raise GateProviderError("Docker server does not match the native platform")
    if _required_canonical_string(info, "OSType", "Docker daemon operating system") != server_os:
        raise GateProviderError("Docker daemon does not match the native platform")
    daemon_arch = _required_canonical_string(
        info,
        "Architecture",
        "Docker daemon architecture",
    )
    if _DOCKER_ARCHITECTURES.get(daemon_arch, daemon_arch) != server_arch:
        raise GateProviderError("Docker daemon does not match the native platform")
    server_version = _required_canonical_string(server, "Version", "Docker server version")
    if _required_canonical_string(info, "ServerVersion", "Docker daemon version") != server_version:
        raise GateProviderError("Docker version and daemon evidence disagree")
    runtimes = _required_object(info, "Runtimes", "Docker daemon runtimes")
    if container_runtime not in runtimes or not isinstance(runtimes[container_runtime], dict):
        raise GateProviderError("Docker daemon does not provide the selected runtime")
    security_options = info.get("SecurityOptions")
    if (
        not isinstance(security_options, list)
        or not security_options
        or any(not isinstance(option, str) for option in security_options)
        or len(set(security_options)) != len(security_options)
        or not any(option == "name=seccomp" or option.startswith("name=seccomp,") for option in security_options)
    ):
        raise GateProviderError("Docker daemon does not advertise seccomp")
    for option in security_options:
        _canonical_text("Docker security option", option)
    daemon_id = _required_canonical_string(info, "ID", "Docker daemon ID")
    _canonical_ascii_text("Docker daemon ID", daemon_id)
    if not hmac.compare_digest(daemon_id, expected_daemon_id):
        raise GateProviderError("Docker daemon does not match the trusted Docker daemon ID")
    driver = _required_canonical_string(info, "Driver", "Docker image storage driver")
    docker_root = _required_string(info, "DockerRootDir", "Docker image store root")
    _canonical_absolute_path("Docker image store root", docker_root)
    evidence = {
        "docker_host": docker_host,
        "image_store": {
            "driver": driver,
            "root": docker_root,
        },
        "runtime": {
            "definition": runtimes[container_runtime],
            "name": container_runtime,
        },
        "schema": DOCKER_RUNTIME_FINGERPRINT_SCHEMA,
        "security_options": sorted(security_options),
        "server": {
            "api_version": server_api,
            "architecture": server_arch,
            "cgroup_driver": _required_canonical_string(
                info,
                "CgroupDriver",
                "Docker cgroup driver",
            ),
            "cgroup_version": _required_canonical_string(
                info,
                "CgroupVersion",
                "Docker cgroup version",
            ),
            "daemon_architecture": daemon_arch,
            "default_runtime": _required_canonical_string(
                info,
                "DefaultRuntime",
                "Docker default runtime",
            ),
            "git_commit": _required_canonical_string(
                server,
                "GitCommit",
                "Docker server Git commit",
            ),
            "id": daemon_id,
            "kernel_version": _required_canonical_string(
                info,
                "KernelVersion",
                "Docker kernel version",
            ),
            "operating_system": _required_canonical_string(
                info,
                "OperatingSystem",
                "Docker operating system",
            ),
            "os": server_os,
            "version": server_version,
        },
    }
    return hashlib.sha256(_json_bytes(evidence)).hexdigest()


def _minimum_api_version(value: str, *, label: str) -> None:
    if _DOCKER_API_VERSION.fullmatch(value) is None:
        raise GateProviderError(f"{label} is invalid")
    major, minor = (int(component) for component in value.split(".", 1))
    if (major, minor) < _MINIMUM_DOCKER_API_VERSION:
        raise GateProviderError(f"{label} must be at least 1.48")


def _validate_gate_image(
    image: dict[str, Any],
    *,
    image_reference: str,
    platform: str,
    runtime_bundle_sha256: str,
) -> str:
    image_id = _required_string(image, "Id", "Docker image ID")
    if not image_id.startswith("sha256:"):
        raise GateProviderError("Docker image ID must use sha256")
    _sha256("Docker image ID", image_id.removeprefix("sha256:"))
    digests = image.get("RepoDigests")
    if (
        not isinstance(digests, list)
        or not digests
        or any(not isinstance(digest, str) or _IMAGE.fullmatch(digest) is None for digest in digests)
        or len(set(digests)) != len(digests)
        or image_reference not in digests
    ):
        raise GateProviderError("Docker image must include the requested repository digest")
    image_os = _required_string(image, "Os", "Docker image operating system")
    image_arch = _required_string(image, "Architecture", "Docker image architecture")
    variant = image.get("Variant", "")
    if not isinstance(variant, str):
        raise GateProviderError("Docker image variant must be text")
    observed_platform = f"{image_os}/{image_arch}" + (f"/{variant}" if variant else "")
    if observed_platform != platform:
        raise GateProviderError("Docker image does not match the native platform")
    container = _required_object(image, "Config", "Docker image config")
    volumes = container.get("Volumes")
    if volumes not in (None, {}):
        raise GateProviderError("Docker gate image must not declare volumes")
    labels = container.get("Labels")
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in labels.items()
    ):
        raise GateProviderError("Docker gate image labels are invalid")
    expected = {
        _RUNTIME_BUNDLE_SCHEMA_LABEL: DOCKER_RUNTIME_BUNDLE_SCHEMA,
        _RUNTIME_BUNDLE_SHA256_LABEL: runtime_bundle_sha256,
    }
    observed = {key: value for key, value in labels.items() if key.startswith(_AUTOFORM_LABEL_PREFIX)}
    if observed != expected:
        if observed.get(_RUNTIME_BUNDLE_SCHEMA_LABEL) != DOCKER_RUNTIME_BUNDLE_SCHEMA:
            raise GateProviderError("Docker gate image has the wrong runtime bundle schema")
        if observed.get(_RUNTIME_BUNDLE_SHA256_LABEL) != runtime_bundle_sha256:
            raise GateProviderError("Docker gate image has the wrong runtime bundle digest")
        raise GateProviderError("Docker gate image has unexpected Autoform image labels")
    return image_id


def _revalidate_static_provider_files(config: DockerGateProviderConfig) -> None:
    try:
        runtime_path = _resolve_docker_executable(config.runtime_path)
        runtime = _inspect_regular_file(
            Path(runtime_path),
            label="Docker executable",
            maximum=_MAX_EXECUTABLE_BYTES,
            exact_mode=None,
            single_link=False,
            executable=True,
        )
    except GateProviderError as error:
        raise GateProviderError("Docker executable changed since provider discovery") from error
    if (
        runtime_path != config.runtime_path
        or runtime.device != config.runtime_device
        or runtime.inode != config.runtime_inode
        or runtime.mode != config.runtime_mode
        or runtime.size != config.runtime_size
        or runtime.owner_uid != config.runtime_owner_uid
        or not hmac.compare_digest(runtime.sha256, config.runtime_executable_sha256)
    ):
        raise GateProviderError("Docker executable changed since provider discovery")

    try:
        state_path = _canonical_real_path(config.state_directory, label="state directory")
        state = _inspect_private_directory(state_path, label="state directory", empty=False)
    except GateProviderError as error:
        raise GateProviderError("state directory changed since provider discovery") from error
    if (
        state.device != config.state_directory_device
        or state.inode != config.state_directory_inode
        or state.owner_uid != config.state_directory_owner_uid
    ):
        raise GateProviderError("state directory changed since provider discovery")

    try:
        config_path = _canonical_real_path(
            config.docker_config_directory,
            label="Docker config directory",
        )
        docker_config = _inspect_private_directory(
            config_path,
            label="Docker config directory",
            empty=True,
        )
    except GateProviderError as error:
        raise GateProviderError("Docker config directory changed since provider discovery") from error
    if (
        docker_config.device != config.docker_config_device
        or docker_config.inode != config.docker_config_inode
        or docker_config.owner_uid != config.docker_config_owner_uid
    ):
        raise GateProviderError("Docker config directory changed since provider discovery")

    try:
        seccomp_path = _canonical_real_path(config.seccomp_profile_path, label="seccomp profile")
        seccomp, seccomp_policy_sha256 = _inspect_seccomp_profile(seccomp_path)
    except GateProviderError as error:
        raise GateProviderError("seccomp profile changed since provider discovery") from error
    if (
        seccomp.device != config.seccomp_profile_device
        or seccomp.inode != config.seccomp_profile_inode
        or seccomp.size != config.seccomp_profile_size
        or seccomp.owner_uid != config.seccomp_profile_owner_uid
        or not hmac.compare_digest(seccomp.sha256, config.seccomp_profile_sha256)
        or not hmac.compare_digest(seccomp_policy_sha256, config.seccomp_policy_sha256)
    ):
        raise GateProviderError("seccomp profile changed since provider discovery")


def _bounded_docker_command(
    argv: tuple[str, ...],
    *,
    env: Mapping[str, str],
    timeout_seconds: float,
    output_bytes_limit: int,
) -> DockerCommandResult:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    result: DockerCommandResult | None = None
    primary_error: BaseException | None = None
    cleanup_errors: list[BaseException] = []
    try:
        process = subprocess.Popen(
            list(argv),
            cwd="/",
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            for key, _mask in events:
                remaining_output = output_bytes_limit - len(stdout) - len(stderr)
                chunk = os.read(key.fileobj.fileno(), min(64 * 1024, remaining_output + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if len(stdout) + len(stderr) > output_bytes_limit:
                    raise GateProviderError("Docker discovery command exceeded its output limit")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        returncode = process.wait(timeout=remaining)
        result = DockerCommandResult(bytes(stdout), bytes(stderr), returncode)
    except BaseException as error:
        primary_error = error
    finally:
        try:
            selector.close()
        except BaseException as error:
            cleanup_errors.append(error)
        if process is not None:
            running = True
            try:
                running = process.poll() is None
            except BaseException as error:
                cleanup_errors.append(error)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                if running:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except BaseException as error:
                        cleanup_errors.append(error)
            except BaseException as group_error:
                cleanup_errors.append(group_error)
                if running:
                    try:
                        process.kill()
                    except BaseException as process_error:
                        cleanup_errors.append(process_error)
            try:
                process.wait(timeout=1)
            except BaseException as error:
                cleanup_errors.append(error)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException as error:
                        cleanup_errors.append(error)
    if cleanup_errors:
        raise GateProviderError("Docker discovery command cleanup failed") from (
            primary_error if primary_error is not None else cleanup_errors[0]
        )
    if primary_error is not None:
        raise primary_error
    assert result is not None
    return result


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


def _required_canonical_string(value: dict[str, Any], key: str, label: str) -> str:
    result = _required_string(value, key, label)
    _canonical_text(label, result)
    return result


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
        ("PidMode", "", "PID namespace"),
        ("UTSMode", "", "UTS namespace"),
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
                "/autoform/work": _work_tmpfs_options(config),
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
    if not isinstance(value, list) or len(value) != 2 or any(not isinstance(option, str) for option in value):
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
    if hashlib.sha256(_json_bytes(policy)).hexdigest() != config.seccomp_policy_sha256:
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
        if not isinstance(name, str) or type(soft) is not int or type(hard) is not int or name in observed:
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
                "/autoform/work": {
                    "bytes": limits.scratch_bytes,
                    "gid": int(config.user.split(":", 1)[1]),
                    "mode": "0700",
                    "uid": int(config.user.split(":", 1)[0]),
                },
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
        "seccomp_policy_sha256": config.seccomp_policy_sha256,
    }


def _canonical_absolute_path(label: str, value: object) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise GateProviderError(f"{label} must be a nonempty canonical absolute path") from error
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("//")
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


def _canonical_ascii_text(label: str, value: object) -> None:
    _canonical_text(label, value)
    assert isinstance(value, str)
    try:
        value.encode("ascii")
    except UnicodeEncodeError as error:
        raise GateProviderError(f"{label} must be nonempty canonical ASCII text") from error


def _sha256(label: str, value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise GateProviderError(f"{label} must be lowercase hexadecimal")


def _positive_integer(label: str, value: object) -> None:
    if type(value) is not int or value <= 0:
        raise GateProviderError(f"{label} must be a positive integer")


def _nonnegative_integer(label: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise GateProviderError(f"{label} must be a nonnegative integer")


def _permission_mode(label: str, value: object) -> None:
    if type(value) is not int or value < 0 or value > 0o7777:
        raise GateProviderError(f"{label} must be a Unix permission mode")


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


def _work_tmpfs_options(config: DockerGateProviderConfig) -> str:
    uid, gid = config.user.split(":", 1)
    return f"rw,nosuid,nodev,size={config.limits.scratch_bytes},uid={uid},gid={gid},mode=0700"


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
            parse_constant=lambda constant: _raise_json_constant(constant),
        )
    except (RecursionError, UnicodeError, ValueError, TypeError) as error:
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
    except (RecursionError, TypeError, ValueError) as error:
        raise GateProviderError("Docker gate provider config is not canonical JSON") from error


__all__ = [
    "DOCKER_CREATE_ATTESTATION_SCHEMA",
    "DOCKER_GATE_PROVIDER_SCHEMA",
    "DOCKER_RUNTIME_BUNDLE_SCHEMA",
    "DOCKER_RUNTIME_FINGERPRINT_SCHEMA",
    "DOCKER_SANDBOX_POLICY",
    "GATE_EVALUATOR_SCHEMA",
    "GATE_INVOCATION_SCHEMA",
    "GATE_RESULT_FRAME_SCHEMA",
    "DockerCreateAttestation",
    "DockerCommandResult",
    "DockerDiscoveryRunner",
    "DockerGateProviderConfig",
    "DockerSandboxLimits",
    "GateInvocationRequest",
    "GateProviderError",
    "attest_docker_create",
    "docker_create_argv",
    "discover_docker_gate_provider",
    "encode_gate_result_frame",
    "parse_gate_result_frame",
    "revalidate_docker_gate_provider",
]
