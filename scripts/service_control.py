#!/usr/bin/env python3
"""Manage Autoform's local review and blueprint web services.

On macOS, child processes started by an assistant task can be reaped when that
task ends, even when they were launched with ``nohup``.  This helper installs
project-scoped launchd jobs instead.  The jobs bind only to loopback, restart
after an unexpected exit, and keep their configuration and logs inside the
project's ``.autoform`` directory.

Examples:

    python service_control.py start review \
      --project /path/to/plan --plugin-root /path/to/autoform \
      --graph /path/to/plan/graph.json --lean-root /path/to/lean --port 0

    python service_control.py start blueprint \
      --project /path/to/plan \
      --directory /path/to/plan/blueprint_export/blueprint/web --port 8005

    python service_control.py status --project /path/to/plan
    python service_control.py stop --project /path/to/plan --service all
"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class ServiceError(RuntimeError):
    """A user-actionable service configuration or launch failure."""


@dataclass(frozen=True)
class ServiceSpec:
    """Complete launchd description for one project-local service."""

    name: str
    project: Path
    label: str
    port: int
    command: tuple[str, ...]
    working_directory: Path

    @property
    def state_directory(self) -> Path:
        return self.project / ".autoform" / "services"

    @property
    def log_directory(self) -> Path:
        return self.project / ".autoform" / "logs"

    @property
    def plist_path(self) -> Path:
        return self.state_directory / f"{self.name}.plist"

    @property
    def stdout_path(self) -> Path:
        return self.log_directory / f"{self.name}.out.log"

    @property
    def stderr_path(self) -> Path:
        return self.log_directory / f"{self.name}.err.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


def _resolved_directory(value: str | Path, description: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ServiceError(f"{description} is not a directory: {path}")
    return path


def _resolved_file(value: str | Path, description: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ServiceError(f"{description} does not exist: {path}")
    return path


def project_key(project: Path) -> str:
    """Return the stable, compact launchd namespace for *project*."""

    return hashlib.sha256(os.fsencode(str(project.resolve()))).hexdigest()[:12]


def service_label(project: Path, name: str) -> str:
    if name not in {"review", "blueprint"}:
        raise ServiceError(f"unknown service: {name}")
    return f"com.autoform.{project_key(project)}.{name}"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _existing_port(plist_path: Path) -> int | None:
    """Read the configured port from an Autoform-generated plist."""

    try:
        data = plistlib.loads(plist_path.read_bytes())
        arguments = [str(value) for value in data["ProgramArguments"]]
    except (FileNotFoundError, KeyError, OSError, plistlib.InvalidFileException):
        return None
    for flag in ("--port",):
        if flag in arguments:
            position = arguments.index(flag) + 1
            if position < len(arguments):
                try:
                    return int(arguments[position])
                except ValueError:
                    return None
    # ``python -m http.server <port>`` has no --port flag.
    if "http.server" in arguments:
        position = arguments.index("http.server") + 1
        if position < len(arguments):
            try:
                return int(arguments[position])
            except ValueError:
                return None
    return None


def _select_port(requested: int, plist_path: Path) -> int:
    if not 0 <= requested <= 65535:
        raise ServiceError(f"port must be between 0 and 65535, got {requested}")
    if requested:
        return requested
    return _existing_port(plist_path) or _free_loopback_port()


def review_spec(
    *,
    project: str | Path,
    plugin_root: str | Path,
    graph: str | Path,
    lean_root: str | Path,
    port: int,
) -> ServiceSpec:
    project_path = _resolved_directory(project, "project")
    plugin_path = _resolved_directory(plugin_root, "plugin root")
    graph_path = _resolved_file(graph, "graph")
    lean_path = _resolved_directory(lean_root, "Lean root")
    server_path = _resolved_file(
        plugin_path / "scripts" / "review_ui" / "serve_review.py",
        "review server",
    )
    uv = shutil.which("uv")
    if not uv:
        raise ServiceError("uv is required to run the review dashboard")
    placeholder = project_path / ".autoform" / "services" / "review.plist"
    selected_port = _select_port(port, placeholder)
    command = (
        str(Path(uv).absolute()),
        "run",
        "--directory",
        str(plugin_path),
        "python",
        "-u",
        str(server_path),
        "--graph",
        str(graph_path),
        "--lean-root",
        str(lean_path),
        "--port",
        str(selected_port),
    )
    return ServiceSpec(
        name="review",
        project=project_path,
        label=service_label(project_path, "review"),
        port=selected_port,
        command=command,
        working_directory=project_path,
    )


def blueprint_spec(
    *,
    project: str | Path,
    directory: str | Path,
    port: int,
    python: str | Path | None = None,
) -> ServiceSpec:
    project_path = _resolved_directory(project, "project")
    web_path = _resolved_directory(directory, "blueprint web directory")
    if not (web_path / "index.html").is_file():
        raise ServiceError(f"blueprint web directory has no index.html: {web_path}")
    if python is None:
        python_path = Path(sys.executable).resolve()
    else:
        python_path = _resolved_file(python, "Python executable")
    if not python_path.is_file():
        raise ServiceError(f"Python executable does not exist: {python_path}")
    placeholder = project_path / ".autoform" / "services" / "blueprint.plist"
    selected_port = _select_port(port, placeholder)
    command = (
        str(python_path),
        "-u",
        "-m",
        "http.server",
        str(selected_port),
        "--bind",
        "127.0.0.1",
        "--directory",
        str(web_path),
    )
    return ServiceSpec(
        name="blueprint",
        project=project_path,
        label=service_label(project_path, "blueprint"),
        port=selected_port,
        command=command,
        working_directory=project_path,
    )


def launchd_plist(spec: ServiceSpec) -> dict[str, object]:
    """Return the launchd payload for *spec* without mutating the filesystem."""

    path_entries = {
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        str(Path(spec.command[0]).parent),
    }
    service_path = ":".join(sorted(path_entries))
    return {
        "Label": spec.label,
        "Program": spec.command[0],
        "ProgramArguments": list(spec.command),
        "WorkingDirectory": str(spec.working_directory),
        "EnvironmentVariables": {"PATH": service_path},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 3,
        "StandardOutPath": str(spec.stdout_path),
        "StandardErrorPath": str(spec.stderr_path),
    }


def _write_plist(spec: ServiceSpec) -> bytes:
    spec.state_directory.mkdir(parents=True, exist_ok=True)
    spec.log_directory.mkdir(parents=True, exist_ok=True)
    payload = plistlib.dumps(launchd_plist(spec), fmt=plistlib.FMT_XML, sort_keys=True)
    temporary = spec.plist_path.with_suffix(".plist.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, spec.plist_path)
    return payload


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ServiceError(
            "durable Autoform services currently require macOS launchd; "
            "use the documented foreground command on this host"
        )
    if not shutil.which("launchctl"):
        raise ServiceError("launchctl was not found")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target(label: str) -> str:
    return f"{_domain()}/{label}"


def _launchctl(
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["launchctl", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(
            f"launchctl {' '.join(arguments)} failed"
            + (f": {detail}" if detail else "")
        )
    return result


def _job_details(label: str) -> str | None:
    result = _launchctl(["print", _target(label)], check=False)
    return result.stdout if result.returncode == 0 else None


def _job_pid(details: str | None) -> int | None:
    if not details:
        return None
    match = re.search(r"(?m)^\s*pid = (\d+)\s*$", details)
    return int(match.group(1)) if match else None


def _wait_until_listening(spec: ServiceSpec, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_is_open(spec.port):
            return
        time.sleep(0.1)
    tail = ""
    try:
        lines = spec.stderr_path.read_text(errors="replace").splitlines()
        if lines:
            tail = f"\nLast error: {lines[-1]}"
    except OSError:
        pass
    raise ServiceError(
        f"{spec.name} service did not listen on 127.0.0.1:{spec.port} "
        f"within {timeout:g} seconds{tail}"
    )


def _wait_until_closed(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_is_open(port):
            return
        time.sleep(0.1)
    raise ServiceError(
        f"the previous service did not release 127.0.0.1:{port} "
        f"within {timeout:g} seconds"
    )


def start_service(spec: ServiceSpec, *, timeout: float = 60.0) -> str:
    """Install or refresh *spec*, wait for readiness, and return its URL."""

    _require_macos()
    desired = plistlib.dumps(launchd_plist(spec), fmt=plistlib.FMT_XML, sort_keys=True)
    current = None
    try:
        current = spec.plist_path.read_bytes()
    except OSError:
        pass
    details = _job_details(spec.label)

    if current == desired and details is not None:
        if not _port_is_open(spec.port):
            _launchctl(["kickstart", "-k", _target(spec.label)])
        _wait_until_listening(spec, timeout)
        return spec.url

    if details is not None:
        _launchctl(["bootout", _target(spec.label)])
        previous_port = _existing_port(spec.plist_path)
        if previous_port:
            _wait_until_closed(previous_port)
    if _port_is_open(spec.port):
        raise ServiceError(
            f"127.0.0.1:{spec.port} is already used by another process; "
            "choose a different port"
        )
    _write_plist(spec)
    _launchctl(["bootstrap", _domain(), str(spec.plist_path)])
    _wait_until_listening(spec, timeout)
    return spec.url


def stop_service(project: Path, name: str) -> bool:
    """Unload one named project service. Return whether it had been loaded."""

    _require_macos()
    label = service_label(project, name)
    if _job_details(label) is None:
        return False
    _launchctl(["bootout", _target(label)])
    return True


def status_rows(project: Path, names: Sequence[str]) -> list[dict[str, object]]:
    """Return display-friendly status without changing service state."""

    rows: list[dict[str, object]] = []
    for name in names:
        plist_path = project / ".autoform" / "services" / f"{name}.plist"
        port = _existing_port(plist_path)
        details = _job_details(service_label(project, name))
        rows.append(
            {
                "service": name,
                "loaded": details is not None,
                "pid": _job_pid(details),
                "port": port,
                "responding": bool(port and _port_is_open(port)),
                "url": f"http://127.0.0.1:{port}/" if port else None,
            }
        )
    return rows


def _service_names(value: str) -> tuple[str, ...]:
    return ("review", "blueprint") if value == "all" else (value,)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage durable, loopback-only Autoform web services on macOS."
    )
    actions = parser.add_subparsers(dest="action", required=True)

    start = actions.add_parser("start", help="install or refresh a service")
    starts = start.add_subparsers(dest="service", required=True)
    review = starts.add_parser("review", help="start the review dashboard")
    review.add_argument("--project", required=True)
    review.add_argument("--plugin-root", required=True)
    review.add_argument("--graph", required=True)
    review.add_argument("--lean-root", required=True)
    review.add_argument("--port", type=int, default=0)
    review.add_argument("--timeout", type=float, default=60)

    blueprint = starts.add_parser("blueprint", help="serve a built blueprint")
    blueprint.add_argument("--project", required=True)
    blueprint.add_argument("--directory", required=True)
    blueprint.add_argument("--port", type=int, default=8005)
    blueprint.add_argument("--python")
    blueprint.add_argument("--timeout", type=float, default=30)

    status = actions.add_parser("status", help="show service state")
    status.add_argument("--project", required=True)
    status.add_argument(
        "--service", choices=("review", "blueprint", "all"), default="all"
    )

    stop = actions.add_parser("stop", help="unload a service")
    stop.add_argument("--project", required=True)
    stop.add_argument(
        "--service", choices=("review", "blueprint", "all"), default="all"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "start":
            if args.service == "review":
                spec = review_spec(
                    project=args.project,
                    plugin_root=args.plugin_root,
                    graph=args.graph,
                    lean_root=args.lean_root,
                    port=args.port,
                )
            else:
                spec = blueprint_spec(
                    project=args.project,
                    directory=args.directory,
                    port=args.port,
                    python=args.python,
                )
            url = start_service(spec, timeout=args.timeout)
            print(f"{spec.name} service → {url}")
            print(f"  launchd: {spec.label} (automatic restart enabled)")
            print(f"  logs:    {spec.log_directory}")
            return 0

        project = _resolved_directory(args.project, "project")
        names = _service_names(args.service)
        if args.action == "stop":
            for name in names:
                stopped = stop_service(project, name)
                print(f"{name}: {'stopped' if stopped else 'not loaded'}")
            return 0

        _require_macos()
        for row in status_rows(project, names):
            state = "responding" if row["responding"] else (
                "loaded, not responding" if row["loaded"] else "stopped"
            )
            pid = f", pid {row['pid']}" if row["pid"] else ""
            url = f" — {row['url']}" if row["url"] else ""
            print(f"{row['service']}: {state}{pid}{url}")
        return 0
    except ServiceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
