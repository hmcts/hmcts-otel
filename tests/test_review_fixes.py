"""Regression tests for the principal review findings (H1–H5, M1–M6)."""

from __future__ import annotations

import json
import logging
import traceback
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import BaseModel, SecretStr, ValidationError

import hmcts_otel._setup as setup_mod
import hmcts_otel.events as events_mod
import hmcts_otel.fastapi as fastapi_mod
import hmcts_otel.stages as stages_mod
from hmcts_otel import TelemetrySchema, TelemetrySettings, journey, record_event, setup_telemetry, stage
from hmcts_otel import conventions as c
from hmcts_otel._scrubbing import ScrubbingSpanExporter, sanitize_stacktrace
from hmcts_otel.fastapi import create_telemetry_relay_router, instrument_fastapi
from hmcts_otel.redaction import default_redactor, redact
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

SECRET = "the appellant stated that she fled"


@pytest.fixture
def lenient() -> Iterator[TelemetryCapture]:
    """Production-like capture: strict mode off."""
    settings = TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None, strict=False)
    with capture_telemetry(settings) as capture:
        yield capture


def _errors(tel: TelemetryCapture) -> dict[str, int]:
    return {p["attributes"]["kind"]: p["value"] for p in tel.metric_points(c.METRIC_TELEMETRY_ERRORS)}


class BusinessError(Exception):
    pass


def _break_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise RuntimeError("metrics backend broken")

    monkeypatch.setattr(stages_mod, "_duration_histogram", boom)


# H1 — telemetry never changes application behaviour -----------------------------------


@pytest.mark.parametrize("strict", [False, True])
def test_business_exception_is_never_replaced(monkeypatch: pytest.MonkeyPatch, strict: bool) -> None:
    settings = TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None, strict=strict)
    with capture_telemetry(settings) as tel:
        _break_metrics(monkeypatch)
        with pytest.raises(BusinessError):
            with stage("submit"):
                raise BusinessError("judge submission failed")
        assert _errors(tel) == {"internal.stage_finish": 1}


def test_internal_error_without_business_error_is_swallowed_in_production(
    lenient: TelemetryCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_metrics(monkeypatch)
    with stage("submit"):
        result = "ok"
    assert result == "ok"
    assert _errors(lenient) == {"internal.stage_finish": 1}


def test_internal_error_surfaces_in_strict_mode(tel: TelemetryCapture, monkeypatch: pytest.MonkeyPatch) -> None:
    _break_metrics(monkeypatch)
    with pytest.raises(RuntimeError):
        with stage("submit"):
            pass


def test_misuse_degrades_quietly_in_production(lenient: TelemetryCapture) -> None:
    ran = []
    with journey("recording.job", "PA/50123/2024") as j:  # a case reference: rejected
        with stage("Upload Stage") as s:  # invalid name
            s.fail("free text about the case")  # invalid code
            ran.append(j)
    record_event("Not A Valid Name")
    assert ran == [None]
    (failed,) = lenient.events("invalid_stage.failed")
    assert failed[c.ERROR_CODE] == "invalid_error_code"
    assert c.JOURNEY_ID not in failed
    assert _errors(lenient) == {
        "invalid.journey": 1,
        "invalid.stage_name": 1,
        "invalid.error_code": 1,
        "invalid.event_name": 1,
    }
    lenient.assert_no_prohibited_content("PA/50123/2024", "free text about the case")


def test_misuse_raises_in_strict_mode(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        with journey("recording.job", "PA/50123/2024"):
            pass
    with pytest.raises(ValueError):
        record_event("Not A Valid Name")


# H2 — reserved LogRecord names ---------------------------------------------------------


@pytest.mark.parametrize("name", ["filename", "name", "module", "process", "message", "args"])
def test_reserved_logrecord_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        TelemetrySchema().attribute(name)


# H3 — auto-instrumented stack traces and status descriptions --------------------------


class _Model(BaseModel):
    count: int


def _pydantic_chain() -> RuntimeError:
    try:
        try:
            _Model(count=SECRET)  # type: ignore[arg-type]
        except ValidationError as inner:
            raise RuntimeError("wrapper " + SECRET) from inner
    except RuntimeError as exc:
        return exc
    raise AssertionError("unreachable")


def test_sanitize_stacktrace_keeps_structure_and_drops_all_messages() -> None:
    text = "".join(traceback.format_exception(_pydantic_chain()))
    assert SECRET in text  # the raw traceback does carry it
    clean = sanitize_stacktrace(text)
    assert SECRET not in clean and "input_value" not in clean
    assert "Traceback (most recent call last):" in clean
    assert "ValidationError: [REDACTED]" in clean
    assert "RuntimeError: [REDACTED]" in clean
    assert "The above exception was the direct cause of the following exception:" in clean
    assert 'File "' in clean


def test_auto_instrumented_span_exceptions_are_sanitized() -> None:
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(sink, default_redactor)))
    # Default SDK behaviour: record_exception and set_status_on_exception, as instrumentations do.
    with pytest.raises(RuntimeError):
        with provider.get_tracer("t").start_as_current_span("POST /api/submit"):
            raise _pydantic_chain()
    (span,) = sink.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "RuntimeError: [REDACTED]"
    exported = json.dumps([dict(e.attributes) for e in span.events]) + span.status.description
    assert SECRET not in exported and "input_value" not in exported


