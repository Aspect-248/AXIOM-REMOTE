"""Time-based one-time passwords (RFC 6238, the 6-digit codes authenticator
apps show). Stdlib only."""

import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code_for_counter(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10**DIGITS:0{DIGITS}d}"


def code_at(secret: str, now: "float | None" = None) -> str:
    now = time.time() if now is None else now
    return _code_for_counter(secret, int(now // STEP_SECONDS))


def verify(secret: str, code: str, now: "float | None" = None, after: int = 0, window: int = 1):
    """Returns the matched time-step counter, or None. Pass the previously
    accepted counter as `after` so a code can only ever be used once. `window`
    allows that many steps of clock drift either way."""
    code = re.sub(r"\s", "", code or "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = time.time() if now is None else now
    current = int(now // STEP_SECONDS)
    for counter in range(current - window, current + window + 1):
        if counter > after and hmac.compare_digest(_code_for_counter(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account)}"
        f"?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    )
