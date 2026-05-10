"""Unit tests for master.core.auth.cert_mint."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from master.core.auth.cert_mint import mint_client_credentials
from master.core.config import get_settings


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def dev_ca(tmp_path: Path, monkeypatch):
    """Spin up a throw-away CA on disk and point settings at it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "lucifer-test-ca")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    cert_path = tmp_path / "ca.crt"
    key_path = tmp_path / "ca.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    settings = get_settings()
    monkeypatch.setattr(settings, "pairing_ca_cert_path", str(cert_path))
    monkeypatch.setattr(settings, "pairing_ca_key_path", str(key_path))
    monkeypatch.setattr(settings, "pairing_cert_validity_days", 5)
    return cert_path, key_path


# ── Tests ────────────────────────────────────────────────────────────────────


def test_missing_ca_raises_filenotfound(tmp_path: Path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "pairing_ca_cert_path", str(tmp_path / "nope.crt"))
    monkeypatch.setattr(settings, "pairing_ca_key_path", str(tmp_path / "nope.key"))
    with pytest.raises(FileNotFoundError):
        mint_client_credentials("device-x")


def test_minted_cert_has_expected_subject(dev_ca):
    creds = mint_client_credentials("device-mac-01")
    cert = x509.load_pem_x509_certificate(creds.client_cert_pem)
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "lucifer-device-device-mac-01"
    assert creds.common_name == "lucifer-device-device-mac-01"


def test_minted_cert_chain_validates(dev_ca):
    cert_path, _ = dev_ca
    creds = mint_client_credentials("device-mac-01")
    ca = x509.load_pem_x509_certificate(cert_path.read_bytes())
    cert = x509.load_pem_x509_certificate(creds.client_cert_pem)
    # Cert was issued by the CA (subject match) and signature verifies.
    assert cert.issuer == ca.subject
    ca.public_key().verify(
        cert.signature,
        cert.tbs_certificate_bytes,
        # cryptography's `verify` for RSA needs padding + hash explicitly.
        # We use the same algo cert_mint chose.
        # Importing inside the test to keep top-level imports tidy.
        __import_padding(),
        cert.signature_hash_algorithm,
    )


def __import_padding():
    from cryptography.hazmat.primitives.asymmetric import padding

    return padding.PKCS1v15()


def test_minted_cert_has_client_auth_eku(dev_ca):
    creds = mint_client_credentials("device-mac-01")
    cert = x509.load_pem_x509_certificate(creds.client_cert_pem)
    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    assert ExtendedKeyUsageOID.CLIENT_AUTH in eku.value


def test_minted_cert_has_basic_constraints_ca_false(dev_ca):
    creds = mint_client_credentials("device-mac-01")
    cert = x509.load_pem_x509_certificate(creds.client_cert_pem)
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.value.ca is False


def test_minted_cert_validity_window_respects_settings(dev_ca):
    creds = mint_client_credentials("device-mac-01")
    cert = x509.load_pem_x509_certificate(creds.client_cert_pem)
    days = (cert.not_valid_after_utc - cert.not_valid_before_utc).days
    # validity_days override fixture set to 5 — give a one-day cushion for the
    # not-before backdating in cert_mint.
    assert 5 <= days <= 6


def test_minted_key_is_pem_pkcs8(dev_ca):
    creds = mint_client_credentials("device-mac-01")
    key = serialization.load_pem_private_key(creds.client_key_pem, password=None)
    assert isinstance(key, rsa.RSAPrivateKey)


def test_unique_serial_numbers(dev_ca):
    a = mint_client_credentials("device-a")
    b = mint_client_credentials("device-b")
    assert a.serial_number != b.serial_number
