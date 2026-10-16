# Design notes

Decisions behind hmcts-otel, the evidence for them, and known limitations.

## 1. Own the trace pipeline; let the distro do the rest

**Decision.** `setup_telemetry` builds the `TracerProvider` itself (Application Insights
sampler, journey span processor, `ScrubbingSpanExporter` wrapping
`AzureMonitorTraceExporter`). It then calls `configure_azure_monitor(disable_tracing=True, ...)`,
so the distro sets up metrics, logs and auto-instrumentation, and the instrumentations
use our tracer provider.

**Why.** Privacy controls must cover spans from auto-instrumentation (FastAPI, httpx,
requests, psycopg2, Azure SDK) as well as our own, and the only reliable place to
change a span is before export:

- In opentelemetry-sdk 1.44, `Span.end()` sets `_attributes._immutable = True` before
  any processor hook runs. The only earlier hook, `_on_ending`, is private and differs
  between 1.40 and 1.44.
- `ReadableSpan(...)` and `Event(...)` have identical public constructors in 1.40 and
  1.44, so an exporter wrapper can rebuild each span without private API.
- The distro registers its exporter inside `_setup_tracing` with no hook for wrapping it.

**Consequence.** The distro's Live Metrics span processor is not attached, so Live
Metrics shows no request data. Live Metrics is off by default.

## 2. Our own sampler setting

The distro reads `OTEL_TRACES_SAMPLER_ARG` only when `OTEL_TRACES_SAMPLER` is also
set. Otherwise it uses a rate-limited sampler at 5 traces per second
(`_utils/configurations.py`, `_default_sampling_ratio`). This was found in DIAAT-274:
deployments setting `OTEL_TRACES_SAMPLER_ARG=0.05` had no effect. hmcts-otel uses
`HMCTS_OTEL_SAMPLING_RATIO` (default 1.0) with the Application Insights sampler, and
deliberately does not read `OTEL_TRACES_SAMPLER_ARG`.

Lifecycle events are log records and metrics are pre-aggregated; neither is
trace-sampled. App Insights **ingestion** sampling (`sampling_percentage` on the
resource) still applies and should be 100 for services using this package.

## 3. Events as log records

Events are emitted on the `hmcts_otel.events` logger with the
`microsoft.custom_event.name` attribute, which the Azure Monitor exporter maps to
custom events. This is supported from azure-monitor-opentelemetry-exporter 1.0.0b51,
the version locked by azure-monitor-opentelemetry 1.8.7. The events logger is set to
INFO so events are recorded whatever the application log level.

## 4. Filters on handlers, not loggers

A logging filter on a logger sees only records logged directly to that logger, not
records propagated from child loggers. Redaction and context filters are therefore
attached to every handler the package adds, and to the handler the distro attaches
to the root logger.

## 5. Bounded shutdown

When ingestion is unreachable, provider shutdown blocks: the Azure exporter retries
and writes to offline storage. In testing against a refused endpoint, shutdown had
not completed after 40 seconds. `shutdown_telemetry` therefore runs flush and shutdown
in a daemon thread and stops waiting when its budget (default 5 seconds) is spent.
That keeps it inside the Kubernetes termination grace period. Providers are created
with `shutdown_on_exit=False` because the package owns shutdown.

## 6. FastAPI instrumentation per app

The distro instruments FastAPI by replacing the `fastapi.FastAPI` class, which only
affects apps created afterwards. Any module that imported `FastAPI` earlier gets an
uninstrumented app. This was found in DIAAT-274 for the recording service.
`instrument_fastapi(app)` instruments a specific app instance, and the distro's
FastAPI instrumentation is disabled to avoid doing it twice.

Recent versions of `opentelemetry-instrumentation-fastapi` wrap the whole middleware
stack, so the server span starts before any user middleware. The request-ID
middleware therefore tags the active span directly as well as setting the context.

## 7. Telemetry never changes application behaviour

`stage()` and `record_event()` isolate every internal step. A telemetry failure
(metric, logging, exporter) is counted on `hmcts.telemetry.errors` and never replaces
or masks the application's exception. Misuse (invalid names, codes, journey IDs)
raises only in strict mode, which `capture_telemetry` enables so mistakes fail CI.
Production degrades: the work runs, the misuse is counted.

