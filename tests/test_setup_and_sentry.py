import logging
from typing import Any

import pytest
from pydantic import SecretStr

import hmcts_otel._setup as setup_mod
from hmcts_otel import TelemetrySettings, is_configured, setup_telemetry, shutdown_telemetry
from hmcts_otel.redaction import default_redactor
from hmcts_otel.sentry import scrub_breadcrumb, scrub_event


def test_settings_from_standard_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SERVICE_NAME", "transcribe-api")
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=x")
    monkeypatch.setenv("HMCTS_OTEL_SAMPLING_RATIO", "0.5")
    monkeypatch.setenv("APP_VERSION", "1.2.3")
    s = TelemetrySettings()
    assert s.service_name == "transcribe-api" and s.sampling_ratio == 0.5 and s.service_version == "1.2.3"
    assert s.exports_to_azure
    assert "InstrumentationKey" not in repr(s)  # secret not printed


def test_settings_ignore_otel_sampler_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    # The distro ignores OTEL_TRACES_SAMPLER_ARG unless OTEL_TRACES_SAMPLER is set; we do not read it at all,
    # so heritage settings like 0.05 cannot silently reduce journey sampling.
    monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "0.05")
    assert TelemetrySettings().sampling_ratio == 1.0


def test_sampling_ratio_bounds() -> None:
    lenient = TelemetrySettings(sampling_ratio=1.5)
    assert lenient.sampling_ratio == 1.0 and lenient.invalid_fields == ["sampling_ratio"]
    with pytest.raises(ValueError):
        TelemetrySettings(sampling_ratio=1.5, strict=True)


def test_no_exporters_means_stdout_only() -> None:
    settings = setup_telemetry(TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None))
    assert is_configured() and settings.service_name == "t"
    assert setup_telemetry() is settings  # idempotent
    assert setup_mod.STATE.tracer_provider is None
    shutdown_telemetry()
    assert not is_configured()
    shutdown_telemetry()  # idempotent


def test_azure_path_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_configure(**kwargs: Any) -> None:
        import os

        calls.update(kwargs)
        calls["OTEL_TRACES_EXPORTER"] = os.environ.get("OTEL_TRACES_EXPORTER")
        handler = logging.Handler()
        handler.name = "distro-otel-handler"
        logging.getLogger().addHandler(handler)

    class FakeExporter:
        def __init__(self, **kwargs: Any) -> None:
            calls["exporter_kwargs"] = kwargs

        def export(self, spans):  # pragma: no cover - not exercised
            return None

        def shutdown(self) -> None:
            return None

    import azure.monitor.opentelemetry as distro
    import azure.monitor.opentelemetry.exporter as exporter_mod

    monkeypatch.setattr(distro, "configure_azure_monitor", fake_configure)
    monkeypatch.setattr(exporter_mod, "AzureMonitorTraceExporter", FakeExporter)
    monkeypatch.setattr(setup_mod.trace, "set_tracer_provider", lambda tp: calls.setdefault("global_tp", tp))

    setup_telemetry(
        TelemetrySettings(
            service_name="transcribe-api",
            connection_string=SecretStr("InstrumentationKey=00000000-0000-0000-0000-000000000000"),
            sentry_dsn=None,
        )
    )
    assert calls["disable_tracing"] is True
    # The distro ignores disable_tracing in favour of this variable; it must be set
    # during the call and restored afterwards.
    import os

    assert calls["OTEL_TRACES_EXPORTER"] == "none" and "OTEL_TRACES_EXPORTER" not in os.environ
    assert calls["instrumentation_options"]["fastapi"] == {"enabled": False}
    assert calls["logger_name"] == ""
    assert calls["exporter_kwargs"]["connection_string"].startswith("InstrumentationKey=")
    assert calls["global_tp"] is setup_mod.STATE.tracer_provider
    distro_handler = next(h for h in logging.getLogger().handlers if h.name == "distro-otel-handler")
    assert {type(f).__name__ for f in distro_handler.filters} == {"ContextFilter", "RedactingFilter"}


