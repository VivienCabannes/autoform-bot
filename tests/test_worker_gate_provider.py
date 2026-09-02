from __future__ import annotations

from dataclasses import replace

import pytest

from autoform_worker.gate_provider import (
    DockerGateProviderConfig,
    DockerSandboxLimits,
    GateProviderError,
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
        user="65532:65532",
        limits=_limits(),
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
        ("user", "0:0", "non-root numeric"),
        ("user", "worker:worker", "non-root numeric"),
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
