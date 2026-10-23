"""Regression tests for the fifth review round (PR #4)."""

from __future__ import annotations

import logging
import time

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan

from hmcts_otel.redaction import MAX_TEXT_LENGTH, default_redactor
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
