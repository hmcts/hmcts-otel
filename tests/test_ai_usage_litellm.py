"""instrument_litellm(): token usage from real litellm calls (mock responses, no network)."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

from hmcts_otel import ai_fallback, ai_feature, ai_usage, instrument_litellm, stage
from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture

# Use litellm's bundled model map instead of fetching it on import.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
litellm = pytest.importorskip("litellm")
CustomLogger = pytest.importorskip("litellm.integrations.custom_logger").CustomLogger

PROMPT = "SENTINEL-PROMPT the appellant described her medical history"
ANSWER = "SENTINEL-ANSWER summary of the medical evidence"
_LISTS = ("callbacks", "success_callback", "failure_callback", "_async_success_callback", "_async_failure_callback")


@pytest.fixture(autouse=True)
def _litellm_callbacks() -> Iterator[None]:
    """Each test starts with no litellm callbacks and leaves none behind."""
    saved = {name: list(getattr(litellm, name)) for name in _LISTS}
    for name in _LISTS:
        getattr(litellm, name).clear()
    yield
    for name in _LISTS:
        getattr(litellm, name)[:] = saved[name]


class Recorder(CustomLogger):
    """Stands in for another integration (e.g. Langfuse) already registered."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self.calls += 1

    async def async_log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
        self.calls += 1


def ours() -> list[Any]:
    return [cb for cb in litellm.callbacks if getattr(cb, "hmcts_otel_usage", False) is True]


async def wait_for(tel: TelemetryCapture, calls: int, timeout: float = 5.0) -> list[dict[str, Any]]:
    """litellm logs asynchronously: wait until ``calls`` requests are recorded."""
    deadline = time.monotonic() + timeout
    while True:
        points = tel.metric_points(c.METRIC_AI_REQUESTS)
        if sum(p["value"] for p in points) >= calls or time.monotonic() > deadline:
            return points
        await asyncio.sleep(0.02)


def tokens(tel: TelemetryCapture) -> dict[str, dict[str, Any]]:
    return {p["attributes"][c.GEN_AI_TOKEN_TYPE]: p for p in tel.metric_points(c.METRIC_GEN_AI_TOKEN_USAGE)}


def messages(text: str = PROMPT) -> list[dict[str, str]]:
    return [{"role": "user", "content": text}]


async def test_async_completion_records_tokens(tel: TelemetryCapture) -> None:
    assert instrument_litellm() is True
    with ai_feature("summarise"):
        response = await litellm.acompletion(
            model="azure/gpt-4o",
            messages=messages(),
            mock_response=ANSWER,
            api_key="fake",
            api_base="https://example.invalid",
        )
    (request,) = await wait_for(tel, 1)
    await asyncio.sleep(0.2)  # a second (duplicate) record would arrive now

    common = {
        c.GEN_AI_OPERATION_NAME: "chat",
        c.GEN_AI_PROVIDER_NAME: "azure.ai.openai",
        c.GEN_AI_REQUEST_MODEL: "gpt-4o",
        c.GEN_AI_RESPONSE_MODEL: response.model,
        c.AI_FEATURE: "summarise",
        c.OUTCOME: "success",
        c.AI_FALLBACK: False,
    }
    assert request == {"attributes": common, "value": 1}  # exactly once
    points = tokens(tel)
    assert points["input"]["attributes"] == {**common, c.GEN_AI_TOKEN_TYPE: "input"}
    assert (points["input"]["count"], points["input"]["sum"]) == (1, response.usage.prompt_tokens)
    assert (points["output"]["count"], points["output"]["sum"]) == (1, response.usage.completion_tokens)
    assert response.usage.prompt_tokens > 0 and response.usage.completion_tokens > 0
    tel.assert_no_prohibited_content(PROMPT, ANSWER, "SENTINEL")


async def test_sync_completion_records_tokens_with_stage_feature(tel: TelemetryCapture) -> None:
    instrument_litellm()
    with stage("draft_judgment"):
        response = litellm.completion(
            model="gemini/gemini-2.0-flash", messages=messages(), mock_response=ANSWER, api_key="fake"
        )
    (request,) = await wait_for(tel, 1)
    assert request["attributes"][c.GEN_AI_PROVIDER_NAME] == "gcp.gemini"
    assert request["attributes"][c.AI_FEATURE] == "draft_judgment"
    assert tokens(tel)["output"]["sum"] == response.usage.completion_tokens
    tel.assert_no_prohibited_content(PROMPT, ANSWER, "SENTINEL")


