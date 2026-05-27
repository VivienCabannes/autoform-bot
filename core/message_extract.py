# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Format-agnostic extraction of text, thinking, and tool calls from messages.

Supports three message formats:
- **OpenAI**: ``content`` is a string, ``tool_calls`` is a list of function dicts
- **Anthropic**: ``content`` is a list of typed blocks (``text``, ``thinking``, ``tool_use``)
- **Gemini**: ``parts`` is a list of typed blocks (``text``, ``function_call``, ``function_response``)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum


@dataclass(frozen=True)
class ExtractedToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ExtractedAssistant:
    text: str = ""
    thinking: str = ""
    tool_calls: list[ExtractedToolCall] = field(default_factory=list)


def extract_assistant(msg: dict) -> ExtractedAssistant:
    """Extract text, thinking, and tool calls from an assistant message."""
    content = msg.get("content")
    parts = msg.get("parts")

    # Gemini format: keyed by "parts"
    if parts and isinstance(parts, list):
        return _extract_gemini_assistant(parts)

    # Anthropic format: content is a list of typed blocks
    if isinstance(content, list):
        return _extract_anthropic_assistant(content)

    # OpenAI format: content is a string (or None)
    text = content if isinstance(content, str) else ""
    thinking = msg.get("thinking", "")
    tool_calls = _extract_openai_tool_calls(msg.get("tool_calls"))
    return ExtractedAssistant(text=text, thinking=thinking, tool_calls=tool_calls)


def extract_tool_results(messages: list[dict]) -> dict[str, tuple[str, bool]]:
    """Build ``call_id -> (result_text, is_error)`` map from all messages."""
    result_map: dict[str, tuple[str, bool]] = {}
    for msg in messages:
        role = msg.get("role", "")

        # OpenAI: role=="tool" messages
        if role == "tool":
            call_id = msg.get("tool_call_id", "")
            content = msg.get("content", "")
            is_error = False
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    is_error = parsed.get("ok") is False
                    content = parsed.get("text", content)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
            result_map[call_id] = (content if isinstance(content, str) else str(content), is_error)

        # Anthropic: tool_result blocks inside user messages
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    call_id = block.get("tool_use_id", "")
                    result_content = block.get("content", "")
                    is_error = bool(block.get("is_error", False))
                    if isinstance(result_content, str):
                        try:
                            parsed = json.loads(result_content)
                            is_error = is_error or parsed.get("ok") is False
                            result_content = parsed.get("text", result_content)
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            pass
                    result_map[call_id] = (
                        result_content if isinstance(result_content, str) else str(result_content),
                        is_error,
                    )

        # Gemini: function_response blocks in parts
        parts = msg.get("parts")
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "function_response":
                    name = part.get("name", "")
                    response = part.get("response", {})
                    result_text = response.get("result", "") if isinstance(response, dict) else str(response)
                    is_error = response.get("is_error", False) if isinstance(response, dict) else False
                    result_map[name] = (
                        result_text if isinstance(result_text, str) else str(result_text),
                        bool(is_error),
                    )

    return result_map


def extract_text_content(msg: dict) -> str:
    """Return plain text from any message format (for ``/compact`` and ``/save``)."""
    content = msg.get("content")
    parts = msg.get("parts")

    # Gemini format
    if parts and isinstance(parts, list):
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(texts)

    # Anthropic format
    if isinstance(content, list):
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(texts)

    # OpenAI format
    if isinstance(content, str):
        return content
    return ""


class Language(StrEnum):
    """Known code-block languages with their fence tag aliases.

    ``tags`` returns the markdown fence tags an LLM may emit for this
    language (e.g. ``("lean4", "lean")``), preferred variant first.
    Only languages with multiple common fence aliases are enumerated;
    single-tag languages (``java``, ``c``, ``php``, ...) can be passed
    as a raw string since the enum would add no value.
    """

    CPP = "cpp"
    GO = "go"
    JAVASCRIPT = "javascript"
    LEAN = "lean"
    PYTHON = "python"
    RUST = "rust"
    SHELL = "shell"
    TYPESCRIPT = "typescript"

    @property
    def tags(self) -> tuple[str, ...]:
        match self:
            case Language.CPP:
                return ("cpp", "c++")
            case Language.GO:
                return ("go", "golang")
            case Language.JAVASCRIPT:
                return ("javascript", "js")
            case Language.LEAN:
                return ("lean4", "lean")
            case Language.PYTHON:
                return ("python", "py")
            case Language.RUST:
                return ("rust", "rs")
            case Language.SHELL:
                return ("shell", "bash", "sh")
            case Language.TYPESCRIPT:
                return ("typescript", "ts")


