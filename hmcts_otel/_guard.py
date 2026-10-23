"""Telemetry must never change application behaviour.

Two kinds of problem are handled here:

* **Misuse** (an invalid stage name, error code, event name or journey ID). In
  strict mode (tests, ``HMCTS_OTEL_STRICT=true``) it raises ``ValueError`` so the
  mistake is caught in CI. Otherwise it is counted and logged once, and the
  application carries on.
* **Internal failure** (an exporter, metric or logging error inside the package).
  Never raised into application code; counted on ``hmcts.telemetry.errors`` and
  logged once per kind, by type only.
"""

from __future__ import annotations

import logging
import threading

from hmcts_otel import _state
from hmcts_otel import conventions as c

_log = logging.getLogger("hmcts_otel")
_reported: set[str] = set()
_reported_lock = threading.Lock()
_MAX_REPORTED = 256


def strict() -> bool:
    settings = _state.STATE.settings
    return bool(settings is not None and settings.strict)


def _count(kind: str) -> None:
    try:
        _state.instrument(
            "counter", c.METRIC_TELEMETRY_ERRORS, unit="{error}", description="Telemetry misuse and internal errors"
        ).add(1, {"kind": kind})
    except Exception:  # pragma: no cover - last line of defence
        pass


def _log_once(kind: str, text: str) -> None:
    with _reported_lock:
        if kind in _reported or len(_reported) >= _MAX_REPORTED:
            return
        _reported.add(kind)
    try:
        _log.warning("hmcts_otel %s: %s", kind, text)
    except Exception:  # pragma: no cover
        pass


def invalid(kind: str, message: str) -> None:
    """Report misuse. Raises ``ValueError`` in strict mode."""
    if strict():
        raise ValueError(message)
    _count(f"invalid.{kind}")
    _log_once(f"invalid.{kind}", "invalid input ignored (set HMCTS_OTEL_STRICT=true in tests to raise)")


def internal_error(kind: str, exc: BaseException | None = None) -> None:
    """Report an internal failure. Never raises."""
    _count(f"internal.{kind}")
    _log_once(f"internal.{kind}", type(exc).__name__ if exc is not None else "error")


def invalid_data(kind: str) -> None:
    """Report a bad value that came from data rather than code, e.g. a model name or
    token count from a provider response. Counted and logged once like misuse, but
    never raised, even in strict mode: the caller cannot fix the provider."""
    _count(f"invalid.{kind}")
    _log_once(f"invalid.{kind}", "invalid value replaced or ignored")


def safe_dimension(value: str, fallback: str = "redacted") -> str:
    """A metric dimension value, checked against the active redactor.

    Metric dimensions are not redacted at export, so a value that matches a redaction
    rule (including rules added with ``setup_telemetry(redactor=...)``) is replaced by
    ``fallback`` and counted. Never raises.
    """
    try:
        if _state.current_redactor().matches(value):
            invalid_data("metric_dimension")
            return fallback
    except Exception as exc:  # pragma: no cover - last line of defence
        internal_error("metric_dimension", exc)
        return fallback
    return value
