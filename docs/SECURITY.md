# Security Policy

## Supported Versions

This project follows [semantic versioning](https://semver.org/). Security fixes are made
against the latest released minor version on the `main` branch; older versions are not
patched separately.

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Report suspected vulnerabilities via **GitHub private vulnerability reporting**: open a
report from the "Security" tab of this repository → "Report a vulnerability". This
creates a private advisory visible only to maintainers until a fix is published.

Please include:

- A description of the vulnerability and its potential impact
- Steps to reproduce (a minimal reproduction is ideal)
- The affected version(s) of `hmcts-otel`

We aim to acknowledge reports within 5 working days and to keep you informed of
remediation progress. Please allow us a reasonable period to investigate and release a
fix before any public disclosure.

## Scope Notes

For this library, a vulnerability includes any way for personal, case or credential
data to reach exported telemetry (traces, logs, events, metrics or Sentry) despite the
[privacy model](../README.md#privacy-model): for example, a redaction rule that can be
bypassed, an attribute that escapes the schema allowlist, or exception text exported
while `HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES` is off.

The library cannot know that a free-text value a service logs in a message is
sensitive; logging content in the application itself is out of scope, although
suggestions for catching it are welcome. The browser telemetry relay must be mounted
behind the consuming service's authentication; reports that rely on an unauthenticated
relay should note that assumption.
