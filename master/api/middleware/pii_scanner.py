"""
master.api.middleware.pii_scanner
==================================
PII detection middleware. Scans every inbound message for PII patterns
before forwarding to agents. If PII is found in content destined for a
cloud LLM, raises PIIDetectedError (caught at router layer → 422).

Detection strategy:
  1. Regex patterns (fast, dependency-free): emails, phone numbers, SSN, credit
     cards, IP addresses. Always on — this is the CI-safe baseline.
  2. Presidio NER (accurate): PERSON, LOCATION, ORGANISATION entities, enabled on
     the cloud-dispatch path (use_ner=True). Presidio replaced the raw spaCy call
     (ADR-002/W1-3): it wraps the same spaCy model but adds validation, context
     scoring, and a maintained recognizer set. If Presidio or its model is
     unavailable, the scanner degrades to regex-only rather than failing.

Scan results are cached by content hash (W1-4): graph nodes are immutable between
writes, and the assembled context prefix is stable across turns, so an identical
block is scanned once. This keeps the added cloud-path NER within the p95 budget.

Cloud dispatch gate: content that tests positive must either be masked
or the user must explicitly set X-Lucifer-Allow-PII: true.

Two consumers, two policies:
  * The chat router (inbound user message) uses ``scan_or_raise`` — raw PII in
    a user's own prompt is rejected (422) unless they opt in.
  * The provider registry's cloud-dispatch gate uses ``ReversiblePseudonymiser``
    — personal-graph context assembled into a CompletionRequest is *reversibly
    pseudonymised* (never destructively masked) before it reaches a cloud LLM,
    then de-pseudonymised on the response.
"""

from __future__ import annotations

import enum
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, ClassVar

from master.core.exceptions import PIIDetectedError
from master.core.logging import get_logger

log = get_logger(__name__)


