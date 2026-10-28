# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.3] - unreleased

### Fixed
- **The export path's own log records are no longer exported.** The Azure Monitor
  exporter logs every upload at INFO, and so does the HTTP logging policy of the
  pipeline it uploads with. Exported, each record caused another upload, which logged
  again: one application log became over 4,000 exported records in a few seconds,
  until something lowered those loggers' levels (found by transcribe-api, its #25).
  Records from `azure.monitor.opentelemetry`, `azure.core.pipeline.policies.http_logging_policy`
  and `opentelemetry` (whose own warnings would feed the same loop during an outage)
  are now kept off the exporting handler, in both setup paths. They still reach stdout,
  and no logger's level changes. A service no longer needs to quieten the Azure SDK
  first; doing so is harmless.

## [0.2.2] - 2026-10-28

First published release. 0.1.0, 0.2.0 and 0.2.1 were never tagged, so their changes
ship in this version. That includes the 0.2.1 relay defaults: without `journey_types`
a browser journey is denied, and browser journey IDs must be UUIDs.

Privacy fixes, and scrubbing of log records emitted through the OpenTelemetry Logs API.
No API changes. **Behaviour notes:** some lines are now redacted further than before
(see "Changed"), and dropped-attribute warnings for browser events show unregistered
keys as a hash.

### Fixed (PR #5 review)
- **An attribute key that matches a redaction rule is dropped.** Span attributes and
  OpenTelemetry log records used to keep the key and redact only the value, so a key
  such as an email address was exported. `RedactingFilter` already deletes those keys.

### Fixed (PR #4 review, round 4)
- **A JWT straddling the truncation boundary is fully redacted.** Redaction runs on a
  prefix longer than the text cap; the margin now covers the longest default match (a
  JWT of up to three 8,192-character segments), derived from the rule's bounds.
- **Metric dimensions follow the active redactor.** Stage names, error codes, journey
  types and event names recorded on `hmcts.stage.duration`, `hmcts.events` and
  `hmcts.event.duration` are checked against the redactor configured by
  `setup_telemetry(redactor=...)`; a matching value is recorded as `redacted` and counted
  as `invalid.metric_dimension`. Schemas created without an explicit redactor now follow
  the configured one too, so custom rules also reject matching attribute values and
  identifiers (strict mode raises).
- **Oversized tokens fail closed.** Bearer tokens of any length, and JWTs of any
  length (including a segment longer than 8,192 characters, a token with its signature
  cut off, or a token truncated before its first dot, from 16 characters) are redacted
  whole. Previously a bearer token over 4,096 characters kept its tail and an oversized
  JWT was not redacted at all. Possessive quantifiers keep both rules linear-time.
- **Long URLs keep no query string.** The absolute-URL rule had a 2,048-character bound
  on the part before `?`, and the relative-path rule likewise, so a longer URL exported its
  query unchanged. Both now match any length in linear time (the absolute-URL part never
  crosses another `http(s)://`, so repeated scheme prefixes stay cheap).
- **URL userinfo fails closed.** The whole userinfo is redacted at any length and in every
  form: `user:password@`, `user@` (a token as username) and `user:@`. Each component was
  bounded at 256 characters, and username-only or empty-password URLs were not matched.
  Credentials whose `@` lies past the inspected prefix of a very long text are redacted up
  to the cut, and `matches()` reports them.
- **Secret-named structured values are redacted.** In Sentry events (extra, contexts,
  breadcrumbs, transactions), log `extra=` fields and exported span and log attributes, a
  value under a secret-named key (`AccountKey`, `password`, `x-api-key`, `client_secret`,
  `Authorization`, `connection_string`, ...; `is_secret_key()`) is replaced by
  `[REDACTED]`. A key and its value are checked apart there, so they never formed the
  `key=value` pair the rules look for.
- **Static service metadata is redacted in JSON logs.** The service name, environment and
  version are added after the redaction filter has run, so the formatter now redacts them.
- **The prohibited-content scan searches each exported string whole**, so a match longer
  than the old window overlap (a long URL with a query) is found.
- **URL credentials are removed before the email rule runs**, so
  `https://user:password@host.domain/path` no longer keeps the username.

