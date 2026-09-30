"""Passwords (scrypt), API-key encryption at rest (Fernet), tokens, and redaction."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_N, _R, _P, _DKLEN = 2 ** 14, 8, 1, 64


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(_N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        # Same work as a real check, so response time doesn't reveal whether an email exists.
        hashlib.scrypt(password.encode(), salt=b"0" * 16, n=_N, r=_R, p=_P, dklen=_DKLEN)
        return False
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def fernet_key(secret_key: str, encryption_key: str | None = None) -> bytes:
    """ENCRYPTION_KEY if given (a Fernet key), else one derived from SECRET_KEY with HKDF-SHA256."""
    if encryption_key:
        key = encryption_key.encode()
        Fernet(key)  # raises if malformed
        return key
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"timeline-keys-v1",
               info=b"api key encryption").derive(secret_key.encode())
    return base64.urlsafe_b64encode(raw)


class Box:
    """Encrypts and decrypts one user's API keys."""

    def __init__(self, key: bytes):
        self._f = Fernet(key)

    def seal(self, value: str) -> str:
        return self._f.encrypt(value.encode()).decode()

    def open(self, token: str) -> str | None:
        try:
            return self._f.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None  # e.g. SECRET_KEY changed; the user re-enters the key


def recap_token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


def redact(text: str, secrets_: list[str]) -> str:
    """Remove any of the user's key values from text before it's stored or shown."""
    for s in secrets_:
        if s and len(s) >= 6:
            text = text.replace(s, "[redacted]")
    return text
