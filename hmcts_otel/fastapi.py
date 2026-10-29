"""FastAPI integration (``hmcts-otel[fastapi]``).

* ``instrument_fastapi(app)`` — request spans and metrics, health probes excluded,
  request-ID middleware. Works regardless of import order.
* ``create_telemetry_relay_router(...)`` — a same-origin endpoint through which the
  browser sends operational and analytics events (decision OD1). Only events
  registered with ``browser=True`` are accepted; attributes are validated against
  the schema; analytics events require consent.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.params import Depends
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr, ValidationError
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hmcts_otel import _guard, _state
from hmcts_otel import conventions as c
from hmcts_otel._logging import capture_uvicorn_loggers, uvicorn_needs_capture
from hmcts_otel.context import Journey, _set_area_for_context, is_valid_request_id, request_id_context
from hmcts_otel.conventions import EventClass, EventSource
from hmcts_otel.events import record_event
from hmcts_otel.schema import TelemetrySchema, default_schema
from hmcts_otel.settings import TelemetrySettings

REQUEST_ID_HEADER = "x-request-id"


_access_log_warned = False


def _ensure_uvicorn_capture() -> None:
    """Second line of defence for uvicorn logging (the first is the patched
    ``Config.configure_logging``): re-capture uvicorn's loggers if they were
    reconfigured, or warn once if capture is turned off and access logging is on."""
    global _access_log_warned
    try:
        settings = _state.STATE.settings
        if settings is not None and settings.capture_uvicorn_logs:
            if uvicorn_needs_capture():
                capture_uvicorn_loggers()
            return
        access = logging.getLogger("uvicorn.access")
        if not _access_log_warned and not access.disabled and access.handlers:
            _access_log_warned = True
            logging.getLogger("hmcts_otel").warning(
                "uvicorn access logging is enabled; its lines include query strings and bypass hmcts-otel "
                "redaction. Run uvicorn with --no-access-log (or access_log=False)."
            )
    except Exception as exc:
        _guard.internal_error("uvicorn_capture", exc)


class UvicornLoggingGuard:
    """Runs ``_ensure_uvicorn_capture`` on lifespan startup and on each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "lifespan"):
            _ensure_uvicorn_capture()
        await self.app(scope, receive, send)


class RequestIdMiddleware:
    """Accepts a well-formed ``X-Request-Id`` or generates one; echoes it on the response."""

    def __init__(self, app: ASGIApp, header_name: str = REQUEST_ID_HEADER) -> None:
        self.app = app
        self.header_name = header_name.lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = None
        for name, value in scope.get("headers", []):
            if name == self.header_name:
                incoming = value.decode("latin-1")
                break
        # Canonical UUID / 32-hex only (see context.is_valid_request_id).
        request_id = incoming if is_valid_request_id(incoming) else uuid.uuid4().hex

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                try:
                    message.setdefault("headers", [])  # optional in the ASGI spec
                    MutableHeaders(scope=message)["X-Request-Id"] = request_id
                except Exception as exc:
                    _guard.internal_error("request_id_header", exc)
            await send(message)

        # Also on the ASGI scope (request.state.request_id): Starlette runs the
        # Exception handler in ServerErrorMiddleware, outside every user middleware,
        # after this context has been restored, so handlers must read it from there.
        scope.setdefault("state", {})["request_id"] = request_id
        # OpenTelemetry's ASGI middleware may wrap this one, in which case the server
        # span already exists; tag it directly as well as setting the context.
        trace.get_current_span().set_attribute(c.REQUEST_ID, request_id)
        with request_id_context(request_id):
            await self.app(scope, receive, send_with_id)


def request_id_for(request: Request) -> str | None:
    """The request ID for ``request``, also inside exception handlers.

    Prefer this over ``get_request_id()`` in exception handlers: the handler for
    unhandled exceptions runs after the middleware's context has ended.
    """
    from hmcts_otel.context import get_request_id

    state_id = getattr(request.state, "request_id", None)
    return state_id if isinstance(state_id, str) else get_request_id()


def area_dependency(name: str) -> Callable[[], Awaitable[None]]:
    """A router dependency that puts every request of the router in a product area.

    ``APIRouter(dependencies=[Depends(area_dependency("recording"))])``: events, stage
    events, log records and spans of the request carry ``hmcts.area`` (see
    ``area()``). The area is set for the rest of the request, which runs in its own
    context: exception handlers, background tasks and streamed bodies included. The
    current span when the dependency runs (the request's span, unless another span
    is current) is stamped too, since it started before dependencies ran. Async on
    purpose: an area set in a sync dependency runs in a thread pool and would not
    reach the endpoint. An invalid name is misuse, reported when a request runs
    (raised in strict mode, otherwise counted, and the request gets no area).
    """

    async def in_area() -> None:
        current = _set_area_for_context(name)
        if current is None:
            return
        try:
            trace.get_current_span().set_attribute(c.AREA, current)
        except Exception as exc:  # telemetry never fails the request
            _guard.internal_error("area_dependency", exc)

    return in_area


def instrument_fastapi(app: FastAPI, settings: TelemetrySettings | None = None, *, request_id: bool = True) -> None:
    """Instrument one FastAPI app. Call after ``setup_telemetry``."""
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    settings = settings or _state.STATE.settings or TelemetrySettings()
    kwargs: dict[str, Any] = {"excluded_urls": settings.excluded_urls}
    if _state.STATE.tracer_provider is not None:
        kwargs["tracer_provider"] = _state.STATE.tracer_provider
    if _state.STATE.meter_provider is not None:
        kwargs["meter_provider"] = _state.STATE.meter_provider
    FastAPIInstrumentor.instrument_app(app, **kwargs)
    if request_id:
        app.add_middleware(RequestIdMiddleware)
    # Added last so it is outermost and sees the lifespan startup.
    app.add_middleware(UvicornLoggingGuard)


