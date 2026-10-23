"""The telemetry schema: which attributes and events a service may emit.

Every custom attribute passed to ``stage()``, ``record_event()`` or the browser
relay must be registered here. Unregistered keys, wrong types, over-long strings
and values that trip a redaction rule are dropped, never truncated, and counted.
The schema doubles as the documented field list (``to_markdown()``).
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from hmcts_otel import conventions as c
from hmcts_otel.conventions import EventClass, EventSource, Outcome, SpeechMode
from hmcts_otel.redaction import Redactor, default_redactor

AttributeValue = str | bool | int | float

_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_.]{0,99}")

# Attributes travel as LogRecord extras on the events path; these names would make
# logging raise "Attempt to overwrite ... in LogRecord".
RESERVED_NAMES = frozenset(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None)).keys()) | {
    "message",
    "asctime",
    "taskName",
}
_ID_PATTERN = r"[A-Za-z0-9_-]{1,64}"
_CODE_PATTERN = r"[A-Za-z0-9_.:-]{1,64}"
_SLUG_PATTERN = r"[a-z0-9][a-z0-9_.-]{0,63}"
_STAGE_PATTERN = r"[a-z][a-z0-9_.]{0,63}"


@dataclass(frozen=True)
class AttributeSpec:
    name: str
    type: type = str
    description: str = ""
    max_length: int = 128
    allowed_values: frozenset[str] | None = None
    pattern: str | None = None
    _compiled: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.type not in (str, int, float, bool):
            raise ValueError(f"attribute {self.name!r}: type must be str, int, float or bool")
        if self.pattern is not None:
            object.__setattr__(self, "_compiled", re.compile(self.pattern))

    def check(self, value: object, redactor: Redactor) -> tuple[AttributeValue | None, str | None]:
        """Return ``(clean_value, None)`` or ``(None, reason)``."""
        if self.type is bool:
            return (value, None) if isinstance(value, bool) else (None, "type")
        if self.type is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
            return (value, None) if ok else (None, "type")  # type: ignore[return-value]
        if self.type is float:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return None, "type"
            return float(value), None
        if not isinstance(value, str):
            return None, "type"
        if len(value) > self.max_length:
            return None, "length"
        if self.allowed_values is not None and value not in self.allowed_values:
            return None, "value"
        if self._compiled is not None and not self._compiled.fullmatch(value):
            return None, "pattern"
        if redactor.matches(value):
            return None, "redaction"
        return value, None


@dataclass(frozen=True)
class EventSpec:
    name: str
    event_class: EventClass = EventClass.OPERATIONAL
    description: str = ""
    attributes: tuple[str, ...] = ()
    # Whether the browser relay accepts this event.
    browser: bool = False


_JOURNEY_KEYS = (c.JOURNEY_TYPE, c.JOURNEY_ID, c.JOURNEY_PARENT_ID)


def _core_attributes() -> list[AttributeSpec]:
    return [
        AttributeSpec(
            c.JOURNEY_TYPE, description="Kind of journey, e.g. upload.job or user.session.", pattern=_SLUG_PATTERN
        ),
        AttributeSpec(c.JOURNEY_ID, description="Pseudonymous journey identifier (UUID).", pattern=_ID_PATTERN),
        AttributeSpec(
            c.JOURNEY_PARENT_ID, description="Parent journey, e.g. the batch a file belongs to.", pattern=_ID_PATTERN
        ),
        AttributeSpec(c.STAGE, description="Stage name.", pattern=_STAGE_PATTERN),
        AttributeSpec(
            c.AI_FEATURE,
            description="Product feature an AI call served, e.g. summarise. Defaults to the current stage.",
            pattern=_STAGE_PATTERN,
        ),
        AttributeSpec(c.AI_FALLBACK, type=bool, description="Whether an AI call was a fallback to another model."),
        AttributeSpec(
            c.SPEECH_MODE,
            description="Speech transcription mode.",
            allowed_values=frozenset(m.value for m in SpeechMode),
        ),
        AttributeSpec(c.OUTCOME, description="Stage outcome.", allowed_values=frozenset(o.value for o in Outcome)),
        AttributeSpec(c.ERROR_CODE, description="Controlled error code. Never a message.", pattern=_CODE_PATTERN),
        AttributeSpec(
            c.EVENT_CLASS,
            description="operational or analytics.",
            allowed_values=frozenset(e.value for e in EventClass),
        ),
        AttributeSpec(
            c.EVENT_SOURCE, description="server or browser.", allowed_values=frozenset(s.value for s in EventSource)
        ),
        AttributeSpec(c.DURATION_MS, type=float, description="Duration in milliseconds."),
        AttributeSpec(
            c.DEPENDENCY_REF,
            description="Reference in an external system, e.g. Azure Speech job ID.",
            pattern=r"[A-Za-z0-9_.:-]{1,128}",
        ),
        AttributeSpec(
            c.REQUEST_ID,
            description="Request correlation ID (UUID or 32 hex).",
            pattern=r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32}",
        ),
    ]


class TelemetrySchema:
    def __init__(self, *, include_core: bool = True, redactor: Redactor | None = None) -> None:
        self._attributes: dict[str, AttributeSpec] = {}
        self._events: dict[str, EventSpec] = {}
        self._lock = threading.Lock()
        self.redactor = redactor or default_redactor
        self.core_keys: frozenset[str] = frozenset()
        if include_core:
            for spec in _core_attributes():
                self._attributes[spec.name] = spec
            self.core_keys = frozenset(self._attributes)

    # Registration -----------------------------------------------------------------

    def attribute(
        self,
        name: str,
        type: type = str,  # noqa: A002 - mirrors AttributeSpec
        *,
        description: str = "",
        max_length: int = 128,
        allowed_values: Iterable[str] | None = None,
        pattern: str | None = None,
    ) -> AttributeSpec:
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"attribute name {name!r} must be lower-case dotted snake case")
        if name in RESERVED_NAMES:
            raise ValueError(
                f"attribute name {name!r} is reserved by Python logging; use a dotted name such as 'file.{name}'"
            )
        spec = AttributeSpec(
            name,
            type,
            description,
            max_length,
            frozenset(allowed_values) if allowed_values is not None else None,
            pattern,
        )
        with self._lock:
            existing = self._attributes.get(name)
            if existing is not None and existing != spec:
                raise ValueError(f"attribute {name!r} is already registered with a different definition")
            self._attributes[name] = spec
        return spec

    def event(
        self,
        name: str,
        *,
        event_class: EventClass = EventClass.OPERATIONAL,
        description: str = "",
        attributes: Iterable[str] = (),
        browser: bool = False,
    ) -> EventSpec:
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"event name {name!r} must be lower-case dotted snake case")
        attrs = tuple(attributes)
        unknown = [a for a in attrs if a not in self._attributes]
        if unknown:
            raise ValueError(f"event {name!r} uses unregistered attributes: {unknown}")
        spec = EventSpec(name, EventClass(event_class), description, attrs, browser)
        with self._lock:
            existing = self._events.get(name)
            if existing is not None and existing != spec:
                raise ValueError(f"event {name!r} is already registered with a different definition")
            self._events[name] = spec
        return spec

    # Lookup -----------------------------------------------------------------------

    def get_attribute(self, name: str) -> AttributeSpec | None:
        return self._attributes.get(name)

    def get_event(self, name: str) -> EventSpec | None:
        return self._events.get(name)

    @property
    def attributes(self) -> Mapping[str, AttributeSpec]:
        return dict(self._attributes)

    @property
    def events(self) -> Mapping[str, EventSpec]:
        return dict(self._events)

    # Validation -------------------------------------------------------------------

    def validate(
        self, values: Mapping[str, object], *, allowed: Collection[str] | None = None
    ) -> tuple[dict[str, AttributeValue], list[tuple[str, str]]]:
        """Split ``values`` into accepted attributes and ``(key, reason)`` drops.

        ``allowed`` narrows the keys further (e.g. to an event's declared attributes).
        Journey keys and core keys are always allowed if registered.
        """
        clean: dict[str, AttributeValue] = {}
        dropped: list[tuple[str, str]] = []
        for key, value in values.items():
            spec = self._attributes.get(key)
            if spec is None:
                dropped.append((key, "unregistered"))
                continue
            if allowed is not None and key not in allowed and key not in self.core_keys:
                dropped.append((key, "not_allowed_for_event"))
                continue
            checked, reason = spec.check(value, self.redactor)
            if reason is not None:
                dropped.append((key, reason))
                continue
            assert checked is not None
            clean[key] = checked
        return clean, dropped

    def check_identifier(self, key: str, value: str) -> str:
        """Validate a core identifier and return it, or raise ``ValueError``.

        Used for values that come from code (journey IDs, stage names, error codes),
        where a bad value is a programming error.
        """
        spec = self._attributes[key]
        checked, reason = spec.check(value, self.redactor)
        if reason is not None:
            raise ValueError(f"invalid {key}: {reason}")
        return str(checked)

    # Documentation ----------------------------------------------------------------

    def to_markdown(self) -> str:
        lines = [
            "| Attribute | Type | Rule | Description |",
            "|---|---|---|---|",
        ]
        for spec in sorted(self._attributes.values(), key=lambda s: s.name):
            if spec.allowed_values is not None:
                rule = "one of " + ", ".join(f"`{v}`" for v in sorted(spec.allowed_values))
            elif spec.pattern is not None:
                rule = f"matches `{spec.pattern}`"
            elif spec.type is str:
                rule = f"max {spec.max_length} chars"
            else:
                rule = "—"
            lines.append(f"| `{spec.name}` | {spec.type.__name__} | {rule} | {spec.description} |")
        if self._events:
            lines += ["", "| Event | Class | Browser | Attributes | Description |", "|---|---|---|---|---|"]
            for ev in sorted(self._events.values(), key=lambda e: e.name):
                attrs = ", ".join(f"`{a}`" for a in ev.attributes) or "—"
                browser = "yes" if ev.browser else "no"
                lines.append(f"| `{ev.name}` | {ev.event_class.value} | {browser} | {attrs} | {ev.description} |")
        return "\n".join(lines) + "\n"


default_schema = TelemetrySchema()


def check_name(name: str) -> str:
    """Validate an event or attribute name (lower-case dotted snake case)."""
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise ValueError("name must be a lower-case dotted snake case string, max 100 chars")
    return name


JOURNEY_KEYS = _JOURNEY_KEYS
