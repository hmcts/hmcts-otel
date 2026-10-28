"""Stages: the unit of journey instrumentation.

One ``with stage("upload"):`` block produces

* a span named after the stage (with outcome, error code and journey attributes),
* a ``<stage>.started`` event and a ``<stage>.completed`` / ``.failed`` / ``.cancelled`` event,
* a duration measurement on the ``hmcts.stage.duration`` histogram.

Unexpected exceptions are recorded by type and stack only (messages removed by
default) and re-raised unchanged. Telemetry problems never replace or mask the
application's own exception (see ``_guard``).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from hmcts_otel import _guard, _state
from hmcts_otel import conventions as c
from hmcts_otel._exceptions import exception_attributes
from hmcts_otel.context import _restore, _stage, current_journey
from hmcts_otel.conventions import Outcome
from hmcts_otel.events import record_event, report_dropped
from hmcts_otel.propagation import links_from
from hmcts_otel.schema import AttributeValue, TelemetrySchema, default_schema

_VERB = {
    Outcome.SUCCESS: "completed",
    Outcome.FAILURE: "failed",
    Outcome.ERROR: "failed",
    Outcome.CANCELLED: "cancelled",
}
INVALID_STAGE = "invalid_stage"
INVALID_CODE = "invalid_error_code"


class StageHandle:
    """Returned by ``stage()``. Use it to add attributes or mark a controlled failure."""

    def __init__(self, name: str, span: Span, schema: TelemetrySchema, attributes: dict[str, AttributeValue]):
        self.name = name
        self.span = span
        self._schema = schema
        self.attributes = attributes
        self.outcome: Outcome = Outcome.SUCCESS
        self.error_code: str | None = None

    def set_attribute(self, key: str, value: object) -> None:
        try:
            clean, dropped = self._schema.validate({key: value})
            if dropped:
                report_dropped(dropped, where=f"stage:{self.name}")
                return
            self.attributes.update(clean)
            self.span.set_attributes(clean)
        except Exception as exc:
            _guard.internal_error("stage_set_attribute", exc)

    def set_dependency_ref(self, ref: str) -> None:
        """Record the identifier of this work in an external system (e.g. an Azure Speech job ID)."""
        self.set_attribute(c.DEPENDENCY_REF, ref)

    def _code(self, error_code: str) -> str:
        try:
            return self._schema.check_identifier(c.ERROR_CODE, error_code)
        except ValueError as exc:
            _guard.invalid("error_code", f"invalid error code for stage {self.name!r}: {exc}")
            return INVALID_CODE

    def fail(self, error_code: str) -> None:
        """Mark a handled failure with a controlled error code. The block continues normally."""
        self.error_code = self._code(error_code)
        self.outcome = Outcome.FAILURE

    def error(self, error_code: str) -> None:
        """Mark a server-side or dependency failure (storage, external service) with a
        controlled code. Unlike ``fail``, the stage counts as an error (span status
        ERROR, outcome ``error``). The code is kept if an exception is then raised, so
        it is not replaced by ``unhandled.<Type>``."""
        self.error_code = self._code(error_code)
        self.outcome = Outcome.ERROR

    def cancel(self, error_code: str | None = None) -> None:
        if error_code is not None:
            self.error_code = self._code(error_code)
        self.outcome = Outcome.CANCELLED


_CLIENT_ERROR = range(400, 500)


def client_failure_code(exc: BaseException) -> str | None:
    """``http.<status>`` when ``exc`` is a client error (an HTTP 4xx exception), else None.

    The single classification rule: a client-caused rejection is a handled failure,
    anything else leaving a stage is an error. ``stage()`` applies it to exceptions
    raised inside the block, and services use it for job-level outcomes so the stage
    and the job agree.

    Only Starlette/FastAPI ``HTTPException`` is recognised (checked without importing
    Starlette). Other libraries' ``status_code`` attributes, e.g. an Azure SDK 404, are
    dependency failures, not client errors.
    """
    module = sys.modules.get("starlette.exceptions")
    http_exception = getattr(module, "HTTPException", None) if module is not None else None
    if http_exception is not None and isinstance(exc, http_exception):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status in _CLIENT_ERROR:
            return f"http.{status}"
    return None


def _duration_histogram() -> Any:
    return _state.instrument("histogram", c.METRIC_STAGE_DURATION, unit="s", description="Duration of a journey stage")


def _stage_name(schema: TelemetrySchema, name: str) -> str:
    try:
        return schema.check_identifier(c.STAGE, name)
    except ValueError as exc:
        _guard.invalid("stage_name", f"invalid stage name: {exc}")
        return INVALID_STAGE


@contextmanager
def stage(
    name: str,
    *,
    attributes: Mapping[str, object] | None = None,
    link_from: str | None = None,
    emit_started: bool = True,
    schema: TelemetrySchema | None = None,
) -> Iterator[StageHandle]:
    """Instrument one stage of a journey.

    ``link_from`` is a stored ``traceparent`` (see ``capture_trace_context``) used to
    link asynchronous work back to the request that created it.
    """
    schema = schema or default_schema
    stage_name = _stage_name(schema, name)

    clean: dict[str, AttributeValue] = {}
    try:
        clean, dropped = schema.validate(attributes or {})
        if dropped:
            report_dropped(dropped, where=f"stage:{stage_name}")
    except Exception as exc:
        _guard.internal_error("stage_validate", exc)

    span_cm: AbstractContextManager[Span] | None = None
    span: Span = trace.INVALID_SPAN
    try:
        span_cm = _state.get_tracer().start_as_current_span(
            stage_name,
            links=links_from(link_from),
            attributes={c.STAGE: stage_name, **clean},
            record_exception=False,
            set_status_on_exception=False,
        )
        span = span_cm.__enter__()
    except Exception as exc:
        span_cm = None
        _guard.internal_error("stage_span_start", exc)

    handle = StageHandle(stage_name, span, schema, dict(clean))
    # The innermost stage, e.g. the default feature for AI usage inside the block.
    previous_stage = _stage.get()
    stage_token = _stage.set(stage_name)
    try:
        if emit_started:
            record_event(f"{stage_name}.started", {c.STAGE: stage_name, **clean}, schema=schema)
        start = time.perf_counter()
        try:
            yield handle
        except (asyncio.CancelledError, GeneratorExit):
            handle.outcome = Outcome.CANCELLED
            handle.error_code = handle.error_code or "cancelled"
            _finish_safely(handle, span, time.perf_counter() - start, None, business_error=True)
            raise
        except BaseException as exc:
            client_code = client_failure_code(exc) if handle.outcome is Outcome.SUCCESS else None
            if handle.outcome is Outcome.FAILURE or client_code is not None:
                # fail(code) then raise, or a client 4xx raised anywhere in the block
                # (e.g. a helper's 404): a handled failure, not an unexpected error.
                handle.outcome = Outcome.FAILURE
                handle.error_code = handle.error_code or client_code
                _finish_safely(handle, span, time.perf_counter() - start, None, business_error=True)
                raise
            handle.outcome = Outcome.ERROR
            handle.error_code = handle.error_code or f"unhandled.{type(exc).__name__}"[:64]
            _finish_safely(handle, span, time.perf_counter() - start, exc, business_error=True)
            raise
        else:
            _finish_safely(handle, span, time.perf_counter() - start, None, business_error=False)
    finally:
        _restore(_stage, stage_token, previous_stage)
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception as exc:
                _guard.internal_error("stage_span_end", exc)


def _finish_safely(
    handle: StageHandle, span: Span, duration_s: float, exc: BaseException | None, *, business_error: bool
) -> None:
    try:
        _finish(handle, span, duration_s, exc)
    except Exception as err:
        # Never replace the application's exception. Without one in flight, strict
        # mode surfaces the telemetry bug in tests.
        if _guard.strict() and not business_error:
            raise
        _guard.internal_error("stage_finish", err)


def _finish(handle: StageHandle, span: Span, duration_s: float, exc: BaseException | None) -> None:
    outcome_attrs: dict[str, AttributeValue] = {c.OUTCOME: handle.outcome.value}
    if handle.error_code:
        outcome_attrs[c.ERROR_CODE] = handle.error_code
    span.set_attributes(outcome_attrs)
    # Only unexpected errors fail the span. Handled failures (validation, user
    # cancellation) are recorded by outcome so they do not inflate failure rates.
    if handle.outcome is Outcome.ERROR:
        span.set_status(Status(StatusCode.ERROR, handle.error_code))
    if exc is not None:
        span.add_event(
            "exception",
            exception_attributes(
                exc, include_messages=_state.capture_exception_messages(), redactor=_state.current_redactor()
            ),
        )

    current = current_journey()
    _duration_histogram().record(
        duration_s,
        {
            c.STAGE: _guard.safe_dimension(handle.name),
            c.OUTCOME: handle.outcome.value,
            c.ERROR_CODE: _guard.safe_dimension(handle.error_code) if handle.error_code else "none",
            c.JOURNEY_TYPE: _guard.safe_dimension(current.type) if current is not None else "none",
        },
    )
    record_event(
        f"{handle.name}.{_VERB[handle.outcome]}",
        {**handle.attributes, c.STAGE: handle.name, **outcome_attrs},
        duration_ms=duration_s * 1000.0,
        schema=handle._schema,
    )
