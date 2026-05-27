# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for message_extract — OpenAI, Anthropic, and Gemini message formats."""

from core.message_extract import (
    extract_assistant,
    extract_text_content,
    extract_tool_results,
    has_visible_content,
)

# ---------------------------------------------------------------------------
# Fixtures: representative messages from each backend
# ---------------------------------------------------------------------------

OPENAI_ASSISTANT_TEXT = {"role": "assistant", "content": "Hello, world!"}

OPENAI_ASSISTANT_THINKING = {
    "role": "assistant",
    "content": "The answer is 42.",
    "thinking": "Let me think about this...",
}

OPENAI_ASSISTANT_TOOL_CALLS = {
    "role": "assistant",
    "content": "",
    "tool_calls": [
        {
            "id": "call_123",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "/tmp/foo.txt"}'},
        }
    ],
}

OPENAI_TOOL_RESULT = {
    "role": "tool",
    "tool_call_id": "call_123",
    "content": '{"ok": true, "text": "file contents here"}',
}

OPENAI_TOOL_RESULT_ERROR = {
    "role": "tool",
    "tool_call_id": "call_456",
    "content": '{"ok": false, "text": "permission denied"}',
}

ANTHROPIC_ASSISTANT = {
    "role": "assistant",
    "content": [
        {"type": "thinking", "thinking": "Hmm, let me consider..."},
        {"type": "text", "text": "Here is the answer."},
        {"type": "tool_use", "id": "tu_abc", "name": "bash", "input": {"command": "ls"}},
    ],
}

ANTHROPIC_ASSISTANT_TEXT_ONLY = {
    "role": "assistant",
    "content": [{"type": "text", "text": "Just text, no tools."}],
}

ANTHROPIC_TOOL_RESULT = {
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "tu_abc",
            "content": '{"ok": true, "text": "file1.py\\nfile2.py"}',
            "is_error": False,
        }
    ],
}

ANTHROPIC_TOOL_RESULT_ERROR = {
    "role": "user",
    "content": [
        {
            "type": "tool_result",
            "tool_use_id": "tu_err",
            "content": "something went wrong",
            "is_error": True,
        }
    ],
}

GEMINI_ASSISTANT = {
    "role": "model",
    "parts": [
        {"type": "text", "text": "Gemini says hello."},
        {"type": "function_call", "name": "write_file", "args": {"path": "/tmp/out.txt", "content": "data"}},
    ],
}

GEMINI_ASSISTANT_THINKING = {
    "role": "model",
    "parts": [
        {"type": "thinking", "text": "Gemini thinking..."},
        {"type": "text", "text": "Gemini answer."},
    ],
}

GEMINI_TOOL_RESULT = {
    "role": "user",
    "parts": [
        {
            "type": "function_response",
            "name": "write_file",
            "response": {"result": "written 4 bytes", "is_error": False},
        }
    ],
}


# ---------------------------------------------------------------------------
# extract_assistant
# ---------------------------------------------------------------------------


class TestExtractAssistant:
    def test_openai_text(self):
        ex = extract_assistant(OPENAI_ASSISTANT_TEXT)
        assert ex.text == "Hello, world!"
        assert ex.thinking == ""
        assert ex.tool_calls == []

    def test_openai_thinking(self):
        ex = extract_assistant(OPENAI_ASSISTANT_THINKING)
        assert ex.text == "The answer is 42."
        assert ex.thinking == "Let me think about this..."

    def test_openai_tool_calls(self):
        ex = extract_assistant(OPENAI_ASSISTANT_TOOL_CALLS)
        assert ex.text == ""
        assert len(ex.tool_calls) == 1
        tc = ex.tool_calls[0]
        assert tc.id == "call_123"
        assert tc.name == "read_file"
        assert tc.arguments == {"path": "/tmp/foo.txt"}

    def test_anthropic_full(self):
        ex = extract_assistant(ANTHROPIC_ASSISTANT)
        assert ex.text == "Here is the answer."
        assert ex.thinking == "Hmm, let me consider..."
        assert len(ex.tool_calls) == 1
        tc = ex.tool_calls[0]
        assert tc.id == "tu_abc"
        assert tc.name == "bash"
        assert tc.arguments == {"command": "ls"}

    def test_anthropic_text_only(self):
        ex = extract_assistant(ANTHROPIC_ASSISTANT_TEXT_ONLY)
        assert ex.text == "Just text, no tools."
        assert ex.thinking == ""
        assert ex.tool_calls == []

    def test_gemini_with_tool(self):
        ex = extract_assistant(GEMINI_ASSISTANT)
        assert ex.text == "Gemini says hello."
        assert len(ex.tool_calls) == 1
        tc = ex.tool_calls[0]
        assert tc.name == "write_file"
        assert tc.arguments == {"path": "/tmp/out.txt", "content": "data"}

    def test_gemini_thinking(self):
        ex = extract_assistant(GEMINI_ASSISTANT_THINKING)
        assert ex.text == "Gemini answer."
        assert ex.thinking == "Gemini thinking..."

    def test_empty_message(self):
        ex = extract_assistant({"role": "assistant"})
        assert ex.text == ""
        assert ex.thinking == ""
        assert ex.tool_calls == []


