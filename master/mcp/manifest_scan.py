"""
master.mcp.manifest_scan
========================
CI gate (W3-1, ADR-003): scan the MCP server manifest for tool-description
injection. A malicious or compromised MCP server can smuggle instructions into
a tool's `description` or `name` — text that the model reads when deciding which
tool to call. This is the "mcp-scan" style supply-chain check, run against
Lucifer's own manifest format (`infra/mcp_servers.yaml`).

Reuses ContentPolicyValidator's injection patterns (AGENTS.md rule 4: reuse,
don't fork a parallel detector).

Run:  python -m master.mcp.manifest_scan [path]
Exit: 0 = clean, 1 = at least one injection finding.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from master.api.middleware.content_policy import ContentPolicyValidator

_DEFAULT_MANIFEST = Path(__file__).parents[2] / "infra" / "mcp_servers.yaml"


@dataclass(frozen=True)
class ManifestFinding:
    """One injection finding in a manifest field."""

    server_id: str
    location: str  # e.g. "tool 'read_pr' description"
    check: str  # which policy check fired
    reason: str


def scan_manifest(path: Path | None = None) -> list[ManifestFinding]:
    """Scan the manifest at ``path`` and return any injection findings."""
    manifest_path = path or _DEFAULT_MANIFEST
    policy = ContentPolicyValidator()
    findings: list[ManifestFinding] = []

    raw: dict[str, Any] = yaml.safe_load(manifest_path.read_text()) or {}
    servers = raw.get("servers", [])
    if isinstance(servers, dict):
        servers = [{"server_id": k, **v} for k, v in servers.items()]

    for server in servers:
        server_id = server.get("server_id", "?")

        def _check(text: str | None, location: str, sid: str = server_id) -> None:
            if not text:
                return
            result = policy.validate_input(text)
            if not result.allowed:
                findings.append(
                    ManifestFinding(
                        server_id=sid,
                        location=location,
                        check=result.check or "injection",
                        reason=result.violation or "flagged",
                    )
                )

        _check(server.get("display_name"), "display_name")
        for tool in server.get("tools", []):
            name = tool.get("name", "?")
            _check(name, f"tool '{name}' name")
            _check(tool.get("description"), f"tool '{name}' description")

    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    path = Path(args[0]) if args else None
    findings = scan_manifest(path)

    if not findings:
        print("mcp-scan: manifest clean — no tool-description injection detected.")
        return 0

    print(f"mcp-scan: {len(findings)} injection finding(s):", file=sys.stderr)
    for f in findings:
        print(f"  [{f.server_id}] {f.location}: {f.check} — {f.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
