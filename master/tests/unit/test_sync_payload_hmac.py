"""Unit tests for LuciferSyncServicer._verify_hmac — the payload HMAC contract.

Contract: payload_hmac = HMAC-SHA256(key, SerializeToString(message with the
payload_hmac field cleared)), full 32 bytes.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock

import pytest

from master.core.crypto import payload_hmac_key as derive_payload_hmac_key
from master.sync.lucifer_sync_pb2 import NodeDelta, SyncMessage  # type: ignore[attr-defined]
from master.sync.server import LuciferSyncServicer

_KEY = bytes(range(32))  # fixed 32-byte test key, injected — settings untouched
_DEVICE = "device-mac-01"


@pytest.fixture
def servicer() -> LuciferSyncServicer:
    return LuciferSyncServicer(
        graph_client=MagicMock(),
        db_pool=MagicMock(),
        nats_js=MagicMock(),
        audit_logger=MagicMock(),
        payload_hmac_key=_KEY,
    )


def _message() -> SyncMessage:
    return SyncMessage(
        device_id="device-mac-01",
        sync_session_id="sess-1",
        vector_clock=42,
        node_deltas=[
            NodeDelta(
                node_id="n1",
                operation="upsert",
                node_type="Memory",
                payload=b"hello",
                classification="standard",
            )
        ],
    )


def _sign(msg: SyncMessage, key: bytes = _KEY) -> bytes:
    clone = SyncMessage()
    clone.CopyFrom(msg)
    clone.ClearField("payload_hmac")
    return hmac.new(key, clone.SerializeToString(), hashlib.sha256).digest()


def test_valid_mac_accepted(servicer: LuciferSyncServicer) -> None:
    msg = _message()
    msg.payload_hmac = _sign(msg)
    assert servicer._verify_hmac(msg, _DEVICE) is True


def test_tampered_payload_rejected(servicer: LuciferSyncServicer) -> None:
    msg = _message()
    msg.payload_hmac = _sign(msg)
    msg.device_id = "attacker-device"  # mutate after signing
    assert servicer._verify_hmac(msg, _DEVICE) is False


def test_tampered_delta_rejected(servicer: LuciferSyncServicer) -> None:
    msg = _message()
    msg.payload_hmac = _sign(msg)
    msg.node_deltas[0].payload = b"evil"
    assert servicer._verify_hmac(msg, _DEVICE) is False


def test_mac_over_message_including_hmac_field_rejected(servicer: LuciferSyncServicer) -> None:
    """Regression guard for the clear-field rule: the MAC must not cover itself."""
    msg = _message()
    msg.payload_hmac = b"\x00" * 32  # placeholder so serialisation includes field 7
    wrong = hmac.new(_KEY, msg.SerializeToString(), hashlib.sha256).digest()
    msg.payload_hmac = wrong
    assert servicer._verify_hmac(msg, _DEVICE) is False


def test_wrong_key_rejected(servicer: LuciferSyncServicer) -> None:
    msg = _message()
    msg.payload_hmac = _sign(msg, key=b"\xff" * 32)
    assert servicer._verify_hmac(msg, _DEVICE) is False


def test_truncated_mac_rejected(servicer: LuciferSyncServicer) -> None:
    msg = _message()
    msg.payload_hmac = _sign(msg)[:16]
    assert servicer._verify_hmac(msg, _DEVICE) is False


def test_absent_mac_allowed(servicer: LuciferSyncServicer) -> None:
    # Edge clients don't send the MAC yet — absence must not block the stream.
    assert servicer._verify_hmac(_message(), _DEVICE) is True


def test_per_device_keys_are_isolated() -> None:
    """Without an injected key, each device verifies under its own derived key:
    a MAC valid for device A must not verify when checked as device B."""
    servicer = LuciferSyncServicer(
        graph_client=MagicMock(),
        db_pool=MagicMock(),
        nats_js=MagicMock(),
        audit_logger=MagicMock(),
        app_secret_key="unit-test-secret",
    )
    msg = _message()  # device_id == _DEVICE
    msg.payload_hmac = _sign(msg, key=derive_payload_hmac_key("unit-test-secret", _DEVICE))

    assert servicer._verify_hmac(msg, _DEVICE) is True
    # Same bytes, checked under a different device's key → rejected.
    assert servicer._verify_hmac(msg, "device-other-02") is False
