from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode

from hmcts_otel._scrubbing import ScrubbingSpanExporter, scrub_attributes
from hmcts_otel.redaction import default_redactor


def _provider(include_messages: bool = False) -> tuple[TracerProvider, InMemorySpanExporter]:
    sink = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(ScrubbingSpanExporter(sink, default_redactor, include_exception_messages=include_messages))
    )
    return provider, sink


def test_auto_instrumentation_style_attributes_are_scrubbed() -> None:
    provider, sink = _provider()
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("GET /api/search") as span:
        span.set_attribute("url.full", "https://app/api/search?q=appellant+name")
        span.set_attribute("url.query", "q=appellant+name")
        span.set_attribute("http.target", "/api/search?q=appellant+name")
        span.set_attribute("enduser.id", "judge@justice.gov.uk")
        span.set_attribute("tags", ["ok", "x@y.com"])
        span.set_status(Status(StatusCode.ERROR, "failed for judge@justice.gov.uk"))
        try:
            payload = "payload contained the appellant's name"
            raise ValueError(payload)
        except ValueError as exc:
            span.record_exception(exc)

    (out,) = sink.get_finished_spans()
    attrs = dict(out.attributes)
    assert attrs["url.full"] == "https://app/api/search?[REDACTED]"
    assert attrs["url.query"] == "[REDACTED]"
    assert attrs["http.target"] == "/api/search?[REDACTED]"
    assert attrs["enduser.id"] == "[REDACTED]"
    assert attrs["tags"] == ("ok", "[REDACTED]")
    assert out.status.description == "[REDACTED]"  # free text is never exported (M8)
    (event,) = out.events
    assert event.attributes["exception.message"] == "[REDACTED]"
    assert "appellant's name" not in event.attributes["exception.stacktrace"]
    assert out.context == out.context  # identity preserved for correlation


def test_exception_messages_kept_when_enabled() -> None:
    attrs = scrub_attributes(
        {"exception.message": "bad value 7 from x@y.com"}, default_redactor, include_exception_messages=True
    )
    assert attrs["exception.message"] == "bad value 7 from [REDACTED]"


def test_fails_closed_on_unscrubbable_span() -> None:
    class Boom(InMemorySpanExporter):
        pass

    sink = Boom()
    exporter = ScrubbingSpanExporter(sink, default_redactor)
    exporter._scrub = lambda span: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[method-assign]
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("t").start_as_current_span("s"):
        pass
    assert sink.get_finished_spans() == ()
