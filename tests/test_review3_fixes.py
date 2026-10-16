"""Regression tests for the third review: M5 and L1 completed, N1–N3."""

from __future__ import annotations

import ast
import io
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

import hmcts_otel._setup as setup_mod
from hmcts_otel import TelemetrySettings, get_tracer, is_configured, setup_telemetry, shutdown_telemetry
from hmcts_otel._scrubbing import ScrubbingSpanExporter, sanitize_stacktrace
from hmcts_otel.redaction import default_redactor
from hmcts_otel.testing import capture_telemetry

ROOT = Path(__file__).resolve().parent.parent


def _secret() -> str:
    # Built at runtime so the literal never appears in a source line of a stack trace.
    return "appellant " + "SECRET" + "CONTENT"


# M5 — every way of building settings survives bad values -----------------------------------


def test_settings_constructor_survives_bad_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HMCTS_OTEL_SAMPLING_RATIO", "abc")
    monkeypatch.setenv("HMCTS_OTEL_ENABLE_LIVE_METRICS", "maybe")
    settings = TelemetrySettings(service_name="worker")  # the README / worker-example form
    assert settings.service_name == "worker" and settings.sampling_ratio == 1.0
    assert settings.enable_live_metrics is False
    assert settings.invalid_fields == ["enable_live_metrics", "sampling_ratio"]


def test_settings_constructor_strict_from_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HMCTS_OTEL_SAMPLING_RATIO", "abc")
    monkeypatch.setenv("HMCTS_OTEL_STRICT", "true")
    with pytest.raises(ValueError):
        TelemetrySettings(service_name="worker")


def test_setup_reports_invalid_fields_of_passed_settings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HMCTS_OTEL_SAMPLING_RATIO", "abc")
    with caplog.at_level(logging.ERROR, logger="hmcts_otel"):
        setup_telemetry(TelemetrySettings(service_name="worker", connection_string=None, sentry_dsn=None))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "sampling_ratio" in text and "abc" not in text


def test_worker_example_starts_with_bad_env() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "HMCTS_OTEL_SAMPLING_RATIO": "abc"}
    env.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
    code = "import examples.worker as w; from hmcts_otel import TelemetrySettings; TelemetrySettings(service_name='x')"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode()


# L1 — exceptions on spans are formatted from the object -----------------------------------


def _crafted_message(secret: str) -> str:
    return (
        "bad input\n\nDuring handling of the above exception, another exception occurred:\n\n"
        'Traceback (most recent call last):\n  File "x.py", line 1, in f\n    '
        + secret
        + "\n"
        + secret.replace(" ", "")
        + "Error: x"
    )


@pytest.mark.parametrize("case", ["crafted", "syntax_real_filename", "chained"])
def test_instrumentation_recorded_exceptions_never_carry_text(case: str) -> None:
    secret = _secret()

    def boom() -> None:
        if case == "crafted":
            raise ValueError(_crafted_message(secret))
        if case == "syntax_real_filename":
            ast.parse("x = 1\n" + secret + " (", filename="template.py")
        try:
            raise KeyError(secret)
        except KeyError as inner:
            raise RuntimeError(secret) from inner

    with capture_telemetry() as tel:
        tracer = get_tracer("some.instrumentation")
        with pytest.raises(Exception):  # noqa: B017 - each case raises a different type
            # record_exception=True and set_status_on_exception=True: what instrumentations do
            with tracer.start_as_current_span("auto"):
                boom()
        (span,) = tel.finished_spans()
        (event,) = span.events
        assert event.attributes["exception.message"] == "[REDACTED]"
        assert event.attributes["exception.stacktrace"].rstrip().endswith(": [REDACTED]")
        assert "SECRET" not in tel.all_exported_text()


def test_hook_is_inactive_when_not_configured() -> None:
    assert not is_configured()
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(sink))
    with pytest.raises(ValueError):
        with provider.get_tracer("t").start_as_current_span("s"):
            raise ValueError("plain")
    (event,) = sink.get_finished_spans()[0].events
    assert event.attributes["exception.message"] == "plain"  # standard SDK behaviour


def test_sanitizer_drops_syntax_error_source_for_real_filenames() -> None:
    secret = _secret()
    try:
        ast.parse("x = 1\n" + secret + " (", filename="template.py")
    except SyntaxError as exc:
        text = "".join(traceback.format_exception(exc))
    assert "SECRET" in text
    clean = sanitize_stacktrace(text)
    assert "SECRET" not in clean and "SyntaxError: [REDACTED]" in clean
    assert 'File "template.py", line 2' in clean  # location kept, source dropped


def test_exporter_defence_in_depth_still_scrubs_text() -> None:
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(sink, default_redactor)))
    with pytest.raises(SyntaxError):
        with provider.get_tracer("t").start_as_current_span("s"):
            ast.parse("x = 1\n" + _secret() + " (", filename="template.py")
    (event,) = sink.get_finished_spans()[0].events
    assert "SECRET" not in str(dict(event.attributes))


