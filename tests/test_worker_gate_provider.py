from __future__ import annotations

import base64
from dataclasses import replace

import pytest

from autoform_worker.gate_provider import (
    DockerGateProviderConfig,
    DockerSandboxLimits,
    GateInvocationRequest,
    GateProviderError,
    docker_create_argv,
)


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


def _config() -> DockerGateProviderConfig:
    return DockerGateProviderConfig(
        runtime_path="/usr/local/bin/docker",
        runtime_executable_sha256="1" * 64,
        runtime_fingerprint_sha256="2" * 64,
        image_reference=f"autoform-gates@sha256:{'3' * 64}",
        image_id=f"sha256:{'4' * 64}",
        platform="linux/arm64",
        runtime_bundle_sha256="5" * 64,
        seccomp_profile_path="/etc/autoform/seccomp.json",
        seccomp_profile_sha256="6" * 64,
        evaluator_executable="/usr/local/bin/python3",
        container_runtime="runc",
        user="65532:65532",
        limits=_limits(),
    )


def _request() -> GateInvocationRequest:
    return GateInvocationRequest(
        invocation_id="7" * 64,
        run_id="run-1",
        attempt_id="attempt-1",
        base_oid="8" * 40,
        candidate_oid="9" * 40,
        node_id="chapter/result",
        article_id="af_0123456789abcdef01234567",
        phase="proof",
        attempt=1,
        source_revision="d" * 64,
        source_contract_sha256="a" * 64,
        protected_roadmap_sha256="b" * 64,
        work_item_sha256="c" * 64,
        provider_config_sha256=_config().sha256,
    )


