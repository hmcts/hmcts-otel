"""Exceptions recorded on spans are formatted from the exception object, not text.

Instrumentations (FastAPI/ASGI, httpx, psycopg, Azure SDK) call
``span.record_exception(exc)``. The SDK implementation stores ``str(exc)`` and a
``traceback.format_exception`` string, which carry the exception message and, for
``SyntaxError``, the offending source text. Once formatted, a message that imitates
traceback structure cannot be told apart from real structure, so the only complete
fix is to never produce that text.

While hmcts-otel is configured (and exception messages are not opted in),
``Span.record_exception`` on SDK spans records ``exception.type``, a redacted
``exception.message`` and a stack trace built from the exception object by
``_exceptions.format_exception``: frames from the code objects, every message in
the chain replaced. The export-time text sanitiser remains as defence in depth.

``Span.record_exception`` has the same signature in opentelemetry-sdk 1.40–1.45.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opentelemetry.sdk.trace import Span

from hmcts_otel import _state
from hmcts_otel._exceptions import exception_type_name, format_exception
from hmcts_otel.conventions import EXCEPTION_FORMATTED, REDACTED

_ORIGINAL = Span.record_exception


def _record_exception(
    self: Span,
    exception: BaseException,
    attributes: Mapping[str, Any] | None = None,
    timestamp: int | None = None,
    escaped: bool = False,
) -> None:
    state = _state.STATE
    if not state.configured or _state.capture_exception_messages():
        _ORIGINAL(self, exception, attributes, timestamp, escaped)  # type: ignore[arg-type]
        return
    try:
        safe: dict[str, Any] = {
            "exception.type": exception_type_name(type(exception)),
            "exception.message": REDACTED,
            "exception.stacktrace": format_exception(exception, include_messages=False, redactor=state.redactor),
            "exception.escaped": str(escaped),
            EXCEPTION_FORMATTED: True,
        }
    except Exception:  # never fail the caller; keep the type only
        safe = {"exception.type": type(exception).__name__, "exception.message": REDACTED}
    if attributes:
        safe.update(attributes)
    self.add_event(name="exception", attributes=safe, timestamp=timestamp)


def install() -> None:
    """Idempotent. Applies to every SDK span; inactive while hmcts-otel is not configured."""
    if getattr(Span.record_exception, "_hmcts_otel", False):
        return
    _record_exception._hmcts_otel = True  # type: ignore[attr-defined]
    Span.record_exception = _record_exception  # type: ignore[method-assign]
