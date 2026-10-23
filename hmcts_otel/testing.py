"""Test support: capture everything a code path would export, and assert it is clean.

Example (pytest)::

    from hmcts_otel.testing import capture_telemetry

    def test_submission_emits_no_content():
        with capture_telemetry() as tel:
            submit_hearing(transcript="the appellant said ...")
        tel.assert_no_prohibited_content("the appellant said")
        assert tel.events("submission.completed")
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hmcts_otel import conventions as c
from hmcts_otel._setup import is_configured, setup_telemetry, shutdown_telemetry
from hmcts_otel.redaction import RedactionRule, Redactor, default_redactor
from hmcts_otel.settings import TelemetrySettings


def _log_record(item: Any) -> Any:
    # Exported items are ReadableLogRecord (or LogData on older SDKs); both expose .log_record.
    return getattr(item, "log_record", item)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _attribute_strings(where: str, attributes: Any) -> Iterator[tuple[str, str]]:
    for k, (key, value) in enumerate(dict(attributes or {}).items()):
        yield f"{where} #{k} key", str(key)
        yield f"{where} #{k}", _as_text(value)


def _matching_rules(rules: Iterable[RedactionRule], text: str) -> list[str]:
    """Names of the rules matching anywhere in ``text``, however long it is.

    Each string is searched whole: the default rules are linear-time and unbounded, so a
    match of any length (e.g. a 70,000-character URL with a query) is found. Windows of a
    fixed size would miss a match longer than their overlap.
    """
    return [rule.name for rule in rules if rule.pattern.search(text)]


@dataclass
class TelemetryCapture:
    spans: InMemorySpanExporter = field(default_factory=InMemorySpanExporter)
    logs: InMemoryLogRecordExporter = field(default_factory=InMemoryLogRecordExporter)
    metrics: InMemoryMetricReader = field(default_factory=InMemoryMetricReader)

    # Accessors --------------------------------------------------------------------

    def finished_spans(self) -> list[Any]:
        return list(self.spans.get_finished_spans())

    def log_records(self) -> list[Any]:
        return [_log_record(item) for item in self.logs.get_finished_logs()]

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        """Attributes of custom events, optionally filtered by event name."""
        found = []
        for record in self.log_records():
            attrs = dict(record.attributes or {})
            event_name = attrs.get(c.CUSTOM_EVENT_NAME)
            if event_name is None:
                continue
            if name is None or event_name == name:
                found.append(attrs)
        return found

    def event_names(self) -> list[str]:
        return [str(e[c.CUSTOM_EVENT_NAME]) for e in self.events()]

    def metric_points(self, name: str) -> list[dict[str, Any]]:
        """Data points for a metric as ``{"attributes": {...}, ...}`` with the point's
        ``value`` (counters) or ``count``, ``sum``, ``min``, ``max``, ``bucket_counts``
        and ``explicit_bounds`` (histograms)."""
        data = self.metrics.get_metrics_data()
        points: list[dict[str, Any]] = []
        if data is None:
            return points
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name != name:
                        continue
                    for p in metric.data.data_points:
                        entry: dict[str, Any] = {"attributes": dict(p.attributes or {})}
                        for attr in ("value", "count", "sum", "min", "max", "bucket_counts", "explicit_bounds"):
                            if hasattr(p, attr):
                                entry[attr] = getattr(p, attr)
                        points.append(entry)
        return points

    def all_exported_text(self) -> str:
        """Every string that would leave the process, concatenated, for content assertions."""
        parts: list[str] = []
        for span in self.finished_spans():
            parts.append(span.name)
            parts.append(json.dumps(dict(span.attributes or {}), default=str))
            parts.append(json.dumps(dict(span.resource.attributes or {}), default=str))
            for link in span.links:
                parts.append(json.dumps(dict(link.attributes or {}), default=str))
            if span.status.description:
                parts.append(span.status.description)
            for event in span.events:
                parts.append(event.name)
                parts.append(json.dumps(dict(event.attributes or {}), default=str))
        for record in self.log_records():
            parts.append(str(record.body))
            parts.append(json.dumps(dict(record.attributes or {}), default=str))
        data = self.metrics.get_metrics_data()
        if data is not None:
            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        for p in metric.data.data_points:
                            parts.append(json.dumps(dict(p.attributes or {}), default=str))
        return "\n".join(parts)

    def exported_strings(self) -> Iterator[tuple[str, str]]:
        """Every exported string with where it came from, e.g. ``("log record #3 body", ...)``.

        The location names the part (span, event, link, log record, metric point and the
        index of the attribute), never its content. Non-string values are given as JSON.
        """
        for i, span in enumerate(self.finished_spans()):
            where = f"span #{i}"
            yield f"{where} name", str(span.name)
            yield from _attribute_strings(f"{where} attribute", span.attributes)
            if span.status.description:
                yield f"{where} status", span.status.description
            for j, event in enumerate(span.events):
                yield f"{where} event #{j} name", str(event.name)
                yield from _attribute_strings(f"{where} event #{j} attribute", event.attributes)
            for j, link in enumerate(span.links):
                yield from _attribute_strings(f"{where} link #{j} attribute", link.attributes)
        for i, record in enumerate(self.log_records()):
            where = f"log record #{i}"
            if record.body is not None:
                yield f"{where} body", _as_text(record.body)
            event_name = getattr(record, "event_name", None)
            if event_name:
                yield f"{where} event name", str(event_name)
            yield from _attribute_strings(f"{where} attribute", record.attributes)
        data = self.metrics.get_metrics_data()
        if data is not None:
            for rm in data.resource_metrics:
                for sm in rm.scope_metrics:
                    for metric in sm.metrics:
                        for j, p in enumerate(metric.data.data_points):
                            yield from _attribute_strings(f"metric {metric.name!r} point #{j} attribute", p.attributes)

    # Assertions -------------------------------------------------------------------

    def assert_no_prohibited_content(self, *needles: str, redactor: Redactor | None = None) -> None:
        """Fail if any needle, or anything matching a redaction rule, appears in exported telemetry.

        Needles are searched for in all exported text. Rules are checked against each
        exported string separately and in full (a long string in overlapping windows),
        so a match in a late record or at the end of a long string is found. The
        failure message names the needle index, or the rule and where it matched, never
        the matched content.
        """
        text = self.all_exported_text()
        problems = [f"needle #{i}" for i, needle in enumerate(needles) if needle and needle in text]
        rules = (redactor or default_redactor).rules
        for where, value in self.exported_strings():
            problems += [f"rule '{name}' in {where}" for name in _matching_rules(rules, value)]
        if problems:
            raise AssertionError("prohibited content in exported telemetry: " + ", ".join(problems))

    def clear(self) -> None:
        self.spans.clear()
        self.logs.clear()
        self.metrics.get_metrics_data()  # collecting resets cumulative state for new readings


@contextmanager
def capture_telemetry(
    settings: TelemetrySettings | None = None, *, redactor: Redactor | None = None
) -> Iterator[TelemetryCapture]:
    """Set up telemetry with in-memory exporters for the duration of the block.

    Strict mode is on by default, so invalid stage names, error codes, event names
    and journey IDs raise ``ValueError``. Any existing configuration is shut down first. Global OpenTelemetry providers
    are not replaced, so captures can be repeated within one test session.
    """
    # Restore whatever configuration was active before (e.g. set up when the
    # application module was imported), so test outcomes do not depend on order.
    from hmcts_otel._state import STATE

    previous = (STATE.settings, STATE.redactor) if is_configured() else None
    if previous is not None:
        shutdown_telemetry()
    settings = settings or TelemetrySettings(
        service_name="test",
        environment="test",
        connection_string=None,
        sentry_dsn=None,
        log_format="json",
        strict=True,  # misuse raises in tests; production degrades quietly
    )
    capture = TelemetryCapture()
    setup_telemetry(
        settings,
        span_exporter=capture.spans,
        log_exporter=capture.logs,
        metric_readers=[capture.metrics],
        set_global_providers=False,
        redactor=redactor,
    )
    try:
        yield capture
    finally:
        shutdown_telemetry()
        if previous is not None and previous[0] is not None and not previous[0].exports_to_azure:
            setup_telemetry(previous[0], redactor=previous[1])
