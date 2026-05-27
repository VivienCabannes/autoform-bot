# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for core.agent.Agent."""

import asyncio

import pytest

from core.agent import Agent, AgentConfig, AgentDefinition
from core.inference import InferenceProtocol, PromptTooLongError, TokenUsage, ToolCall, ToolResult, TurnResult


def _make_agent(definition: AgentDefinition, inference: InferenceProtocol) -> Agent:
    """Create an Agent with runtime attributes initialized for testing (without __aenter__)."""
    agent = Agent(definition, inference=inference)
    agent._idle_event = asyncio.Event()
    agent._idle_event.set()
    return agent


def _defn(
    system_prompt: str = "You are a helpful assistant with access to tools.",
    config: AgentConfig | None = None,
    **kwargs,
) -> AgentDefinition:
    """Shorthand for creating an AgentDefinition in tests."""
    return AgentDefinition(
        name="test",
        system_prompt=system_prompt,
        config=config or AgentConfig(),
        **kwargs,
    )


class MockInference(InferenceProtocol):
    """Mock inference that returns canned responses."""

    def __init__(self, responses: list[TurnResult] | None = None):
        self.responses = responses or [
            TurnResult(
                text="Hello! I'm a mock agent.", usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
            )
        ]
        self._call_count = 0
        self._system_prompt = ""
        self._messages: list[dict] = []

    @property
    def model_name(self) -> str:
        return "mock-model"

    async def complete(self, *, tools=None, inference_config=None):
        idx = min(self._call_count, len(self.responses) - 1)
        result = self.responses[idx]
        self._call_count += 1
        result = TurnResult(
            text=result.text,
            thinking=result.thinking,
            tool_calls=result.tool_calls,
            usage=result.usage,
            model=self.model_name,
            call_id=f"mock-{self._call_count}",
            finish_reason=result.finish_reason,
        )
        # Auto-append assistant message (mimics real backend)
        msg = {"role": "assistant", "content": result.text}
        if result.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in result.tool_calls
            ]
        self._messages.append(msg)
        return result

    def add_user_message(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self._messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})

    def set_system_prompt(self, prompt: str) -> None:
        self._system_prompt = prompt

    def get_system_prompt(self) -> str:
        return self._system_prompt

    def reset(self) -> None:
        self._messages.clear()

    def get_messages(self) -> list[dict]:
        return [{"role": "system", "content": self._system_prompt}] + list(self._messages)

    def replace_history(self, summary: str) -> None:
        self._messages.clear()
        self._messages.append({"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"})

    def replace_messages(self, messages: list[dict]) -> None:
        self._messages = list(messages)

    def cleanup_interrupted(self) -> None:
        while self._messages:
            last = self._messages[-1]
            role = last.get("role", "")
            if role == "tool":
                self._messages.pop()
                continue
            if role == "assistant":
                if last.get("tool_calls"):
                    self._messages.pop()
                    continue
                content = last.get("content", "")
                if not content or (isinstance(content, str) and not content.strip()):
                    self._messages.pop()
                    continue
            break


def test_agent_creation():
    """Agent can be created without MCP servers."""
    inference = MockInference()
    agent = _make_agent(_defn(system_prompt="Test prompt"), inference=inference)
    assert agent.id
    assert agent.max_turns == 200


@pytest.mark.asyncio
async def test_agent_call():
    """Agent can make a basic call without tools."""
    inference = MockInference()
    agent = _make_agent(_defn(), inference=inference)
    # Manually set up (no MCP servers to enter)
    agent.reset()
    result = await agent.call("Hello")
    assert result == "Hello! I'm a mock agent."


@pytest.mark.asyncio
async def test_agent_turn_limit():
    """Agent respects max_turns."""
    inference = MockInference()
    agent = _make_agent(_defn(max_turns=1), inference=inference)
    agent.reset()
    result = await agent.call("First call")
    assert result == "Hello! I'm a mock agent."

    # Second call should hit turn limit
    result = await agent.call("Second call")
    assert result == ""


