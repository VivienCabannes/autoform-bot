from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace

import pytest

from autoform_worker.gate_provider import (
    DOCKER_CREATE_ATTESTATION_SCHEMA,
    DockerCreateAttestation,
    DockerGateProviderConfig,
    DockerSandboxLimits,
    GateInvocationRequest,
    GateProviderError,
    attest_docker_create,
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


def _config(*, seccomp_profile_sha256: str = "6" * 64) -> DockerGateProviderConfig:
    return DockerGateProviderConfig(
        runtime_path="/usr/local/bin/docker",
        runtime_executable_sha256="1" * 64,
        runtime_fingerprint_sha256="2" * 64,
        image_reference=f"autoform-gates@sha256:{'3' * 64}",
        image_id=f"sha256:{'4' * 64}",
        platform="linux/arm64",
        runtime_bundle_sha256="5" * 64,
        seccomp_profile_path="/etc/autoform/seccomp.json",
        seccomp_profile_sha256=seccomp_profile_sha256,
        evaluator_executable="/usr/local/bin/python3",
        container_runtime="runc",
        user="65532:65532",
        limits=_limits(),
    )


def _request(config: DockerGateProviderConfig | None = None) -> GateInvocationRequest:
    if config is None:
        config = _config()
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
        provider_config_sha256=config.sha256,
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _seccomp_policy() -> dict[str, object]:
    return {
        "architectures": ["SCMP_ARCH_AARCH64"],
        "defaultAction": "SCMP_ACT_ERRNO",
        "syscalls": [],
    }


def _inspection(
    config: DockerGateProviderConfig,
    request: GateInvocationRequest,
) -> dict[str, object]:
    command = docker_create_argv(config, request, "/srv/autoform/repository")
    image_index = command.index(config.image_reference)
    evaluator_arguments = list(command[image_index + 1 :])
    labels = dict(label.split("=", 1) for label in request.ownership_labels())
    seccomp = _canonical_json(_seccomp_policy()).decode()
    return {
        "Id": "e" * 64,
        "Name": f"/{request.container_name}",
        "Image": config.image_id,
        "Path": config.evaluator_executable,
        "Args": evaluator_arguments,
        "RestartCount": 0,
        "Platform": "linux",
        "State": {
            "Status": "created",
            "Running": False,
            "Paused": False,
            "Restarting": False,
            "OOMKilled": False,
            "Dead": False,
            "Pid": 0,
            "ExitCode": 0,
            "Error": "",
        },
        "Config": {
            "Hostname": "autoform-gate",
            "Domainname": "",
            "User": config.user,
            "Tty": False,
            "OpenStdin": False,
            "StdinOnce": False,
            "Cmd": evaluator_arguments,
            "Image": config.image_reference,
            "WorkingDir": "/autoform/work",
            "StopSignal": "SIGKILL",
            "StopTimeout": 0,
            "Entrypoint": [config.evaluator_executable],
            "ExposedPorts": None,
            "Volumes": None,
            "Healthcheck": {"Test": ["NONE"]},
            "Labels": labels,
            "Env": [
                "HOME=/nonexistent",
                "TMPDIR=/autoform/work/tmp",
                "AUTOFORM_GATE_RESULT=/autoform/result/result.json",
            ],
        },
        "HostConfig": {
            "NetworkMode": "none",
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
            "AutoRemove": False,
            "CapAdd": None,
            "CapDrop": ["ALL"],
            "CgroupnsMode": "private",
            "IpcMode": "none",
            "PidMode": "private",
            "UTSMode": "private",
            "Privileged": False,
            "PublishAllPorts": False,
            "ReadonlyRootfs": True,
            "Runtime": config.container_runtime,
            "Memory": config.limits.memory_bytes,
            "MemorySwap": config.limits.memory_swap_bytes,
            "MemorySwappiness": 0,
            "NanoCpus": config.limits.cpu_nanos,
            "PidsLimit": config.limits.pids_limit,
            "OomKillDisable": False,
            "Init": True,
            "ShmSize": 16 * 1024 * 1024,
            "LogConfig": {"Type": "none", "Config": {}},
            "Tmpfs": {
                "/autoform/work": (
                    f"rw,nosuid,nodev,size={config.limits.scratch_bytes}"
                ),
                "/autoform/result": (
                    f"rw,nosuid,nodev,noexec,size={config.limits.output_bytes}"
                ),
            },
            "Binds": None,
            "PortBindings": {},
            "Links": None,
            "Dns": None,
            "DnsOptions": None,
            "DnsSearch": None,
            "ExtraHosts": None,
            "VolumesFrom": None,
            "GroupAdd": None,
            "Devices": [],
            "DeviceCgroupRules": None,
            "DeviceRequests": None,
            "Sysctls": None,
            "StorageOpt": None,
            "SecurityOpt": [
                "no-new-privileges=true",
                f"seccomp={seccomp}",
            ],
            "Ulimits": [
                {"Name": "nofile", "Soft": 4096, "Hard": 4096},
                {"Name": "core", "Soft": 0, "Hard": 0},
            ],
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/autoform/repository",
                    "Target": "/autoform/input/repository",
                    "ReadOnly": True,
                    "Consistency": "",
                    "BindOptions": {
                        "Propagation": "rprivate",
                        "NonRecursive": False,
                        "CreateMountpoint": False,
                        "ReadOnlyNonRecursive": False,
                        "ReadOnlyForceRecursive": True,
                    },
                }
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/srv/autoform/repository",
                "Destination": "/autoform/input/repository",
                "Mode": "ro",
                "RW": False,
                "Propagation": "rprivate",
            }
        ],
        "NetworkSettings": {
            "Bridge": "",
            "SandboxID": "",
            "SandboxKey": "",
            "HairpinMode": False,
            "LinkLocalIPv6Address": "",
            "LinkLocalIPv6PrefixLen": 0,
            "Ports": {},
            "SecondaryIPAddresses": None,
            "SecondaryIPv6Addresses": None,
            "Networks": {
                "none": {
                    "IPAMConfig": None,
                    "Links": None,
                    "Aliases": None,
                    "DriverOpts": None,
                    "NetworkID": "f" * 64,
                    "EndpointID": "",
                    "Gateway": "",
                    "IPAddress": "",
                    "IPPrefixLen": 0,
                    "IPv6Gateway": "",
                    "GlobalIPv6Address": "",
                    "GlobalIPv6PrefixLen": 0,
                    "MacAddress": "",
                    "DNSNames": None,
                }
            },
        },
    }


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
        ("--pids-limit", "256"),
        ("--memory", str(8 * 1024**3)),
        ("--memory-swap", str(8 * 1024**3)),
        ("--memory-swappiness", "0"),
        ("--shm-size", str(16 * 1024 * 1024)),
        ("--cpus", "2.000000000"),
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
    assert "--oom-kill-disable=false" in command
    assert command.count("--security-opt") == 2
    assert command.count("--tmpfs") == 2
    assert command.count("--ulimit") == 2
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