# Browser relay -----------------------------------------------------------------------


class RelayJourney(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: StrictStr = Field(max_length=64)
    id: StrictStr = Field(max_length=64)
    parent_id: StrictStr | None = Field(default=None, max_length=64)


class RelayEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: StrictStr = Field(max_length=100)
    attributes: dict[StrictStr, StrictBool | StrictInt | StrictFloat | StrictStr] = Field(
        default_factory=dict, max_length=32
    )
    journey: RelayJourney | None = None
    duration_ms: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=86_400_000)


class RelayBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[RelayEvent] = Field(max_length=50)


class _FixedWindowLimiter:
    """Per-process, per-identity request limit. Multiple replicas each apply their own.

    At most ``max_keys`` identities are tracked. Expired buckets are pruned only when a
    new identity arrives at capacity, and at most once per ``prune_interval_s``, so the
    work per request stays bounded however many identities arrive. While still at
    capacity a new identity is refused (counted as ``invalid.relay_capacity``) and the
    identities already tracked keep their state, so nobody's limit is reset.
    """

    def __init__(
        self, limit: int, window_s: float = 60.0, max_keys: int = 10_000, prune_interval_s: float = 5.0
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self.prune_interval_s = min(prune_interval_s, window_s)
        self._buckets: dict[str, tuple[float, int]] = {}
        self._next_prune = 0.0
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None and len(self._buckets) >= self.max_keys:
                if now >= self._next_prune:
                    self._next_prune = now + self.prune_interval_s
                    self._buckets = {k: v for k, v in self._buckets.items() if now - v[0] < self.window_s}
                at_capacity = len(self._buckets) >= self.max_keys
            else:
                at_capacity = False
            if not at_capacity:
                start, count = bucket or (now, 0)
                if now - start >= self.window_s:
                    start, count = now, 0
                if count >= self.limit:
                    self._buckets[key] = (start, count)
                    return False
                self._buckets[key] = (start, count + 1)
                return True
        _guard.invalid_data("relay_capacity")
        return False


def _default_identity(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def create_telemetry_relay_router(
    *,
    schema: TelemetrySchema | None = None,
    path: str = "/telemetry",
    dependencies: Sequence[Depends] = (),
    identity: Callable[[Request], str | None] | None = None,
    analytics_consent: Callable[[Request], bool] | None = None,
    journey_types: Collection[str] | None = None,
    allow_parent_id: bool = False,
    max_body_bytes: int = 32_768,
    rate_limit_per_minute: int = 120,
) -> APIRouter:
    """Build the browser telemetry relay.

    ``dependencies`` should include the application's authentication dependency.
    ``identity`` keys the rate limit (default: client address).
    ``analytics_consent`` decides whether analytics-class events are accepted
    (default: never). ``journey_types`` lists the journeys a browser may attach
    events to (default: none). Journey IDs come from the client, so a listed type
    must be one the browser owns (e.g. its own session), never a server-side
    journey such as another user's job. An event whose journey type is not listed
    is rejected. Journey ``id`` and ``parent_id`` must be a UUID or 32 hex characters
    (anything else, such as a name or phone number, rejects the event).
    ``allow_parent_id`` keeps a browser-sent ``parent_id`` (default: dropped, since it
    would let a user link their events under any journey).
    Responses never echo the submitted content.
    """
    schema = schema or default_schema
    limiter = _FixedWindowLimiter(rate_limit_per_minute)
    identify = identity or _default_identity
    allowed_journeys = frozenset(journey_types or ())
    router = APIRouter(dependencies=list(dependencies))

    @router.post(path, status_code=202, include_in_schema=True, summary="Browser telemetry relay")
    async def relay(request: Request) -> JSONResponse:
        # JSON only: a cross-site form or text/plain POST (no CORS preflight) cannot
        # submit events with the user's cookies.
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse({"detail": "content type must be application/json"}, status_code=415)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_body_bytes:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        # Count while reading: without Content-Length (chunked), request.body() would
        # buffer an unbounded body before any check.
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_body_bytes:
                return JSONResponse({"detail": "payload too large"}, status_code=413)
            chunks.append(chunk)
        body = b"".join(chunks)
        key = identify(request) or _default_identity(request)
        if not limiter.allow(key):
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        try:
            batch = RelayBatch.model_validate_json(body)
        except ValidationError:
            return JSONResponse({"detail": "invalid telemetry batch"}, status_code=422)

        consent = bool(analytics_consent(request)) if analytics_consent is not None else False
        accepted = rejected = 0
        for event in batch.events:
            spec = schema.get_event(event.name)
            if spec is None or not spec.browser:
                rejected += 1
                continue
            if spec.event_class is EventClass.ANALYTICS and not consent:
                rejected += 1
                continue
            journey = None
            if event.journey is not None:
                if event.journey.type not in allowed_journeys:
                    rejected += 1
                    continue
                parent_id = event.journey.parent_id if allow_parent_id else None
                if not is_valid_request_id(event.journey.id) or (
                    parent_id is not None and not is_valid_request_id(parent_id)
                ):
                    rejected += 1
                    continue
                try:
                    journey = Journey(event.journey.type, event.journey.id, parent_id)
                except ValueError:
                    rejected += 1
                    continue
            record_event(
                event.name,
                event.attributes,
                source=EventSource.BROWSER,
                journey=journey,
                duration_ms=float(event.duration_ms) if event.duration_ms is not None else None,
                schema=schema,
            )
            accepted += 1
        return JSONResponse({"accepted": accepted, "rejected": rejected}, status_code=202)

    return router