def test_sanitize_handles_frames_without_source_and_carets() -> None:
    text = (
        "Traceback (most recent call last):\n"
        '  File "<stdin>", line 1, in <module>\n'
        '  File "/app/x.py", line 3, in f\n'
        "    return d[key]\n"
        "           ~^^^^^\n"
        "KeyError: 'appellant name'\n"
    )
    assert sanitize_stacktrace(text) == (
        "Traceback (most recent call last):\n"
        '  File "<stdin>", line 1, in <module>\n'
        '  File "/app/x.py", line 3, in f\n'
        "    return d[key]\n"
        "           ~^^^^^\n"
        "KeyError: [REDACTED]\n"
    )


# H4 — foreign tracer provider ----------------------------------------------------------


def _azure_settings(**kw: Any) -> TelemetrySettings:
    return TelemetrySettings(
        service_name="t",
        connection_string=SecretStr("InstrumentationKey=00000000-0000-0000-0000-000000000000"),
        sentry_dsn=None,
        **kw,
    )


def test_setup_refuses_when_another_tracer_provider_is_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_mod.trace, "get_tracer_provider", lambda: TracerProvider())
    with pytest.raises(RuntimeError, match="bypass hmcts-otel scrubbing"):
        setup_telemetry(_azure_settings())
    assert not setup_mod.STATE.configured


def test_foreign_tracer_provider_can_be_explicitly_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.monitor.opentelemetry as distro

    monkeypatch.setattr(setup_mod.trace, "get_tracer_provider", lambda: TracerProvider())
    monkeypatch.setattr(setup_mod.trace, "set_tracer_provider", lambda tp: None)
    monkeypatch.setattr(distro, "configure_azure_monitor", lambda **kw: None)
    setup_telemetry(_azure_settings(allow_foreign_tracer_provider=True))
    assert setup_mod.STATE.configured


# H5 — duration buckets in seconds -------------------------------------------------------


def test_duration_histograms_use_second_buckets(tel: TelemetryCapture) -> None:
    with stage("upload"):
        pass
    data = tel.metrics.get_metrics_data()
    assert data is not None
    histograms = [
        m
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name in (c.METRIC_STAGE_DURATION, c.METRIC_EVENT_DURATION)
    ]
    assert histograms
    for metric in histograms:
        for point in metric.data.data_points:
            assert tuple(point.explicit_bounds) == c.DURATION_BUCKETS_S


# M1 — relay accepts JSON only ------------------------------------------------------------


def test_relay_rejects_non_json_content_types(tel: TelemetryCapture) -> None:
    app = FastAPI()
    app.include_router(create_telemetry_relay_router(path="/t"))
    client = TestClient(app)
    body = '{"events": []}'
    assert client.post("/t", content=body, headers={"content-type": "text/plain"}).status_code == 415
    form = client.post("/t", data={"events": "x"})
    assert form.status_code == 415
    ok = client.post("/t", content=body, headers={"content-type": "application/json; charset=utf-8"})
    assert ok.status_code == 202


# M2 — access logs ------------------------------------------------------------------------


