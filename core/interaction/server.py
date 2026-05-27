# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Registry API server — exposes the agent registry over HTTP.

Started by the pipeline as a background thread so that external
processes (e.g. the visualizer) can send messages to running agents
and read their conversation history.
"""

from __future__ import annotations

import logging
import socket
import threading

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .registry import get_registry

logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Registry API")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)


class MessageBody(BaseModel):
    message: str


@app.post("/agent/{agent_id}/message")
async def send_message(agent_id: str, body: MessageBody):
    """Send an interactive message to an agent.

    Returns immediately. The agent handles it internally — calling
    directly if idle, or injecting mid-loop if busy.
    """
    sent = get_registry().send(agent_id, body.message)
    if not sent:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    return {"status": "sent", "agent_id": agent_id}


@app.get("/agent/{agent_id}/messages")
async def get_messages(agent_id: str):
    """Return the agent's conversation history."""
    messages = get_registry().get_messages(agent_id)
    if messages is None:
        raise HTTPException(404, f"Agent not found: {agent_id}")
    pending = get_registry().get_pending_messages(agent_id) or []
    return {"agent_id": agent_id, "messages": messages, "pending": pending}


@app.get("/agents/active")
async def list_active():
    """List agent IDs with their current status and metadata."""
    registry = get_registry()
    agents = []
    for agent_id in registry.active_agents():
        agent = registry._agents.get(agent_id)
        if agent is None:
            continue
        if not hasattr(agent, "_idle_event"):
            status = "pending"
        elif agent.is_busy():
            status = "running"
        else:
            status = "idle"
        agents.append(
            {
                "id": agent_id,
                "status": status,
                "turns": getattr(agent, "total_turns", 0),
                "pending": len(getattr(agent, "_pending_messages", [])),
            }
        )
    return {"agents": agents}


def start_registry_server(port: int | None = None) -> int:
    """Start the registry API in a background thread.

    Args:
        port: Fixed port to bind to. If None, an available port is chosen.

    Returns:
        The port the server is listening on.
    """
    import uvicorn

    if port is None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            port = s.getsockname()[1]

    def serve():
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    logger.info("Registry API running at http://localhost:%d", port)
    return port
