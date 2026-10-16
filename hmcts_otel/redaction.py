"""Pattern-based redaction applied to every string that leaves the process.

This is a safety net, not the primary control. The primary control is that
custom attributes must be registered in the schema (see ``schema.py``).
Applications add rules for their own identifiers, e.g. case reference formats.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from hmcts_otel.conventions import REDACTED

Replacement = str | Callable[[re.Match[str]], str]


@dataclass(frozen=True)
class RedactionRule:
    name: str
    pattern: re.Pattern[str]
    replacement: Replacement = REDACTED


def _keep_key(match: re.Match[str]) -> str:
    # group 1: key, group 2: separator as written (e.g. '=', '="', '": "'), group 3: value
    return f"{match.group(1)}{match.group(2)}{REDACTED}"


def _keep_url(match: re.Match[str]) -> str:
    return f"{match.group(1)}?{REDACTED}"


# Every quantifier that can be retried from many start positions is bounded, so
# each rule runs in linear time on adversarial input (long runs of letters, dots,
# slashes, "http://"). See tests/test_redaction.py::test_rules_are_linear_time.
DEFAULT_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "email",
        # Local part: the RFC 5322 dot-atom set (o'brien@..., foo!bar@...), bounded.
        re.compile(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}"
        ),
    ),
    RedactionRule("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{1,8192}\.[A-Za-z0-9_-]{1,8192}\.[A-Za-z0-9_-]{1,8192}")),
    # Tokens are long; requiring 16+ characters avoids "the bearer of the warrant".
    RedactionRule("bearer", re.compile(r"(?i)\bbearer\s{1,8}[A-Za-z0-9._~+/-]{16,4096}=*")),
    RedactionRule(
        "secret_key_value",
        re.compile(
            r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|sig|password|pwd|secret|client_secret"
            r"|api[_-]?key|access_token|refresh_token)"
            # key=value, key="value", key='value', "key": "value", key: value
            r"(\"?\s{0,3}[=:]\s{0,3}[\"']?)(?!\[REDACTED\])([^;&\s\"',}]{1,4096})"
        ),
        _keep_key,
    ),
    RedactionRule("url_userinfo", re.compile(r"(?i)(?<=://)[^/\s:@]{1,256}:[^/\s@]{1,256}@")),
    # Query strings of absolute URLs (search terms, tokens, identifiers), e.g. in
    # httpx's "HTTP Request: GET https://...?..." INFO logs.
    RedactionRule(
        "url_query",
        re.compile(r"(?i)\b(https?://[^\s?#\"'<>]{1,2048})\?(?!\[REDACTED\])[^\s#\"'<>]+"),
        _keep_url,
    ),
    # Query strings of relative paths, e.g. access-log request lines "GET /search?q=... HTTP/1.1".
    # The lookbehind excludes every character of the path class, so a run of path
    # characters has a single start position.
    RedactionRule(
        "path_query",
        re.compile(r"(?<![\w:/.?=&%~-])(/[\w\-./%~]{0,2048})\?(?!\[REDACTED\])[^\s#\"'<>]+"),
        _keep_url,
    ),
)

# Text longer than this is truncated after redaction. App Insights caps messages at
# 32,768 characters anyway, and the cap bounds the cost of every rule (including
# rules applications add). Redaction runs on a slightly longer prefix so a secret
# straddling the cut is still matched in full.
MAX_TEXT_LENGTH = 32_768
_REDACTION_MARGIN = 8_192 + 512
TRUNCATED = "...[truncated]"


class Redactor:
    """Applies redaction rules to text. Thread-safe for concurrent use and rule additions."""

    def __init__(self, rules: Iterable[RedactionRule] = DEFAULT_RULES) -> None:
        self._rules: tuple[RedactionRule, ...] = tuple(rules)
        self._lock = threading.Lock()

    @property
    def rules(self) -> tuple[RedactionRule, ...]:
        return self._rules

    def add_rule(self, name: str, pattern: str | re.Pattern[str], replacement: Replacement = REDACTED) -> None:
        compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
        with self._lock:
            self._rules = (*self._rules, RedactionRule(name, compiled, replacement))

    def redact(self, text: str) -> str:
        truncated = len(text) > MAX_TEXT_LENGTH
        if truncated:
            text = text[: MAX_TEXT_LENGTH + _REDACTION_MARGIN]
        for rule in self._rules:
            text = rule.pattern.sub(rule.replacement, text)
        if truncated:
            text = text[:MAX_TEXT_LENGTH] + TRUNCATED
        return text

    def matches(self, text: str) -> list[str]:
        """Names of the rules that match ``text``. Never returns the matched values."""
        text = text[: MAX_TEXT_LENGTH + _REDACTION_MARGIN]
        return [rule.name for rule in self._rules if rule.pattern.search(text)]


default_redactor = Redactor()


def redact(text: str) -> str:
    return default_redactor.redact(text)