# N1 — filters from an earlier setup are replaced ----------------------------------------


def test_stale_filters_from_earlier_setup_are_replaced(capsys: pytest.CaptureFixture[str]) -> None:
    stream = io.StringIO()
    early = logging.StreamHandler(stream)
    logging.getLogger().addHandler(early)
    try:
        first = TelemetrySettings(
            service_name="t", connection_string=None, sentry_dsn=None, capture_exception_messages=True
        )
        setup_telemetry(first)
        shutdown_telemetry()

        from hmcts_otel import Redactor

        redactor = Redactor()
        redactor.add_rule("case_ref", r"\bAB\d{5}\b")
        setup_telemetry(TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None), redactor=redactor)
        secret = _secret()
        try:
            raise ValueError(secret)
        except ValueError:
            logging.getLogger("app").exception("failed case %s", "AB12345")
        out = capsys.readouterr().out + stream.getvalue()
        assert "SECRET" not in out and "AB12345" not in out
        assert "failed case [REDACTED]" in stream.getvalue()
        assert sum(type(f).__name__ == "RedactingFilter" for f in early.filters) == 1
    finally:
        logging.getLogger().removeHandler(early)


# N3 — a failed distro setup leaves nothing running --------------------------------------


def test_failed_distro_setup_stops_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.monitor.opentelemetry as distro
    from opentelemetry.sdk.metrics import MeterProvider

    registered: dict[str, Any] = {}
    stopped: list[str] = []

    class TrackedTracerProvider(TracerProvider):
        def shutdown(self) -> None:
            stopped.append("tracer")
            super().shutdown()

    class TrackedMeterProvider(MeterProvider):
        def shutdown(self, timeout_millis: float = 30_000, **kw: Any) -> None:
            stopped.append("meter")
            super().shutdown(timeout_millis=timeout_millis, **kw)

    def fake_configure(**kw: Any) -> None:
        registered["meter"] = TrackedMeterProvider()  # the distro registers a meter provider, then fails
        raise RuntimeError("distro failed")

    monkeypatch.setattr(setup_mod, "TracerProvider", TrackedTracerProvider)
    monkeypatch.setattr(setup_mod.trace, "set_tracer_provider", lambda tp: registered.__setitem__("tracer", tp))
    monkeypatch.setattr(
        setup_mod.trace, "get_tracer_provider", lambda: registered.get("tracer", trace.ProxyTracerProvider())
    )
    monkeypatch.setattr(
        setup_mod.metrics, "get_meter_provider", lambda: registered.get("meter", metrics.NoOpMeterProvider())
    )
    monkeypatch.setattr(distro, "configure_azure_monitor", fake_configure)

    setup_telemetry(
        TelemetrySettings(
            service_name="t",
            connection_string=SecretStr("InstrumentationKey=00000000-0000-0000-0000-000000000000"),
            sentry_dsn=None,
        )
    )
    assert is_configured() and setup_mod.STATE.tracer_provider is None
    assert sorted(stopped) == ["meter", "tracer"]


# L1 — provenance: only package-formatted exception text is exported ------------------------


def test_foreign_exception_text_is_reduced_to_its_type() -> None:
    secret = _secret()
    forged = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in f\n    ' + secret + "\n"
        "ValueError: [REDACTED]\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\n"
        '  File "y.py", line 2, in g\n    ' + secret + "\n"
        "KeyError: x\n"
    )
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(sink, default_redactor)))
    with provider.get_tracer("t").start_as_current_span("s") as span:
        # e.g. a library adding its own exception event from formatted text
        span.add_event("exception", {"exception.type": "KeyError", "exception.stacktrace": forged})
        # and one forging the marker inside a message would still lack the attribute itself
        span.add_event(
            "exception", {"exception.type": "X", "exception.stacktrace": forged + "hmcts.exception.formatted"}
        )
    events = sink.get_finished_spans()[0].events
    assert events[0].attributes["exception.stacktrace"] == "KeyError: [REDACTED]\n"
    assert events[1].attributes["exception.stacktrace"] == "X: [REDACTED]\n"
    assert "SECRET" not in str([dict(e.attributes) for e in events])


def test_package_formatted_exceptions_keep_frames_and_drop_the_marker() -> None:
    with capture_telemetry() as tel:
        with pytest.raises(ValueError):
            with get_tracer("auto").start_as_current_span("s"):
                raise ValueError(_secret())
        (event,) = tel.finished_spans()[0].events
        assert 'File "' in event.attributes["exception.stacktrace"]
        assert "hmcts.exception.formatted" not in event.attributes


