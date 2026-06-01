"""User interaction — question formatting and handler dispatch.

No MCP dependencies.
"""

from __future__ import annotations

import json
from typing import Callable


class UserInteraction:
    """Manages user question/answer interactions.

    The handler is a callable that receives a question dict and returns
    the user's answer as a string. The caller must provide a handler
    appropriate for their environment (web UI, chat bridge, CLI, etc.).
    """

    def __init__(self, handler: Callable[[dict], str]) -> None:
        self.handler = handler

    def ask(
        self,
        question: str,
        options: str | list[dict] = "",
        multi_select: bool = False,
    ) -> str:
        """Ask the user a question and return their answer.

        Args:
            question: The question to ask.
            options: Option objects as a list of dicts or a JSON string encoding one.
                Each object must have a "label" key (displayed and returned as the
                answer) and may have an optional "description" key (extra context
                shown to the user).
                Example: [{"label": "Yes", "description": "Proceed"}, {"label": "No"}]
            multi_select: If True, allow selecting multiple options.

        Returns:
            The user's answer prefixed with "User answered: ".
        """
        if isinstance(options, list):
            parsed_options = options
        elif options:
            try:
                parsed_options = json.loads(options)
            except json.JSONDecodeError:
                return f"Error: Invalid JSON for options: {options}"
        else:
            parsed_options = []
        if not isinstance(parsed_options, list):
            return "Error: options must be a JSON array."
        for i, opt in enumerate(parsed_options):
            if not isinstance(opt, dict) or "label" not in opt:
                return f'Error: each option must be an object with a "label" key (option {i}).'

        question_data = {
            "question": question,
            "options": parsed_options,
            "multi_select": multi_select,
        }

        answer = self.handler(question_data)
        return f"User answered: {answer}"
