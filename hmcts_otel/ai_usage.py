"""AI usage metering: token counts and audio seconds, as metrics.

What is recorded, and nothing else:

* ``gen_ai.client.token.usage`` (histogram, ``{token}``): one point per token type
  (``input``, ``output``) per call, with the GenAI semantic-convention attributes
  (operation, provider, request and response model, token type) and
  ``hmcts.ai.feature``, ``hmcts.outcome`` and ``hmcts.ai.fallback``.
* ``hmcts.ai.requests`` (counter, ``{request}``): one per call, with the same
  attributes (no token type) and ``error.type`` (an exception class name) on failures.
* ``hmcts.speech.audio.duration`` (counter, ``s``): audio seconds transcribed, by
  ``hmcts.speech.mode``, feature and outcome.

No prices and no cost: cost comes from Azure and GCP billing, allocated by usage
share at query time. No prompt or response content is ever read or recorded.

Every value is validated. Providers and models must match
``[A-Za-z0-9._:/@-]{1,128}`` (and no redaction rule), otherwise they are recorded as
``unknown``. Token counts and seconds must be non-negative numbers, otherwise that
measurement is skipped. Both cases are counted on ``hmcts.telemetry.errors``.
``record_llm_usage``, ``record_speech_usage`` and the litellm callback never raise,
even in strict mode, because their values often come from provider responses.
``ai_feature`` with an invalid name is code misuse and, like ``stage()``, raises
only in strict mode.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from hmcts_otel import _guard, _state
from hmcts_otel import conventions as c
from hmcts_otel.context import _restore, current_stage
from hmcts_otel.conventions import Outcome, SpeechMode, TokenType
from hmcts_otel.schema import AttributeSpec, default_schema

__all__ = [
    "ai_fallback",
    "ai_feature",
    "current_ai_feature",
    "instrument_litellm",
    "record_llm_usage",
    "record_speech_usage",
]

_log = logging.getLogger("hmcts_otel")

INVALID_FEATURE = "invalid_feature"

_IDENTIFIER = r"[A-Za-z0-9._:/@-]{1,128}"
_PROVIDER = AttributeSpec(c.GEN_AI_PROVIDER_NAME, pattern=_IDENTIFIER)
_MODEL = AttributeSpec(c.GEN_AI_REQUEST_MODEL, pattern=_IDENTIFIER)
_OPERATION = AttributeSpec(c.GEN_AI_OPERATION_NAME, pattern=r"[a-z][a-z0-9_]{0,63}")
_ERROR_TYPE = AttributeSpec(c.ERROR_TYPE, pattern=r"[A-Za-z_][A-Za-z0-9_.]{0,63}")
# The core schema's rule for hmcts.ai.feature (the stage-name rule).
_FEATURE = default_schema.attributes[c.AI_FEATURE]

# litellm provider IDs mapped to gen_ai.provider.name well-known values. Anything else
# that passes validation is recorded as given.
_PROVIDER_NAMES = {
    "azure": "azure.ai.openai",
    "azure_text": "azure.ai.openai",
    "azure_ai": "azure.ai.inference",
    "vertex_ai": "gcp.vertex_ai",
    "vertex_ai_beta": "gcp.vertex_ai",
    "gemini": "gcp.gemini",
    "bedrock": "aws.bedrock",
    "mistral": "mistral_ai",
    "xai": "x_ai",
}

_feature: ContextVar[str | None] = ContextVar("hmcts_otel_ai_feature", default=None)
_fallback: ContextVar[bool] = ContextVar("hmcts_otel_ai_fallback", default=False)


# Context ------------------------------------------------------------------------------


@contextmanager
def ai_feature(name: str) -> Iterator[str]:
    """Attribute AI usage inside the block to a product feature, e.g. ``summarise``.

    Names follow the stage-name rule (``[a-z][a-z0-9_.]{0,63}``). An invalid name
    raises ``ValueError`` in strict mode; otherwise the block runs with the feature
    ``invalid_feature`` and the misuse is counted. Works across ``await``.
    """
    checked, reason = _FEATURE.check(name, _state.current_redactor())
    if reason is None:
        feature = str(checked)
    else:
        _guard.invalid("ai_feature", f"invalid AI feature name: {reason}")
        feature = INVALID_FEATURE
    previous = _feature.get()
    token = _feature.set(feature)
    try:
        yield feature
    finally:
        _restore(_feature, token, previous)


@contextmanager
def ai_fallback() -> Iterator[None]:
    """Mark AI calls inside the block as fallbacks (e.g. the secondary model after
    the primary failed)."""
    previous = _fallback.get()
    token = _fallback.set(True)
    try:
        yield
    finally:
        _restore(_fallback, token, previous)


def current_ai_feature() -> str:
    """The feature AI usage is attributed to here: the innermost ``ai_feature``, else
    the innermost ``stage()`` name, else ``unknown``."""
    return _feature.get() or current_stage() or c.UNKNOWN


# Validation ---------------------------------------------------------------------------


def _identifier(spec: AttributeSpec, value: object, kind: str) -> str:
    checked, reason = spec.check(value, _state.current_redactor())
    if reason is not None:
        _guard.invalid_data(f"ai_usage.{kind}")
        return c.UNKNOWN
    return str(checked)


def _provider(value: object) -> str:
    name = _identifier(_PROVIDER, value, "provider")
    return _PROVIDER_NAMES.get(name, name)


def _feature_name(value: str | None) -> str:
    if value is None:
        return current_ai_feature()
    checked, reason = _FEATURE.check(value, _state.current_redactor())
    if reason is not None:
        _guard.invalid_data("ai_usage.feature")
        return INVALID_FEATURE
    return str(checked)


def _enum_value(enum: type[Outcome] | type[SpeechMode], value: object, kind: str) -> str:
    try:
        return enum(value).value  # type: ignore[arg-type]
    except (ValueError, TypeError):
        _guard.invalid_data(f"ai_usage.{kind}")
        return c.UNKNOWN


def _token_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


# Instruments --------------------------------------------------------------------------


def _token_histogram() -> Any:
    return _state.instrument(
        "histogram",
        c.METRIC_GEN_AI_TOKEN_USAGE,
        unit="{token}",
        description="Number of input and output tokens used.",
    )


def _requests_counter() -> Any:
    return _state.instrument("counter", c.METRIC_AI_REQUESTS, unit="{request}", description="AI model calls")


def _audio_counter() -> Any:
    return _state.instrument(
        "counter", c.METRIC_SPEECH_AUDIO_DURATION, unit="s", description="Audio transcribed by speech services"
    )


# Public API ---------------------------------------------------------------------------


def record_llm_usage(
    *,
    provider: str,
    request_model: str,
    response_model: str | None = None,
    input_tokens: int,
    output_tokens: int,
    feature: str | None = None,
    outcome: Outcome | str = Outcome.SUCCESS,
    fallback: bool = False,
    operation: str = "chat",
    error_type: str | None = None,
) -> None:
    """Record the token usage of one model call. Never raises.

    ``feature`` defaults to the current ``ai_feature`` / stage. ``fallback`` is also
    true inside ``ai_fallback()``. ``error_type`` is an exception class name, never a
    message. Pass ``0`` tokens for a failed call that consumed none.
    """
    try:
        _record_llm(
            provider=provider,
            request_model=request_model,
            response_model=response_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            feature=feature,
            outcome=outcome,
            fallback=fallback,
            operation=operation,
            error_type=error_type,
        )
    except Exception as exc:
        _guard.internal_error("record_llm_usage", exc)


def _record_llm(
    *,
    provider: object,
    request_model: object,
    response_model: object,
    input_tokens: object,
    output_tokens: object,
    feature: str | None,
    outcome: object,
    fallback: object,
    operation: object,
    error_type: object,
) -> None:
    if not isinstance(fallback, bool):
        _guard.invalid_data("ai_usage.fallback")
        fallback = False
    attrs: dict[str, str | bool] = {
        c.GEN_AI_OPERATION_NAME: _identifier(_OPERATION, operation, "operation"),
        c.GEN_AI_PROVIDER_NAME: _provider(provider),
        c.GEN_AI_REQUEST_MODEL: _identifier(_MODEL, request_model, "model"),
        c.AI_FEATURE: _feature_name(feature),
        c.OUTCOME: _enum_value(Outcome, outcome, "outcome"),
        c.AI_FALLBACK: fallback or _fallback.get(),
    }
    if response_model is not None:
        attrs[c.GEN_AI_RESPONSE_MODEL] = _identifier(_MODEL, response_model, "model")

    histogram = _token_histogram()
    for token_type, value in ((TokenType.INPUT, input_tokens), (TokenType.OUTPUT, output_tokens)):
        count = _token_count(value)
        if count is None:
            _guard.invalid_data("ai_usage.tokens")
            continue
        histogram.record(count, {**attrs, c.GEN_AI_TOKEN_TYPE: token_type.value})

    request_attrs = dict(attrs)
    if error_type is not None:
        request_attrs[c.ERROR_TYPE] = _identifier(_ERROR_TYPE, error_type, "error_type")
    _requests_counter().add(1, request_attrs)


def record_speech_usage(
    *,
    seconds: float,
    mode: SpeechMode | str,
    feature: str | None = None,
    outcome: Outcome | str = Outcome.SUCCESS,
) -> None:
    """Record seconds of audio sent for transcription. Never raises.

    ``mode`` is ``realtime``, ``fast`` or ``batch``. ``feature`` defaults to the
    current ``ai_feature`` / stage.
    """
    try:
        amount = _seconds(seconds)
        if amount is None:
            _guard.invalid_data("ai_usage.seconds")
            return
        _audio_counter().add(
            amount,
            {
                c.SPEECH_MODE: _enum_value(SpeechMode, mode, "speech_mode"),
                c.AI_FEATURE: _feature_name(feature),
                c.OUTCOME: _enum_value(Outcome, outcome, "outcome"),
            },
        )
    except Exception as exc:
        _guard.internal_error("record_speech_usage", exc)


# litellm ------------------------------------------------------------------------------

# litellm call types mapped to gen_ai.operation.name.
_LITELLM_OPERATIONS = {
    "completion": "chat",
    "acompletion": "chat",
    "responses": "chat",
    "aresponses": "chat",
    "embedding": "embeddings",
    "aembedding": "embeddings",
    "text_completion": "text_completion",
    "atext_completion": "text_completion",
    "generate_content": "generate_content",
    "agenerate_content": "generate_content",
}
_FALLBACK_KEY = "hmcts_ai_fallback"
_MARKER = "hmcts_otel_usage"
_install_lock = threading.Lock()


def _get(obj: object, name: str) -> object:
    """``obj.name`` or ``obj[name]``, else None. Reads only the named field."""
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage(response_obj: object) -> tuple[object, object] | None:
    """Input and output token counts from a litellm response's ``usage``, or None."""
    usage = _get(response_obj, "usage")
    if usage is None:
        return None
    prompt = _get(usage, "prompt_tokens")
    if prompt is None:
        prompt = _get(usage, "input_tokens")
    completion = _get(usage, "completion_tokens")
    if completion is None:
        completion = _get(usage, "output_tokens")
    if prompt is None and completion is None:
        return None
    return (0 if prompt is None else prompt, 0 if completion is None else completion)


