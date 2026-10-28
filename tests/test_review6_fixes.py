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


# Oversized tokens fail closed (#9) ---------------------------------------------------


def test_an_oversized_bearer_token_is_redacted_whole() -> None:
    out = default_redactor.redact("Authorization: Bearer " + "A" * 5_000 + "ZSUFFIX")
    assert "ZSUFFIX" not in out and "AAAA" not in out


@pytest.mark.parametrize(
    "token",
    [
        "eyJ" + "a" * 9_000 + "." + "b" * 20 + "." + "c" * 20,  # header past the old bound
        "eyJ" + "a" * 30 + "." + "b" * 9_000 + "." + "c" * 20,  # payload past the old bound
        "eyJ" + "a" * 30 + "." + "b" * 30,  # two segments only (signature cut off)
        "eyJ" + "a" * 40,  # no dot: a truncated token
    ],
)
def test_an_oversized_or_partial_jwt_is_redacted_whole(token: str) -> None:
    out = default_redactor.redact(f"token {token} end")
    assert out == "token [REDACTED] end"


def test_short_tokens_and_prose_are_unchanged() -> None:
    assert default_redactor.redact("eyJa.b.c") == "[REDACTED]"  # still a JWT shape
    assert default_redactor.redact("the bearer of the warrant") == "the bearer of the warrant"
    assert default_redactor.redact("eyJohn") == "eyJohn"


@pytest.mark.parametrize(
    "text",
    ["-eyJ" * 15_000, "eyJa." * 12_000, "bearer " * 9_000, "bearer " + ("A" * 15 + " bearer ") * 3_000],
)
def test_token_rules_stay_linear_time(text: str) -> None:
    import time

    start = time.perf_counter()
    default_redactor.redact(text)
    assert time.perf_counter() - start < 0.5


# Long URLs keep no query (PR #4 review) ----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "GET https://example.test/" + "a" * 2_100 + "?q=QUERY_SENTINEL 200",  # past the old bound
        "redirect=https://example.test/" + "a" * 5_000 + "?code=QUERY_SENTINEL",
        "https://a.test/p/https://b.test/q?x=QUERY_SENTINEL",  # a nested scheme
        "GET /" + "b" * 3_000 + "?token=QUERY_SENTINEL HTTP/1.1",  # a long relative path
    ],
)
def test_a_long_url_keeps_no_query(text: str) -> None:
    out = default_redactor.redact(text)
    assert "QUERY_SENTINEL" not in out and "?[REDACTED]" in out


def test_a_url_without_a_query_is_unchanged() -> None:
    url = "https://example.test/" + "a" * 3_000 + "#fragment"
    assert default_redactor.redact(url) == url


@pytest.mark.parametrize(
    "text",
    ["https://" * 7_000, "http://a" * 7_000, "https://" * 7_000 + "?x=1", "https://" + "a" * 58_000, "/x" * 20_000],
)
def test_url_rules_stay_linear_time(text: str) -> None:
    import time

    start = time.perf_counter()
    default_redactor.redact(text)
    assert time.perf_counter() - start < 0.5


# URL userinfo fails closed (PR #4 review) --------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://fictional_user:" + "A" * 257 + "@example.test/path",  # password past the old bound
        "https://" + "fictional_user" * 30 + ":pw@example.test/path",  # username past the old bound
        "https://FICTIONAL_TOKEN@localhost/path",  # username only
        "https://fictional_user:@example.test/",  # empty password
        "postgresql://fictional_user:secret.value@db.example.test:5432/app",
    ],
)
def test_url_userinfo_is_redacted_whole(url: str) -> None:
    out = default_redactor.redact(f"connecting to {url} now")
    assert "fictional_user" not in out and "FICTIONAL_TOKEN" not in out
    assert "AAAA" not in out and "secret" not in out and ":pw@" not in out
    assert out.endswith(" now")


def test_an_at_sign_in_the_path_is_not_userinfo() -> None:
    assert default_redactor.redact("https://example.test/path@thing") == "https://example.test/path@thing"


def test_userinfo_whose_at_sign_is_past_the_inspected_text_fails_closed() -> None:
    cut = MAX_TEXT_LENGTH + redaction._REDACTION_MARGIN
    text = "x" * (MAX_TEXT_LENGTH - 50) + " https://fictional_user:" + "P" * cut + "@example.test/p"
    out = default_redactor.redact(text)
    assert "fictional_user" not in out and "PPPP" not in out
    assert out.endswith(redaction.TRUNCATED)
    assert "url_userinfo" in default_redactor.matches(text)


