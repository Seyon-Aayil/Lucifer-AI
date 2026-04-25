"""
integrations.gcal.server
=========================
Google Calendar MCP Server — MCP JSON-RPC 2.0 over HTTP.

Tools:
  - list_events   → upcoming events
  - create_event  → new event (HitL)
  - find_slot     → free/busy search
  - update_event  → modify event (HitL)
  - delete_event  → remove event (HitL)
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Lucifer GCal MCP Server", version="0.1.0")

_cal_service: Any = None


@app.on_event("startup")
async def startup() -> None:
    global _cal_service
    from google.oauth2.credentials import Credentials  # type: ignore[import]
    from googleapiclient.discovery import build  # type: ignore[import]

    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    _cal_service = build("calendar", "v3", credentials=creds, cache_discovery=False)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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


async def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    dispatch = {
        "list_events": _list_events,
        "create_event": _create_event,
        "find_slot": _find_slot,
        "update_event": _update_event,
        "delete_event": _delete_event,
    }
    handler = dispatch.get(tool_name)
    if not handler:
        raise ValueError(f"Unknown tool: {tool_name}")
    return await handler(args)


async def _list_events(args: dict[str, Any]) -> dict[str, Any]:
    days = int(args.get("days_ahead", 7))
    cal_id = args.get("calendar_id", "primary")
    now = datetime.now(UTC)
    time_max = (now + timedelta(days=days)).isoformat()

    def _fetch() -> list[dict[str, Any]]:
        result = _cal_service.events().list(
            calendarId=cal_id,
            timeMin=now.isoformat(),
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        events = []
        for e in result.get("items", []):
            events.append({
                "id": e["id"],
                "title": e.get("summary", ""),
                "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
                "location": e.get("location", ""),
                "attendees": [a.get("email") for a in e.get("attendees", [])],
            })
        return events

    events = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    return {"events": events, "count": len(events)}


async def _create_event(args: dict[str, Any]) -> dict[str, Any]:
    body = {
        "summary": args["title"],
        "start": {"dateTime": args["start_datetime"], "timeZone": "UTC"},
        "end": {"dateTime": args["end_datetime"], "timeZone": "UTC"},
    }
    if args.get("description"):
        body["description"] = args["description"]
    if args.get("location"):
        body["location"] = args["location"]
    if args.get("attendees"):
        body["attendees"] = [{"email": e} for e in args["attendees"]]

    def _create() -> dict[str, Any]:
        return _cal_service.events().insert(calendarId="primary", body=body).execute()

    event = await asyncio.get_event_loop().run_in_executor(None, _create)
    return {"event_id": event["id"], "html_link": event.get("htmlLink", ""), "status": "created"}


async def _find_slot(args: dict[str, Any]) -> dict[str, Any]:
    duration = int(args["duration_minutes"])
    days = int(args.get("days_ahead", 7))
    now = datetime.now(UTC)
    time_max = (now + timedelta(days=days)).isoformat()

    def _freebusy() -> dict[str, Any]:
        return _cal_service.freebusy().query(body={
            "timeMin": now.isoformat(),
            "timeMax": time_max,
            "items": [{"id": "primary"}],
        }).execute()

    fb = await asyncio.get_event_loop().run_in_executor(None, _freebusy)
    busy = fb.get("calendars", {}).get("primary", {}).get("busy", [])
    return {"duration_minutes": duration, "busy_slots": busy, "suggestion": "Check busy slots to find free time"}


async def _update_event(args: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if args.get("title"):
        patch["summary"] = args["title"]
    if args.get("start_datetime"):
        patch["start"] = {"dateTime": args["start_datetime"], "timeZone": "UTC"}
    if args.get("end_datetime"):
        patch["end"] = {"dateTime": args["end_datetime"], "timeZone": "UTC"}

    def _patch() -> dict[str, Any]:
        return _cal_service.events().patch(
            calendarId="primary", eventId=args["event_id"], body=patch
        ).execute()

    event = await asyncio.get_event_loop().run_in_executor(None, _patch)
    return {"event_id": event["id"], "status": "updated"}


async def _delete_event(args: dict[str, Any]) -> dict[str, Any]:
    def _delete() -> None:
        _cal_service.events().delete(calendarId="primary", eventId=args["event_id"]).execute()

    await asyncio.get_event_loop().run_in_executor(None, _delete)
    return {"event_id": args["event_id"], "status": "deleted"}


def _tools_list_response(req_id: int) -> dict[str, Any]:
    tools = [
        {"name": "list_events", "description": "List upcoming calendar events",
         "inputSchema": {"type": "object", "properties": {
             "days_ahead": {"type": "integer", "default": 7},
             "calendar_id": {"type": "string", "default": "primary"},
         }}},
        {"name": "create_event", "description": "Create a new calendar event",
         "requiresConfirmation": True,
         "inputSchema": {"type": "object", "required": ["title", "start_datetime", "end_datetime"],
                         "properties": {
                             "title": {"type": "string"}, "start_datetime": {"type": "string"},
                             "end_datetime": {"type": "string"}, "description": {"type": "string"},
                             "location": {"type": "string"}, "attendees": {"type": "array"},
                         }}},
        {"name": "find_slot", "description": "Find a free time slot",
         "inputSchema": {"type": "object", "required": ["duration_minutes"],
                         "properties": {"duration_minutes": {"type": "integer"},
                                        "days_ahead": {"type": "integer", "default": 7}}}},
        {"name": "update_event", "description": "Update an existing event",
         "requiresConfirmation": True,
         "inputSchema": {"type": "object", "required": ["event_id"],
                         "properties": {"event_id": {"type": "string"}, "title": {"type": "string"},
                                        "start_datetime": {"type": "string"}, "end_datetime": {"type": "string"}}}},
        {"name": "delete_event", "description": "Delete a calendar event",
         "requiresConfirmation": True,
         "inputSchema": {"type": "object", "required": ["event_id"],
                         "properties": {"event_id": {"type": "string"}}}},
    ]
    return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
