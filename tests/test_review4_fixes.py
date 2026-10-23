"""Regression tests for the fourth review round (PR #4)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hmcts_otel import EventClass, TelemetrySchema, journey, record_event
from hmcts_otel import conventions as c
from hmcts_otel.context import request_id_context
from hmcts_otel.fastapi import RequestIdMiddleware, create_telemetry_relay_router
from hmcts_otel.testing import TelemetryCapture

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
