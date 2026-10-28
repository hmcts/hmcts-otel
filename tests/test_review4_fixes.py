"""Regression tests for the fourth review round (PR #4)."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hmcts_otel import EventClass, TelemetrySchema, journey, record_event
from hmcts_otel import conventions as c
from hmcts_otel.context import request_id_context
from hmcts_otel.fastapi import RequestIdMiddleware, create_telemetry_relay_router
from hmcts_otel.redaction import default_redactor
from hmcts_otel.settings import TelemetrySettings
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

SESSION = "dictation.session"


def _relay_schema() -> TelemetrySchema:
    s = TelemetrySchema()
    s.attribute("ui.mode", allowed_values={"dictation", "upload"})
    s.event("ui.clicked", attributes=["ui.mode"], browser=True)
    # As courtstranscribe registers browser failure and support events.
    s.event("minutes.generation_failed", attributes=[c.OUTCOME, c.ERROR_CODE], browser=True)
    s.event("support.error_report_copied", event_class=EventClass.OPERATIONAL, attributes=[c.REQUEST_ID], browser=True)
    return s


def _client(schema: TelemetrySchema, **kwargs: object) -> TestClient:
    kwargs.setdefault("journey_types", {SESSION})
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)  # events carry the relay request's ID
    app.include_router(create_telemetry_relay_router(schema=schema, **kwargs))  # type: ignore[arg-type]
    return TestClient(app)


def _post(client: TestClient, events: list[dict[str, object]]) -> dict[str, int]:
    resp = client.post(
        "/telemetry", content=json.dumps({"events": events}), headers={"content-type": "application/json"}
    )
    assert resp.status_code == 202
    return resp.json()


def _reserved_drops(tel: TelemetryCapture) -> int:
    return sum(
        int(p["value"])
        for p in tel.metric_points(c.METRIC_DROPPED_ATTRIBUTES)
        if p["attributes"]["reason"] == "reserved"
    )


# --- #6: caller attributes cannot overwrite event metadata -------------------------

FORGED = {
    c.JOURNEY_TYPE: "recording.job",
    c.JOURNEY_ID: "0f8fad5b-d9cb-469f-a165-70867728950e",
    c.JOURNEY_PARENT_ID: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    c.EVENT_SOURCE: "server",
    c.EVENT_CLASS: "analytics",
    c.DURATION_MS: 1.0,
    c.REQUEST_ID: "9b2f3c1d4e5f60718293a4b5c6d7e8f9",
}


def test_relay_attributes_cannot_overwrite_metadata(tel: TelemetryCapture) -> None:
    own = str(uuid.uuid4())
    attrs = {**FORGED, c.OUTCOME: "failure", c.ERROR_CODE: "forged.code", c.STAGE: "upload", "ui.mode": "upload"}
    result = _post(
        _client(_relay_schema()), [{"name": "ui.clicked", "attributes": attrs, "journey": {"type": SESSION, "id": own}}]
    )
    assert result == {"accepted": 1, "rejected": 0}
    (ev,) = tel.events("ui.clicked")
    assert ev[c.JOURNEY_TYPE] == SESSION and ev[c.JOURNEY_ID] == own
    assert c.JOURNEY_PARENT_ID not in ev
    assert ev[c.EVENT_SOURCE] == "browser" and ev[c.EVENT_CLASS] == "operational"
    assert c.DURATION_MS not in ev
    assert c.REQUEST_ID in ev and ev[c.REQUEST_ID] != FORGED[c.REQUEST_ID]
    # Core keys the event does not register are refused from a browser.
    assert c.OUTCOME not in ev and c.ERROR_CODE not in ev and c.STAGE not in ev
    assert ev["ui.mode"] == "upload"
    assert _reserved_drops(tel) == len(FORGED)
    tel.assert_no_prohibited_content(FORGED[c.JOURNEY_ID], FORGED[c.JOURNEY_PARENT_ID], "recording.job", "forged.code")


def test_relay_journey_types_cannot_be_bypassed_with_attributes(tel: TelemetryCapture) -> None:
    victim = str(uuid.uuid4())
    attrs = {c.JOURNEY_TYPE: "recording.job", c.JOURNEY_ID: victim}
    _post(_client(_relay_schema()), [{"name": "ui.clicked", "attributes": attrs}])
    (ev,) = tel.events("ui.clicked")
    assert c.JOURNEY_TYPE not in ev and c.JOURNEY_ID not in ev
    tel.assert_no_prohibited_content(victim, "recording.job")


def test_relay_accepts_outcome_and_error_code_where_registered(tel: TelemetryCapture) -> None:
    attrs = {c.OUTCOME: "error", c.ERROR_CODE: "minutes.timeout"}
    _post(_client(_relay_schema()), [{"name": "minutes.generation_failed", "attributes": attrs}])
    (ev,) = tel.events("minutes.generation_failed")
    assert ev[c.OUTCOME] == "error" and ev[c.ERROR_CODE] == "minutes.timeout"
    assert ev[c.EVENT_SOURCE] == "browser"


def test_relay_accepts_a_request_id_where_registered(tel: TelemetryCapture) -> None:
    reported = uuid.uuid4().hex
    _post(_client(_relay_schema()), [{"name": "support.error_report_copied", "attributes": {c.REQUEST_ID: reported}}])
    (ev,) = tel.events("support.error_report_copied")
    assert ev[c.REQUEST_ID] == reported


def test_server_outcome_and_error_code_still_accepted(tel: TelemetryCapture) -> None:
    record_event("job.finished", {c.OUTCOME: "failure", c.ERROR_CODE: "speech.timeout"})
    (ev,) = tel.events("job.finished")
    assert ev[c.OUTCOME] == "failure" and ev[c.ERROR_CODE] == "speech.timeout"
    assert _reserved_drops(tel) == 0


def test_server_attributes_cannot_overwrite_metadata(tel: TelemetryCapture) -> None:
    jid, rid = str(uuid.uuid4()), uuid.uuid4().hex
    with request_id_context(rid), journey("upload.job", jid):
        record_event("job.finished", {**FORGED, c.OUTCOME: "success"}, duration_ms=5)
    (ev,) = tel.events("job.finished")
    assert ev[c.JOURNEY_TYPE] == "upload.job" and ev[c.JOURNEY_ID] == jid and c.JOURNEY_PARENT_ID not in ev
    assert ev[c.EVENT_SOURCE] == "server" and ev[c.EVENT_CLASS] == "operational"
    assert ev[c.REQUEST_ID] == rid and ev[c.DURATION_MS] == 5.0
    assert ev[c.OUTCOME] == "success"
    assert _reserved_drops(tel) == len(FORGED)


@pytest.mark.parametrize("key", sorted(FORGED))
def test_each_reserved_key_is_dropped_without_a_journey(tel: TelemetryCapture, key: str) -> None:
    forged = {**FORGED, c.EVENT_SOURCE: "browser"}[key]
    record_event("job.finished", {key: forged})
    (ev,) = tel.events("job.finished")
    assert ev.get(key) != forged
    assert _reserved_drops(tel) == 1


# --- #7: the relay denies journeys by default ---------------------------------------


def test_relay_without_journey_types_refuses_journeys(tel: TelemetryCapture) -> None:
    sid = str(uuid.uuid4())
    client = _client(_relay_schema(), journey_types=None)
    result = _post(client, [{"name": "ui.clicked", "journey": {"type": SESSION, "id": sid}}, {"name": "ui.clicked"}])
    assert result == {"accepted": 1, "rejected": 1}
    (ev,) = tel.events("ui.clicked")
    assert c.JOURNEY_ID not in ev
    tel.assert_no_prohibited_content(sid)


def test_relay_with_journey_types_accepts_a_listed_type(tel: TelemetryCapture) -> None:
    sid = str(uuid.uuid4())
    result = _post(_client(_relay_schema()), [{"name": "ui.clicked", "journey": {"type": SESSION, "id": sid}}])
    assert result == {"accepted": 1, "rejected": 0}
    (ev,) = tel.events("ui.clicked")
    assert ev[c.JOURNEY_TYPE] == SESSION and ev[c.JOURNEY_ID] == sid


# --- #8: browser journey IDs must be UUID or 32-hex ---------------------------------


@pytest.mark.parametrize("bad", ["Jane_Smith", "07700900123", "PA-50123-2024", "a" * 32 + "-x"])
def test_relay_refuses_journey_ids_that_are_not_uuids(tel: TelemetryCapture, bad: str) -> None:
    result = _post(_client(_relay_schema()), [{"name": "ui.clicked", "journey": {"type": SESSION, "id": bad}}])
    assert result == {"accepted": 0, "rejected": 1}
    assert tel.events("ui.clicked") == []
    tel.assert_no_prohibited_content(bad)


@pytest.mark.parametrize("sid", [str(uuid.uuid4()), uuid.uuid4().hex, str(uuid.uuid4()).upper()])
def test_relay_accepts_uuid_and_hex_journey_ids(tel: TelemetryCapture, sid: str) -> None:
    result = _post(_client(_relay_schema()), [{"name": "ui.clicked", "journey": {"type": SESSION, "id": sid}}])
    assert result == {"accepted": 1, "rejected": 0}
    assert tel.events("ui.clicked")[0][c.JOURNEY_ID] == sid


def test_relay_drops_browser_parent_ids_by_default(tel: TelemetryCapture) -> None:
    sid, parent = str(uuid.uuid4()), str(uuid.uuid4())
    events = [{"name": "ui.clicked", "journey": {"type": SESSION, "id": sid, "parent_id": parent}}]
    assert _post(_client(_relay_schema()), events) == {"accepted": 1, "rejected": 0}
    (ev,) = tel.events("ui.clicked")
    assert ev[c.JOURNEY_ID] == sid and c.JOURNEY_PARENT_ID not in ev
    tel.assert_no_prohibited_content(parent)


def test_relay_keeps_a_uuid_parent_id_when_allowed(tel: TelemetryCapture) -> None:
    sid, parent = str(uuid.uuid4()), str(uuid.uuid4())
    events = [
        {"name": "ui.clicked", "journey": {"type": SESSION, "id": sid, "parent_id": parent}},
        {"name": "ui.clicked", "journey": {"type": SESSION, "id": sid, "parent_id": "07700900123"}},
    ]
    assert _post(_client(_relay_schema(), allow_parent_id=True), events) == {"accepted": 1, "rejected": 1}
    (ev,) = tel.events("ui.clicked")
    assert ev[c.JOURNEY_PARENT_ID] == parent
    tel.assert_no_prohibited_content("07700900123")


# --- #2: a failed redaction forwards no extras ---------------------------------------

SENTINEL = "sentinel@example.test"


@pytest.fixture
def lenient() -> Iterator[TelemetryCapture]:
    settings = TelemetrySettings(
        service_name="t", connection_string=None, sentry_dsn=None, log_format="json", strict=False
    )
    with capture_telemetry(settings) as capture:
        yield capture


def _broken(self: Any, *args: Any) -> Any:
    raise RuntimeError("redactor broken")


@pytest.mark.parametrize("method", ["_redact", "_safe_extra"])
def test_failed_redaction_drops_extras(
    lenient: TelemetryCapture, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    monkeypatch.setattr(f"hmcts_otel._logging.RedactingFilter.{method}", _broken)
    logging.getLogger("app").warning("contact %s", SENTINEL, extra={"person": SENTINEL, "case.ref": [SENTINEL]})
    (record,) = [r for r in lenient.log_records() if r.body == "[unformattable log record]"]
    assert "person" not in dict(record.attributes or {})
    lenient.assert_no_prohibited_content(SENTINEL)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    # Fail closed: the logger name is replaced too (it may have been built from data).
    (line,) = [entry for entry in lines if entry.get("message") == "[unformattable log record]"]
    assert line["logger"] == "[unformattable logger name]" and line["level"] == "WARNING"
    assert SENTINEL not in json.dumps(line)
    assert not default_redactor.matches(json.dumps(line))


# --- #10: quoted and connection-string secrets are redacted in full ------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('password="two words"', 'password="[REDACTED]"'),
        ("password='a,b&c'", "password='[REDACTED]'"),
        ('{"password": "two words", "user": "u1"}', '{"password": "[REDACTED]", "user": "u1"}'),
        (r'client_secret="a\"b c" tail', 'client_secret="[REDACTED]" tail'),
        (r"pwd='it\'s secret' tail", "pwd='[REDACTED]' tail"),
        ('password="unclosed two words', 'password="[REDACTED]'),
        ("Server=db;Uid=u;Password=two words;Encrypt=yes", "Server=db;Uid=u;Password=[REDACTED];Encrypt=yes"),
        ("Server=db;PWD=two words", "Server=db;PWD=[REDACTED]"),
        ("Pwd={two words;x};Uid=u", "Pwd={[REDACTED]};Uid=u"),
        ("AccountKey=s3cr3t==;EndpointSuffix=x", "AccountKey=[REDACTED];EndpointSuffix=x"),
        # A key=value log line keeps its other fields.
        ("password=hunter2 user=u1 retry=3", "password=[REDACTED] user=u1 retry=3"),
    ],
)
def test_quoted_and_connection_string_secrets_are_redacted_in_full(text: str, expected: str) -> None:
    assert default_redactor.redact(text) == expected
    assert default_redactor.redact(expected) == expected  # idempotent


@pytest.mark.parametrize(
    "text",
    [
        "Your password is incorrect, please try again.",
        "The password policy requires 12 characters; see the guidance.",
        "Passwords and secrets are never logged.",
        'He said "password" twice.',
    ],
)
def test_prose_about_passwords_is_left_alone(text: str) -> None:
    assert default_redactor.redact(text) == text


def test_quoted_value_at_the_bound_is_redacted_in_full() -> None:
    value = "x y" * 1365 + "z"  # 4096 characters
    assert len(value) == 4096
    assert default_redactor.redact(f'password="{value}" tail') == 'password="[REDACTED]" tail'


@pytest.mark.parametrize(
    "text",
    [
        'password="' * 30_000,
        "password='" * 30_000,
        'password="' + "\\" * 200_000,
        "password=" * 30_000,
        "password=" + "a " * 100_000,
        "Pwd={" * 40_000,
        "pwd=" + "a" * 200_000 + ";",
    ],
    ids=["dquotes", "squotes", "backslashes", "keys", "spaced", "braces", "long_odbc"],
)
def test_secret_rule_is_fast_on_adversarial_input(text: str) -> None:
    started = time.perf_counter()
    default_redactor.redact(text)
    assert time.perf_counter() - started < 0.5