class PIICategory(enum.StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    IP_ADDRESS = "ip_address"
    PERSON_NAME = "person_name"
    LOCATION = "location"
    ORGANISATION = "organisation"


@dataclass
class PIIMatch:
    """A single detected PII instance."""

    category: PIICategory
    start: int
    end: int
    value: str  # Original matched text
    masked: str  # Replacement (e.g. "[EMAIL]")


@dataclass
class ScanResult:
    """Result from PIIScanner.scan()."""

    has_pii: bool
    matches: list[PIIMatch] = field(default_factory=list)
    masked_text: str = ""  # Original text with PII replaced by placeholders


class PIIScanner:
    """
    Detects and masks PII in text content.
    Combines regex (fast) with optional spaCy NER (accurate).
    Thread-safe — all state is read-only after construction.
    """

    # Compiled regex patterns for common PII
    _PATTERNS: ClassVar[list[tuple[PIICategory, re.Pattern[str]]]] = [
        (
            PIICategory.EMAIL,
            re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        ),
        (
            PIICategory.PHONE,
            re.compile(
                r"(?<!\d)(?:\+?1[\s\-.]?)?(?:\(\d{3}\)|\d{3})[\s\-.]?\d{3}[\s\-.]?\d{4}(?!\d)"
            ),
        ),
        (
            PIICategory.SSN,
            re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b"),
        ),
        (
            PIICategory.CREDIT_CARD,
            re.compile(
                r"\b(?:4\d{3}|5[1-5]\d{2}|6011|3[47]\d{2})"
                r"[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b"
            ),
        ),
        (
            PIICategory.IP_ADDRESS,
            re.compile(
                r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
                r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"
            ),
        ),
    ]

    # Presidio entity type → PIICategory. Only NER-derived entities are taken
    # from Presidio; the structured types (email/phone/SSN/…) stay on the regex
    # layer, so the two engines never emit the same span twice.
    _PRESIDIO_ENTITY_MAP: ClassVar[dict[str, PIICategory]] = {
        "PERSON": PIICategory.PERSON_NAME,
        "LOCATION": PIICategory.LOCATION,
        "ORGANIZATION": PIICategory.ORGANISATION,
    }

    def __init__(self, use_ner: bool = True, cache_size: int = 2048) -> None:
        self._use_ner = use_ner
        self._analyzer: Any = None
        # Content-hash scan cache (W1-4). 0 disables it.
        self._cache_size = max(0, cache_size)
        self._cache: OrderedDict[str, ScanResult] = OrderedDict()
        self._cache_lock = threading.Lock()
        if use_ner:
            self._load_analyzer()

    # spaCy models Presidio can use, best first. Presidio defaults to _lg.
    _SPACY_MODELS: ClassVar[tuple[str, ...]] = (
        "en_core_web_lg",
        "en_core_web_md",
        "en_core_web_sm",
    )

    def _load_analyzer(self) -> None:
        """
        Build the Presidio analyzer against an already-installed spaCy model.

        Presidio's default engine will pip-install a model on first use — an
        implicit network call that also raises SystemExit on failure. We refuse
        that path: probe for an installed model and only build the engine if one
        exists, otherwise degrade to the regex layer. This keeps construction
        offline-safe and side-effect-free.
        """
        try:
            model_name = self._installed_spacy_model()
            if model_name is None:
                raise RuntimeError("no spaCy model installed for Presidio NER")

            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": model_name}],
                }
            )
            self._analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
            log.info("pii_scanner.presidio.loaded", model=model_name)
        except Exception as exc:  # ImportError, missing model, engine build error
            log.warning("pii_scanner.presidio.unavailable", error=str(exc))
            self._analyzer = None
            self._use_ner = False

    @classmethod
    def _installed_spacy_model(cls) -> str | None:
        """Return the best installed spaCy model name, or None if none present."""
        try:
            import spacy.util
        except ImportError:
            return None
        return next((m for m in cls._SPACY_MODELS if spacy.util.is_package(m)), None)

    def _ner_matches(self, text: str) -> list[PIIMatch]:
        """Run Presidio and map recognised NER entities to PIIMatch objects."""
        if not self._use_ner or self._analyzer is None:
            return []
        try:
            results = self._analyzer.analyze(
                text=text,
                language="en",
                entities=list(self._PRESIDIO_ENTITY_MAP),
            )
        except Exception as exc:
            log.warning("pii_scanner.presidio.analyze_failed", error=str(exc))
            return []
        matches: list[PIIMatch] = []
        for r in results:
            cat = self._PRESIDIO_ENTITY_MAP.get(r.entity_type)
            if cat is None:
                continue
            matches.append(
                PIIMatch(
                    category=cat,
                    start=r.start,
                    end=r.end,
                    value=text[r.start : r.end],
                    masked=f"[{cat.value.upper()}]",
                )
            )
        return matches

    def scan(self, text: str) -> ScanResult:
        """
        Scan text for PII. Returns a ScanResult with all matches and masked text.
        Results are memoised by content hash. This method is synchronous — call
        from async code via run_in_executor when NER is enabled on long texts.
        """
        if not self._cache_size:
            return self._scan_uncached(text)

        key = sha256(text.encode("utf-8")).hexdigest()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

        result = self._scan_uncached(text)

        with self._cache_lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return result

    def _scan_uncached(self, text: str) -> ScanResult:
        """Detection pass without the cache — regex baseline plus Presidio NER."""
        matches: list[PIIMatch] = []

        # ── Regex scan ──────────────────────────────────────────────────────
        for category, pattern in self._PATTERNS:
            for m in pattern.finditer(text):
                matches.append(
                    PIIMatch(
                        category=category,
                        start=m.start(),
                        end=m.end(),
                        value=m.group(),
                        masked=f"[{category.value.upper()}]",
                    )
                )

        # ── NER scan (Presidio) ─────────────────────────────────────────────
        matches.extend(self._ner_matches(text))

        if not matches:
            return ScanResult(has_pii=False, masked_text=text)

        # ── Build masked text ───────────────────────────────────────────────
        # Sort by start position (reverse) so replacements don't shift offsets
        sorted_matches = sorted(matches, key=lambda m: m.start, reverse=True)
        masked = list(text)
        for match in sorted_matches:
            masked[match.start : match.end] = list(match.masked)

        log.info(
            "pii_scanner.pii_detected",
            count=len(matches),
            categories=list({m.category.value for m in matches}),
        )

        return ScanResult(
            has_pii=True,
            matches=matches,
            masked_text="".join(masked),
        )

    def scan_or_raise(self, text: str, context: str = "content") -> str:
        """
        Scan text and raise PIIDetectedError if PII is found.
        Returns the original text if clean.
        Use this before dispatching to cloud LLMs.
        """
        result = self.scan(text)
        if result.has_pii:
            categories = {m.category.value for m in result.matches}
            raise PIIDetectedError(
                f"PII detected in {context}: {', '.join(sorted(categories))}. "
                "Mask or obtain user consent before cloud dispatch."
            )
        return text


