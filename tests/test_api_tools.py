from __future__ import annotations

from pathlib import Path

import pytest

from autoform.prover.api_tools import (
    MAX_ASSISTANT_CONTENT_BYTES,
    MAX_TOOL_CALLS_PER_TURN,
    ProjectTools,
    ToolPolicyError,
    run_tool_loop,
)


def test_project_tools_reject_escape_and_read_lines(tmp_path: Path):
    (tmp_path / "Proof.lean").write_text("line one\nline two\n", encoding="utf-8")
    tools = ProjectTools(tmp_path)
    assert tools.execute(
        "read_file", {"path": "Proof.lean", "start_line": 2, "end_line": 2}
    ) == "2: line two"
    with pytest.raises(ToolPolicyError, match="escapes"):
        tools.execute("read_file", {"path": "../secret"})


def test_read_only_tools_do_not_expose_write(tmp_path: Path):
    tools = ProjectTools(tmp_path, writable=False)
    names = {item["function"]["name"] for item in tools.definitions()}
    assert "write_lean_file" not in names
    with pytest.raises(ToolPolicyError, match="unavailable"):
        tools.execute("write_lean_file", {"path": "X.lean", "content": "x"})


def test_project_tools_hide_secret_like_project_files(tmp_path: Path):
    (tmp_path / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (tmp_path / "client.pem").write_text("private\n", encoding="utf-8")
    (tmp_path / "credentials.toml").write_text("password='hidden'\n", encoding="utf-8")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("cloud=hidden\n", encoding="utf-8")
    (tmp_path / "Proof.lean").write_text("theorem t : True := trivial\n")
    tools = ProjectTools(tmp_path)
    for path in (".env", "credentials.toml", ".aws/credentials"):
        with pytest.raises(ToolPolicyError, match="secret-like"):
            tools.execute("read_file", {"path": path})
    listed = tools.execute("list_files", {"pattern": "*"})
    assert "Proof.lean" in listed
    assert ".env" not in listed
    assert "client.pem" not in listed
    assert "credentials.toml" not in listed
    assert ".aws/credentials" not in listed
    searched = tools.execute("search_text", {"query": "secret"})
    assert "TOKEN" not in searched
    assert "private" not in searched
    assert "hidden" not in tools.execute("search_text", {"query": "hidden"})


def test_search_text_has_a_bounded_fallback_without_ripgrep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "Proof.lean").write_text(
        "theorem first : True := trivial\n"
        "theorem second : True := trivial\n",
        encoding="utf-8",
    )
    (tmp_path / "Notes.txt").write_text("theorem outside glob\n", encoding="utf-8")
    monkeypatch.setattr("autoform.prover.api_tools.shutil.which", lambda _: None)

    searched = ProjectTools(tmp_path).execute(
        "search_text", {"query": "theorem", "glob": "*.lean"}
    )

    assert "Proof.lean:1:theorem first" in searched
    assert "Proof.lean:2:theorem second" in searched
    assert "Notes.txt" not in searched


def test_write_is_lean_only_and_project_rooted(tmp_path: Path):
    tools = ProjectTools(tmp_path, writable=True)
    result = tools.execute(
        "write_lean_file", {"path": "Autoform/X.lean", "content": "import Mathlib\n"}
    )
    assert "wrote Autoform/X.lean" in result
    with pytest.raises(ToolPolicyError, match="only .lean"):
        tools.execute("write_lean_file", {"path": "notes.txt", "content": "secret"})


def test_lean_runner_rejects_arbitrary_executable_and_external_path(tmp_path: Path):
    tools = ProjectTools(tmp_path)
    with pytest.raises(ToolPolicyError, match="restricted"):
        tools.execute("run_lean", {"command": "lake", "args": ["env", "bash"]})
    with pytest.raises(ToolPolicyError, match="option"):
        tools.execute("run_lean", {"command": "lake", "args": ["build", "--help"]})
    with pytest.raises(ToolPolicyError, match="absolute"):
        tools.execute(
            "run_lean", {"command": "lean", "args": ["/tmp/outside.lean"]}
        )


