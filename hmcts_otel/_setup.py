"""setup_telemetry / shutdown_telemetry.

Azure path (connection string set):
  * tracing: our own TracerProvider with the Application Insights sampler, the
    journey span processor, and the Azure exporter wrapped in ScrubbingSpanExporter;
  * metrics, logs and auto-instrumentation: the Azure Monitor distro, with its own
    tracing disabled so instrumentations use our provider. The distro's log handler
    gets the redaction and context filters.

Custom path (exporters passed in, e.g. tests): SDK providers with those exporters.

No exporters: stdout logging only; OpenTelemetry API calls are no-ops.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from hmcts_otel import _span_exceptions, _state
from hmcts_otel._logging import StdoutHandler, add_filters, configure_logging, make_filters, remove_handlers
from hmcts_otel._scrubbing import ScrubbingSpanExporter
from hmcts_otel._state import LOCK, STATE
from hmcts_otel.context import JourneySpanProcessor
from hmcts_otel.redaction import Redactor, default_redactor
from hmcts_otel.settings import TelemetrySettings, load_settings

_log = logging.getLogger("hmcts_otel")
_atexit_registered = False
# Tracer providers this package registered globally. A later setup in the same
# process (e.g. a retry after a failed setup) must not mistake them for foreign ones.
_OWN_TRACER_PROVIDERS: set[int] = set()

# FastAPI is instrumented per app by hmcts_otel.fastapi.instrument_fastapi, which
# works whatever the import order. The distro's global instrumentation only affects
# apps created after setup, so it is disabled to avoid double instrumentation.
DEFAULT_INSTRUMENTATION_OPTIONS: Mapping[str, Mapping[str, bool]] = {"fastapi": {"enabled": False}}


def _resource(settings: TelemetrySettings) -> Resource:
    attrs: dict[str, Any] = {
        "service.name": settings.service_name,
        "deployment.environment": settings.environment,
        "deployment.environment.name": settings.environment,
    }
    if settings.service_version:
        attrs["service.version"] = settings.service_version
    instance = os.environ.get("HOSTNAME") or os.environ.get("WEBSITE_INSTANCE_ID")
    if instance:
        attrs["service.instance.id"] = instance
    return Resource.create(attrs)


def _credential(settings: TelemetrySettings) -> Any:
    if not settings.use_entra_auth:
        return None
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError("use_entra_auth requires the hmcts-otel[entra] extra (azure-identity)") from exc
    return DefaultAzureCredential()


def _scrubbing(exporter: SpanExporter, settings: TelemetrySettings, redactor: Redactor) -> ScrubbingSpanExporter:
    return ScrubbingSpanExporter(exporter, redactor, include_exception_messages=settings.capture_exception_messages)


def _check_tracer_provider(settings: TelemetrySettings) -> None:
    """Fail if a TracerProvider is already registered globally.

    OpenTelemetry allows the global provider to be set once. If something else set
    it (a leftover ``configure_azure_monitor()``, another library), ours would be
    ignored and auto-instrumented spans would be exported without scrubbing.
    """
    current = trace.get_tracer_provider()
    if isinstance(current, trace.ProxyTracerProvider) or id(current) in _OWN_TRACER_PROVIDERS:
        return
    message = (
        "hmcts_otel: another OpenTelemetry TracerProvider is already registered, so auto-instrumented "
        "spans would bypass hmcts-otel scrubbing. Remove other OpenTelemetry/Azure Monitor setup "
        "(e.g. configure_azure_monitor) and call setup_telemetry first."
    )
    if settings.allow_foreign_tracer_provider:
        _log.error(message)
        return
    raise RuntimeError(message)


@contextmanager
def _env_override(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _sdk_disabled() -> bool:
    """The standard OpenTelemetry kill switch (used e.g. by CI import smoke tests)."""
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def _metric_views() -> list[Any]:
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    from hmcts_otel import conventions as c

    buckets = ExplicitBucketHistogramAggregation(boundaries=list(c.DURATION_BUCKETS_S))
    token_buckets = ExplicitBucketHistogramAggregation(boundaries=[float(b) for b in c.GEN_AI_TOKEN_BUCKETS])
    return [
        View(instrument_name=c.METRIC_STAGE_DURATION, aggregation=buckets),
        View(instrument_name=c.METRIC_EVENT_DURATION, aggregation=buckets),
        View(instrument_name=c.METRIC_GEN_AI_TOKEN_USAGE, aggregation=token_buckets),
    ]


# Variables that switch prompt/response content capture on in GenAI instrumentation:
# the OpenTelemetry GenAI instrumentations (openai-v2, google-genai, vertexai, ...)
# and the Azure AI SDKs' tracing. All default to off; setup makes that explicit, so
# instrumentation added later by a service, or by a dependency, starts without content.
GENAI_CONTENT_CAPTURE_VARIABLES = (
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
    "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED",
)
_CONTENT_OFF = frozenset({"", "false", "0", "no_content"})


def _genai_content_defaults() -> None:
    """Default GenAI content capture to off. An explicit opt-in is kept, with a warning
    (content attributes are still removed from exported spans)."""
    try:
        for name in GENAI_CONTENT_CAPTURE_VARIABLES:
            value = os.environ.get(name)
            if value is None:
                os.environ[name] = "false"
            elif value.strip().lower() not in _CONTENT_OFF:
                _log.warning(
                    "hmcts_otel: %s is enabled; GenAI instrumentation may capture prompt and response content", name
                )
    except Exception:  # pragma: no cover - os.environ does not fail in practice
        pass


def _setup_azure(
    settings: TelemetrySettings,
    resource: Resource,
    redactor: Redactor,
    instrumentation_options: Mapping[str, Mapping[str, bool]] | None,
    extra_span_exporter: SpanExporter | None,
    metric_readers: Sequence[Any],
) -> None:
    from azure.monitor.opentelemetry import configure_azure_monitor
    from azure.monitor.opentelemetry.exporter import ApplicationInsightsSampler, AzureMonitorTraceExporter

    assert settings.connection_string is not None
    conn = settings.connection_string.get_secret_value()
    credential = _credential(settings)
    exporter_kwargs: dict[str, Any] = {"connection_string": conn}
    if credential is not None:
        exporter_kwargs["credential"] = credential

    tracer_provider = TracerProvider(
        sampler=ApplicationInsightsSampler(sampling_ratio=settings.sampling_ratio),
        resource=resource,
        shutdown_on_exit=False,  # shutdown_telemetry owns shutdown, with a time budget
    )
    tracer_provider.add_span_processor(JourneySpanProcessor())
    tracer_provider.add_span_processor(
        BatchSpanProcessor(_scrubbing(AzureMonitorTraceExporter(**exporter_kwargs), settings, redactor))
    )
    if extra_span_exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(_scrubbing(extra_span_exporter, settings, redactor)))
    trace.set_tracer_provider(tracer_provider)
    _OWN_TRACER_PROVIDERS.add(id(tracer_provider))
    STATE.tracer_provider = tracer_provider  # set before the distro runs, so a failure can stop it

    root = logging.getLogger()
    before = set(root.handlers)
    options = {**DEFAULT_INSTRUMENTATION_OPTIONS, **(instrumentation_options or {})}
    # The distro overwrites the disable_tracing argument with a default derived from
    # OTEL_TRACES_EXPORTER (azure-monitor-opentelemetry 1.8.7-1.8.11,
    # _utils/configurations.py::_default_disable_tracing), so the argument alone would
    # still build a second tracer provider with an unscrubbed exporter. Set the
    # variable it actually reads for the duration of the call.
    with _env_override("OTEL_TRACES_EXPORTER", "none"):
        configure_azure_monitor(
            **exporter_kwargs,
            resource=resource,
            disable_tracing=True,
            logger_name="",
            instrumentation_options=options,
            enable_live_metrics=settings.enable_live_metrics,
            metric_readers=list(metric_readers),
            views=_metric_views(),
        )
    # The distro attaches its OpenTelemetry log handler to the root logger.
    filters = make_filters(redactor, settings)
    for handler in root.handlers:
        if handler not in before:
            add_filters(handler, filters)
            STATE.filtered_handlers.append(handler)

    STATE.tracer_provider = tracer_provider
    STATE.meter_provider = metrics.get_meter_provider()
    from opentelemetry._logs import get_logger_provider

    STATE.logger_provider = get_logger_provider()


def _otel_logging_handler(logger_provider: Any) -> logging.Handler:
    """The OpenTelemetry logging handler. The SDK's own ``LoggingHandler`` is deprecated
    (from SDK 1.40) in favour of opentelemetry-instrumentation-logging's, which the
    distro depends on and uses."""
    try:
        from opentelemetry.instrumentation.logging.handler import LoggingHandler

        return LoggingHandler(logger_provider=logger_provider)
    except ImportError:  # pragma: no cover - the distro always installs it
        import warnings

        from opentelemetry.sdk._logs import LoggingHandler as SdkLoggingHandler

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return SdkLoggingHandler(logger_provider=logger_provider)


def _setup_custom(
    settings: TelemetrySettings,
    resource: Resource,
    redactor: Redactor,
    span_exporter: SpanExporter | None,
    log_exporter: Any,
    metric_readers: Sequence[Any],
    set_global_providers: bool,
) -> None:
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider

    tracer_provider = TracerProvider(
        sampler=ParentBased(TraceIdRatioBased(settings.sampling_ratio)), resource=resource, shutdown_on_exit=False
    )
    tracer_provider.add_span_processor(JourneySpanProcessor())
    if span_exporter is not None:
        tracer_provider.add_span_processor(SimpleSpanProcessor(_scrubbing(span_exporter, settings, redactor)))

    logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
    if log_exporter is not None:
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
        handler = _otel_logging_handler(logger_provider)
        add_filters(handler, make_filters(redactor, settings))
        logging.getLogger().addHandler(handler)
        STATE.handlers.append(handler)

    meter_provider = MeterProvider(
        resource=resource, metric_readers=list(metric_readers), views=_metric_views(), shutdown_on_exit=False
    )

    if set_global_providers:
        from opentelemetry._logs import set_logger_provider

        trace.set_tracer_provider(tracer_provider)
        _OWN_TRACER_PROVIDERS.add(id(tracer_provider))
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)

    STATE.tracer_provider = tracer_provider
    STATE.meter_provider = meter_provider
    STATE.logger_provider = logger_provider
    STATE.owns_providers = True


def setup_telemetry(
    settings: TelemetrySettings | None = None,
    *,
    instrumentation_options: Mapping[str, Mapping[str, bool]] | None = None,
    span_exporter: SpanExporter | None = None,
    log_exporter: Any = None,
    metric_readers: Sequence[Any] = (),
    set_global_providers: bool = True,
    redactor: Redactor | None = None,
) -> TelemetrySettings:
    """Configure logging, tracing, metrics and (optionally) Sentry for this process.

    Safe to call more than once: later calls return the existing settings. Call it
    as early as possible, before creating clients you want instrumented.
    """
    global _atexit_registered
    with LOCK:
        if STATE.configured:
            assert STATE.settings is not None
            return STATE.settings
        if settings is None:
            settings, _ = load_settings()
        invalid_fields = settings.invalid_fields
        redactor = redactor or default_redactor
        if (settings.exports_to_azure and not _sdk_disabled()) or (set_global_providers and span_exporter is not None):
            # Deliberately fatal (unless overridden): exporting unscrubbed spans is worse
            # than failing at startup, where deployment checks catch it.
            _check_tracer_provider(settings)
        STATE.settings = settings
        STATE.redactor = redactor
        _genai_content_defaults()
        _span_exceptions.install()
        STATE.handlers = configure_logging(settings, redactor)
        if invalid_fields:
            _log.error("hmcts_otel: invalid configuration for %s; defaults used", ", ".join(invalid_fields))
        root = logging.getLogger()
        baseline = set(root.handlers)
        globals_before = _global_providers()
        resource = _resource(settings)

        try:
            if settings.exports_to_azure and _sdk_disabled():
                _log.info("hmcts_otel: OTEL_SDK_DISABLED=true; telemetry export disabled, logging to stdout only")
            elif settings.exports_to_azure:
                _setup_azure(settings, resource, redactor, instrumentation_options, span_exporter, metric_readers)
                STATE.owns_providers = False
            elif span_exporter is not None or log_exporter is not None or metric_readers:
                _setup_custom(
                    settings, resource, redactor, span_exporter, log_exporter, metric_readers, set_global_providers
                )
        except Exception as exc:
            # Roll back exporter wiring; keep stdout logging so the service still runs.
            for handler in list(root.handlers):
                if handler not in baseline:
                    root.removeHandler(handler)
            STATE.handlers = [h for h in STATE.handlers if h in baseline]
            STATE.filtered_handlers = []
            # Providers created by this attempt may already be registered globally
            # (OpenTelemetry cannot unregister them) with exporter threads running:
            # stop them so nothing is recorded or exported in the background.
            created = [STATE.tracer_provider, STATE.meter_provider, STATE.logger_provider]
            created += [p for p in _global_providers() if all(p is not b for b in globals_before)]
            _stop_providers(_unique(created), ROLLBACK_BUDGET_MS)
            STATE.tracer_provider = STATE.meter_provider = STATE.logger_provider = None
            if settings.strict:
                remove_handlers(STATE.handlers)
                _state.reset_state()
                raise
            _log.error("hmcts_otel: telemetry export disabled (%s); logging to stdout only", type(exc).__name__)

        if settings.sentry_dsn is not None and settings.sentry_dsn.get_secret_value():
            try:
                from hmcts_otel.sentry import init_sentry

                init_sentry(settings, redactor=redactor)
            except ImportError:
                _log.warning("SENTRY_DSN is set but sentry-sdk is not installed; install hmcts-otel[sentry]")
            except Exception as exc:  # e.g. a malformed DSN
                if settings.strict:
                    remove_handlers(STATE.handlers)
                    _state.reset_state()
                    raise
                _log.error("hmcts_otel: Sentry disabled (%s)", type(exc).__name__)

        STATE.configured = True
        if not _atexit_registered:
            atexit.register(shutdown_telemetry)
            _atexit_registered = True
        _log.debug("hmcts_otel configured: service=%s azure=%s", settings.service_name, settings.exports_to_azure)
        return settings


ROLLBACK_BUDGET_MS = 2000


def _global_providers() -> list[Any]:
    """The globally registered SDK providers (proxies excluded)."""
    from opentelemetry._logs import get_logger_provider

    found: list[Any] = []
    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider(), get_logger_provider()):
        if callable(getattr(provider, "shutdown", None)):
            found.append(provider)
    return found


def _unique(providers: list[Any]) -> list[Any]:
    out: list[Any] = []
    for provider in providers:
        if provider is not None and all(provider is not seen for seen in out):
            out.append(provider)
    return out


def _stop_providers(providers: list[Any], timeout_millis: int) -> bool:
    """Flush and shut down within a time budget. Returns False if the budget ran out."""
    if not providers:
        return True
    worker = threading.Thread(
        target=_flush_and_stop, args=(providers, timeout_millis), name="hmcts-otel-shutdown", daemon=True
    )
    worker.start()
    worker.join(timeout_millis / 1000)
    return not worker.is_alive()


def _flush_and_stop(providers: list[Any], timeout_millis: int) -> None:
    for provider in providers:
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            try:
                flush(timeout_millis)
            except Exception:  # pragma: no cover - best effort
                pass
        stop = getattr(provider, "shutdown", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # pragma: no cover - best effort
                pass


def shutdown_telemetry(timeout_millis: int = 5000) -> None:
    """Flush and shut down everything set up by ``setup_telemetry``. Idempotent.

    Call on SIGTERM / application shutdown so the last batch is not lost.
    ``timeout_millis`` is an overall budget: provider shutdown can block on an
    unreachable ingestion endpoint (exporter retries, offline storage), so it runs
    in a daemon thread and is abandoned when the budget is spent. This keeps pod
    shutdown inside the Kubernetes termination grace period.
    """
    with LOCK:
        if not STATE.configured:
            return
        deadline = time.monotonic() + timeout_millis / 1000
        providers = [p for p in (STATE.tracer_provider, STATE.meter_provider, STATE.logger_provider) if p]
        if not _stop_providers(_unique(providers), timeout_millis):
            _log.warning("hmcts_otel shutdown exceeded %d ms; remaining telemetry may be lost", timeout_millis)
        try:
            import sentry_sdk

            remaining = deadline - time.monotonic()
            if sentry_sdk.is_initialized() and remaining > 0:
                sentry_sdk.flush(timeout=remaining)
        except ImportError:
            pass
        remove_handlers(STATE.handlers)
        for handler in STATE.filtered_handlers:
            logging.getLogger().removeHandler(handler)
        _state.reset_state()


def setup_logging(settings: TelemetrySettings | None = None, *, redactor: Redactor | None = None) -> None:
    """Logging only: redaction for every root handler, JSON stdout if there is none.

    For processes that import application modules without the application's entry
    point (scripts, one-off jobs). No exporters, no Sentry, no tracer-provider check,
    so it is safe as an import-time side effect. Does nothing once ``setup_telemetry``
    has run; a later ``setup_telemetry`` replaces what it installed.
    """
    with LOCK:
        if STATE.configured:
            return
        settings = settings or load_settings()[0]
        redactor = redactor or default_redactor
        root = logging.getLogger()
        filters = make_filters(redactor, settings)
        if logging.lastResort is not None:
            add_filters(logging.lastResort, filters)
        foreign = [h for h in root.handlers if not isinstance(h, StdoutHandler)]
        if foreign:
            # Someone else owns output (e.g. alembic's fileConfig): only redact it.
            for handler in foreign:
                add_filters(handler, filters)
            return
        if not any(isinstance(h, StdoutHandler) for h in root.handlers):
            configure_logging(settings, redactor)


def is_configured() -> bool:
    return STATE.configured
