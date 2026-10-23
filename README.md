# hmcts-otel

Privacy-safe OpenTelemetry for HMCTS Python services on Azure.

One call sets up structured logging, tracing and metrics that export to Application
Insights, with redaction built in. A small API (`journey`, `stage`, `record_event`)
gives every service the same lifecycle events, durations and correlation IDs, so
dashboards and alerts can be written once.

It is a thin layer over the
[Azure Monitor OpenTelemetry distro](https://learn.microsoft.com/azure/azure-monitor/app/opentelemetry-enable?tabs=python).
The distro does the exporting and auto-instrumentation; this package adds HMCTS
conventions and the privacy controls a judicial service needs.

- [What you get](#what-you-get)
- [Install](#install)
- [Quick start: FastAPI](#quick-start-fastapi)
- [Quick start: worker or script](#quick-start-worker-or-script)
- [Journeys, stages and events](#journeys-stages-and-events)
- [Asynchronous jobs](#asynchronous-jobs)
- [AI usage metering](#ai-usage-metering)
- [Browser telemetry relay](#browser-telemetry-relay)
- [Sentry](#sentry)
- [Privacy model](#privacy-model)
- [Testing your service](#testing-your-service)
- [Configuration](#configuration)
- [Querying in App Insights](#querying-in-app-insights)
- [Compatibility and versioning](#compatibility-and-versioning)

## What you get

| Signal | What it is | Where it lands |
|---|---|---|
| Logs | JSON to stdout, with trace ID, span ID, journey and request ID on every line | stdout (container logs) and App Insights `traces` |
| Traces | Request, dependency (HTTP, Postgres, Azure SDK) and stage spans | App Insights `requests` / `dependencies` |
| Lifecycle events | `<stage>.started`, `.completed`, `.failed`, `.cancelled`, plus your own events | App Insights `customEvents`. Not trace-sampled; App Insights ingestion sampling must be 100% |
| Metrics | `hmcts.stage.duration`, `hmcts.events`, `hmcts.event.duration`, `hmcts.telemetry.dropped_attributes`, `hmcts.telemetry.errors` | App Insights `customMetrics` (count, sum, min, max per interval) |
| AI usage | `gen_ai.client.token.usage`, `hmcts.ai.requests`, `hmcts.speech.audio.duration`: tokens and audio seconds by model and feature, never content | App Insights `customMetrics`. Never sampled |
| Errors (optional) | Hardened Sentry setup for triage | Sentry |

## Install

The package is published to the HMCTS `hmcts-lib` Azure Artifacts feed.

```toml
# pyproject.toml (uv)
[project]
dependencies = ["hmcts-otel[fastapi,sentry]>=0.2,<0.3"]

[[tool.uv.index]]
name = "hmcts-lib"
url = "https://pkgs.dev.azure.com/hmcts/Artifacts/_packaging/hmcts-lib/pypi/simple/"
```

Extras:

| Extra | Adds |
|---|---|
| `fastapi` | `instrument_fastapi`, request-ID middleware, browser telemetry relay |
| `sqlalchemy` | SQLAlchemy instrumentation package |
| `sentry` | `sentry-sdk`, initialised automatically when `SENTRY_DSN` is set |
| `entra` | `azure-identity`, for Entra-authenticated ingestion |

There is no extra for litellm: `instrument_litellm()` uses the litellm your service
already depends on and imports it only when called.

Do not vendor the wheel. Resolve it from the feed and let Dependabot or Renovate raise upgrades.

The feed is private, so the installer must authenticate. Locally, the Azure Artifacts
credential provider is the simplest option (`pip install keyring artifacts-keyring`, or
`uv tool install keyring --with artifacts-keyring` and `UV_KEYRING_PROVIDER=subprocess`
for uv). In CI, use the `AZURE_DEVOPS_ARTIFACT_USERNAME` / `AZURE_DEVOPS_ARTIFACT_TOKEN`
credentials. Releases and versions are described in [docs/releasing.md](docs/releasing.md).

## Quick start: FastAPI

```python
from fastapi import FastAPI
from hmcts_otel import setup_telemetry, shutdown_telemetry
from hmcts_otel.fastapi import instrument_fastapi

setup_telemetry()  # reads configuration from the environment; call before creating clients

app = FastAPI(lifespan=...)  # call shutdown_telemetry() when the lifespan ends
instrument_fastapi(app)  # request spans and metrics, health probes excluded, X-Request-Id
```

Set at least:

```bash
OTEL_SERVICE_NAME=transcribe-api
APPLICATIONINSIGHTS_CONNECTION_STRING=...   # unset locally: logs go to stdout only
HMCTS_OTEL_ENVIRONMENT=stg
APP_VERSION=1.4.2
```

`instrument_fastapi` works whatever the import order. Incoming `X-Request-Id` values
are accepted only if they are UUIDs or hex; anything else is replaced by a generated
ID, so a client cannot put a name on every span, log and event. Do not also call
`FastAPIInstrumentor` or `configure_azure_monitor` yourself: `setup_telemetry` refuses
to start if another tracer provider is already registered, because its spans would
bypass scrubbing.

**uvicorn logging.** uvicorn installs its own log handlers, which would bypass
redaction. That includes its "Exception in ASGI application" tracebacks, which carry
full exception messages. With `HMCTS_OTEL_CAPTURE_UVICORN_LOGS=true` (the default),
the package routes uvicorn's loggers through its own handlers and turns access logs
off. It does this at setup, again whenever uvicorn applies its logging config
(`uvicorn.run(app)`, `fastapi run`, `uvicorn.Config`), and again on lifespan startup
and requests through `instrument_fastapi`. Still run uvicorn with **`--no-access-log`**
(or `access_log=False`) as a belt-and-braces measure.

## Quick start: worker or script

```python
import signal
import threading
from hmcts_otel import TelemetrySettings, setup_telemetry, shutdown_telemetry

setup_telemetry(TelemetrySettings(service_name="transcribe-api-worker"))
stopping = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stopping.set())  # Kubernetes sends SIGTERM

try:
    while not stopping.is_set():
        do_one_batch()
        stopping.wait(30)  # wakes immediately on SIGTERM
finally:
    shutdown_telemetry()
# The process then exits normally. A handler that only flushes telemetry would
# leave the worker running until Kubernetes sends SIGKILL.
```

For a script with no loop of its own, use
`signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))`: `SystemExit` runs `finally`
blocks and the package's exit hook, which shuts telemetry down.

See `examples/worker.py`.

Give each workload its own `service_name` (for example API and worker) so a
saturated worker is distinguishable from a slow API.

`shutdown_telemetry(timeout_millis=5000)` flushes everything within an overall time
budget. If ingestion is unreachable it gives up when the budget is spent rather
than blocking pod termination.

## Journeys, stages and events

A **journey** is one user or system flow, identified pseudonymously. A **stage** is
one step in it.

```python
from hmcts_otel import default_schema, journey, record_event, stage

# Register the attributes your service emits (once, at import time).
default_schema.attribute("audio.duration_s", float, description="Audio length in seconds")
default_schema.attribute("audio.format", allowed_values={"wav", "mp3", "m4a"})

with journey("recording.job", job.id, parent_id=hearing.id):
    with stage("upload", attributes={"audio.format": "wav"}) as s:
        ...
        if not valid:
            s.fail("validation.unsupported_format")  # controlled code, never a message
    with stage("speech_submission") as s:
        response = submit_to_speech(...)
        s.set_dependency_ref(response.speech_job_id)  # relate to the external system's ID

record_event("transcript.available", {"audio.duration_s": 812.4})
```

Each `stage` produces:

- a span named after the stage, with `hmcts.outcome` and `hmcts.error.code`;
- `upload.started` and `upload.completed` / `.failed` / `.cancelled` custom events with the duration;
- a measurement on the `hmcts.stage.duration` histogram (dimensions: stage, outcome, error code, journey type).

Unexpected exceptions are recorded by type and stack with **messages removed**, then
re-raised.

Telemetry never changes application behaviour:

- Your exceptions pass through `stage()` unchanged, even if telemetry itself fails.
- Misuse (an invalid stage name, error code, event name or journey ID) raises
  `ValueError` only in **strict mode** (`HMCTS_OTEL_STRICT=true`, on by default in
  `capture_telemetry`). In production it is counted on `hmcts.telemetry.errors` and
  the code carries on: an invalid journey runs without a journey, an invalid name
  becomes `invalid_stage`, an invalid code becomes `invalid_error_code`.
- One classification rule:
  - **client-caused** → handled failure: `s.fail(code)`, or any FastAPI/Starlette
    `HTTPException` with a 4xx status leaving the block (code `http.<status>` unless
    `fail` set one). The outcome is `failure` and the span is not marked as an error,
    so these do not count as failed requests or dependencies in App Insights;
  - **server-side or dependency** → `s.error(code)`, or any other exception
    (`unhandled.<Type>`): outcome `error`, span status ERROR.

  `client_failure_code(exc)` exposes the same rule, so job-level outcomes (e.g. a
  terminal `job.finished` event) agree with the stage that stopped the job.

Rules:

- Journey IDs and parent IDs are system-generated (UUIDs). Case references, file
  names and anything a person typed are rejected.
- Stage and event names are lower-case dotted snake case (`speech_submission`, `upload.selected`).
- Error codes are controlled values (`speech.timeout`, `validation.too_large`), not text.
- Attribute names cannot be Python log-record field names (`filename`, `name`, `module`, ...); use a
  dotted name such as `file.name`.

## Asynchronous jobs

Store the trace context on the job when it is created, and link to it when the
job is processed, even in another process hours later:

```python
from hmcts_otel import capture_trace_context, journey, stage

# In the request handler
job.traceparent = capture_trace_context()

# In the poller or worker
with journey("recording.job", job.id):
    with stage("transcription", link_from=job.traceparent):
        ...
```

All of a job's events carry `hmcts.journey.id`, so the full lifecycle is one query
whatever the trace sampling.

## AI usage metering

Token and audio usage are recorded as **metrics**, which are pre-aggregated and never
sampled. Only usage is recorded: **no prices and no cost**. Cost comes from the Azure
and GCP bills, allocated by each feature's share of usage at query time, so there is
no price table to keep up to date. **No prompt or response content is ever recorded.**

| Metric | Type | Attributes |
|---|---|---|
| `gen_ai.client.token.usage` | histogram, `{token}` | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.token.type` (`input` / `output`), `hmcts.ai.feature`, `hmcts.outcome`, `hmcts.ai.fallback` |
| `hmcts.ai.requests` | counter, `{request}` | the same without the token type, plus `error.type` (exception class name) on failures |
| `hmcts.speech.audio.duration` | counter, `s` | `hmcts.speech.mode` (`realtime`, `fast`, `batch`), `hmcts.ai.feature`, `hmcts.outcome` |

`gen_ai.*` names follow the OpenTelemetry GenAI semantic conventions v1.37.0
(`conventions.GEN_AI_SEMCONV_VERSION`). Each model call records one token point per
token type and one request. `hmcts.ai.requests` exists so that call counts, error
types and the fallback rate do not depend on token points.

```python
from hmcts_otel import ai_fallback, ai_feature, instrument_litellm, record_speech_usage

setup_telemetry()
instrument_litellm()  # once, after setup; keeps existing callbacks such as Langfuse

with ai_feature("summarise"):  # or rely on the enclosing stage() name
    response = await litellm.acompletion(model="azure/gpt-4o", messages=...)

with ai_feature("summarise"), ai_fallback():  # the secondary model, after the primary failed
    response = await litellm.acompletion(model="gemini/gemini-2.0-flash", messages=...)

record_speech_usage(seconds=audio_seconds, mode="batch", feature="transcribe")
```

Without litellm, call `record_llm_usage(provider=..., request_model=..., response_model=...,
input_tokens=..., output_tokens=..., outcome=..., fallback=...)` after each model call.

- **Feature**: the `feature=` argument, else the innermost `ai_feature(...)`, else the
  innermost `stage()` name, else `unknown`. Feature names follow the stage-name rule; an
  invalid name in `ai_feature` raises only in strict mode, like `stage()`.
- **Fallback**: `fallback=True`, or a call inside `ai_fallback()`, or (litellm)
  `metadata={"hmcts_ai_fallback": True}`, or a litellm **Router** fallback
  (`attempted_fallbacks` > 0 in the call's metadata). litellm's SDK-level
  `completion(fallbacks=[...])` does not tell callbacks that a call was a fallback, so
  mark those with `ai_fallback()`.
- **Failures** (litellm): outcome `error`, 0 tokens unless the provider reported usage,
  and the exception class on `hmcts.ai.requests`. Never the message.
- **Validation**: providers and models must match `[A-Za-z0-9._:/@-]{1,128}` and no
  redaction rule, else they are recorded as `unknown`. Negative or non-numeric token
  counts and seconds are not recorded. Both are counted on `hmcts.telemetry.errors`
  (`invalid.ai_usage.*`). These functions and the litellm callback never raise, even in
  strict mode. litellm provider IDs are mapped to semantic-convention values (`azure` to
  `azure.ai.openai`, `vertex_ai` to `gcp.vertex_ai`, `gemini` to `gcp.gemini`).
- **Content**: the litellm callback reads only the provider, model, call type, two
  metadata flags, the exception class and the response's `usage` and `model`, never
  `messages` or `choices`. `setup_telemetry` defaults
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` and
  `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED` to `false` (an explicit `true` is kept,
  with a warning), and prompt/response attributes written by any GenAI instrumentation
  (`gen_ai.prompt.*`, `gen_ai.completion.*`, `gen_ai.input.messages`, ...) are dropped from
  exported spans. Other litellm callbacks (e.g. Langfuse) are not changed and send what
  they are configured to send.

```kusto
// Tokens by model and feature (last 30 days)
customMetrics
| where name == "gen_ai.client.token.usage" and timestamp > ago(30d)
| extend provider = tostring(customDimensions["gen_ai.provider.name"]),
         model = coalesce(tostring(customDimensions["gen_ai.response.model"]), tostring(customDimensions["gen_ai.request.model"])),
         feature = tostring(customDimensions["hmcts.ai.feature"]),
         token_type = tostring(customDimensions["gen_ai.token.type"])
| summarize tokens = sum(valueSum), calls = sum(valueCount) by provider, model, feature, token_type
| order by tokens desc

// Share of each model's input and output tokens per feature, to split that model's bill
// (input and output tokens are billed at different rates, so allocate per token type)
let usage = customMetrics
| where name == "gen_ai.client.token.usage" and timestamp between (datetime(2026-09-01) .. datetime(2026-10-01))
| extend model = tostring(customDimensions["gen_ai.request.model"]), token_type = tostring(customDimensions["gen_ai.token.type"]),
         feature = tostring(customDimensions["hmcts.ai.feature"])
| summarize tokens = sum(valueSum) by model, token_type, feature;
usage
| join kind=inner (usage | summarize total = sum(tokens) by model, token_type) on model, token_type
| project model, token_type, feature, tokens, share_pct = round(100.0 * tokens / total, 2)

// Audio hours by mode and feature
customMetrics
| where name == "hmcts.speech.audio.duration" and timestamp > ago(30d)
| extend mode = tostring(customDimensions["hmcts.speech.mode"]), feature = tostring(customDimensions["hmcts.ai.feature"])
| summarize hours = sum(valueSum) / 3600.0 by mode, feature

// Fallback and error rate by feature
customMetrics
| where name == "hmcts.ai.requests" and timestamp > ago(7d)
| extend feature = tostring(customDimensions["hmcts.ai.feature"]),
         fallback = tostring(customDimensions["hmcts.ai.fallback"]) =~ "true",
         failed = tostring(customDimensions["hmcts.outcome"]) == "error"
| summarize calls = sum(valueSum), fallbacks = sumif(valueSum, fallback), errors = sumif(valueSum, failed) by feature
| extend fallback_pct = round(100.0 * fallbacks / calls, 1), error_pct = round(100.0 * errors / calls, 1)
```

## Browser telemetry relay

Browser events go through a same-origin endpoint into the backend, then to App
Insights. There is no third-party script, no CSP change, and the server enforces the
schema.

```python
from fastapi import Depends
from hmcts_otel import EventClass, default_schema
from hmcts_otel.fastapi import create_telemetry_relay_router

default_schema.attribute("speech.cancel_reason", allowed_values={"error", "end_of_stream"})
default_schema.attribute("speech.error_code", int)
default_schema.event(
    "speech.session_canceled",
    attributes=["speech.cancel_reason", "speech.error_code"],
    browser=True,
)
default_schema.event("feature.opened", event_class=EventClass.ANALYTICS, browser=True)

app.include_router(
    create_telemetry_relay_router(
        path="/api/telemetry",
        dependencies=[Depends(get_current_user)],  # your auth
        identity=lambda request: request.state.user_id,  # rate-limit key
        analytics_consent=lambda request: request.cookies.get("cookie_policy_analytics") == "yes",
    )
)
```

```http
POST /api/telemetry
{"events": [{"name": "speech.session_canceled",
             "attributes": {"speech.cancel_reason": "error", "speech.error_code": 4},
             "journey": {"type": "dictation.session", "id": "8b0e…"},
             "duration_ms": 1200}]}
```

- Only events registered with `browser=True` are accepted. Unknown attributes are dropped.
- **Operational** events are accepted regardless of analytics consent.
  **Analytics** events need `analytics_consent` to return true.
- `Content-Type: application/json` only (415 otherwise), so cross-site forms cannot post events with the user's cookies.
- Limits: 50 events per batch, 32 KB body (enforced while reading, including chunked uploads),
  120 requests per minute per identity per replica.
- Error responses never echo the submitted content.

## Sentry

With the `sentry` extra and `SENTRY_DSN` set, `setup_telemetry` initialises Sentry for
error triage:

- `send_default_pii=False`, `include_local_variables=False`, `max_request_body_size="never"`;
- exception messages replaced with `[REDACTED]` unless `HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES=true`,
  the same policy as the rest of the package. Type, stack frames and source lines are kept;
- tracing and profiling off (`HMCTS_OTEL_SENTRY_TRACES_SAMPLE_RATE=0`), because tracing lives in OpenTelemetry;
- every string in error events, **transactions** and breadcrumbs goes through the redactor;
  query-string fields (`http.query`, `query_string`, `url.query`, fragments) are removed
  wherever they appear; request data is reduced to method and path; `user` to `id`;
- events are tagged with the trace ID, journey and request ID, so an issue can be
  joined to App Insights.

## Privacy model

Layered, so one mistake does not leak content:

1. **Schema allowlist.** Custom attributes must be registered with a type and rule.
   Unregistered keys, wrong types, over-long strings and values matching a redaction
   rule are **dropped** (never truncated) and counted on `hmcts.telemetry.dropped_attributes`.
2. **Redaction** of every exported string: emails, JWTs, bearer tokens, key/secret
   pairs, URL credentials and query strings of absolute and relative URLs. Log
   `extra` values that are not primitives (dicts, objects) are replaced by
   `[dict omitted]`. Add your own rules:
   ```python
   from hmcts_otel import default_redactor

   default_redactor.add_rule("appeal_reference", r"\b[A-Z]{2}/\d{5}/\d{4}\b")
   ```
3. **Exception messages removed** from exported logs, spans and stage events
   (`HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES=false` by default), across the whole
   cause/context chain. Types and stacks are kept.
4. **Exceptions are formatted from the object.** While configured, exceptions recorded
   on spans by any instrumentation (`span.record_exception`) are formatted by the
   package from the exception object, not from text. Exception text that did not come
   from the package (e.g. a library adding its own event) is exported as the type only.
   **Span scrubbing at export.** Spans from auto-instrumentation are rebuilt before
   export: query strings removed from `url.*` / `http.target`; exception messages
   removed from exception events, from every line of exported stack traces (each
   traceback is rebuilt from its frames, so chained and multi-line messages such as
   pydantic validation errors cannot pass) and from span status descriptions; all
   strings redacted. A span that cannot be scrubbed is dropped.
5. **Noise and risk reduction:** health probes are not traced; uvicorn access logs,
   which repeat request telemetry and include query strings, are off.

6. **Paths become route templates.** On spans with `http.route`, the concrete path in
   `http.target`, `url.path` and `url.full` is replaced by the template
   (`/parties/{name}`), so identifiers and names in paths are not exported.
7. **Status descriptions** keep only an exception type or a controlled error code.
8. **Long strings** are truncated at 32,768 characters after redaction (App Insights'
   own message limit); every redaction rule runs in linear time.
9. **Logging never raises.** An unprintable message or a pathological exception chain
   produces a placeholder record; chains are capped at 25 exceptions. Handlers that
   existed before setup, and Python's last-resort handler, also redact.

What this does **not** do: it cannot know that a free-text variable you log in a
message is sensitive. Do not log content. The test harness below exists to catch it.

## Testing your service

```python
from hmcts_otel.testing import capture_telemetry


def test_submission_emits_no_content(client):
    draft = "the appellant gave evidence that..."
    with capture_telemetry() as tel:
        client.post("/api/live-transcription/submission", json={"text": draft})
    tel.assert_no_prohibited_content(draft)  # also fails on any redaction-rule match
    assert "submission.completed" in tel.event_names()
```

`TelemetryCapture` gives you `finished_spans()`, `log_records()`, `events(name)`,
`metric_points(name)` (counter `value`; histogram `count`, `sum`, `min`, `max`,
`bucket_counts`, `explicit_bounds`) and `all_exported_text()`.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OTEL_SERVICE_NAME` | `unknown_service` | Service name, one per workload |
| `APP_VERSION` / `HMCTS_OTEL_SERVICE_VERSION` | — | Release, attached to all telemetry |
| `HMCTS_OTEL_ENVIRONMENT` / `DEPLOYMENT_ENVIRONMENT` | `local` | Environment name |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | Unset means no export to Azure |
| `HMCTS_OTEL_USE_ENTRA_AUTH` | `false` | Ingest with `DefaultAzureCredential` (needs `entra` extra) |
| `HMCTS_OTEL_SAMPLING_RATIO` | `1.0` | Trace sampling. Metrics and events are never sampled. `OTEL_TRACES_SAMPLER_ARG` is deliberately not read. |
| `HMCTS_OTEL_LOG_LEVEL` / `LOG_LEVEL` | `INFO` | Root log level. Lifecycle events are recorded at any level. |
| `HMCTS_OTEL_LOG_FORMAT` | `json` | `json` or `text` |
| `HMCTS_OTEL_EXCLUDED_URLS` | `/health,/healthz,/healthcheck,/livez,/readyz,/version` | Not traced |
| `HMCTS_OTEL_CAPTURE_UVICORN_LOGS` | `true` | Route uvicorn logs through the package; access logs off |
| `HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES` | `false` | Include exception messages in exported telemetry |
| `HMCTS_OTEL_STRICT` | `false` | Raise on telemetry misuse. `capture_telemetry` turns it on |
| `HMCTS_OTEL_ALLOW_FOREIGN_TRACER_PROVIDER` | `false` | Start (with an error log) even if another tracer provider is registered |
| `HMCTS_OTEL_ENABLE_LIVE_METRICS` | `false` | App Insights Live Metrics |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | set to `false` by setup if unset | GenAI instrumentation content capture. Leave off |
| `AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED` | set to `false` by setup if unset | Azure AI SDK content recording. Leave off |
| `SENTRY_DSN` | — | Enables Sentry (with `sentry` extra) |
| `SENTRY_ENVIRONMENT` | environment | Sentry environment |
| `HMCTS_OTEL_SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Sentry tracing |

Pass `TelemetrySettings(...)` to `setup_telemetry` to set values in code. Auto-instrumentation
can be adjusted with `setup_telemetry(instrumentation_options={"requests": {"enabled": False}})`.

## Querying in App Insights

```kusto
// Everything that happened to one job, in order
customEvents
| where customDimensions["hmcts.journey.id"] == "<job id>"
| project timestamp, name, outcome = customDimensions["hmcts.outcome"],
          error = customDimensions["hmcts.error.code"], ms = customDimensions["hmcts.duration_ms"]
| order by timestamp asc

// p50/p95 stage duration, from the completed events (exact: one row per stage run)
customEvents
| where timestamp > ago(1d) and name endswith ".completed"
| extend stage = tostring(customDimensions["hmcts.stage"]), ms = todouble(customDimensions["hmcts.duration_ms"])
| summarize p50 = percentile(ms, 50), p95 = percentile(ms, 95), runs = count() by stage

// Throughput and average duration by outcome, from the metric
customMetrics
| where name == "hmcts.stage.duration" and timestamp > ago(1d)
| extend stage = tostring(customDimensions["hmcts.stage"]), outcome = tostring(customDimensions["hmcts.outcome"])
| summarize runs = sum(valueCount), avg_s = sum(valueSum) / sum(valueCount) by stage, outcome
```

Use **events for percentiles** and **metrics for counts, rates and averages**. The App
Insights exporter sends histograms as count, sum, min and max per interval, without
buckets, so a percentile over `customMetrics` is not a percentile of durations. The
histograms do carry second-based buckets for backends that use them.

All of this assumes App Insights ingestion sampling (`sampling_percentage`) is 100.

## Things to know

- **Bad configuration does not stop the service.** Invalid values (for example
  `HMCTS_OTEL_SAMPLING_RATIO=abc`), whether from the environment or passed to
  `TelemetrySettings(...)`, fall back to defaults (see `settings.invalid_fields`), and an unusable connection
  string or Sentry DSN disables that exporter. Both are logged as errors by field name,
  never by value. With `HMCTS_OTEL_STRICT=true` they raise instead. The one
  deliberate exception: another tracer provider already being registered stops
  setup, because its spans would bypass scrubbing.
- **litellm's own log lines** (the `LiteLLM` loggers) go through the package's redaction
  like any other log, which is pattern-based. Do not run litellm at DEBUG level in
  production: it logs requests.
- **`stage()` inside a sync generator** (e.g. a `StreamingResponse` body) works, but the
  span stays current between `yield`s and OpenTelemetry may log "Failed to detach
  context" when the generator is resumed in another context. Prefer instrumenting the
  work that produces the data, not the generator that streams it.

(Workspace-based resources: `AppEvents` / `AppMetrics` with `Properties`.)

## Development

```bash
uv sync --extra dev
pre-commit install          # ruff, hygiene hooks, conventions-doc check, gitleaks
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run python scripts/generate_conventions.py --check
```

`docs/conventions.md` is generated from `hmcts_otel/conventions.py`
(`uv run python scripts/generate_conventions.py`). See [CLAUDE.md](CLAUDE.md) for the
invariants every change must keep.

## Security

Report vulnerabilities privately, as described in [docs/SECURITY.md](docs/SECURITY.md).
Do not open a public issue.

## Compatibility and versioning

- Python 3.11–3.13. Tested against opentelemetry-sdk 1.40 to current and
  azure-monitor-opentelemetry 1.8.7 to current.
- Semantic versioning. Attribute, event and metric names in `hmcts_otel.conventions`
  are a public contract: renaming one is a major version.
- See [docs/design.md](docs/design.md) for design decisions and known limitations, and
  [docs/conventions.md](docs/conventions.md) for the field list.
