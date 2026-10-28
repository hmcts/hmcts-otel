from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

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


@pytest.fixture
def run_isolated(tmp_path: Path) -> Callable[[str], str]:
    """Run a Python script in a fresh interpreter and return its ``RESULT`` line.

    For tests of the real distro and exporter: global OpenTelemetry providers are
    set once per process. ``TMPDIR`` is the test's own directory, so the Azure
    exporter's offline storage (batches it failed to upload, re-sent on its next
    start) cannot carry over from one run or test to another.
    """
    root = Path(__file__).resolve().parent.parent

    def run(script: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root), "TMPDIR": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        return next((x for x in result.stdout.splitlines() if x.startswith("RESULT")), result.stderr[-2000:])

    return run