### Fixed (PR #4 review, round 3)
- **The relay rate limiter has a hard capacity.** At most `max_keys` (10,000) identities
  are tracked. Expired buckets are pruned only when a new identity arrives at capacity,
  and at most once every 5 seconds, so the work per request stays bounded. While still at
  capacity a new identity is refused with 429 and counted as `invalid.relay_capacity`;
  identities already tracked keep their state. Previously every request above 10,000
  identities rebuilt the whole table under the lock.
- An invalid nested `journey()` (non-strict mode) now runs without a journey, as
  documented, instead of leaving the outer journey active.
- Sentry events: dictionary keys are redacted too, and redaction runs after the context
  tags are added, so the tags are covered.
- `ContextFilter` no longer adds context attributes to custom-event records, because
  `record_event` already sets the journey and request ID itself.

### Security
- **Log records emitted through the OpenTelemetry Logs API are scrubbed.** They skipped
  the logging filters, so a GenAI instrumentation event (openai-v2 events, litellm's
  "otel" callback, with content capture on) such as `gen_ai.user.message` with
  `body={"content": ...}` was exported verbatim. `setup_telemetry` now installs a
  scrubbing log record processor ahead of the exporter: bodies and attribute values are
  redacted (nested maps and lists too; bytes become a placeholder), GenAI content
  attributes are dropped, and exception text the package did not format is reduced to
  `Type: [REDACTED]`. A record that cannot be scrubbed is exported without content and
  counted on `hmcts.telemetry.errors` (`internal.log_record_scrub`).
- **GenAI events keep no content.** Span events and log records named `gen_ai.*` keep only
  `gen_ai.system`, `gen_ai.provider.name`, `gen_ai.operation.name`, the request and
  response models, `index`, `finish_reason`, `gen_ai.response.finish_reasons` and token
  usage counts. The body and every other attribute (`content`, `message.content`,
  `tool_calls`, ...) are dropped. Before, only `gen_ai.`-prefixed content keys were.
- **Spaces after a secret's separator no longer shorten the redaction.**
  `Password= two words;` was exported as `Password= [REDACTED] words;`, and
  `password: two words` as `password: [REDACTED] words`. The spaces are kept and the
  value is redacted whole.
- **Over-long secret values fail closed.** A quoted value over 4,096 characters, or a
  braced or connection-string value over 256, was cut at the bound and the rest
  exported. It is now redacted to its closing quote or brace on the same line, otherwise
  to the end of the line (a connection-string value to the next `;`).
- **Browser-supplied keys are hashed in dropped-attribute warnings.** An unregistered key
  sent through the relay (e.g. `fictional_jane_smith`) appeared verbatim in the exported
  warning. For browser events, only registered schema and metadata keys are shown;
  others appear as `sha256:<12 hex>`. Server-side warnings are unchanged.

### Changed
- **Unquoted secret values run to the next delimiter.** `password: <value>` and
  `password = <value>` are redacted up to `;`, `&`, `,`, `}`, a quote, the end of the
  line, or the next `key=value` / `key: value` pair, not just the first word. So
  `password=x user=u1` still keeps `user=u1`, but `password: hunter2 at 10:30` becomes
  `password: [REDACTED] 10:30`, and a long value after `=` is redacted to the next `;`
  or the end of the line. Over-long quoted or braced values without a closing delimiter
  take the rest of the line with them. This over-redaction is deliberate.
- Standard-library log records carry the `hmcts.exception.formatted` marker between the
  logging filter and the exporter (it is removed before export and never printed by
  the JSON stdout formatter). A custom handler that prints every record attribute may
  show it.

### Testing
- `assert_no_prohibited_content` checks every exported string separately and in full
  (long strings in overlapping windows). Before, the rules saw only the first 41,472
  characters of all exported text joined together, so a leak in a later record of a
  large capture passed. Failures name the rule and where it matched
  (e.g. `rule 'email' in log record #60 body`), never the content. New
  `TelemetryCapture.exported_strings()` yields each string with its location.
  Needles are still searched for in all exported text.

## [0.2.1] - not published

Security and privacy fixes, and stricter relay defaults. **Upgrading:** a service that
mounts the relay must pass `journey_types` for browser journeys, and its browser
journey IDs must be UUIDs. A browser event that sends `hmcts.outcome`,
`hmcts.error.code` or `hmcts.request.id` must list it in its registration (`attributes=`).
A service that removed `journey.parent_id` itself before the relay no longer needs to.