def test_agent_reset():
    """Agent reset clears messages and turn counter."""
    inference = MockInference()
    agent = _make_agent(_defn(system_prompt="Test"), inference=inference)
    inference.add_user_message("Hi")
    agent.total_turns = 5
    agent.reset()
    assert len(inference.get_messages()) == 1  # just system message
    assert agent.total_turns == 0


def test_compact_token_limit():
    """compact_token_limit returns correct threshold or None."""
    # Disabled when context_window is None
    agent = _make_agent(_defn(config=AgentConfig(context_window=None)), inference=MockInference())
    assert agent._compact_token_limit is None

    # Enabled (uses default context_window=200000)
    agent = _make_agent(_defn(), inference=MockInference())
    assert agent._compact_token_limit == 150000

    # Custom
    agent = _make_agent(
        _defn(config=AgentConfig(context_window=100000, compact_threshold=0.75)),
        inference=MockInference(),
    )
    assert agent._compact_token_limit == 75000


@pytest.mark.asyncio
async def test_compaction_triggers_when_over_threshold():
    """Agent compacts messages when input tokens exceed threshold."""

    class HighTokenInference(InferenceProtocol):
        """Returns high input_tokens to trigger compaction."""

        def __init__(self):
            self._call_count = 0
            self._system_prompt = ""
            self._messages: list[dict] = []

        @property
        def model_name(self) -> str:
            return "mock-model"

        async def complete(self, *, tools=None, inference_config=None):
            self._call_count += 1
            # First call: return tool call to build up conversation
            if self._call_count == 1:
                tc = [ToolCall(id="tc1", name="test_tool", arguments="{}")]
                result = TurnResult(
                    text="",
                    tool_calls=tc,
                    usage=TokenUsage(input_tokens=50, output_tokens=10, total_tokens=60),
                    model=self.model_name,
                )
                self._messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "tc1", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}}
                        ],
                    }
                )
                return result
            # Second call: high token count triggers compaction, returns final answer
            if self._call_count == 2:
                result = TurnResult(
                    text="Final answer.",
                    usage=TokenUsage(input_tokens=8000, output_tokens=10, total_tokens=8010),
                    model=self.model_name,
                )
                self._messages.append({"role": "assistant", "content": "Final answer."})
                return result
            # Summary call (from _compact)
            result = TurnResult(
                text="Summary of conversation so far.",
                usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
                model=self.model_name,
            )
            self._messages.append({"role": "assistant", "content": "Summary of conversation so far."})
            return result

        def add_user_message(self, content: str) -> None:
            self._messages.append({"role": "user", "content": content})

        def add_tool_results(self, results: list[ToolResult]) -> None:
            for r in results:
                self._messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})

        def set_system_prompt(self, prompt: str) -> None:
            self._system_prompt = prompt

        def get_system_prompt(self) -> str:
            return self._system_prompt

        def reset(self) -> None:
            self._messages.clear()

        def get_messages(self) -> list[dict]:
            return [{"role": "system", "content": self._system_prompt}] + list(self._messages)

        def replace_history(self, summary: str) -> None:
            self._messages.clear()
            self._messages.append(
                {"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"}
            )

        def replace_messages(self, messages: list[dict]) -> None:
            self._messages = list(messages)

        def cleanup_interrupted(self) -> None:
            pass

    inference = HighTokenInference()
    agent = _make_agent(
        _defn(
            system_prompt="System prompt",
            config=AgentConfig(context_window=10000, compact_threshold=0.75),
            max_turns=5,
        ),
        inference=inference,
    )
    agent.reset()

    # Manually build up a long conversation to have enough messages for compaction
    inference.add_user_message("Do the task")
    for i in range(6):
        inference._messages.append({"role": "assistant", "content": f"Step {i}"})
        inference._messages.append({"role": "user", "content": f"Result {i}"})

    result = await agent.call()
    assert result == "Final answer."
    # Verify compaction happened: summary call was made (call_count >= 3)
    assert inference._call_count >= 3


