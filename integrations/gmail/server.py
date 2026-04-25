"""
integrations.gmail.server
==========================
Gmail MCP Server — FastAPI implementation of the MCP JSON-RPC 2.0 protocol.

Exposes tools:
  - read_emails    → list recent messages
  - search_emails  → search inbox by query
  - compose_draft  → create a draft (HitL required before send)
  - send_email     → send an existing draft
  - apply_label    → tag a message

Auth: Google OAuth2 via server-side refresh token (GOOGLE_REFRESH_TOKEN env var).
All tool calls are scoped to the authenticated user's mailbox only.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Lucifer Gmail MCP Server", version="0.1.0")

# ── Google API client (initialised at startup) ─────────────────────────────────

_gmail_service: Any = None


@app.on_event("startup")
async def startup() -> None:
    global _gmail_service
    from google.oauth2.credentials import Credentials  # type: ignore[import]
    from googleapiclient.discovery import build  # type: ignore[import]

    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    _gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── MCP JSON-RPC Endpoint ─────────────────────────────────────────────────────

@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    method: str = body.get("method", "")
    params: dict[str, Any] = body.get("params", {})
    req_id = body.get("id", 1)

    if method == "tools/list":
        return JSONResponse(_tools_list_response(req_id))

    if method == "tools/call":
        tool_name: str = params.get("name", "")
        args: dict[str, Any] = params.get("arguments", {})
        try:
            result = await _dispatch(tool_name, args)
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            return JSONResponse({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32603, "message": str(exc)},
            })

    raise HTTPException(status_code=400, detail=f"Unknown method: {method}")


# ── Tool Dispatch ─────────────────────────────────────────────────────────────

async def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "read_emails":
        return await _read_emails(args)
    if tool_name == "search_emails":
        return await _search_emails(args)
    if tool_name == "compose_draft":
        return await _compose_draft(args)
    if tool_name == "send_email":
        return await _send_email(args)
    if tool_name == "apply_label":
        return await _apply_label(args)
    raise ValueError(f"Unknown tool: {tool_name}")


async def _read_emails(args: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    import base64

    max_results = int(args.get("max_results", 10))
    query = args.get("query", "")
    label = args.get("label", "INBOX")

    def _fetch() -> list[dict[str, Any]]:
        results = _gmail_service.users().messages().list(
            userId="me", maxResults=max_results,
            q=query, labelIds=[label] if label else None,
        ).execute()
        messages = results.get("messages", [])
        emails = []
        for msg in messages[:max_results]:
            detail = _gmail_service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
            emails.append({
                "id": msg["id"],
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
            })
        return emails

    emails = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    return {"emails": emails, "count": len(emails)}


async def _search_emails(args: dict[str, Any]) -> dict[str, Any]:
    return await _read_emails({**args, "label": ""})


async def _compose_draft(args: dict[str, Any]) -> dict[str, Any]:
    import asyncio
    import base64
    from email.mime.text import MIMEText

    to_addrs = ", ".join(args["to"])
    msg = MIMEText(args["body"])
    msg["to"] = to_addrs
    msg["subject"] = args["subject"]
    if args.get("cc"):
        msg["cc"] = ", ".join(args["cc"])

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    def _create() -> dict[str, Any]:
        return _gmail_service.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()

    draft = await asyncio.get_event_loop().run_in_executor(None, _create)
    return {"draft_id": draft["id"], "status": "draft_created"}


async def _send_email(args: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    def _send() -> dict[str, Any]:
        return _gmail_service.users().drafts().send(
            userId="me", body={"id": args["draft_id"]}
        ).execute()

    result = await asyncio.get_event_loop().run_in_executor(None, _send)
    return {"message_id": result.get("id"), "status": "sent"}


async def _apply_label(args: dict[str, Any]) -> dict[str, Any]:
    import asyncio

    def _modify() -> dict[str, Any]:
        return _gmail_service.users().messages().modify(
            userId="me", id=args["message_id"],
            body={"addLabelIds": [args["label"]]},
        ).execute()

    await asyncio.get_event_loop().run_in_executor(None, _modify)
    return {"status": "label_applied", "message_id": args["message_id"]}


# ── Tools Manifest ────────────────────────────────────────────────────────────

def _tools_list_response(req_id: int) -> dict[str, Any]:
    tools = [
        {
            "name": "read_emails",
            "description": "Read recent emails from inbox",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "default": 10},
                    "query": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
        },
        {
            "name": "search_emails",
            "description": "Search emails by query string",
            "inputSchema": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 20},
                },
            },
        },
        {
            "name": "compose_draft",
            "description": "Compose an email draft (does not send)",
            "requiresConfirmation": True,
            "inputSchema": {
                "type": "object",
                "required": ["to", "subject", "body"],
                "properties": {
                    "to": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        {
            "name": "send_email",
            "description": "Send a composed draft (requires HitL approval)",
            "requiresConfirmation": True,
            "inputSchema": {
                "type": "object",
                "required": ["draft_id"],
                "properties": {"draft_id": {"type": "string"}},
            },
        },
        {
            "name": "apply_label",
            "description": "Apply a label to an email",
            "inputSchema": {
                "type": "object",
                "required": ["message_id", "label"],
                "properties": {
                    "message_id": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
        },
    ]
    return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