def test_a_long_url_without_userinfo_past_the_cut_is_not_reported() -> None:
    text = "x" * (MAX_TEXT_LENGTH + redaction._REDACTION_MARGIN + 10) + " https://example.test/ok"
    assert "url_userinfo" not in default_redactor.matches(text)


@pytest.mark.parametrize(
    "text",
    ["://" * 20_000, "://a" * 15_000, "https://" + "a" * 58_000, "x" * 40_000 + " https://" + "u" * 90_000],
)
def test_userinfo_rule_stays_linear_time(text: str) -> None:
    import time

    start = time.perf_counter()
    default_redactor.redact(text)
    default_redactor.matches(text)
    assert time.perf_counter() - start < 0.5


# Round 5: long-URL scan, secret-named keys, static metadata --------------------------


def test_the_prohibited_content_scan_finds_a_query_in_a_very_long_string() -> None:
    # Exported strings the scan checks can be longer than the old window (attributes joined
    # as JSON, for example); a match longer than the window overlap was missed.
    from hmcts_otel.testing import _matching_rules

    text = "https://example.test/" + "a" * 70_000 + "?q=PRIVATE_SENTINEL"
    assert "url_query" in _matching_rules(default_redactor.rules, text)
    assert _matching_rules(default_redactor.rules, "https://example.test/" + "a" * 70_000) == []


@pytest.mark.parametrize(
    "key",
    [
        "AccountKey",
        "password",
        "db.password",
        "x-api-key",
        "AzureStorageAccountKey",
        "client_secret",
        "Authorization",
        "access_token",
        "SharedAccessSignature",
        "connection_string",
        "privateKey",
    ],
)
def test_secret_named_keys_are_recognised(key: str) -> None:
    from hmcts_otel.redaction import is_secret_key

    assert is_secret_key(key)


@pytest.mark.parametrize(
    "key", ["design", "token_count", "assignment", "config", "key", "account", "secretary", "sigma"]
)
def test_ordinary_keys_are_not_secret(key: str) -> None:
    from hmcts_otel.redaction import is_secret_key

    assert not is_secret_key(key)


def test_sentry_drops_secret_named_structured_values() -> None:
    from hmcts_otel.sentry import scrub_event

    event = {
        "extra": {"AccountKey": "FICTIONAL_CREDENTIAL", "note": "ok"},
        "breadcrumbs": {"values": [{"data": {"x-api-key": "FICTIONAL_CREDENTIAL"}}]},
        "contexts": {"db": {"connection_string": "FICTIONAL_CREDENTIAL"}},
    }
    out = scrub_event(event, default_redactor)
    assert "FICTIONAL_CREDENTIAL" not in str(out)
    assert out["extra"]["note"] == "ok"


def test_secret_named_log_extras_and_span_attributes_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    from hmcts_otel import get_tracer

    settings = TelemetrySettings(
        service_name="test", environment="test", connection_string=None, sentry_dsn=None, log_format="json"
    )
    with capture_telemetry(settings) as tel:
        logging.getLogger("app").warning("saved", extra={"api_key": "FICTIONAL_CREDENTIAL", "count": 3})
        with get_tracer().start_as_current_span("call") as span:
            span.set_attribute("http.request.header.authorization", "FICTIONAL_CREDENTIAL")
        tel.assert_no_prohibited_content("FICTIONAL_CREDENTIAL")  # exported logs and spans
    assert "FICTIONAL_CREDENTIAL" not in capsys.readouterr().out  # the JSON stdout lines


def test_static_service_metadata_is_redacted_in_json_logs(capsys: pytest.CaptureFixture[str]) -> None:
    import json
    import logging

    settings = TelemetrySettings(
        service_name="fictional.person@example.test",
        environment="test",
        connection_string=None,
        sentry_dsn=None,
        log_format="json",
        strict=False,
    )
    with capture_telemetry(settings):
        logging.getLogger("app").warning("plain")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert lines and all("fictional.person@example.test" not in json.dumps(line) for line in lines)


# Rule changes applied on GitHub (a54e756, c23498f) -----------------------------------


def test_single_quoted_secret_keys_are_redacted() -> None:
    assert "FICTIONAL" not in default_redactor.redact("{'password': 'FICTIONAL_PASSWORD'}")


def test_relative_path_queries_with_reserved_characters_are_redacted() -> None:
    out = default_redactor.redact("GET /files/a!b,c;d=e:f@g/report?token=QUERY_SENTINEL HTTP/1.1")
    assert "QUERY_SENTINEL" not in out and "?[REDACTED]" in out