def extract_code_block(
    text: str,
    language: str | tuple[str, ...] | None = None,
    last: bool = True,
) -> str | None:
    """Return the contents of a markdown fenced code block.

    Matches ```` ```<tag>\n...``` ```` spans. When ``language`` is given, only
    blocks whose opening tag matches are considered (case-insensitive). A
    string that matches a ``Language`` member (e.g. ``"lean"``) is expanded
    to all of that language's fence aliases (e.g. ``("lean4", "lean")``);
    unknown strings are matched verbatim. Pass a tuple for a custom set.
    When ``language`` is None, any tag (including an empty tag) matches.

    Args:
        text: Markdown or LLM output to scan.
        language: Language tag(s) to filter by, or ``None`` to match any.
        last: Return the final match when True (typical for LLM outputs that
            show intermediate attempts before a final answer); else the first.

    Returns:
        Block contents without the fences, or ``None`` if no match.
    """
    if language is None:
        pattern = r"```[^\n`]*\n([\s\S]*?)```"
    else:
        if isinstance(language, str):
            try:
                tags: tuple[str, ...] = Language(language).tags
            except ValueError:
                tags = (language,)
        else:
            tags = tuple(language)
        # Sort longest-first so ``("lean4", "lean")`` matches ``lean4``
        # without first short-matching ``lean`` and leaving the ``4``.
        sorted_tags = sorted(tags, key=len, reverse=True)
        tag_re = "(?:" + "|".join(re.escape(tag) for tag in sorted_tags) + ")"
        pattern = rf"```{tag_re}\s*\n([\s\S]*?)```"
    matches = re.findall(pattern, text, re.IGNORECASE)
    if not matches:
        return None
    return matches[-1] if last else matches[0]


def has_visible_content(msg: dict) -> bool:
    """True if the message has text, thinking, or tool calls worth rendering."""
    role = msg.get("role", "")
    if role not in ("user", "assistant", "model"):
        return False
    if role == "user":
        return bool(extract_text_content(msg).strip())
    # Assistant / model
    ex = extract_assistant(msg)
    return bool(ex.text.strip() or ex.thinking or ex.tool_calls)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_anthropic_assistant(content: list) -> ExtractedAssistant:
    texts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ExtractedToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            texts.append(block.get("text", ""))
        elif block_type == "thinking":
            thinking_parts.append(block.get("thinking", ""))
        elif block_type == "tool_use":
            raw_input = block.get("input", {})
            arguments = raw_input if isinstance(raw_input, dict) else {}
            tool_calls.append(
                ExtractedToolCall(id=block.get("id", ""), name=block.get("name", ""), arguments=arguments)
            )
    return ExtractedAssistant(
        text="\n".join(texts),
        thinking="\n".join(thinking_parts),
        tool_calls=tool_calls,
    )


def _extract_gemini_assistant(parts: list) -> ExtractedAssistant:
    texts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ExtractedToolCall] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type", "")
        if part_type == "text":
            texts.append(part.get("text", ""))
        elif part_type == "thinking":
            thinking_parts.append(part.get("text", ""))
        elif part_type == "function_call":
            name = part.get("name", "")
            args = part.get("args", {})
            tool_calls.append(ExtractedToolCall(id=name, name=name, arguments=args if isinstance(args, dict) else {}))
    return ExtractedAssistant(
        text="\n".join(texts),
        thinking="\n".join(thinking_parts),
        tool_calls=tool_calls,
    )


def _extract_openai_tool_calls(tool_calls: list | None) -> list[ExtractedToolCall]:
    if not tool_calls:
        return []
    result: list[ExtractedToolCall] = []
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        result.append(ExtractedToolCall(id=tc.get("id", ""), name=fn.get("name", "unknown"), arguments=arguments))
    return result
