import pytest

from hmcts_otel import EventClass, TelemetrySchema
from hmcts_otel import conventions as c


def test_validate_accepts_registered_and_drops_the_rest(schema: TelemetrySchema) -> None:
    clean, dropped = schema.validate(
        {
            "file.size_bytes": 10,
            "file.format": "flac",  # not an allowed value
            "speech.region": "UK South",  # fails pattern
            "unknown": "x",
            c.ERROR_CODE: "SPEECH_TIMEOUT",
        }
    )
    assert clean == {"file.size_bytes": 10, c.ERROR_CODE: "SPEECH_TIMEOUT"}
    assert sorted(dropped) == [("file.format", "value"), ("speech.region", "pattern"), ("unknown", "unregistered")]


@pytest.mark.parametrize(
    ("type_", "value", "ok"),
    [
        (int, True, False),  # bool is not an int here
        (int, 3, True),
        (float, 3, True),
        (float, False, False),
        (bool, 1, False),
        (str, 5, False),
    ],
)
def test_type_checks(type_: type, value: object, ok: bool) -> None:
    s = TelemetrySchema()
    s.attribute("x.value", type_)
    clean, dropped = s.validate({"x.value": value})
    assert bool(clean) is ok
    assert bool(dropped) is not ok


def test_overlong_and_sensitive_strings_are_dropped_not_truncated() -> None:
    s = TelemetrySchema()
    s.attribute("note.label", max_length=10)
    assert s.validate({"note.label": "x" * 11})[1] == [("note.label", "length")]
    assert s.validate({"note.label": "a@b.co"})[1] == [("note.label", "redaction")]


def test_event_registration_restricts_attributes(schema: TelemetrySchema) -> None:
    schema.event("upload.selected", attributes=["file.size_bytes"], browser=True)
    spec = schema.get_event("upload.selected")
    assert spec is not None and spec.browser and spec.event_class is EventClass.OPERATIONAL
    clean, dropped = schema.validate({"file.size_bytes": 1, "file.format": "wav"}, allowed=spec.attributes)
    assert clean == {"file.size_bytes": 1}
    assert dropped == [("file.format", "not_allowed_for_event")]


def test_registration_conflicts_and_bad_names() -> None:
    s = TelemetrySchema()
    s.attribute("a.b", int)
    s.attribute("a.b", int)  # identical re-registration is fine
    with pytest.raises(ValueError):
        s.attribute("a.b", str)
    with pytest.raises(ValueError):
        s.attribute("Bad-Name")
    with pytest.raises(ValueError):
        s.event("ok.event", attributes=["not.registered"])


def test_check_identifier() -> None:
    s = TelemetrySchema()
    assert s.check_identifier(c.ERROR_CODE, "speech.timeout") == "speech.timeout"
    with pytest.raises(ValueError):
        s.check_identifier(c.ERROR_CODE, "contains spaces and text")


def test_markdown_lists_attributes_and_events(schema: TelemetrySchema) -> None:
    schema.event("upload.selected", attributes=["file.size_bytes"], browser=True, description="File chosen")
    md = schema.to_markdown()
    assert "`file.size_bytes`" in md and "`hmcts.journey.id`" in md
    assert "| `upload.selected` | operational | yes |" in md