def _litellm_fallback(litellm_params: Mapping[str, Any]) -> bool:
    """True if the call's metadata marks it as a fallback: ``hmcts_ai_fallback=True``
    passed by the caller, or a litellm Router fallback (``attempted_fallbacks`` > 0)."""
    for key in ("metadata", "litellm_metadata"):
        metadata = litellm_params.get(key)
        if not isinstance(metadata, Mapping):
            continue
        if metadata.get(_FALLBACK_KEY) is True:
            return True
        attempted = metadata.get("attempted_fallbacks")
        if isinstance(attempted, int) and not isinstance(attempted, bool) and attempted > 0:
            return True
    return False


def _on_litellm_event(kwargs: object, response_obj: object, *, success: bool) -> None:
    """Record one litellm call. Reads only provider, model, call type, metadata flags,
    the exception's class, and the response's ``usage`` and ``model``: never
    ``messages``, ``input`` or the response's ``choices``. Never raises."""
    try:
        if not isinstance(kwargs, Mapping):
            return
        params = kwargs.get("litellm_params")
        params = params if isinstance(params, Mapping) else {}
        provider = params.get("custom_llm_provider") or kwargs.get("custom_llm_provider")
        call_type = kwargs.get("call_type")
        operation = _LITELLM_OPERATIONS.get(call_type, c.UNKNOWN) if isinstance(call_type, str) else c.UNKNOWN
        usage = _usage(response_obj)
        error_type = None
        if success:
            tokens: tuple[object, object] = usage if usage is not None else (None, None)
        else:
            tokens = usage if usage is not None else (0, 0)
            exception = kwargs.get("exception")
            error_type = type(exception).__name__ if isinstance(exception, BaseException) else c.UNKNOWN
        response_model = _get(response_obj, "model") if response_obj is not None else None
        _record_llm(
            provider=provider,
            request_model=kwargs.get("model"),
            response_model=response_model if isinstance(response_model, str) else None,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            feature=None,
            outcome=Outcome.SUCCESS if success else Outcome.ERROR,
            fallback=_litellm_fallback(params),
            operation=operation,
            error_type=error_type,
        )
    except Exception as exc:
        _guard.internal_error("litellm_callback", exc)


