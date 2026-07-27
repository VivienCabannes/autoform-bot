"""Bounded local tools and a Chat Completions tool loop for API-backed agents."""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MAX_TOOL_OUTPUT = 40_000
MAX_WRITE_BYTES = 1_000_000
MAX_ASSISTANT_CONTENT_BYTES = 1_200_000
MAX_TOOL_ARGUMENT_BYTES = 2_000_000
MAX_TOOL_CALLS_PER_TURN = 32
_RESERVED_PAYLOAD_KEYS = frozenset({"model", "messages", "tools", "tool_choice"})
_LEAN_NAME_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$"
)
_SENSITIVE_NAMES = frozenset(
    {
        ".env",
        ".envrc",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".zuliprc",
        "credentials",
        "credentials.json",
        "credentials.toml",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.toml",
        "secrets.yaml",
        "secrets.yml",
        "token.json",
    }
)
_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_SENSITIVE_DIRS = frozenset({".aws", ".git", ".gnupg", ".kube", ".ssh"})
_RG_SECRET_EXCLUDES = (
    "!.aws/**",
    "!.git/**",
    "!.gnupg/**",
    "!.kube/**",
    "!.ssh/**",
    "!.env",
    "!.env.*",
    "!.envrc",
    "!.zuliprc",
    "!credentials",
    "!credentials.json",
    "!credentials.toml",
    "!secrets.json",
    "!secrets.toml",
    "!secrets.yaml",
    "!secrets.yml",
    "!token.json",
    "!*.key",
    "!*.pem",
    "!*.p12",
    "!*.pfx",
)


class ToolPolicyError(ValueError):
    """The model requested an operation outside the declared local policy."""


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    head = limit * 3 // 5
    tail = limit - head
    return f"{text[:head]}\n...[{omitted} characters omitted]...\n{text[-tail:]}"


