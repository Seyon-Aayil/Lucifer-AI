"""
master.api.middleware.pii_scanner
==================================
PII detection middleware. Scans every inbound message for PII patterns
before forwarding to agents. If PII is found in content destined for a
cloud LLM, raises PIIDetectedError (caught at router layer → 422).

Detection strategy:
  1. Regex patterns (fast): emails, phone numbers, SSN, credit cards, IP addresses.
  2. spaCy NER (accurate): PERSON, ORG, GPE, LOC entities.

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
from dataclasses import dataclass, field
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

    def __init__(self, use_ner: bool = True) -> None:
        self._use_ner = use_ner
        self._nlp: Any = None
        if use_ner:
            self._load_ner()

    def _load_ner(self) -> None:
        """Lazy-load spaCy model (en_core_web_sm)."""
        try:
            import spacy

            self._nlp = spacy.load("en_core_web_sm", disable=["parser", "lemmatizer"])
            log.info("pii_scanner.ner.loaded", model="en_core_web_sm")
        except (ImportError, OSError) as exc:
            log.warning("pii_scanner.ner.unavailable", error=str(exc))
            self._use_ner = False

    def scan(self, text: str) -> ScanResult:
        """
        Scan text for PII. Returns a ScanResult with all matches and masked text.
        This method is synchronous — call from async code via run_in_executor
        if the NER model is being used on long texts.
        """
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

        # ── NER scan ────────────────────────────────────────────────────────
        if self._use_ner and self._nlp is not None:
            doc = self._nlp(text)
            ner_label_map = {
                "PERSON": PIICategory.PERSON_NAME,
                "GPE": PIICategory.LOCATION,
                "LOC": PIICategory.LOCATION,
                "ORG": PIICategory.ORGANISATION,
            }
            for ent in doc.ents:
                if ent.label_ in ner_label_map:
                    cat = ner_label_map[ent.label_]
                    matches.append(
                        PIIMatch(
                            category=cat,
                            start=ent.start_char,
                            end=ent.end_char,
                            value=ent.text,
                            masked=f"[{cat.value.upper()}]",
                        )
                    )

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
