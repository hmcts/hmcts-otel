import asyncio

import pytest
from opentelemetry.trace import StatusCode

from hmcts_otel import EventClass, EventSource, TelemetrySchema, journey, record_event, stage
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture


def test_successful_stage(tel: TelemetryCapture, schema: TelemetrySchema, journey_id: str) -> None:
    with journey("recording.job", journey_id, parent_id="hearing-1"):
        with stage("upload", attributes={"file.size_bytes": 42}, schema=schema) as s:
            s.set_dependency_ref("0c1f2e3d-speech")

    assert tel.event_names() == ["upload.started", "upload.completed"]
    done = tel.events("upload.completed")[0]
    assert done[c.OUTCOME] == "success"
    assert done[c.JOURNEY_ID] == journey_id and done[c.JOURNEY_PARENT_ID] == "hearing-1"
    assert done["file.size_bytes"] == 42 and done[c.DEPENDENCY_REF] == "0c1f2e3d-speech"
    assert done[c.DURATION_MS] >= 0

    (span,) = tel.finished_spans()
    assert span.name == "upload"
    assert span.attributes[c.JOURNEY_TYPE] == "recording.job"
    assert span.status.status_code is StatusCode.UNSET

    (point,) = tel.metric_points(c.METRIC_STAGE_DURATION)
    assert point["count"] == 1
    assert point["attributes"] == {
        c.STAGE: "upload",
        c.OUTCOME: "success",
        c.ERROR_CODE: "none",
        c.JOURNEY_TYPE: "recording.job",
    }
    # Journey IDs never become metric dimensions (cardinality).
    assert c.JOURNEY_ID not in point["attributes"]


def test_controlled_failure(tel: TelemetryCapture) -> None:
    with stage("validation") as s:
        s.fail("format.unsupported")
    failed = tel.events("validation.failed")[0]
    assert failed[c.OUTCOME] == "failure" and failed[c.ERROR_CODE] == "format.unsupported"
    (span,) = tel.finished_spans()
    # Handled failures are an outcome, not a span error (M4).
    assert span.status.status_code is StatusCode.UNSET
    assert span.attributes[c.OUTCOME] == "failure"


def test_fail_rejects_free_text(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        with stage("validation") as s:
            s.fail("the file called appeal 123 was bad")


def test_unhandled_exception_recorded_without_message(tel: TelemetryCapture) -> None:
    with pytest.raises(RuntimeError):
        with stage("post_processing"):
            content = "speaker Jane Doe said something private"
            raise RuntimeError(content)
    failed = tel.events("post_processing.failed")[0]
    assert failed[c.OUTCOME] == "error" and failed[c.ERROR_CODE] == "unhandled.RuntimeError"
    (span,) = tel.finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    (exc_event,) = span.events
    assert exc_event.attributes["exception.type"] == "RuntimeError"
    tel.assert_no_prohibited_content("Jane Doe")


async def test_cancellation_and_async_context(tel: TelemetryCapture, journey_id: str) -> None:
    async def work() -> None:
        with journey("dictation.session", journey_id):
            with stage("speech_session"):
                await asyncio.sleep(10)

    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled = tel.events("speech_session.cancelled")[0]
    assert cancelled[c.OUTCOME] == "cancelled" and cancelled[c.JOURNEY_ID] == journey_id


def test_stage_name_validation(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        with stage("Upload Stage"):
            pass


def test_dropped_attributes_are_counted(tel: TelemetryCapture) -> None:
    with stage("upload", attributes={"original.file_name": "PA_50123_2024.mp3"}):
        pass
    assert "original.file_name" not in tel.events("upload.completed")[0]
    points = tel.metric_points(c.METRIC_DROPPED_ATTRIBUTES)
    assert points and points[0]["attributes"]["reason"] == "unregistered"
    tel.assert_no_prohibited_content("PA_50123_2024")


def test_record_event_registered_class_wins(tel: TelemetryCapture, schema: TelemetrySchema) -> None:
    schema.event("feature.used", event_class=EventClass.ANALYTICS, attributes=["file.format"])
    record_event("feature.used", {"file.format": "wav"}, event_class=EventClass.OPERATIONAL, schema=schema)
    (ev,) = tel.events("feature.used")
    assert ev[c.EVENT_CLASS] == "analytics" and ev[c.EVENT_SOURCE] == "server"
    (point,) = tel.metric_points(c.METRIC_EVENTS)
    assert point["attributes"] == {"event": "feature.used", "class": "analytics", "source": "server"}


def test_record_event_duration_and_source(tel: TelemetryCapture) -> None:
    record_event("speech.first_result", source=EventSource.BROWSER, duration_ms=1500)
    (ev,) = tel.events("speech.first_result")
    assert ev[c.DURATION_MS] == 1500.0 and ev[c.EVENT_SOURCE] == "browser"
    (point,) = tel.metric_points(c.METRIC_EVENT_DURATION)
    assert point["sum"] == pytest.approx(1.5)


def test_record_event_rejects_bad_names(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        record_event("Speech First Result")


def test_journey_rejects_business_identifiers(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        with journey("recording.job", "PA/50123/2024"):
            pass


def test_fail_then_raise_is_a_handled_failure(tel: TelemetryCapture) -> None:
    class HTTPError(Exception):
        pass

    with pytest.raises(HTTPError):
        with stage("upload") as s:
            s.fail("upload.too_large")
            raise HTTPError("413")
    failed = tel.events("upload.failed")[0]
    assert failed[c.OUTCOME] == "failure" and failed[c.ERROR_CODE] == "upload.too_large"
    (span,) = tel.finished_spans()
    assert span.status.status_code is StatusCode.UNSET
    assert span.events == ()