### Security
- Event metadata can no longer be overwritten by caller attributes. Journey type, ID and
  parent, event source and class, duration and the custom-event name are set by
  hmcts-otel only; a caller (or a browser via the relay) passing one has it dropped and
  counted on `hmcts.telemetry.dropped_attributes` (reason `reserved`). Before, a browser
  could relabel its event as a server event in another journey, bypassing `journey_types`.
  The request ID is reserved too, unless the event registers `hmcts.request.id`
  explicitly. Browser events accept only the attributes their registration lists: core
  keys such as `hmcts.outcome` and `hmcts.error.code` are accepted from a browser only
  where the event lists them. Server-side `record_event` still accepts outcome and error
  code on any event.
- When the logging filter fails closed, it now also removes every extra attribute
  (`extra=` and context), not only the message, arguments and exception. Before, a
  failing redactor let unredacted extras reach stdout and the OpenTelemetry log.
- Quoted secret values are redacted to the closing quote (double or single, backslash
  escapes allowed, at most 4,096 characters; an unclosed quote to the end of the line):
  `password="two words"` was exported as `password="[REDACTED] words"`. Connection-string
  values with spaces (`Password=two words;`, up to `;` or the end of the line) and ODBC
  braced values (`Pwd={two words}`) are redacted in full.

### Changed (relay behaviour)
- **The relay denies journeys by default.** Without `journey_types`, an event carrying a
  journey is now rejected (counted in the response's `rejected`), as an event whose
  journey type is not listed already was. Before, any journey type was accepted. Pass
  `journey_types={...}` with the journeys the browser owns.
- **Browser journey IDs must be a UUID or 32 hex characters.** Before, any
  `[A-Za-z0-9_-]{1,64}` value was accepted, including names and phone numbers. An event
  with any other `id` (or kept `parent_id`) is rejected. Server-side `Journey` validation
  is unchanged.
- **The relay drops a browser-sent `journey.parent_id` by default.** New option
  `create_telemetry_relay_router(allow_parent_id=True)` keeps it (validated as above).

### Build
- Draft versions take their base from `pyproject.toml`
  (`<version>.dev<run_number>+<sha>`), not from the latest `v*` tag, which gave
  `0.0.0.dev<n>+<sha>` in a repository without tags (never satisfying `>=0.2.0`). A
  release publishes the `pyproject.toml` version and fails if the release tag differs.
  The computation is in `scripts/artefact_version.sh`, with tests.

## [0.2.0] - not published

### Added (OBS-5: AI usage metering)
- `hmcts_otel.ai_usage`: `record_llm_usage`, `record_speech_usage`, `ai_feature`,
  `ai_fallback`, `current_ai_feature` and `instrument_litellm`, exported from `hmcts_otel`.
- Metrics `gen_ai.client.token.usage` (histogram, `{token}`, one point per token type per
  call, OpenTelemetry GenAI semantic conventions v1.37.0), `hmcts.ai.requests` (counter,
  with `error.type` on failures) and `hmcts.speech.audio.duration` (counter, seconds).
  Usage only: no prices or cost; billing is allocated by usage share at query time.
- Core attributes `hmcts.ai.feature`, `hmcts.ai.fallback` and `hmcts.speech.mode`;
  `conventions.SpeechMode`, `conventions.TokenType`, `conventions.GEN_AI_SEMCONV_VERSION`.
- `current_stage()`: the innermost `stage()` name, used as the default AI feature.
- `instrument_litellm()`: a litellm `CustomLogger` (sync and async success and failure
  hooks), registered once, appended to existing callbacks. Reads usage, model, provider,
  call type and fallback flags only. litellm stays optional and is imported lazily.
- `TelemetryCapture.metric_points` also returns `min`, `max`, `bucket_counts` and
  `explicit_bounds` for histograms.

### Privacy
- `setup_telemetry` defaults `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` and
  `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED` to `false`, and warns if either is on.
- GenAI prompt and response attributes (`gen_ai.prompt.*`, `gen_ai.completion.*`,
  `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, tool
  call arguments and results, `llm.prompts`, `input.value`, ...) are dropped from exported
  spans and span events, whichever instrumentation set them.

## [0.1.0] - not published

### Relay
- `create_telemetry_relay_router(journey_types=...)`: browser events may attach only
  to the listed journey types, so a client cannot write into a server-side journey.

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
