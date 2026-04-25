"""
integrations.github.server
===========================
GitHub MCP Server — MCP JSON-RPC 2.0 over HTTP.

Tools:
  - list_prs, read_pr, create_issue, get_repo_summary, list_workflows
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Lucifer GitHub MCP Server", version="0.1.0")

_GH_API = "https://api.github.com"
_github_token: str = ""


@app.on_event("startup")
async def startup() -> None:
    global _github_token
    _github_token = os.environ["GITHUB_TOKEN"]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    method: str = body.get("method", "")
    params: dict[str, Any] = body.get("params", {})
    req_id = body.get("id", 1)

    if method == "tools/list":
        tools = [
            {"name": "list_prs", "description": "List open PRs",
             "inputSchema": {"type": "object", "required": ["repo"],
                             "properties": {"repo": {"type": "string"},
                                            "state": {"type": "string", "default": "open"},
                                            "limit": {"type": "integer", "default": 20}}}},
            {"name": "read_pr", "description": "Read a pull request diff and comments",
             "inputSchema": {"type": "object", "required": ["repo", "pr_number"],
                             "properties": {"repo": {"type": "string"}, "pr_number": {"type": "integer"}}}},
            {"name": "create_issue", "description": "Create a GitHub issue",
             "requiresConfirmation": True,
             "inputSchema": {"type": "object", "required": ["repo", "title", "body"],
                             "properties": {"repo": {"type": "string"}, "title": {"type": "string"},
                                            "body": {"type": "string"}, "labels": {"type": "array"}}}},
            {"name": "get_repo_summary", "description": "Get repository summary",
             "inputSchema": {"type": "object", "required": ["repo"],
                             "properties": {"repo": {"type": "string"}}}},
            {"name": "list_workflows", "description": "List GitHub Actions workflow runs",
             "inputSchema": {"type": "object", "required": ["repo"],
                             "properties": {"repo": {"type": "string"}, "limit": {"type": "integer"}}}},
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
    repo = args.get("repo", "")

    if tool_name == "list_prs":
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{_GH_API}/repos/{repo}/pulls", headers=_headers(),
                            params={"state": args.get("state", "open"),
                                    "per_page": int(args.get("limit", 20))})
            r.raise_for_status()
        prs = [{"number": p["number"], "title": p["title"],
                "author": p["user"]["login"], "url": p["html_url"]}
               for p in r.json()]
        return {"prs": prs, "count": len(prs)}

    if tool_name == "read_pr":
        pr_num = args["pr_number"]
        async with httpx.AsyncClient() as c:
            pr_r = await c.get(f"{_GH_API}/repos/{repo}/pulls/{pr_num}", headers=_headers())
            pr_r.raise_for_status()
            diff_r = await c.get(f"{_GH_API}/repos/{repo}/pulls/{pr_num}/files", headers=_headers())
            diff_r.raise_for_status()
        pr = pr_r.json()
        files = [{"filename": f["filename"], "additions": f["additions"],
                  "deletions": f["deletions"], "patch": f.get("patch", "")[:500]}
                 for f in diff_r.json()]
        return {"title": pr["title"], "body": pr.get("body", ""),
                "state": pr["state"], "files": files}

    if tool_name == "create_issue":
        payload: dict[str, Any] = {"title": args["title"], "body": args["body"]}
        if args.get("labels"):
            payload["labels"] = args["labels"]
        async with httpx.AsyncClient() as c:
            r = await c.post(f"{_GH_API}/repos/{repo}/issues",
                             headers=_headers(), json=payload)
            r.raise_for_status()
            issue = r.json()
        return {"issue_number": issue["number"], "url": issue["html_url"], "status": "created"}

    if tool_name == "get_repo_summary":
        async with httpx.AsyncClient() as c:
            repo_r = await c.get(f"{_GH_API}/repos/{repo}", headers=_headers())
            repo_r.raise_for_status()
        data = repo_r.json()
        return {"name": data["full_name"], "description": data.get("description", ""),
                "stars": data["stargazers_count"], "language": data.get("language", ""),
                "default_branch": data["default_branch"]}

    if tool_name == "list_workflows":
        async with httpx.AsyncClient() as c:
            r = await c.get(f"{_GH_API}/repos/{repo}/actions/runs", headers=_headers(),
                            params={"per_page": int(args.get("limit", 10))})
            r.raise_for_status()
        runs = [{"id": w["id"], "name": w["name"], "status": w["status"],
                 "conclusion": w.get("conclusion"), "created_at": w["created_at"]}
                for w in r.json().get("workflow_runs", [])]
        return {"runs": runs, "count": len(runs)}

    raise ValueError(f"Unknown tool: {tool_name}")