@pytest.mark.asyncio
async def test_compaction_preserves_structure():
    """Compacted messages produce [system, user_summary]."""

    class SummaryInference(InferenceProtocol):
        def __init__(self):
            self._system_prompt = ""
            self._messages: list[dict] = []

        @property
        def model_name(self) -> str:
            return "mock-model"

        async def complete(self, *, tools=None, inference_config=None):
            result = TurnResult(
                text="This is the summary.",
                usage=TokenUsage(input_tokens=100, output_tokens=50, total_tokens=150),
                model=self.model_name,
            )
            self._messages.append({"role": "assistant", "content": "This is the summary."})
            return result

        def add_user_message(self, content: str) -> None:
            self._messages.append({"role": "user", "content": content})

        def add_tool_results(self, results: list[ToolResult]) -> None:
            for r in results:
                self._messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})

        def set_system_prompt(self, prompt: str) -> None:
            self._system_prompt = prompt

        def get_system_prompt(self) -> str:
            return self._system_prompt

        def reset(self) -> None:
            self._messages.clear()

        def get_messages(self) -> list[dict]:
            return [{"role": "system", "content": self._system_prompt}] + list(self._messages)

        def replace_history(self, summary: str) -> None:
            self._messages.clear()
            self._messages.append(
                {"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"}
            )

        def replace_messages(self, messages: list[dict]) -> None:
            self._messages = list(messages)

        def cleanup_interrupted(self) -> None:
            pass

    inference = SummaryInference()
    agent = _make_agent(
        _defn(
            system_prompt="System",
            config=AgentConfig(context_window=10000, compact_threshold=0.75),
        ),
        inference=inference,
    )
    agent.reset()

    # Build messages: user + 10 middle
    inference.add_user_message("Original task")
    for i in range(10):
        inference._messages.append({"role": "assistant", "content": f"Response {i}"})

    await agent._compact()

    messages = inference.get_messages()
    # After compaction: system + user_summary
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "[Context summary" in messages[1]["content"]


@pytest.mark.asyncio
async def test_no_compaction_when_disabled():
    """No compaction when context_window is None."""
    inference = MockInference()
    agent = _make_agent(_defn(config=AgentConfig(context_window=None)), inference=inference)
    assert agent._compact_token_limit is None
    assert not agent._should_compact_from_usage(999999)


@pytest.mark.asyncio
async def test_truncated_tool_call_detected():
    """Agent drops tool calls and retries when output is truncated."""
    inference = MockInference(
        responses=[
            # First call: truncated tool call (finish_reason="length")
            TurnResult(
                text="",
                tool_calls=[ToolCall(id="tc1", name="write_file", arguments="{}")],
                usage=TokenUsage(input_tokens=100, output_tokens=4096, total_tokens=4196),
                finish_reason="length",
            ),
            # Second call: successful text response (model adapted)
            TurnResult(
                text="Done, I broke it into smaller steps.",
                usage=TokenUsage(input_tokens=200, output_tokens=50, total_tokens=250),
            ),
        ]
    )
    agent = _make_agent(_defn(), inference=inference)
    agent.reset()

    result = await agent.call("Write a very large file")
    assert result == "Done, I broke it into smaller steps."
    # Should have called LLM twice: truncated + retry
    assert inference._call_count == 2
    # The retry message should be in the conversation
    messages = inference.get_messages()
    truncation_msgs = [m for m in messages if "malformed" in m.get("content", "").lower()]
    assert len(truncation_msgs) >= 1


@pytest.mark.asyncio
async def test_truncated_tool_call_detected_via_flag():
    """Agent drops tool calls when arguments are truncated (invalid JSON), even with finish_reason='stop'."""
    inference = MockInference(
        responses=[
            # First call: truncated tool call args, but finish_reason="stop" (some providers return stop instead of length)
            TurnResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="scratchpad_write",
                        arguments='{"path": "plan.md", "content": "# Long content that got cut',
                        truncated=True,
                    )
                ],
                usage=TokenUsage(input_tokens=100, output_tokens=4096, total_tokens=4196),
                finish_reason="stop",
            ),
            # Second call: successful text response (model adapted)
            TurnResult(
                text="Done, I broke it into smaller steps.",
                usage=TokenUsage(input_tokens=200, output_tokens=50, total_tokens=250),
            ),
        ]
    )
    agent = _make_agent(_defn(), inference=inference)
    agent.reset()

    result = await agent.call("Write a very large file")
    assert result == "Done, I broke it into smaller steps."
    assert inference._call_count == 2
    messages = inference.get_messages()
    truncation_msgs = [m for m in messages if "malformed" in m.get("content", "").lower()]
    assert len(truncation_msgs) >= 1


