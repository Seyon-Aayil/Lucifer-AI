"""Unit tests for master.core.auth.operator."""

from __future__ import annotations

import pytest
from argon2 import PasswordHasher

from master.core.auth.operator import (
    InvalidCredentialsError,
    is_operator_login_configured,
    login,
    validate_operator_session,
    verify_password,
)
from master.core.config import get_settings
from master.core.exceptions import InvalidTokenError, TokenExpiredError


@pytest.fixture
def operator_creds(monkeypatch):
    settings = get_settings()
    hasher = PasswordHasher()
    plain = "supersecret"
    monkeypatch.setattr(settings, "operator_username", "alice")
    monkeypatch.setattr(settings, "operator_password_hash", hasher.hash(plain))
    monkeypatch.setattr(settings, "operator_session_ttl_seconds", 3600)
    return "alice", plain


@pytest.fixture
def no_operator(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "operator_username", "")
    monkeypatch.setattr(settings, "operator_password_hash", "")


def test_is_configured_reflects_settings(operator_creds):
    assert is_operator_login_configured() is True


def test_is_configured_false_when_unset(no_operator):
    assert is_operator_login_configured() is False


def test_verify_password_returns_true_on_match(operator_creds):
    _, plain = operator_creds
    assert verify_password(plain) is True


def test_verify_password_false_on_mismatch(operator_creds):
    assert verify_password("wrong") is False


def test_verify_password_false_when_unconfigured(no_operator):
    assert verify_password("anything") is False


def test_login_returns_jwt(operator_creds):
    user, plain = operator_creds
    token = login(user, plain)
    assert isinstance(token, str)
    payload = validate_operator_session(token)
    assert payload["sub"] == user
    assert payload["type"] == "operator_session"


def test_login_rejects_wrong_username(operator_creds):
    _, plain = operator_creds
    with pytest.raises(InvalidCredentialsError):
        login("attacker", plain)


def test_login_rejects_wrong_password(operator_creds):
    user, _ = operator_creds
    with pytest.raises(InvalidCredentialsError):
        login(user, "nope")


def test_login_rejects_when_unconfigured(no_operator):
    with pytest.raises(InvalidCredentialsError):
        login("alice", "anything")


def test_validate_rejects_non_operator_jwt():
    # An access JWT minted for a device should NOT pass operator validation.
    from master.core.auth.jwt import create_access_token

    token = create_access_token("device-x")
    with pytest.raises(InvalidTokenError):
        validate_operator_session(token)


def test_validate_rejects_garbage():
    with pytest.raises(InvalidTokenError):
        validate_operator_session("not.a.token")


def test_validate_rejects_expired(operator_creds, monkeypatch):
    # Force a 0-second TTL so the token is born expired.
    settings = get_settings()
    monkeypatch.setattr(settings, "operator_session_ttl_seconds", -1)
    user, plain = operator_creds
    token = login(user, plain)
    with pytest.raises(TokenExpiredError):
        validate_operator_session(token)
