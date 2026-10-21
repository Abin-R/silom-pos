"""Reversible storage for the 4-digit till PIN, so an admin can read a
staff member's current PIN back in the backoffice.

``pin_hash`` stays the thing the till checks; this is a second, encrypted copy
kept only for display. The key is derived from ``SECRET_KEY`` (no extra
dependency, nothing new in ``.env``): a random nonce feeds an HMAC keystream
that is XORed with the PIN, and the result is signed so a tampered or
foreign-key value reads back as "unknown" rather than wrong digits.

Rotating ``SECRET_KEY`` makes every stored copy unreadable — the till keeps
working off the hash, and the next PIN reset writes a fresh copy.

Till PINs only. The backoffice password is never stored this way.
"""
from __future__ import annotations

import secrets
from typing import Optional

from django.core import signing
from django.utils.crypto import salted_hmac

_SALT = "bravepos.pin_vault"


def _keystream(nonce: str, length: int) -> bytes:
    return salted_hmac(_SALT, nonce).digest()[:length]


def seal(pin: str) -> str:
    nonce = secrets.token_hex(16)
    raw = pin.encode()
    cipher = bytes(a ^ b for a, b in zip(raw, _keystream(nonce, len(raw))))
    return signing.dumps({"n": nonce, "c": cipher.hex()}, salt=_SALT)


def unseal(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        data = signing.loads(token, salt=_SALT)
        cipher = bytes.fromhex(data["c"])
        stream = _keystream(data["n"], len(cipher))
        return bytes(a ^ b for a, b in zip(cipher, stream)).decode()
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None
