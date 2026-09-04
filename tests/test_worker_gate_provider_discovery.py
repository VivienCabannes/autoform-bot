from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import pytest

import autoform_worker.gate_provider as gate_provider_module
from autoform_worker.gate_provider import (
    DOCKER_RUNTIME_BUNDLE_SCHEMA,
    DockerCommandResult,
    DockerGateProviderConfig,
    DockerSandboxLimits,
    GateProviderError,
    discover_docker_gate_provider,
    revalidate_docker_gate_provider,
)


_IMAGE_DIGEST = "3" * 64
_IMAGE_ID = "sha256:" + "4" * 64
_BUNDLE_DIGEST = "5" * 64


def _json_line(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _limits() -> DockerSandboxLimits:
    return DockerSandboxLimits(
        wall_timeout_seconds=900,
        memory_bytes=8 * 1024**3,
        memory_swap_bytes=8 * 1024**3,
        cpu_nanos=2_000_000_000,
        pids_limit=256,
        scratch_bytes=16 * 1024**3,
        output_bytes=4 * 1024**2,
        nofile_limit=4096,
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seccomp_policy_sha256(path: Path) -> str:
    value = json.loads(path.read_bytes())
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class _Environment:
    docker: Path
    state: Path
    docker_config: Path
    seccomp: Path
    socket_path: Path
    socket_handle: socket.socket


@pytest.fixture
def provider_environment(tmp_path: Path) -> _Environment:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    docker_config = state / "docker-config"
    docker_config.mkdir(mode=0o700)
    seccomp = state / "seccomp.json"
    seccomp.write_bytes(b'{"defaultAction":"SCMP_ACT_ERRNO","syscalls":[]}\n')
    seccomp.chmod(0o444)
    docker = tmp_path / "docker"
    docker.write_bytes(b"#!/bin/sh\nexit 125\n")
    docker.chmod(0o500)
    with tempfile.TemporaryDirectory(prefix="afd-") as socket_directory:
        socket_path = Path(socket_directory) / "docker.sock"
        socket_handle = socket.socket(socket.AF_UNIX)
        socket_handle.bind(os.fspath(socket_path))
        environment = _Environment(
            docker=docker,
            state=state,
            docker_config=docker_config,
            seccomp=seccomp,
            socket_path=socket_path,
            socket_handle=socket_handle,
        )
        try:
            yield environment
        finally:
            environment.socket_handle.close()


def _version(*, api: str = "1.48", os_name: str = "linux") -> dict[str, object]:
    return {
        "Client": {
            "ApiVersion": api,
            "Version": "27.1.1",
        },
        "Server": {
            "ApiVersion": api,
            "Arch": "arm64",
            "GitCommit": "server-commit",
            "KernelVersion": "6.10.0",
            "Os": os_name,
            "Version": "27.1.1",
        },
    }


def _info() -> dict[str, object]:
    return {
        "Architecture": "arm64",
        "CgroupDriver": "cgroupfs",
        "CgroupVersion": "2",
        "DockerRootDir": "/var/lib/docker",
        "Driver": "overlay2",
        "ID": "ABCDEFGHIJKLMNOPQRSTUVWX",
        "KernelVersion": "6.10.0",
        "Name": "builder",
        "OSType": "linux",
        "OperatingSystem": "Linux",
        "Runtimes": {
            "io.containerd.runc.v2": {"path": "runc-v2"},
            "runc": {"path": "runc"},
        },
        "DefaultRuntime": "runc",
        "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"],
        "ServerVersion": "27.1.1",
    }


def _image() -> dict[str, object]:
    return {
        "Architecture": "arm64",
        "Config": {
            "Labels": {
                "org.autoform.runtime-bundle-schema": DOCKER_RUNTIME_BUNDLE_SCHEMA,
                "org.autoform.runtime-bundle-sha256": _BUNDLE_DIGEST,
                "org.opencontainers.image.source": "https://example.invalid/autoform",
            },
            "Volumes": None,
        },
        "Id": _IMAGE_ID,
        "Os": "linux",
        "RepoDigests": [f"autoform-gates@sha256:{_IMAGE_DIGEST}"],
        "Variant": "",
    }


@dataclass
class _Runner:
    environment: _Environment
    version: dict[str, object] = field(default_factory=_version)
    info: dict[str, object] = field(default_factory=_info)
    image: dict[str, object] = field(default_factory=_image)
    calls: list[tuple[tuple[str, ...], dict[str, str], float, int]] = field(default_factory=list)
    mutate: Callable[[str, int], None] | None = None
    override: dict[str, DockerCommandResult | BaseException] = field(default_factory=dict)

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
        output_bytes_limit: int,
    ) -> DockerCommandResult:
        if "context" in argv:
            operation = "context"
            output = _json_line(f"unix://{self.environment.socket_path}")
        elif "version" in argv:
            operation = "version"
            output = _json_line(self.version)
        elif "info" in argv:
            operation = "info"
            output = _json_line(self.info)
        else:
            operation = "image"
            output = _json_line(self.image)
        self.calls.append((argv, dict(env), timeout_seconds, output_bytes_limit))
        if self.mutate is not None:
            self.mutate(operation, len(self.calls))
        overridden = self.override.get(operation)
        if isinstance(overridden, BaseException):
            raise overridden
        if overridden is not None:
            return overridden
        return DockerCommandResult(stdout=output, stderr=b"", returncode=0)


