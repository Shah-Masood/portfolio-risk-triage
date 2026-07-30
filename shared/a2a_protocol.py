"""Minimal, spec-shaped A2A transport layer.

Why hand-rolled instead of the official `a2a-sdk` package:
the version available on PyPI today (1.1.2) is built around a
gRPC/protobuf core (a2a.types.a2a_pb2) rather than the lighter
pydantic types + FastAPI helpers that most A2A tutorials show.
Wiring five toy agents through the protobuf surface would add a lot
of incidental complexity without changing what this project
demonstrates architecturally.

This module implements the parts of the A2A spec that matter for the
demo, using plain FastAPI + JSON-RPC 2.0 + httpx:

- GET /.well-known/agent-card.json  -> Agent Card discovery
- POST /                            -> JSON-RPC 2.0, method "message/send"
- Task/Artifact/Part response shape modeled on the real A2A wire format

Swapping this out for the official SDK later is a contained change:
only this file and `call_agent()` would need to move to the SDK's
AgentExecutor / A2AFastAPIApplication classes.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    tags: List[str] = []


class AgentCard(BaseModel):
    name: str
    description: str
    url: str
    version: str = "1.0.0"
    protocol: str = "a2a-jsonrpc-2.0-lite"
    capabilities: Dict[str, Any] = {"streaming": False, "pushNotifications": False}
    skills: List[AgentSkill] = []


HandlerFn = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _task_envelope(artifact_name: str, artifact_data: Dict[str, Any]) -> Dict[str, Any]:
    """Wrap a dict payload into an A2A-shaped Task/Artifact/Part structure."""
    now = time.time()
    return {
        "id": str(uuid.uuid4()),
        "kind": "task",
        "status": {"state": "completed", "timestamp": now},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": artifact_name,
                "parts": [{"kind": "data", "data": artifact_data}],
            }
        ],
    }


def build_agent_app(agent_card: AgentCard, handler: HandlerFn) -> FastAPI:
    """Construct a FastAPI app implementing agent-card discovery + message/send.

    `handler` receives the `params.message` dict from the JSON-RPC
    request (already unwrapped) and returns a plain dict, which gets
    packed into a Task->Artifact->Part envelope for the response.
    """
    app = FastAPI(title=agent_card.name)

    @app.get("/.well-known/agent-card.json")
    async def get_agent_card() -> Dict[str, Any]:
        return agent_card.model_dump()

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok", "agent": agent_card.name}

    @app.post("/")
    async def jsonrpc_endpoint(request: Request) -> JSONResponse:
        body = await request.json()
        rpc_id = body.get("id")
        method = body.get("method")

        if method != "message/send":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                },
                status_code=400,
            )

        message = body.get("params", {}).get("message", {})
        try:
            result_data = await handler(message)
        except Exception as exc:  # noqa: BLE001 - surface as JSON-RPC error
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32000, "message": str(exc)},
                },
                status_code=500,
            )

        task = _task_envelope(f"{agent_card.name}.result", result_data)
        return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": task})

    return app


async def call_agent(
    base_url: str,
    payload: Dict[str, Any],
    text: str = "",
    timeout: float = 30.0,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Send an A2A `message/send` JSON-RPC request to another agent.

    `payload` is delivered as `message.metadata` (structured task
    input, e.g. {"tickers": ["AAPL", "TSLA"]}); `text` is an optional
    natural-language part for logging / future LLM use.

    Returns the unwrapped artifact `data` dict from the first artifact
    of the response Task.
    """
    request_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": text}] if text else [],
                "metadata": payload,
            }
        },
    }

    owns_client = client is None
    if owns_client:
        # trust_env=False: these are always calls between local agent
        # services on localhost, so we skip any HTTP_PROXY/ALL_PROXY env
        # vars picked up from the host environment.
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
    try:
        resp = await client.post(base_url.rstrip("/") + "/", json=request_body)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"A2A error from {base_url}: {body['error']}")
        task = body["result"]
        artifacts = task.get("artifacts", [])
        if not artifacts:
            return {}
        parts = artifacts[0].get("parts", [])
        for part in parts:
            if part.get("kind") == "data":
                return part.get("data", {})
        return {}
    finally:
        if owns_client:
            await client.aclose()


async def fetch_agent_card(base_url: str, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=10.0, trust_env=False)
    try:
        resp = await client.get(base_url.rstrip("/") + "/.well-known/agent-card.json")
        resp.raise_for_status()
        return resp.json()
    finally:
        if owns_client:
            await client.aclose()
