"""
The session certificate this node presents on the direct path.

QUIC needs an X.509 certificate at the listening end, and here it is pinned
rather than trusted: a peer receives this certificate's DER inside an
authenticated Reticulum message and makes it the sole trust root of the
connection it opens. The certificate names nothing and proves nothing on its
own; what proves the identity behind it is the HELLO that follows, which signs
over the fingerprint of both certificates.

It is persisted so a peer that pinned it once can reconnect without a new
exchange, and re-minted when the file cannot be read or the certificate has
run out: an expired certificate fails the peer's verification, which looks
exactly like an impostor.
"""

import datetime
import hashlib
from pathlib import Path

import RNS
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from trenchchat.core.fileutils import atomic_write_bytes, secure_file

CERT_FILE_NAME = "session_cert.pem"
CERT_COMMON_NAME = "trenchchat-session"
CERT_VALIDITY_DAYS = 365

# Re-mint this long before expiry, so a long-running node never presents a
# certificate that expires mid-session.
CERT_RENEW_BEFORE_DAYS = 7

_CERT_MARKER = b"-----BEGIN CERTIFICATE-----"


def fingerprint_for(der: bytes) -> bytes:
    """The SHA-256 of a certificate's DER encoding, which a HELLO signs over."""
    return hashlib.sha256(der).digest()


def pem_for(der: bytes) -> bytes:
    """Re-encode a DER certificate as PEM, the only form aioquic's cadata takes."""
    return x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM)


class SessionCertificate:
    """One node's session certificate and the key behind it."""

    def __init__(self, private_key, certificate: x509.Certificate):
        self._private_key = private_key
        self._certificate = certificate
        self._der = certificate.public_bytes(serialization.Encoding.DER)
        self._fingerprint = fingerprint_for(self._der)

    @classmethod
    def mint(cls) -> "SessionCertificate":
        """Mint a fresh self-signed Ed25519 certificate with no name to rely on."""
        key = ed25519.Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CERT_COMMON_NAME)])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                           critical=True)
            .sign(key, None)
        )
        return cls(key, certificate)

    @classmethod
    def load_or_create(cls, data_dir: Path) -> "SessionCertificate":
        """The node's certificate, minted and persisted on first use.

        A file that cannot be read, or holds a certificate close to expiry, is
        replaced: peers that pinned the old one will be handed the new one the
        next time they are offered a session.
        """
        path = Path(data_dir) / CERT_FILE_NAME
        existing = cls._load(path)
        if existing is not None:
            return existing
        minted = cls.mint()
        minted.save(path)
        RNS.log(f"TrenchChat [ip]: minted a session certificate at {path}",
                RNS.LOG_NOTICE)
        return minted

    @classmethod
    def _load(cls, path: Path) -> "SessionCertificate | None":
        if not path.exists():
            return None
        try:
            blob = path.read_bytes()
            index = blob.index(_CERT_MARKER)
            private_key = serialization.load_pem_private_key(
                blob[:index], password=None)
            certificate = x509.load_pem_x509_certificate(blob[index:])
        except (OSError, ValueError, TypeError) as e:
            RNS.log(f"TrenchChat [ip]: re-minting an unreadable session "
                    f"certificate at {path}: {e}", RNS.LOG_WARNING)
            return None
        secure_file(path)
        expires = certificate.not_valid_after_utc
        if expires - datetime.timedelta(days=CERT_RENEW_BEFORE_DAYS) <= \
                datetime.datetime.now(datetime.timezone.utc):
            RNS.log("TrenchChat [ip]: re-minting a session certificate that is "
                    "out of time", RNS.LOG_NOTICE)
            return None
        return cls(private_key, certificate)

    def save(self, path: Path) -> None:
        """Persist the key and certificate, owner-readable only, atomically."""
        key_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        atomic_write_bytes(Path(path), key_pem + self.pem)

    @property
    def der(self) -> bytes:
        """The certificate's DER encoding, which is what travels to a peer."""
        return self._der

    @property
    def pem(self) -> bytes:
        """The certificate's PEM encoding."""
        return self._certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def fingerprint(self) -> bytes:
        """SHA-256 over the DER, the value a HELLO signature covers."""
        return self._fingerprint

    @property
    def certificate(self) -> x509.Certificate:
        """The certificate itself, for a QUIC configuration."""
        return self._certificate

    @property
    def private_key(self):
        """The certificate's private key, for a QUIC configuration."""
        return self._private_key
