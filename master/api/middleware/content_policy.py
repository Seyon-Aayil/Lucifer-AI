"""
master.api.middleware.content_policy
=====================================
Content policy validation for inbound user messages and outbound responses.

Checks (applied in order, short-circuit on first match):
  1. Length guard      — reject messages over the hard char limit
  2. Keyword blocklist — catch obvious harmful / jailbreak patterns
  3. Prompt injection  — detect attempts to override system instructions
  4. PII leak guard    — flag outbound responses containing raw credentials

Each check returns a PolicyResult with a violation reason if blocked.
validate() is synchronous and designed to run in a thread-pool executor
(see PIIScanner precedent in pii_scanner.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from master.core.logging import get_logger

log = get_logger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

_MAX_INPUT_CHARS = 32_000

# Patterns for prompt-injection attempts (case-insensitive)
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"forget\s+(all\s+)?(previous|prior|above)\s+instructions?", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a\s+)?(?:dan|jailbreak|unrestricted)", re.I),
    re.compile(r"\[system\]|\[assistant\]|\[user\]", re.I),  # role-injection
    re.compile(r"<\|im_start\|>|<\|im_end\|>", re.I),  # token injection
    re.compile(r"system\s*:\s*you\s+are", re.I),
]

# Outbound PII leak guard — raw secrets / credentials in responses
_OUTBOUND_LEAK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*\S+", re.I),
    re.compile(r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"[a-z0-9]{32,}:[a-z0-9]{32,}"),  # likely credential pair
]

# Hard blocklist: topics that must never be processed regardless of context.
# Kept minimal — overly broad blocks degrade UX. This is a last-resort layer.
_BLOCKLIST_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(?:generate|create|write)\b.{0,40}\b(?:malware|ransomware|keylogger|rootkit)\b", re.I
    ),
    re.compile(
        r"\b(?:step[s\s-]*by[s\s-]*step|instructions?\s+for)\b.{0,60}\b(?:make|build|create|synthesize)\b.{0,40}\b(?:bomb|explosive|weapon|poison)\b",
        re.I,
    ),
    re.compile(r"\b(?:csam|child\s+pornography|child\s+sexual\s+abuse)\b", re.I),
]


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PolicyResult:
    allowed: bool
    violation: str | None = None  # set when allowed=False
    check: str | None = None  # which check triggered

    @classmethod
    def ok(cls) -> PolicyResult:
        return cls(allowed=True)

    @classmethod
    def blocked(cls, check: str, reason: str) -> PolicyResult:
        return cls(allowed=False, violation=reason, check=check)


# ── Validator ─────────────────────────────────────────────────────────────────


class ContentPolicyValidator:
    """
    Stateless content policy validator.
    Thread-safe; safe to call from a thread-pool executor.
    """

    def validate_input(self, text: str) -> PolicyResult:
        """
        Check an inbound user message against all input policies.
        Returns PolicyResult(allowed=True) or a blocked result with reason.
        """
        # 1. Length guard
        if len(text) > _MAX_INPUT_CHARS:
            return PolicyResult.blocked(
                "length",
                f"Message exceeds maximum length ({len(text)} > {_MAX_INPUT_CHARS} chars)",
            )

        # 2. Hard blocklist
        for pattern in _BLOCKLIST_PATTERNS:
            if pattern.search(text):
                log.warning("content_policy.blocked.blocklist", pattern=pattern.pattern[:50])
                return PolicyResult.blocked("blocklist", "Message contains prohibited content")

        # 3. Prompt injection
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(text):
                log.warning("content_policy.blocked.injection", pattern=pattern.pattern[:50])
                return PolicyResult.blocked(
                    "injection",
                    "Message appears to contain a prompt injection attempt",
                )

        return PolicyResult.ok()

    def validate_output(self, text: str) -> PolicyResult:
        """
        Check an outbound LLM response for accidental credential/secret leakage.
        Returns PolicyResult(allowed=True) or blocked with reason.
        """
        for pattern in _OUTBOUND_LEAK_PATTERNS:
            if pattern.search(text):
                log.error(
                    "content_policy.output_leak_detected",
                    pattern=pattern.pattern[:50],
                )
                return PolicyResult.blocked(
                    "output_leak",
                    "Response may contain sensitive credentials — blocked before delivery",
                )
        return PolicyResult.ok()

    # Legacy compat — kept so existing call-sites don't break
    def validate(self, text: str) -> bool:
        return self.validate_input(text).allowed