def _discover(
    environment: _Environment,
    runner: _Runner,
    *,
    docker_executable: str | None = None,
    expected_runtime_executable_sha256: str | None = None,
    expected_daemon_id: str | None = None,
    expected_seccomp_profile_sha256: str | None = None,
) -> DockerGateProviderConfig:
    return discover_docker_gate_provider(
        docker_executable=os.fspath(environment.docker) if docker_executable is None else docker_executable,
        expected_runtime_executable_sha256=(
            _file_sha256(environment.docker)
            if expected_runtime_executable_sha256 is None
            else expected_runtime_executable_sha256
        ),
        expected_daemon_id=(str(runner.info["ID"]) if expected_daemon_id is None else expected_daemon_id),
        state_directory=environment.state,
        docker_config_directory=environment.docker_config,
        image_reference=f"autoform-gates@sha256:{_IMAGE_DIGEST}",
        platform="linux/arm64",
        runtime_bundle_sha256=_BUNDLE_DIGEST,
        seccomp_profile_path=environment.seccomp,
        expected_seccomp_profile_sha256=(
            _file_sha256(environment.seccomp)
            if expected_seccomp_profile_sha256 is None
            else expected_seccomp_profile_sha256
        ),
        evaluator_executable="/usr/local/bin/python3",
        container_runtime="runc",
        user="65532:65532",
        limits=_limits(),
        command_timeout_seconds=7,
        command_output_bytes=131072,
        runner=runner,
    )


