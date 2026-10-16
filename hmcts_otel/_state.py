"""Process-wide state owned by setup_telemetry / shutdown_telemetry."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import metrics, trace

from hmcts_otel.redaction import Redactor, default_redactor
from hmcts_otel.settings import TelemetrySettings

INSTRUMENTATION_NAME = "hmcts_otel"


@dataclass
class _State:
    configured: bool = False
    settings: TelemetrySettings | None = None
    redactor: Redactor = field(default_factory=lambda: default_redactor)
    tracer_provider: Any = None
    meter_provider: Any = None
    logger_provider: Any = None
    # Handlers this package attached to the root logger, removed on shutdown.
    handlers: list[logging.Handler] = field(default_factory=list)
    # Handlers created by others (e.g. the distro) that we added filters to.
    filtered_handlers: list[logging.Handler] = field(default_factory=list)
    owns_providers: bool = False
    instruments: dict[tuple[int, str], Any] = field(default_factory=dict)


STATE = _State()
LOCK = threading.RLock()


def reset_state() -> None:
    """Return STATE to its defaults, in place (other modules hold a reference to it)."""
    STATE.__dict__.update(_State().__dict__)


def get_tracer(name: str = INSTRUMENTATION_NAME) -> trace.Tracer:
    if STATE.tracer_provider is not None:
        return STATE.tracer_provider.get_tracer(name)
    return trace.get_tracer(name)


def get_meter(name: str = INSTRUMENTATION_NAME) -> metrics.Meter:
    if STATE.meter_provider is not None:
        return STATE.meter_provider.get_meter(name)
    return metrics.get_meter(name)


def instrument(kind: str, name: str, **kwargs: Any) -> Any:
    """Create (once per meter provider) and return a metric instrument."""
    provider = STATE.meter_provider or metrics.get_meter_provider()
    key = (id(provider), name)
    existing = STATE.instruments.get(key)
    if existing is not None:
        return existing
    meter = get_meter()
    factory = {"histogram": meter.create_histogram, "counter": meter.create_counter}[kind]
    created = factory(name, **kwargs)
    STATE.instruments[key] = created
    return created


def current_redactor() -> Redactor:
    return STATE.redactor


def capture_exception_messages() -> bool:
    return bool(STATE.settings and STATE.settings.capture_exception_messages)
