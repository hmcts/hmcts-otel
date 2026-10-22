# CLAUDE.md

Guidance for Claude Code (and contributors) working in **hmcts-otel**: a
privacy-safe OpenTelemetry **library** for HMCTS Python services on Azure,
consumed by several applications.

## Treat this as public, privacy-critical code

Assume the repository and its full git history may be world-readable. The library
exists to stop personal and case data reaching telemetry: a flaw here is inherited
by every service that depends on it.

**Never commit:** secrets or credentials, connection strings or instrumentation keys,
real Azure tenant/subscription/resource IDs, internal hostnames, or real person or
case data. Use placeholders and env-sourced configuration. Test data must be
obviously fictional.

## Invariants: do NOT regress these

See [docs/design.md](docs/design.md) for the reasoning behind each.

- **Allowlist, not blocklist.** Custom attributes are exported only if registered in
  the schema with a type and rule. Unregistered keys, wrong types and values that
  match a redaction rule are **dropped, never truncated**, and counted on
  `hmcts.telemetry.dropped_attributes`. Never add a "pass everything" escape hatch.
- **Exception text is not exported by default.** `HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES`
  defaults to `false`; traces are rebuilt from frames, never pattern-matched. Do not
  flip a privacy default to make something pass: fix the caller or the test.
- **Telemetry never changes application behaviour.** Instrumentation, events, stages
  and settings must never raise into the caller, block it, or change its result.
  Failures are counted on `hmcts.telemetry.errors`.
- **Names are a public contract.** Attribute, event and metric names in
  `hmcts_otel/conventions.py` are consumed by services' dashboards and alerts.
  Renaming one is a major version. `docs/conventions.md` is generated from it.
- **Browser input is untrusted.** The relay accepts only registered events,
  attributes and journey types, behind the service's own authentication.

## Dev workflow

- Managed with **uv**. Install: `uv sync --extra dev`.
- **Verify before claiming done** (run and read the output):
  ```bash
  uv run pytest -q
  uv run ruff check .
  uv run ruff format --check .
  uv run pyright
  uv run python scripts/generate_conventions.py --check
  uv build            # packaging must stay valid (published to the hmcts-lib feed)
  ```
- `ruff` and `pyright` are blocking in CI. CI also runs the tests on Python
  3.11–3.13 and against the minimum supported dependency versions.

## Testing

Behavioural code ships with a test that turns red if it regresses. Use
`hmcts_otel.testing.capture_telemetry` and `assert_no_prohibited_content` to assert
on what is actually exported, not on mocks being called. A new privacy rule (a
redaction, a drop) must have a test that fails if the rule is removed.

## Pre-commit

`pre-commit` runs ruff, hygiene hooks, the conventions-doc check and **gitleaks**
(secret scanning). Install once: `pre-commit install` (a Claude Code
`SessionStart` hook in `.claude/settings.json` does this automatically). Requires
`gitleaks` on `PATH`.