def test_discovery_binds_provider_identity_and_scrubs_every_command(
    provider_environment: _Environment,
) -> None:
    runner = _Runner(provider_environment)

    config = _discover(provider_environment, runner)

    docker_info = provider_environment.docker.stat()
    socket_info = provider_environment.socket_path.stat()
    assert config.runtime_path == os.fspath(provider_environment.docker)
    assert config.runtime_device == docker_info.st_dev
    assert config.runtime_inode == docker_info.st_ino
    assert config.runtime_mode == 0o500
    assert config.runtime_size == docker_info.st_size
    assert config.runtime_owner_uid == os.geteuid()
    assert config.runtime_executable_sha256 == hashlib.sha256(provider_environment.docker.read_bytes()).hexdigest()
    assert config.docker_daemon_id == runner.info["ID"]
    assert config.docker_host == f"unix://{provider_environment.socket_path}"
    assert config.docker_socket_device == socket_info.st_dev
    assert config.docker_socket_inode == socket_info.st_ino
    assert config.image_id == _IMAGE_ID
    assert config.runtime_fingerprint_sha256 != config.runtime_executable_sha256
    assert DockerGateProviderConfig.from_bytes(config.evidence_bytes()) == config

    assert len(runner.calls) == 4
    context, version, info, image = (call[0] for call in runner.calls)
    common = (
        os.fspath(provider_environment.docker),
        f"--config={provider_environment.docker_config}",
    )
    assert context == (
        *common,
        "context",
        "inspect",
        "default",
        "--format",
        "{{json .Endpoints.docker.Host}}",
    )
    host = f"--host=unix://{provider_environment.socket_path}"
    assert version == (*common, host, "version", "--format", "{{json .}}")
    assert info == (*common, host, "info", "--format", "{{json .}}")
    assert image == (
        *common,
        host,
        "image",
        "inspect",
        "--format",
        "{{json .}}",
        f"autoform-gates@sha256:{_IMAGE_DIGEST}",
    )
    for _argv, environment, timeout, maximum in runner.calls:
        assert environment == {
            "DOCKER_CONFIG": os.fspath(provider_environment.docker_config),
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
        }
        assert timeout == 7
        assert maximum == 131072
        assert {name for name in environment if name.startswith("DOCKER_")} == {"DOCKER_CONFIG"}


def test_default_runner_executes_the_pinned_binary_with_bounded_output(
    provider_environment: _Environment,
) -> None:
    outputs = {
        "context": _json_line(f"unix://{provider_environment.socket_path}"),
        "version": _json_line(_version()),
        "info": _json_line(_info()),
        "image": _json_line(_image()),
    }
    program = (
        f"#!{sys.executable}\n"
        "import sys\n"
        f"outputs = {outputs!r}\n"
        "args = sys.argv[1:]\n"
        "name = ('context' if 'context' in args else 'version' if 'version' in args "
        "else 'info' if 'info' in args else 'image')\n"
        "sys.stdout.buffer.write(outputs[name])\n"
    )
    provider_environment.docker.chmod(0o600)
    provider_environment.docker.write_text(program)
    provider_environment.docker.chmod(0o500)

    config = discover_docker_gate_provider(
        docker_executable=provider_environment.docker,
        expected_runtime_executable_sha256=_file_sha256(provider_environment.docker),
        expected_daemon_id=str(_info()["ID"]),
        state_directory=provider_environment.state,
        docker_config_directory=provider_environment.docker_config,
        image_reference=f"autoform-gates@sha256:{_IMAGE_DIGEST}",
        platform="linux/arm64",
        runtime_bundle_sha256=_BUNDLE_DIGEST,
        seccomp_profile_path=provider_environment.seccomp,
        expected_seccomp_profile_sha256=_file_sha256(provider_environment.seccomp),
        evaluator_executable="/usr/local/bin/python3",
        container_runtime="runc",
        user="65532:65532",
        limits=_limits(),
        command_timeout_seconds=2,
        command_output_bytes=131072,
    )

    assert config.image_id == _IMAGE_ID


