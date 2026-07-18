"""
W1-3 / W1-4: Presidio NER engine swap + content-hash scan cache.

The real Presidio NER path needs a spaCy model that CI does not download, so the
engine is exercised here through an injected analyzer returning real
``RecognizerResult`` objects — this pins the Presidio→PIIMatch mapping and the
regex/NER merge without a model. The graceful-degradation and cache behaviours
are model-independent and tested directly.

``test_pii_scanner.py`` is intentionally left unmodified; it proves the regex
baseline (use_ner=False) still stands after the engine swap.
"""

from __future__ import annotations

from master.api.middleware.pii_scanner import PIICategory, PIIScanner


class _FakeResult:
    """Stand-in for presidio_analyzer.RecognizerResult (entity_type/start/end)."""

    def __init__(self, entity_type: str, start: int, end: int) -> None:
        self.entity_type = entity_type
        self.start = start
        self.end = end
        self.score = 0.99


class _FakeAnalyzer:
    """Returns canned Presidio results for a fixed name; mimics AnalyzerEngine."""

    def __init__(self, entity_type: str, needle: str) -> None:
        self._entity_type = entity_type
        self._needle = needle
        self.calls = 0

    def analyze(self, text: str, language: str, entities: list[str]) -> list[_FakeResult]:
        self.calls += 1
        idx = text.find(self._needle)
        if idx == -1:
            return []
        return [_FakeResult(self._entity_type, idx, idx + len(self._needle))]


def _scanner_with_analyzer(analyzer: object, cache_size: int = 2048) -> PIIScanner:
    """A regex-only scanner (no real model) with an injected Presidio analyzer."""
    scanner = PIIScanner(use_ner=False, cache_size=cache_size)
    scanner._use_ner = True  # type: ignore[attr-defined]
    scanner._analyzer = analyzer  # type: ignore[attr-defined]
    return scanner


# ── Presidio → PIIMatch mapping ───────────────────────────────────────────────


def test_presidio_person_mapped_to_person_name() -> None:
    scanner = _scanner_with_analyzer(_FakeAnalyzer("PERSON", "Jane Doe"))
    result = scanner.scan("Remind Jane Doe about lunch")
    assert result.has_pii
    assert any(
        m.category == PIICategory.PERSON_NAME and m.value == "Jane Doe" for m in result.matches
    )
    assert "[PERSON_NAME]" in result.masked_text


def test_presidio_location_and_org_mapping() -> None:
    for entity, needle, cat in (
        ("LOCATION", "Bangalore", PIICategory.LOCATION),
        ("ORGANIZATION", "Cisco", PIICategory.ORGANISATION),
    ):
        scanner = _scanner_with_analyzer(_FakeAnalyzer(entity, needle))
        result = scanner.scan(f"Note about {needle} here")
        assert any(m.category == cat for m in result.matches), entity


def test_unmapped_presidio_entity_ignored() -> None:
    # DATE_TIME is not in the NER map — it must not produce a match.
    scanner = _scanner_with_analyzer(_FakeAnalyzer("DATE_TIME", "tomorrow"))
    result = scanner.scan("Do it tomorrow")
    assert not result.has_pii


def test_regex_and_ner_both_contribute() -> None:
    scanner = _scanner_with_analyzer(_FakeAnalyzer("PERSON", "Jane Doe"))
    result = scanner.scan("Email Jane Doe at jane@example.com")
    cats = {m.category for m in result.matches}
    assert PIICategory.PERSON_NAME in cats
    assert PIICategory.EMAIL in cats


# ── Graceful degradation ──────────────────────────────────────────────────────


def test_ner_disabled_uses_regex_only() -> None:
    # No analyzer wired: NER contributes nothing, regex still fires.
    scanner = PIIScanner(use_ner=False)
    result = scanner.scan("Reach me at admin@lucifer.ai")
    assert result.has_pii
    assert all(m.category != PIICategory.PERSON_NAME for m in result.matches)


def test_analyzer_exception_degrades_to_regex() -> None:
    class _Boom:
        def analyze(self, **_: object) -> list[_FakeResult]:
            raise RuntimeError("presidio blew up")

    scanner = _scanner_with_analyzer(_Boom())
    # Regex PII still detected; the analyzer error is swallowed, not raised.
    result = scanner.scan("Card 4111-1111-1111-1111 and name Jane Doe")
    assert result.has_pii
    assert any(m.category == PIICategory.CREDIT_CARD for m in result.matches)
    assert all(m.category != PIICategory.PERSON_NAME for m in result.matches)


def test_missing_model_disables_ner() -> None:
    # use_ner=True but AnalyzerEngine() cannot build without a model in CI.
    # The scanner must come up regex-only, not raise.
    scanner = PIIScanner(use_ner=True)
    assert scanner._use_ner is False  # type: ignore[attr-defined]
    assert scanner.scan("Email admin@lucifer.ai").has_pii


# ── Content-hash cache (W1-4) ─────────────────────────────────────────────────


def test_cache_hit_skips_second_scan() -> None:
    analyzer = _FakeAnalyzer("PERSON", "Jane Doe")
    scanner = _scanner_with_analyzer(analyzer, cache_size=8)
    text = "Remind Jane Doe about lunch"

    first = scanner.scan(text)
    second = scanner.scan(text)

    assert analyzer.calls == 1, "identical text must not be re-analysed"
    assert first is second  # same cached object returned


def test_cache_distinguishes_different_text() -> None:
    analyzer = _FakeAnalyzer("PERSON", "Jane Doe")
    scanner = _scanner_with_analyzer(analyzer, cache_size=8)

    scanner.scan("Remind Jane Doe about lunch")
    scanner.scan("A totally different sentence")

    assert analyzer.calls == 2


def test_cache_disabled_when_size_zero() -> None:
    analyzer = _FakeAnalyzer("PERSON", "Jane Doe")
    scanner = _scanner_with_analyzer(analyzer, cache_size=0)
    text = "Remind Jane Doe about lunch"

    scanner.scan(text)
    scanner.scan(text)

    assert analyzer.calls == 2, "cache_size=0 must not memoise"


def test_cache_evicts_lru_over_capacity() -> None:
    scanner = PIIScanner(use_ner=False, cache_size=2)
    scanner.scan("one")
    scanner.scan("two")
    scanner.scan("three")  # evicts "one"
    assert len(scanner._cache) == 2  # type: ignore[attr-defined]