# ---------------------------------------------------------------------------
# extract_tool_results
# ---------------------------------------------------------------------------


class TestExtractToolResults:
    def test_openai(self):
        results = extract_tool_results([OPENAI_TOOL_RESULT, OPENAI_TOOL_RESULT_ERROR])
        text, is_error = results["call_123"]
        assert text == "file contents here"
        assert is_error is False
        text2, is_error2 = results["call_456"]
        assert text2 == "permission denied"
        assert is_error2 is True

    def test_anthropic(self):
        results = extract_tool_results([ANTHROPIC_TOOL_RESULT, ANTHROPIC_TOOL_RESULT_ERROR])
        text, is_error = results["tu_abc"]
        assert text == "file1.py\nfile2.py"
        assert is_error is False
        text2, is_error2 = results["tu_err"]
        assert text2 == "something went wrong"
        assert is_error2 is True

    def test_gemini(self):
        results = extract_tool_results([GEMINI_TOOL_RESULT])
        text, is_error = results["write_file"]
        assert text == "written 4 bytes"
        assert is_error is False

    def test_empty(self):
        assert extract_tool_results([]) == {}


# ---------------------------------------------------------------------------
# extract_text_content
# ---------------------------------------------------------------------------


class TestExtractTextContent:
    def test_openai(self):
        assert extract_text_content(OPENAI_ASSISTANT_TEXT) == "Hello, world!"

    def test_anthropic(self):
        assert extract_text_content(ANTHROPIC_ASSISTANT) == "Here is the answer."

    def test_anthropic_text_only(self):
        assert extract_text_content(ANTHROPIC_ASSISTANT_TEXT_ONLY) == "Just text, no tools."

    def test_gemini(self):
        assert extract_text_content(GEMINI_ASSISTANT) == "Gemini says hello."

    def test_empty(self):
        assert extract_text_content({}) == ""

    def test_none_content(self):
        assert extract_text_content({"role": "assistant", "content": None}) == ""


# ---------------------------------------------------------------------------
# has_visible_content
# ---------------------------------------------------------------------------


class TestHasVisibleContent:
    def test_user_with_text(self):
        assert has_visible_content({"role": "user", "content": "hello"}) is True

    def test_user_empty(self):
        assert has_visible_content({"role": "user", "content": ""}) is False

    def test_assistant_text(self):
        assert has_visible_content(OPENAI_ASSISTANT_TEXT) is True

    def test_assistant_tool_calls_only(self):
        assert has_visible_content(OPENAI_ASSISTANT_TOOL_CALLS) is True

    def test_assistant_empty(self):
        assert has_visible_content({"role": "assistant", "content": ""}) is False

    def test_tool_role_excluded(self):
        assert has_visible_content(OPENAI_TOOL_RESULT) is False

    def test_anthropic_assistant(self):
        assert has_visible_content(ANTHROPIC_ASSISTANT) is True

    def test_gemini_assistant(self):
        assert has_visible_content(GEMINI_ASSISTANT) is True

    def test_anthropic_user_tool_result(self):
        # Tool result user messages have no visible text
        assert has_visible_content(ANTHROPIC_TOOL_RESULT) is False