def test_default_runner_enforces_the_deadline(provider_environment: _Environment) -> None:
    program = f"#!{sys.executable}\nimport time\ntime.sleep(30)\n"
    provider_environment.docker.chmod(0o600)
    provider_environment.docker.write_text(program)
    provider_environment.docker.chmod(0o500)

    with pytest.raises(GateProviderError, match="timed out"):
        discover_docker_gate_provider(
            docker_executable=provider_environment.docker,
            expected_runtime_executable_sha256=_file_sha256(provider_environment.docker),
            expected_daemon_id=str(_info()["ID"]),
            state_directory=provider_environment.state,
            docker_config_directory=provider_environment.docker_config,
            image_reference=f"autoform-gates@sha256:{_IMAGE_DIGEST}",
            platform="linux/arm64",
            runtime_bundle_sha256=_BUNDLE_DIGEST,
            seccomp_profile_path=provider_environment.seccomp,
            expected_seccomp_profile_sha256=_file_sha256(provider_environment.seccomp),
            evaluator_executable="/usr/local/bin/python3",
            container_runtime="runc",
            user="65532:65532",
            limits=_limits(),
            command_timeout_seconds=0.05,
            command_output_bytes=131072,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "tcp://127.0.0.1:2375",
        "unix://relative.sock",
        "unix://localhost/tmp/docker.sock",
        "unix:///tmp/../tmp/docker.sock",
        "unix:////tmp/docker.sock",
        "unix:///tmp/docker.sock?x=1",
        "unix:///tmp/docker.sock#fragment",
        "unix://user:password@/tmp/docker.sock",
        "unix:///tmp/docker%2Esock",
    ],
)
def test_discovery_rejects_nonlocal_or_ambiguous_endpoint(
    provider_environment: _Environment,
    endpoint: str,
) -> None:
    runner = _Runner(provider_environment)
    runner.override["context"] = DockerCommandResult(stdout=_json_line(endpoint), stderr=b"", returncode=0)

    with pytest.raises(GateProviderError, match="Unix socket"):
        _discover(provider_environment, runner)


@pytest.mark.parametrize("mode", [0o600, 0o522, 0o777])
def test_discovery_rejects_unsafe_docker_executable_mode(
    provider_environment: _Environment,
    mode: int,
) -> None:
    provider_environment.docker.chmod(mode)

    with pytest.raises(GateProviderError, match="Docker executable"):
        _discover(provider_environment, _Runner(provider_environment))


def test_discovery_accepts_an_explicit_symlink_to_the_anchored_docker_executable(
    provider_environment: _Environment,
) -> None:
    link = provider_environment.docker.parent / "docker-link"
    link.symlink_to(provider_environment.docker)

    config = _discover(
        provider_environment,
        _Runner(provider_environment),
        docker_executable=os.fspath(link),
    )

    assert config.runtime_path == os.fspath(provider_environment.docker)


def test_discovery_rejects_a_regular_file_in_place_of_the_docker_socket(
    provider_environment: _Environment,
) -> None:
    provider_environment.socket_handle.close()
    provider_environment.socket_path.unlink()
    provider_environment.socket_path.write_text("not a socket")

    with pytest.raises(GateProviderError, match="not a socket"):
        _discover(provider_environment, _Runner(provider_environment))


def test_discovery_rejects_a_world_writable_docker_socket(
    provider_environment: _Environment,
) -> None:
    provider_environment.socket_path.chmod(0o777)

    with pytest.raises(GateProviderError, match="world writable"):
        _discover(provider_environment, _Runner(provider_environment))


