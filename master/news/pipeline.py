"""
master.news.pipeline
======================
News sync pipeline: fetch → score → deduplicate → write to Neo4j.

Pipeline stages:
  1. Fetch    — pull RawArticles from configured NewsSource list
  2. Score    — rank by recency + keyword relevance (0.0–1.0)
  3. Dedupe   — drop URL-hash duplicates already present in Neo4j
  4. Write    — upsert News nodes into Neo4j via GraphClient

The pipeline is idempotent: re-running for the same URLs is a no-op
because GraphClient.upsert_node() uses MERGE on node id.
"""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from master.agents.librarian.access_control import NodeType
from master.core.logging import get_logger
from master.core.telemetry import get_tracer
from master.news.fetcher import NewsFetcher, NewsSource, RawArticle

if TYPE_CHECKING:
    from master.agents.librarian.graph_client import GraphClient

log = get_logger(__name__)
tracer = get_tracer(__name__)

# Recency half-life for scoring: articles older than this score ≈ 0.5 on the time axis
_RECENCY_HALF_LIFE_HOURS = 24.0
# Minimum score to persist an article
_MIN_SCORE = 0.2


class NewsPipeline:
    """
    Orchestrates the full fetch-score-dedupe-write news sync cycle.
    One instance is reused across scheduled runs.
    """

    def __init__(
        self,
        graph_client: GraphClient,
        sources: list[NewsSource],
    ) -> None:
        self._gc = graph_client
        self._sources = sources
        self._fetcher = NewsFetcher()

    async def run(self) -> dict[str, int]:
        """
        Execute one full sync cycle.
        Returns stats: {fetched, scored, deduped, written, errors}.
        """
        with tracer.start_as_current_span("news_pipeline.run"):
            stats: dict[str, int] = {
                "fetched": 0, "scored": 0, "deduped": 0, "written": 0, "errors": 0,
            }
            now = datetime.now(UTC)

            # 1. Fetch all sources concurrently
            fetch_results = await asyncio.gather(
                *[self._fetcher.fetch_source(src) for src in self._sources],
                return_exceptions=True,
            )
            raw_articles: list[RawArticle] = []
            for result in fetch_results:
                if isinstance(result, Exception):
                    log.warning("news_pipeline.fetch_error", error=str(result))
                    stats["errors"] += 1
                else:
                    raw_articles.extend(result)

            stats["fetched"] = len(raw_articles)
            if not raw_articles:
                log.info("news_pipeline.no_articles")
                return stats

            # 2. Score
            scored = _score_articles(raw_articles, now)
            scored = [a for a in scored if a[1] >= _MIN_SCORE]
            stats["scored"] = len(scored)

            # 3. Deduplicate against existing Neo4j nodes
            known_hashes = await self._fetch_known_hashes()
            fresh = [(art, score) for art, score in scored if art.url_hash not in known_hashes]
            stats["deduped"] = stats["scored"] - len(fresh)

            # 4. Write to Neo4j
            for article, score in fresh:
                try:
                    await self._write_article(article, score)
                    stats["written"] += 1
                except Exception as exc:
                    log.warning("news_pipeline.write_error", url=article.url, error=str(exc))
                    stats["errors"] += 1

            log.info("news_pipeline.run.complete", **stats)
            return stats

    async def _fetch_known_hashes(self) -> set[str]:
        """Return url_hash values for News nodes already in Neo4j."""
        try:
            async with self._gc._driver.session() as session:
                result = await session.run(
                    "MATCH (n:News) WHERE n.deletedAt IS NULL RETURN n.url_hash AS h"
                )
                records = await result.data()
                return {r["h"] for r in records if r.get("h")}
        except Exception as exc:
            log.warning("news_pipeline.hash_fetch_failed", error=str(exc))
            return set()

    async def _write_article(self, article: RawArticle, score: float) -> None:
        """Upsert a single article as a News node in Neo4j."""
        attributes: dict[str, Any] = {
            "title": article.title,
            "url": article.url,
            "url_hash": article.url_hash,
            "body": article.body[:2000],  # cap stored body
            "source_id": article.source_id,
            "author": article.author or "",
            "tags": ",".join(article.tags),
            "relevance_score": round(score, 4),
            "published_at": (
                article.published_at.isoformat() if article.published_at else ""
            ),
            "decayScore": score,
        }
        await self._gc.upsert_node(NodeType.NEWS.value, article.url_hash, attributes)

    async def close(self) -> None:
        await self._fetcher.close()


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_articles(
    articles: list[RawArticle], now: datetime
) -> list[tuple[RawArticle, float]]:
    """
    Score each article 0.0–1.0 combining recency and a content length signal.
    recency:  exponential decay based on hours since publication
    content:  log-normalised body length (longer → slightly higher; capped)
    """
    scored: list[tuple[RawArticle, float]] = []
    for article in articles:
        recency = _recency_score(article.published_at, now)
        content = _content_score(article.body)
        score = 0.7 * recency + 0.3 * content
        scored.append((article, round(score, 4)))

    # Sort descending so callers can slice top-N easily
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _recency_score(published_at: datetime | None, now: datetime) -> float:
    if published_at is None:
        return 0.3  # unknown age gets a neutral score
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    hours_ago = max(0.0, (now - published_at).total_seconds() / 3600.0)
    # exponential decay with configurable half-life
    return math.exp(-math.log(2) * hours_ago / _RECENCY_HALF_LIFE_HOURS)


def _content_score(body: str) -> float:
    length = len(body.strip())
    if length == 0:
        return 0.0
    # log-normalise: 500 chars → ~0.5, 5000 chars → ~1.0
    return min(1.0, math.log10(max(1, length)) / 4.0)
