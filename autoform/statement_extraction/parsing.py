# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""YAML response parsing — robust extraction from LLM output.

Handles three response formats:
1. Clean YAML (the intended format)
2. YAML inside markdown code fences
3. Prose with embedded YAML fragments (``- name: ...`` lines)
"""

from __future__ import annotations

import logging
import re

import yaml

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)
_YAML_ITEM_RE = re.compile(r"^- \w+:", re.MULTILINE)


def parse_yaml_response(response: str) -> list[dict] | None:
    """Parse a YAML list from an LLM response.

    Tries in order:
    1. Extract from markdown code fences
    2. Parse the whole response as YAML
    3. Extract consecutive YAML-looking lines (``- key: value``) from prose

    Returns a list of dicts on success, or ``None`` if nothing parseable
    was found.  An empty ``[]`` response returns ``[]``.
    """
    stripped = response.strip()
    if not stripped or stripped == "[]":
        return []

    # 1. Try code fences first
    m = _FENCE_RE.search(stripped)
    if m:
        result = _try_parse(m.group(1).strip())
        if result is not None:
            return result

    # 2. Try the whole response as YAML
    result = _try_parse(stripped)
    if result is not None:
        return result

    # 3. Extract YAML fragments from prose
    return _extract_yaml_fragments(stripped)


def _try_parse(text: str) -> list[dict] | None:
    """Attempt to yaml.safe_load *text* as a list of dicts."""
    if not text or text == "[]":
        return []
    # Try as-is first, then with escaped backslashes (LaTeX in double-quoted
    # YAML strings causes parse failures because \l, \o, \m etc. are not valid
    # YAML escape sequences).
    for candidate in (text, _escape_backslashes(text)):
        try:
            parsed = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, list) and all(isinstance(e, dict) for e in parsed):
            return parsed
    return None


def _escape_backslashes(text: str) -> str:
    """Double all backslashes inside double-quoted YAML strings.

    LLM output often contains LaTeX like ``\\mathbb`` inside
    double-quoted YAML values.  YAML treats ``\\m`` as an invalid escape.
    This function doubles all backslashes inside double-quoted regions
    so YAML reads them as literal backslashes.
    """
    out: list[str] = []
    in_double_quote = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"' and (i == 0 or text[i - 1] != "\\"):
            in_double_quote = not in_double_quote
            out.append(ch)
        elif ch == "\\" and in_double_quote:
            out.append("\\\\")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _extract_yaml_fragments(text: str) -> list[dict] | None:
    """Pull YAML list items out of a prose response.

    Finds runs of consecutive lines that start with ``- key:`` or
    continuation indented lines (``  key:``), joins them, and tries
    to parse each block.
    """
    if not _YAML_ITEM_RE.search(text):
        return None

    blocks: list[str] = []
    current: list[str] = []

    for line in text.splitlines():
        # A new YAML list item
        if re.match(r"^- \w+:", line):
            current.append(line)
        # Continuation of a YAML item (indented key or value)
        elif current and re.match(r"^  \w+:", line):
            current.append(line)
        else:
            if current:
                blocks.append("\n".join(current))
                current = []

    if current:
        blocks.append("\n".join(current))

    if not blocks:
        return None

    combined = "\n".join(blocks)
    result = _try_parse(combined)
    if result is not None:
        logger.info("Extracted %d YAML entries from prose response", len(result))
        return result

    # Try each block individually
    entries: list[dict] = []
    for block in blocks:
        parsed = _try_parse(block)
        if parsed:
            entries.extend(parsed)

    if entries:
        logger.info("Extracted %d YAML entries from prose response (per-block)", len(entries))
        return entries

    return None