def test_discovery_rejects_a_bare_docker_name_from_ambient_path(
    provider_environment: _Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", os.fspath(provider_environment.docker.parent))
    with pytest.raises(GateProviderError, match="explicit absolute path"):
        _discover(provider_environment, _Runner(provider_environment), docker_executable="docker")


def test_discovery_requires_external_runtime_daemon_and_seccomp_anchors(
    provider_environment: _Environment,
) -> None:
    runner = _Runner(provider_environment)
    with pytest.raises(GateProviderError, match="trusted Docker executable"):
        _discover(
            provider_environment,
            runner,
            expected_runtime_executable_sha256="0" * 64,
        )
    assert runner.calls == []

    runner = _Runner(provider_environment)
    with pytest.raises(GateProviderError, match="trusted seccomp"):
        _discover(
            provider_environment,
            runner,
            expected_seccomp_profile_sha256="0" * 64,
        )
    assert runner.calls == []

    runner = _Runner(provider_environment)
    with pytest.raises(GateProviderError, match="trusted Docker daemon"):
        _discover(provider_environment, runner, expected_daemon_id="different-daemon")
    assert ["context" in call[0] for call in runner.calls] == [True, False, False]


@pytest.mark.parametrize(
    "policy",
    [
        {"defaultAction": "SCMP_ACT_ALLOW", "syscalls": []},
        {"defaultAction": [], "syscalls": []},
        {"defaultAction": "SCMP_ACT_ERRNO"},
        {
            "defaultAction": "SCMP_ACT_ERRNO",
            "listenerPath": "/tmp/seccomp-listener",
            "syscalls": [],
        },
        {
            "defaultAction": "SCMP_ACT_ERRNO",
            "syscalls": [{"action": "SCMP_ACT_NOTIFY", "names": ["openat"]}],
        },
        {
            "defaultAction": "SCMP_ACT_ERRNO",
            "syscalls": [{"action": [], "names": ["openat"]}],
        },
    ],
)
def test_discovery_rejects_unsafe_or_incomplete_seccomp_semantics(
    provider_environment: _Environment,
    policy: dict[str, object],
) -> None:
    provider_environment.seccomp.chmod(0o600)
    provider_environment.seccomp.write_bytes(_json_line(policy))
    provider_environment.seccomp.chmod(0o444)

    with pytest.raises(GateProviderError, match="seccomp"):
        _discover(provider_environment, _Runner(provider_environment))


def test_discovery_binds_raw_and_canonical_seccomp_digests(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))

    assert config.seccomp_profile_sha256 == _file_sha256(provider_environment.seccomp)
    assert config.seccomp_policy_sha256 == _seccomp_policy_sha256(provider_environment.seccomp)
    assert config.seccomp_profile_sha256 != config.seccomp_policy_sha256
    policy = json.dumps(json.loads(provider_environment.seccomp.read_bytes()), separators=(",", ":"))
    gate_provider_module._validate_security_options(
        config,
        {"SecurityOpt": ["no-new-privileges=true", f"seccomp={policy}"]},
    )


@pytest.mark.parametrize("defect", ["state-mode", "config-mode", "config-entry", "seccomp-mode", "seccomp-link"])
def test_discovery_rejects_untrusted_packaged_policy_state(
    provider_environment: _Environment,
    defect: str,
) -> None:
    if defect == "state-mode":
        provider_environment.state.chmod(0o755)
    elif defect == "config-mode":
        provider_environment.docker_config.chmod(0o755)
    elif defect == "config-entry":
        (provider_environment.docker_config / "config.json").write_text("{}")
    elif defect == "seccomp-mode":
        provider_environment.seccomp.chmod(0o644)
    else:
        linked = provider_environment.state / "seccomp-link.json"
        os.link(provider_environment.seccomp, linked)

    with pytest.raises(GateProviderError):
        _discover(provider_environment, _Runner(provider_environment))


