"""hmcts.area: the product area of a service that serves more than one (DIAAT-205).

Set from context with ``area()``, stamped on events, stage events, log records and
spans; never on metrics; never settable by callers or the browser relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator

import pytest
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from hmcts_otel import (
    EventSource,
    TelemetrySchema,
    area,
    current_area,
    get_tracer,
    journey,
    record_event,
    stage,
)
from hmcts_otel import conventions as c
from hmcts_otel.fastapi import area_dependency, create_telemetry_relay_router, instrument_fastapi
from hmcts_otel.settings import TelemetrySettings
from hmcts_otel.testing import TelemetryCapture, capture_telemetry

log = logging.getLogger("tests.area")


@pytest.fixture
def lenient() -> Iterator[TelemetryCapture]:
    settings = TelemetrySettings(service_name="t", connection_string=None, sentry_dsn=None, strict=False)
    with capture_telemetry(settings) as capture:
        yield capture


def _log_attributes(tel: TelemetryCapture, body: str) -> dict[str, object]:
    (record,) = [r for r in tel.log_records() if r.body == body]
    return dict(record.attributes or {})


def _drops(tel: TelemetryCapture, reason: str) -> int:
    return sum(
        int(p["value"]) for p in tel.metric_points(c.METRIC_DROPPED_ATTRIBUTES) if p["attributes"]["reason"] == reason
    )


def test_everything_emitted_in_the_block_carries_the_area(tel: TelemetryCapture) -> None:
    with area("recording"):
        assert current_area() == "recording"
        record_event("job.submitted")
        with stage("upload"):
            log.warning("inside the stage")
        with get_tracer().start_as_current_span("plain span"):
            pass

    assert current_area() is None
    assert {e[c.AREA] for e in tel.events()} == {"recording"}
    assert tel.event_names() == ["job.submitted", "upload.started", "upload.completed"]
    assert _log_attributes(tel, "inside the stage")[c.AREA] == "recording"
    assert {span.attributes[c.AREA] for span in tel.finished_spans()} == {"recording"}


def test_nothing_outside_a_block_carries_one(tel: TelemetryCapture) -> None:
    """Unset means shared code (dependency health, auth, the relay)."""
    record_event("health.checked")
    log.warning("shared")
    with get_tracer().start_as_current_span("shared span"):
        pass
    assert c.AREA not in tel.events("health.checked")[0]
    assert c.AREA not in _log_attributes(tel, "shared")
    assert c.AREA not in tel.finished_spans()[0].attributes


def test_the_innermost_block_wins_and_the_outer_one_is_restored(tel: TelemetryCapture) -> None:
    with area("dictation"):
        with area("recording"):
            record_event("inner.event")
        record_event("outer.event")
    assert tel.events("inner.event")[0][c.AREA] == "recording"
    assert tel.events("outer.event")[0][c.AREA] == "dictation"


def test_it_is_carried_across_await_and_into_tasks_started_in_the_block(tel: TelemetryCapture) -> None:
    async def work(name: str) -> None:
        await asyncio.sleep(0)
        record_event(name)

    async def main() -> None:
        with area("dictation"):
            await work("awaited.event")
            await asyncio.gather(asyncio.create_task(work("task.event")))
        await work("after.event")

    asyncio.run(main())
    assert tel.events("awaited.event")[0][c.AREA] == "dictation"
    assert tel.events("task.event")[0][c.AREA] == "dictation"
    assert c.AREA not in tel.events("after.event")[0]


def test_metrics_do_not_carry_it(tel: TelemetryCapture) -> None:
    """Metric dimensions are explicit (decision 1): the area is not added to them."""
    with area("recording"), stage("upload"):
        pass
    (point,) = tel.metric_points(c.METRIC_STAGE_DURATION)
    assert c.AREA not in point["attributes"]
    for event_point in tel.metric_points(c.METRIC_EVENTS):
        assert c.AREA not in event_point["attributes"]


def test_a_caller_cannot_set_it_as_an_attribute(tel: TelemetryCapture) -> None:
    record_event("job.submitted", {c.AREA: "recording"})
    with area("dictation"):
        record_event("job.finished", {c.AREA: "recording"})
    assert c.AREA not in tel.events("job.submitted")[0]
    assert tel.events("job.finished")[0][c.AREA] == "dictation"  # metadata wins
    assert _drops(tel, "reserved") == 2


def test_the_browser_relay_cannot_set_it(tel: TelemetryCapture) -> None:
    schema = TelemetrySchema()
    schema.event("ui.clicked", browser=True)
    app = FastAPI()
    app.include_router(create_telemetry_relay_router(schema=schema, journey_types={"dictation.session"}))
    resp = TestClient(app).post(
        "/telemetry",
        content=json.dumps({"events": [{"name": "ui.clicked", "attributes": {c.AREA: "recording"}}]}),
        headers={"content-type": "application/json"},
    )
    assert resp.json() == {"accepted": 1, "rejected": 0}
    (event,) = tel.events("ui.clicked")
    assert c.AREA not in event
    assert event[c.EVENT_SOURCE] == EventSource.BROWSER.value
    assert _drops(tel, "reserved") == 1


@pytest.mark.parametrize("name", ["Recording", "recording area", "x" * 40, "", "PA/50123/2024"])
def test_a_malformed_name_is_misuse(tel: TelemetryCapture, name: str) -> None:
    with pytest.raises(ValueError):
        with area(name):
            pass


def test_a_malformed_name_without_strict_mode_leaves_the_area_unchanged(lenient: TelemetryCapture) -> None:
    with area("PA/50123/2024") as current:
        assert current is None
        record_event("job.submitted")
    with area("dictation"), area("PA/50123/2024") as current:
        assert current == "dictation"  # misuse does not erase the outer area
        record_event("job.finished")
    assert c.AREA not in lenient.events("job.submitted")[0]
    assert lenient.events("job.finished")[0][c.AREA] == "dictation"
    errors = {p["attributes"]["kind"]: p["value"] for p in lenient.metric_points(c.METRIC_TELEMETRY_ERRORS)}
    assert errors.get("invalid.area") == 2
    lenient.assert_no_prohibited_content("PA/50123/2024")


def test_journey_and_area_are_independent(tel: TelemetryCapture) -> None:
    journey_id = str(uuid.uuid4())
    with area("recording"), journey("recording.job", journey_id):
        record_event("job.submitted")
    event = tel.events("job.submitted")[0]
    assert event[c.AREA] == "recording" and event[c.JOURNEY_ID] == journey_id


def _recording_app() -> FastAPI:
    recording = APIRouter(dependencies=[Depends(area_dependency("recording"))])
    shared = APIRouter()

    @recording.get("/jobs")
    async def jobs() -> dict[str, str]:
        record_event("jobs.listed")
        return {"area": str(current_area())}

    @recording.get("/sync-jobs")
    def sync_jobs() -> dict[str, str]:  # a sync endpoint runs in a thread pool
        record_event("sync_jobs.listed")
        return {"area": str(current_area())}

    @recording.get("/failing")
    async def failing() -> None:
        raise LookupError("no such job")

    @recording.get("/later")
    async def later(tasks: BackgroundTasks) -> dict[str, bool]:
        tasks.add_task(record_event, "cleanup.done")
        return {"ok": True}

    @recording.get("/stream")
    def stream() -> StreamingResponse:
        def chunks() -> Iterator[bytes]:  # a sync generator: each chunk in a copied context
            record_event("chunk.sent")
            yield b"x"

        return StreamingResponse(chunks())

    @shared.get("/settings")
    async def user_settings() -> dict[str, str]:
        record_event("settings.read")
        return {"area": str(current_area())}

    app = FastAPI()
    app.include_router(recording)
    app.include_router(shared)

    @app.exception_handler(LookupError)
    async def not_found(_request: Request, _exc: LookupError) -> JSONResponse:
        log.warning("job lookup failed")
        return JSONResponse({"detail": "not found"}, status_code=404)

    instrument_fastapi(app)
    return app


def _server_spans(tel: TelemetryCapture) -> dict[object, object]:
    return {
        span.attributes.get("http.route"): span.attributes.get(c.AREA)
        for span in tel.finished_spans()
        if span.kind.name == "SERVER"
    }


def test_area_dependency_covers_the_endpoint_and_the_request_span(tel: TelemetryCapture) -> None:
    client = TestClient(_recording_app())
    assert client.get("/jobs").json() == {"area": "recording"}
    assert client.get("/sync-jobs").json() == {"area": "recording"}
    # The next request, on a shared router, does not inherit it.
    assert client.get("/settings").json() == {"area": "None"}

    assert tel.events("jobs.listed")[0][c.AREA] == "recording"
    assert tel.events("sync_jobs.listed")[0][c.AREA] == "recording"
    assert c.AREA not in tel.events("settings.read")[0]
    spans = _server_spans(tel)
    assert spans["/jobs"] == spans["/sync-jobs"] == "recording"
    assert spans["/settings"] is None


def test_area_dependency_lasts_for_the_rest_of_the_request(tel: TelemetryCapture) -> None:
    """Exception handlers, background tasks and streamed bodies run after a dependency
    has returned."""
    client = TestClient(_recording_app())
    assert client.get("/failing").status_code == 404
    assert client.get("/later").json() == {"ok": True}
    assert client.get("/stream").content == b"x"

    assert _log_attributes(tel, "job lookup failed")[c.AREA] == "recording"
    assert tel.events("cleanup.done")[0][c.AREA] == "recording"
    assert tel.events("chunk.sent")[0][c.AREA] == "recording"
    assert _server_spans(tel)["/failing"] == "recording"


def test_area_dependency_with_a_malformed_name_is_misuse_when_a_request_runs(tel: TelemetryCapture) -> None:
    router = APIRouter(dependencies=[Depends(area_dependency("Recording Area"))])

    @router.get("/jobs")
    async def jobs() -> None:
        return None

    app = FastAPI()
    app.include_router(router)
    with pytest.raises(ValueError):
        TestClient(app).get("/jobs")


def test_area_dependency_with_a_malformed_name_without_strict_mode(lenient: TelemetryCapture) -> None:
    router = APIRouter(dependencies=[Depends(area_dependency("Recording Area"))])

    @router.get("/jobs")
    async def jobs() -> dict[str, str]:
        record_event("jobs.listed")
        return {"area": str(current_area())}

    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).get("/jobs").json() == {"area": "None"}
    assert c.AREA not in lenient.events("jobs.listed")[0]
    errors = {p["attributes"]["kind"]: p["value"] for p in lenient.metric_points(c.METRIC_TELEMETRY_ERRORS)}
    assert errors.get("invalid.area") == 1


def test_a_log_record_cannot_carry_a_caller_supplied_area(tel: TelemetryCapture) -> None:
    log.warning("outside", extra={c.AREA: "recording"})
    with area("dictation"):
        log.warning("inside", extra={c.AREA: "recording"})
        log.warning("plain")
    assert c.AREA not in _log_attributes(tel, "outside")
    assert _log_attributes(tel, "inside")[c.AREA] == "dictation"
    assert _log_attributes(tel, "plain")[c.AREA] == "dictation"
    assert _drops(tel, "reserved") == 2


def test_sentry_events_are_tagged_with_it() -> None:
    from hmcts_otel.sentry import _tag_with_context

    event: dict[str, object] = {}
    with area("recording"):
        _tag_with_context(event)
    assert event["tags"] == {c.AREA: "recording"}