def test_relative_request_line_query_is_redacted() -> None:
    line = '127.0.0.1:5 - "GET /api/search?q=appellant+name HTTP/1.1" 200'
    assert redact(line) == '127.0.0.1:5 - "GET /api/search?[REDACTED] HTTP/1.1" 200'
    assert redact("https://host/p?x=1") == "https://host/p?[REDACTED]"
    assert redact("ratio 3/4? yes") == "ratio 3/4? yes"


def test_warns_when_access_log_on_and_capture_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = TelemetrySettings(
        service_name="t", connection_string=None, sentry_dsn=None, strict=True, capture_uvicorn_logs=False
    )
    with capture_telemetry(settings):
        access = logging.getLogger("uvicorn.access")
        monkeypatch.setattr(access, "disabled", False)
        monkeypatch.setattr(access, "handlers", [logging.StreamHandler()])
        monkeypatch.setattr(fastapi_mod, "_access_log_warned", False)
        app = FastAPI()

        @app.get("/x")
        def x() -> dict[str, str]:
            return {}

        instrument_fastapi(app)
        with caplog.at_level(logging.WARNING, logger="hmcts_otel"):
            TestClient(app).get("/x")
        assert any("--no-access-log" in r.getMessage() for r in caplog.records)


# M3 — non-primitive log extras ------------------------------------------------------------


def test_non_primitive_extras_are_omitted(tel: TelemetryCapture) -> None:
    logging.getLogger("app").info("payload", extra={"body": {"text": SECRET}, "ids": ["a", "x@y.com"], "n": 3})
    (record,) = [r for r in tel.log_records() if r.body == "payload"]
    assert record.attributes["body"] == "[dict omitted]"
    assert tuple(record.attributes["ids"]) == ("a", "[REDACTED]")
    assert record.attributes["n"] == 3
    tel.assert_no_prohibited_content(SECRET)


# M5 — bounded warn-once memory ---------------------------------------------------------------


def test_dropped_attribute_warning_memory_is_bounded(tel: TelemetryCapture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_mod, "_warned", set())
    monkeypatch.setattr(events_mod, "_MAX_WARNED", 10)
    events_mod.report_dropped([(f"key{i}", "unregistered") for i in range(50)], where="relay")
    assert len(events_mod._warned) == 10
    (point,) = tel.metric_points(c.METRIC_DROPPED_ATTRIBUTES)
    assert point["value"] == 50


# Second review — H1: context resets across contexts ------------------------------------


def test_journey_in_streaming_response_generator(tel: TelemetryCapture) -> None:
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.get("/export")
    def export() -> StreamingResponse:
        def body() -> Iterator[str]:
            with journey("export.job", "abc123"):
                for i in range(3):
                    yield f"chunk{i}\n"

        return StreamingResponse(body())

    instrument_fastapi(app)
    resp = TestClient(app).get("/export")
    assert resp.status_code == 200 and resp.text == "chunk0\nchunk1\nchunk2\n"


def test_context_restore_across_copied_contexts() -> None:
    import contextvars

    from hmcts_otel import current_journey, get_request_id, request_id_context

    def gen() -> Iterator[int]:
        with journey("export.job", "abc123"), request_id_context("rid-1"):
            yield 1
            yield 2

    g = gen()
    contextvars.copy_context().run(next, g)  # enter in one context
    contextvars.copy_context().run(next, g)  # resume in another
    ctx = contextvars.copy_context()
    with pytest.raises(StopIteration):
        ctx.run(next, g)  # exit in a third: must not raise ValueError
    assert ctx.run(current_journey) is None and ctx.run(get_request_id) is None


# Second review — H2: uvicorn reinstalling its handlers ----------------------------------


def _uvicorn_secret() -> str:
    return "appellant " + "SECRET" + "CONTENT" + " jane@example.com"