@pytest.mark.parametrize(
    ("operation", "result", "message"),
    [
        ("version", DockerCommandResult(stdout=b"{}\n", stderr=b"warning", returncode=0), "stderr"),
        ("version", DockerCommandResult(stdout=b"{}\n", stderr=b"", returncode=125), "exit 125"),
        ("version", DockerCommandResult(stdout=b"\xff\n", stderr=b"", returncode=0), "UTF-8"),
        ("version", DockerCommandResult(stdout=b"{}\n{}\n", stderr=b"", returncode=0), "JSON"),
        (
            "version",
            DockerCommandResult(stdout=b"x" * 131073, stderr=b"", returncode=0),
            "output limit",
        ),
        ("version", subprocess.TimeoutExpired(["docker"], 7), "timed out"),
    ],
)
def test_discovery_rejects_unbounded_or_ambiguous_command_results(
    provider_environment: _Environment,
    operation: str,
    result: DockerCommandResult | BaseException,
    message: str,
) -> None:
    runner = _Runner(provider_environment)
    runner.override[operation] = result

    with pytest.raises(GateProviderError, match=message):
        _discover(provider_environment, runner)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("server-os", "Linux server"),
        ("client-api", "API version"),
        ("server-api", "API version"),
        ("runtime", "selected runtime"),
        ("seccomp", "seccomp"),
        ("platform", "native platform"),
        ("image-id", "image ID"),
        ("image-digest", "repository digest"),
        ("image-volumes", "volumes"),
        ("bundle-schema", "bundle schema"),
        ("bundle-digest", "bundle digest"),
        ("autoform-label", "Autoform image labels"),
    ],
)
def test_discovery_rejects_daemon_or_image_semantic_mismatch(
    provider_environment: _Environment,
    mutation: str,
    message: str,
) -> None:
    runner = _Runner(provider_environment)
    if mutation == "server-os":
        runner.version["Server"]["Os"] = "windows"  # type: ignore[index]
    elif mutation == "client-api":
        runner.version["Client"]["ApiVersion"] = "1.47"  # type: ignore[index]
    elif mutation == "server-api":
        runner.version["Server"]["ApiVersion"] = "1.47"  # type: ignore[index]
    elif mutation == "runtime":
        del runner.info["Runtimes"]["runc"]  # type: ignore[index]
    elif mutation == "seccomp":
        runner.info["SecurityOptions"] = ["name=cgroupns"]
    elif mutation == "platform":
        runner.info["Architecture"] = "amd64"
    elif mutation == "image-id":
        runner.image["Id"] = "sha512:" + "6" * 64
    elif mutation == "image-digest":
        runner.image["RepoDigests"] = ["other@sha256:" + "3" * 64]
    elif mutation == "image-volumes":
        runner.image["Config"]["Volumes"] = {"/data": {}}  # type: ignore[index]
    elif mutation == "bundle-schema":
        runner.image["Config"]["Labels"][  # type: ignore[index]
            "org.autoform.runtime-bundle-schema"
        ] = "other/v1"
    elif mutation == "bundle-digest":
        runner.image["Config"]["Labels"][  # type: ignore[index]
            "org.autoform.runtime-bundle-sha256"
        ] = "6" * 64
    else:
        runner.image["Config"]["Labels"]["org.autoform.unbound"] = "x"  # type: ignore[index]

    with pytest.raises(GateProviderError, match=message):
        _discover(provider_environment, runner)


def test_discovery_accepts_multiple_repository_digest_aliases_and_binds_image_id(
    provider_environment: _Environment,
) -> None:
    runner = _Runner(provider_environment)
    runner.image["RepoDigests"] = [
        f"mirror.invalid/autoform-gates@sha256:{'6' * 64}",
        f"autoform-gates@sha256:{_IMAGE_DIGEST}",
    ]

    config = _discover(provider_environment, runner)

    assert config.image_id == _IMAGE_ID

    changed = _Runner(provider_environment)
    changed.image["Id"] = "sha256:" + "6" * 64
    with pytest.raises(GateProviderError, match="image identity"):
        revalidate_docker_gate_provider(config, runner=changed)


def test_discovery_rejects_executable_or_socket_swap_during_command(
    provider_environment: _Environment,
) -> None:
    def mutate_executable(operation: str, _call: int) -> None:
        if operation == "version":
            provider_environment.docker.chmod(0o700)

    runner = _Runner(provider_environment, mutate=mutate_executable)
    with pytest.raises(GateProviderError, match="Docker executable changed"):
        _discover(provider_environment, runner)

    provider_environment.docker.chmod(0o500)

    def mutate_socket(operation: str, _call: int) -> None:
        if operation == "version":
            provider_environment.socket_handle.close()
            provider_environment.socket_path.unlink()
            replacement = socket.socket(socket.AF_UNIX)
            replacement.bind(os.fspath(provider_environment.socket_path))
            provider_environment.socket_handle = replacement

    runner = _Runner(provider_environment, mutate=mutate_socket)
    with pytest.raises(GateProviderError, match="Docker socket changed"):
        _discover(provider_environment, runner)


