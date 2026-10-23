"""hmcts-otel: privacy-safe OpenTelemetry for HMCTS Python services on Azure."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from hmcts_otel import conventions
from hmcts_otel._setup import is_configured, setup_logging, setup_telemetry, shutdown_telemetry
from hmcts_otel._state import get_meter, get_tracer
from hmcts_otel.ai_usage import (
    ai_fallback,
    ai_feature,
    current_ai_feature,
    instrument_litellm,
    record_llm_usage,
    record_speech_usage,
)
from hmcts_otel.context import (
    Journey,
    current_journey,
    current_stage,
    get_request_id,
    is_valid_request_id,
    journey,
    request_id_context,
)
from hmcts_otel.conventions import EventClass, EventSource, Outcome, SpeechMode, TokenType
from hmcts_otel.events import record_event
from hmcts_otel.propagation import background_span, capture_trace_context, links_from
from hmcts_otel.redaction import RedactionRule, Redactor, default_redactor, redact
from hmcts_otel.schema import AttributeSpec, EventSpec, TelemetrySchema, default_schema
from hmcts_otel.settings import TelemetrySettings
from hmcts_otel.stages import StageHandle, client_failure_code, stage

try:
    __version__ = version("hmcts-otel")
except PackageNotFoundError:  # pragma: no cover - running from source
    __version__ = "0.0.0"

__all__ = [
    "AttributeSpec",
    "ai_fallback",
    "ai_feature",
    "background_span",
    "EventClass",
    "EventSource",
    "EventSpec",
    "Journey",
    "Outcome",
    "RedactionRule",
    "SpeechMode",
    "Redactor",
    "StageHandle",
    "TelemetrySchema",
    "TelemetrySettings",
    "TokenType",
    "capture_trace_context",
    "client_failure_code",
    "conventions",
    "current_ai_feature",
    "current_journey",
    "current_stage",
    "default_redactor",
    "default_schema",
    "get_meter",
    "get_request_id",
    "get_tracer",
    "instrument_litellm",
    "is_configured",
    "is_valid_request_id",
    "journey",
    "links_from",
    "record_event",
    "record_llm_usage",
    "record_speech_usage",
    "redact",
    "request_id_context",
    "setup_logging",
    "setup_telemetry",
    "shutdown_telemetry",
    "stage",
    "__version__",
]
