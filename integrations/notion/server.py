"""
integrations.notion.server
===========================
Notion MCP Server — MCP JSON-RPC 2.0 over HTTP.

Tools:
  - search_pages, read_page, create_page, update_block, query_database
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Lucifer Notion MCP Server", version="0.1.0")

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_notion_token: str = ""


@app.on_event("startup")
async def startup() -> None:
    global _notion_token
    _notion_token = os.environ["NOTION_API_KEY"]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_notion_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    method: str = body.get("method", "")
    params: dict[str, Any] = body.get("params", {})
    req_id = body.get("id", 1)

    if method == "tools/list":
        tools = [
            {"name": "search_pages", "description": "Search Notion pages",
             "inputSchema": {"type": "object", "required": ["query"],
                             "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}},
            {"name": "read_page", "description": "Read a Notion page as markdown",
             "inputSchema": {"type": "object", "required": ["page_id"],
                             "properties": {"page_id": {"type": "string"}}}},
            {"name": "create_page", "description": "Create a Notion page",
             "inputSchema": {"type": "object", "required": ["parent_id", "title", "content"],
                             "properties": {"parent_id": {"type": "string"}, "title": {"type": "string"},
                                            "content": {"type": "string"}}}},
            {"name": "update_block", "description": "Update a Notion block",
             "inputSchema": {"type": "object", "required": ["block_id", "content"],
                             "properties": {"block_id": {"type": "string"}, "content": {"type": "string"}}}},
            {"name": "query_database", "description": "Query a Notion database",
             "inputSchema": {"type": "object", "required": ["database_id"],
                             "properties": {"database_id": {"type": "string"},
                                            "filter": {"type": "object"}, "limit": {"type": "integer"}}}},
        ]
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})

    if method == "tools/call":
        tool_name: str = params.get("name", "")
        args: dict[str, Any] = params.get("arguments", {})
        try:
            result = await _dispatch(tool_name, args)
            return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            return JSONResponse({"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": -32603, "message": str(exc)}})

    raise HTTPException(status_code=400, detail=f"Unknown method: {method}")


async def _dispatch(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    import httpx
    if tool_name == "search_pages":
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{_NOTION_API}/search", headers=_headers(),
                             json={"query": args["query"], "page_size": int(args.get("limit", 10))})
            r.raise_for_status()
            results = r.json().get("results", [])
        pages = [{"id": x["id"], "url": x.get("url", "")} for x in results]
        return {"pages": pages, "count": len(pages)}

    if tool_name == "read_page":
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{_NOTION_API}/blocks/{args['page_id']}/children",
                            headers=_headers(), params={"page_size": 100})
            r.raise_for_status()
            blocks = r.json().get("results", [])
        lines = []
        for b in blocks:
            bt = b.get("type", "")
            rich = b.get(bt, {}).get("rich_text", [])
            lines.append("".join(t.get("plain_text", "") for t in rich))
        return {"page_id": args["page_id"], "content": "\n".join(lines)}

    if tool_name == "create_page":
        body = {
            "parent": {"database_id": args["parent_id"]},
            "properties": {"Name": {"title": [{"text": {"content": args["title"]}}]}},
            "children": [{"object": "block", "type": "paragraph",
                          "paragraph": {"rich_text": [{"text": {"content": args["content"]}}]}}],
        }
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{_NOTION_API}/pages", headers=_headers(), json=body)
            r.raise_for_status()
            page = r.json()
        return {"page_id": page["id"], "status": "created"}

    if tool_name == "update_block":
        async with httpx.AsyncClient() as c:
            r = await c.patch(f"{_NOTION_API}/blocks/{args['block_id']}", headers=_headers(),
                              json={"paragraph": {"rich_text": [{"text": {"content": args["content"]}}]}})
            r.raise_for_status()
        return {"block_id": args["block_id"], "status": "updated"}

    if tool_name == "query_database":
        payload: dict[str, Any] = {"page_size": int(args.get("limit", 20))}
        if args.get("filter"):
            payload["filter"] = args["filter"]
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{_NOTION_API}/databases/{args['database_id']}/query",
                             headers=_headers(), json=payload)
            r.raise_for_status()
            data = r.json()
        return {"results": data.get("results", []), "count": len(data.get("results", []))}

    raise ValueError(f"Unknown tool: {tool_name}")