def test_provider_config_round_trips_as_canonical_evidence() -> None:
    config = _config()

    loaded = DockerGateProviderConfig.from_bytes(config.evidence_bytes())

    assert loaded == config
    assert loaded.evidence_bytes() == config.evidence_bytes()
    assert loaded.sha256 == config.sha256


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("runtime_path", "docker", "absolute path"),
        ("runtime_path", "/usr/local/../bin/docker", "absolute path"),
        ("runtime_path", "/usr/local/bin/docker\nignored", "absolute path"),
        ("runtime_executable_sha256", "A" * 64, "lowercase hexadecimal"),
        ("runtime_fingerprint_sha256", "2" * 63, "lowercase hexadecimal"),
        ("image_reference", "autoform-gates:latest", "immutable sha256 digest"),
        ("image_id", "4" * 64, "sha256 algorithm"),
        ("platform", "darwin/arm64", "Linux OCI platform"),
        ("container_runtime", "Runc", "runtime name"),
        ("user", "0:0", "non-root numeric"),
        ("user", "worker:worker", "non-root numeric"),
        ("user", f"{2**31}:65532", "signed 32-bit"),
    ],
)
def test_provider_config_rejects_unpinned_or_ambiguous_identity(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(GateProviderError, match=message):
        replace(_config(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_bytes", 0),
        ("memory_swap_bytes", -1),
        ("cpu_nanos", True),
        ("pids_limit", 0),
        ("scratch_bytes", 0),
        ("output_bytes", 0),
        ("nofile_limit", 0),
        ("wall_timeout_seconds", float("inf")),
        pytest.param("wall_timeout_seconds", 10**10_000, id="overflowing-timeout"),
    ],
)
def test_provider_limits_reject_nonpositive_or_nonfinite_values(field: str, value: object) -> None:
    with pytest.raises(GateProviderError):
        replace(_limits(), **{field: value})


def test_provider_limits_disable_swap_expansion() -> None:
    with pytest.raises(GateProviderError, match="swap bytes must equal memory bytes"):
        replace(_limits(), memory_swap_bytes=_limits().memory_bytes * 2)


def test_provider_limits_require_output_to_fit_in_scratch() -> None:
    with pytest.raises(GateProviderError, match="output bytes must not exceed scratch bytes"):
        replace(_limits(), output_bytes=_limits().scratch_bytes + 1)


@pytest.mark.parametrize(
    "content",
    [
        b"{}",
        b'{"schema":"autoform-docker-gate-provider/v1","schema":"duplicate"}',
        b'{"schema":NaN}',
        b"[]",
        b"\xff",
        b"x" * (64 * 1024 + 1),
    ],
)
def test_provider_config_loader_rejects_noncanonical_or_wrong_shape(content: bytes) -> None:
    with pytest.raises(GateProviderError):
        DockerGateProviderConfig.from_bytes(content)


def test_gate_invocation_round_trips_with_deterministic_ownership() -> None:
    request = _request()

    loaded = GateInvocationRequest.from_bytes(request.evidence_bytes())

    assert loaded == request
    assert loaded.container_name == f"autoform-gate-{'7' * 64}"
    assert loaded.ownership_labels() == (
        "org.autoform.gate=1",
        f"org.autoform.invocation={'7' * 64}",
        f"org.autoform.request-sha256={request.sha256}",
        f"org.autoform.provider-sha256={_config().sha256}",
    )
    assert "run_id" not in loaded.evaluator_dict()
    assert "attempt_id" not in loaded.evaluator_dict()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("invocation_id", "7" * 63, "256-bit"),
        ("run_id", "run\nother", "canonical text"),
        ("run_id", "bad\udcff", "canonical text"),
        ("base_oid", "8" * 39, "object ID"),
        ("candidate_oid", "9" * 64, "same object format"),
        ("candidate_oid", "8" * 40, "must differ"),
        ("article_id", "result", "durable Autoform format"),
        ("source_revision", "source-revision", "lowercase hexadecimal"),
        ("phase", "review", "statement or proof"),
        ("attempt", 0, "positive integer"),
        ("provider_config_sha256", "A" * 64, "lowercase hexadecimal"),
    ],
)
def test_gate_invocation_rejects_ambiguous_identity(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(GateProviderError, match=message):
        replace(_request(), **{field: value})


@pytest.mark.parametrize(
    "content",
    [
        b"{}",
        b'{"schema":"autoform-gate-invocation/v1","schema":"duplicate"}',
        b'{"schema":Infinity}',
        b"[]",
        b"\xff",
        b"x" * (64 * 1024 + 1),
    ],
)
def test_gate_invocation_loader_rejects_noncanonical_or_wrong_shape(content: bytes) -> None:
    with pytest.raises(GateProviderError):
        GateInvocationRequest.from_bytes(content)


def test_docker_create_command_has_one_exact_fail_closed_policy() -> None:
    config = _config()
    request = _request()

    command = docker_create_argv(config, request, "/srv/autoform/repository")

    assert command[:4] == (
        "/usr/local/bin/docker",
        "create",
        "--name",
        request.container_name,
    )
    assert command.count("--label") == 4
    assert command.count("--mount") == 1
    assert command[command.index("--mount") + 1] == (
        "type=bind,source=/srv/autoform/repository,target=/autoform/input/repository,"
        "readonly,bind-recursive=readonly"
    )
    for pair in (
        ("--pull", "never"),
        ("--network", "none"),
        ("--pid", "private"),
        ("--ipc", "none"),
        ("--uts", "private"),
        ("--cgroupns", "private"),
        ("--runtime", "runc"),
        ("--user", "65532:65532"),
        ("--cap-drop", "ALL"),
        ("--log-driver", "none"),
        ("--restart", "no"),
        ("--stop-signal", "SIGKILL"),
        ("--stop-timeout", "0"),
        ("--entrypoint", "/usr/local/bin/python3"),
    ):
        position = command.index(pair[0])
        assert command[position : position + 2] == pair
    assert "--read-only" in command
    assert "--init" in command
    assert "--no-healthcheck" in command
    assert command[-5:-1] == (
        "-I",
        "-m",
        "autoform_worker.gate_evaluator",
        "--request-base64",
    )
    assert GateInvocationRequest.from_bytes(base64.urlsafe_b64decode(command[-1])) == request


def test_docker_create_command_rejects_unbound_config_or_unrepresentable_mount() -> None:
    with pytest.raises(GateProviderError, match="does not bind"):
        docker_create_argv(
            replace(_config(), runtime_bundle_sha256="f" * 64),
            _request(),
            "/srv/autoform/repository",
        )
    with pytest.raises(GateProviderError, match="bind mount"):
        docker_create_argv(_config(), _request(), "/srv/autoform/repo,other")
