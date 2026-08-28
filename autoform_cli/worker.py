"""Cron-friendly supervision for unattended Autoform Codex workers.

The public command is deliberately a single-shot reconciler.  Cron provides
the durable scheduling loop; each invocation starts, observes, resumes, or
recovers one detached Codex turn and then exits.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import psutil

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as tomllib


CONFIG_NAME = ".aiworker"
STATE_VERSION = 1
TERMINAL_STATUSES = frozenset({"complete", "blocked", "needs_input"})
RESULT_STATUSES = TERMINAL_STATUSES | {"continue"}
LONG_ITEM_TYPES = frozenset({"command_execution", "mcp_tool_call"})
RETRY_MARKERS = ("retry", "api error", "connection", "rate limit", "stream disconnected")

RESULT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": sorted(RESULT_STATUSES)},
        "summary": {"type": "string"},
        "next_action": {"type": "string"},
    },
    "required": ["status", "summary", "next_action"],
}


class WorkerError(RuntimeError):
    """A configuration or safety error that should be shown to the operator."""


@dataclass(frozen=True)
class WorkerConfig:
    """Validated contents of a repository-local ``.aiworker`` file."""

    objective: str
    stale_after_seconds: float
    tool_stale_after_seconds: float
    max_restarts_per_hour: int


@dataclass(frozen=True)
class WorkerPaths:
    """Private host-side state for one real repository path."""

    directory: Path
    lock: Path
    state: Path
    schema: Path
    runs: Path

    @classmethod
    def for_repository(cls, repository: Path, state_home: Path | None = None) -> WorkerPaths:
        if state_home is None:
            configured = os.environ.get("XDG_STATE_HOME")
            if configured:
                state_home = Path(configured).expanduser()
                if not state_home.is_absolute():
                    raise WorkerError("XDG_STATE_HOME must be an absolute path")
            else:
                state_home = Path.home() / ".local" / "state"
        digest = hashlib.sha256(os.fsencode(repository)).hexdigest()[:16]
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", repository.name).strip("-.") or "repository"
        directory = state_home / "autoform" / "workers" / f"{label}-{digest}"
        return cls(
            directory=directory,
            lock=directory / "reconcile.lock",
            state=directory / "state.json",
            schema=directory / "result-schema.json",
            runs=directory / "runs",
        )

    def run_files(self, generation: int) -> tuple[Path, Path, Path]:
        stem = f"{generation:06d}"
        return (
            self.runs / f"{stem}.events.jsonl",
            self.runs / f"{stem}.stderr.log",
            self.runs / f"{stem}.result.json",
        )


def reconcile(
    repository: str | Path,
    *,
    now: float | None = None,
    state_home: Path | None = None,
    codex_bin: str | None = None,
    require_linux: bool = True,
) -> int:
    """Reconcile one unattended worker and return a CLI-style exit code."""

    try:
        if require_linux and not sys.platform.startswith("linux"):
            raise WorkerError("autoform worker currently supports Linux only")
        current_time = time.time() if now is None else now
        root = _repository_root(Path(repository))
        paths = WorkerPaths.for_repository(root, state_home)
        _prepare_state_directory(paths)

        with _state_lock(paths.lock) as acquired:
            if not acquired:
                print(f"autoform worker: another reconciliation owns {root}")
                return 0

            state = _read_state(paths.state)
            if state is not None and state.get("repository") != str(root):
                raise WorkerError(f"worker state belongs to another repository: {paths.state}")

            config_path = root / CONFIG_NAME
            if not config_path.exists() and not config_path.is_symlink():
                if state is None:
                    raise WorkerError(f"missing {config_path}")
                if _owned_process(state):
                    _terminate_owned_process(state)
                _reap_child(_integer(state.get("pid"), default=0))
                state.pop("pid", None)
                state.pop("process_created_at", None)
                state.pop("process_group", None)
                state["status"] = "disabled"
                state["summary"] = f"{CONFIG_NAME} was removed"
                state["finished_at"] = current_time
                _write_state(paths.state, state)
                print(f"autoform worker: disabled; recreate {config_path} to start again")
                return 0

            config = _load_config(root)
            objective_hash = hashlib.sha256(config.objective.encode("utf-8")).hexdigest()

            if state is not None:
                _ingest_run(state, paths, current_time)

            if state is not None and state.get("status") == "disabled":
                generation = _integer(state.get("generation"), default=0)
                state = _new_state(root, objective_hash, generation=generation)
                print("autoform worker: configuration restored; starting a new Codex session")
            elif state is not None and state.get("objective_hash") != objective_hash:
                if _owned_process(state):
                    _terminate_owned_process(state)
                generation = _integer(state.get("generation"), default=0)
                state = _new_state(root, objective_hash, generation=generation)
                print("autoform worker: objective changed; starting a new Codex session")

            if state is None:
                state = _new_state(root, objective_hash)

            if _owned_process(state):
                return _reconcile_running(state, config, paths, current_time)

            _reap_child(_integer(state.get("pid"), default=0))
            state.pop("pid", None)
            state.pop("process_created_at", None)
            state.pop("process_group", None)

            status = state.get("status")
            if status in TERMINAL_STATUSES:
                _write_state(paths.state, state)
                print(f"autoform worker: {status}: {state.get('summary', '')}")
                return 0

            if status == "waiting":
                not_before = _number(state.get("not_before"), default=0.0)
                if current_time < not_before:
                    wait = max(1, round(not_before - current_time))
                    print(f"autoform worker: recovery paused for {wait}s: {state.get('wait_reason', '')}")
                    _write_state(paths.state, state)
                    return 0
                return _launch(state, config, paths, current_time, codex_bin=codex_bin)

            if status == "new":
                return _launch(state, config, paths, current_time, codex_bin=codex_bin)

            if _integer(state.get("generation"), default=0) > 0:
                result = _read_result(state, paths)
                if result is not None:
                    if result["status"] in TERMINAL_STATUSES:
                        state.update(result)
                        state["finished_at"] = current_time
                        state["status"] = result["status"]
                        _write_state(paths.state, state)
                        print(f"autoform worker: {result['status']}: {result['summary']}")
                        return 0
                    state["status"] = "ready"
                    state["summary"] = result["summary"]
                    state["next_action"] = result["next_action"]
                    return _launch(state, config, paths, current_time, codex_bin=codex_bin)

                reason = "Codex exited without a valid structured result"
                return _schedule_recovery(state, config, paths, current_time, reason)

            return _launch(state, config, paths, current_time, codex_bin=codex_bin)
    except WorkerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _repository_root(candidate: Path) -> Path:
    candidate = candidate.expanduser().resolve()
    if not candidate.is_dir():
        raise WorkerError(f"repository does not exist or is not a directory: {candidate}")
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise WorkerError(f"cannot run git: {exc}") from exc
    if result.returncode != 0:
        raise WorkerError(f"not a Git repository: {candidate}")
    root = Path(result.stdout.strip()).resolve()
    if root != candidate:
        raise WorkerError(f"pass the repository root instead: {root}")
    return root


def _load_config(repository: Path) -> WorkerConfig:
    path = repository / CONFIG_NAME
    if path.is_symlink():
        raise WorkerError(f"refusing symlinked configuration: {path}")
    if not path.is_file():
        raise WorkerError(f"missing {path}")

    tracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--error-unmatch", "--", CONFIG_NAME],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if tracked.returncode == 0:
        raise WorkerError(f"{path} must not be tracked by Git")
    ignored = subprocess.run(
        ["git", "-C", str(repository), "check-ignore", "-q", "--", CONFIG_NAME],
        check=False,
    )
    if ignored.returncode != 0:
        raise WorkerError(f"{path} must be ignored; add {CONFIG_NAME} to .git/info/exclude")

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise WorkerError(f"cannot read {path}: {exc}") from exc
    allowed = {
        "version",
        "objective",
        "stale_after_minutes",
        "tool_stale_after_minutes",
        "max_restarts_per_hour",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise WorkerError(f"unknown {CONFIG_NAME} key(s): {', '.join(unknown)}")
    if raw.get("version") != 1:
        raise WorkerError(f"{CONFIG_NAME} version must be 1")
    objective = raw.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise WorkerError(f"{CONFIG_NAME} objective must be a non-empty string")
    stale_minutes = _positive_number(raw.get("stale_after_minutes", 15), "stale_after_minutes")
    tool_minutes = _positive_number(
        raw.get("tool_stale_after_minutes", 60),
        "tool_stale_after_minutes",
    )
    if tool_minutes < stale_minutes:
        raise WorkerError("tool_stale_after_minutes must be at least stale_after_minutes")
    max_restarts = raw.get("max_restarts_per_hour", 3)
    if isinstance(max_restarts, bool) or not isinstance(max_restarts, int) or not 1 <= max_restarts <= 20:
        raise WorkerError("max_restarts_per_hour must be an integer between 1 and 20")
    return WorkerConfig(
        objective=objective.strip(),
        stale_after_seconds=stale_minutes * 60,
        tool_stale_after_seconds=tool_minutes * 60,
        max_restarts_per_hour=max_restarts,
    )


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise WorkerError(f"{name} must be a positive number")
    return float(value)


def _prepare_state_directory(paths: WorkerPaths) -> None:
    paths.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.directory, 0o700)
    os.chmod(paths.runs, 0o700)
    if not paths.schema.exists():
        _atomic_json_write(paths.schema, RESULT_SCHEMA)


@contextlib.contextmanager
def _state_lock(path: Path) -> Iterator[bool]:
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - the CLI rejects non-Linux hosts
        raise WorkerError("autoform worker requires POSIX file locking") from exc
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise WorkerError(f"cannot open worker lock {path}: {exc}") from exc
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(descriptor)


def _new_state(repository: Path, objective_hash: str, *, generation: int = 0) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "repository": str(repository),
        "objective_hash": objective_hash,
        "generation": generation,
        "status": "new",
        "session_id": None,
        "restarts": [],
    }


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise WorkerError(f"refusing symlinked worker state: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerError(f"cannot read worker state {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise WorkerError(f"unsupported worker state in {path}")
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    _atomic_json_write(path, state)


def _atomic_json_write(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise WorkerError(f"cannot write worker state {path}: {exc}") from exc


def _reconcile_running(
    state: dict[str, Any],
    config: WorkerConfig,
    paths: WorkerPaths,
    now: float,
) -> int:
    last_progress = _number(state.get("last_progress_at"), default=now)
    active_items = state.get("active_long_items")
    long_running = isinstance(active_items, list) and bool(active_items)
    limit = config.tool_stale_after_seconds if long_running else config.stale_after_seconds
    elapsed = max(0.0, now - last_progress)
    if elapsed <= limit:
        state["stale_observations"] = 0
        _write_state(paths.state, state)
        detail = "long tool call" if long_running else "JSONL progress"
        print(f"autoform worker: healthy pid {state['pid']} ({detail}, {round(elapsed)}s ago)")
        return 0

    observations = _integer(state.get("stale_observations"), default=0) + 1
    state["stale_observations"] = observations
    if observations < 2:
        _write_state(paths.state, state)
        print(f"autoform worker: possible stall pid {state['pid']}; awaiting confirmation")
        return 0

    retry_count = _integer(state.get("retry_errors"), default=0)
    reason = f"no structured progress for {round(elapsed)}s"
    if retry_count:
        reason += f" after {retry_count} retry-related error(s)"
    _terminate_owned_process(state)
    _reap_child(_integer(state.get("pid"), default=0))
    state.pop("pid", None)
    state.pop("process_created_at", None)
    state.pop("process_group", None)
    return _schedule_recovery(state, config, paths, now, reason)


def _launch(
    state: dict[str, Any],
    config: WorkerConfig,
    paths: WorkerPaths,
    now: float,
    *,
    codex_bin: str | None,
) -> int:
    executable = _codex_binary(codex_bin)
    generation = _integer(state.get("generation"), default=0) + 1
    events_path, errors_path, result_path = paths.run_files(generation)
    for path in (events_path, errors_path, result_path):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    session_id = state.get("session_id")
    if isinstance(session_id, str) and session_id:
        prompt = (
            "Use $autoform:worker and $autoform:orchestrate. Continue the same unattended objective. "
            "Inspect the previous turn and repository state, then make the next verified unit of progress."
        )
        command = [
            executable,
            "exec",
            "resume",
            "--json",
            "--full-auto",
            "--output-schema",
            str(paths.schema),
            "-o",
            str(result_path),
            session_id,
            prompt,
        ]
        action = "resumed"
    else:
        prompt = (
            "Use $autoform:worker and $autoform:orchestrate. Work unattended in this repository on the "
            "following local operator objective:\n\n"
            f"{config.objective}\n\n"
            "Do not edit .aiworker."
        )
        command = [
            executable,
            "exec",
            "--json",
            "--full-auto",
            "--output-schema",
            str(paths.schema),
            "-o",
            str(result_path),
            prompt,
        ]
        action = "started"

    process: subprocess.Popen[bytes] | None = None
    try:
        with events_path.open("wb", buffering=0) as events, errors_path.open("wb", buffering=0) as errors:
            process = subprocess.Popen(
                command,
                cwd=state["repository"],
                stdin=subprocess.DEVNULL,
                stdout=events,
                stderr=errors,
                close_fds=True,
                start_new_session=True,
            )
        created_at = psutil.Process(process.pid).create_time()
    except (OSError, psutil.Error) as exc:
        if process is not None:
            _terminate_new_process(process)
        raise WorkerError(f"cannot launch Codex: {exc}") from exc

    state.update(
        {
            "generation": generation,
            "status": "running",
            "pid": process.pid,
            "process_created_at": created_at,
            "process_group": process.pid,
            "launched_at": now,
            "last_progress_at": now,
            "event_offset": 0,
            "error_offset": 0,
            "stale_observations": 0,
            "retry_errors": 0,
            "active_long_items": [],
        }
    )
    state.pop("not_before", None)
    state.pop("wait_reason", None)
    try:
        _write_state(paths.state, state)
    except WorkerError:
        _terminate_owned_process(state)
        _reap_child(process.pid)
        raise
    print(f"autoform worker: {action} Codex pid {process.pid}; events: {events_path}")
    return 0


def _codex_binary(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("AUTOFORM_CODEX_BIN") or shutil.which("codex")
    if not candidate:
        raise WorkerError("codex executable not found; set AUTOFORM_CODEX_BIN to an absolute path")
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        resolved = shutil.which(str(path))
        if resolved:
            path = Path(resolved)
    path = path.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise WorkerError(f"codex executable is not an executable file: {path}")
    return str(path)


def _ingest_run(state: dict[str, Any], paths: WorkerPaths, now: float) -> None:
    generation = _integer(state.get("generation"), default=0)
    if generation <= 0:
        return
    events_path, errors_path, _ = paths.run_files(generation)
    progressed = False
    stored_active = state.get("active_long_items", [])
    if not isinstance(stored_active, list):
        stored_active = []
    active = {item for item in stored_active if isinstance(item, str)}
    event_offset = _integer(state.get("event_offset"), default=0)
    lines, event_offset = _complete_lines(events_path, event_offset)
    for line in lines:
        try:
            event = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            state["invalid_event_lines"] = _integer(state.get("invalid_event_lines"), default=0) + 1
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) and isinstance(event.get("thread"), dict):
                thread_id = event["thread"].get("id")
            if isinstance(thread_id, str) and thread_id:
                state["session_id"] = thread_id
        item = event.get("item")
        if isinstance(item, dict):
            item_id = item.get("id")
            item_type = item.get("type")
            if event_type == "item.started" and isinstance(item_id, str) and item_type in LONG_ITEM_TYPES:
                active.add(item_id)
            elif event_type in {"item.completed", "item.failed"} and isinstance(item_id, str):
                active.discard(item_id)
        if event_type == "error":
            message = json.dumps(event, ensure_ascii=False).casefold()
            if any(marker in message for marker in RETRY_MARKERS):
                state["retry_errors"] = _integer(state.get("retry_errors"), default=0) + 1
        elif isinstance(event_type, str):
            progressed = True
    state["event_offset"] = event_offset

    error_offset = _integer(state.get("error_offset"), default=0)
    error_lines, error_offset = _complete_lines(errors_path, error_offset)
    for line in error_lines:
        lowered = line.casefold()
        if any(marker in lowered for marker in RETRY_MARKERS):
            state["retry_errors"] = _integer(state.get("retry_errors"), default=0) + 1
    state["error_offset"] = error_offset
    state["active_long_items"] = sorted(active)
    if progressed:
        state["last_progress_at"] = now
        state["stale_observations"] = 0


def _complete_lines(path: Path, offset: int) -> tuple[list[str], int]:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], 0
    if offset < 0 or offset > size:
        offset = 0
    try:
        with path.open("rb") as stream:
            stream.seek(offset)
            payload = stream.read()
    except OSError as exc:
        raise WorkerError(f"cannot read worker log {path}: {exc}") from exc
    complete_size = payload.rfind(b"\n") + 1
    if complete_size == 0:
        return [], offset
    complete = payload[:complete_size]
    return complete.decode("utf-8", errors="replace").splitlines(), offset + complete_size


def _read_result(state: dict[str, Any], paths: WorkerPaths) -> dict[str, str] | None:
    generation = _integer(state.get("generation"), default=0)
    _, _, result_path = paths.run_files(generation)
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"status", "summary", "next_action"}:
        return None
    if payload.get("status") not in RESULT_STATUSES:
        return None
    if not isinstance(payload.get("summary"), str) or not isinstance(payload.get("next_action"), str):
        return None
    return payload


def _schedule_recovery(
    state: dict[str, Any],
    config: WorkerConfig,
    paths: WorkerPaths,
    now: float,
    reason: str,
) -> int:
    stored_restarts = state.get("restarts", [])
    if not isinstance(stored_restarts, list):
        stored_restarts = []
    recent = [
        timestamp
        for timestamp in stored_restarts
        if isinstance(timestamp, (int, float)) and now - float(timestamp) < 3600
    ]
    recent.append(now)
    state["restarts"] = recent
    state["status"] = "waiting"
    state["wait_reason"] = reason
    if len(recent) > config.max_restarts_per_hour:
        state["not_before"] = float(recent[0]) + 3600
        message = "restart circuit open"
    else:
        delay = min(60 * (2 ** (len(recent) - 1)), 900)
        state["not_before"] = now + delay
        message = f"retrying in {delay}s"
    _write_state(paths.state, state)
    print(f"autoform worker: {message}: {reason}")
    return 0


def _owned_process(state: dict[str, Any]) -> bool:
    pid = _integer(state.get("pid"), default=0)
    created_at = _number(state.get("process_created_at"), default=-1.0)
    process_group = _integer(state.get("process_group"), default=0)
    if pid <= 0 or created_at < 0 or process_group != pid:
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE or not process.is_running():
            return False
        if abs(process.create_time() - created_at) > 0.01:
            return False
        return os.getpgid(pid) == process_group
    except (ProcessLookupError, PermissionError, psutil.Error):
        return False


def _terminate_owned_process(state: dict[str, Any]) -> None:
    if not _owned_process(state):
        return
    process_group = _integer(state.get("process_group"), default=0)
    for signum, grace in ((signal.SIGINT, 3.0), (signal.SIGTERM, 3.0), (signal.SIGKILL, 1.0)):
        if not _owned_process(state):
            return
        try:
            os.killpg(process_group, signum)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            raise WorkerError(f"cannot signal Codex process group {process_group}: {exc}") from exc
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _owned_process(state):
                return
            time.sleep(0.05)
    if _owned_process(state):
        raise WorkerError(f"Codex process group {process_group} survived SIGKILL")


def _terminate_new_process(process: subprocess.Popen[bytes]) -> None:
    """Best-effort cleanup before a new process has durable identity state."""

    if process.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)


def _reap_child(pid: int) -> None:
    if pid <= 0:
        return
    with contextlib.suppress(ChildProcessError, ProcessLookupError, OSError):
        os.waitpid(pid, os.WNOHANG)


def _integer(value: object, *, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _number(value: object, *, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default
