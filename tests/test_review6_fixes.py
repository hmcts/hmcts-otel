"""PR #4 review, round 4: JWT margin, metric dimensions and URL userinfo order."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hmcts_otel import TelemetrySettings, journey, record_event, redaction, stage
from hmcts_otel import conventions as c
from hmcts_otel._guard import safe_dimension
from hmcts_otel.redaction import MAX_TEXT_LENGTH, Redactor, default_redactor
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

# A JWT straddling the truncation boundary -------------------------------------------


def _jwt(header: int, payload: int, signature: int) -> str:
    return "eyJ" + "a" * header + "." + "b" * payload + "." + "c" * signature


def test_the_margin_covers_the_longest_default_match() -> None:
    assert redaction._REDACTION_MARGIN >= redaction._LONGEST_DEFAULT_MATCH
    longest = _jwt(8_192 - 3, 8_192, 8_192)  # the JWT rule's longest possible match
    assert default_redactor.redact(longest) == redaction.REDACTED


@pytest.mark.parametrize(("header", "payload"), [(5_400, 6_700), (8_189, 8_192)])
def test_a_jwt_straddling_the_truncation_boundary_is_fully_redacted(header: int, payload: int) -> None:
    token = _jwt(header, payload, 200)
    text = "x" * (MAX_TEXT_LENGTH - 128) + " " + token + " tail"
    out = default_redactor.redact(text)
    assert "eyJ" not in out and "aaaa" not in out  # no part of the token survives the cut
    assert out.endswith(redaction.TRUNCATED)


# Metric dimensions follow the active redactor ---------------------------------------


@pytest.fixture
def custom() -> Redactor:
    r = Redactor()
    r.add_rule("case_ref", r"\b(?:AB\d{5}|case\d{4})\b")
    return r


def _lenient(redactor: Redactor) -> Iterator[TelemetryCapture]:
    settings = TelemetrySettings(
        service_name="test", environment="test", connection_string=None, sentry_dsn=None, strict=False
    )
    return capture_telemetry(settings, redactor=redactor)


def _duration_points(tel: TelemetryCapture) -> list[dict]:
    return tel.metric_points(c.METRIC_STAGE_DURATION)


def test_schema_validation_uses_the_configured_redactor(custom: Redactor) -> None:
    with capture_telemetry(redactor=custom):
        with pytest.raises(ValueError), stage("upload") as s:
            s.error("AB12345")  # passes the code pattern, but matches the custom rule


def test_a_rejected_error_code_never_reaches_a_metric_dimension(custom: Redactor) -> None:
    with _lenient(custom) as tel:
        with stage("upload") as s:
            s.error("AB12345")
        dims = [p["attributes"] for p in _duration_points(tel)]
        assert dims and all(d.get(c.ERROR_CODE) != "AB12345" for d in dims)
        tel.assert_no_prohibited_content("AB12345")


def test_stage_names_and_journey_types_are_checked_against_the_active_redactor(custom: Redactor) -> None:
    with _lenient(custom) as tel:
        with journey("case1234.job", str(uuid.uuid4())):  # rejected: runs without a journey
            with stage("case1234"):
                pass
        dims = [p["attributes"] for p in _duration_points(tel)]
        assert all("case1234" not in str(v) for d in dims for v in d.values())
        record_event("case1234.done")
        tel.assert_no_prohibited_content("case1234")


def test_safe_dimension_replaces_values_matching_the_active_redactor(custom: Redactor) -> None:
    with _lenient(custom):
        assert safe_dimension("upload") == "upload"
        assert safe_dimension("AB12345") == "redacted"
        assert safe_dimension("AB12345", "none") == "none"


def test_an_explicit_schema_redactor_still_wins(custom: Redactor) -> None:
    from hmcts_otel.schema import TelemetrySchema

    with capture_telemetry(redactor=custom):
        assert TelemetrySchema().redactor is custom  # follows the active redactor
        assert TelemetrySchema(redactor=default_redactor).redactor is default_redactor


# URL userinfo before email ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://fictional_user:dummy_credential@example.test/path",
        "https://fictional_user:dummy_credential@sub.example.test:8443/a?b",
        "postgresql://app_user:s3cret.value@db.internal.example/app",
    ],
)
def test_url_credentials_are_removed_before_the_email_rule(url: str) -> None:
    out = default_redactor.redact(f"connecting to {url}")
    assert "fictional_user" not in out and "dummy_credential" not in out
    assert "app_user" not in out and "s3cret" not in out
    assert default_redactor.matches(out) == []


def test_plain_emails_are_still_redacted() -> None:
    assert default_redactor.redact("contact judge.smith@example.test now") == "contact [REDACTED] now"