@pytest.mark.asyncio
async def test_reactive_compaction_on_prompt_too_long():
    """Agent catches PromptTooLongError, compacts, and retries."""

    class PromptTooLongThenOkInference(InferenceProtocol):
        """Raises PromptTooLongError on first call, succeeds after compaction."""

        def __init__(self):
            self._call_count = 0
            self._system_prompt = ""
            self._messages: list[dict] = []
            self.compacted = False

        @property
        def model_name(self) -> str:
            return "mock-model"

        async def complete(self, *, tools=None, inference_config=None):
            self._call_count += 1
            # If this is a summarizer call (system prompt changed for compaction)
            if "summarizer" in self._system_prompt.lower():
                result = TurnResult(
                    text="Summary of prior conversation.",
                    usage=TokenUsage(input_tokens=50, output_tokens=20, total_tokens=70),
                    model=self.model_name,
                )
                self._messages.append({"role": "assistant", "content": result.text})
                return result
            # First real call: prompt too long
            if self._call_count == 1:
                raise PromptTooLongError("context length exceeded")
            # After compaction: succeed
            result = TurnResult(
                text="Answer after compaction.",
                usage=TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
                model=self.model_name,
            )
            self._messages.append({"role": "assistant", "content": result.text})
            return result

        def add_user_message(self, content: str) -> None:
            self._messages.append({"role": "user", "content": content})

        def add_tool_results(self, results: list[ToolResult]) -> None:
            for r in results:
                self._messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})

        def set_system_prompt(self, prompt: str) -> None:
            self._system_prompt = prompt

        def get_system_prompt(self) -> str:
            return self._system_prompt

        def reset(self) -> None:
            self._messages.clear()

        def get_messages(self) -> list[dict]:
            return [{"role": "system", "content": self._system_prompt}] + list(self._messages)

        def replace_history(self, summary: str) -> None:
            self.compacted = True
            self._messages.clear()
            self._messages.append(
                {"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"}
            )

        def replace_messages(self, messages: list[dict]) -> None:
            self._messages = list(messages)

        def cleanup_interrupted(self) -> None:
            pass

    inference = PromptTooLongThenOkInference()
    agent = _make_agent(
        _defn(
            system_prompt="System",
            config=AgentConfig(context_window=10000, compact_threshold=0.75),
            max_turns=5,
        ),
        inference=inference,
    )
    agent.reset()

    # Build enough messages for compaction to have material
    inference.add_user_message("Do something big")
    for i in range(6):
        inference._messages.append({"role": "assistant", "content": f"Step {i}"})
        inference._messages.append({"role": "user", "content": f"Continue {i}"})

    result = await agent.call()
    assert result == "Answer after compaction."
    assert inference.compacted


@pytest.mark.asyncio
async def test_reactive_compaction_exhausted_raises():
    """Agent re-raises PromptTooLongError after max reactive compaction attempts."""

    class AlwaysPromptTooLongInference(InferenceProtocol):
        def __init__(self):
            self._system_prompt = ""
            self._messages: list[dict] = []

        @property
        def model_name(self) -> str:
            return "mock-model"

        async def complete(self, *, tools=None, inference_config=None):
            # Summarizer calls succeed
            if "summarizer" in self._system_prompt.lower():
                result = TurnResult(
                    text="Summary.",
                    usage=TokenUsage(input_tokens=50, output_tokens=20, total_tokens=70),
                    model=self.model_name,
                )
                self._messages.append({"role": "assistant", "content": result.text})
                return result
            raise PromptTooLongError("still too long")

        def add_user_message(self, content: str) -> None:
            self._messages.append({"role": "user", "content": content})

        def add_tool_results(self, results: list[ToolResult]) -> None:
            for r in results:
                self._messages.append({"role": "tool", "tool_call_id": r.tool_call_id, "content": r.content})

        def set_system_prompt(self, prompt: str) -> None:
            self._system_prompt = prompt

        def get_system_prompt(self) -> str:
            return self._system_prompt

        def reset(self) -> None:
            self._messages.clear()

        def get_messages(self) -> list[dict]:
            return [{"role": "system", "content": self._system_prompt}] + list(self._messages)

        def replace_history(self, summary: str) -> None:
            self._messages.clear()
            self._messages.append(
                {"role": "user", "content": f"[Context summary of previous conversation]\n\n{summary}"}
            )

        def replace_messages(self, messages: list[dict]) -> None:
            self._messages = list(messages)

        def cleanup_interrupted(self) -> None:
            pass

    inference = AlwaysPromptTooLongInference()
    agent = _make_agent(
        _defn(
            system_prompt="System",
            config=AgentConfig(context_window=10000, compact_threshold=0.75),
            max_turns=10,
        ),
        inference=inference,
    )
    agent.reset()

    # Build enough messages for compaction
    inference.add_user_message("Big task")
    for i in range(6):
        inference._messages.append({"role": "assistant", "content": f"Step {i}"})
        inference._messages.append({"role": "user", "content": f"Continue {i}"})

    with pytest.raises(PromptTooLongError):
        await agent.call()


