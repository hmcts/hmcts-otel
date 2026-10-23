"""Regression tests for the second review's medium (M1–M8) and low (L1–L9) findings."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import os
import signal
import subprocess
import sys
import time
import traceback
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, Status, StatusCode
from pydantic import SecretStr

import hmcts_otel._setup as setup_mod
import hmcts_otel.events as events_mod
from hmcts_otel import (
    TelemetrySettings,
    is_configured,
    record_event,
    request_id_context,
    setup_telemetry,
    shutdown_telemetry,
    stage,
)
from hmcts_otel import conventions as c
from hmcts_otel._scrubbing import ScrubbingSpanExporter, sanitize_stacktrace
from hmcts_otel.fastapi import RequestIdMiddleware, create_telemetry_relay_router
from hmcts_otel.redaction import MAX_TEXT_LENGTH, TRUNCATED, default_redactor, redact
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

ROOT = Path(__file__).resolve().parent.parent
SECRET = "the appellant stated that she fled"


@pytest.fixture
def lenient() -> Iterator[TelemetryCapture]:
    settings = TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None, strict=False)
    with capture_telemetry(settings) as capture:
        yield capture


def _errors(tel: TelemetryCapture) -> dict[str, int]:
    return {p["attributes"]["kind"]: p["value"] for p in tel.metric_points(c.METRIC_TELEMETRY_ERRORS)}


def _exporter() -> tuple[TracerProvider, InMemorySpanExporter]:
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ScrubbingSpanExporter(sink, default_redactor)))
    return provider, sink


# M1 — redaction is linear time and bounded -------------------------------------------------

ADVERSARIAL = {
    "letters": "a" * 200_000,
    "dots_after_at": "a@" + "a." * 100_000,
    "many_at": "a@" * 100_000,
    "path_tilde": "/~" * 100_000,
    "http_chain": "http://" * 30_000,
    "bearer_chain": "Bearer " * 30_000,
    "userinfo": "http://" + "a:" * 100_000,
    "jwt_like": "eyJ" + "a." * 100_000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_redaction_is_fast_on_adversarial_input(name: str) -> None:
    started = time.perf_counter()
    redact(ADVERSARIAL[name])
    assert time.perf_counter() - started < 0.5


def test_each_rule_is_fast_on_capped_input() -> None:
    for text in ADVERSARIAL.values():
        capped = text[: MAX_TEXT_LENGTH + 9000]
        for rule in default_redactor.rules:
            started = time.perf_counter()
            rule.pattern.sub(rule.replacement, capped)
            assert time.perf_counter() - started < 0.5, rule.name


def test_long_text_is_truncated_and_secrets_at_the_cut_are_redacted() -> None:
    email = "judge.smith@justice.gov.uk"
    text = "x " * ((MAX_TEXT_LENGTH - 10) // 2) + email + " tail" * 5000
    out = redact(text)
    assert out.endswith(TRUNCATED) and len(out) == MAX_TEXT_LENGTH + len(TRUNCATED)
    assert "judge.smith" not in out and "justice.gov" not in out


# M2 — the logging filter never raises into the caller --------------------------------------


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("boom")


def _deep_chain(depth: int) -> BaseException:
    exc: BaseException = ValueError("root")
    for i in range(depth):
        try:
            raise ValueError(f"level {i}") from exc
        except ValueError as new:
            exc = new
    return exc


def test_unprintable_message_does_not_raise(tel: TelemetryCapture) -> None:
    logging.getLogger("app").warning(_Unprintable())
    assert any(r.body == "[unformattable log message]" for r in tel.log_records())


def test_deep_exception_chain_does_not_raise(tel: TelemetryCapture) -> None:
    logging.getLogger("app").error("deep", exc_info=_deep_chain(1500))
    (record,) = [r for r in tel.log_records() if r.body == "deep"]
    stack = record.attributes["exception.stacktrace"]
    assert "most recent exceptions of a longer chain" in stack
    assert stack.count("ValueError: [REDACTED]") == 25


def test_log_exception_then_raise_keeps_the_app_exception(tel: TelemetryCapture) -> None:
    class BusinessError(Exception):
        pass

    with pytest.raises(BusinessError):
        try:
            raise BusinessError("x") from _deep_chain(1500)
        except BusinessError:
            logging.getLogger("app").exception("failed")
            raise


def test_internal_filter_failure_is_contained(lenient: TelemetryCapture, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: Any, record: logging.LogRecord) -> None:
        raise RuntimeError("redactor broken")

    monkeypatch.setattr("hmcts_otel._logging.RedactingFilter._redact", broken)
    logging.getLogger("app").warning("contact %s", "judge@justice.gov.uk")
    assert any(r.body == "[unformattable log record]" for r in lenient.log_records())
    assert _errors(lenient).get("internal.log_filter", 0) >= 1
    lenient.assert_no_prohibited_content("judge@justice.gov.uk")


# M3 — non-string event names ----------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, b"bytes"])
def test_non_string_event_names(lenient: TelemetryCapture, bad: Any) -> None:
    record_event(bad)
    assert lenient.event_names() == []
    assert _errors(lenient) == {"invalid.event_name": 1}


def test_non_string_event_name_strict_raises_value_error(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        record_event(None)  # type: ignore[arg-type]


# M4 — request-ID middleware with headerless response start ------------------------------


def test_request_id_middleware_handles_missing_headers_key() -> None:
    async def raw(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204})
        await send({"type": "http.response.body", "body": b""})

    resp = TestClient(RequestIdMiddleware(raw)).get("/")
    assert resp.status_code == 204 and resp.headers["X-Request-Id"]


# M5 — bad configuration does not crash startup -------------------------------------------


def test_invalid_env_values_fall_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HMCTS_OTEL_SAMPLING_RATIO", "abc")
    monkeypatch.setenv("HMCTS_OTEL_LOG_FORMAT", "JSON")
    monkeypatch.setenv("LOG_LEVEL", "trace")
    with caplog.at_level(logging.WARNING, logger="hmcts_otel"):
        settings = setup_telemetry()
    assert settings.sampling_ratio == 1.0 and settings.log_format == "json"
    assert logging.getLogger().level == logging.INFO
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "sampling_ratio" in messages and "abc" not in messages
    assert "unknown log level" in messages


def test_invalid_env_values_raise_in_strict_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HMCTS_OTEL_SAMPLING_RATIO", "abc")
    monkeypatch.setenv("HMCTS_OTEL_STRICT", "true")
    with pytest.raises(ValueError):
        setup_telemetry()
    assert not is_configured()


def _stdout_handlers() -> int:
    return sum(type(h).__name__ == "StdoutHandler" for h in logging.getLogger().handlers)


def test_bad_connection_string_degrades_to_stdout(caplog: pytest.LogCaptureFixture) -> None:
    settings = TelemetrySettings(service_name="t", connection_string=SecretStr("garbage"), sentry_dsn=None)
    with caplog.at_level(logging.ERROR, logger="hmcts_otel"):
        setup_telemetry(settings)
    assert is_configured() and setup_mod.STATE.tracer_provider is None
    assert any("telemetry export disabled" in r.getMessage() for r in caplog.records)
    setup_telemetry(settings)  # a retry returns the existing configuration
    assert _stdout_handlers() == 1
    with stage("still_works"):
        pass


def test_bad_connection_string_raises_in_strict_mode_without_leftovers() -> None:
    settings = TelemetrySettings(service_name="t", connection_string=SecretStr("garbage"), sentry_dsn=None, strict=True)
    with pytest.raises(ValueError):  # the exporter rejects the connection string
        setup_telemetry(settings)
    assert not is_configured() and _stdout_handlers() == 0


def test_bad_sentry_dsn_does_not_crash(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="hmcts_otel"):
        setup_telemetry(TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=SecretStr("not a dsn")))
    assert is_configured()
    assert any("Sentry disabled" in r.getMessage() for r in caplog.records)


# M6 — no unredacted output before setup or after shutdown --------------------------------


def test_preexisting_root_handlers_are_filtered() -> None:
    stream = io.StringIO()
    early = logging.StreamHandler(stream)
    logging.getLogger().addHandler(early)
    with capture_telemetry():
        logging.getLogger("app").warning("contact %s", "judge@justice.gov.uk")
    assert "judge@justice.gov.uk" not in stream.getvalue()
    assert "contact [REDACTED]" in stream.getvalue()


def test_last_resort_output_after_shutdown_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    with capture_telemetry():
        pass
    root = logging.getLogger()
    saved = list(root.handlers)
    root.handlers.clear()
    try:
        try:
            raise ValueError(SECRET)
        except ValueError:
            logging.getLogger("app").exception("after shutdown %s", "judge@justice.gov.uk")
    finally:
        root.handlers[:] = saved
    err = capsys.readouterr().err
    assert "after shutdown [REDACTED]" in err
    assert SECRET not in err and "judge@justice.gov.uk" not in err


# M7 — the worker example stops on SIGTERM -------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_worker_example_exits_promptly_on_sigterm() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "OTEL_SERVICE_NAME": "example-worker"}
    env.pop("APPLICATIONINSIGHTS_CONNECTION_STRING", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "examples.worker"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.5)
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("worker example ignored SIGTERM")
    assert code == 0, proc.stderr.read().decode() if proc.stderr else ""


def test_readme_worker_snippet_exits() -> None:
    readme = (ROOT / "README.md").read_text()
    section = readme.split("## Quick start: worker or script", 1)[1].split("\n## ", 1)[0]
    assert "SystemExit" in section or "sys.exit" in section


# M8 — free-text status descriptions -------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("DETAIL: Key (party_name)=(SECRET Smith) already exists", "DETAIL: [REDACTED]"),
        ("duplicate key value violates constraint for SECRET Smith", "[REDACTED]"),
        ("ÉchecError: SECRET Smith", "[REDACTED]"),
        ("speech.timeout", "speech.timeout"),
        ("ValueError: SECRET Smith", "ValueError: [REDACTED]"),
    ],
)
def test_status_descriptions(description: str, expected: str) -> None:
    provider, sink = _exporter()
    with provider.get_tracer("t").start_as_current_span("wsgi") as span:
        span.set_status(Status(StatusCode.ERROR, description))
    (out,) = sink.get_finished_spans()
    assert out.status.description == expected


# L1 — stack trace sanitiser cannot be fooled by message text -------------------------------


def test_fake_frames_inside_a_message_are_dropped() -> None:
    try:
        raise ValueError('bad input\n  File "x.py", line 1, in f\n    ' + SECRET)
    except ValueError as exc:
        text = "".join(traceback.format_exception(exc))
    assert SECRET in text
    clean = sanitize_stacktrace(text)
    assert SECRET not in clean and clean.rstrip().endswith("ValueError: [REDACTED]")


def test_syntax_error_source_from_runtime_text_is_dropped() -> None:
    user_text = "the appellant's statement {"
    try:
        ast.literal_eval(user_text)
    except SyntaxError as exc:
        text = "".join(traceback.format_exception(exc))
    assert "appellant" in text
    clean = sanitize_stacktrace(text)
    assert "appellant" not in clean and "SyntaxError: [REDACTED]" in clean


def test_locals_type_names_are_kept() -> None:
    def inner() -> None:
        class LocalError(Exception):
            pass

        raise LocalError(SECRET)

    try:
        inner()
    except Exception as exc:
        clean = sanitize_stacktrace("".join(traceback.format_exception(exc)))
    assert "<locals>.LocalError: [REDACTED]" in clean and SECRET not in clean


# L2 — links, paths and request IDs --------------------------------------------------------


def test_link_attributes_are_scrubbed() -> None:
    provider, sink = _exporter()
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("a") as first:
        ctx = first.get_span_context()
    with tracer.start_as_current_span("b", links=[Link(ctx, {"note": "judge@justice.gov.uk"})]):
        pass
    linked = next(s for s in sink.get_finished_spans() if s.name == "b")
    assert linked.links[0].attributes["note"] == "[REDACTED]"


def test_concrete_paths_are_replaced_by_route_templates() -> None:
    provider, sink = _exporter()
    with provider.get_tracer("t").start_as_current_span("GET /parties/{name}") as span:
        span.set_attribute("http.route", "/parties/{name}")
        span.set_attribute("http.target", "/parties/SECRETPATH?x=1")
        span.set_attribute("url.full", "https://app/parties/SECRETPATH?x=1")
        span.set_attribute("url.path", "/parties/SECRETPATH")
    (out,) = sink.get_finished_spans()
    assert out.attributes["http.target"] == "/parties/{name}"
    assert out.attributes["url.path"] == "/parties/{name}"
    assert out.attributes["url.full"] == "https://app/parties/{name}"


def test_free_form_request_ids_are_replaced(tel: TelemetryCapture) -> None:
    app = FastAPI()

    @app.get("/x")
    def x() -> dict[str, str]:
        return {}

    from hmcts_otel.fastapi import instrument_fastapi

    instrument_fastapi(app)
    resp = TestClient(app).get("/x", headers={"X-Request-Id": "Jane.Smith.AB12345"})
    assert resp.headers["X-Request-Id"] != "Jane.Smith.AB12345"
    tel.assert_no_prohibited_content("Jane.Smith")


def test_request_id_context_validates(lenient: TelemetryCapture) -> None:
    with request_id_context("Jane.Smith") as rid:
        assert rid != "Jane.Smith" and len(rid) == 32
    assert _errors(lenient) == {"invalid.request_id": 1}


# L3 — free-text dropped keys are hashed in logs -------------------------------------------


def test_dropped_free_text_keys_are_hashed(tel: TelemetryCapture, caplog: pytest.LogCaptureFixture) -> None:
    events_mod._warned.clear()
    with caplog.at_level(logging.WARNING, logger="hmcts_otel"):
        events_mod.report_dropped([("Smith v Jones hearing notes", "unregistered"), ("file.name", "x")], where="t")
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Smith" not in text and "sha256:" in text and "'file.name'" in text


# L4 — redaction gaps and false positives ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="hunter2"', 'password="[REDACTED]"'),
        ("connect(password='hunter2')", "connect(password='[REDACTED]')"),
        ('{"password": "hunter2"}', '{"password": "[REDACTED]"}'),
        ("password: hunter2", "password: [REDACTED]"),
        ("the bearer of the warrant attended", "the bearer of the warrant attended"),
        ("Bearer token missing", "Bearer token missing"),
        ("Authorization: Bearer abcdefghijklmnopqrstu", "Authorization: [REDACTED]"),
    ],
)
def test_secret_and_bearer_rules(text: str, expected: str) -> None:
    assert redact(text) == expected


# L5 — no deprecated handler ---------------------------------------------------------------


def test_capture_raises_no_deprecation_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with capture_telemetry():
            logging.getLogger("app").info("hello")


# L6 — exc_info=True outside an except block ----------------------------------------------


def test_exc_info_without_exception(tel: TelemetryCapture, capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("app").error("no active exception", exc_info=True)
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if "no active exception" in x)
    assert "exception.type" not in line


# L7 — shutdown budget includes the Sentry flush ---------------------------------------------


def test_sentry_flush_gets_only_the_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    class Slow(InMemorySpanExporter):
        def shutdown(self) -> None:
            time.sleep(2)

    flushes: list[float] = []
    monkeypatch.setattr(sentry_sdk, "is_initialized", lambda: True)
    monkeypatch.setattr(sentry_sdk, "flush", lambda timeout=None: flushes.append(timeout or 0))
    setup_telemetry(
        TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None),
        span_exporter=Slow(),
        set_global_providers=False,
    )
    started = time.monotonic()
    shutdown_telemetry(timeout_millis=300)
    assert time.monotonic() - started < 1.0
    assert all(f <= 0.3 for f in flushes)


# L8 — chunked relay bodies are size-limited while streaming --------------------------------


def test_relay_limits_chunked_bodies(tel: TelemetryCapture) -> None:
    app = FastAPI()
    app.include_router(create_telemetry_relay_router(path="/t", max_body_bytes=1000))

    def chunks() -> Iterator[bytes]:
        yield b'{"events": ['
        for _ in range(500):
            yield b'{"name": "x.y"},'
        yield b'{"name": "x.y"}]}'

    resp = TestClient(app).post("/t", content=chunks(), headers={"content-type": "application/json"})
    assert resp.status_code == 413


# L9 — stage() in a sync generator does not raise ------------------------------------------


def test_stage_inside_streaming_generator(tel: TelemetryCapture) -> None:
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    @app.get("/export")
    def export() -> StreamingResponse:
        def body() -> Iterator[str]:
            with stage("export"):
                for i in range(3):
                    yield f"{i}"

        return StreamingResponse(body())

    assert TestClient(app).get("/export").text == "012"
    assert "export.completed" in tel.event_names()


async def test_stage_cancellation_still_works(tel: TelemetryCapture) -> None:
    async def work() -> None:
        with stage("long_task"):
            await asyncio.sleep(10)

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "long_task.cancelled" in tel.event_names()
