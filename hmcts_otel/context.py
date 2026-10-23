"""Journey and request context carried on every span, log record and event."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, TypeGuard

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from hmcts_otel import conventions as c
from hmcts_otel.schema import default_schema


@dataclass(frozen=True)
class Journey:
    """A user or system journey, identified pseudonymously.

    ``type`` is a slug such as ``dictation.session`` or ``recording.job``.
    ``id`` and ``parent_id`` are system-generated identifiers (UUIDs), never
    case references, file names or anything a person typed.
    """

    type: str
    id: str
    parent_id: str | None = None

    def __post_init__(self) -> None:
        default_schema.check_identifier(c.JOURNEY_TYPE, self.type)
        default_schema.check_identifier(c.JOURNEY_ID, self.id)
        if self.parent_id is not None:
            default_schema.check_identifier(c.JOURNEY_PARENT_ID, self.parent_id)

    def attributes(self) -> dict[str, str]:
        attrs = {c.JOURNEY_TYPE: self.type, c.JOURNEY_ID: self.id}
        if self.parent_id is not None:
            attrs[c.JOURNEY_PARENT_ID] = self.parent_id
        return attrs


_journey: ContextVar[Journey | None] = ContextVar("hmcts_otel_journey", default=None)
_request_id: ContextVar[str | None] = ContextVar("hmcts_otel_request_id", default=None)
# Name of the innermost ``stage()`` block, set by ``stages.stage``.
_stage: ContextVar[str | None] = ContextVar("hmcts_otel_stage", default=None)


@contextmanager
def journey(journey_type: str, journey_id: str, *, parent_id: str | None = None) -> Iterator[Journey | None]:
    """Attach a journey to everything emitted inside the block.

    Works across ``await`` boundaries. A malformed ID (e.g. a case reference) is
    rejected: in strict mode ``ValueError`` is raised; otherwise the block runs
    without a journey and the misuse is counted.
    """
    from hmcts_otel import _guard

    try:
        current: Journey | None = Journey(
            journey_type, str(journey_id), str(parent_id) if parent_id is not None else None
        )
    except ValueError as exc:
        _guard.invalid("journey", f"invalid journey: {exc}")
        current = None
    previous = _journey.get()
    token = _journey.set(current)
    try:
        yield current
    finally:
        if token is not None:
            _restore(_journey, token, previous)


def _restore(var: ContextVar[Any], token: Token[Any], previous: Any) -> None:
    """Undo a ``set`` without ever raising.

    ``ContextVar.reset`` raises ``ValueError`` if the block is resumed in a different
    context than it started in, e.g. a sync generator driving a Starlette
    ``StreamingResponse`` (each ``next()`` runs in a copied context). Fall back to
    restoring the previous value in the current context, as OpenTelemetry's own
    ``detach`` tolerates the same situation.
    """
    try:
        var.reset(token)
    except ValueError:
        var.set(previous)


def current_journey() -> Journey | None:
    return _journey.get()


def current_stage() -> str | None:
    """Name of the innermost ``stage()`` block in the current context, or None."""
    return _stage.get()


def get_request_id() -> str | None:
    return _request_id.get()


# Canonical UUID or 32 hex characters only. A looser rule ("hex-ish, 8+ chars")
# admits phone numbers, numeric case or NI-style IDs and hex-shaped references, which
# would then be copied onto every log line, span and event of the request.
_REQUEST_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}")


def is_valid_request_id(value: object) -> TypeGuard[str]:
    """True for a canonical UUID or 32-character hex string. The single rule used by
    the middleware, ``request_id_context`` and applications."""
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


@contextmanager
def request_id_context(request_id: str) -> Iterator[str]:
    """Set the request ID for the block. Only UUID/hex values are accepted; anything
    else is replaced by a generated ID (misuse raises in strict mode)."""
    if not is_valid_request_id(request_id):
        from hmcts_otel import _guard

        _guard.invalid("request_id", "request IDs must be UUID or hex")
        request_id = uuid.uuid4().hex
    previous = _request_id.get()
    token = _request_id.set(request_id)
    try:
        yield request_id
    finally:
        _restore(_request_id, token, previous)


def context_attributes() -> dict[str, str]:
    """Journey and request attributes for the current context."""
    attrs: dict[str, str] = {}
    current = _journey.get()
    if current is not None:
        attrs.update(current.attributes())
    rid = _request_id.get()
    if rid is not None:
        attrs[c.REQUEST_ID] = rid
    return attrs


class JourneySpanProcessor(SpanProcessor):
    """Adds journey and request attributes to every span when it starts."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        attrs = context_attributes()
        if attrs:
            span.set_attributes(attrs)

    def on_end(self, span: ReadableSpan) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
