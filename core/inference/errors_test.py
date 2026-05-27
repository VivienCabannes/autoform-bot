# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Tests for inference error types and detection helpers."""

from core.inference import PromptTooLongError, is_prompt_too_long_message


class TestPromptTooLongError:
    def test_construction_with_defaults(self):
        err = PromptTooLongError("too long")
        assert str(err) == "too long"
        assert err.input_tokens is None
        assert err.max_tokens is None

    def test_construction_with_fields(self):
        err = PromptTooLongError("too long", input_tokens=200_000, max_tokens=128_000)
        assert err.input_tokens == 200_000
        assert err.max_tokens == 128_000

    def test_is_exception(self):
        assert issubclass(PromptTooLongError, Exception)


class TestIsPromptTooLongMessage:
    def test_openai_context_length(self):
        msg = "This model's maximum context length is 128000 tokens. However, your messages resulted in 200000 tokens."
        assert is_prompt_too_long_message(msg) is True

    def test_anthropic_prompt_too_long(self):
        msg = "prompt is too long: 300000 tokens > 200000 maximum"
        assert is_prompt_too_long_message(msg) is True

    def test_token_limit(self):
        assert is_prompt_too_long_message("token limit exceeded") is True

    def test_too_many_tokens(self):
        assert is_prompt_too_long_message("Request has too many tokens") is True

    def test_input_too_long(self):
        assert is_prompt_too_long_message("input is too long for this model") is True

    def test_request_too_large(self):
        assert is_prompt_too_long_message("request too large") is True

    def test_exceeds_context(self):
        assert is_prompt_too_long_message("input exceeds the context window") is True

    def test_unrelated_error(self):
        assert is_prompt_too_long_message("invalid API key") is False

    def test_tool_not_supported(self):
        assert is_prompt_too_long_message("model does not support tools") is False

    def test_empty_string(self):
        assert is_prompt_too_long_message("") is False

    def test_max_tokens_parameter_not_matched(self):
        """max_tokens validation errors should not be detected as prompt-too-long."""
        assert is_prompt_too_long_message("max_tokens must be an integer") is False
        assert is_prompt_too_long_message("invalid max_tokens value") is False
        assert is_prompt_too_long_message("The max_tokens parameter is required") is False
