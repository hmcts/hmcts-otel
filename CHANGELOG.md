# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - unreleased

### Outcome classification
- `StageHandle.error(code)` for server-side and dependency failures (outcome
  `error`, code kept if an exception follows).
- A FastAPI/Starlette `HTTPException` with a 4xx status leaving a stage is a handled
  failure (`http.<status>`), wherever it was raised. `client_failure_code(exc)` exposes
  the rule for job-level outcomes.

### Security and robustness (principal review)
- Telemetry never replaces or masks the application's exception; internal failures
  are counted on `hmcts.telemetry.errors`. Misuse raises only in strict mode
  (`HMCTS_OTEL_STRICT`, on in `capture_telemetry`). (H1)
- Attribute names that collide with `LogRecord` fields are rejected at registration. (H2)
- Exported stack traces are rebuilt from their structure, removing chained and
  multi-line exception messages; span status descriptions reduced to the type. (H3)
- `setup_telemetry` refuses to start if another tracer provider is registered
  (`HMCTS_OTEL_ALLOW_FOREIGN_TRACER_PROVIDER` to override). (H4)
- Duration histograms use second-based buckets; percentiles documented from events. (H5)
- Browser relay accepts `application/json` only. (M1)
- Query strings of relative URLs redacted; warning when uvicorn access logging is on. (M2)
- Non-primitive log `extra` values are omitted. (M3)
- Handled failures no longer set span error status. (M4)
- Bounded memory for dropped-attribute warnings. (M5)
- Ingestion-sampling requirement documented. (M6)

### Security and robustness (second review)
- `journey()` / `request_id_context()` no longer raise when a block is resumed in a
  different context (e.g. a sync generator behind `StreamingResponse`). (H1)
- uvicorn's loggers are re-captured whenever uvicorn applies its logging config
  (patched `Config.configure_logging`) and by the `instrument_fastapi` guard
  middleware, so uvicorn tracebacks no longer bypass redaction. (H2)
- Sentry: exception messages replaced unless opted in; transactions scrubbed via
  `before_send_transaction`; query-string keys removed at any depth. (H3)

### Found during adoption (OBS-2)
- The distro overwrites `disable_tracing` from `OTEL_TRACES_EXPORTER`, so it built a
  second tracer provider with an unscrubbed exporter (orphaned, but running).
  `OTEL_TRACES_EXPORTER=none` is now set for the duration of the distro call; a
  subprocess test with the real distro asserts exactly one trace pipeline.
- `OTEL_SDK_DISABLED=true` is honoured (stdout only).
- Email rule covers the RFC 5322 local-part character set.

### Completed in the third review
- `TelemetrySettings` itself falls back to defaults for invalid values (environment or
  keyword) and reports them in `invalid_fields`; strict mode raises. (M5, N2)
- Exceptions recorded on spans by instrumentations are formatted from the exception
  object; exception text not built by the package is exported as the type only;
  `extra=` exception text is not trusted. (L1)
- Re-setup replaces filters left on foreign handlers by an earlier setup. (N1)
- A failed distro setup stops every provider it created. (N3)

### Robustness and privacy (second review, medium and low)
- Linear-time redaction rules; text capped at 32,768 characters after redaction. (M1)
- Logging filter never raises; exception chains formatted iteratively, capped at 25. (M2)
- `record_event` with a non-string name no longer raises outside strict mode. (M3)
- Request-ID middleware handles response starts without `headers`. (M4)
- Invalid configuration falls back to defaults; exporter/Sentry failures degrade to
  stdout logging; strict mode raises and rolls back. (M5)
- Pre-existing root handlers and `logging.lastResort` are filtered. (M6)
- Worker example and README stop promptly on SIGTERM. (M7)
- Status descriptions keep only a type or controlled code. (M8)
- Stack-trace sanitiser is a strict state machine; runtime source lines dropped;
  `<locals>` types kept. (L1)
- Link attributes scrubbed; concrete paths replaced by route templates; request IDs
  must be UUID/hex. (L2)
- Free-text dropped keys logged as a hash. (L3)
- Quoted / JSON secrets redacted; `bearer` needs a 16+ character token. (L4)
- OTel logging handler from opentelemetry-instrumentation-logging. (L5)
- `exc_info=True` without an exception no longer emits `exception.type`. (L6)
- Shutdown budget includes the Sentry flush. (L7)
- Relay body limit enforced while streaming. (L8)
- `stage()` in sync generators documented. (L9)

### Added
- `setup_telemetry` / `shutdown_telemetry` with an Azure Monitor path (own trace
  pipeline with export-time scrubbing; distro for metrics, logs and
  auto-instrumentation) and a custom-exporter path. Shutdown has a time budget.
- JSON stdout logging with trace, span, journey and request IDs; redaction and
  exception-message removal on every handler.
- `journey`, `stage`, `record_event`, `capture_trace_context` / `links_from`.
- `TelemetrySchema` attribute and event registry, with validation, drop counting
  and markdown generation.
- `Redactor` with default rules (email, JWT, bearer, key/secret pairs, URL
  credentials, URL query strings).
- `hmcts_otel.fastapi`: `instrument_fastapi`, `RequestIdMiddleware`,
  `create_telemetry_relay_router`.
- `hmcts_otel.sentry`: hardened `init_sentry`.
- `hmcts_otel.testing`: `capture_telemetry` and `assert_no_prohibited_content`.