Before this rule, a broken metrics backend turned a caller's `BusinessError` into a
`RuntimeError`, and a typo in an error code raised `ValueError` into a request
handler. Both were found in review and have regression tests.

## 8. Stack traces are rebuilt, not pattern-matched

For spans we did not create (instrumentations call `record_exception` and set the
status to `"<Type>: <message>"`), the exporter only has formatted text. Replacing
just the final message missed chained exceptions and multi-line messages. Pydantic
`ValidationError` echoes `input_value=...`, which leaked request content in review.
`sanitize_stacktrace` now rebuilds the traceback from its structure: header, frames
with source and caret lines, chain separators, and `Type: [REDACTED]`. Every other
line is dropped. Status descriptions are reduced to the exception type.

## 9. One tracer provider

OpenTelemetry's global tracer provider can be set once per process. If anything
registered one before `setup_telemetry`, ours would be ignored and instrumented spans
would be exported unscrubbed, with only a warning. `setup_telemetry` therefore
raises in that case. `HMCTS_OTEL_ALLOW_FOREIGN_TRACER_PROVIDER=true` overrides this
with an error log, for the rare case where that is intended.

## 10. Percentiles come from events

The Azure Monitor exporter sends histogram points as count, sum, min and max per
export interval (`_exporter.py`, `HistogramDataPoint` branch). Percentiles of
durations therefore cannot be computed from `customMetrics`. Each stage emits one
completed or failed event carrying `hmcts.duration_ms`, which gives exact percentiles
in KQL. Duration histograms use second-based bucket boundaries
(`conventions.DURATION_BUCKETS_S`) because the SDK defaults assume milliseconds.
This matters for backends that keep buckets.

## 11. Context variables restore safely

`journey()` and `request_id_context()` restore their context variables with a
fallback. `ContextVar.reset` raises if a block is resumed in a different context,
e.g. a sync generator behind a Starlette `StreamingResponse`, where each `next()`
runs in a copied context. In that case the previous value is set instead. Found in
the second review: the request failed with `ValueError`.

## 12. uvicorn reinstalls its handlers

`uvicorn.run(app)`, `fastapi run` and `uvicorn.Config(...)` apply uvicorn's
`LOGGING_CONFIG` after the application module has been imported, so after
`setup_telemetry`. That puts back uvicorn's own `StreamHandler`s with
`propagate=False`. Tracebacks logged there carried full exception messages to stderr.
The package wraps `uvicorn.config.Config.configure_logging` (the single place this
happens since uvicorn 0.20; verified in 0.46 and 0.49) to re-capture immediately
afterwards. The `instrument_fastapi` guard middleware re-checks on lifespan startup
and on requests as a second line.

## 13. Sentry follows the same message policy

Sentry originally kept exception messages for triage. That contradicted the package's
default (`capture_exception_messages=False`) and sent content to a third party.
Messages are now replaced unless opted in. `before_send_transaction` applies the same
scrubbing to transactions, which `before_send` never sees. Query-string keys are
removed at any depth.

## 14. Third review round (medium and low findings)

- **Linear-time redaction.** Every quantifier that can be retried from many start
  positions is bounded, and text is cut at 32,768 characters after redacting a slightly
  longer prefix, so a secret straddling the cut is still matched. Before this change,
  logging a 100k-character token blocked the caller for over 5 seconds, and
  `path_query` was also quadratic.
- **The logging filter fails closed.** Handler filters run outside logging's own
  error handling, so the filter catches everything and replaces the record's content
  with a placeholder. Exception chains are formatted iteratively and capped at 25.
- **Setup degrades instead of crashing.** Invalid settings fall back to defaults
  (`load_settings`). An exporter or Sentry failure rolls back exporter handlers and
  leaves stdout logging. A tracer provider registered by an earlier attempt in the same
  process is recognised as ours on retry. Strict mode raises and rolls back fully.
- **Redaction outside the configured window.** Handlers present before setup and
  `logging.lastResort` get the filters, so `basicConfig` output and post-shutdown
  tracebacks are redacted.
- **Stack-trace state machine.** A `Traceback` header is accepted only at the start or
  after a chain separator, and frames only inside a traceback. Source lines of
  runtime-supplied code (`File "<unknown>"`) are dropped.