def test_uvicorn_config_after_setup_is_recaptured(capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    async def app(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover - never served
        return None

    with capture_telemetry() as tel:
        uvicorn.Config(app, log_config=LOGGING_CONFIG)  # what uvicorn.run(app) / `fastapi run` do
        for name in ("uvicorn", "uvicorn.error"):
            assert logging.getLogger(name).handlers == [] and logging.getLogger(name).propagate
        assert logging.getLogger("uvicorn.access").disabled
        secret = _uvicorn_secret()
        try:
            raise ValueError(secret)
        except ValueError as exc:
            logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=exc)
        captured = capsys.readouterr()
        assert "SECRETCONTENT" not in captured.out + captured.err
        assert "Exception in ASGI application" in captured.out
        tel.assert_no_prohibited_content(secret)


def test_guard_middleware_recaptures_on_lifespan_and_requests(tel: TelemetryCapture) -> None:
    app = FastAPI()

    @app.get("/x")
    def x() -> dict[str, str]:
        return {}

    instrument_fastapi(app, request_id=False)  # guard is installed even without request IDs
    uv = logging.getLogger("uvicorn")
    uv.addHandler(logging.StreamHandler())
    uv.propagate = False
    with TestClient(app) as client:  # runs lifespan startup
        assert uv.handlers == [] and uv.propagate
        uv.addHandler(logging.StreamHandler())
        client.get("/x")
        assert uv.handlers == []


# Second review — H3: Sentry --------------------------------------------------------------


@pytest.fixture
def sentry_capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    import hmcts_otel.sentry as hs

    sent: list[dict[str, Any]] = []

    class Capture(Transport):
        def capture_envelope(self, envelope: Any) -> None:
            for item in envelope.items:
                try:
                    sent.append(json.loads(item.payload.get_bytes()))
                except ValueError:
                    pass

    real_init = sentry_sdk.init
    monkeypatch.setattr(hs.sentry_sdk, "init", lambda **kw: real_init(**{**kw, "transport": Capture}))
    yield sent
    sentry_sdk.get_client().close()


def _sentry_settings(**kw: Any) -> TelemetrySettings:
    return TelemetrySettings(
        service_name="t", connection_string=None, sentry_dsn=SecretStr("https://k@o1.ingest.sentry.io/1"), **kw
    )


def _strip_source_context(payload: Any) -> Any:
    """Source lines (context_line/pre/post_context) are code, not data; drop them before
    searching so literals in this test file do not count as leaks."""
    if isinstance(payload, dict):
        return {
            k: _strip_source_context(v)
            for k, v in payload.items()
            if k not in ("context_line", "pre_context", "post_context")
        }
    if isinstance(payload, list):
        return [_strip_source_context(v) for v in payload]
    return payload


def test_sentry_error_events_have_no_messages_or_queries(sentry_capture: list[dict[str, Any]]) -> None:
    import sentry_sdk

    secret = "Judge ruled the appellant " + "SECRET" + "CONTENT"
    query = "name=" + "SECRET" + "Q"
    setup_telemetry(_sentry_settings())
    sentry_sdk.add_breadcrumb(category="httplib", type="http", data={"url": "https://x/y", "http.query": query})
    try:
        raise ValueError(secret)
    except ValueError:
        sentry_sdk.capture_exception()
    sentry_sdk.flush()
    (event,) = [e for e in sentry_capture if "exception" in e]
    assert event["exception"]["values"][0]["value"] == "[REDACTED]"
    assert event["exception"]["values"][0]["type"] == "ValueError"
    text = json.dumps(_strip_source_context(event))
    assert "SECRETCONTENT" not in text and "SECRETQ" not in text


def test_sentry_transactions_are_scrubbed(sentry_capture: list[dict[str, Any]]) -> None:
    import sentry_sdk

    query = "q=" + "SECRET" + "QUERY"
    setup_telemetry(_sentry_settings(sentry_traces_sample_rate=1.0))
    with sentry_sdk.start_transaction(name="GET /search"):
        with sentry_sdk.start_span(op="http.client", description="GET https://api.example/search?" + query) as span:
            span.set_data("url.query", query)
            span.set_data("http.query", query)
    sentry_sdk.flush()
    transactions = [e for e in sentry_capture if e.get("type") == "transaction"]
    assert transactions
    assert "SECRETQUERY" not in json.dumps(transactions)


def test_sentry_messages_kept_when_opted_in(sentry_capture: list[dict[str, Any]]) -> None:
    import sentry_sdk

    setup_telemetry(_sentry_settings(capture_exception_messages=True))
    try:
        raise ValueError("timeout talking to x@y.com")
    except ValueError:
        sentry_sdk.capture_exception()
    sentry_sdk.flush()
    (event,) = [e for e in sentry_capture if "exception" in e]
    assert event["exception"]["values"][0]["value"] == "timeout talking to [REDACTED]"
