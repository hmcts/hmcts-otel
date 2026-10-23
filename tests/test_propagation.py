from hmcts_otel import capture_trace_context, get_tracer, journey, links_from, stage
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture


def test_job_created_in_request_is_linked_from_worker(tel: TelemetryCapture, journey_id: str) -> None:
    # Request: create the job and store the traceparent on the row.
    with get_tracer().start_as_current_span("POST /jobs") as request_span:
        stored_traceparent = capture_trace_context()
    assert stored_traceparent is not None

    # Later, in another process: the worker picks the job up.
    with journey("recording.job", journey_id):
        with stage("transcription", link_from=stored_traceparent):
            pass

    worker_span = next(s for s in tel.finished_spans() if s.name == "transcription")
    (link,) = worker_span.links
    assert link.context.trace_id == request_span.get_span_context().trace_id
    assert link.context.span_id == request_span.get_span_context().span_id
    # The worker's events are queryable by job ID regardless of trace.
    assert tel.events("transcription.completed")[0][c.JOURNEY_ID] == journey_id


def test_no_context_and_bad_input() -> None:
    assert capture_trace_context() is None
    assert links_from(None) == []
    assert links_from("not-a-traceparent") == []