@dataclass
class ProjectTools:
    """Execute a small capability set rooted under one project directory."""

    root: Path
    writable: bool = False
    on_write: Callable[[Path, str], str] | None = None
    command_timeout: float = 120.0

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"tool root is not a directory: {self.root}")

    def _path(self, raw: str, *, must_exist: bool = False) -> Path:
        requested = Path(raw or ".")
        if requested.is_absolute():
            raise ToolPolicyError("absolute paths are not allowed")
        candidate = (self.root / requested).resolve(strict=False)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise ToolPolicyError("path escapes the project root") from error
        if self._is_sensitive(relative):
            raise ToolPolicyError("secret-like project paths are not exposed")
        if must_exist and not candidate.exists():
            raise ToolPolicyError(f"path does not exist: {raw}")
        return candidate

    @staticmethod
    def _is_sensitive(relative: Path) -> bool:
        if any(part in _SENSITIVE_DIRS for part in relative.parts):
            return True
        name = relative.name.lower()
        return (
            name in _SENSITIVE_NAMES
            or name.startswith(".env.")
            or name.endswith(_SENSITIVE_SUFFIXES)
        )

    def definitions(self) -> list[dict[str, Any]]:
        functions = [
            {
                "name": "read_file",
                "description": "Read a UTF-8 project file with optional 1-based line bounds.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                },
            },
            {
                "name": "list_files",
                "description": "List project-relative files matching a glob.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"pattern": {"type": "string"}},
                },
            },
            {
                "name": "search_text",
                "description": "Fixed-string search within project files.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "path": {"type": "string"},
                        "glob": {"type": "string"},
                    },
                },
            },
            {
                "name": "run_lean",
                "description": (
                    "Run an allowlisted Lean command: lake build, lake env lean, "
                    "or lean. Shell syntax is not accepted."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["command", "args"],
                    "properties": {
                        "command": {"type": "string", "enum": ["lake", "lean"]},
                        "args": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            {
                "name": "check_axioms",
                "description": (
                    "Import a Lean module and run #print axioms for one declaration "
                    "through a generated temporary probe."
                ),
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["module", "declaration"],
                    "properties": {
                        "module": {"type": "string"},
                        "declaration": {"type": "string"},
                    },
                },
            },
        ]
        if self.writable:
            functions.append(
                {
                    "name": "write_lean_file",
                    "description": "Write complete UTF-8 content to a project-relative .lean file.",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                }
            )
        return [{"type": "function", "function": function} for function in functions]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            return self._read_file(arguments)
        if name == "list_files":
            return self._list_files(arguments)
        if name == "search_text":
            return self._search_text(arguments)
        if name == "run_lean":
            return self._run_lean(arguments)
        if name == "check_axioms":
            return self._check_axioms(arguments)
        if name == "write_lean_file" and self.writable:
            return self._write_lean(arguments)
        raise ToolPolicyError(f"unknown or unavailable tool: {name}")

    def _read_file(self, args: dict[str, Any]) -> str:
        path = self._path(str(args.get("path", "")), must_exist=True)
        if not path.is_file():
            raise ToolPolicyError("read_file target is not a file")
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = min(len(lines), int(args.get("end_line") or len(lines)))
        if end < start:
            raise ToolPolicyError("end_line precedes start_line")
        rendered = "\n".join(
            f"{number}: {lines[number - 1]}" for number in range(start, end + 1)
        )
        return _clip(rendered)

    def _list_files(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern") or "**/*")
        paths = [
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if (
                path.is_file()
                and not self._is_sensitive(path.relative_to(self.root))
                and fnmatch.fnmatch(str(path.relative_to(self.root)), pattern)
            )
        ]
        return _clip("\n".join(sorted(paths)[:1000]))

    def _search_text(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "")
        if not query:
            raise ToolPolicyError("query must not be empty")
        scope = self._path(str(args.get("path") or "."), must_exist=True)
        glob = str(args.get("glob") or "*")
        command = [
            "rg",
            "--fixed-strings",
            "--line-number",
            "--no-heading",
            "--max-count",
            "200",
            "--glob",
            glob,
        ]
        for exclusion in _RG_SECRET_EXCLUDES:
            command.extend(["--glob", exclusion])
        command.extend(["--", query, str(scope)])
        process = subprocess.run(
            command,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=self.command_timeout,
            check=False,
        )
        if process.returncode not in (0, 1):
            raise ToolPolicyError(f"rg failed: {_clip(process.stderr, 2000)}")
        return _clip(process.stdout)

    def _run_lean(self, args: dict[str, Any]) -> str:
        executable = str(args.get("command") or "")
        argv = args.get("args")
        if executable not in {"lake", "lean"} or not isinstance(argv, list):
            raise ToolPolicyError("invalid Lean command")
        argv = [str(value) for value in argv]
        if any("\x00" in value for value in argv):
            raise ToolPolicyError("NUL bytes are not allowed")
        if executable == "lake":
            if argv and argv[0] == "build":
                for value in argv[1:]:
                    if value in {"-q", "--quiet"}:
                        continue
                    if value.startswith("-"):
                        raise ToolPolicyError("lake build option is not allowlisted")
                    if not re.fullmatch(r"[A-Za-z0-9_.:'-]+", value):
                        raise ToolPolicyError("unsafe lake build target or option")
            elif argv[:2] == ["env", "lean"]:
                argv = ["env", "lean", *self._validated_lean_args(argv[2:])]
            else:
                raise ToolPolicyError("lake is restricted to build or env lean")
        else:
            argv = self._validated_lean_args(argv)
        process = subprocess.run(
            [executable, *argv],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=self.command_timeout,
            check=False,
        )
        return _clip(
            f"exit_code={process.returncode}\nSTDOUT:\n{process.stdout}\nSTDERR:\n{process.stderr}"
        )

    def _validated_lean_args(self, argv: list[str]) -> list[str]:
        if not argv:
            raise ToolPolicyError("lean needs a project-relative .lean file")
        validated: list[str] = []
        saw_file = False
        for value in argv:
            if value in {"-q", "--quiet", "--json"} or value.startswith("-D"):
                validated.append(value)
                continue
            if value.startswith("-"):
                raise ToolPolicyError(f"Lean option is not allowlisted: {value}")
            if not value.endswith(".lean"):
                raise ToolPolicyError("Lean positional arguments must be .lean files")
            path = self._path(value, must_exist=True)
            if not path.is_file():
                raise ToolPolicyError("Lean input is not a file")
            validated.append(str(path.relative_to(self.root)))
            saw_file = True
        if not saw_file:
            raise ToolPolicyError("lean needs a project-relative .lean file")
        return validated

    def _check_axioms(self, args: dict[str, Any]) -> str:
        module = str(args.get("module") or "")
        declaration = str(args.get("declaration") or "")
        if not _LEAN_NAME_RE.fullmatch(module) or not _LEAN_NAME_RE.fullmatch(declaration):
            raise ToolPolicyError("module and declaration must be Lean identifiers")
        probe = f"import {module}\n#print axioms {declaration}\n"
        path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".lean", prefix="autoform-axioms-", delete=False
            ) as handle:
                handle.write(probe)
                path = handle.name
            process = subprocess.run(
                ["lake", "env", "lean", path],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
            return _clip(
                f"exit_code={process.returncode}\n{process.stdout}\n{process.stderr}"
            )
        finally:
            if path:
                Path(path).unlink(missing_ok=True)

    def _write_lean(self, args: dict[str, Any]) -> str:
        path = self._path(str(args.get("path") or ""))
        content = str(args.get("content") or "")
        if path.suffix != ".lean":
            raise ToolPolicyError("only .lean files may be written")
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ToolPolicyError("write exceeds the one-megabyte limit")
        if self.on_write is not None:
            return self.on_write(path, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"wrote {path.relative_to(self.root)} ({len(content)} characters)"


def run_tool_loop(
    transport: Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]],
    *,
    url: str,
    headers: dict[str, str],
    model: str,
    system_prompt: str,
    user_prompt: str,
    tools: ProjectTools,
    timeout: float,
    max_turns: int = 16,
    extra_payload: dict[str, Any] | None = None,
) -> tuple[str, dict[str, int], list[dict[str, Any]]]:
    """Run an OpenAI Chat Completions function-call loop."""
    if timeout <= 0:
        raise ValueError("tool-loop timeout must be positive")
    if not 1 <= max_turns <= 64:
        raise ValueError("max_turns must be between 1 and 64")
    reserved = _RESERVED_PAYLOAD_KEYS & set(extra_payload or {})
    if reserved:
        raise ValueError(
            "extra_payload may not override tool-loop contract keys: "
            + ", ".join(sorted(reserved))
        )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    transcript: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    for _ in range(max_turns):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"provider tool loop exceeded {timeout}s")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": tools.definitions(),
            "tool_choice": "auto",
        }
        payload.update(extra_payload or {})
        response = transport(url, headers, payload, remaining)
        if not isinstance(response, dict):
            raise RuntimeError("provider response must be a JSON object")
        raw_usage = response.get("usage") or {}
        usage["input_tokens"] += int(
            raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0
        )
        usage["output_tokens"] += int(
            raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0
        )
        usage["turns"] += 1
        choices = response.get("choices") or []
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("provider returned no choices")
        if not isinstance(choices[0], dict):
            raise RuntimeError("provider choice must be an object")
        raw_message = choices[0].get("message") or {}
        if not isinstance(raw_message, dict):
            raise RuntimeError("provider message must be an object")
        message = dict(raw_message)
        content = str(message.get("content") or "")
        if len(content.encode("utf-8")) > MAX_ASSISTANT_CONTENT_BYTES:
            raise RuntimeError("provider assistant content exceeds the bounded limit")
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise RuntimeError("provider tool_calls must be an array")
        if len(tool_calls) > MAX_TOOL_CALLS_PER_TURN:
            raise RuntimeError(
                f"provider returned more than {MAX_TOOL_CALLS_PER_TURN} tool calls in one turn"
            )
        call_ids: set[str] = set()
        for call in tool_calls:
            if not isinstance(call, dict):
                raise RuntimeError("provider tool call must be an object")
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id:
                raise RuntimeError("provider tool call has no non-empty id")
            if call_id in call_ids:
                raise RuntimeError(f"provider repeated tool call id {call_id!r}")
            call_ids.add(call_id)
            function = call.get("function")
            if not isinstance(function, dict):
                raise RuntimeError("provider tool call function must be an object")
            raw_arguments = function.get("arguments")
            if not isinstance(raw_arguments, str):
                raise RuntimeError("provider tool arguments must be a JSON string")
            if len(raw_arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
                raise RuntimeError("provider tool arguments exceed the bounded limit")
        transcript.append({"content": content, "tool_calls": len(tool_calls)})
        assistant: dict[str, Any] = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)
        if not tool_calls:
            return content, usage, transcript
        for call in tool_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            try:
                arguments = json.loads(function.get("arguments") or "{}")
                if not isinstance(arguments, dict):
                    raise ToolPolicyError("tool arguments must be an object")
                output = tools.execute(name, arguments)
            except Exception as error:  # return policy/runtime errors to the model
                output = f"ERROR: {type(error).__name__}: {error}"
            output = _clip(str(output))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": output,
                }
            )
    raise RuntimeError(f"provider exceeded the {max_turns}-turn tool-loop limit")
