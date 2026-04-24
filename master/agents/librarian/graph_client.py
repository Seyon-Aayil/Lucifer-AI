"""
master.agents.librarian.graph_client
======================================
Neo4j async client wrapper for the Librarian Agent.
All graph reads and writes in agent code go through this client.
Never import the neo4j driver directly outside this module.
"""
from __future__ import annotations

from typing import Any

import re

from neo4j import AsyncDriver, AsyncGraphDatabase, AsyncSession

from master.agents.librarian.access_control import NodeType, RelationType
from master.core.config import get_settings
from master.core.exceptions import NodeNotFoundError
from master.core.logging import get_logger
from master.core.telemetry import get_tracer

log = get_logger(__name__)
tracer = get_tracer(__name__)


class GraphClient:
    """
    Async Neo4j client with connection pooling.
    A single instance is shared across all Librarian operations.
    """

    def __init__(self, driver: AsyncDriver) -> None:
        self._driver = driver

    @classmethod
    def from_settings(cls) -> GraphClient:
        """Factory: create from application settings."""
        s = get_settings()
        driver = AsyncGraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_user, s.neo4j_password),
            database=s.neo4j_database,
            max_connection_pool_size=50,
        )
        return cls(driver)

    async def close(self) -> None:
        await self._driver.close()

    def _validate_identifier(self, value: str, allowed_enum: type[NodeType | RelationType]) -> None:
        """
        Validate that a Cypher identifier (label or relationship type) is safe.
        Must be alphanumeric and present in the provided allow-list enum.
        """
        if not re.match(r"^[a-zA-Z0-9_]+$", value):
            log.error("graph.security.invalid_identifier", value=value)
            raise ValueError(f"Invalid characters in identifier: {value}")

        if value not in {item.value for item in allowed_enum}:
            log.error("graph.security.unauthorized_identifier", value=value)
            raise ValueError(f"Unauthorized graph identifier: {value}")

    # ── Node CRUD ────────────────────────────────────────────────────────────

    async def upsert_node(self, node_type: str, node_id: str, attributes: dict[str, Any]) -> None:
        """
        Create or update a node of the given type.
        Uses MERGE on id property. All attribute keys are set atomically.
        """
        self._validate_identifier(node_type, NodeType)
        with tracer.start_as_current_span("neo4j.upsert_node"):
            async with self._driver.session() as session:
                await session.run(
                    f"MERGE (n:{node_type} {{id: $id}}) SET n += $attrs SET n.updatedAt = datetime()",
                    id=node_id,
                    attrs=attributes,
                )
            log.debug("graph.node.upserted", node_type=node_type, node_id=node_id)

    async def get_node(self, node_id: str) -> dict[str, Any]:
        """
        Fetch a node by its id. Raises NodeNotFoundError if missing.
        Returns node properties as a plain dict.
        """
        with tracer.start_as_current_span("neo4j.get_node"):
            async with self._driver.session() as session:
                result = await session.run(
                    "MATCH (n {id: $id}) RETURN n",
                    id=node_id,
                )
                record = await result.single()
                if record is None:
                    raise NodeNotFoundError(f"Node '{node_id}' not found in graph")
                return dict(record["n"])

    async def soft_delete_node(self, node_id: str) -> None:
        """
        Soft-delete: set deletedAt timestamp, do not DETACH DELETE.
        Preserves edge history; node is filtered from future reads by convention.
        """
        async with self._driver.session() as session:
            await session.run(
                "MATCH (n {id: $id}) SET n.deletedAt = datetime()",
                id=node_id,
            )

    # ── Edge CRUD ────────────────────────────────────────────────────────────

    async def upsert_edge(
        self,
        from_id: str,
        to_id: str,
        relation: str,
        weight: float = 1.0,
        valid_from: str | None = None,
        valid_until: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Upsert a temporal edge between two nodes."""
        self._validate_identifier(relation, RelationType)
        with tracer.start_as_current_span("neo4j.upsert_edge"):
            async with self._driver.session() as session:
                await session.run(
                    f"""
                    MATCH (a {{id: $from_id}}), (b {{id: $to_id}})
                    MERGE (a)-[r:{relation}]->(b)
                    SET r.weight = $weight,
                        r.validFrom = coalesce($valid_from, datetime()),
                        r.validUntil = $valid_until,
                        r.updatedAt = datetime()
                    SET r += $attrs
                    """,
                    from_id=from_id,
                    to_id=to_id,
                    weight=weight,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    attrs=attributes or {},
                )

    # ── Semantic Search ───────────────────────────────────────────────────────

    async def vector_search(
        self,
        embedding: list[float],
        k: int = 10,
        node_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Semantic search using the node_embedding vector index.
        Optionally filter by node type label.
        Returns top-k nodes with their score.
        """
        with tracer.start_as_current_span("neo4j.vector_search"):
            type_filter = ""
            if node_types:
                for t in node_types:
                    self._validate_identifier(t, NodeType)
                type_filter = "WHERE any(label in labels(n) WHERE label IN $node_types)"

            async with self._driver.session() as session:
                result = await session.run(
                    f"""
                    CALL db.index.vector.queryNodes('node_embedding', $k, $embedding)
                    YIELD node AS n, score
                    {type_filter}
                    WHERE n.deletedAt IS NULL
                    RETURN n, score
                    ORDER BY score DESC
                    """,
                    k=k,
                    embedding=embedding,
                    node_types=node_types,
                )
                records = await result.data()
                return [{"node": dict(r["n"]), "score": r["score"]} for r in records]

    # ── Batch Operations ─────────────────────────────────────────────────────

    async def update_decay_scores(self, scores: dict[str, float]) -> None:
        """
        Batch update decayScore for multiple nodes.
        Called nightly by the decay scheduler.
        """
        async with self._driver.session() as session:
            await session.run(
                """
                UNWIND $updates AS upd
                MATCH (n {id: upd.id})
                SET n.decayScore = upd.score
                """,
                updates=[{"id": nid, "score": s} for nid, s in scores.items()],
            )
        log.info("graph.decay_scores.updated", count=len(scores))
