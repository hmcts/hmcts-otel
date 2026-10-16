import json
import uuid

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from hmcts_otel import EventClass, TelemetrySchema, get_request_id
from hmcts_otel import conventions as c
from hmcts_otel.fastapi import create_telemetry_relay_router, instrument_fastapi
from hmcts_otel.testing import TelemetryCapture


@pytest.fixture
def relay_schema() -> TelemetrySchema:
    s = TelemetrySchema()
    s.attribute("speech.cancel_reason", allowed_values={"error", "end_of_stream"})
    s.attribute("speech.error_code", type=int)
    s.attribute("ui.mode", allowed_values={"dictation", "upload"})
    s.event("speech.session_canceled", attributes=["speech.cancel_reason", "speech.error_code"], browser=True)
    s.event("feature.opened", event_class=EventClass.ANALYTICS, attributes=["ui.mode"], browser=True)
    s.event("server.only")
    return s


def _require_user(request: Request) -> str:
    if request.headers.get("x-user") is None:
        raise HTTPException(status_code=401)
    return request.headers["x-user"]


def _app(schema: TelemetrySchema, **kwargs) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_telemetry_relay_router(
            schema=schema,
            path="/api/telemetry",
            dependencies=[Depends(_require_user)],
            identity=lambda r: r.headers.get("x-user"),
            analytics_consent=lambda r: r.cookies.get("analytics") == "on",
            **kwargs,
        )
    )
    return app


def _post(client: TestClient, events: list[dict], **kwargs):
    return client.post(
        "/api/telemetry",
        content=json.dumps({"events": events}),
        headers={"x-user": "u1", "content-type": "application/json", **kwargs.pop("headers", {})},
        **kwargs,
    )


def test_relay_records_valid_browser_events(tel: TelemetryCapture, relay_schema: TelemetrySchema) -> None:
    client = TestClient(_app(relay_schema))
    jid = str(uuid.uuid4())
    resp = _post(
        client,
        [
            {
                "name": "speech.session_canceled",
                "attributes": {"speech.cancel_reason": "error", "speech.error_code": 4},
                "journey": {"type": "dictation.session", "id": jid},
                "duration_ms": 1200,
            },
            {"name": "server.only"},  # not browser-enabled
            {"name": "never.registered"},
            {"name": "speech.session_canceled", "journey": {"type": "dictation.session", "id": "PA/1/2"}},
        ],
    )
    assert resp.status_code == 202
    assert resp.json() == {"accepted": 1, "rejected": 3}
    (ev,) = tel.events("speech.session_canceled")
    assert ev[c.EVENT_SOURCE] == "browser" and ev[c.JOURNEY_ID] == jid
    assert ev["speech.cancel_reason"] == "error" and ev["speech.error_code"] == 4


def test_relay_drops_unknown_attributes_and_never_echoes(tel: TelemetryCapture, relay_schema: TelemetrySchema) -> None:
    client = TestClient(_app(relay_schema))
    resp = _post(
        client, [{"name": "speech.session_canceled", "attributes": {"transcript": "the appellant said private things"}}]
    )
    assert resp.status_code == 202
    tel.assert_no_prohibited_content("the appellant said")
    bad = client.post(
        "/api/telemetry",
        content='{"events": [{"name": "x", "secret": "echo me"}]}',
        headers={"x-user": "u", "content-type": "application/json"},
    )
    assert bad.status_code == 422 and "echo me" not in bad.text


def test_relay_analytics_requires_consent(tel: TelemetryCapture, relay_schema: TelemetrySchema) -> None:
    client = TestClient(_app(relay_schema))
    event = [{"name": "feature.opened", "attributes": {"ui.mode": "upload"}}]
    assert _post(client, event).json() == {"accepted": 0, "rejected": 1}
    client.cookies.set("analytics", "on")
    assert _post(client, event).json() == {"accepted": 1, "rejected": 0}
    assert tel.events("feature.opened")[0][c.EVENT_CLASS] == "analytics"


def test_relay_auth_size_and_rate_limits(tel: TelemetryCapture, relay_schema: TelemetrySchema) -> None:
    client = TestClient(_app(relay_schema, max_body_bytes=200, rate_limit_per_minute=2))
    assert client.post("/api/telemetry", content='{"events": []}').status_code == 401
    assert _post(client, [{"name": "server.only", "attributes": {"x": "y" * 300}}]).status_code == 413
    # Oversized and unauthenticated requests are refused before the limiter counts them.
    assert _post(client, []).status_code == 202
    assert _post(client, []).status_code == 202
    assert _post(client, []).status_code == 429


def test_relay_rejects_oversized_batches(tel: TelemetryCapture, relay_schema: TelemetrySchema) -> None:
    client = TestClient(_app(relay_schema))
    assert _post(client, [{"name": "server.only"}] * 51).status_code == 422


def test_instrument_fastapi_spans_request_id_and_exclusions(tel: TelemetryCapture) -> None:
    app = FastAPI()
    seen: dict[str, str | None] = {}

    @app.get("/api/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        seen["rid"] = get_request_id()
        return {"ok": item_id}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    instrument_fastapi(app)
    client = TestClient(app)
    rid = "0f8fad5b-d9cb-469f-a165-70867728950e"
    resp = client.get("/api/items/7?q=private", headers={"X-Request-Id": rid})
    assert resp.headers["X-Request-Id"] == rid and seen["rid"] == rid
    generated = client.get("/api/items/8", headers={"X-Request-Id": "bad id with spaces"})
    assert generated.headers["X-Request-Id"] != "bad id with spaces"
    client.get("/health")

    server = [s for s in tel.finished_spans() if s.kind.name == "SERVER"]
    assert {s.name for s in server} == {"GET /api/items/{item_id}"}
    first = next(s for s in server if s.attributes.get(c.REQUEST_ID) == rid)
    assert first is not None
    tel.assert_no_prohibited_content("q=private")


def test_relay_rejects_journeys_outside_the_allowed_types():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from hmcts_otel import TelemetrySchema
    from hmcts_otel.fastapi import create_telemetry_relay_router
    from hmcts_otel.testing import capture_telemetry

    schema = TelemetrySchema()
    schema.event("ui.clicked", browser=True)
    app = FastAPI()
    app.include_router(create_telemetry_relay_router(schema=schema, journey_types={"browser.session"}))
    client = TestClient(app)
    sid = "6f1c2d3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
    with capture_telemetry():
        response = client.post(
            "/telemetry",
            json={
                "events": [
                    {"name": "ui.clicked", "journey": {"type": "browser.session", "id": sid}},
                    {"name": "ui.clicked", "journey": {"type": "recording.job", "id": sid}},  # a server journey
                    {"name": "ui.clicked"},
                ]
            },
        )
    assert response.json() == {"accepted": 2, "rejected": 1}