def test_revalidation_accepts_socket_replacement_only_after_full_stable_reprobe(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))
    old_inode = config.docker_socket_inode
    provider_environment.socket_handle.close()
    provider_environment.socket_path.unlink()
    provider_environment.socket_handle = socket.socket(socket.AF_UNIX)
    provider_environment.socket_handle.bind(os.fspath(provider_environment.socket_path))
    runner = _Runner(provider_environment)

    refreshed = revalidate_docker_gate_provider(
        config,
        command_timeout_seconds=7,
        command_output_bytes=131072,
        runner=runner,
    )

    assert refreshed.docker_socket_inode != old_inode
    assert refreshed.runtime_fingerprint_sha256 == config.runtime_fingerprint_sha256
    assert len(runner.calls) == 4


def test_revalidation_rejects_semantic_drift_after_socket_replacement(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))
    provider_environment.socket_handle.close()
    provider_environment.socket_path.unlink()
    provider_environment.socket_handle = socket.socket(socket.AF_UNIX)
    provider_environment.socket_handle.bind(os.fspath(provider_environment.socket_path))
    runner = _Runner(provider_environment)
    runner.info["ID"] = "DIFFERENT-DAEMON"

    with pytest.raises(GateProviderError, match="trusted Docker daemon"):
        revalidate_docker_gate_provider(
            config,
            command_timeout_seconds=7,
            command_output_bytes=131072,
            runner=runner,
        )


def test_revalidation_rejects_bound_file_drift_before_running_docker(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))
    provider_environment.docker.chmod(0o700)
    runner = _Runner(provider_environment)

    with pytest.raises(GateProviderError, match="Docker executable changed"):
        revalidate_docker_gate_provider(config, runner=runner)
    assert runner.calls == []


def test_revalidation_rejects_seccomp_drift_before_running_docker(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))
    provider_environment.seccomp.chmod(0o600)
    runner = _Runner(provider_environment)

    with pytest.raises(GateProviderError, match="seccomp profile changed"):
        revalidate_docker_gate_provider(config, runner=runner)
    assert runner.calls == []


def test_revalidation_rejects_daemon_drift_without_a_socket_change(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))
    runner = _Runner(provider_environment)
    runner.info["ID"] = "DIFFERENT-DAEMON"

    with pytest.raises(GateProviderError, match="trusted Docker daemon"):
        revalidate_docker_gate_provider(config, runner=runner)


def test_discovery_rejects_output_limits_above_the_hard_cap(
    provider_environment: _Environment,
) -> None:
    with pytest.raises(GateProviderError, match="hard maximum"):
        discover_docker_gate_provider(
            docker_executable=provider_environment.docker,
            expected_runtime_executable_sha256=_file_sha256(provider_environment.docker),
            expected_daemon_id=str(_info()["ID"]),
            state_directory=provider_environment.state,
            docker_config_directory=provider_environment.docker_config,
            image_reference=f"autoform-gates@sha256:{_IMAGE_DIGEST}",
            platform="linux/arm64",
            runtime_bundle_sha256=_BUNDLE_DIGEST,
            seccomp_profile_path=provider_environment.seccomp,
            expected_seccomp_profile_sha256=_file_sha256(provider_environment.seccomp),
            evaluator_executable="/usr/local/bin/python3",
            container_runtime="runc",
            user="65532:65532",
            limits=_limits(),
            command_output_bytes=2 * 1024 * 1024 + 1,
            runner=_Runner(provider_environment),
        )


