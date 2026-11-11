"""Time-based one-time passwords (RFC 6238 over RFC 4226): SHA-1, 6 digits, 30-second steps (D-095).

Pure: the caller passes the time. `verify` returns the time step a code belongs to, so the caller can refuse a
step it has already accepted (a code is good for one sign-in).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from urllib.parse import quote

__all__ = ['DIGITS', 'STEP', 'WINDOW', 'code_at', 'decode_secret', 'hotp', 'uri', 'verify']

STEP = 30       # seconds per code
DIGITS = 6
WINDOW = 1      # steps accepted either side of the current one, for clocks that are a little off


def decode_secret(secret: str) -> bytes:
    """The key bytes of a base32 secret as authenticator apps show it (spaces and lower case allowed)."""
    s = ''.join(secret.split()).upper()
    return base64.b32decode(s + '=' * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS) -> str:
    mac = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    at = mac[-1] & 0x0F
    number = struct.unpack('>I', mac[at:at + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


def code_at(secret: str, unix_time: float) -> str:
    """The code an authenticator shows at this time."""
    return hotp(decode_secret(secret), int(unix_time) // STEP)


def verify(secret: str, code: str, unix_time: float, *, after_step: int | None = None,
           window: int = WINDOW) -> int | None:
    """The time step `code` is valid for (now, or up to `window` steps either side), else None. Steps at or before
    `after_step` (the last one accepted for this user) are never accepted again. Compared in constant time."""
    given = ''.join(code.split())
    if len(given) != DIGITS or not (given.isascii() and given.isdigit()):
        return None
    key = decode_secret(secret)
    now = int(unix_time) // STEP
    found = None
    for step in range(now - window, now + window + 1):
        if step < 0 or (after_step is not None and step <= after_step):
            continue
        if hmac.compare_digest(hotp(key, step), given) and found is None:
            found = step
    return found


def uri(secret: str, account: str, issuer: str) -> str:
    """The otpauth:// link authenticator apps read (as text, a link or a QR code)."""
    label = quote('%s:%s' % (issuer, account), safe='')
    return 'otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d' % (
        label, ''.join(secret.split()).upper(), quote(issuer, safe=''), DIGITS, STEP)
