"""Carry trace context across processes and time, e.g. through a job table.

A batch job is created in a request, then processed minutes or hours later by a
poller or worker. Store ``capture_trace_context()`` on the job row when it is
created, then pass it to ``stage(..., link_from=...)`` in the worker. The worker
span is *linked* to the original request rather than parented, which is the
OpenTelemetry convention for asynchronous work.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Link, Span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_PROPAGATOR = TraceContextTextMapPropagator()


def capture_trace_context() -> str | None:
    """Return the current W3C ``traceparent``, or ``None`` if there is no active span."""
    carrier: dict[str, str] = {}
    _PROPAGATOR.inject(carrier)
    return carrier.get("traceparent")


def links_from(traceparent: str | None) -> list[Link]:
    """Links to the span identified by a stored ``traceparent``. Invalid input yields no links."""
    if not traceparent:
        return []
    ctx = _PROPAGATOR.extract({"traceparent": traceparent})
    span_context = trace.get_current_span(ctx).get_span_context()
    return [Link(span_context)] if span_context.is_valid else []


@contextmanager
def background_span(name: str, *, tracer_name: str = "hmcts_otel") -> Iterator[Span]:
    """Start a new trace for background work, linked to the current span.

    ``asyncio.create_task`` copies the request's context, so a span started in the
    task would become a child of a request span that has already ended: the request
    shows as fast with a long orphaned child, and the task cannot be queried as its
    own operation. This starts a root span instead, with a link to the originating
    span. Journey and request IDs (context variables) still flow, so logs and events
    from the task stay correlated.
    """
    from hmcts_otel import _state

    origin = trace.get_current_span().get_span_context()
    links = [Link(origin)] if origin.is_valid else []
    tracer = _state.get_tracer(tracer_name)
    with tracer.start_as_current_span(name, context=otel_context.Context(), links=links) as span:
        yield span
