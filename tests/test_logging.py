import json
import logging

import pytest

from hmcts_otel import TelemetrySettings, journey, setup_telemetry
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture


def _stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


def test_json_logs_carry_context_and_are_redacted(
    tel: TelemetryCapture, capsys: pytest.CaptureFixture[str], journey_id: str
) -> None:
    log = logging.getLogger("app.module")
    with journey("dictation.session", journey_id):
        log.info("saved draft for %s", "judge@justice.gov.uk")

    (line,) = [entry for entry in _stdout_lines(capsys) if entry["logger"] == "app.module"]
    assert line["message"] == "saved draft for [REDACTED]"
    assert line[c.JOURNEY_ID] == journey_id
    assert line["service.name"] == "test" and line["level"] == "INFO"

    (record,) = [r for r in tel.log_records() if r.body == "saved draft for [REDACTED]"]
    assert record.attributes[c.JOURNEY_ID] == journey_id
    tel.assert_no_prohibited_content("judge@justice.gov.uk")


def test_logged_exceptions_lose_their_messages(tel: TelemetryCapture, capsys: pytest.CaptureFixture[str]) -> None:
    log = logging.getLogger("app.module")
    try:
        draft = "draft decision text: the appellant is credible"
        raise ValueError(draft)
    except ValueError:
        log.exception("failed to save")

    (line,) = [e for e in _stdout_lines(capsys) if e.get("message") == "failed to save"]
    assert line["exception.type"] == "ValueError"
    assert "ValueError: [REDACTED]" in line["exception.stacktrace"]
    tel.assert_no_prohibited_content("the appellant is credible")


def test_string_extras_are_redacted(tel: TelemetryCapture, capsys: pytest.CaptureFixture[str]) -> None:
    logging.getLogger("app").warning("lookup", extra={"who": "someone@example.org"})
    (line,) = [e for e in _stdout_lines(capsys) if e.get("message") == "lookup"]
    assert line["who"] == "[REDACTED]"


def test_events_recorded_even_when_app_log_level_is_warning(capsys: pytest.CaptureFixture[str]) -> None:
    from hmcts_otel import record_event
    from hmcts_otel.testing import capture_telemetry

    settings = TelemetrySettings(service_name="t", log_level="WARNING", connection_string=None, sentry_dsn=None)
    with capture_telemetry(settings) as tel:
        logging.getLogger("app").info("hidden")
        record_event("job.created")
        assert tel.event_names() == ["job.created"]
        assert all(r.body != "hidden" for r in tel.log_records())


def test_text_format_and_uvicorn_routing(capsys: pytest.CaptureFixture[str]) -> None:
    setup_telemetry(TelemetrySettings(service_name="t", log_format="text", connection_string=None, sentry_dsn=None))
    logging.getLogger("uvicorn.error").info("server started")
    logging.getLogger("uvicorn.access").info("GET /x?search=private HTTP/1.1 200")
    out = capsys.readouterr().out
    assert "server started" in out
    assert "search=private" not in out