- **Status descriptions** keep only `Type: [REDACTED]` or a controlled code.
- **Route templates** replace concrete paths, and link attributes are scrubbed.
- **Request IDs** must be UUID or hex.
- **Dropped keys** that are not attribute-like are logged as a hash.
- **The OTel logging handler** comes from opentelemetry-instrumentation-logging (the
  SDK's is deprecated from 1.40).
- **The shutdown budget** covers the Sentry flush.
- **The relay** limits body size while streaming.

## 15. Exception text: formatted here or not exported

Two cases defeated text sanitising in the third review: a message that imitates a chain
separator, `Traceback` header and frames, and a `SyntaxError` from
`ast.parse(user_text, filename="template.py")`, whose "source line" is runtime input.
Once formatted, such text cannot be told apart from real structure. So:

1. While configured, `Span.record_exception` (same signature in SDK 1.40–1.45) records
   exceptions on SDK spans using `_exceptions.format_exception`. That formats frames from
   code objects and replaces every message, so no text is ever parsed. `stage()` and
   logging already did this.
2. Events built that way carry `hmcts.exception.formatted`. The exporter keeps their
   stack trace and removes the marker. Exception text without the marker is reduced to
   `Type: [REDACTED]`. The marker is an attribute key, which message text cannot create.
3. On log records, provenance is tracked by object identity (a `WeakSet`), because
   `extra=` can set any attribute name. Exception text supplied through `extra=` is
   replaced.

`sanitize_stacktrace` remains as a best-effort tool, and syntax-error location frames
(`File "...", line N` with no `, in`) no longer keep their source line, but nothing
relies on it for untrusted text.

## 16. Settings never fail

`TelemetrySettings` has a wrap validator. If validation fails, the offending fields are
dropped and validated again, so they take their defaults, and their names are kept in
`invalid_fields`. This holds whether the bad value came from the environment or a keyword
argument. Strict mode re-raises.

## 17. Re-setup and failed setup

- Filters on handlers the package does not own (pre-existing root handlers,
  `logging.lastResort`, the distro's handler) outlive shutdown on purpose. Each setup
  *replaces* them, so a stale `RedactingFilter` with older settings cannot run first.
- If the distro fails after our tracer provider was registered, setup stops every
  provider created by that attempt (ours, and any the distro registered) within a budget,
  so nothing is exported in the background. The provider stays the global one, because
  OpenTelemetry cannot unregister it, but its processors are shut down. The Azure
  exporter's own internal usage telemetry ("statsbeat") threads keep running, as they do
  in every process using the exporter. They carry SDK metadata, not application
  telemetry. `APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL=true` turns them off.

## 18. The distro ignores `disable_tracing`

`azure-monitor-opentelemetry` 1.8.7–1.8.11 sets `disable_tracing` from
`OTEL_TRACES_EXPORTER` unconditionally (`_utils/configurations.py`,
`_default_disable_tracing`), overwriting the keyword argument. The distro therefore
built its own tracer provider and exporter. Its `set_tracer_provider` call was refused
because ours was already global, so spans were still scrubbed, but the second exporter
and its threads ran idle. That was found while adopting the package in the dictation
API. `OTEL_TRACES_EXPORTER=none` is set only for the duration of the call, and restored.

## 19. Drop, never truncate

A truncated string still carries part of its content. Over-long or non-conforming
values are dropped and counted.

## Known limitations

- **uvicorn access logs** use uvicorn's own handler, which this package cannot filter.
  Services must run with `--no-access-log`; the package warns on the first request if
  access logging is on.

- **Free text in log messages.** Redaction is pattern-based. A log line containing a
  transcript fragment with no email or token in it will not be caught. Mitigations:
  do not log content; use `capture_telemetry().assert_no_prohibited_content(...)` in
  tests.
- **Source lines in stack traces.** Exported stack traces include the source line of
  each frame (standard Python formatting). Literal strings in source code appear;
  runtime values do not.
- **Rate limiting in the relay is per process.** With N replicas the effective limit is N × the configured limit.
- **Exception text from outside the package** is exported as the type only (no frames),
  because it cannot be verified. Spans from the package's tracer provider always get
  full, safe stack traces.
- **`stage()` in sync generators** leaves the span current between `yield`s (see README).
- **Re-setup after shutdown on the Azure path** is not supported: OpenTelemetry global
  providers can be set once per process.
