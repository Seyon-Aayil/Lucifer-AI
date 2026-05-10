"""
master.core.auth.cert_mint
===========================
Mint per-device client certificates signed by the local Lucifer dev CA.

The CA key + cert live at `settings.pairing_ca_{cert,key}_path`. For dev
they are produced by `make dev-certs`; in production an operator-managed
intermediate CA should be wired up via the same paths.

Outputs PEM-encoded client cert + key bytes — never the CA private key.
The minting service does not persist the device key; the edge device
stores it (e.g. macOS Keychain) immediately after the pairing call.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from master.core.config import get_settings
from master.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class MintedClientCredentials:
    """A freshly-issued client cert + key + CA chain, all PEM-encoded."""

    client_cert_pem: bytes
    client_key_pem: bytes
    ca_cert_pem: bytes
    serial_number: int
    not_after: dt.datetime
    common_name: str


def mint_client_credentials(
    device_id: str,
    rsa_bits: int = 2048,
) -> MintedClientCredentials:
    """
    Mint a fresh keypair + cert for `device_id`. Caller is responsible for
    transmitting the credentials to the edge device over the existing TLS
    pairing channel and for revoking them on device loss.
    """
    settings = get_settings()
    ca_cert_path = Path(settings.pairing_ca_cert_path)
    ca_key_path = Path(settings.pairing_ca_key_path)

    if not ca_cert_path.exists() or not ca_key_path.exists():
        raise FileNotFoundError(
            f"Pairing CA missing: cert={ca_cert_path} key={ca_key_path}. "
            "Run `make dev-certs` (dev) or set pairing_ca_*_path to your "
            "production intermediate CA."
        )

    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    if not isinstance(ca_key, rsa.RSAPrivateKey):
        raise TypeError("CA key must be RSA — found a non-RSA key in pairing_ca_key_path")

    # Generate a new device key and CSR-equivalent in-memory.
    device_key = rsa.generate_private_key(public_exponent=65537, key_size=rsa_bits)

    cn = f"lucifer-device-{device_id}"
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    serial_number = x509.random_serial_number()
    now = dt.datetime.now(dt.UTC)
    not_after = now + dt.timedelta(days=settings.pairing_cert_validity_days)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(device_key.public_key())
        .serial_number(serial_number)
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(cn)]),
            critical=False,
        )
    )
    cert = builder.sign(private_key=ca_key, algorithm=hashes.SHA256())

    client_cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    client_key_pem = device_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM)

    log.info(
        "cert_mint.issued",
        device_id=device_id,
        serial=hex(serial_number),
        not_after=not_after.isoformat(),
    )
    return MintedClientCredentials(
        client_cert_pem=client_cert_pem,
        client_key_pem=client_key_pem,
        ca_cert_pem=ca_cert_pem,
        serial_number=serial_number,
        not_after=not_after,
        common_name=cn,
    )