async def test_failure_records_error_outcome_and_class_only(tel: TelemetryCapture) -> None:
    instrument_litellm()
    with pytest.raises(Exception) as raised:
        await litellm.acompletion(
            model="openai/gpt-4o",
            messages=messages(),
            mock_response=Exception("SENTINEL-ERROR provider echoed the prompt"),
            api_key="fake",
        )
    (request,) = await wait_for(tel, 1)
    assert request["attributes"][c.OUTCOME] == "error"
    assert request["attributes"][c.ERROR_TYPE] == type(raised.value).__name__
    points = tokens(tel)
    assert points["input"]["sum"] == 0 and points["output"]["sum"] == 0
    assert points["input"]["attributes"][c.OUTCOME] == "error"
    tel.assert_no_prohibited_content(PROMPT, "SENTINEL")


async def test_fallback_from_metadata_and_context(tel: TelemetryCapture) -> None:
    instrument_litellm()
    await litellm.acompletion(
        model="openai/gpt-4o",
        messages=messages(),
        mock_response="ok",
        api_key="fake",
        metadata={"hmcts_ai_fallback": True},
    )
    with ai_fallback():
        await litellm.acompletion(model="openai/gpt-4o-mini", messages=messages(), mock_response="ok", api_key="fake")
    await litellm.acompletion(model="openai/o3-mini", messages=messages(), mock_response="ok", api_key="fake")
    points = await wait_for(tel, 3)
    by_model = {p["attributes"][c.GEN_AI_REQUEST_MODEL]: p["attributes"][c.AI_FALLBACK] for p in points}
    assert by_model == {"gpt-4o": True, "gpt-4o-mini": True, "o3-mini": False}


async def test_router_fallback_is_detected(tel: TelemetryCapture) -> None:
    instrument_litellm()
    router = litellm.Router(
        model_list=[
            {
                "model_name": "primary",
                "litellm_params": {
                    "model": "azure/gpt-4o",
                    "api_key": "fake",
                    "api_base": "https://example.invalid",
                    "mock_response": Exception("primary down"),
                },
            },
            {
                "model_name": "backup",
                "litellm_params": {"model": "gemini/gemini-2.0-flash", "api_key": "fake", "mock_response": "ok"},
            },
        ],
        fallbacks=[{"primary": ["backup"]}],
        num_retries=0,
    )
    await router.acompletion(model="primary", messages=messages())
    points = await wait_for(tel, 2)
    by_provider = {p["attributes"][c.GEN_AI_PROVIDER_NAME]: p["attributes"] for p in points}
    assert by_provider["azure.ai.openai"][c.OUTCOME] == "error"
    assert by_provider["azure.ai.openai"][c.AI_FALLBACK] is False
    assert by_provider["gcp.gemini"][c.OUTCOME] == "success"
    assert by_provider["gcp.gemini"][c.AI_FALLBACK] is True


async def test_registration_is_idempotent_and_keeps_existing_callbacks(tel: TelemetryCapture) -> None:
    existing = Recorder()
    litellm.callbacks.append(existing)
    litellm.success_callback.append("langfuse")  # string integrations live in other lists

    assert instrument_litellm() is True
    assert instrument_litellm() is True
    assert len(ours()) == 1
    assert existing in litellm.callbacks
    assert litellm.success_callback == ["langfuse"]

    litellm.success_callback.clear()  # do not actually call Langfuse
    await litellm.acompletion(model="openai/gpt-4o", messages=messages(), mock_response="ok", api_key="fake")
    (request,) = await wait_for(tel, 1)
    assert request["value"] == 1
    deadline = time.monotonic() + 5
    while existing.calls == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert existing.calls == 1


def test_reregisters_if_callbacks_were_replaced() -> None:
    instrument_litellm()
    litellm.callbacks[:] = [Recorder()]  # e.g. an app assigning litellm.callbacks later
    assert instrument_litellm() is True
    assert len(ours()) == 1 and len(litellm.callbacks) == 2


