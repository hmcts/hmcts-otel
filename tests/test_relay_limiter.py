"""The relay rate limiter has a hard capacity and bounded cleanup (PR #4 review, M2)."""

from __future__ import annotations

import pytest

from hmcts_otel import conventions as c
from hmcts_otel import fastapi as relay
from hmcts_otel.testing import TelemetryCapture


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(relay.time, "monotonic", fake)
    return fake


def test_new_identities_are_refused_at_capacity_and_known_ones_keep_their_state(clock: _Clock) -> None:
    limiter = relay._FixedWindowLimiter(limit=2, max_keys=3)
    for key in ("a", "b", "c"):
        assert limiter.allow(key)
    assert limiter.allow("a")  # second request for a known identity
    assert not limiter.allow("a")  # its limit is kept, not reset

    assert not limiter.allow("d")  # a new identity at capacity is refused
    assert not limiter.allow("e")
    assert len(limiter._buckets) == 3
    assert limiter.allow("b")  # known identities are unaffected


def test_expired_identities_make_room_after_the_window(clock: _Clock) -> None:
    limiter = relay._FixedWindowLimiter(limit=5, window_s=60, max_keys=2)
    assert limiter.allow("a") and limiter.allow("b")
    assert not limiter.allow("c")

    clock.now += 61  # both buckets have expired
    assert limiter.allow("c")
    assert set(limiter._buckets) == {"c"}


def test_cleanup_runs_at_most_once_per_interval(clock: _Clock) -> None:
    limiter = relay._FixedWindowLimiter(limit=5, window_s=60, max_keys=3, prune_interval_s=5)
    for key in ("a", "b", "c"):
        assert limiter.allow(key)

    rebuilds = 0
    original = limiter._buckets

    def requests_for_new_identities(count: int) -> None:
        nonlocal rebuilds, original
        for i in range(count):
            limiter.allow(f"new-{clock.now}-{i}")
            if limiter._buckets is not original:
                rebuilds += 1
                original = limiter._buckets

    requests_for_new_identities(50)  # one cleanup, then refusals without rebuilding
    assert rebuilds <= 1
    clock.now += 1
    requests_for_new_identities(50)  # still inside the interval: no cleanup
    assert rebuilds <= 1
    clock.now += 5
    requests_for_new_identities(50)  # the next interval allows one more
    assert rebuilds <= 2


def test_requests_for_known_identities_never_trigger_cleanup(clock: _Clock) -> None:
    limiter = relay._FixedWindowLimiter(limit=1000, max_keys=3)
    for key in ("a", "b", "c"):
        limiter.allow(key)
    buckets = limiter._buckets
    for _ in range(100):
        limiter.allow("a")
    assert limiter._buckets is buckets


def test_refusals_at_capacity_are_counted(tel: TelemetryCapture, clock: _Clock) -> None:
    limiter = relay._FixedWindowLimiter(limit=5, max_keys=1)
    assert limiter.allow("a")
    assert not limiter.allow("b")
    assert not limiter.allow("c")
    points = [
        p
        for p in tel.metric_points(c.METRIC_TELEMETRY_ERRORS)
        if p["attributes"].get("kind") == "invalid.relay_capacity"
    ]
    assert sum(p["value"] for p in points) == 2


# Fixes applied on GitHub alongside the limiter change -------------------------------


def _lenient() -> object:
    from hmcts_otel import TelemetrySettings
    from hmcts_otel.testing import capture_telemetry

    settings = TelemetrySettings(
        service_name="test", environment="test", connection_string=None, sentry_dsn=None, strict=False
    )
    return capture_telemetry(settings)


def test_a_rejected_nested_journey_runs_without_the_outer_journey(journey_id: str) -> None:
    from hmcts_otel import journey, record_event

    with _lenient() as capture:  # type: ignore[attr-defined]
        with journey("recording.job", journey_id):
            with journey("recording.job", "PA/50123/2024") as inner:  # a business identifier: rejected
                assert inner is None
                record_event("job.created")
            record_event("job.queued")
        inside, after = capture.events("job.created")[0], capture.events("job.queued")[0]
    assert c.JOURNEY_ID not in inside  # not attributed to the outer journey
    assert after[c.JOURNEY_ID] == journey_id  # the outer journey is restored afterwards


def test_events_keep_their_journey_and_request_id(tel: TelemetryCapture, journey_id: str) -> None:
    from hmcts_otel import journey, record_event, request_id_context

    with request_id_context("0f8e2c6a-5b3d-4c1e-9a7f-2d4b6e8a0c1e"), journey("recording.job", journey_id):
        record_event("job.created")
    (event,) = tel.events("job.created")
    assert event[c.JOURNEY_ID] == journey_id
    assert event[c.REQUEST_ID] == "0f8e2c6a-5b3d-4c1e-9a7f-2d4b6e8a0c1e"


def test_stack_info_is_redacted() -> None:
    # stack_info is printed by the JSON stdout formatter, so the filter must redact it.
    import logging

    from hmcts_otel import default_redactor
    from hmcts_otel._logging import RedactingFilter

    record = logging.LogRecord("app", logging.WARNING, __file__, 1, "stack check", None, None)
    record.stack_info = "Stack (most recent call last):\n  note: judge.smith@example.test"
    assert RedactingFilter(default_redactor).filter(record)
    assert "judge.smith@example.test" not in (record.stack_info or "")


def test_an_extra_whose_key_matches_a_redaction_rule_is_dropped(tel: TelemetryCapture) -> None:
    import logging

    logging.getLogger("app").warning("saved", extra={"judge.smith@example.test": "x"})
    tel.assert_no_prohibited_content("judge.smith@example.test")
