"""Standard-library logging: JSON to stdout, redaction and context on every handler.

Filters are attached to *handlers*, not loggers. A logger-level filter only sees
records logged directly to that logger, so records from child loggers would
bypass it.
"""

from __future__ import annotations

import json
import logging
import sys
import weakref
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from hmcts_otel._exceptions import exception_attributes
from hmcts_otel.context import context_attributes
from hmcts_otel.conventions import EXCEPTION_FORMATTED, REDACTED
from hmcts_otel.redaction import Redactor
from hmcts_otel.settings import TelemetrySettings

EVENTS_LOGGER = "hmcts_otel.events"

_EXCEPTION_KEYS = frozenset({"exception.type", "exception.message", "exception.stacktrace", EXCEPTION_FORMATTED})
# Records whose exception attributes this package built. Identity-based, so it cannot
# be forged through ``extra=`` (which can set any attribute name).
_FORMATTED_RECORDS: weakref.WeakSet[logging.LogRecord] = weakref.WeakSet()

_RESERVED = frozenset(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}


def _extra_items(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED}


class RedactingFilter(logging.Filter):
    """Redacts the message and string extras, and replaces ``exc_info`` with safe attributes.

    Idempotent: a record passing through several handlers is redacted once in effect.
    """

    def __init__(self, redactor: Redactor, *, include_exception_messages: bool = False) -> None:
        super().__init__()
        self._redactor = redactor
        self._include_messages = include_exception_messages

    def _safe_extra(self, value: Any) -> Any:
        """Primitives pass (strings redacted); flat sequences of primitives pass; anything
        else (dicts, objects) is replaced by a placeholder, because its string form can
        carry content that no pattern recognises."""
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            return self._redactor.redact(value)
        if isinstance(value, list | tuple) and all(v is None or isinstance(v, str | bool | int | float) for v in value):
            return tuple(self._redactor.redact(v) if isinstance(v, str) else v for v in value)
        return f"[{type(value).__name__} omitted]"

    def filter(self, record: logging.LogRecord) -> bool:
        # Handler filters run outside logging's error handling, so anything raised
        # here would reach the logging call site (and could replace an exception
        # being handled there). Fail closed: keep the record, drop its content.
        try:
            self._redact(record)
        except Exception as exc:
            record.msg = "[unformattable log record]"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            # Extras (``extra=``, context) may be partly or not at all redacted.
            for key in list(_extra_items(record)):
                del record.__dict__[key]
            try:
                from hmcts_otel import _guard

                _guard.internal_error("log_filter", exc)
            except Exception:  # pragma: no cover
                pass
        return True

    def _redact(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args or an unprintable msg object
            try:
                message = str(record.msg)
            except Exception:
                message = "[unformattable log message]"
        record.msg = self._redactor.redact(message)
        record.args = None
        formatted_here = record in _FORMATTED_RECORDS
        for key, value in _extra_items(record).items():
            if key in _EXCEPTION_KEYS:
                # Exception text supplied via ``extra=`` is not trusted; only the
                # package's own formatting (below, from exc_info) is. A record already
                # formatted by another handler's filter keeps that formatting.
                if not formatted_here:
                    if key == EXCEPTION_FORMATTED:
                        del record.__dict__[key]
                    else:
                        record.__dict__[key] = REDACTED
                continue
            if isinstance(key, str) and self._redactor.matches(key):
                del record.__dict__[key]
                continue
            record.__dict__[key] = self._safe_extra(value)
        if record.exc_info:
            exc = record.exc_info[1] if isinstance(record.exc_info, tuple) else None
            if exc is not None:
                # Keeps the EXCEPTION_FORMATTED marker, which tells the log record
                # processor this stack trace is safe; the processor removes it.
                attrs = exception_attributes(exc, include_messages=self._include_messages, redactor=self._redactor)
                record.__dict__.update(attrs)
                _FORMATTED_RECORDS.add(record)
            # exc_info=True outside an except block gives (None, None, None): nothing to record.
            record.exc_info = None
            record.exc_text = None


class ContextFilter(logging.Filter):
    """Adds journey and request attributes. Explicit ``extra`` values win."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == EVENTS_LOGGER:
            return True
        for key, value in context_attributes().items():
            record.__dict__.setdefault(key, value)
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, settings: TelemetrySettings) -> None:
        super().__init__()
        self._static = {"service.name": settings.service_name, "deployment.environment": settings.environment}
        if settings.service_version:
            self._static["service.version"] = settings.service_version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._static,
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        for key, value in _extra_items(record).items():
            if key != EXCEPTION_FORMATTED:
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:  # only if no RedactingFilter ran
            payload["exception.type"] = record.exc_info[0].__name__
        return json.dumps(payload, default=str)


class StdoutHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stdout`` is at emit time (survives redirection and test capture)."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property  # type: ignore[override]
    def stream(self):  # noqa: D401
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")


def make_filters(redactor: Redactor, settings: TelemetrySettings) -> list[logging.Filter]:
    return [
        ContextFilter(),
        RedactingFilter(redactor, include_exception_messages=settings.capture_exception_messages),
    ]


def add_filters(handler: logging.Handler, filters: list[logging.Filter]) -> None:
    """Install this setup's filters on ``handler``, replacing any left by an earlier setup.

    Filters on handlers the package does not own (pre-existing root handlers,
    ``logging.lastResort``, the distro's handler) outlive ``shutdown_telemetry`` on
    purpose, so output stays redacted. A later setup must replace them: a stale
    ``RedactingFilter`` with older settings (e.g. exception messages on) would run
    first and its output would be reused by every later handler.
    """
    ours = {type(f) for f in filters}
    for existing in list(handler.filters):
        if type(existing) in ours:
            handler.removeFilter(existing)
    for f in filters:
        handler.addFilter(f)


def resolve_level(value: str | int) -> tuple[int, bool]:
    """Return ``(level, valid)``. Accepts names in any case and numbers; unknown values give INFO."""
    if isinstance(value, int):
        return value, True
    text = str(value).strip()
    if text.isdigit():
        return int(text), True
    level = logging.getLevelName(text.upper())
    if isinstance(level, int):
        return level, True
    return logging.INFO, False


def configure_logging(settings: TelemetrySettings, redactor: Redactor) -> list[logging.Handler]:
    """Install the stdout handler on the root logger and return the handlers added.

    Handlers that already exist on the root logger (e.g. from ``logging.basicConfig``)
    and Python's last-resort handler (used when no handler exists, e.g. after
    shutdown) get the redaction filters too, so nothing prints unredacted.
    """
    root = logging.getLogger()
    level, valid = resolve_level(settings.log_level)
    root.setLevel(level)

    filters = make_filters(redactor, settings)
    for existing in list(root.handlers):
        add_filters(existing, filters)
    if logging.lastResort is not None:
        add_filters(logging.lastResort, filters)

    # Replace a stdout handler from an earlier setup or a setup_logging() bootstrap,
    # so a later full setup never doubles every line.
    for existing in list(root.handlers):
        if isinstance(existing, StdoutHandler):
            root.removeHandler(existing)
    handler = StdoutHandler()
    handler.setFormatter(JsonFormatter(settings) if settings.log_format == "json" else TextFormatter())
    add_filters(handler, filters)
    root.addHandler(handler)
    if not valid:
        logging.getLogger("hmcts_otel").warning("hmcts_otel: unknown log level; using INFO")

    # Lifecycle events must be recorded whatever the application log level is.
    logging.getLogger(EVENTS_LOGGER).setLevel(logging.INFO)

    if settings.capture_uvicorn_logs:
        capture_uvicorn_loggers()
        patch_uvicorn_logging()
    return [handler]


_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error")
_UVICORN_ACCESS = "uvicorn.access"


def capture_uvicorn_loggers() -> None:
    """Route uvicorn's loggers through the root handlers (which redact) and turn access logs off.

    uvicorn installs its own ``StreamHandler``s with ``propagate=False``; records
    sent there (including "Exception in ASGI application" tracebacks with full
    messages) would bypass redaction. Access lines duplicate request telemetry and
    include query strings, so they are disabled.
    """
    for name in _UVICORN_LOGGERS:
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True
    access = logging.getLogger(_UVICORN_ACCESS)
    access.handlers.clear()
    access.propagate = False
    access.disabled = True


def uvicorn_needs_capture() -> bool:
    """True if uvicorn (re)installed its own handlers or re-enabled access logging."""
    for name in _UVICORN_LOGGERS:
        uv = logging.getLogger(name)
        if uv.handlers or not uv.propagate:
            return True
    access = logging.getLogger(_UVICORN_ACCESS)
    return not access.disabled or bool(access.handlers)


def patch_uvicorn_logging() -> None:
    """Re-capture uvicorn's loggers every time uvicorn applies its logging config.

    ``uvicorn.run(app)``, ``fastapi run`` and programmatic ``uvicorn.Config`` apply
    uvicorn's ``LOGGING_CONFIG`` *after* the application module (and so
    ``setup_telemetry``) has been imported, reinstalling uvicorn's handlers.
    ``Config.configure_logging`` is the single place this happens (uvicorn 0.20+);
    wrapping it is best-effort and safe if uvicorn is absent or changes.
    """
    try:
        import uvicorn.config as uvicorn_config
    except Exception:
        return
    try:
        original = uvicorn_config.Config.configure_logging
        if getattr(original, "_hmcts_otel", False):
            return

        def configure_logging(self: Any) -> None:
            original(self)
            try:
                from hmcts_otel._state import STATE

                if STATE.configured and STATE.settings is not None and STATE.settings.capture_uvicorn_logs:
                    capture_uvicorn_loggers()
            except Exception:  # never break uvicorn startup
                pass

        configure_logging._hmcts_otel = True  # type: ignore[attr-defined]
        uvicorn_config.Config.configure_logging = configure_logging  # type: ignore[method-assign]
    except Exception:
        return


def remove_handlers(handlers: list[logging.Handler]) -> None:
    root = logging.getLogger()
    for h in handlers:
        root.removeHandler(h)
        try:
            h.flush()
        except Exception:
            pass
