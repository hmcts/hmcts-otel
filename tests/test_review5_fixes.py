"""Regression tests for the fifth review round (PR #4)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from pydantic import SecretStr

from hmcts_otel import _state
from hmcts_otel.redaction import MAX_TEXT_LENGTH, default_redactor
from hmcts_otel.settings import TelemetrySettings
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

# --- N3: spaces after the separator do not shorten the redacted value ----------------


@pytest.mark.parametrize("spaces", ["", " ", "   "])
@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # Quoted
        ('password={s}"two words" tail', 'password={s}"[REDACTED]" tail'),
        ("pwd:{s}'two words' tail", "pwd:{s}'[REDACTED]' tail"),
        ('{{"password":{s}"two words", "user": "u1"}}', '{{"password":{s}"[REDACTED]", "user": "u1"}}'),
        # ODBC braced
        ("Pwd={s}{{two words;x}};Uid=u", "Pwd={s}{{[REDACTED]}};Uid=u"),
        # Connection string, up to ';' or the end of the line
        ("Server=db;Password={s}two words;Encrypt=yes", "Server=db;Password={s}[REDACTED];Encrypt=yes"),
        ("Server=db;Password ={s}two words;Encrypt=yes", "Server=db;Password ={s}[REDACTED];Encrypt=yes"),
        ("Server=db;PWD={s}two words", "Server=db;PWD={s}[REDACTED]"),
        (
            "Driver={{ODBC Driver 18 for SQL Server}};Server=db;Uid=u;PWD={s}two words;Encrypt=yes",
            "Driver={{ODBC Driver 18 for SQL Server}};Server=db;Uid=u;PWD={s}[REDACTED];Encrypt=yes",
        ),
        # Unquoted
        ("password:{s}two words", "password:{s}[REDACTED]"),
        ("password ={s}hunter2 more", "password ={s}[REDACTED]"),
        ("secret:{s}two words\nnext line", "secret:{s}[REDACTED]\nnext line"),
        ("password={s}hunter2 user=u1 retry=3", "password={s}[REDACTED] user=u1 retry=3"),
        ("password:{s}two words, user: u1", "password:{s}[REDACTED], user: u1"),
        ("api_key:{s}two words&next=1", "api_key:{s}[REDACTED]&next=1"),
    ],
)
def test_spaces_after_the_separator_keep_the_value_whole(template: str, expected: str, spaces: str) -> None:
    text, want = template.format(s=spaces), expected.format(s=spaces)
    assert default_redactor.redact(text) == want
    assert default_redactor.redact(want) == want  # idempotent
    assert "words" not in want and "hunter2" not in want and "more" not in want


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Prose without a separator after the key is untouched.
        ("Your password is incorrect, please try again.", None),
        ("The password policy requires 12 characters; see the guidance.", None),
        ('He said "password" twice.', None),
        # A key: value phrase loses the value, up to the next delimiter or pair.
        ("Reset failed, password: expired token, retry later", "Reset failed, password: [REDACTED], retry later"),
        ("login password: hunter2 at 10:30 today", "login password: [REDACTED] 10:30 today"),
        ("password: hunter2\nThe next line is kept.", "password: [REDACTED]\nThe next line is kept."),
    ],
)
def test_prose_is_not_mangled(text: str, expected: str | None) -> None:
    assert default_redactor.redact(text) == (text if expected is None else expected)


@pytest.mark.parametrize(
    "text",
    [
        "password: " + "a " * 100_000,
        "password: " + "a b=" * 50_000,
        "password: x " * 30_000,
        "password=   " * 30_000,
        "Server=db;Password=   " + "a " * 100_000,
        "password: " + " a" * 30_000 + "=",
        "password: " + ("a" * 63 + " ") * 3_000,
    ],
    ids=["words", "pairs", "repeated_keys", "spaced_keys", "spaced_odbc", "late_separator", "long_words"],
)
def test_spaced_values_are_linear_time(text: str) -> None:
    started = time.perf_counter()
    default_redactor.redact(text)
    assert time.perf_counter() - started < 0.5


# --- N4: values past the length bound fail closed ------------------------------------

SUFFIX = "PRIVATE_SUFFIX"
ODBC_REDACTED = "Server=db;Password=[REDACTED];Encrypt=yes"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="' + "a" * 4100 + f' {SUFFIX}" tail', 'password="[REDACTED]" tail'),
        ("password='" + "a b" * 1400 + f" {SUFFIX}' tail", "password='[REDACTED]' tail"),
        ('password="' + "a\\" * 4200 + f'"{SUFFIX}" tail', 'password="[REDACTED]" tail'),  # escapes past the bound
        ('password="' + "a" * 4100 + f" {SUFFIX}\nnext", 'password="[REDACTED]\nnext'),  # unclosed: end of line
        ("Pwd={" + "a" * 300 + f", {SUFFIX}}};Uid=u", "Pwd={[REDACTED]};Uid=u"),
        ("Pwd={" + "a" * 300 + f", {SUFFIX}\nnext", "Pwd={[REDACTED]\nnext"),
        ("Server=db;Password=" + "a " * 150 + f"& {SUFFIX};Encrypt=yes", ODBC_REDACTED),
        ("Server=db;Password=" + "a " * 150 + f"x='y' {SUFFIX};Encrypt=yes", ODBC_REDACTED),
        ("Server=db;Password= " + "a " * 150 + ", " + SUFFIX, "Server=db;Password= [REDACTED]"),
        # After '=', a long value is read as a connection-string value: to ';' or the end.
        ("password=" + "a" * 5000 + SUFFIX + " user=u1", "password=[REDACTED]"),
        ("password: " + "a" * 5000 + SUFFIX + " user=u1", "password: [REDACTED] user=u1"),
        ("password: " + "a " * 3000 + SUFFIX, "password: [REDACTED]"),
    ],
    ids=[
        "dquote",
        "squote",
        "escapes",
        "unclosed",
        "braced",
        "braced_unclosed",
        "odbc",
        "odbc_quote",
        "odbc_spaced",
        "long_word",
        "long_word_colon",
        "many_words",
    ],
)
def test_values_past_the_bound_are_redacted_in_full(text: str, expected: str) -> None:
    redacted = default_redactor.redact(text)
    assert SUFFIX not in redacted
    assert redacted == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="' + "a" * 4096 + '" tail', 'password="[REDACTED]" tail'),
        ("Pwd={" + "a" * 256 + "};Uid=u", "Pwd={[REDACTED]};Uid=u"),
        ("Server=db;Password=" + "a" * 256 + ";Encrypt=yes", "Server=db;Password=[REDACTED];Encrypt=yes"),
        ("Pwd={short;Uid=u", "Pwd={[REDACTED];Uid=u"),  # unclosed and short: as before
    ],
    ids=["dquote", "braced", "odbc", "braced_unclosed_short"],
)
def test_values_at_the_bound_keep_the_rest_of_the_line(text: str, expected: str) -> None:
    assert default_redactor.redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        'password="' + "a" * 40_000,
        'password="' * 30_000,
        "password='" + "a b" * 15_000,
        'password="' + "a\\" * 20_000,
        "Pwd={" + "a" * 40_000,
        "Pwd={" * 40_000,
        "Pwd={" + "a" * 300 + "\n" + "Pwd={" * 10_000,
        "Server=db;Password=" + "a " * 100_000,
        "Server=db;Password=" + ("a" * 300 + ";Password=") * 200,
        ("password=" + "a" * 5000 + "\n") * 20,
        'x="' * 30_000 + "password=" + "a" * 40_000,
    ],
    ids=[
        "long_quote",
        "open_quotes",
        "long_squote",
        "escapes",
        "long_brace",
        "open_braces",
        "brace_lines",
        "long_odbc",
        "odbc_pairs",
        "long_lines",
        "late_key",
    ],
)
def test_overflow_paths_are_linear_time(text: str) -> None:
    started = time.perf_counter()
    default_redactor.redact(text)
    assert time.perf_counter() - started < 0.5


# --- M3: the prohibited-content check covers every exported string in full ----------

FICTIONAL_EMAIL = "fictional.person@example.test"


def _export_raw_log(tel: TelemetryCapture, body: object) -> None:
    """Put a record in the capture as an exporter would receive it, bypassing scrubbing."""
    tel.logs.export([ReadableLogRecord(LogRecord(body=body), resource=Resource.create({}))])  # type: ignore[arg-type]


def _fill(tel: TelemetryCapture) -> None:
    for _ in range(60):
        logging.getLogger("app").info("filler " + "x" * 1000)
    assert len(tel.all_exported_text()) > 60_000


def test_a_leak_in_a_late_record_of_a_large_capture_is_found() -> None:
    with capture_telemetry() as tel:
        _fill(tel)
        tel.assert_no_prohibited_content()  # clean so far
        n = len(tel.log_records())
        _export_raw_log(tel, f"contact {FICTIONAL_EMAIL}")
        with pytest.raises(AssertionError) as failure:
            tel.assert_no_prohibited_content()
    message = str(failure.value)
    assert f"rule 'email' in log record #{n} body" in message
    assert FICTIONAL_EMAIL not in message


@pytest.mark.parametrize("where", ["log", "span_attribute"])
@pytest.mark.parametrize("offset", [0, MAX_TEXT_LENGTH, 100_000, 1_000_000 - 40])
def test_a_leak_anywhere_in_one_huge_string_is_found(where: str, offset: int) -> None:
    text = "y" * offset + f" {FICTIONAL_EMAIL} " + "z" * (1_000_000 - offset)
    with capture_telemetry() as tel:
        if where == "log":
            _export_raw_log(tel, text)
        else:
            tel.spans.export([ReadableSpan(name="late", attributes={"x": text})])
        started = time.perf_counter()
        with pytest.raises(AssertionError, match="rule 'email'"):
            tel.assert_no_prohibited_content()
        assert time.perf_counter() - started < 2


def test_a_jwt_straddling_a_window_boundary_is_found() -> None:
    jwt = "eyJ" + "a" * 8000 + "." + "b" * 8000 + "." + "c" * 8000
    with capture_telemetry() as tel:
        _export_raw_log(tel, "y " * 20_000 + jwt + " tail")
        with pytest.raises(AssertionError, match="rule 'jwt'"):
            tel.assert_no_prohibited_content()


def test_a_clean_large_capture_passes_quickly() -> None:
    with capture_telemetry() as tel:
        _fill(tel)
        _export_raw_log(tel, "clean words " * 200_000)
        _export_raw_log(tel, {"nested": ["clean"] * 1000})
        started = time.perf_counter()
        tel.assert_no_prohibited_content("needle that is absent")
        assert time.perf_counter() - started < 2


def test_needles_are_still_searched_in_all_exported_text() -> None:
    with capture_telemetry() as tel:
        _fill(tel)
        _export_raw_log(tel, "y" * 100_000 + " SENTINEL_FICTIONAL")
        with pytest.raises(AssertionError, match="needle #1"):
            tel.assert_no_prohibited_content("absent", "SENTINEL_FICTIONAL")


# --- Logs API records are scrubbed; GenAI events keep no content (N1) ----------------

PROMPT = "PROMPT_FICTIONAL_JANE_SMITH"
ANSWER = "ANSWER_FICTIONAL_CASE_123"


def _emit(**kwargs: Any) -> None:
    """Emit through the OpenTelemetry Logs API on the package's LoggerProvider, as
    GenAI instrumentation (openai-v2 events, litellm's "otel" callback) does."""
    provider = _state.STATE.logger_provider
    assert provider is not None
    provider.get_logger("fictional.genai.instrumentation").emit(LogRecord(**kwargs))


def _only_record(tel: TelemetryCapture) -> Any:
    (record,) = [r for r in tel.log_records() if r.body != "filler"]
    return record


@pytest.mark.parametrize("named_by", ["attribute", "event_name"])
def test_logs_api_genai_events_are_exported_without_content(named_by: str) -> None:
    attributes: dict[str, Any] = {
        "gen_ai.system": "openai",
        "gen_ai.request.model": "fictional-model",
        "content": PROMPT,
        "message.content": PROMPT,
        "gen_ai.prompt": PROMPT,
        "tool_calls": json.dumps([{"arguments": PROMPT}]),
        "index": 0,
        "finish_reason": "stop",
    }
    name = {"event_name": "gen_ai.user.message"}
    if named_by == "attribute":
        attributes["event.name"] = "gen_ai.user.message"
        name = {}
    with capture_telemetry() as tel:
        _emit(body={"content": PROMPT, "role": "user"}, attributes=attributes, **name)
        record = _only_record(tel)
        tel.assert_no_prohibited_content(PROMPT)
    assert record.body is None
    kept = dict(record.attributes or {})
    kept.pop("event.name", None)
    assert kept == {
        "gen_ai.system": "openai",
        "gen_ai.request.model": "fictional-model",
        "index": 0,
        "finish_reason": "stop",
    }


def test_logs_api_genai_choice_drops_the_message() -> None:
    with capture_telemetry() as tel:
        _emit(
            event_name="gen_ai.choice",
            body={"index": 0, "finish_reason": "stop", "message": {"content": ANSWER}},
            attributes={"gen_ai.system": "openai", "gen_ai.response.finish_reasons": ("stop",)},
        )
        record = _only_record(tel)
        tel.assert_no_prohibited_content(ANSWER)
    assert record.body is None
    assert dict(record.attributes or {}) == {"gen_ai.system": "openai", "gen_ai.response.finish_reasons": ("stop",)}


def test_other_logs_api_records_are_redacted() -> None:
    with capture_telemetry() as tel:
        _emit(
            body={"text": f"contact {FICTIONAL_EMAIL}", "nested": [{"to": FICTIONAL_EMAIL}], "n": 3},
            attributes={
                "note": f"mail {FICTIONAL_EMAIL}",
                "list": (f"x {FICTIONAL_EMAIL}", "ok"),
                "map": {"to": FICTIONAL_EMAIL},
                "raw": b"bytes " + FICTIONAL_EMAIL.encode(),
                "gen_ai.prompt": PROMPT,
                "count": 2,
            },
        )
        _emit(body=f"plain body {FICTIONAL_EMAIL}")
        records = [r for r in tel.log_records()]
        tel.assert_no_prohibited_content(FICTIONAL_EMAIL, PROMPT)
    structured, plain = records
    assert structured.body == {"text": "contact [REDACTED]", "nested": [{"to": "[REDACTED]"}], "n": 3}
    attrs = dict(structured.attributes or {})
    assert attrs["note"] == "mail [REDACTED]" and attrs["count"] == 2
    assert attrs["list"] == ("x [REDACTED]", "ok") and attrs["map"] == {"to": "[REDACTED]"}
    assert attrs["raw"] == "[bytes omitted]" and "gen_ai.prompt" not in attrs
    assert plain.body == "plain body [REDACTED]"


def test_logs_api_exception_text_is_reduced_to_the_type() -> None:
    forged = f'Traceback (most recent call last):\n  File "x.py", line 1, in f\n    {PROMPT}\nValueError: {PROMPT}'
    with capture_telemetry() as tel:
        _emit(
            body="failed",
            attributes={"exception.type": "ValueError", "exception.message": PROMPT, "exception.stacktrace": forged},
        )
        attrs = dict(_only_record(tel).attributes or {})
        tel.assert_no_prohibited_content(PROMPT)
    assert attrs["exception.message"] == "[REDACTED]"
    assert attrs["exception.stacktrace"] == "ValueError: [REDACTED]\n"


def test_genai_span_events_keep_no_content() -> None:
    with capture_telemetry() as tel:
        with _state.get_tracer().start_as_current_span("chat") as span:
            span.add_event("gen_ai.user.message", {"content": PROMPT, "gen_ai.system": "openai"})
            span.add_event("gen_ai.choice", {"message.content": ANSWER, "index": 0, "finish_reason": "stop"})
            span.add_event("ordinary", {"content": f"mail {FICTIONAL_EMAIL}"})
        tel.assert_no_prohibited_content(PROMPT, ANSWER)
        (exported,) = tel.finished_spans()
    events = {e.name: dict(e.attributes or {}) for e in exported.events}
    assert events["gen_ai.user.message"] == {"gen_ai.system": "openai"}
    assert events["gen_ai.choice"] == {"index": 0, "finish_reason": "stop"}
    assert events["ordinary"] == {"content": "mail [REDACTED]"}  # not a GenAI event: redacted only


def test_a_record_that_cannot_be_scrubbed_keeps_no_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("scrubber broke")

    monkeypatch.setattr("hmcts_otel._scrubbing.scrub_attributes", broken)
    with capture_telemetry() as tel:
        _emit(body=f"contact {FICTIONAL_EMAIL}", attributes={"note": PROMPT})
        logging.getLogger("app").info("stdlib %s", PROMPT)  # does not raise into the caller
        bodies = [r.body for r in tel.log_records()]
        attrs = [dict(r.attributes or {}) for r in tel.log_records()]
        tel.assert_no_prohibited_content(FICTIONAL_EMAIL, PROMPT)
        errors = tel.metric_points("hmcts.telemetry.errors")
    assert bodies[:2] == ["[unscrubbable log record]"] * 2 and attrs[:2] == [{}, {}]
    assert any(p["attributes"].get("kind") == "internal.log_record_scrub" for p in errors)


def test_stdlib_exception_logs_keep_their_frames_and_no_marker(capsys: pytest.CaptureFixture[str]) -> None:
    with capture_telemetry() as tel:
        try:
            raise ValueError(PROMPT)
        except ValueError:
            logging.getLogger("app").exception("failed")
        (record,) = [r for r in tel.log_records() if r.body == "failed"]
        tel.assert_no_prohibited_content(PROMPT)
    attrs = dict(record.attributes or {})
    assert "hmcts.exception.formatted" not in attrs
    assert 'File "' in attrs["exception.stacktrace"] and "ValueError: [REDACTED]" in attrs["exception.stacktrace"]
    (line,) = [json.loads(x) for x in capsys.readouterr().out.splitlines() if '"failed"' in x]
    assert "hmcts.exception.formatted" not in line and 'File "' in line["exception.stacktrace"]


def test_azure_path_scrubs_log_records_before_the_distro_exports_them(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.monitor.opentelemetry as distro
    import azure.monitor.opentelemetry.exporter as exporter_mod
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

    from hmcts_otel import _setup as setup_mod

    exported = InMemoryLogRecordExporter()
    providers: list[LoggerProvider] = []

    def fake_configure(**kwargs: Any) -> None:
        # As the distro's _setup_logging: custom processors, then its exporting processor.
        provider = LoggerProvider()
        for processor in kwargs.get("log_record_processors", []):
            provider.add_log_record_processor(processor)
        provider.add_log_record_processor(SimpleLogRecordProcessor(exported))
        providers.append(provider)

    class FakeExporter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(distro, "configure_azure_monitor", fake_configure)
    monkeypatch.setattr(exporter_mod, "AzureMonitorTraceExporter", FakeExporter)
    monkeypatch.setattr(setup_mod.trace, "set_tracer_provider", lambda tp: None)
    setup_mod.setup_telemetry(
        TelemetrySettings(
            service_name="t",
            connection_string=SecretStr("InstrumentationKey=00000000-0000-0000-0000-000000000000"),
            sentry_dsn=None,
        )
    )
    try:
        (provider,) = providers
        provider.get_logger("x").emit(
            LogRecord(body={"content": PROMPT}, attributes={"event.name": "gen_ai.user.message"})
        )
        provider.get_logger("x").emit(LogRecord(body=f"contact {FICTIONAL_EMAIL}"))
    finally:
        setup_mod.shutdown_telemetry()
    genai, other = [getattr(item, "log_record", item) for item in exported.get_finished_logs()]
    assert genai.body is None and other.body == "contact [REDACTED]"


# --- N2: browser keys are hashed in drop warnings -------------------------------------

PERSON_KEY = "fictional_jane_smith"


def _relay_client() -> Any:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hmcts_otel import TelemetrySchema
    from hmcts_otel.fastapi import create_telemetry_relay_router

    schema = TelemetrySchema()
    schema.attribute("ui.mode", allowed_values={"dictation", "upload"})
    schema.attribute("ui.unused")
    schema.event("ui.clicked", attributes=["ui.mode"], browser=True)
    app = FastAPI()
    app.include_router(create_telemetry_relay_router(schema=schema))
    return TestClient(app)


def _lenient() -> TelemetrySettings:
    return TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None, log_format="json", strict=False)


def _drop_warnings(tel: TelemetryCapture) -> list[str]:
    return [str(r.body) for r in tel.log_records() if "dropped attribute" in str(r.body)]


def test_relay_drop_warnings_hash_unregistered_keys() -> None:
    from hmcts_otel import events as events_mod

    events_mod._warned.clear()
    with capture_telemetry(_lenient()) as tel:
        response = _relay_client().post(
            "/telemetry",
            json={
                "events": [
                    {
                        "name": "ui.clicked",
                        "attributes": {PERSON_KEY: True, "Jane Smith 07700900123": 1, "ui.unused": "x"},
                    }
                ]
            },
        )
        assert response.status_code < 300
        warnings = _drop_warnings(tel)
        tel.assert_no_prohibited_content(PERSON_KEY, "Jane Smith")
    assert len(warnings) == 3
    text = " ".join(warnings)
    assert text.count("sha256:") == 2 and PERSON_KEY not in text
    assert "'ui.unused'" in text  # registered for another event: shown as written


def test_server_side_drop_warnings_are_unchanged() -> None:
    from hmcts_otel import events as events_mod
    from hmcts_otel import record_event

    events_mod._warned.clear()
    with capture_telemetry(_lenient()) as tel:
        record_event("ui.server_side", {PERSON_KEY: 1, "Smith v Jones notes": 2})
        text = " ".join(_drop_warnings(tel))
    assert f"'{PERSON_KEY}'" in text  # attribute-like keys from code are shown
    assert "Smith" not in text and text.count("sha256:") == 1
