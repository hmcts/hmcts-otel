"""Hardened Sentry setup for error triage (``hmcts-otel[sentry]``).

Sentry's role is narrow: grouping and triaging errors. Tracing, dashboards and
alerting live in OpenTelemetry / App Insights / Dynatrace. Defaults:

* no PII, no request bodies, no local variables in stack frames;
* exception messages replaced unless ``HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES`` is set
  (the same policy as the rest of the package);
* every string in error events, transactions and breadcrumbs passes through the
  redactor, and query-string fields (``http.query`` and similar) are removed;
* request data reduced to method and path (query string removed);
* events tagged with the OpenTelemetry trace ID and journey, so Sentry issues can
  be joined to App Insights.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import sentry_sdk
from opentelemetry import trace

from hmcts_otel import conventions as c
from hmcts_otel.context import context_attributes
from hmcts_otel.conventions import REDACTED
from hmcts_otel.redaction import Redactor, default_redactor
from hmcts_otel.settings import TelemetrySettings

_KEEP_REQUEST_KEYS = ("method", "url")
_MAX_DEPTH = 12
# Keys whose values are query strings or fragments, wherever they appear
# (breadcrumb data, span data, contexts).
_QUERY_KEYS = frozenset({"http.query", "http.fragment", "query_string", "url.query", "url.fragment"})


def _redact_deep(value: Any, redactor: Redactor, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        return {
            (redactor.redact(k) if isinstance(k, str) else k): _redact_deep(v, redactor, depth + 1)
            for k, v in value.items()
            if not (isinstance(k, str) and k in _QUERY_KEYS)
        }
    if isinstance(value, list | tuple):
        return [_redact_deep(v, redactor, depth + 1) for v in value]
    return value


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


def _scrub_request_and_user(event: dict[str, Any]) -> None:
    request = event.get("request")
    if isinstance(request, dict):
        kept = {k: request[k] for k in _KEEP_REQUEST_KEYS if k in request}
        if isinstance(kept.get("url"), str):
            kept["url"] = _strip_query(kept["url"])
        event["request"] = kept
    user = event.get("user")
    if isinstance(user, dict):
        event["user"] = {"id": user["id"]} if "id" in user else {}


def _tag_with_context(event: dict[str, Any]) -> None:
    tags = event.setdefault("tags", {})
    if not isinstance(tags, dict):
        return
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        tags["trace_id"] = format(span_context.trace_id, "032x")
    for key, value in context_attributes().items():
        if key in (c.JOURNEY_TYPE, c.JOURNEY_ID, c.REQUEST_ID):
            tags[key] = value


def scrub_event(
    event: dict[str, Any], redactor: Redactor, *, include_exception_messages: bool = False
) -> dict[str, Any]:
    """Scrub an error event (also used for transactions).

    Exception messages are replaced unless ``include_exception_messages`` (the same
    policy as the rest of hmcts-otel, ``HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES``).
    Stack frames keep file, function and source lines; local variables are never
    collected (``include_local_variables=False``).
    """
    _scrub_request_and_user(event)
    if not include_exception_messages:
        exception = event.get("exception")
        values = exception.get("values") if isinstance(exception, dict) else None
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and item.get("value"):
                    item["value"] = REDACTED
    _tag_with_context(event)
    return _redact_deep(event, redactor)


def scrub_breadcrumb(crumb: dict[str, Any], redactor: Redactor) -> dict[str, Any] | None:
    data = crumb.get("data")
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        data["url"] = _strip_query(data["url"])
    return _redact_deep(crumb, redactor)


def init_sentry(
    settings: TelemetrySettings,
    *,
    redactor: Redactor | None = None,
    integrations: Sequence[Any] = (),
    before_send: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None,
) -> bool:
    """Initialise Sentry if a DSN is configured. Returns whether Sentry was initialised.

    ``before_send`` runs after the built-in scrubbing, for app-specific filtering.
    """
    if settings.sentry_dsn is None or not settings.sentry_dsn.get_secret_value():
        return False
    red = redactor or default_redactor

    include_messages = settings.capture_exception_messages

    def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        scrubbed = scrub_event(event, red, include_exception_messages=include_messages)
        return before_send(scrubbed, hint) if before_send is not None else scrubbed

    def _before_send_transaction(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        # before_send does not run for transactions; span descriptions and data can
        # carry URLs with query strings.
        return scrub_event(event, red, include_exception_messages=include_messages)

    def _before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
        return scrub_breadcrumb(crumb, red)

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        environment=settings.sentry_environment or settings.environment,
        release=settings.service_version,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profiles_sample_rate=0.0,
        before_send=_before_send,  # type: ignore[arg-type]
        before_send_transaction=_before_send_transaction,  # type: ignore[arg-type]
        before_breadcrumb=_before_breadcrumb,  # type: ignore[arg-type]
        integrations=list(integrations),
    )
    return True
