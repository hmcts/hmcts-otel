"""Exception formatting that can leave messages out.

Exception messages routinely carry the data being processed (a validation error
echoing input, a JSON decode error quoting the payload). By default exported
telemetry keeps the type and the stack, and replaces every message in the chain.
"""

from __future__ import annotations

import builtins
import traceback

from hmcts_otel.conventions import EXCEPTION_FORMATTED, REDACTED
from hmcts_otel.redaction import Redactor

_CAUSE = "\nThe above exception was the direct cause of the following exception:\n\n"
_CONTEXT = "\nDuring handling of the above exception, another exception occurred:\n\n"
# Chains deeper than this are cut (a chain of thousands would otherwise cost
# recursion depth and megabytes of text).
MAX_CHAIN = 25


def exception_type_name(exc_type: type[BaseException]) -> str:
    module = exc_type.__module__
    if module in (builtins.__name__, "__main__"):
        return exc_type.__qualname__
    return f"{module}.{exc_type.__qualname__}"


def _safe_str(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:
        return "<unprintable message>"


def _chain(exc: BaseException) -> tuple[list[tuple[BaseException, str | None]], bool]:
    """Oldest-first list of (exception, separator before it), iteratively, plus whether it was cut."""
    items: list[tuple[BaseException, str | None]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    separator: str | None = None
    while current is not None and id(current) not in seen:
        if len(items) >= MAX_CHAIN:
            return list(reversed(items)), True
        seen.add(id(current))
        items.append((current, separator))
        if current.__cause__ is not None:
            current, separator = current.__cause__, _CAUSE
        elif current.__context__ is not None and not current.__suppress_context__:
            current, separator = current.__context__, _CONTEXT
        else:
            current = None
    return list(reversed(items)), False


def format_exception(exc: BaseException, *, include_messages: bool, redactor: Redactor) -> str:
    """Full stack trace with the chain, messages replaced unless ``include_messages``."""
    items, cut = _chain(exc)
    parts: list[str] = []
    if cut:
        parts.append(f"[{MAX_CHAIN} most recent exceptions of a longer chain]\n")
    # Items are oldest first. An older item carries the separator that links it to
    # the newer exception printed after it (cause or context).
    for index, (item, separator) in enumerate(items):
        if item.__traceback__ is not None:
            parts.append("Traceback (most recent call last):\n")
            try:
                parts += traceback.format_tb(item.__traceback__)
            except Exception:
                parts.append("  <traceback unavailable>\n")
        message = _safe_str(item) if include_messages else REDACTED
        parts.append(f"{exception_type_name(type(item))}: {message}\n")
        if index + 1 < len(items):
            parts.append(separator or _CONTEXT)
    return redactor.redact("".join(parts))


def exception_attributes(exc: BaseException, *, include_messages: bool, redactor: Redactor) -> dict[str, str | bool]:
    attrs: dict[str, str | bool] = {
        "exception.type": exception_type_name(type(exc)),
        "exception.stacktrace": format_exception(exc, include_messages=include_messages, redactor=redactor),
        EXCEPTION_FORMATTED: True,
    }
    if include_messages:
        attrs["exception.message"] = redactor.redact(_safe_str(exc))
    return attrs
