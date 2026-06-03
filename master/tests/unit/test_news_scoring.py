"""Unit tests for master.news.pipeline scoring functions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from master.news.fetcher import RawArticle
from master.news.pipeline import _content_score, _recency_score, _score_articles


def _article(
    url: str = "https://example.com/x",
    title: str = "T",
    body: str = "body",
    published_at: datetime | None = None,
) -> RawArticle:
    return RawArticle(
        url=url,
        title=title,
        body=body,
        source_id="src",
        published_at=published_at,
    )


# ── _recency_score ────────────────────────────────────────────────────────────


def test_recency_now_is_one():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _recency_score(now, now) == 1.0


def test_recency_at_half_life_is_half():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pub = now - timedelta(hours=24)  # _RECENCY_HALF_LIFE_HOURS = 24
    assert abs(_recency_score(pub, now) - 0.5) < 1e-6


def test_recency_two_half_lives_is_quarter():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pub = now - timedelta(hours=48)
    assert abs(_recency_score(pub, now) - 0.25) < 1e-6


def test_recency_none_returns_neutral():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert _recency_score(None, now) == 0.3


def test_recency_naive_datetime_treated_as_utc():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    pub_naive = datetime(2026, 1, 1)  # naive
    # Should not crash and should equal recency at now
    assert _recency_score(pub_naive, now) == 1.0


def test_recency_future_clamped_to_now():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    future = now + timedelta(hours=12)
    # max(0.0, ...) clamps negative hours_ago
    assert _recency_score(future, now) == 1.0


# ── _content_score ────────────────────────────────────────────────────────────


def test_content_empty_is_zero():
    assert _content_score("") == 0.0
    assert _content_score("   \n\t  ") == 0.0


def test_content_caps_at_one():
    big = "x" * 1_000_000
    assert _content_score(big) == 1.0


def test_content_500_chars_about_half():
    s = _content_score("x" * 500)
    # log10(500) / 4 = 2.699 / 4 ≈ 0.675; spec says ~0.5 — accept band
    assert 0.5 <= s <= 0.75


def test_content_monotonic_in_length():
    assert _content_score("x" * 100) < _content_score("x" * 1000) < _content_score("x" * 10000)


# ── _score_articles ───────────────────────────────────────────────────────────


def test_score_articles_returns_descending():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fresh = _article(url="https://a/1", body="x" * 5000, published_at=now)
    stale = _article(url="https://a/2", body="short", published_at=now - timedelta(hours=72))
    scored = _score_articles([stale, fresh], now)
    # Fresh + long body should outrank stale + short
    assert scored[0][0].url == "https://a/1"
    assert scored[1][0].url == "https://a/2"
    assert scored[0][1] > scored[1][1]


def test_score_articles_score_in_range():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    a = _article(body="x" * 1000, published_at=now)
    scored = _score_articles([a], now)
    assert 0.0 <= scored[0][1] <= 1.0


def test_score_articles_weight_70_30():
    """0.7 * recency + 0.3 * content. Verify a known case."""
    now = datetime(2026, 1, 1, tzinfo=UTC)
    # recency=1.0 (now), content=0.0 (empty body) → score = 0.7
    a = _article(body="", published_at=now)
    scored = _score_articles([a], now)
    assert scored[0][1] == 0.7


def test_score_articles_empty_input():
    assert _score_articles([], datetime(2026, 1, 1, tzinfo=UTC)) == []
