"""RFC 6238 TOTP verification, stdlib only.

Deliberately not a dependency: TOTP is ~20 lines of HMAC and adding a package
to the image for it would be a poor trade. Generation is not implemented --
1Password creates the secret and the codes; this only ever verifies.
"""

# Ported from anki-mcp's oauth package, which solved this first. The two are
# near-identical by design: same flow, same threat model, one user each. Fix
# bugs in both.

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time

# RFC 6238 defaults, which is what 1Password (and every authenticator) uses.
TIME_STEP_SECONDS = 30
DIGITS = 6

# Accept the immediately-neighbouring steps to tolerate clock skew between the
# server and the phone. One step either way is the usual compromise: it widens the
# window to 90s total rather than leaving a valid code rejected on a boundary.
DEFAULT_SKEW_STEPS = 1


class InvalidTOTPSecret(ValueError):
    """The configured TOTP secret is not valid base32."""


def normalise_secret(secret: str) -> bytes:
    """Decode a base32 TOTP secret as shown by 1Password.

    Accepts the spaced, lowercase, unpadded forms people actually paste.
    """
    cleaned = secret.strip().replace(" ", "").replace("-", "").upper()
    # base32 needs the length to be a multiple of 8; authenticators habitually
    # strip the '=' padding.
    padding = (-len(cleaned)) % 8
    try:
        return base64.b32decode(cleaned + "=" * padding, casefold=True)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a config error
        raise InvalidTOTPSecret(
            "OBSIDIAN_MCP_TOTP_SECRET is not valid base32. Copy the 'setup key' / "
            "secret from the 1Password one-time-password field, not the "
            "6-digit code."
        ) from exc


def _code_for_counter(key: bytes, counter: int) -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    truncated = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(truncated % (10**DIGITS)).zfill(DIGITS)


def verify(
    secret: bytes,
    code: str,
    *,
    now: float | None = None,
    skew_steps: int = DEFAULT_SKEW_STEPS,
) -> int | None:
    """Check a submitted code. Returns the matching counter, or None.

    The counter is returned so the caller can reject replays: a TOTP code stays
    valid for its whole window, so without remembering the last accepted
    counter an intercepted code could be used again within ~90 seconds.
    """
    submitted = code.strip().replace(" ", "")
    if not submitted.isdigit() or len(submitted) != DIGITS:
        return None

    counter = int((now if now is not None else time.time()) // TIME_STEP_SECONDS)
    for offset in range(-skew_steps, skew_steps + 1):
        candidate = counter + offset
        # compare_digest to keep the comparison constant-time; the loop itself
        # leaks only which window matched, which is not secret.
        if hmac.compare_digest(_code_for_counter(secret, candidate), submitted):
            return candidate
    return None
