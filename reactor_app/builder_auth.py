from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


REACTOR_BUILDER_WRITE_SCOPE = "reactor_builder:write"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def create_scoped_token(secret_key: str, *, scope: str, ttl_seconds: int) -> str:
    now = int(time.time())
    payload = {
        "scope": scope,
        "iat": now,
        "exp": now + max(int(ttl_seconds), 1),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_segment = _b64url_encode(payload_bytes)
    signature = hmac.new(secret_key.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload_segment}.{signature}"


def verify_scoped_token(token: str, *, secret_key: str, expected_scope: str, now: int | None = None) -> bool:
    try:
        payload_segment, provided_signature = token.split(".", 1)
    except ValueError:
        return False

    expected_signature = hmac.new(
        secret_key.encode("utf-8"),
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(provided_signature, expected_signature):
        return False

    try:
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, json.JSONDecodeError):
        return False

    if not isinstance(payload, dict):
        return False
    if payload.get("scope") != expected_scope:
        return False

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        return False

    current_time = int(time.time()) if now is None else int(now)
    return expires_at >= current_time