def test_bounded_runner_kills_the_process_group_after_the_parent_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pipe:
        def close(self) -> None:
            return None

    class ExitedParent:
        pid = 4242
        stdout = Pipe()
        stderr = Pipe()

        def poll(self) -> int:
            return 0

        def kill(self) -> None:
            raise AssertionError("the exited parent cannot clean up its descendants")

        def wait(self, timeout: float) -> int:
            return 0

    class FailingSelector:
        def register(self, *args: object) -> None:
            return None

        def get_map(self) -> dict[str, object]:
            return {"descendant-pipe": object()}

        def select(self, timeout: float) -> list[object]:
            raise GateProviderError("primary discovery failure")

        def close(self) -> None:
            return None

    process = ExitedParent()
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(gate_provider_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(gate_provider_module.selectors, "DefaultSelector", FailingSelector)
    monkeypatch.setattr(
        gate_provider_module.os,
        "killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    with pytest.raises(GateProviderError, match="primary discovery failure"):
        gate_provider_module._bounded_docker_command(
            ("/trusted/docker", "version"),
            env={},
            timeout_seconds=1,
            output_bytes_limit=1024,
        )

    assert killed == [(process.pid, gate_provider_module.signal.SIGKILL)]


def test_bounded_runner_kills_a_descendant_after_a_successful_parent_exit(
    tmp_path: Path,
) -> None:
    process_ids = tmp_path / "process-ids"
    child_script = "import os, time; os.close(1); os.close(2); time.sleep(60)"
    parent_script = (
        "import os, pathlib, subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[2]]); "
        "pathlib.Path(sys.argv[1]).write_text("
        "f'{os.getpid()} {os.getpgrp()} {child.pid}', encoding='ascii')"
    )
    parent_pid: int | None = None
    child_pid: int | None = None

    try:
        result = gate_provider_module._bounded_docker_command(
            (sys.executable, "-c", parent_script, os.fspath(process_ids), child_script),
            env={},
            timeout_seconds=2,
            output_bytes_limit=1024,
        )
        parent_pid, process_group, child_pid = (
            int(component) for component in process_ids.read_text(encoding="ascii").split()
        )

        assert result == DockerCommandResult(stdout=b"", stderr=b"", returncode=0)
        assert process_group == parent_pid
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("the successful Docker command left its descendant running")
        with pytest.raises(ProcessLookupError):
            os.killpg(process_group, 0)
    finally:
        if parent_pid is not None:
            try:
                os.killpg(parent_pid, gate_provider_module.signal.SIGKILL)
            except ProcessLookupError:
                pass
        if child_pid is not None:
            try:
                os.kill(child_pid, gate_provider_module.signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_discovery_rejects_a_non_ascii_trusted_daemon_id(
    provider_environment: _Environment,
) -> None:
    runner = _Runner(provider_environment)

    with pytest.raises(GateProviderError, match="trusted Docker daemon ID"):
        _discover(provider_environment, runner, expected_daemon_id="daemon-\N{SNOWMAN}")

    assert runner.calls == []


def test_discovery_rejects_a_non_ascii_observed_daemon_id(
    provider_environment: _Environment,
) -> None:
    runner = _Runner(provider_environment)
    runner.info["ID"] = "daemon-\N{SNOWMAN}"

    with pytest.raises(GateProviderError, match="Docker daemon ID"):
        _discover(
            provider_environment,
            runner,
            expected_daemon_id=str(_info()["ID"]),
        )


def test_irrelevant_daemon_counters_do_not_change_semantic_fingerprint(
    provider_environment: _Environment,
) -> None:
    config = _discover(provider_environment, _Runner(provider_environment))
    runner = _Runner(provider_environment)
    runner.info["ContainersRunning"] = 999

    assert revalidate_docker_gate_provider(config, runner=runner) == config


def test_discovery_normalizes_the_daemon_architecture_to_the_exact_oci_platform(
    provider_environment: _Environment,
) -> None:
    runner = _Runner(provider_environment)
    runner.info["Architecture"] = "aarch64"

    assert _discover(provider_environment, runner).platform == "linux/arm64"
