"""
master.tests.unit.test_pii_scanner
====================================
Unit tests for the PII scanner — regex patterns and masked output.
NER model is disabled (use_ner=False) to avoid runtime model download in CI.
"""
from __future__ import annotations

import pytest

from master.api.middleware.pii_scanner import PIICategory, PIIScanner
from master.core.exceptions import PIIDetectedError


@pytest.fixture(scope="module")
def scanner() -> PIIScanner:
    """Shared scanner instance without NER (CI-safe)."""
    return PIIScanner(use_ner=False)


class TestEmailDetection:
    def test_detects_plain_email(self, scanner: PIIScanner) -> None:
        result = scanner.scan("Send it to john.doe@example.com please.")
        assert result.has_pii
        assert any(m.category == PIICategory.EMAIL for m in result.matches)

    def test_email_masked_in_output(self, scanner: PIIScanner) -> None:
        result = scanner.scan("Contact admin@lucifer.ai for help.")
        assert "[EMAIL]" in result.masked_text

    def test_no_false_positive_on_plain_text(self, scanner: PIIScanner) -> None:
        result = scanner.scan("The weather is nice today.")
        assert not result.has_pii


class TestPhoneDetection:
    def test_detects_us_phone_formats(self, scanner: PIIScanner) -> None:
        for phone in ("555-867-5309", "(555) 867-5309", "+1 555 867 5309"):
            result = scanner.scan(f"Call me at {phone}")
            assert result.has_pii, f"Should detect phone: {phone}"

    def test_phone_masked_in_output(self, scanner: PIIScanner) -> None:
        result = scanner.scan("My number is 555-867-5309.")
        assert "[PHONE]" in result.masked_text


class TestSSNDetection:
    def test_detects_ssn_with_dashes(self, scanner: PIIScanner) -> None:
        result = scanner.scan("SSN: 123-45-6789")
        assert result.has_pii
        assert any(m.category == PIICategory.SSN for m in result.matches)


class TestCreditCardDetection:
    def test_detects_visa(self, scanner: PIIScanner) -> None:
        result = scanner.scan("Card: 4111-1111-1111-1111")
        assert result.has_pii
        assert any(m.category == PIICategory.CREDIT_CARD for m in result.matches)


class TestScanOrRaise:
    def test_clean_text_returns_unchanged(self, scanner: PIIScanner) -> None:
        text = "What is the capital of France?"
        assert scanner.scan_or_raise(text) == text

    def test_pii_text_raises(self, scanner: PIIScanner) -> None:
        with pytest.raises(PIIDetectedError):
            scanner.scan_or_raise("Please email john@example.com")


class TestACL:
    def test_acl_financial_agent_cannot_read_health(self) -> None:
        from master.agents.librarian.access_control import check_permission
        from master.core.exceptions import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            check_permission("financial-agent", "HealthRecord", "read")

    def test_acl_health_agent_can_write_health(self) -> None:
        from master.agents.librarian.access_control import check_permission

        # Should not raise
        check_permission("health-agent", "HealthRecord", "write")

    def test_acl_librarian_always_passes(self) -> None:
        from master.agents.librarian.access_control import check_permission

        check_permission("librarian-agent", "HealthRecord", "read")
        check_permission("librarian-agent", "Financial", "write")

    def test_acl_unknown_agent_denied(self) -> None:
        from master.agents.librarian.access_control import check_permission
        from master.core.exceptions import PermissionDeniedError

        with pytest.raises(PermissionDeniedError):
            check_permission("unknown-agent-xyz", "Task", "write")