def _non_overlapping(matches: list[PIIMatch]) -> list[PIIMatch]:
    """
    Drop overlapping matches, preferring the longer span.

    Regex and NER passes can tag overlapping ranges (e.g. an email whose local
    part also trips the PERSON heuristic). Replacing overlapping spans by naive
    offset slicing corrupts the text, so keep a non-overlapping subset.
    """
    kept: list[PIIMatch] = []
    occupied: list[tuple[int, int]] = []
    # Longest span first, then leftmost — so the more specific match wins.
    for m in sorted(matches, key=lambda m: (-(m.end - m.start), m.start)):
        if any(not (m.end <= s or m.start >= e) for s, e in occupied):
            continue
        kept.append(m)
        occupied.append((m.start, m.end))
    return kept


class ReversiblePseudonymiser:
    """
    Stateful, reversible PII pseudonymisation scoped to a single logical request.

    Unlike ``PIIScanner.scan()``'s destructive ``[EMAIL]`` masking, this replaces
    each *distinct* PII value with a stable, numbered token (``EMAIL_1``,
    ``PERSON_2``) so a cloud model still sees coherent, de-referenceable
    placeholders — and the reverse mapping lets the response be de-pseudonymised
    if the model echoes a token back.

    One instance == one request. Reuse it across every message in that request so
    the same value maps to the same token throughout (``john@x.com`` is
    ``EMAIL_1`` in both the system and user turn).

    Thread-safety: an instance is single-request scoped and not shared across
    coroutines; the wrapped scanner is read-only and safe to share.
    """

    def __init__(self, scanner: PIIScanner) -> None:
        self._scanner = scanner
        self._forward: dict[str, str] = {}  # original value -> token
        self._reverse: dict[str, str] = {}  # token -> original value
        self._counters: dict[PIICategory, int] = {}

    @property
    def mapping(self) -> dict[str, str]:
        """Copy of the token → original-value map used to restore the response."""
        return dict(self._reverse)

    @property
    def has_pii(self) -> bool:
        """True once at least one value has been pseudonymised."""
        return bool(self._reverse)

    def pseudonymise(self, text: str) -> str:
        """Return ``text`` with every detected PII value replaced by a token."""
        result = self._scanner.scan(text)
        if not result.has_pii:
            return text
        # Replace right-to-left so earlier character offsets stay valid.
        chars = list(text)
        for match in sorted(_non_overlapping(result.matches), key=lambda m: m.start, reverse=True):
            token = self._token_for(match)
            chars[match.start : match.end] = list(token)
        return "".join(chars)

    def _token_for(self, match: PIIMatch) -> str:
        """Return the stable token for a value, minting a new one on first sight."""
        if match.value in self._forward:
            return self._forward[match.value]
        n = self._counters.get(match.category, 0) + 1
        self._counters[match.category] = n
        token = f"{match.category.value.upper()}_{n}"
        self._forward[match.value] = token
        self._reverse[token] = match.value
        return token

    def restore(self, text: str) -> str:
        """Replace any pseudonyms in ``text`` with their original values."""
        if not self._reverse:
            return text
        # Longest tokens first so PERSON_10 is restored before PERSON_1; the word
        # boundary stops PERSON_1 from matching inside PERSON_10 as well.
        for token in sorted(self._reverse, key=len, reverse=True):
            text = re.sub(rf"\b{re.escape(token)}\b", self._reverse[token], text)
        return text
