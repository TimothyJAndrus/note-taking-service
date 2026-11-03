"""Operations: structured logs with request IDs, Prometheus metrics, rate limiting, tracing."""
import hashlib
import json
import logging
import re
import sys
import threading
import time
import uuid

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from app import config

log = logging.getLogger("notes")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "route"])
RATE_LIMITED = Counter("http_rate_limited_total", "Requests rejected by the rate limiter")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ---- logging ------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        out.update(getattr(record, "fields", {}))
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if config.LOG_FORMAT == "json"
                         else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)
    logging.getLogger("uvicorn.access").disabled = True  # replaced by our access log below


# ---- rate limiting ------------------------------------------------------------------

class TokenBucket:
    """Per-credential token bucket: `rate` requests per minute, bursts up to `rate`.

    Per instance. Behind the load balancer, AWS WAF's rate-based rule is the cross-instance
    limit (per client IP); this one stops a single noisy key from starving everyone else.
    """

    def __init__(self, per_minute: int, max_keys: int = 50_000):
        self.capacity = float(per_minute)
        self.refill = per_minute / 60.0
        self.max_keys = max_keys
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float:
        """Return 0 if allowed, else seconds until the next token."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                if len(self._buckets) > self.max_keys:  # bound memory: drop the oldest half
                    for k in sorted(self._buckets, key=lambda k: self._buckets[k][1])[: self.max_keys // 2]:
                        del self._buckets[k]
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / self.refill


def _client_key(request: Request) -> str:
    auth = request.headers.get("authorization")
    if auth:  # never keep raw credentials in memory as dict keys
        return "cred:" + hashlib.sha256(auth.encode()).hexdigest()[:32]
    # The ALB appends the real client IP as the LAST entry; earlier entries are client-supplied.
    fwd = request.headers.get("x-forwarded-for", "")
    return "ip:" + (fwd.split(",")[-1].strip() or (request.client.host if request.client else "unknown"))


# ---- middleware ----------------------------------------------------------------------

class RequestContextMiddleware(BaseHTTPMiddleware):
    UNMETERED = {"/healthz", "/readyz", "/metrics"}

    def __init__(self, app, limiter: TokenBucket | None):
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id", "")
        rid = rid if _REQUEST_ID.match(rid) else uuid.uuid4().hex
        request.state.request_id = rid
        start = time.perf_counter()
        path = request.url.path

        if self.limiter and path not in self.UNMETERED:
            wait = self.limiter.take(_client_key(request))
            if wait:
                RATE_LIMITED.inc()
                return Response('{"detail":"Rate limit exceeded"}', status_code=429,
                                media_type="application/json",
                                headers={"Retry-After": str(max(1, round(wait))), "X-Request-ID": rid})
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled error", extra={"fields": {"request_id": rid, "path": path}})
            raise
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")  # template, e.g. /teams/{team_id}/notes
        elapsed = time.perf_counter() - start
        REQUESTS.labels(request.method, route_path, response.status_code).inc()
        LATENCY.labels(request.method, route_path).observe(elapsed)
        response.headers["X-Request-ID"] = rid
        if path not in self.UNMETERED:
            log.info("request", extra={"fields": {
                "request_id": rid, "method": request.method, "route": route_path,
                "status": response.status_code, "duration_ms": round(elapsed * 1000, 1)}})
        return response


def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def setup_tracing(app: FastAPI) -> None:
    """OpenTelemetry traces, only when an OTLP endpoint is configured (e.g. ADOT collector sidecar)."""
    import os
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": os.environ.get(
        "OTEL_SERVICE_NAME", "note-taking-service")}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
    PsycopgInstrumentor().instrument(enable_commenter=False)  # spans carry SQL text, never parameters
