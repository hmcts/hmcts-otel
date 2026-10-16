from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator

import pytest

from hmcts_otel import TelemetrySchema, is_configured, shutdown_telemetry
from hmcts_otel.testing import TelemetryCapture, capture_telemetry


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Developer machines may export these; tests must never reach Azure or Sentry.
    for var in ("APPLICATIONINSIGHTS_CONNECTION_STRING", "SENTRY_DSN", "OTEL_SERVICE_NAME"):
        monkeypatch.delenv(var, raising=False)
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    if is_configured():
        shutdown_telemetry()
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def tel() -> Iterator[TelemetryCapture]:
    with capture_telemetry() as capture:
        yield capture


@pytest.fixture
def schema() -> TelemetrySchema:
    s = TelemetrySchema()
    s.attribute("file.size_bytes", int, description="Upload size")
    s.attribute("file.format", allowed_values={"wav", "mp3"})
    s.attribute("speech.region", pattern=r"[a-z]+")
    return s


@pytest.fixture
def journey_id() -> str:
    return str(uuid.uuid4())
