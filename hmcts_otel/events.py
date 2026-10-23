"""Lifecycle and analytics events.

Events are emitted as log records carrying ``microsoft.custom_event.name``, which
the Azure Monitor exporter sends to App Insights as custom events
(``customEvents`` / ``AppEvents``). Logs are not trace-sampled, so every event
arrives. Each event also increments the ``hmcts.events`` counter.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections.abc import Container, Iterable, Mapping

from hmcts_otel import _guard, _state
from hmcts_otel import conventions as c
from hmcts_otel._logging import EVENTS_LOGGER
from hmcts_otel.context import Journey, current_journey, get_request_id
from hmcts_otel.conventions import EventClass, EventSource
from hmcts_otel.schema import METADATA_KEYS, TelemetrySchema, check_name, default_schema

_ATTRIBUTE_LIKE = re.compile(r"[a-z][a-z0-9_.]{0,63}")
_events_log = logging.getLogger(EVENTS_LOGGER)
_diag_log = logging.getLogger("hmcts_otel")
# Keys can come from browsers via the relay, so the warn-once memory is bounded.
_MAX_WARNED = 1000
_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def _hash_key(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _describe_key(key: object, *, known: Container[str] | None = None) -> str:
    """How to name a dropped key in a log line.

    Keys from application code are attribute-like names and are shown. Keys that
    are not (free text) are shown only as a short hash, so their content is never
    logged. With ``known`` (keys from a browser via the relay), only a key in
    ``known`` (a registered schema key) is shown; every other key is hashed, because
    an attribute-like key such as ``jane_smith`` can still be personal data.
    """
    text = key if isinstance(key, str) else repr(type(key))
    if known is not None:
        return text if isinstance(key, str) and text in known else _hash_key(text)
    if _ATTRIBUTE_LIKE.fullmatch(text):
        return text
    return _hash_key(text)


def report_dropped(dropped: Iterable[tuple[str, str]], *, where: str, known_keys: Container[str] | None = None) -> None:
    """Count dropped attributes and warn once per (key, reason). Values are never logged. Never raises.

    Pass ``known_keys`` (the registered schema keys) for untrusted input, such as
    browser events from the relay: any other key is logged only as a hash.
    """
    try:
        counter = _state.instrument(
            "counter", c.METRIC_DROPPED_ATTRIBUTES, unit="{attribute}", description="Attributes rejected by the schema"
        )
        for key, reason in dropped:
            counter.add(1, {"reason": reason, "where": _guard.safe_dimension(where)})
            safe_key = _describe_key(key, known=known_keys)
            with _warned_lock:
                if (safe_key, reason) in _warned or len(_warned) >= _MAX_WARNED:
                    continue
                _warned.add((safe_key, reason))
            _diag_log.warning("hmcts_otel dropped attribute %r (%s) in %s", safe_key, reason, where)
    except Exception as exc:
        _guard.internal_error("report_dropped", exc)


def record_event(
    name: str,
    attributes: Mapping[str, object] | None = None,
    *,
    event_class: EventClass | None = None,
    source: EventSource = EventSource.SERVER,
    journey: Journey | None = None,
    duration_ms: float | None = None,
    schema: TelemetrySchema | None = None,
) -> None:
    """Record a named event with validated attributes.

    If the event is registered in the schema, its class and permitted attributes
    come from the registration. Unregistered events may use any registered attribute.
    Never raises outside strict mode.
    """
    try:
        check_name(name)
    except (TypeError, ValueError) as exc:
        _guard.invalid("event_name", str(exc))
        return
    try:
        _record(name, attributes, event_class, source, journey, duration_ms, schema or default_schema)
    except Exception as exc:
        if _guard.strict():
            raise
        _guard.internal_error("record_event", exc)


def _record(
    name: str,
    attributes: Mapping[str, object] | None,
    event_class: EventClass | None,
    source: EventSource,
    journey: Journey | None,
    duration_ms: float | None,
    schema: TelemetrySchema,
) -> None:
    spec = schema.get_event(name)
    allowed = spec.attributes if spec is not None else None
    cls = spec.event_class if spec is not None else (event_class or EventClass.OPERATIONAL)
    source = EventSource(source)
    browser = source is EventSource.BROWSER

    # Metadata is never caller-settable. The request ID comes from the request
    # context unless the event registers it explicitly. Browser input is held to the
    # event's registered attributes only: core keys such as the outcome and error
    # code are accepted from a browser only where the event lists them.
    reserved = set(METADATA_KEYS)
    if allowed is None or c.REQUEST_ID not in allowed:
        reserved.add(c.REQUEST_ID)
    clean, dropped = schema.validate(
        attributes or {},
        allowed=(allowed or ()) if browser else allowed,
        reserved=reserved,
        core_exempt=not browser,
    )
    if dropped:
        # Browser keys are untrusted: only registered and metadata keys are logged as written.
        known = frozenset(schema.attributes) | frozenset(METADATA_KEYS) if browser else None
        report_dropped(dropped, where=f"event:{name}", known_keys=known)

    # Metadata is applied last, so it is authoritative.
    extra: dict[str, object] = dict(clean)
    extra[c.CUSTOM_EVENT_NAME] = name
    extra[c.EVENT_CLASS] = cls.value
    extra[c.EVENT_SOURCE] = source.value
    current = journey or current_journey()
    if current is not None:
        extra.update(current.attributes())
    rid = get_request_id()
    if rid is not None and c.REQUEST_ID not in clean:
        extra[c.REQUEST_ID] = rid
    if duration_ms is not None:
        extra[c.DURATION_MS] = float(duration_ms)

    _events_log.info(name, extra=extra)

    metric_attrs = {"event": _guard.safe_dimension(name), "class": cls.value, "source": source.value}
    _state.instrument("counter", c.METRIC_EVENTS, unit="{event}", description="Events recorded").add(1, metric_attrs)
    if duration_ms is not None:
        _state.instrument(
            "histogram", c.METRIC_EVENT_DURATION, unit="s", description="Duration reported with an event"
        ).record(float(duration_ms) / 1000.0, metric_attrs)
