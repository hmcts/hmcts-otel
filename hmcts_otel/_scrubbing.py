"""Scrubbing of spans and log records before they leave the process.

Why an exporter wrapper rather than a span processor: span attributes become
immutable when a span ends (opentelemetry-sdk sets ``_attributes._immutable`` in
``Span.end``), and the only hook that runs earlier, ``_on_ending``, is private.
Rebuilding each span as a new ``ReadableSpan`` uses public API only and covers
spans created by auto-instrumentation as well as our own.

The wrapper fails closed: a span that cannot be scrubbed is dropped.

Log records are scrubbed by ``ScrubbingLogRecordProcessor``, the first processor on
the package's LoggerProvider. Records logged through the standard library have
already passed ``RedactingFilter``; records emitted directly through the
OpenTelemetry Logs API (GenAI instrumentation events, for example) have not, and
get the same protections here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from opentelemetry.sdk._logs import LogRecordProcessor
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Link
from opentelemetry.trace.status import Status, StatusCode

from hmcts_otel._exceptions import MAX_CHAIN
from hmcts_otel.conventions import EXCEPTION_FORMATTED, REDACTED
from hmcts_otel.redaction import Redactor, is_secret_key

_log = logging.getLogger(__name__)

# Query strings can carry search terms, tokens and identifiers.
_QUERY_KEYS = frozenset({"url.query"})
_URL_KEYS = frozenset({"url.full", "http.url", "http.target", "url.path"})
_MESSAGE_KEYS = frozenset({"exception.message"})
# Prompt and response content written by GenAI instrumentation (OpenTelemetry GenAI
# semantic conventions, older indexed forms such as gen_ai.prompt.0.content used by
# litellm's and OpenLLMetry's integrations, and OpenInference). Dropped, never
# redacted: content cannot be made safe by pattern matching.
_GENAI_CONTENT_PREFIXES = (
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
    "gen_ai.tool.definitions",
    "gen_ai.retrieval.query.text",
    "gen_ai.retrieval.documents",
    "gen_ai.embeddings.input",
    "gen_ai.event.content",
    "llm.prompts",
    "llm.completions",
    "llm.input_messages",
    "llm.output_messages",
    "input.value",
    "output.value",
)


def is_genai_content_key(key: str) -> bool:
    """True for an attribute that carries GenAI prompt or response content."""
    return any(key == p or key.startswith(p + ".") for p in _GENAI_CONTENT_PREFIXES)


# GenAI events (span events and log records named gen_ai.*, e.g. gen_ai.user.message,
# gen_ai.choice) carry content under unprefixed keys (content, message.content,
# tool_calls) and in the log body. They keep only these attributes; everything else,
# and the body, is dropped.
GENAI_EVENT_PREFIX = "gen_ai."
_GENAI_EVENT_ATTRIBUTES = frozenset(
    {
        "event.name",
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.response.finish_reasons",
        "index",
        "finish_reason",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.usage.total_tokens",
    }
)


def is_genai_event(name: object) -> bool:
    return isinstance(name, str) and name.startswith(GENAI_EVENT_PREFIX)


def genai_event_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """The non-content attributes of a GenAI event."""
    return {k: v for k, v in (attributes or {}).items() if k in _GENAI_EVENT_ATTRIBUTES}


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
_CHAIN_CUT = f"[{MAX_CHAIN} most recent exceptions of a longer chain]"
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
        if not out and stripped == _CHAIN_CUT:  # fixed text from format_exception
            out.append(_CHAIN_CUT)
            continue
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


_MAX_DEPTH = 16


def scrub_any(value: Any, redactor: Redactor, _depth: int = 0) -> Any:
    """Redact every string in a log body or attribute value (OpenTelemetry ``AnyValue``:
    nested maps and lists). Bytes and other objects are replaced by a placeholder."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redactor.redact(value)
    if _depth >= _MAX_DEPTH:
        return "[omitted]"
    if isinstance(value, Mapping):
        return {redactor.redact(str(k)): scrub_any(v, redactor, _depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        scrubbed = [scrub_any(v, redactor, _depth + 1) for v in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return f"[{type(value).__name__} omitted]"


def scrub_attributes(
    attributes: Mapping[str, Any] | None,
    redactor: Redactor,
    *,
    include_exception_messages: bool,
    scrub_value: Callable[[Any, Redactor], Any] = _scrub_value,
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
        if key == EXCEPTION_FORMATTED or is_genai_content_key(key):
            continue
        # The key can be the sensitive value (an email used as an attribute name).
        # RedactingFilter deletes those keys; redacting only the value would export
        # the key. Span attributes and Logs API records use this function.
        if redactor.matches(key):
            continue
        if key in _QUERY_KEYS or is_secret_key(key):
            out[key] = REDACTED if value else value
            continue
        if key in _URL_KEYS and isinstance(value, str):
            value = _with_route(key, value, route) if route is not None else _strip_query(value)
        if key in _MESSAGE_KEYS and not include_exception_messages:
            out[key] = REDACTED
            continue
        if (
            key == "exception.stacktrace"
            and isinstance(value, str)
            and value != REDACTED  # already replaced (e.g. by the logging filter)
            and not include_exception_messages
        ):
            if formatted_here:
                value = sanitize_stacktrace(value)  # already safe; idempotent
            else:
                type_name = attributes.get("exception.type")
                match = _EXCEPTION_LINE.match(type_name) if isinstance(type_name, str) else None
                value = f"{match.group(1) if match else 'Exception'}: {REDACTED}\n"
        out[key] = scrub_value(value, redactor)
    return out


class ScrubbingSpanExporter(SpanExporter):
    def __init__(self, delegate: SpanExporter, redactor: Redactor, *, include_exception_messages: bool = False):
        self._delegate = delegate
        self._redactor = redactor
        self._include_messages = include_exception_messages

    def _scrub(self, span: ReadableSpan) -> ReadableSpan:
        def scrub(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
            return scrub_attributes(attributes, self._redactor, include_exception_messages=self._include_messages)

        events = [
            Event(
                e.name,
                scrub(genai_event_attributes(e.attributes) if is_genai_event(e.name) else e.attributes),
                e.timestamp,
            )
            for e in span.events
        ]
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
            attributes=scrub(span.attributes),
            events=events,
            links=[Link(link.context, scrub(link.attributes)) for link in span.links],
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


_UNSCRUBBABLE = "[unscrubbable log record]"


class ScrubbingLogRecordProcessor(LogRecordProcessor):
    """Scrubs every log record in place before any later processor exports it.

    Must be the first processor (after any that only add attributes) on the
    LoggerProvider. Applies the span exporter's rules: the body and attributes are
    redacted, GenAI content attributes are dropped, a GenAI event (named ``gen_ai.*``)
    keeps only non-content attributes and loses its body, and exception text this
    package did not format is reduced to its type. Records from the standard library
    have already passed ``RedactingFilter``; redaction is idempotent, and the
    exception marker that filter sets keeps their stack traces.

    Never raises. A record that cannot be scrubbed keeps no content and the failure is
    counted on ``hmcts.telemetry.errors``.
    """

    def __init__(self, redactor: Redactor, *, include_exception_messages: bool = False) -> None:
        self._redactor = redactor
        self._include_messages = include_exception_messages

    def _scrub(self, record: Any) -> None:
        attributes = record.attributes
        name = getattr(record, "event_name", None) or (attributes or {}).get("event.name")
        if is_genai_event(name):
            record.body = None
            attributes = genai_event_attributes(attributes)
        elif record.body is not None:
            record.body = scrub_any(record.body, self._redactor)
        scrubbed = scrub_attributes(
            attributes,
            self._redactor,
            include_exception_messages=self._include_messages,
            scrub_value=scrub_any,
        )
        _replace_attributes(record, scrubbed)

    def on_emit(self, log_record: Any) -> None:
        record = getattr(log_record, "log_record", log_record)
        try:
            self._scrub(record)
        except Exception as exc:  # fail closed: keep the record, drop its content
            try:
                record.body = _UNSCRUBBABLE
                _replace_attributes(record, {})
            except Exception:  # pragma: no cover - last line of defence
                pass
            try:
                from hmcts_otel import _guard

                _guard.internal_error("log_record_scrub", exc)
            except Exception:  # pragma: no cover
                pass

    # opentelemetry-sdk < 1.37 called this hook ``emit``.
    emit = on_emit

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _replace_attributes(record: Any, attributes: dict[str, Any]) -> None:
    """Swap a record's attributes, keeping the SDK's bounded container (and its dropped count)."""
    current = record.attributes
    try:
        from opentelemetry.attributes import BoundedAttributes

        if isinstance(current, BoundedAttributes):
            replacement = BoundedAttributes(
                maxlen=current.maxlen,
                attributes=attributes,
                immutable=False,
                max_value_len=current.max_value_len,
                extended_attributes=True,
            )
            replacement.dropped = current.dropped
            record.attributes = replacement
            return
    except Exception:  # pragma: no cover - a plain dict is enough for every exporter
        pass
    record.attributes = attributes
