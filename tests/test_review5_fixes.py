"""Regression tests for the fifth review round (PR #4)."""

from __future__ import annotations

import time

import pytest

from hmcts_otel.redaction import default_redactor

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