def test_exception_text_in_logging_extra_is_not_trusted() -> None:
    forged = 'Traceback (most recent call last):\n  File "x.py", line 1, in f\n    ' + _secret()
    with capture_telemetry() as tel:
        logging.getLogger("app").error(
            "x",
            extra={"exception.stacktrace": forged, "exception.message": _secret(), "hmcts.exception.formatted": True},
        )
        (record,) = [r for r in tel.log_records() if r.body == "x"]
        assert record.attributes["exception.stacktrace"] == "[REDACTED]"
        assert "hmcts.exception.formatted" not in record.attributes
        tel.assert_no_prohibited_content(_secret())


def test_logged_exception_through_two_handlers_keeps_safe_stack(capsys: pytest.CaptureFixture[str]) -> None:
    stream = io.StringIO()
    extra_handler = logging.StreamHandler(stream)
    logging.getLogger().addHandler(extra_handler)
    try:
        with capture_telemetry() as tel:
            try:
                raise ValueError(_secret())
            except ValueError:
                logging.getLogger("app").exception("failed")
            (record,) = [r for r in tel.log_records() if r.body == "failed"]
            assert "ValueError: [REDACTED]" in record.attributes["exception.stacktrace"]
            assert 'File "' in record.attributes["exception.stacktrace"]
    finally:
        logging.getLogger().removeHandler(extra_handler)


# Code review (OBS-2): canonical request IDs; capture restores configuration ------------


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("0f8fad5b-d9cb-469f-a165-70867728950e", True),
        ("0f8fad5bd9cb469fa16570867728950e", True),
        ("07700900123", False),  # phone number: all digits are hex
        ("12345678901234567890", False),  # numeric reference
        ("ABCDE12345", False),  # hex-shaped case reference
        ("BA-1234-5678", False),
        ("Jane.Smith", False),
        (None, False),
    ],
)
def test_request_id_rule(value: object, valid: bool) -> None:
    from hmcts_otel import is_valid_request_id

    assert is_valid_request_id(value) is valid


def test_capture_restores_previous_configuration() -> None:
    settings = TelemetrySettings(service_name="app-under-test", connection_string=None, sentry_dsn=None)
    setup_telemetry(settings)
    with capture_telemetry():
        assert setup_mod.STATE.settings is not settings
    assert is_configured() and setup_mod.STATE.settings is settings


# Code review 2 (OBS-2) -------------------------------------------------------------------


def test_request_id_reaches_the_unhandled_exception_handler() -> None:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from hmcts_otel.fastapi import RequestIdMiddleware, request_id_for

    rid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    app = FastAPI()

    @app.get("/boom")
    def boom() -> None:
        raise ValueError("x")

    @app.exception_handler(Exception)
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        found = request_id_for(request) or ""
        return JSONResponse({"request_id": found}, status_code=500, headers={"X-Request-Id": found})

    app.add_middleware(RequestIdMiddleware)
    response = TestClient(app, raise_server_exceptions=False).get("/boom", headers={"X-Request-Id": rid})
    assert response.status_code == 500
    assert response.json() == {"request_id": rid}
    assert response.headers["X-Request-Id"] == rid


def test_background_span_is_a_linked_root() -> None:
    from hmcts_otel import background_span

    with capture_telemetry() as tel:
        tracer = get_tracer("t")
        with tracer.start_as_current_span("POST /minutes") as request_span:
            with background_span("generate minutes"):
                pass
        task = next(s for s in tel.finished_spans() if s.name == "generate minutes")
        assert task.parent is None
        assert task.context.trace_id != request_span.get_span_context().trace_id
        (link,) = task.links
        assert link.context.span_id == request_span.get_span_context().span_id


def test_setup_logging_bootstrap_then_full_setup_does_not_duplicate(capsys: pytest.CaptureFixture[str]) -> None:
    from hmcts_otel import setup_logging

    logging.getLogger().handlers.clear()  # a script process: no root handlers (pytest adds its own)
    setup_logging(TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None))
    assert not is_configured()
    logging.getLogger("script").warning("contact %s", "judge@justice.gov.uk")
    setup_telemetry(TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None))
    logging.getLogger("app").warning("once")
    out = capsys.readouterr().out
    assert "contact [REDACTED]" in out and "judge@justice.gov.uk" not in out
    assert out.count('"message": "once"') == 1
    assert sum(type(h).__name__ == "StdoutHandler" for h in logging.getLogger().handlers) == 1


def test_setup_logging_only_redacts_when_someone_else_owns_output() -> None:
    from hmcts_otel import setup_logging

    stream = io.StringIO()
    owner = logging.StreamHandler(stream)  # e.g. alembic's fileConfig handler
    logging.getLogger().addHandler(owner)
    try:
        setup_logging(TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None))
        assert not any(type(h).__name__ == "StdoutHandler" for h in logging.getLogger().handlers)
        logging.getLogger("alembic").warning("user %s", "judge@justice.gov.uk")
        assert "user [REDACTED]" in stream.getvalue()
    finally:
        logging.getLogger().removeHandler(owner)
