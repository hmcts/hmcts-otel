"""Attribute, event and metric names shared by every service using hmcts-otel.

These names are the contract dashboards, alerts and KQL queries are written
against. Changing one is a breaking change.
"""

from __future__ import annotations

from enum import StrEnum


class Outcome(StrEnum):
    """How a stage ended."""

    SUCCESS = "success"
    # An expected, handled failure with a controlled error code (e.g. validation).
    FAILURE = "failure"
    # An unexpected exception.
    ERROR = "error"
    CANCELLED = "cancelled"


class EventClass(StrEnum):
    """Why an event is collected, which decides whether consent gates it."""

    # Failures, lifecycle and durations. Sent regardless of analytics consent.
    OPERATIONAL = "operational"
    # Feature usage and adoption. Sent only with analytics consent.
    ANALYTICS = "analytics"


class EventSource(StrEnum):
    SERVER = "server"
    BROWSER = "browser"


# Journey identifiers: pseudonymous, system-generated, never business references.
JOURNEY_TYPE = "hmcts.journey.type"
JOURNEY_ID = "hmcts.journey.id"
JOURNEY_PARENT_ID = "hmcts.journey.parent_id"

STAGE = "hmcts.stage"
OUTCOME = "hmcts.outcome"
ERROR_CODE = "hmcts.error.code"
EVENT_CLASS = "hmcts.event.class"
EVENT_SOURCE = "hmcts.event.source"
DURATION_MS = "hmcts.duration_ms"
# Reference to a record in an external system, e.g. an Azure Speech job ID.
DEPENDENCY_REF = "hmcts.dependency.ref"
REQUEST_ID = "hmcts.request.id"

# Log records carrying this attribute are exported to App Insights as custom
# events (customEvents / AppEvents) rather than traces.
CUSTOM_EVENT_NAME = "microsoft.custom_event.name"

# Metric names. Units follow OpenTelemetry conventions (seconds for durations).
METRIC_STAGE_DURATION = "hmcts.stage.duration"
METRIC_EVENTS = "hmcts.events"
METRIC_EVENT_DURATION = "hmcts.event.duration"
METRIC_DROPPED_ATTRIBUTES = "hmcts.telemetry.dropped_attributes"
METRIC_TELEMETRY_ERRORS = "hmcts.telemetry.errors"

# Histogram bucket boundaries for duration metrics, in seconds. The App Insights
# exporter sends only count/sum/min/max; buckets matter for other backends.
DURATION_BUCKETS_S: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    30,
    60,
    120,
    300,
    600,
    1800,
    3600,
)

# --- AI usage metering (OBS-5) ------------------------------------------------------
# Usage only: token counts and audio seconds. No prices and no cost: cost comes from
# Azure and GCP billing, allocated by usage share at query time. No prompt or
# response content is ever recorded.
#
# GenAI names follow the OpenTelemetry GenAI semantic conventions at this version,
# the first with ``gen_ai.provider.name`` (which replaced ``gen_ai.system``). Checked
# unchanged against opentelemetry-semantic-conventions 0.66b1 (schema 1.44.0), where
# the GenAI conventions are marked as moved to the semantic-conventions-genai repo.
GEN_AI_SEMCONV_VERSION = "1.37.0"

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_TOKEN_TYPE = "gen_ai.token.type"
# OpenTelemetry's general attribute: the exception class name of a failed call.
ERROR_TYPE = "error.type"

AI_FEATURE = "hmcts.ai.feature"
AI_FALLBACK = "hmcts.ai.fallback"
SPEECH_MODE = "hmcts.speech.mode"

# Value used for a missing feature, or a provider, model, mode or outcome that failed
# validation.
UNKNOWN = "unknown"


class TokenType(StrEnum):
    INPUT = "input"
    OUTPUT = "output"


class SpeechMode(StrEnum):
    """How audio was transcribed (Azure Speech billing differs by mode)."""

    REALTIME = "realtime"
    FAST = "fast"
    BATCH = "batch"


# Semantic-convention histogram, one point per token type per call.
METRIC_GEN_AI_TOKEN_USAGE = "gen_ai.client.token.usage"
# One per call, with the error type on failures.
METRIC_AI_REQUESTS = "hmcts.ai.requests"
# Speech has no semantic convention.
METRIC_SPEECH_AUDIO_DURATION = "hmcts.speech.audio.duration"

# Bucket boundaries recommended by the semantic conventions for gen_ai.client.token.usage.
GEN_AI_TOKEN_BUCKETS: tuple[int, ...] = (
    1,
    4,
    16,
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    67108864,
)

# Set on exception events whose stack trace hmcts-otel built from the exception object.
# Exception text without it came from outside the package and is exported as the
# type only (see _scrubbing.scrub_attributes). Removed before export.
EXCEPTION_FORMATTED = "hmcts.exception.formatted"

REDACTED = "[REDACTED]"
