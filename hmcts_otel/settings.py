"""Configuration, read from environment variables.

Standard variable names (``OTEL_SERVICE_NAME``, ``APPLICATIONINSIGHTS_CONNECTION_STRING``,
``SENTRY_DSN``) are honoured so existing deployments keep working. Package-specific
settings use the ``HMCTS_OTEL_`` prefix.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env(*names: str) -> AliasChoices:
    return AliasChoices(*names)


_TRUE = frozenset({"1", "true", "yes", "on"})


def _strict_requested(data: dict[str, Any]) -> bool:
    for key, value in data.items():
        if str(key).lower() in ("strict", "hmcts_otel_strict"):
            return value is True or str(value).strip().lower() in _TRUE
    return False


class TelemetrySettings(BaseSettings):
    """Telemetry configuration.

    Never fails on a bad value: a field whose value (from the environment or a
    keyword argument) is invalid falls back to its default, and its name is
    recorded in ``invalid_fields`` for ``setup_telemetry`` to report. In strict mode
    (``strict=True`` / ``HMCTS_OTEL_STRICT=true``) invalid values raise.
    """

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)
    _invalid_fields: list[str] = PrivateAttr(default_factory=list)

    service_name: str = Field(
        default="unknown_service",
        validation_alias=_env("service_name", "OTEL_SERVICE_NAME", "HMCTS_OTEL_SERVICE_NAME"),
        description="Logical service name, one per workload (e.g. transcribe-api, transcribe-api-worker).",
    )
    service_version: str | None = Field(
        default=None,
        validation_alias=_env("service_version", "HMCTS_OTEL_SERVICE_VERSION", "APP_VERSION"),
    )
    environment: str = Field(
        default="local",
        validation_alias=_env("environment", "HMCTS_OTEL_ENVIRONMENT", "DEPLOYMENT_ENVIRONMENT"),
    )
    connection_string: SecretStr | None = Field(
        default=None,
        validation_alias=_env("connection_string", "APPLICATIONINSIGHTS_CONNECTION_STRING"),
        description="Application Insights connection string. Unset = no export to Azure.",
    )
    use_entra_auth: bool = Field(
        default=False,
        validation_alias=_env("use_entra_auth", "HMCTS_OTEL_USE_ENTRA_AUTH"),
        description="Authenticate ingestion with DefaultAzureCredential (requires the [entra] extra).",
    )
    sampling_ratio: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        validation_alias=_env("sampling_ratio", "HMCTS_OTEL_SAMPLING_RATIO"),
        description="Trace sampling ratio. Journey telemetry should stay at 1.0; metrics are never sampled.",
    )
    log_level: str = Field(default="INFO", validation_alias=_env("log_level", "HMCTS_OTEL_LOG_LEVEL", "LOG_LEVEL"))
    log_format: Literal["json", "text"] = Field(
        default="json", validation_alias=_env("log_format", "HMCTS_OTEL_LOG_FORMAT")
    )
    excluded_urls: str = Field(
        default="/health,/healthz,/healthcheck,/livez,/readyz,/version",
        validation_alias=_env("excluded_urls", "HMCTS_OTEL_EXCLUDED_URLS"),
        description="Comma-separated URL patterns not traced (health probes and similar noise).",
    )
    capture_uvicorn_logs: bool = Field(
        default=True,
        validation_alias=_env("capture_uvicorn_logs", "HMCTS_OTEL_CAPTURE_UVICORN_LOGS"),
        description="Route uvicorn's own loggers through the package handlers. Access logs stay off.",
    )
    capture_exception_messages: bool = Field(
        default=False,
        validation_alias=_env("capture_exception_messages", "HMCTS_OTEL_CAPTURE_EXCEPTION_MESSAGES"),
        description="Include exception messages in exported telemetry. Off: messages can carry content.",
    )
    strict: bool = Field(
        default=False,
        validation_alias=_env("strict", "HMCTS_OTEL_STRICT"),
        description="Raise on telemetry misuse (invalid names, codes, IDs). On in tests; off in production.",
    )
    allow_foreign_tracer_provider: bool = Field(
        default=False,
        validation_alias=_env("allow_foreign_tracer_provider", "HMCTS_OTEL_ALLOW_FOREIGN_TRACER_PROVIDER"),
        description=(
            "Continue (with an error log) if another TracerProvider is already registered. "
            "Off: setup fails, because auto-instrumented spans would bypass scrubbing."
        ),
    )
    enable_live_metrics: bool = Field(
        default=False, validation_alias=_env("enable_live_metrics", "HMCTS_OTEL_ENABLE_LIVE_METRICS")
    )

    sentry_dsn: SecretStr | None = Field(default=None, validation_alias=_env("sentry_dsn", "SENTRY_DSN"))
    sentry_environment: str | None = Field(
        default=None, validation_alias=_env("sentry_environment", "SENTRY_ENVIRONMENT")
    )
    sentry_traces_sample_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        validation_alias=_env("sentry_traces_sample_rate", "HMCTS_OTEL_SENTRY_TRACES_SAMPLE_RATE"),
        description="Sentry is for error triage; tracing lives in OpenTelemetry. Keep at 0 unless needed.",
    )

    @model_validator(mode="wrap")
    @classmethod
    def _lenient(cls, data: Any, handler: ValidatorFunctionWrapHandler) -> TelemetrySettings:
        try:
            return handler(data)
        except ValidationError as exc:
            if not isinstance(data, dict) or _strict_requested(data):
                raise
            bad_keys = {err["loc"][0] for err in exc.errors() if err.get("loc")}
            model = handler({k: v for k, v in data.items() if k not in bad_keys})
            model._invalid_fields = sorted({_field_for(str(k)) for k in bad_keys})
            return model

    @property
    def invalid_fields(self) -> list[str]:
        """Names (never values) of fields whose configured value was invalid."""
        return list(self._invalid_fields)

    @field_validator("log_format", mode="before")
    @classmethod
    def _lenient_log_format(cls, value: object) -> str:
        text = str(value).strip().lower() if value is not None else "json"
        return text if text in ("json", "text") else "json"

    @property
    def exports_to_azure(self) -> bool:
        return self.connection_string is not None and bool(self.connection_string.get_secret_value())


def _field_for(key: str) -> str:
    """Map an alias (e.g. ``HMCTS_OTEL_SAMPLING_RATIO``) back to its field name."""
    lowered = key.lower()
    for name, field in TelemetrySettings.model_fields.items():
        if lowered == name:
            return name
        choices = getattr(field.validation_alias, "choices", ()) or ()
        if any(isinstance(choice, str) and choice.lower() == lowered for choice in choices):
            return name
    return key


def load_settings() -> tuple[TelemetrySettings, list[str]]:
    """Read settings from the environment; returns them with the names of invalid fields."""
    settings = TelemetrySettings()
    return settings, settings.invalid_fields
