"""
master.tests.unit.test_crypto
==============================
Unit tests for master.core.crypto — Argon2id key derivation, AES-256-GCM
encrypt/decrypt, HMAC chain computation.
No external services required.
"""
from __future__ import annotations

import pytest

from master.core.crypto import (
    compute_chain_hmac,
    decrypt,
    decrypt_str,
    derive_aes_key,
    derive_key,
    encrypt,
    encrypt_str,
    hmac_sha256,
    sha256_hex,
    verify_key,
)


class TestArgon2id:
    def test_derive_key_returns_hash_string(self) -> None:
        h = derive_key("my-password")
        assert h.startswith("$argon2id$")

    def test_verify_key_correct_password(self) -> None:
        h = derive_key("correct-password")
        assert verify_key(h, "correct-password") is True

    def test_verify_key_wrong_password(self) -> None:
        h = derive_key("correct-password")
        assert verify_key(h, "wrong-password") is False

    def test_derive_aes_key_returns_32_bytes(self) -> None:
        key, salt = derive_aes_key("password")
        assert len(key) == 32
        assert len(salt) == 16

    def test_derive_aes_key_deterministic_with_same_salt(self) -> None:
        key1, salt = derive_aes_key("password")
        key2, _ = derive_aes_key("password", salt=salt)
        assert key1 == key2

    def test_derive_aes_key_different_salt_different_key(self) -> None:
        key1, _ = derive_aes_key("password")
        key2, _ = derive_aes_key("password")
        assert key1 != key2  # Different random salts


class TestAESGCM:
    def test_encrypt_decrypt_roundtrip(self) -> None:
        key, _ = derive_aes_key("test-key")
        plaintext = b"Hello, Lucifer AI!"
        ciphertext = encrypt(plaintext, key)
        assert decrypt(ciphertext, key) == plaintext

    def test_encrypt_produces_different_ciphertext_each_time(self) -> None:
        key, _ = derive_aes_key("test-key")
        pt = b"same plaintext"
        ct1 = encrypt(pt, key)
        ct2 = encrypt(pt, key)
        assert ct1 != ct2  # Different nonces

    def test_decrypt_wrong_key_raises(self) -> None:
        key1, _ = derive_aes_key("key1")
        key2, _ = derive_aes_key("key2")
        ct = encrypt(b"secret", key1)
        with pytest.raises(Exception):  # cryptography raises InvalidTag
            decrypt(ct, key2)

    def test_encrypt_str_decrypt_str_roundtrip(self) -> None:
        key, _ = derive_aes_key("str-key")
        original = "Private health record"
        assert decrypt_str(encrypt_str(original, key), key) == original

    def test_wrong_key_length_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            encrypt(b"data", b"short-key")


class TestHMACChain:
    def test_hmac_sha256_deterministic(self) -> None:
        key = b"chain-secret"
        h1 = hmac_sha256(key, b"data")
        h2 = hmac_sha256(key, b"data")
        assert h1 == h2

    def test_hmac_sha256_different_key(self) -> None:
        assert hmac_sha256(b"key1", b"data") != hmac_sha256(b"key2", b"data")

    def test_sha256_hex_length(self) -> None:
        assert len(sha256_hex(b"any data")) == 64

    def test_compute_chain_hmac_chaining(self) -> None:
        """Verify that the chain HMAC changes with each new link."""
        secret = b"audit-secret"
        prev = "0" * 64
        payload = sha256_hex(b"event-payload")
        h1 = compute_chain_hmac(prev, payload, secret)
        h2 = compute_chain_hmac(h1, sha256_hex(b"next-payload"), secret)
        assert h1 != h2
        assert len(h1) == 64
        assert len(h2) == 64