def test_chat_tool_loop_executes_call_then_returns_final(tmp_path: Path):
    (tmp_path / "Proof.lean").write_text("theorem t : True := trivial\n")
    calls: list[dict] = []

    def transport(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"Proof.lean"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        assert payload["messages"][-1]["role"] == "tool"
        assert "theorem t" in payload["messages"][-1]["content"]
        return {
            "choices": [{"message": {"content": '{"score":5,"reasoning":"ok"}'}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 4},
        }

    final, usage, transcript = run_tool_loop(
        transport,
        url="https://example.invalid/chat/completions",
        headers={"Authorization": "Bearer test"},
        model="test",
        system_prompt="judge",
        user_prompt="inspect",
        tools=ProjectTools(tmp_path),
        timeout=1,
    )
    assert final == '{"score":5,"reasoning":"ok"}'
    assert usage == {"input_tokens": 30, "output_tokens": 6, "turns": 2}
    assert len(transcript) == 2


def _loop(tmp_path: Path, transport, **overrides):
    return run_tool_loop(
        transport,
        url="https://example.invalid/chat/completions",
        headers={"Authorization": "Bearer test"},
        model="test",
        system_prompt="system",
        user_prompt="user",
        tools=ProjectTools(tmp_path),
        timeout=overrides.pop("timeout", 1),
        **overrides,
    )


def test_tool_loop_returns_malformed_arguments_as_tool_error(tmp_path: Path):
    calls = 0

    def transport(_url, _headers, payload, _timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "id": "bad-json",
                            "function": {
                                "name": "read_file",
                                "arguments": "{not-json",
                            },
                        }]
                    }
                }]
            }
        assert "JSONDecodeError" in payload["messages"][-1]["content"]
        return {"choices": [{"message": {"content": "recovered"}}]}

    final, _, _ = _loop(tmp_path, transport)
    assert final == "recovered"


def test_tool_loop_enforces_turn_bound_under_repeated_calls(tmp_path: Path):
    (tmp_path / "Proof.lean").write_text("theorem t : True := trivial\n")
    calls = 0

    def transport(_url, _headers, _payload, _timeout):
        nonlocal calls
        calls += 1
        return {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": f"read-{calls}",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"Proof.lean"}',
                        },
                    }]
                }
            }]
        }

    with pytest.raises(RuntimeError, match="3-turn"):
        _loop(tmp_path, transport, max_turns=3)
    assert calls == 3


def test_tool_loop_rejects_oversized_fanout_before_executing(tmp_path: Path):
    tool_calls = [
        {
            "id": f"call-{index}",
            "function": {"name": "read_file", "arguments": '{"path":"missing"}'},
        }
        for index in range(MAX_TOOL_CALLS_PER_TURN + 1)
    ]

    def transport(_url, _headers, _payload, _timeout):
        return {"choices": [{"message": {"tool_calls": tool_calls}}]}

    with pytest.raises(RuntimeError, match="more than"):
        _loop(tmp_path, transport)


def test_tool_loop_rejects_duplicate_or_missing_call_ids(tmp_path: Path):
    for ids, message in [
        (["same", "same"], "repeated"),
        ([""], "non-empty"),
    ]:
        tool_calls = [
            {
                "id": call_id,
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }
            for call_id in ids
        ]

        def transport(_url, _headers, _payload, _timeout):
            return {"choices": [{"message": {"tool_calls": tool_calls}}]}

        with pytest.raises(RuntimeError, match=message):
            _loop(tmp_path, transport)


def test_tool_loop_rejects_oversized_assistant_message(tmp_path: Path):
    def transport(_url, _headers, _payload, _timeout):
        return {
            "choices": [{
                "message": {"content": "x" * (MAX_ASSISTANT_CONTENT_BYTES + 1)}
            }]
        }

    with pytest.raises(RuntimeError, match="content exceeds"):
        _loop(tmp_path, transport)


def test_tool_loop_rejects_reserved_payload_overrides(tmp_path: Path):
    with pytest.raises(ValueError, match="messages"):
        _loop(
            tmp_path,
            lambda *_args: {},
            extra_payload={"messages": [{"role": "system", "content": "replace"}]},
        )


@pytest.mark.parametrize(
    "response, message",
    [
        ([], "JSON object"),
        ({"choices": "bad"}, "no choices"),
        ({"choices": ["bad"]}, "choice must be"),
        ({"choices": [{"message": "bad"}]}, "message must be"),
        ({"choices": [{"message": {"tool_calls": "bad"}}]}, "must be an array"),
    ],
)
def test_tool_loop_rejects_malformed_wire_shapes(
    tmp_path: Path, response, message: str
):
    with pytest.raises(RuntimeError, match=message):
        _loop(tmp_path, lambda *_args: response)
