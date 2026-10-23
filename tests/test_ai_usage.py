"""AI usage metering (OBS-5): the public API, feature context and content safety."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as semconv_attrs
from opentelemetry.semconv._incubating.metrics import gen_ai_metrics as semconv_metrics

from hmcts_otel import (
    TelemetrySettings,
    _state,
    ai_fallback,
    ai_feature,
    ai_usage,
    current_ai_feature,
    current_stage,
    get_tracer,
    record_llm_usage,
    record_speech_usage,
    stage,
)
from hmcts_otel import conventions as c
from hmcts_otel._setup import GENAI_CONTENT_CAPTURE_VARIABLES
from hmcts_otel.testing import TelemetryCapture, capture_telemetry


def _settings(**overrides: Any) -> TelemetrySettings:
    values: dict[str, Any] = {
        "service_name": "test",
        "environment": "test",
        "connection_string": None,
        "sentry_dsn": None,
        "strict": False,
    }
    values.update(overrides)
    return TelemetrySettings(**values)


@pytest.fixture
def lenient() -> Iterator[TelemetryCapture]:
    """Production behaviour: misuse is counted, not raised."""
    with capture_telemetry(_settings()) as capture:
        yield capture


def tokens(tel: TelemetryCapture) -> dict[str, dict[str, Any]]:
    """Token-usage points keyed by token type."""
    return {p["attributes"][c.GEN_AI_TOKEN_TYPE]: p for p in tel.metric_points(c.METRIC_GEN_AI_TOKEN_USAGE)}


def errors(tel: TelemetryCapture) -> dict[str, int]:
    return {p["attributes"]["kind"]: p["value"] for p in tel.metric_points(c.METRIC_TELEMETRY_ERRORS)}


def metric(tel: TelemetryCapture, name: str) -> Any:
    data = tel.metrics.get_metrics_data()
    assert data is not None
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    return m
    raise AssertionError(f"metric {name} not exported")


# Names --------------------------------------------------------------------------------


def test_names_match_the_pinned_semantic_conventions() -> None:
    assert c.GEN_AI_SEMCONV_VERSION == "1.37.0"
    assert c.METRIC_GEN_AI_TOKEN_USAGE == semconv_metrics.GEN_AI_CLIENT_TOKEN_USAGE
    assert c.GEN_AI_OPERATION_NAME == semconv_attrs.GEN_AI_OPERATION_NAME
    assert c.GEN_AI_PROVIDER_NAME == semconv_attrs.GEN_AI_PROVIDER_NAME
    assert c.GEN_AI_REQUEST_MODEL == semconv_attrs.GEN_AI_REQUEST_MODEL
    assert c.GEN_AI_RESPONSE_MODEL == semconv_attrs.GEN_AI_RESPONSE_MODEL
    assert c.GEN_AI_TOKEN_TYPE == semconv_attrs.GEN_AI_TOKEN_TYPE
    assert {t.value for t in c.TokenType} == {"input", "output"}


# record_llm_usage ---------------------------------------------------------------------


def test_llm_usage_records_one_point_per_token_type(tel: TelemetryCapture) -> None:
    record_llm_usage(
        provider="azure",
        request_model="gpt-4o",
        response_model="gpt-4o-2024-08-06",
        input_tokens=120,
        output_tokens=45,
        feature="summarise",
    )
    common = {
        c.GEN_AI_OPERATION_NAME: "chat",
        c.GEN_AI_PROVIDER_NAME: "azure.ai.openai",  # litellm's "azure", as the semconv value
        c.GEN_AI_REQUEST_MODEL: "gpt-4o",
        c.GEN_AI_RESPONSE_MODEL: "gpt-4o-2024-08-06",
        c.AI_FEATURE: "summarise",
        c.OUTCOME: "success",
        c.AI_FALLBACK: False,
    }
    points = tokens(tel)
    assert set(points) == {"input", "output"}
    assert points["input"]["attributes"] == {**common, c.GEN_AI_TOKEN_TYPE: "input"}
    assert points["output"]["attributes"] == {**common, c.GEN_AI_TOKEN_TYPE: "output"}
    assert (points["input"]["count"], points["input"]["sum"]) == (1, 120)
    assert (points["output"]["count"], points["output"]["sum"]) == (1, 45)

    (request,) = tel.metric_points(c.METRIC_AI_REQUESTS)
    assert request["value"] == 1
    assert request["attributes"] == common  # no token type; no error.type on success

    histogram = metric(tel, c.METRIC_GEN_AI_TOKEN_USAGE)
    assert histogram.unit == "{token}"
    assert metric(tel, c.METRIC_AI_REQUESTS).unit == "{request}"
    assert errors(tel) == {}


def test_token_histogram_uses_semconv_buckets(tel: TelemetryCapture) -> None:
    record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=5, output_tokens=0)
    point = tokens(tel)["input"]
    assert tuple(point["explicit_bounds"]) == tuple(float(b) for b in c.GEN_AI_TOKEN_BUCKETS)
    # 5 tokens falls in (4, 16].
    assert list(point["bucket_counts"])[2] == 1
    assert point["min"] == point["max"] == 5


def test_failed_call_records_error_type_on_requests_only(tel: TelemetryCapture) -> None:
    record_llm_usage(
        provider="gemini",
        request_model="gemini-2.0-flash",
        input_tokens=0,
        output_tokens=0,
        outcome="error",
        error_type="RateLimitError",
    )
    points = tokens(tel)
    assert points["input"]["sum"] == 0 and points["input"]["attributes"][c.OUTCOME] == "error"
    assert c.ERROR_TYPE not in points["input"]["attributes"]
    (request,) = tel.metric_points(c.METRIC_AI_REQUESTS)
    assert request["attributes"][c.ERROR_TYPE] == "RateLimitError"
    assert request["attributes"][c.GEN_AI_PROVIDER_NAME] == "gcp.gemini"
    assert c.GEN_AI_RESPONSE_MODEL not in request["attributes"]  # unknown, so absent


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "azure openai"),  # space
        ("provider", "x" * 129),  # too long
        ("provider", None),
        ("request_model", "gpt-4o; DROP TABLE"),
        ("request_model", "jane.doe@example.com"),  # passes the pattern, caught by redaction
        ("request_model", 42),
        ("response_model", "model\nwith newline"),
    ],
)
def test_invalid_identifiers_become_unknown(tel: TelemetryCapture, field: str, value: Any) -> None:
    kwargs: dict[str, Any] = {
        "provider": "openai",
        "request_model": "gpt-4o",
        "response_model": "gpt-4o",
        "input_tokens": 1,
        "output_tokens": 2,
    }
    kwargs[field] = value
    record_llm_usage(**kwargs)  # strict mode is on in `tel`: still never raises

    key = {
        "provider": c.GEN_AI_PROVIDER_NAME,
        "request_model": c.GEN_AI_REQUEST_MODEL,
        "response_model": c.GEN_AI_RESPONSE_MODEL,
    }[field]
    for point in tokens(tel).values():
        assert point["attributes"][key] == c.UNKNOWN
    kind = "invalid.ai_usage.provider" if field == "provider" else "invalid.ai_usage.model"
    assert errors(tel)[kind] >= 1
    if isinstance(value, str):
        tel.assert_no_prohibited_content(value)


@pytest.mark.parametrize("bad", [-1, -0.5, math.nan, math.inf, 1.5, True, None, "10"])
def test_invalid_token_counts_are_skipped_and_counted(tel: TelemetryCapture, bad: Any) -> None:
    record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=bad, output_tokens=7)
    points = tokens(tel)
    assert "input" not in points
    assert points["output"]["sum"] == 7
    assert errors(tel)["invalid.ai_usage.tokens"] == 1
    # The call itself is still counted.
    assert tel.metric_points(c.METRIC_AI_REQUESTS)[0]["value"] == 1


def test_integral_float_tokens_are_accepted(tel: TelemetryCapture) -> None:
    record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=3.0, output_tokens=0)  # type: ignore[arg-type]
    assert tokens(tel)["input"]["sum"] == 3


def test_invalid_outcome_operation_feature_and_fallback(tel: TelemetryCapture) -> None:
    record_llm_usage(
        provider="openai",
        request_model="gpt-4o",
        input_tokens=1,
        output_tokens=1,
        outcome="exploded",
        operation="Chat Completion",
        feature="Summarise Hearing",
        fallback="yes",  # type: ignore[arg-type]
        error_type="boom: the appellant said",
    )
    attrs = tel.metric_points(c.METRIC_AI_REQUESTS)[0]["attributes"]
    assert attrs[c.OUTCOME] == c.UNKNOWN
    assert attrs[c.GEN_AI_OPERATION_NAME] == c.UNKNOWN
    assert attrs[c.AI_FEATURE] == ai_usage.INVALID_FEATURE
    assert attrs[c.AI_FALLBACK] is False
    assert attrs[c.ERROR_TYPE] == c.UNKNOWN
    counted = errors(tel)
    for kind in ("outcome", "operation", "feature", "fallback", "error_type"):
        assert counted[f"invalid.ai_usage.{kind}"] >= 1, kind
    tel.assert_no_prohibited_content("the appellant said", "Summarise Hearing")


def test_record_functions_never_raise_on_internal_failure(
    tel: TelemetryCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(_state, "instrument", broken)
    record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=1, output_tokens=1)
    record_speech_usage(seconds=1, mode="fast")


# record_speech_usage ------------------------------------------------------------------


def test_speech_usage_counts_seconds(tel: TelemetryCapture) -> None:
    record_speech_usage(seconds=90.5, mode="batch", feature="transcribe")
    record_speech_usage(seconds=30, mode=c.SpeechMode.BATCH, feature="transcribe")
    record_speech_usage(seconds=12, mode="realtime", feature="dictation", outcome="cancelled")
    points = {p["attributes"][c.SPEECH_MODE]: p for p in tel.metric_points(c.METRIC_SPEECH_AUDIO_DURATION)}
    assert points["batch"]["value"] == pytest.approx(120.5)
    assert points["batch"]["attributes"] == {
        c.SPEECH_MODE: "batch",
        c.AI_FEATURE: "transcribe",
        c.OUTCOME: "success",
    }
    assert points["realtime"]["attributes"][c.OUTCOME] == "cancelled"
    assert metric(tel, c.METRIC_SPEECH_AUDIO_DURATION).unit == "s"


@pytest.mark.parametrize("bad", [-1, math.nan, math.inf, -math.inf, True, None, "60"])
def test_invalid_seconds_are_not_recorded(tel: TelemetryCapture, bad: Any) -> None:
    record_speech_usage(seconds=bad, mode="fast")
    assert tel.metric_points(c.METRIC_SPEECH_AUDIO_DURATION) == []
    assert errors(tel)["invalid.ai_usage.seconds"] == 1


def test_invalid_speech_mode_is_unknown(tel: TelemetryCapture) -> None:
    record_speech_usage(seconds=5, mode="turbo")
    (point,) = tel.metric_points(c.METRIC_SPEECH_AUDIO_DURATION)
    assert point["attributes"][c.SPEECH_MODE] == c.UNKNOWN
    assert errors(tel)["invalid.ai_usage.speech_mode"] == 1


# Feature and fallback context ---------------------------------------------------------


def test_feature_from_context_then_stage_then_unknown(tel: TelemetryCapture) -> None:
    assert current_ai_feature() == c.UNKNOWN
    with stage("summarise_hearing"):
        assert current_stage() == "summarise_hearing"
        assert current_ai_feature() == "summarise_hearing"
        with ai_feature("summarise"):
            assert current_ai_feature() == "summarise"
            record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=1, output_tokens=1)
            # An explicit feature wins over the context.
            record_llm_usage(
                provider="openai", request_model="gpt-4o", input_tokens=1, output_tokens=1, feature="redact"
            )
        assert current_ai_feature() == "summarise_hearing"
        record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=1, output_tokens=1)
    assert current_stage() is None
    record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=1, output_tokens=1)

    features = {p["attributes"][c.AI_FEATURE] for p in tel.metric_points(c.METRIC_AI_REQUESTS)}
    assert features == {"summarise", "redact", "summarise_hearing", c.UNKNOWN}


def test_nested_stages_restore_the_outer_stage(tel: TelemetryCapture) -> None:
    with stage("outer"):
        with stage("inner"):
            assert current_stage() == "inner"
        assert current_stage() == "outer"
        with pytest.raises(KeyError):
            with stage("failing"):
                raise KeyError("x")
        assert current_stage() == "outer"
    assert current_stage() is None


async def test_feature_crosses_await_and_tasks(tel: TelemetryCapture) -> None:
    async def call() -> str:
        await asyncio.sleep(0)
        return current_ai_feature()

    with ai_feature("draft_judgment"):
        assert await call() == "draft_judgment"
        assert await asyncio.create_task(call()) == "draft_judgment"
    assert await call() == c.UNKNOWN


def test_invalid_feature_name_raises_in_strict_mode(tel: TelemetryCapture) -> None:
    with pytest.raises(ValueError):
        with ai_feature("Summarise hearing for Jane"):
            pass


def test_invalid_feature_name_is_counted_in_production(lenient: TelemetryCapture) -> None:
    with ai_feature("Summarise hearing for Jane") as feature:
        assert feature == ai_usage.INVALID_FEATURE
        record_llm_usage(provider="openai", request_model="gpt-4o", input_tokens=1, output_tokens=1)
    assert errors(lenient)["invalid.ai_feature"] == 1
    (request,) = lenient.metric_points(c.METRIC_AI_REQUESTS)
    assert request["attributes"][c.AI_FEATURE] == ai_usage.INVALID_FEATURE
    lenient.assert_no_prohibited_content("Jane")


def test_ai_fallback_marks_calls_inside_the_block(tel: TelemetryCapture) -> None:
    record_llm_usage(provider="azure", request_model="gpt-4o", input_tokens=1, output_tokens=1)
    with ai_fallback():
        record_llm_usage(provider="gemini", request_model="gemini-2.0-flash", input_tokens=1, output_tokens=1)
    record_llm_usage(provider="azure", request_model="gpt-4o-mini", input_tokens=1, output_tokens=1, fallback=True)
    by_model = {
        p["attributes"][c.GEN_AI_REQUEST_MODEL]: p["attributes"][c.AI_FALLBACK]
        for p in tel.metric_points(c.METRIC_AI_REQUESTS)
    }
    assert by_model == {"gpt-4o": False, "gemini-2.0-flash": True, "gpt-4o-mini": True}


# Content safety -----------------------------------------------------------------------


def test_setup_turns_genai_content_capture_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GENAI_CONTENT_CAPTURE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    with capture_telemetry():
        for name in GENAI_CONTENT_CAPTURE_VARIABLES:
            assert os.environ[name] == "false"


def test_explicit_content_capture_is_kept_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true")
    with caplog.at_level(logging.WARNING, logger="hmcts_otel"):
        with capture_telemetry():
            assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "true"
    assert any("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT" in r.getMessage() for r in caplog.records)


def test_genai_content_attributes_are_dropped_from_exported_spans(tel: TelemetryCapture) -> None:
    prompt = "the appellant told the tribunal about her illness"
    answer = "summary of the appellant's evidence"
    with get_tracer("litellm-otel").start_as_current_span(
        "chat gpt-4o",
        attributes={
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.prompt.0.content": prompt,
            "gen_ai.prompt.0.role": "user",
            "gen_ai.completion.0.content": answer,
            "gen_ai.input.messages": f'[{{"role": "user", "content": "{prompt}"}}]',
            "gen_ai.output.messages": answer,
            "gen_ai.system_instructions": prompt,
            "gen_ai.tool.call.arguments": prompt,
            "llm.prompts": prompt,
            "input.value": prompt,
        },
    ) as span:
        span.add_event("gen_ai.content.prompt", {"gen_ai.prompt": prompt})
        span.add_event("gen_ai.content.completion", {"gen_ai.completion": answer})

    (exported,) = tel.finished_spans()
    assert dict(exported.attributes) == {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 12}
    assert all(dict(e.attributes or {}) == {} for e in exported.events)
    tel.assert_no_prohibited_content(prompt, answer)


def test_similar_but_harmless_attribute_names_are_kept(tel: TelemetryCapture) -> None:
    with get_tracer().start_as_current_span(
        "x", attributes={"gen_ai.request.model": "m", "gen_ai.prompt_cache_hit": True, "input.size": 3}
    ):
        pass
    (exported,) = tel.finished_spans()
    assert set(exported.attributes) >= {"gen_ai.request.model", "gen_ai.prompt_cache_hit", "input.size"}
