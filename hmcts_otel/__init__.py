"""hmcts-otel: privacy-safe OpenTelemetry for HMCTS Python services on Azure."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from hmcts_otel import conventions
from hmcts_otel._setup import is_configured, setup_logging, setup_telemetry, shutdown_telemetry
from hmcts_otel._state import get_meter, get_tracer
from hmcts_otel.context import (
    Journey,
    current_journey,
    get_request_id,
    is_valid_request_id,
    journey,
    request_id_context,
)
from hmcts_otel.conventions import EventClass, EventSource, Outcome
from hmcts_otel.events import record_event
from hmcts_otel.propagation import background_span, capture_trace_context, links_from
from hmcts_otel.redaction import RedactionRule, Redactor, default_redactor, redact
from hmcts_otel.schema import AttributeSpec, EventSpec, TelemetrySchema, default_schema
from hmcts_otel.settings import TelemetrySettings
from hmcts_otel.stages import StageHandle, stage

try:
    __version__ = version("hmcts-otel")
except PackageNotFoundError:  # pragma: no cover - running from source
    __version__ = "0.0.0"

__all__ = [
    "AttributeSpec",
    "background_span",
    "EventClass",
    "EventSource",
    "EventSpec",
    "Journey",
    "Outcome",
    "RedactionRule",
    "Redactor",
    "StageHandle",
    "TelemetrySchema",
    "TelemetrySettings",
    "capture_trace_context",
    "conventions",
    "current_journey",
    "default_redactor",
    "default_schema",
    "get_meter",
    "get_request_id",
    "get_tracer",
    "is_configured",
    "is_valid_request_id",
    "journey",
    "links_from",
    "record_event",
    "redact",
    "request_id_context",
    "setup_logging",
    "setup_telemetry",
    "shutdown_telemetry",
    "stage",
    "__version__",
]