def _make_logger(base: type) -> Any:
    class HmctsUsageLogger(base):  # type: ignore[valid-type, misc]
        """litellm callback recording token usage (hmcts-otel)."""

        hmcts_otel_usage = True

        def log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
            _on_litellm_event(kwargs, response_obj, success=True)

        def log_failure_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
            _on_litellm_event(kwargs, response_obj, success=False)

        async def async_log_success_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
            _on_litellm_event(kwargs, response_obj, success=True)

        async def async_log_failure_event(self, kwargs: Any, response_obj: Any, start_time: Any, end_time: Any) -> None:
            _on_litellm_event(kwargs, response_obj, success=False)

    return HmctsUsageLogger()


def _is_ours(callback: object) -> bool:
    try:
        return getattr(callback, _MARKER, False) is True
    except Exception:
        return False


def instrument_litellm() -> bool:
    """Record token usage of every litellm call. Requires ``litellm`` (not a dependency
    of hmcts-otel). Idempotent; existing callbacks (e.g. Langfuse) are kept.

    Returns True if the callback is registered. Never raises.
    """
    try:
        import litellm
        from litellm.integrations.custom_logger import CustomLogger
    except ImportError:
        _log.warning("hmcts_otel: instrument_litellm() needs litellm, which is not installed")
        return False
    except Exception as exc:
        _guard.internal_error("instrument_litellm", exc)
        return False
    try:
        with _install_lock:
            if any(_is_ours(cb) for cb in litellm.callbacks):
                return True
            callback = _make_logger(CustomLogger)
            manager = getattr(litellm, "logging_callback_manager", None)
            add = getattr(manager, "add_litellm_callback", None)
            if callable(add):
                add(callback)  # appends, without duplicates, within litellm's limit
            else:  # pragma: no cover - older litellm
                litellm.callbacks.append(callback)
            if not any(cb is callback for cb in litellm.callbacks):
                _guard.internal_error("instrument_litellm_rejected")
                return False
            return True
    except Exception as exc:
        _guard.internal_error("instrument_litellm", exc)
        return False
