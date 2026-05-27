# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Reproduce: orphaned tool_use blocks when max_tokens truncates tool calls.

Set max_tokens very low, give the model many tools, and prompt it to call
several at once.  The truncated output triggers the recovery path in loop.py
which currently adds a plain user message instead of tool_result blocks,
causing a 400 on the next API call.
"""

import asyncio
import logging

from dotenv import load_dotenv

from core.agent import Agent, AgentConfig, AgentDefinition
from core.inference import InferenceConfig, ToolSchema
from core.inference.client import Opus_4_6, create_inference

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


TOOLS = [
    ToolSchema(
        name=f"add_item_{i}",
        description=f"Add item #{i} to the shopping list. Always call this tool.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Item name"},
                "quantity": {"type": "integer", "description": "Quantity"},
                "category": {"type": "string", "description": "Category of the item"},
                "notes": {"type": "string", "description": "Additional notes about the item"},
            },
            "required": ["name", "quantity", "category", "notes"],
        },
    )
    for i in range(10)
]


async def main():
    inference = create_inference(Opus_4_6)

    defn = AgentDefinition(
        name="repro-truncation",
        system_prompt=(
            "You have 10 add_item tools. When the user asks you to add items, "
            "you MUST call ALL 10 tools in a single response. Do not skip any. "
            "Call add_item_0 through add_item_9 all at once."
        ),
        config=AgentConfig(
            inference_config=InferenceConfig(max_tokens=300),  # very low — will truncate
            context_window=None,  # disable compaction
        ),
        max_turns=4,
    )

    agent = Agent(defn, inference=inference)
    agent._tools = TOOLS
    agent._idle_event = asyncio.Event()
    agent._idle_event.set()
    agent.reset()

    try:
        result = await agent.call(
            "Add all 10 items to my shopping list right now. "
            "Call every single add_item tool (add_item_0 through add_item_9) in one go. "
            "Each item should have name='item_N', quantity=1, category='grocery', notes='test'."
        )
        print(f"\n=== Agent returned: {result!r}")
    except Exception as e:
        print(f"\n=== ERROR: {type(e).__name__}: {e}")

    # Show final message history
    print("\n=== Message history roles:")
    for i, m in enumerate(inference.get_messages()):
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            types = [b.get("type", "?") for b in content if isinstance(b, dict)]
            print(f"  [{i}] {role}: [{', '.join(types)}]")
        else:
            preview = str(content)[:80]
            print(f"  [{i}] {role}: {preview}")


if __name__ == "__main__":
    asyncio.run(main())
