"""Unit tests for master.core.auth.device.fingerprint_matches."""

from __future__ import annotations

import pytest

from master.core.auth.device import fingerprint_matches


@pytest.mark.parametrize(
    ("stored", "presented", "expected"),
    [
        # Either side absent → no opinion → pass.
        (None, None, True),
        ("fp-stored", None, True),
        (None, "fp-presented", True),
        ("", "", True),
        ("fp-stored", "", True),
        ("", "fp-presented", True),
        # Both present: exact match required.
        ("fp-abc123", "fp-abc123", True),
        ("fp-abc123", "fp-abc124", False),
        ("fp-abc123", "FP-ABC123", False),  # case-sensitive
        ("fp-abc123", "fp-abc123 ", False),  # no trimming
    ],
)
def test_fingerprint_matches(stored: str | None, presented: str | None, expected: bool) -> None:
    assert fingerprint_matches(stored, presented) is expected