def test_sentry_event_scrubbing() -> None:
    event = {
        "request": {
            "method": "POST",
            "url": "https://app/api/x?token=abc",
            "data": {"transcript": "private"},
            "headers": {"Authorization": "Bearer xyz"},
            "cookies": {"session": "s"},
        },
        "user": {"id": "oid-1", "email": "judge@justice.gov.uk"},
        "exception": {"values": [{"type": "ValueError", "value": "bad input from judge@justice.gov.uk"}]},
        "extra": {"note": "contact x@y.com"},
    }
    out = scrub_event(event, default_redactor)
    assert out["request"] == {"method": "POST", "url": "https://app/api/x"}
    assert out["user"] == {"id": "oid-1"}
    assert out["exception"]["values"][0]["value"] == "[REDACTED]"  # messages off by default
    kept = scrub_event(
        {"exception": {"values": [{"type": "ValueError", "value": "bad input from judge@justice.gov.uk"}]}},
        default_redactor,
        include_exception_messages=True,
    )
    assert kept["exception"]["values"][0]["value"] == "bad input from [REDACTED]"
    assert out["extra"]["note"] == "contact [REDACTED]"


def test_sentry_breadcrumb_scrubbing() -> None:
    crumb = {"category": "http", "data": {"url": "https://a/b?q=secret", "note": "a@b.co"}, "message": "x@y.com"}
    out = scrub_breadcrumb(crumb, default_redactor)
    assert out == {"category": "http", "data": {"url": "https://a/b", "note": "[REDACTED]"}, "message": "[REDACTED]"}


def test_sentry_init_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    from hmcts_otel.sentry import init_sentry

    captured: dict[str, Any] = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: captured.update(kw))
    assert not init_sentry(TelemetrySettings(sentry_dsn=None))
    assert init_sentry(TelemetrySettings(sentry_dsn=SecretStr("https://k@o.ingest.sentry.io/1"), environment="stg"))
    assert captured["send_default_pii"] is False
    assert captured["include_local_variables"] is False
    assert captured["max_request_body_size"] == "never"
    assert captured["traces_sample_rate"] == 0.0 and captured["environment"] == "stg"


def test_shutdown_is_bounded_when_export_blocks() -> None:
    import time

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from hmcts_otel import stage

    class SlowExporter(InMemorySpanExporter):
        def shutdown(self) -> None:
            time.sleep(5)

    setup_telemetry(
        TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None),
        span_exporter=SlowExporter(),
        set_global_providers=False,
    )
    with stage("work"):
        pass
    started = time.monotonic()
    shutdown_telemetry(timeout_millis=300)
    assert time.monotonic() - started < 2
    assert not is_configured()


def test_otel_sdk_disabled_means_stdout_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.monitor.opentelemetry as distro

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setattr(distro, "configure_azure_monitor", lambda **kw: pytest.fail("distro must not run"))
    setup_telemetry(
        TelemetrySettings(
            service_name="t",
            connection_string=SecretStr("InstrumentationKey=00000000-0000-0000-0000-000000000000"),
            sentry_dsn=None,
        )
    )
    assert is_configured() and setup_mod.STATE.tracer_provider is None


def test_real_distro_builds_exactly_one_trace_pipeline() -> None:
    """Runs the real distro (in a subprocess: global providers are set-once per process).

    Regression: the distro overwrote disable_tracing and built a second tracer provider
    with an unscrubbed exporter. Exactly one AzureMonitorTraceExporter must exist, and it
    must sit behind the scrubbing exporter.
    """
    import subprocess
    import sys
    from pathlib import Path

    script = """
import gc, logging
from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
from opentelemetry import trace
from pydantic import SecretStr
from hmcts_otel import TelemetrySettings, setup_telemetry, shutdown_telemetry
import hmcts_otel._setup as s
setup_telemetry(TelemetrySettings(service_name="t", sentry_dsn=None, connection_string=SecretStr(
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=http://127.0.0.1:9/")))
exporters = [o for o in gc.get_objects() if isinstance(o, AzureMonitorTraceExporter)]
wrapped = [o for o in exporters if any(type(r).__name__ == "ScrubbingSpanExporter" for r in gc.get_referrers(o))]
print("RESULT", len(exporters), len(wrapped), trace.get_tracer_provider() is s.STATE.tracer_provider)
shutdown_telemetry(500)
"""
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**__import__("os").environ, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=120,
    )
    line = next((x for x in result.stdout.splitlines() if x.startswith("RESULT")), result.stderr[-2000:])
    assert line == "RESULT 1 1 True", line
