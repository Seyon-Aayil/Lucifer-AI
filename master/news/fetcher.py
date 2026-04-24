"""
master.news.fetcher
====================
Async news article fetcher.
Supports RSS/Atom feeds, Reddit JSON API, arXiv API, GitHub trending.
Respects ETags and Last-Modified headers for conditional fetching.
Rate-limited per domain to avoid blacklisting.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import aiohttp
import feedparser  # type: ignore[import]

from master.core.logging import get_logger

log = get_logger(__name__)

# Per-domain request rate limit (seconds between requests)
_RATE_LIMITS: dict[str, float] = {
    "reddit.com": 2.0,
    "arxiv.org": 1.0,
    "github.com": 1.0,
}
_DEFAULT_RATE_LIMIT = 0.5


@dataclass
class RawArticle:
    """A single raw article fetched from a source."""
    url: str
    title: str
    body: str
    source_id: str
    published_at: datetime | None
    author: str | None = None
    tags: list[str] = field(default_factory=list)
    url_hash: str = ""

    def __post_init__(self) -> None:
        self.url_hash = hashlib.sha256(self.url.encode()).hexdigest()[:32]


@dataclass
class NewsSource:
    """Configuration for a single news source."""
    source_id: str
    source_type: str        # "rss", "reddit", "arxiv", "github"
    url: str
    cadence_minutes: int = 60
    max_articles: int = 50
    etag: str | None = None
    last_modified: str | None = None


class NewsFetcher:
    """
    Async multi-source news fetcher with ETag-based conditional fetching.
    All fetches are rate-limited per domain.
    """

    def __init__(self, session: aiohttp.ClientSession | None = None) -> None:
        self._session: aiohttp.ClientSession | None = session
        self._last_request: dict[str, float] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "LuciferAI/0.1 (news-sync; +https://github.com/lucifer-ai)"
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def _rate_limit(self, domain: str) -> None:
        """Sleep if we're within the rate limit window for this domain."""
        limit = _RATE_LIMITS.get(domain, _DEFAULT_RATE_LIMIT)
        last = self._last_request.get(domain, 0.0)
        elapsed = asyncio.get_event_loop().time() - last
        if elapsed < limit:
            await asyncio.sleep(limit - elapsed)
        self._last_request[domain] = asyncio.get_event_loop().time()

    def _domain(self, url: str) -> str:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lstrip("www.")

    async def fetch_source(self, source: NewsSource) -> list[RawArticle]:
        """Dispatch to the appropriate fetcher based on source_type."""
        fetchers = {
            "rss": self._fetch_rss,
            "reddit": self._fetch_reddit,
            "arxiv": self._fetch_arxiv,
        }
        fetcher = fetchers.get(source.source_type, self._fetch_rss)
        try:
            articles = await fetcher(source)
            log.info("news.fetched", source=source.source_id, count=len(articles))
            return articles
        except Exception as exc:
            log.warning("news.fetch_failed", source=source.source_id, error=str(exc))
            return []

    async def _fetch_rss(self, source: NewsSource) -> list[RawArticle]:
        session = await self._get_session()
        await self._rate_limit(self._domain(source.url))

        headers: dict[str, str] = {}
        if source.etag:
            headers["If-None-Match"] = source.etag
        if source.last_modified:
            headers["If-Modified-Since"] = source.last_modified

        async with session.get(source.url, headers=headers) as resp:
            if resp.status == 304:
                return []  # Not modified

            # Update conditional fetch headers
            source.etag = resp.headers.get("ETag")
            source.last_modified = resp.headers.get("Last-Modified")
            content = await resp.text()

        feed = feedparser.parse(content)
        articles: list[RawArticle] = []
        for entry in feed.entries[: source.max_articles]:
            body = ""
            if hasattr(entry, "summary"):
                body = entry.summary
            elif hasattr(entry, "content"):
                body = entry.content[0].value if entry.content else ""

            pub: datetime | None = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                import calendar
                pub = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=UTC)

            articles.append(
                RawArticle(
                    url=entry.get("link", ""),
                    title=entry.get("title", ""),
                    body=body,
                    source_id=source.source_id,
                    published_at=pub,
                    author=entry.get("author"),
                    tags=[t.term for t in entry.get("tags", [])],
                )
            )
        return articles

    async def _fetch_reddit(self, source: NewsSource) -> list[RawArticle]:
        session = await self._get_session()
        await self._rate_limit("reddit.com")
        json_url = source.url.rstrip("/") + ".json?limit=25&raw_json=1"

        async with session.get(json_url) as resp:
            data: dict[str, Any] = await resp.json()

        posts = data.get("data", {}).get("children", [])
        return [
            RawArticle(
                url=f"https://reddit.com{p['data']['permalink']}",
                title=p["data"]["title"],
                body=p["data"].get("selftext", ""),
                source_id=source.source_id,
                published_at=datetime.fromtimestamp(p["data"]["created_utc"], tz=UTC),
                tags=[p["data"]["subreddit"]],
            )
            for p in posts[: source.max_articles]
            if p["data"].get("title")
        ]

    async def _fetch_arxiv(self, source: NewsSource) -> list[RawArticle]:
        """Fetch arXiv papers via the Atom feed API."""
        # Use the Atom feed endpoint which is RSS-compatible
        return await self._fetch_rss(source)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
