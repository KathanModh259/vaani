import logging
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextvars import ContextVar
from typing import Deque, Dict, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from scripts.config import settings

logger = logging.getLogger(__name__)

# The current request's id, readable from anywhere (log records, Sentry).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


# ------------------------------------------------------------ rate limits

# Tighter limits where abuse is cheap for an attacker and costly for us:
# credential guessing, account-creation spam, and endpoints that spend LLM or
# TTS credits on every call. First matching prefix wins.
RATE_RULES: Tuple[Tuple[str, int, int], ...] = (
    ("/api/auth/login", 10, 60),
    ("/api/auth/register", 5, 60),
    ("/api/auth/forgot-password", 5, 300),
    ("/api/auth/reset-password", 10, 300),
    ("/api/auth/refresh", 30, 60),
    ("/api/chat", 30, 60),
    ("/api/tts", 30, 60),
    ("/api/stt", 20, 60),
    ("/api/numbers", 20, 60),
    ("/api/business/test-call", 3, 600),   # each one is a paid outbound call
    ("/api/business/import", 5, 600),      # fetches a website + an LLM call
)

# Twilio webhooks are exempt: every client's calls arrive from the same small
# pool of Twilio IPs, so a per-IP limit would start dropping live calls as soon
# as a few clients were busy at once. They are authenticated by signature
# instead (api/routes/voice.py).
EXEMPT_PREFIXES = ("/api/voice/",)


def _rule_for(path: str) -> Optional[Tuple[str, int, int]]:
    if not path.startswith("/api/") or path.startswith(EXEMPT_PREFIXES):
        return None
    for prefix, limit, window in RATE_RULES:
        if path.startswith(prefix):
            return prefix, limit, window
    return "default", settings.RATE_LIMIT_REQUESTS, settings.RATE_LIMIT_WINDOW_SECONDS


class _MemoryLimiter:
    """Sliding window per key. Per-process: with several replicas each counts
    alone, which is why REDIS_URL exists."""

    def __init__(self):
        self.hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    async def hit(self, key: str, limit: int, window: int) -> Tuple[bool, int]:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] >= window:
            q.popleft()
        # Drop idle buckets, or this grows by one entry per IP ever seen.
        if now - self._last_sweep > 300:
            self._last_sweep = now
            for k in [k for k, v in self.hits.items() if not v or now - v[-1] > 600]:
                self.hits.pop(k, None)
            q = self.hits[key]
        if len(q) >= limit:
            return False, 0
        q.append(now)
        return True, limit - len(q)


class _RedisLimiter:
    """Fixed window shared across replicas. Falls back to memory if Redis is
    down: failing open on a limiter beats taking the whole API down with it."""

    def __init__(self, url: str, fallback: _MemoryLimiter):
        try:
            import redis.asyncio as redis
        except ImportError as e:
            # A slim image without the redis extra must still boot. Rate
            # limiting degrades to per-process memory; it never stops the
            # API from serving.
            logger.warning("redis package missing (%s); using in-memory rate limits", e)
            self.redis = None
            self.fallback = fallback
            self._warned = False
            return
        self.redis = redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self.fallback = fallback
        self._warned = False

    async def hit(self, key: str, limit: int, window: int) -> Tuple[bool, int]:
        bucket = f"rl:{key}:{int(time.time() // window)}"
        if self.redis is None:
            return await self.fallback.hit(key, limit, window)
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                count, _ = await pipe.incr(bucket).expire(bucket, window + 1).execute()
            self._warned = False
            return count <= limit, max(0, limit - count)
        except Exception as e:
            if not self._warned:
                self._warned = True
                logger.warning("Redis rate limiter unavailable, using in-memory: %s", e)
            return await self.fallback.hit(key, limit, window)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: FastAPI):
        super().__init__(app)
        memory = _MemoryLimiter()
        self.limiter = _RedisLimiter(settings.REDIS_URL, memory) if settings.REDIS_URL else memory

    @staticmethod
    def _client_ip(request: Request) -> str:
        """The real client IP.

        uvicorn resolves X-Forwarded-For into request.client for trusted
        proxies only (--proxy-headers / --forwarded-allow-ips; see the
        Dockerfile). Reading the header here directly would let any client
        forge it and get a fresh bucket per request.
        """
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        rule = _rule_for(request.url.path) if settings.RATE_LIMIT_ENABLED else None
        if rule is None:
            return await call_next(request)

        name, limit, window = rule
        allowed, remaining = await self.limiter.hit(f"{name}:{self._client_ip(request)}", limit, window)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please try again later."},
                headers={"X-RateLimit-Remaining": "0", "Retry-After": str(window)},
            )
        response: Response = await call_next(request)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


# ---------------------------------------------------------------- logging

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Access log plus a request id, echoed as X-Request-ID so a customer's
    "it failed at 3:14" can be matched to the exact log lines and Sentry event."""

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        rid = incoming if 0 < len(incoming) <= 64 and incoming.replace("-", "").isalnum() else uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        start = time.time()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        duration_ms = int((time.time() - start) * 1000)
        response.headers["X-Request-ID"] = rid
        logger.info(
            "%s %s -> %d (%dms) [%s]",
            request.method, request.url.path, response.status_code, duration_ms, rid,
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline hardening headers for the public site.

    HSTS is only sent over HTTPS, so it is safe to leave enabled in dev where
    everything is plain http. Scripts: our origin, or inline scripts carrying
    this response's nonce (stamped in by api/core/pages.py) - no
    'unsafe-inline', so injected script cannot run. Styles still allow
    inline: the pages use style attributes throughout, and CSS injection is
    far less dangerous than script injection.
    """

    CSP = (
        "default-src 'self'; "
        "script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), payment=()")

        # Docs pages need a looser policy than the app shell.
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            response.headers.setdefault("Content-Security-Policy", self.CSP.format(nonce=nonce))
        # Authenticated API responses (transcripts, exports) must not sit in
        # shared or browser caches.
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")

        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response
