"""
master.core.auth.device
========================
Device hardware-fingerprint validation.

The edge client may present a hardware fingerprint (TPM / Secure Enclave
hash) at pairing time; it is persisted on the `devices` row and re-checked
on token refresh. Enforcement is opportunistic: a device that never sent a
fingerprint (or pre-dates the column) is not penalised — only an actual
mismatch between a stored and a presented value is rejected.
"""

from __future__ import annotations

import secrets

from master.core.logging import get_logger

log = get_logger(__name__)


def fingerprint_matches(stored: str | None, presented: str | None) -> bool:
    """
    Compare a stored device fingerprint against a presented one.
    Mismatch only when BOTH sides are present and differ; either side
    absent means "no opinion" and passes. Constant-time comparison.
    """
    if not stored or not presented:
        return True
    return secrets.compare_digest(stored, presented)
