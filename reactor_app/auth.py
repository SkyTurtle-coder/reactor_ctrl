"""Session-based authentication for reactor_ctrl.

A single shared password (stored as a Werkzeug hash in APP_PASSWORD_HASH)
protects every page.  The API routes at /api/* are exempt because they
carry their own Bearer-token auth.

Brute-force protection: 5 consecutive failures from the same IP address
trigger a 15-minute lockout.  The counter resets on server restart; for
this single-user, lab-grade application that is an acceptable trade-off
over the complexity of a persistent lockout store.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash


auth_bp = Blueprint("auth", __name__)

# ---------------------------------------------------------------------------
# In-memory brute-force tracker
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_failures: dict[str, list[float]] = {}   # ip → list of monotonic timestamps
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 15 * 60               # 15 minutes


def _client_ip() -> str:
    """Best-effort client IP, works behind a single nginx reverse proxy."""
    return (
        request.headers.get("X-Real-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def _is_locked_out(ip: str) -> bool:
    with _lock:
        now = time.monotonic()
        recent = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_SECONDS]
        _failures[ip] = recent
        return len(recent) >= _MAX_ATTEMPTS


def _remaining_lockout_seconds(ip: str) -> int:
    """How many seconds remain in the current lockout (0 if not locked)."""
    with _lock:
        now = time.monotonic()
        recent = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_SECONDS]
        if len(recent) < _MAX_ATTEMPTS:
            return 0
        oldest = min(recent)
        remaining = _LOCKOUT_SECONDS - (now - oldest)
        return max(0, int(remaining))


def _record_failure(ip: str) -> None:
    with _lock:
        now = time.monotonic()
        recent = [t for t in _failures.get(ip, []) if now - t < _LOCKOUT_SECONDS]
        recent.append(now)
        _failures[ip] = recent


def _clear_failures(ip: str) -> None:
    with _lock:
        _failures.pop(ip, None)


# ---------------------------------------------------------------------------
# before_request guard — applied globally from create_app
# ---------------------------------------------------------------------------

# Endpoints that must never require login.
_EXEMPT_ENDPOINTS: frozenset[str] = frozenset({
    "auth.login",
    "auth.logout",
    "static",
    "web.health",
    "web.health_db",
})

# Path prefixes that are exempt (API carries its own Bearer-token auth).
_EXEMPT_PATH_PREFIXES: tuple[str, ...] = ("/api/",)


def require_login() -> object | None:
    """Global before_request hook.  Redirects to /login if not authenticated."""
    # Exempt by endpoint name
    if request.endpoint in _EXEMPT_ENDPOINTS:
        return None

    # Exempt by URL prefix
    for prefix in _EXEMPT_PATH_PREFIXES:
        if request.path.startswith(prefix):
            return None

    # Static files served directly (Flask's built-in static endpoint)
    if request.endpoint == "static":
        return None

    if not session.get("authenticated"):
        next_url = request.full_path.rstrip("?")
        return redirect(url_for("auth.login", next=next_url))

    return None


# ---------------------------------------------------------------------------
# Login / logout views
# ---------------------------------------------------------------------------

@auth_bp.get("/login")
@auth_bp.post("/login")
def login():
    """Show or process the login form."""
    if session.get("authenticated"):
        return redirect(_safe_next_url())

    error: str | None = None
    ip = _client_ip()
    locked = _is_locked_out(ip)
    lockout_minutes = (_remaining_lockout_seconds(ip) + 59) // 60  # round up

    if request.method == "POST":
        # Validate CSRF token first
        submitted_csrf = request.form.get("csrf_token", "")
        expected_csrf = session.get("_login_csrf", "")
        if not submitted_csrf or not secrets.compare_digest(submitted_csrf, expected_csrf):
            error = "Invalid form submission. Please try again."
        elif locked:
            error = (
                f"Too many failed login attempts. "
                f"Please wait {lockout_minutes} minute(s) and try again."
            )
        else:
            password = request.form.get("password", "")
            password_hash = current_app.config.get("APP_PASSWORD_HASH", "")

            if not password_hash:
                error = (
                    "No password has been configured on this server. "
                    "Set APP_PASSWORD_HASH in the .env file."
                )
            elif password_hash and check_password_hash(password_hash, password):
                _clear_failures(ip)
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                return redirect(_safe_next_url())
            else:
                _record_failure(ip)
                locked = _is_locked_out(ip)
                lockout_minutes = (_remaining_lockout_seconds(ip) + 59) // 60
                if locked:
                    error = (
                        f"Too many failed login attempts. "
                        f"Please wait {lockout_minutes} minute(s) and try again."
                    )
                else:
                    remaining = _MAX_ATTEMPTS - len(
                        [t for t in _failures.get(ip, [])
                         if time.monotonic() - t < _LOCKOUT_SECONDS]
                    )
                    error = (
                        f"Incorrect password. "
                        f"{remaining} attempt(s) remaining before lockout."
                    )

    # Refresh CSRF token on every GET (and after a failed POST)
    if request.method == "GET" or error:
        session["_login_csrf"] = secrets.token_hex(32)

    return render_template(
        "login.html",
        error=error,
        locked=locked,
        lockout_minutes=lockout_minutes,
        csrf_token=session.get("_login_csrf", ""),
        next_url=request.args.get("next", ""),
    )


@auth_bp.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_next_url() -> str:
    """Return the 'next' URL only if it is a safe relative path on this host."""
    next_url = request.args.get("next") or request.form.get("next", "")
    if next_url:
        parsed = urlsplit(next_url)
        # Reject anything with a scheme or netloc (prevents open redirect)
        if not parsed.scheme and not parsed.netloc and next_url.startswith("/"):
            # Also reject redirecting back to the login page itself
            if not next_url.startswith(url_for("auth.login")):
                return next_url
    return url_for("web.index")
