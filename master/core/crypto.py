"""
master.core.crypto
==================
Cryptographic primitives used across Lucifer AI.
Provides: Argon2id key derivation, AES-256-GCM encrypt/decrypt.
All key material is kept in memory only — never logged or persisted as plaintext.
"""
from __future__ import annotations

import os
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ── Argon2id Configuration (OWASP 2024 minimum) ──────────────────────────────
_ARGON2 = PasswordHasher(
    time_cost=3,        # iterations
    memory_cost=65536,  # 64 MB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

# AES-GCM nonce size (bytes)
_GCM_NONCE_SIZE = 12


# ── Key Derivation ────────────────────────────────────────────────────────────

def derive_key(password: str) -> str:
    """
    Derive a key from a user password using Argon2id.
    Returns the full Argon2 hash string (includes salt + params).
    Store this string; never store the raw password.
    """
    return _ARGON2.hash(password)


def verify_key(hash_str: str, password: str) -> bool:
    """
    Verify a password against a stored Argon2id hash.
    Returns True if the password matches; False otherwise.
    Constant-time — safe against timing attacks.
    """
    try:
        return _ARGON2.verify(hash_str, password)
    except VerifyMismatchError:
        return False


def derive_aes_key(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    """
    Derive a 32-byte AES key from a password using Argon2id.
    Returns: (key_bytes, salt_bytes).
    Caller must persist the salt alongside the ciphertext.
    """
    if salt is None:
        salt = secrets.token_bytes(16)
    # Use raw mode to get raw bytes instead of encoded hash string
    from argon2.low_level import Type, hash_secret_raw

    key = hash_secret_raw(
        secret=password.encode(),
        salt=salt,
        time_cost=3,
        memory_cost=65536,
        parallelism=4,
        hash_len=32,
        type=Type.ID,
    )
    return key, salt


# ── AES-256-GCM ───────────────────────────────────────────────────────────────

def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    Encrypt plaintext with AES-256-GCM.
    Output format: nonce (12 bytes) || ciphertext+tag.
    The nonce is randomly generated per-call (no counter reuse).
    """
    if len(key) != 32:
        raise ValueError(f"key must be 32 bytes, got {len(key)}")
    nonce = os.urandom(_GCM_NONCE_SIZE)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    return nonce + ciphertext


def decrypt(ciphertext_with_nonce: bytes, key: bytes) -> bytes:
    """
    Decrypt AES-256-GCM ciphertext produced by encrypt().
    Raises ValueError on authentication failure (tampered data).
    """
    if len(key) != 32:
        raise ValueError(f"key must be 32 bytes, got {len(key)}")
    if len(ciphertext_with_nonce) < _GCM_NONCE_SIZE + 16:  # nonce + min tag
        raise ValueError("ciphertext too short")
    nonce = ciphertext_with_nonce[:_GCM_NONCE_SIZE]
    ciphertext = ciphertext_with_nonce[_GCM_NONCE_SIZE:]
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)


def encrypt_str(text: str, key: bytes) -> bytes:
    """Convenience wrapper: encrypt a UTF-8 string."""
    return encrypt(text.encode("utf-8"), key)


def decrypt_str(ciphertext_with_nonce: bytes, key: bytes) -> str:
    """Convenience wrapper: decrypt to a UTF-8 string."""
    return decrypt(ciphertext_with_nonce, key).decode("utf-8")


# ── HMAC Chain (Audit Log) ────────────────────────────────────────────────────

import hashlib
import hmac


def hmac_sha256(key: bytes, data: bytes) -> str:
    """Compute HMAC-SHA256. Returns lowercase hex string."""
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def sha256_hex(data: bytes) -> str:
    """Compute SHA-256 hash. Returns lowercase hex string."""
    return hashlib.sha256(data).hexdigest()


def compute_chain_hmac(prev_hmac: str, payload_hash: str, secret: bytes) -> str:
    """
    Compute the next link in the HMAC audit chain.
    chain_hmac = HMAC-SHA256(secret, prev_hmac || payload_hash)
    """
    data = (prev_hmac + payload_hash).encode("utf-8")
    return hmac_sha256(secret, data)
