"""
master.news.scheduler
=======================
APScheduler-based news sync scheduler.

Wires together NewsPipeline + a default set of NewsSource configs and
runs the pipeline on a configurable cadence (default: every 60 minutes).

Usage in FastAPI lifespan:
    from master.news.scheduler import NewsScheduler
    news_scheduler = await NewsScheduler.create(graph_client)
    news_scheduler.attach(scheduler)       # existing AsyncIOScheduler
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from master.core.config import get_settings
from master.core.logging import get_logger
from master.news.fetcher import NewsSource
from master.news.pipeline import NewsPipeline

if TYPE_CHECKING:
    from master.agents.librarian.graph_client import GraphClient

log = get_logger(__name__)

# ── Default source catalogue ──────────────────────────────────────────────────
# Extend via infra/news_sources.yaml in a future phase.

_DEFAULT_SOURCES: list[NewsSource] = [
    # Tech news
    NewsSource("hn-top", "rss", "https://hnrss.org/frontpage", cadence_minutes=60),
    NewsSource("hn-ask", "rss", "https://hnrss.org/ask", cadence_minutes=120),
    # AI / ML
    NewsSource("arxiv-cs-ai", "arxiv", "https://rss.arxiv.org/rss/cs.AI", cadence_minutes=240),
    NewsSource("arxiv-cs-lg", "arxiv", "https://rss.arxiv.org/rss/cs.LG", cadence_minutes=240),
    # Open source
    NewsSource(
        "github-trending",
        "rss",
        "https://github.com/trending?since=daily.atom",
        cadence_minutes=360,
    ),
    # Developer
    NewsSource(
        "r-programming", "reddit", "https://www.reddit.com/r/programming/", cadence_minutes=120
    ),
    NewsSource(
        "r-machinelearning",
        "reddit",
        "https://www.reddit.com/r/MachineLearning/",
        cadence_minutes=120,
    ),
]


class NewsScheduler:
    """
    Wraps NewsPipeline and attaches it to an AsyncIOScheduler.
    Exposes run_now() for on-demand execution (useful for testing).
    """

    def __init__(self, pipeline: NewsPipeline, cadence_minutes: int) -> None:
        self._pipeline = pipeline
        self._cadence = cadence_minutes

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        graph_client: GraphClient,
        sources: list[NewsSource] | None = None,
    ) -> NewsScheduler:
        """Create a NewsScheduler with the given (or default) source list."""
        settings = get_settings()
        cadence = settings.news_default_fetch_cadence_minutes
        effective_sources = sources if sources is not None else _DEFAULT_SOURCES
        pipeline = NewsPipeline(graph_client=graph_client, sources=effective_sources)
        log.info("news_scheduler.created", sources=len(effective_sources), cadence_min=cadence)
        return cls(pipeline=pipeline, cadence_minutes=cadence)

    # ── Scheduling ────────────────────────────────────────────────────────────

    def attach(self, scheduler: Any) -> None:
        """Register the pipeline as a recurring job on an AsyncIOScheduler."""
        scheduler.add_job(
            self.run_now,
            trigger="interval",
            minutes=self._cadence,
            id="news_sync",
            replace_existing=True,
            max_instances=1,  # never overlap runs
        )
        log.info("news_scheduler.attached", cadence_minutes=self._cadence)

    async def run_now(self) -> dict[str, int]:
        """Trigger one pipeline run immediately. Returns stats dict."""
        log.info("news_scheduler.run_now")
        return await self._pipeline.run()

    async def close(self) -> None:
        await self._pipeline.close()
