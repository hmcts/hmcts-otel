import pytest

from hmcts_otel import Redactor, redact
from hmcts_otel._exceptions import exception_attributes, format_exception
from hmcts_otel.redaction import default_redactor


def test_redacts_default_patterns() -> None:
    text = (
        "user judge.smith@justice.gov.uk token Bearer abc.def-ghi.jkl-mnopq "
        "jwt eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl "
        "conn AccountKey=s3cr3t==;EndpointSuffix=x sas ?sv=1&sig=abcd%2F& "
        "https://user:pass@host/x"
    )
    out = redact(text)
    for secret in ("judge.smith@justice.gov.uk", "abc.def-ghi", "eyJhbGciOi", "s3cr3t", "abcd%2F", "user:pass"):
        assert secret not in out
    assert "AccountKey=[REDACTED]" in out
    assert "EndpointSuffix=x" in out


def test_strips_query_strings_from_urls_in_text() -> None:
    out = redact('HTTP Request: GET https://speech.example/jobs?name=PA_50123&x=1 "HTTP/1.1 200 OK"')
    assert out == 'HTTP Request: GET https://speech.example/jobs?[REDACTED] "HTTP/1.1 200 OK"'


def test_redacted_output_does_not_match_rules() -> None:
    once = redact("https://a/b?q=1 AccountKey=k password=p x@y.com")
    assert redact(once) == once
    assert default_redactor.matches(once) == []


@pytest.mark.parametrize(
    "address",
    ["sentinel@example.test", "o'brien@example.test", "foo!bar@example.test", "a.b#c$d%e&f@example.test", "u+tag@x.co"],
)
def test_email_rule_covers_rfc5322_local_parts(address: str) -> None:
    assert redact(f"user {address} signed in") == "user [REDACTED] signed in"


def test_leaves_ordinary_text_alone() -> None:
    text = "stage upload failed error_code=SPEECH_TIMEOUT after 3 retries"
    assert redact(text) == text


def test_custom_rule_and_matches_never_return_values() -> None:
    r = Redactor()
    r.add_rule("appeal_reference", r"\b[A-Z]{2}/\d{5}/\d{4}\b")
    assert r.redact("appeal PA/50123/2024 listed") == "appeal [REDACTED] listed"
    assert r.matches("appeal PA/50123/2024") == ["appeal_reference"]
    assert default_redactor.matches("PA/50123/2024") == []


INNER = "inner secret transcript"
OUTER = "outer secret john@example.com"


def _raise_chain() -> None:
    # Content arrives in variables, as in real code; source lines appear in stack traces.
    try:
        raise KeyError(INNER)
    except KeyError as inner:
        raise ValueError(OUTER) from inner


def test_exception_formatting_removes_every_message_in_chain() -> None:
    try:
        _raise_chain()
    except ValueError as exc:
        text = format_exception(exc, include_messages=False, redactor=default_redactor)
        attrs = exception_attributes(exc, include_messages=False, redactor=default_redactor)
    assert "inner secret" not in text and "outer secret" not in text
    assert "KeyError: [REDACTED]" in text and "ValueError: [REDACTED]" in text
    assert "direct cause" in text
    assert "exception.message" not in attrs
    assert attrs["exception.type"] == "ValueError"


def test_exception_formatting_with_messages_still_redacts_patterns() -> None:
    try:
        _raise_chain()
    except ValueError as exc:
        attrs = exception_attributes(exc, include_messages=True, redactor=default_redactor)
    assert "outer secret" in attrs["exception.message"]
    assert "john@example.com" not in attrs["exception.message"]
