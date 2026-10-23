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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hmcts_otel import conventions as c
from hmcts_otel._setup import is_configured, setup_telemetry, shutdown_telemetry
from hmcts_otel.redaction import Redactor, default_redactor
from hmcts_otel.settings import TelemetrySettings


def _log_record(item: Any) -> Any:
    # Exported items are ReadableLogRecord (or LogData on older SDKs); both expose .log_record.
    return getattr(item, "log_record", item)


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

    # Assertions -------------------------------------------------------------------

    def assert_no_prohibited_content(self, *needles: str, redactor: Redactor | None = None) -> None:
        """Fail if any needle, or anything matching a redaction rule, appears in exported telemetry.

        The failure message names the needle index or rule, never the matched content.
        """
        text = self.all_exported_text()
        problems = [f"needle #{i}" for i, needle in enumerate(needles) if needle and needle in text]
        problems += [f"rule '{name}'" for name in (redactor or default_redactor).matches(text)]
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
