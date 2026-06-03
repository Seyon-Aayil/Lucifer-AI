"""
integrations.slack.server
==========================
Slack MCP Server — MCP JSON-RPC 2.0 over HTTP.

Tools:
  - read_channel, send_message, search_messages

Mirrors the structure of the gmail / gcal / notion / github MCP servers:
a single ``/mcp`` JSON-RPC endpoint plus a ``/health`` probe. The bot token
is read from the ``SLACK_BOT_TOKEN`` environment variable at startup; the
container is launched (and sandboxed) by the master MCPServerRegistry.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Lucifer Slack MCP Server", version="0.1.0")

_SLACK_API = "https://slack.com/api"
_slack_token: str = ""


@app.on_event("startup")
async def startup() -> None:
    global _slack_token
    _slack_token = os.environ["SLACK_BOT_TOKEN"]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_slack_token}",
        "Content-Type": "application/json; charset=utf-8",
    }


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    method: str = body.get("method", "")
    params: dict[str, Any] = body.get("params", {})
    req_id = body.get("id", 1)

    if method == "tools/list":
        tools = [
            {
                "name": "read_channel",
                "description": "Read recent messages from a Slack channel",
                "inputSchema": {
                    "type": "object",
                    "required": ["channel"],
                    "properties": {
                        "channel": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                },
            },
            {
                "name": "send_message",
                "description": "Send a message to a Slack channel or DM",
                "requiresConfirmation": True,
                "inputSchema": {
                    "type": "object",
                    "required": ["channel", "text"],
                    "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                },
            },
            {
                "name": "search_messages",
                "description": "Search Slack messages by query",
                "inputSchema": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 20},
                    },
                },
            },
        ]
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})

    if method == "tools/call":
        tool_name: str = params.get("name", "")
        args: dict[str, Any] = params.get("arguments", {})
        try:
            result = await _dispatch(tool_name, args)
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}
            )

    raise HTTPException(status_code=400, detail=f"Unknown method: {method}")


def _check_ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Slack returns 200 with {ok: false, error: ...} on failure — surface it."""
    if not payload.get("ok", False):
        raise ValueError(f"slack_api_error: {payload.get('error', 'unknown')}")
    return payload


async def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    import httpx

    if tool_name == "read_channel":
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{_SLACK_API}/conversations.history",
                headers=_headers(),
                params={"channel": args["channel"], "limit": int(args.get("limit", 20))},
            )
            r.raise_for_status()
            data = _check_ok(r.json())
        messages = [
            {"user": m.get("user", ""), "text": m.get("text", ""), "ts": m.get("ts", "")}
            for m in data.get("messages", [])
        ]
        return {"messages": messages, "count": len(messages)}

    if tool_name == "send_message":
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{_SLACK_API}/chat.postMessage",
                headers=_headers(),
                json={"channel": args["channel"], "text": args["text"]},
            )
            r.raise_for_status()
            data = _check_ok(r.json())
        return {"channel": data.get("channel", ""), "ts": data.get("ts", ""), "status": "sent"}

    if tool_name == "search_messages":
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{_SLACK_API}/search.messages",
                headers=_headers(),
                params={"query": args["query"], "count": int(args.get("limit", 20))},
            )
            r.raise_for_status()
            data = _check_ok(r.json())
        matches = data.get("messages", {}).get("matches", [])
        results = [
            {
                "user": m.get("username", ""),
                "text": m.get("text", ""),
                "channel": m.get("channel", {}).get("name", ""),
                "permalink": m.get("permalink", ""),
            }
            for m in matches
        ]
        return {"results": results, "count": len(results)}

    raise ValueError(f"Unknown tool: {tool_name}")
