"""Ed25519 only: cryptography when present, a fixed OpenSSL 3 CLI otherwise."""
from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:
    serialization = None

_run_crypto_process = subprocess.run
OPENSSL = "/opt/homebrew/bin/openssl"
DER_PREFIX = bytes.fromhex("302a300506032b6570032100")


def _openssl(*args: str, input: bytes | None = None) -> bytes:
    result = _run_crypto_process([OPENSSL, *args], input=input, capture_output=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=30)
    if result.returncode:
        # Never include stderr: it can contain private material or file contents.
        raise ValueError("Ed25519 OpenSSL operation failed")
    return result.stdout


def _write_private_file(path: pathlib.Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(data)


def generate_private_der() -> bytes:
    if serialization is not None:
        return Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.DER,
                    serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return _openssl("genpkey", "-algorithm", "ED25519", "-outform", "DER")


def public_from_private_file(path: pathlib.Path) -> bytes:
    if serialization is not None:
        private = serialization.load_der_private_key(path.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError("Keychain item is not an Ed25519 private key")
        public = private.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
    else:
        public = _openssl("pkey", "-inform", "DER", "-in", str(path), "-pubout", "-outform", "DER")
    if len(public) != 44 or public[:12] != DER_PREFIX:
        raise ValueError("key is not Ed25519")
    return public


def public_from_private_der(private: bytes) -> bytes:
    with tempfile.TemporaryDirectory(prefix="ge16-ed25519-") as directory:
        path = pathlib.Path(directory) / "T"
        _write_private_file(path, private)
        return public_from_private_file(path)


def sign_private_file(path: pathlib.Path, payload: bytes) -> bytes:
    if serialization is not None:
        private = serialization.load_der_private_key(path.read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError("key is not an Ed25519 private key")
        return private.sign(payload)
    with tempfile.TemporaryDirectory(prefix="ge16-ed25519-payload-") as directory:
        message = pathlib.Path(directory) / "payload"
        _write_private_file(message, payload)
        return _openssl("pkeyutl", "-sign", "-rawin", "-inkey", str(path),
                        "-keyform", "DER", "-in", str(message))


def verify(public: bytes, signature: bytes, payload: bytes) -> None:
    if len(public) != 44 or public[:12] != DER_PREFIX or len(signature) != 64:
        raise ValueError("invalid Ed25519 key or signature length")
    if serialization is not None:
        try:
            key = serialization.load_der_public_key(public)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("key is not Ed25519")
            key.verify(signature, payload)
        except InvalidSignature as error:
            raise ValueError("scheduled fire signature mismatch") from error
        return
    with tempfile.TemporaryDirectory(prefix="ge16-ed25519-verify-") as directory:
        root = pathlib.Path(directory)
        for name, data in (("public", public), ("signature", signature), ("payload", payload)):
            _write_private_file(root / name, data)
        _openssl("pkeyutl", "-verify", "-pubin", "-rawin", "-inkey", str(root / "public"),
                 "-keyform", "DER", "-sigfile", str(root / "signature"), "-in", str(root / "payload"))
