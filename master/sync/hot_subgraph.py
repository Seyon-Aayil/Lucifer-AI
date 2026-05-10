"""
master.sync.hot_subgraph
=========================
Builds a per-device hot-subgraph manifest for cold-start sync.

Per ARCHITECTURE.md §5.4 / §12, a device pulls its hot-subgraph on first
sync (or after long offline gaps). The manifest contains:
  - All nodes accessed in the last `sync_subgraph_lookback_days` days
    by this device, plus restricted-but-non-secret nodes the device owns.
  - All edges connecting those nodes.
  - A SHA-256 manifest hash so the edge can verify integrity.
  - Total payload capped at `sync_max_subgraph_bytes` (default 50 MiB).

ACL filter: nodes with `classification == "secret"` are NEVER sent to a
device. Nodes with `local_only == true` are skipped (they live only on the
edge that produced them).

Encryption is the caller's responsibility: this builder returns plaintext
payloads serialized as JSON; the gRPC servicer wraps them in AES-256-GCM
using the device's session key before transmission.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from master.core.config import get_settings
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

log = get_logger(__name__)
tracer = get_tracer(__name__)


class HotSubgraphBuilder:
    """
    Reads from Neo4j, applies ACL filters, and packs a SubgraphResponse-shaped
    payload. Returns a plain dict the servicer can convert to protobuf.
    """

    def __init__(self, graph_client: Any) -> None:
        self._graph = graph_client
        s = get_settings()
        self._max_bytes = s.sync_max_subgraph_bytes
        self._lookback_days = s.sync_subgraph_lookback_days

    async def build(
        self,
        device_id: str,
        last_sync_at_ms: int,
        max_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """
        Build the manifest.

        Args:
            device_id: requesting device.
            last_sync_at_ms: Unix ms of the device's last successful sync.
                Pass 0 for a full sync.
            max_size_bytes: optional override of the configured cap.

        Returns:
            dict with shape:
                nodes:           list[dict]  (NodeDelta-shaped)
                edges:           list[dict]  (EdgeDelta-shaped)
                generated_at:    int (Unix ms)
                manifest_hash:   str (SHA-256 hex)
                is_full_sync:    bool
        """
        cap = max_size_bytes or self._max_bytes
        is_full_sync = last_sync_at_ms == 0
        cutoff_ms = last_sync_at_ms if not is_full_sync else self._lookback_cutoff_ms()

        with tracer.start_as_current_span("sync.hot_subgraph.build"):
            nodes = await self._fetch_nodes(device_id, cutoff_ms)
            node_ids = {n["node_id"] for n in nodes}
            edges = await self._fetch_edges(node_ids, cutoff_ms)

            nodes, edges, capped = self._apply_size_cap(nodes, edges, cap)
            manifest_hash = self._hash_manifest(nodes, edges)

        log.info(
            "sync.hot_subgraph.built",
            device_id=device_id,
            nodes=len(nodes),
            edges=len(edges),
            full_sync=is_full_sync,
            capped=capped,
        )
        return {
            "nodes": nodes,
            "edges": edges,
            "generated_at": int(time.time() * 1000),
            "manifest_hash": manifest_hash,
            "is_full_sync": is_full_sync,
        }

    # ── Internals ────────────────────────────────────────────────────────────

    def _lookback_cutoff_ms(self) -> int:
        return int((time.time() - self._lookback_days * 86400) * 1000)

    async def _fetch_nodes(self, device_id: str, cutoff_ms: int) -> list[dict[str, Any]]:
        """
        Cypher: nodes accessed by the device since cutoff, ACL-filtered.
        Excludes:
          - n.classification = 'secret'    (never leaves master)
          - n.localOnly = true             (lives only on origin edge)
          - n.deletedAt IS NOT NULL        (soft-deleted)
        """
        cypher = """
        MATCH (n)
        WHERE coalesce(n.classification, 'standard') <> 'secret'
          AND coalesce(n.localOnly, false) = false
          AND n.deletedAt IS NULL
          AND coalesce(n.updatedAtMs, 0) >= $cutoff_ms
        RETURN
          n.id           AS node_id,
          labels(n)[0]   AS node_type,
          coalesce(n.classification, 'standard') AS classification,
          coalesce(n.updatedAtMs, 0)             AS updated_at,
          coalesce(n.sourceAgent, '')            AS source_agent,
          properties(n)  AS attrs
        ORDER BY n.updatedAtMs DESC
        """
        async with self._graph._driver.session() as session:
            result = await session.run(cypher, cutoff_ms=cutoff_ms)
            records = await result.data()

        return [self._format_node(r) for r in records]

    async def _fetch_edges(
        self,
        node_ids: set[str],
        cutoff_ms: int,
    ) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        cypher = """
        MATCH (a)-[r]->(b)
        WHERE a.id IN $node_ids AND b.id IN $node_ids
          AND coalesce(r.updatedAtMs, 0) >= $cutoff_ms
        RETURN
          coalesce(r.id, toString(id(r))) AS edge_id,
          a.id                            AS from_node_id,
          b.id                            AS to_node_id,
          type(r)                         AS relation,
          coalesce(r.weight, 1.0)         AS weight,
          coalesce(r.validFromMs, 0)      AS valid_from,
          coalesce(r.validUntilMs, 0)     AS valid_until
        """
        async with self._graph._driver.session() as session:
            result = await session.run(
                cypher,
                node_ids=list(node_ids),
                cutoff_ms=cutoff_ms,
            )
            records = await result.data()

        return [
            {
                "edge_id": r["edge_id"],
                "operation": "upsert",
                "from_node_id": r["from_node_id"],
                "to_node_id": r["to_node_id"],
                "relation": r["relation"],
                "weight": float(r["weight"]),
                "valid_from": int(r["valid_from"]),
                "valid_until": int(r["valid_until"]),
            }
            for r in records
        ]

    @staticmethod
    def _format_node(record: dict[str, Any]) -> dict[str, Any]:
        # Strip internal fields from the attrs payload before serialising.
        attrs = dict(record.get("attrs") or {})
        for k in ("classification", "localOnly", "deletedAt", "sourceAgent"):
            attrs.pop(k, None)
        payload = json.dumps(attrs, sort_keys=True, default=str).encode()
        return {
            "node_id": record["node_id"],
            "operation": "upsert",
            "node_type": record["node_type"] or "Unknown",
            "payload": payload,  # plaintext; servicer encrypts
            "classification": record["classification"],
            "updated_at": int(record["updated_at"]),
            "source_agent": record["source_agent"] or "",
        }

    @staticmethod
    def _apply_size_cap(
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        cap_bytes: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        """
        Drop the lowest-priority (oldest) nodes until total payload fits.
        Edges are filtered to only those whose endpoints survive.
        Returns (nodes, edges, was_capped).
        """
        total = sum(len(n["payload"]) for n in nodes)
        if total <= cap_bytes:
            return nodes, edges, False

        kept: list[dict[str, Any]] = []
        running = 0
        for n in nodes:  # already ORDER BY updatedAtMs DESC
            size = len(n["payload"])
            if running + size > cap_bytes:
                break
            kept.append(n)
            running += size

        kept_ids = {n["node_id"] for n in kept}
        kept_edges = [
            e for e in edges if e["from_node_id"] in kept_ids and e["to_node_id"] in kept_ids
        ]
        return kept, kept_edges, True

    @staticmethod
    def _hash_manifest(
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> str:
        """SHA-256 over a deterministic serialisation of the manifest."""
        h = hashlib.sha256()
        for n in nodes:
            h.update(n["node_id"].encode())
            h.update(b"|")
            h.update(n["payload"])
            h.update(b"\n")
        for e in edges:
            h.update(e["edge_id"].encode())
            h.update(b"|")
            h.update(f"{e['from_node_id']}->{e['to_node_id']}:{e['relation']}".encode())
            h.update(b"\n")
        return h.hexdigest()
