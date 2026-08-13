"""
App security helpers — sessions, CSRF, login rate limits, password policy.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ── Session secret ──────────────────────────────────────────────
_WEAK_SECRETS = {
    "",
    "change-me-please-a-long-random-string",
    "replace-with-a-long-random-string",
    "secret",
    "dev",
}


def require_strong_session_secret(raw: Optional[str]) -> str:
    secret = (raw or "").strip()
    if secret in _WEAK_SECRETS or len(secret) < 32:
        raise RuntimeError(
            "SESSION_SECRET is missing or too weak. "
            'Generate one with: python -c "import secrets;print(secrets.token_urlsafe(48))"'
        )
    return secret


# ── Password policy ─────────────────────────────────────────────
_PASSWORD_MIN = 10


def validate_password_strength(password: str) -> None:
    pw = password or ""
    if len(pw) < _PASSWORD_MIN:
        raise ValueError(f"Password must be at least {_PASSWORD_MIN} characters")
    if len(pw) > 128:
        raise ValueError("Password is too long")
    classes = sum(
        [
            bool(re.search(r"[a-z]", pw)),
            bool(re.search(r"[A-Z]", pw)),
            bool(re.search(r"[0-9]", pw)),
            bool(re.search(r"[^A-Za-z0-9]", pw)),
        ]
    )
    if classes < 3:
        raise ValueError(
            "Password needs at least 3 of: lowercase, uppercase, number, symbol"
        )


# ── Login rate limit / lockout ──────────────────────────────────
class LoginGuard:
    """Per-IP + per-account lockout after failed password attempts."""

    def __init__(
        self,
        *,
        max_fails: int = 5,
        window_sec: int = 900,
        lockout_sec: int = 900,
    ):
        self.max_fails = max_fails
        self.window_sec = window_sec
        self.lockout_sec = lockout_sec
        self._lock = threading.Lock()
        # key -> list[fail_timestamps]
        self._fails: dict[str, list[float]] = defaultdict(list)
        # key -> locked_until
        self._locked: dict[str, float] = {}

    def _prune(self, key: str, now: float) -> None:
        cutoff = now - self.window_sec
        self._fails[key] = [t for t in self._fails[key] if t >= cutoff]
        until = self._locked.get(key)
        if until is not None and until <= now:
            self._locked.pop(key, None)

    def check(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                if not key:
                    continue
                self._prune(key, now)
                until = self._locked.get(key)
                if until and until > now:
                    wait = int(until - now)
                    raise HTTPException(
                        429,
                        f"Too many failed sign-in attempts. Try again in {wait}s.",
                    )

    def record_failure(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                if not key:
                    continue
                self._prune(key, now)
                self._fails[key].append(now)
                if len(self._fails[key]) >= self.max_fails:
                    self._locked[key] = now + self.lockout_sec
                    self._fails[key].clear()

    def record_success(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                if not key:
                    continue
                self._fails.pop(key, None)
                self._locked.pop(key, None)


login_guard = LoginGuard()


def client_ip(request: Request) -> str:
    # Prefer direct peer; do not trust X-Forwarded-For unless behind a known proxy.
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


# ── CSRF + session helpers ──────────────────────────────────────
CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_CSRF_EXEMPT_PATHS = frozenset({"/healthz"})


def _new_csrf() -> str:
    return secrets.token_urlsafe(32)


def ua_fingerprint(request: Request) -> str:
    ua = (request.headers.get("user-agent") or "")[:300]
    return hashlib.sha256(ua.encode("utf-8", errors="ignore")).hexdigest()[:32]


def ensure_csrf(request: Request) -> str:
    token = request.session.get("csrf")
    if not token or not isinstance(token, str) or len(token) < 16:
        token = _new_csrf()
        request.session["csrf"] = token
    return token


def establish_session(request: Request, *, email: str, name: str) -> None:
    """Clear any prior session (anti-fixation) and bind a fresh login."""
    request.session.clear()
    request.session["user"] = {"email": email.lower().strip(), "name": name}
    request.session["csrf"] = _new_csrf()
    request.session["ua_fp"] = ua_fingerprint(request)
    request.session["login_at"] = int(time.time())


def rotate_csrf(request: Request) -> str:
    token = _new_csrf()
    request.session["csrf"] = token
    return token


def verify_session_binding(request: Request) -> None:
    """Reject stolen cookies reused from a different browser fingerprint."""
    expected = request.session.get("ua_fp")
    if not expected:
        return
    if not hmac.compare_digest(str(expected), ua_fingerprint(request)):
        request.session.clear()
        raise HTTPException(401, "Session expired. Please sign in again.")


def validate_csrf(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        return
    path = request.url.path
    if path in _CSRF_EXEMPT_PATHS:
        return
    # Only enforce on app mutating routes
    if not (path.startswith("/api/") or path == "/logout"):
        return
    session_token = request.session.get("csrf") or ""
    header = request.headers.get(CSRF_HEADER) or ""
    form_token = ""
    # Allow cookie double-submit as fallback for simple forms
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    provided = header or form_token or cookie
    if not session_token or not provided or not hmac.compare_digest(str(session_token), str(provided)):
        raise HTTPException(403, "Invalid or missing CSRF token")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware (avoids BaseHTTPMiddleware session bugs)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        try:
            ensure_csrf(request)
        except Exception:
            pass

        if request.method not in _SAFE_METHODS:
            try:
                validate_csrf(request)
            except HTTPException as exc:
                resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                await resp(scope, receive, send)
                return

        secure = os.getenv("COOKIE_SECURE", "true").lower() == "true"
        csp = "; ".join(
            [
                "default-src 'self'",
                "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net",
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
                "font-src 'self' https://fonts.gstatic.com data:",
                "img-src 'self' data: blob:",
                "connect-src 'self'",
                "frame-ancestors 'none'",
                "base-uri 'self'",
                "form-action 'self'",
            ]
        )

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
                )
                headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
                headers.setdefault("Content-Security-Policy", csp)
                if secure:
                    headers.setdefault(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                try:
                    token = request.session.get("csrf")
                except Exception:
                    token = None
                if token:
                    cookie = (
                        f"{CSRF_COOKIE}={token}; Path=/; Max-Age={60 * 60 * 12}; SameSite=lax"
                    )
                    if secure:
                        cookie += "; Secure"
                    headers.append("Set-Cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)


def is_local_dev_request(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in {"127.0.0.1", "::1", "localhost"}