def test_without_litellm_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "litellm", None)  # import litellm -> ImportError
    assert instrument_litellm() is False


# The callback reads only what it needs ---------------------------------------------


class Tracking(dict):
    """A dict that remembers which keys were read."""

    def __init__(self, *args: Any, seen: set[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.seen = seen

    def get(self, key: Any, default: Any = None) -> Any:
        self.seen.add(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self.seen.add(key)
        return super().__getitem__(key)


class TrackingObject:
    def __init__(self, seen: set[str], **fields: Any) -> None:
        object.__setattr__(self, "_seen", seen)
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name: str) -> Any:
        self._seen.add(name)
        if name in self._fields:
            return self._fields[name]
        raise AttributeError(name)


def test_callback_reads_only_usage_model_and_provider(tel: TelemetryCapture) -> None:
    seen: set[str] = set()
    usage = TrackingObject(seen, prompt_tokens=11, completion_tokens=22)
    response = TrackingObject(seen, usage=usage, model="gpt-4o", choices=[{"message": {"content": ANSWER}}])
    kwargs = Tracking(
        {
            "model": "gpt-4o",
            "call_type": "acompletion",
            "messages": messages(),
            "input": messages(),
            "original_response": ANSWER,
            "litellm_params": Tracking(
                {"custom_llm_provider": "openai", "metadata": Tracking({"note": PROMPT}, seen=seen)}, seen=seen
            ),
        },
        seen=seen,
    )
    ai_usage._on_litellm_event(kwargs, response, success=True)

    allowed = {
        "model",
        "call_type",
        "litellm_params",
        "custom_llm_provider",
        "metadata",
        "litellm_metadata",
        "hmcts_ai_fallback",
        "attempted_fallbacks",
        "usage",
        "prompt_tokens",
        "completion_tokens",
    }
    assert seen <= allowed, seen - allowed
    assert {"messages", "input", "original_response", "choices", "note"}.isdisjoint(seen)
    points = tokens(tel)
    assert (points["input"]["sum"], points["output"]["sum"]) == (11, 22)
    tel.assert_no_prohibited_content(PROMPT, ANSWER)


def test_callback_reads_input_and_output_tokens_from_dict_usage(tel: TelemetryCapture) -> None:
    ai_usage._on_litellm_event(
        {"model": "gpt-4.1", "call_type": "aresponses", "litellm_params": {"custom_llm_provider": "openai"}},
        {"model": "gpt-4.1", "usage": {"input_tokens": 3, "output_tokens": 4}},
        success=True,
    )
    points = tokens(tel)
    assert (points["input"]["sum"], points["output"]["sum"]) == (3, 4)
    assert points["input"]["attributes"][c.GEN_AI_OPERATION_NAME] == "chat"


def test_callback_never_raises(tel: TelemetryCapture) -> None:
    class Exploding:
        @property
        def usage(self) -> Any:
            raise RuntimeError("SENTINEL broken response")

    ai_usage._on_litellm_event({"model": "gpt-4o"}, Exploding(), success=True)
    ai_usage._on_litellm_event(None, None, success=False)
    ai_usage._on_litellm_event({"litellm_params": "not a dict", "model": ["x"]}, object(), success=True)
    kinds = {p["attributes"]["kind"] for p in tel.metric_points(c.METRIC_TELEMETRY_ERRORS)}
    assert "internal.litellm_callback" in kinds
    tel.assert_no_prohibited_content("SENTINEL")


def test_success_without_usage_is_counted_not_guessed(tel: TelemetryCapture) -> None:
    ai_usage._on_litellm_event(
        {"model": "gpt-4o", "call_type": "completion", "custom_llm_provider": "openai"},
        {"model": "gpt-4o"},
        success=True,
    )
    assert tokens(tel) == {}
    assert tel.metric_points(c.METRIC_AI_REQUESTS)[0]["value"] == 1
    kinds = {p["attributes"]["kind"] for p in tel.metric_points(c.METRIC_TELEMETRY_ERRORS)}
    assert "invalid.ai_usage.tokens" in kinds