@pytest.mark.asyncio
async def test_output_continuation_on_truncated_text():
    """Agent requests continuation when text output is truncated."""
    inference = MockInference(
        responses=[
            # First call: truncated text-only output (finish_reason="length", no tool calls)
            TurnResult(
                text="This is a long response that got cut",
                usage=TokenUsage(input_tokens=100, output_tokens=4096, total_tokens=4196),
                finish_reason="length",
            ),
            # Second call: continuation completes
            TurnResult(
                text="Done with the full response.",
                usage=TokenUsage(input_tokens=200, output_tokens=50, total_tokens=250),
            ),
        ]
    )
    agent = _make_agent(_defn(), inference=inference)
    agent.reset()

    result = await agent.call("Write something long")
    assert result == "Done with the full response."
    assert inference._call_count == 2
    # Continuation prompt should be in the conversation
    messages = inference.get_messages()
    continuation_msgs = [m for m in messages if "Continue exactly" in m.get("content", "")]
    assert len(continuation_msgs) == 1


@pytest.mark.asyncio
async def test_truncated_tool_calls_add_error_tool_results():
    """Truncated tool calls must produce error tool_result blocks before the retry prompt.

    Without this, the conversation history contains assistant tool_use blocks
    followed by a plain user message — which the Anthropic API rejects (400)
    because every tool_use must have a corresponding tool_result.
    """
    inference = MockInference(
        responses=[
            # First call: tool calls with truncated=True (malformed arguments)
            TurnResult(
                text="",
                tool_calls=[
                    ToolCall(id="tc1", name="add_item", arguments="{bad", truncated=True),
                    ToolCall(id="tc2", name="add_item", arguments="{bad", truncated=True),
                ],
                usage=TokenUsage(input_tokens=100, output_tokens=200, total_tokens=300),
            ),
            # Second call: normal response after retry
            TurnResult(
                text="Done.",
                usage=TokenUsage(input_tokens=100, output_tokens=10, total_tokens=110),
            ),
        ]
    )
    agent = _make_agent(_defn(), inference=inference)
    agent.reset()

    result = await agent.call("Add some items")
    assert result == "Done."
    assert inference._call_count == 2

    # Verify message sequence: after the assistant message with tool_calls,
    # there must be tool_result messages BEFORE the user retry prompt.
    messages = inference.get_messages()
    # Find the assistant message with tool_calls
    tool_use_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            tool_use_idx = i
            break
    assert tool_use_idx is not None, "Expected an assistant message with tool_calls"

    # Next messages should be tool results (one per truncated tool call)
    assert messages[tool_use_idx + 1]["role"] == "tool", (
        f"Expected tool_result after tool_use, got {messages[tool_use_idx + 1]['role']}"
    )
    assert messages[tool_use_idx + 2]["role"] == "tool", (
        f"Expected second tool_result after tool_use, got {messages[tool_use_idx + 2]['role']}"
    )
    # Then the user retry prompt
    assert messages[tool_use_idx + 3]["role"] == "user"
    assert "malformed" in messages[tool_use_idx + 3]["content"]
