from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts import judge_runtime


def test_judge_children_never_inherit_parent_stdin(tmp_path, monkeypatch):
    captured = {}

    class Process:
        returncode = 0

        def communicate(self, timeout):
            return "", ""

    def fake_popen(args, **kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(judge_runtime.subprocess, "Popen", fake_popen)
    judge_runtime._run_process(
        ["judge"], repo=str(tmp_path), timeout=10
    )
    assert captured["stdin"] is subprocess.DEVNULL


def test_parse_score_understands_claude_structured_envelope():
    raw = '{"structured_output":{"score":4,"reasoning":"faithful"}}'
    assert judge_runtime.parse_score(raw, "faithfulness") == {
        "score": 4,
        "reasoning": "faithful",
    }


def test_codex_judge_uses_read_only_schema_and_last_message(tmp_path, monkeypatch):
    seen: list[str] = []
    seen_schema = {}

    def fake_process(args, *, repo, timeout, env=None):
        seen.extend(args)
        schema_path = Path(args[args.index("--output-schema") + 1])
        seen_schema.update(__import__("json").loads(schema_path.read_text()))
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text('{"score":5,"reasoning":"checked"}', encoding="utf-8")
        return "", "", 0

    monkeypatch.setattr(judge_runtime, "_run_process", fake_process)
    result = judge_runtime.run_judge(
        "proof_integrity",
        "rubric",
        str(tmp_path),
        None,
        10,
        backend="codex",
    )
    assert result["score"] == 5
    assert ["--sandbox", "read-only"] == seen[
        seen.index("--sandbox") : seen.index("--sandbox") + 2
    ]
    assert "--output-schema" in seen
    assert "--skip-git-repo-check" in seen
    assert "--dangerously-bypass-approvals-and-sandbox" not in seen
    assert set(seen_schema["required"]) == set(seen_schema["properties"])
    assert "null" in seen_schema["properties"]["error"]["type"]


def test_claude_judge_requests_json_schema(tmp_path, monkeypatch):
    seen: list[str] = []

    def fake_process(args, *, repo, timeout, env=None):
        seen.extend(args)
        return '{"structured_output":{"score":3,"reasoning":"mixed"}}', "", 0

    monkeypatch.setattr(judge_runtime, "_run_process", fake_process)
    result = judge_runtime.run_judge(
        "code_quality", "rubric", str(tmp_path), None, 10, backend="claude"
    )
    assert result["score"] == 3
    assert "--json-schema" in seen
    assert "--bare" not in seen  # bare disables the Max OAuth/keychain login
    assert seen[seen.index("--setting-sources") + 1] == "user"
    assert "--strict-mcp-config" in seen
    assert "--disable-slash-commands" in seen
    assert "disableAllHooks" in seen[seen.index("--settings") + 1]
    assert "dontAsk" in seen
    allowed = seen[seen.index("--allowedTools") + 1]
    assert "Bash(lake build *)" in allowed
    assert ",Bash," not in f",{allowed},"


def test_muse_judge_is_read_only_and_parses_terminal_json(tmp_path, monkeypatch):
    captured = {}
    runtime = tmp_path / "muse-runtime"
    monkeypatch.setenv("AUTOFORM_MUSE_BIN", "tbh-test")
    monkeypatch.setenv("AUTOFORM_MUSE_RUNTIME_DIR", str(runtime))

    def fake_process(args, *, repo, timeout, env=None):
        captured.update(args=args, repo=repo, timeout=timeout, env=env)
        stdout = json.dumps(
            {
                "schema_version": 1,
                "stream": {"kind": "session", "id": "judge-session"},
                "payload_type": "run.terminal.completed",
                "payload": {
                    "text": '{"score":4,"reasoning":"checked","error":null}',
                    "usage": {"input_tokens": 7, "output_tokens": 2},
                },
            }
        )
        return stdout, "", 0

    monkeypatch.setattr(judge_runtime, "_run_process", fake_process)
    result = judge_runtime.run_judge(
        "faithfulness", "rubric", str(tmp_path), None, 10, backend="muse"
    )
    assert result == {"score": 4, "reasoning": "checked"}
    args = captured["args"]
    assert args[:3] == ["tbh-test", "exec", "--json"]
    assert "--disable-write" in args
    assert "--disable-shell" in args
    assert "--disable-web-tools" in args
    assert "--disable-approval" in args
    assert "--yolo" not in args
    assert "--disable-sandbox" not in args
    assert captured["env"]["XDG_DATA_HOME"] == str(runtime.resolve())
    assert '"score"' in args[-1]


def test_muse_steer_judge_uses_shared_schema_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOFORM_MUSE_RUNTIME_DIR", str(tmp_path / "runtime"))

    def fake_process(args, *, repo, timeout, env=None):
        assert '"steer"' in args[-1]
        stdout = json.dumps(
            {
                "schema_version": 1,
                "stream": {"kind": "session", "id": "judge-session"},
                "payload_type": "run.terminal.completed",
                "payload": {
                    "text": '{"steer":false,"reason":"on course","prompt":""}',
                    "usage": {"input_tokens": 5, "output_tokens": 1},
                },
            }
        )
        return stdout, "", 0

    monkeypatch.setattr(judge_runtime, "_run_process", fake_process)
    raw, usage = judge_runtime.run_steer_judge(
        "events", str(tmp_path), None, 10, backend="muse"
    )
    assert '"steer": false' in raw
    assert usage == {"input_tokens": 5, "output_tokens": 1}


def test_api_judge_uses_read_only_tool_loop(tmp_path, monkeypatch):
    (tmp_path / "Proof.lean").write_text("theorem t : True := trivial\n")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "test-model")
    payloads: list[dict] = []

    def transport(url, headers, payload, timeout):
        payloads.append(payload)
        if len(payloads) == 1:
            return {
                "choices": [{
                    "message": {
                        "content": None,
                        "tool_calls": [{
                            "id": "read-1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"Proof.lean"}',
                            },
                        }],
                    },
                }],
            }
        return {
            "choices": [{"message": {"content": '{"score":5,"reasoning":"ok"}'}}]
        }

    result = judge_runtime.run_judge(
        "faithfulness",
        "rubric",
        str(tmp_path),
        None,
        10,
        backend="openai",
        transport=transport,
    )
    assert result["score"] == 5
    tool_names = {
        item["function"]["name"] for item in payloads[0]["tools"]
    }
    assert "read_file" in tool_names
    assert "write_lean_file" not in tool_names


