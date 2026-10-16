"""A span exporter wrapper that scrubs spans before they leave the process.

Why an exporter wrapper rather than a span processor: span attributes become
immutable when a span ends (opentelemetry-sdk sets ``_attributes._immutable`` in
``Span.end``), and the only hook that runs earlier, ``_on_ending``, is private.
Rebuilding each span as a new ``ReadableSpan`` uses public API only and covers
spans created by auto-instrumentation as well as our own.

The wrapper fails closed: a span that cannot be scrubbed is dropped.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link
from opentelemetry.trace.status import Status, StatusCode

from hmcts_otel.conventions import EXCEPTION_FORMATTED, REDACTED
from hmcts_otel.redaction import Redactor

_log = logging.getLogger(__name__)

# Query strings can carry search terms, tokens and identifiers.
_QUERY_KEYS = frozenset({"url.query"})
_URL_KEYS = frozenset({"url.full", "http.url", "http.target", "url.path"})
_MESSAGE_KEYS = frozenset({"exception.message"})


_FRAME = re.compile(r'^\s*File "')
# Frames whose "file" is a pseudo-name (<unknown>, <string>, <stdin>) show source that
# was supplied at runtime, e.g. ast.literal_eval(user_text) -> SyntaxError.
_RUNTIME_SOURCE = re.compile(r'^\s*File "<[^"]*>"')
# Ordinary frames read 'File "...", line N, in name'. A frame without ", in ..." is
# the location line of a SyntaxError; its "source" is the text being parsed, which
# may be runtime input whatever the filename (ast.parse(text, filename="x.py")).
_CODE_FRAME = re.compile(r'^\s*File ".*", line \d+, in \S')
_EXCEPTION_LINE = re.compile(r"^([A-Za-z_][\w.<>]*)(?::|$)")
_CARETS = re.compile(r"^\s*[\^~]+\s*$")
_HEADER = "Traceback (most recent call last):"
_SEPARATORS = (
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
)


def sanitize_stacktrace(text: str) -> str:
    """Rebuild a formatted Python traceback keeping structure, never messages.

    A strict state machine over Python's traceback format:

    * a ``Traceback`` header is accepted only at the start or right after a chain
      separator;
    * ``File ...`` frames, their source line and caret markers are accepted only
      inside a traceback (between a header and its exception line). Source lines of
      runtime-supplied code (``File "<unknown>"`` etc.) are dropped;
    * the exception line becomes ``Type: [REDACTED]``;
    * everything after an exception line, until the next separator, is message text
      and is dropped, so a multi-line message (pydantic ``input_value``, a fake
      ``File`` line) cannot pass as structure.
    """
    out: list[str] = []
    expect_header = True  # a header (or a bare exception line) may come next
    in_traceback = False
    keep_source = False
    for line in text.splitlines():
        stripped = line.strip()
        if expect_header and stripped == _HEADER:
            out.append(_HEADER)
            expect_header, in_traceback, keep_source = False, True, False
            continue
        if in_traceback and _FRAME.match(line):
            out.append(line)
            keep_source = bool(_CODE_FRAME.match(line)) and not _RUNTIME_SOURCE.match(line)
            continue
        if in_traceback and _CARETS.match(line):
            out.append(line)
            continue
        if in_traceback and line.startswith("    "):
            if keep_source:
                out.append(line)
            keep_source = False
            continue
        keep_source = False
        if stripped in _SEPARATORS and not in_traceback:
            out += ["", stripped, ""]
            expect_header = True
            continue
        if (in_traceback or expect_header) and line and not line[0].isspace():
            match = _EXCEPTION_LINE.match(line)
            if match:
                out.append(f"{match.group(1)}: {REDACTED}")
            in_traceback, expect_header = False, False
            continue
        # Message text, exception-group boxes, anything else: dropped.
    return "\n".join(out) + "\n" if out else ""


_CODE_LIKE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _status_description(description: str) -> str:
    """Keep a controlled code or an exception type; never message text.

    The SDK sets ``"<Type>: <message>"`` when an exception escapes a span; WSGI
    instrumentation sets ``str(exception)`` alone. Our own stages set a controlled
    error code, which is kept.
    """
    match = re.match(r"^([A-Za-z_][\w.<>]*): ", description)
    if match:
        return f"{match.group(1)}: {REDACTED}"
    if _CODE_LIKE.fullmatch(description):
        return description
    return REDACTED


def _strip_query(url: str) -> str:
    base, sep, _ = url.partition("?")
    return f"{base}?{REDACTED}" if sep else base


def _with_route(key: str, value: str, route: str) -> str:
    """Replace a concrete path (which may carry identifiers or names) with the route template."""
    if key in ("http.target", "url.path"):
        return route
    parts = urlsplit(value)
    if parts.scheme and parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, route, "", ""))
    return route


def _scrub_value(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return tuple(redactor.redact(v) if isinstance(v, str) else v for v in value)
    return value


def scrub_attributes(
    attributes: Mapping[str, Any] | None, redactor: Redactor, *, include_exception_messages: bool
) -> dict[str, Any]:
    if not attributes:
        return {}
    # Exception text hmcts-otel did not build (no marker) cannot be told apart from a
    # message imitating traceback structure, so it is reduced to the type alone.
    formatted_here = attributes.get(EXCEPTION_FORMATTED) is True
    route = attributes.get("http.route")
    route = route if isinstance(route, str) and route else None
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if key == EXCEPTION_FORMATTED:
            continue
        if key in _QUERY_KEYS:
            out[key] = REDACTED if value else value
            continue
        if key in _URL_KEYS and isinstance(value, str):
            value = _with_route(key, value, route) if route is not None else _strip_query(value)
        if key in _MESSAGE_KEYS and not include_exception_messages:
            out[key] = REDACTED
            continue
        if key == "exception.stacktrace" and isinstance(value, str) and not include_exception_messages:
            if formatted_here:
                value = sanitize_stacktrace(value)  # already safe; idempotent
            else:
                type_name = attributes.get("exception.type")
                match = _EXCEPTION_LINE.match(type_name) if isinstance(type_name, str) else None
                value = f"{match.group(1) if match else 'Exception'}: {REDACTED}\n"
        out[key] = _scrub_value(value, redactor)
    return out


class ScrubbingSpanExporter(SpanExporter):
    def __init__(self, delegate: SpanExporter, redactor: Redactor, *, include_exception_messages: bool = False):
        self._delegate = delegate
        self._redactor = redactor
        self._include_messages = include_exception_messages

    def _scrub(self, span: ReadableSpan) -> ReadableSpan:
        kw = {"include_exception_messages": self._include_messages}
        events = [Event(e.name, scrub_attributes(e.attributes, self._redactor, **kw), e.timestamp) for e in span.events]
        status = span.status
        if status.description:
            description = status.description
            if not self._include_messages:
                description = _status_description(description)
            status = Status(status.status_code, self._redactor.redact(description))
        elif status.status_code is StatusCode.UNSET:
            status = Status(StatusCode.UNSET)
        return ReadableSpan(
            name=self._redactor.redact(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=scrub_attributes(span.attributes, self._redactor, **kw),
            events=events,
            links=[Link(link.context, scrub_attributes(link.attributes, self._redactor, **kw)) for link in span.links],
            kind=span.kind,
            status=status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        scrubbed: list[ReadableSpan] = []
        for span in spans:
            try:
                scrubbed.append(self._scrub(span))
            except Exception:  # fail closed
                _log.warning("hmcts_otel: dropped a span that could not be scrubbed", exc_info=False)
        if not scrubbed:
            return SpanExportResult.SUCCESS
        return self._delegate.export(scrubbed)

    def shutdown(self) -> None:
        self._delegate.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self._delegate, "force_flush", None)
        return bool(flush(timeout_millis)) if callable(flush) else True