def test_docker_create_inspection_produces_bound_canonical_attestation() -> None:
    seccomp_sha256 = hashlib.sha256(_canonical_json(_seccomp_policy())).hexdigest()
    config = _config(seccomp_profile_sha256=seccomp_sha256)
    request = _request(config)
    inspect_bytes = _canonical_json(_inspection(config, request))

    attestation = attest_docker_create(
        config,
        request,
        "/srv/autoform/repository",
        inspect_bytes,
    )

    assert attestation.schema == DOCKER_CREATE_ATTESTATION_SCHEMA
    assert attestation.container_id == "e" * 64
    assert attestation.request_sha256 == request.sha256
    assert attestation.provider_config_sha256 == config.sha256
    assert attestation.inspect_sha256 == hashlib.sha256(inspect_bytes).hexdigest()
    assert DockerCreateAttestation.from_bytes(attestation.evidence_bytes()) == attestation
    assert attestation.sha256 == hashlib.sha256(attestation.evidence_bytes()).hexdigest()


def _set_inspection_path(
    inspection: dict[str, object],
    path: tuple[str | int, ...],
    value: object,
) -> None:
    current: object = inspection
    for component in path[:-1]:
        current = current[component]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("State", "Running"), True),
        (("Config", "User"), "0:0"),
        (("Config", "StopTimeout"), 30),
        (("Config", "Labels", "org.autoform.invocation"), "0" * 64),
        (("HostConfig", "NetworkMode"), "host"),
        (("HostConfig", "ReadonlyRootfs"), False),
        (("HostConfig", "Privileged"), True),
        (("HostConfig", "Memory"), 0),
        (("HostConfig", "SecurityOpt", 0), "no-new-privileges=false"),
        (("HostConfig", "SecurityOpt", 1), "seccomp={}"),
        (("HostConfig", "Mounts", 0, "ReadOnly"), False),
        (("HostConfig", "Mounts", 0, "BindOptions", "ReadOnlyForceRecursive"), False),
        (("Mounts", 0, "RW"), True),
        (("NetworkSettings", "Networks"), {"bridge": {}}),
    ],
)
def test_docker_create_inspection_rejects_policy_drift(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    seccomp_sha256 = hashlib.sha256(_canonical_json(_seccomp_policy())).hexdigest()
    config = _config(seccomp_profile_sha256=seccomp_sha256)
    request = _request(config)
    inspection = copy.deepcopy(_inspection(config, request))
    _set_inspection_path(inspection, path, value)

    with pytest.raises(GateProviderError):
        attest_docker_create(
            config,
            request,
            "/srv/autoform/repository",
            _canonical_json(inspection),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "credential-environment",
        "duplicate-environment",
        "extra-autoform-label",
        "extra-device",
        "extra-security-option",
    ],
)
def test_docker_create_inspection_rejects_injected_authority(mutation: str) -> None:
    seccomp_sha256 = hashlib.sha256(_canonical_json(_seccomp_policy())).hexdigest()
    config = _config(seccomp_profile_sha256=seccomp_sha256)
    request = _request(config)
    inspection = copy.deepcopy(_inspection(config, request))
    container = inspection["Config"]
    host = inspection["HostConfig"]
    assert isinstance(container, dict)
    assert isinstance(host, dict)
    if mutation == "credential-environment":
        container["Env"].append("AWS_SECRET_ACCESS_KEY=stolen")
    elif mutation == "duplicate-environment":
        container["Env"].append("HOME=/root")
    elif mutation == "extra-autoform-label":
        container["Labels"]["org.autoform.unbound"] = "1"
    elif mutation == "extra-device":
        host["Devices"] = [{"PathOnHost": "/dev/kvm"}]
    else:
        host["SecurityOpt"].append("label=disable")

    with pytest.raises(GateProviderError):
        attest_docker_create(
            config,
            request,
            "/srv/autoform/repository",
            _canonical_json(inspection),
        )


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("Config", "ExposedPorts"),
        ("Config", "Volumes"),
        ("HostConfig", "Devices"),
        ("HostConfig", "SecurityOpt"),
        ("NetworkSettings", "Networks"),
    ],
)
def test_docker_create_inspection_rejects_missing_policy_evidence(
    section: str,
    field: str,
) -> None:
    seccomp_sha256 = hashlib.sha256(_canonical_json(_seccomp_policy())).hexdigest()
    config = _config(seccomp_profile_sha256=seccomp_sha256)
    request = _request(config)
    inspection = copy.deepcopy(_inspection(config, request))
    selected = inspection[section]
    assert isinstance(selected, dict)
    del selected[field]

    with pytest.raises(GateProviderError):
        attest_docker_create(
            config,
            request,
            "/srv/autoform/repository",
            _canonical_json(inspection),
        )


@pytest.mark.parametrize(
    "inspect_bytes",
    [
        b"{}",
        b'{"Id":"first","Id":"second"}',
        b'{"Id":NaN}',
        b"[]",
        b"\xff",
        b"x" * (2 * 1024 * 1024 + 1),
    ],
)
def test_docker_create_inspection_rejects_invalid_evidence(inspect_bytes: bytes) -> None:
    config = _config()
    with pytest.raises(GateProviderError):
        attest_docker_create(
            config,
            _request(config),
            "/srv/autoform/repository",
            inspect_bytes,
        )


@pytest.mark.parametrize(
    "content",
    [
        b"{}",
        b'{"schema":"autoform-docker-create-attestation/v1","schema":"duplicate"}',
        b'{"schema":NaN}',
        b"[]",
        b"\xff",
        b"x" * (64 * 1024 + 1),
    ],
)
def test_docker_create_attestation_loader_rejects_invalid_evidence(content: bytes) -> None:
    with pytest.raises(GateProviderError):
        DockerCreateAttestation.from_bytes(content)