def test_unknown_judge_fails_closed(tmp_path):
    result = judge_runtime.run_judge(
        "faithfulness", "rubric", str(tmp_path), None, 10, backend="codxe"
    )
    assert result["score"] is None
    assert result["error"] == "backend"


def test_api_judge_cannot_score_without_project_inspection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "test-model")

    def transport(url, headers, payload, timeout):
        return {
            "choices": [{"message": {"content": '{"score":5,"reasoning":"guessed"}'}}]
        }

    result = judge_runtime.run_judge(
        "faithfulness",
        "rubric",
        str(tmp_path),
        None,
        10,
        backend="openai",
        transport=transport,
    )
    assert result["score"] is None
    assert "without inspecting" in result["reasoning"]


def test_codex_steer_judge_uses_the_steer_schema(tmp_path, monkeypatch):
    seen_schema = {}

    def fake_process(args, *, repo, timeout, env=None):
        schema_path = Path(args[args.index("--output-schema") + 1])
        seen_schema.update(__import__("json").loads(schema_path.read_text()))
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_text(
            '{"steer":true,"reason":"looping","prompt":"try induction"}',
            encoding="utf-8",
        )
        return "", "", 0

    monkeypatch.setattr(judge_runtime, "_run_process", fake_process)
    raw, _ = judge_runtime.run_steer_judge(
        "events", str(tmp_path), None, 10, backend="codex"
    )
    assert '"steer": true' in raw
    assert seen_schema["properties"]["steer"]["type"] == "boolean"


def test_api_steer_judge_uses_selected_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("AUTOFORM_OPENAI_MODEL", "test-model")

    def transport(url, headers, payload, timeout):
        assert payload["model"] == "test-model"
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"steer":false,"reason":"on course","prompt":""}'
                        )
                    }
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }

    raw, usage = judge_runtime.run_steer_judge(
        "events",
        str(tmp_path),
        None,
        10,
        backend="openai",
        transport=transport,
    )
    assert '"steer": false' in raw
    assert usage == {"input_tokens": 3, "output_tokens": 2}
