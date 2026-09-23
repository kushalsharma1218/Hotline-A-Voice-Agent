import hashlib
import hmac
import time


class InvalidSignature(Exception):
    """Missing, malformed, mismatched or out-of-window ElevenLabs-Signature header."""


def verify_elevenlabs_signature(
    raw: bytes, header: str, secret: str, tolerance_s: int = 1800, now: float | None = None
) -> None:
    """Verify `ElevenLabs-Signature: t=<unix_ts>,v0=<hex>`, v0 = HMAC_SHA256(secret, f"{t}.{raw}")."""
    if not secret:
        raise InvalidSignature("webhook secret not configured")
    timestamp: str | None = None
    candidates: list[str] = []
    for part in (header or "").split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key.strip() == "t":
            timestamp = value.strip()
        elif key.strip() == "v0":
            candidates.append(value.strip().lower())
    if not timestamp or not candidates:
        raise InvalidSignature("malformed signature header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise InvalidSignature("non-integer timestamp") from exc
    current = time.time() if now is None else now
    if abs(current - ts) > tolerance_s:
        raise InvalidSignature("timestamp outside tolerance")

    expected = hmac.new(
        secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256
    ).hexdigest()
    # Check every candidate (no short-circuit) so timing doesn't reveal which matched.
    matched = False
    for candidate in candidates:
        matched |= hmac.compare_digest(expected, candidate)
    if not matched:
        raise InvalidSignature("signature mismatch")
