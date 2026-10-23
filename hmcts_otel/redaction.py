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
    # group 1: key, group 2: separator as written (e.g. '=', '": '), group 3: value,
    # including any quotes or braces around it, which are kept.
    value = match.group(3)
    opening = value[0] if value[0] in "\"'{" else ""
    closing = {'"': '"', "'": "'", "{": "}"}.get(opening, "")
    if not (len(value) > 1 and value.endswith(closing)):
        closing = ""
    return f"{match.group(1)}{match.group(2)}{opening}{REDACTED}{closing}"


def _keep_url(match: re.Match[str]) -> str:
    return f"{match.group(1)}?{REDACTED}"


# Every quantifier that can be retried from many start positions is bounded, so
# each rule runs in linear time on adversarial input (long runs of letters, dots,
# slashes, "http://"). See tests/test_redaction.py::test_rules_are_linear_time.
DEFAULT_RULES: tuple[RedactionRule, ...] = (
    # URL credentials first: the email rule would otherwise consume "password@host.domain"
    # and leave the username behind ("https://user:[REDACTED]/path").
    # The whole userinfo, any length, with or without a password (user@, user:@, user:pw@):
    # everything after "://" up to "@" that cannot end an authority. One start per "://",
    # and possessive, so linear-time. (An "@" past the inspected prefix: see Redactor.)
    RedactionRule("url_userinfo", re.compile(r"(?<=://)[^/\s?#@\[\]\"'<>]++@")),
    RedactionRule(
        "email",
        # Local part: the RFC 5322 dot-atom set (o'brien@..., foo!bar@...), bounded.
        re.compile(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}"
        ),
    ),
    # Tokens fail closed: a token of any length is consumed whole, never cut at a bound
    # with its remainder exported. Possessive quantifiers (Python 3.11+) never backtrack,
    # so each rule stays linear-time however long the token is.
    # JWT: "eyJ" (base64 of '{"') and its segments. With at least one dot, any length;
    # without a dot (a truncated or oversized token), 16+ characters.
    RedactionRule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]++(?:\.[A-Za-z0-9_-]*+){1,2}|\beyJ[A-Za-z0-9_-]{13,}+"),
    ),
    # Tokens are long; requiring 16+ characters avoids "the bearer of the warrant".
    RedactionRule("bearer", re.compile(r"(?i)\bbearer\s{1,8}[A-Za-z0-9._~+/-]{16,}+=*")),
    RedactionRule(
        "secret_key_value",
        re.compile(
            r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|sig|password|pwd|secret|client_secret"
            r"|api[_-]?key|access_token|refresh_token)"
            # key=value, key="value", key='value', "key": "value", key: value. Up to
            # three spaces may follow the separator; they are kept, and never given
            # back to the value (possessive), so redaction stays idempotent.
            r"([\"']?\s{0,3}[=:]\s{0,3}+)(?![\"'{]?\[REDACTED\])("
            # Each value has a bounded fast path and fails closed past it: a longer value
            # is redacted to its closing quote or brace if one follows on the same line,
            # otherwise to the end of the line (a connection-string value to ';'). The
            # overflow paths are reached only after a key and never fail, so each match
            # scans at most to the end of its line and redaction stays linear. The cost
            # is occasional over-redaction of the rest of a line.
            # Quoted: to the closing quote (backslash escapes allowed), or the end of
            # the line if unclosed.
            r"\"(?:[^\"\\\r\n]|\\.){1,4096}(?:\"|[^\"\\\r\n]*(?:\\.[^\"\\\r\n]*)*\"?)"
            r"|'(?:[^'\\\r\n]|\\.){1,4096}(?:'|[^'\\\r\n]*(?:\\.[^'\\\r\n]*)*'?)"
            # ODBC braced: Pwd={two words}
            r"|\{(?:[^}\r\n]{1,256}\}|[^}\r\n]{257,}\}?)"
            # Connection string, Password=two words;: after '=' (and any spaces), up to
            # ';' or the end of the line, if no other key=value intervenes.
            r"|(?:(?<==)|(?<==\s)|(?<==\s\s)|(?<==\s\s\s))"
            r"(?:[^;=\r\n\"'{}]{1,256}(?=;|\r|\n|\Z)|[^;=\r\n\"'{}]{257}[^;\r\n]*)"
            # Otherwise words up to the next delimiter (; & , } or a quote), the end of
            # the line, or the next key=value / key: value pair, so a value with spaces
            # (password: two words) is redacted whole and a key=value log line keeps its
            # other fields (password=x user=u1 keeps user=u1). Each word is consumed whole.
            r"|[^;&\s\"',}]+(?:[ \t]{1,3}(?![\w.-]{1,64}\s{0,3}[=:])[^;&\s\"',}]+)*)"
        ),
        _keep_key,
    ),
    # Query strings of absolute URLs (search terms, tokens, identifiers), e.g. in
    # httpx's "HTTP Request: GET https://...?..." INFO logs.
    RedactionRule(
        "url_query",
        # Any length, still linear: the URL part never crosses another "http(s)://", so in
        # "https://https://..." each character is scanned by one attempt only, and the
        # possessive quantifier never backtracks.
        re.compile(r"(?i)\b(https?://(?:(?!https?://)[^\s?#\"'<>])*+)\?(?!\[REDACTED\])[^\s#\"'<>]+"),
        _keep_url,
    ),
    # Query strings of relative paths, e.g. access-log request lines "GET /search?q=... HTTP/1.1".
    # The lookbehind excludes every character of the path class, so a run of path
    # characters has a single start position.
    RedactionRule(
        "path_query",
        re.compile(r"(?<![\w:/.?=&%~-])(/[\w\-./%~]*+)\?(?!\[REDACTED\])[^\s#\"'<>]+"),
        _keep_url,
    ),
)

# Text longer than this is truncated after redaction. App Insights caps messages at
# 32,768 characters anyway, and the cap bounds the cost of every rule (including
# rules applications add). Redaction runs on a slightly longer prefix so a secret
# straddling the cut is still matched in full.
MAX_TEXT_LENGTH = 32_768
# The margin covers the longest bounded match of a default rule that can start before
# the cut, so a secret straddling it is still matched in full. JWT and bearer tokens are
# unbounded and fail closed (a token running past the cut is redacted up to it); the
# margin is kept at its former JWT-derived size (24,579 + 2,048) as a safe upper bound
# for the remaining bounded rules.
_JWT_SEGMENT_MAX = 8_192
_LONGEST_DEFAULT_MATCH = len("eyJ") + 3 * _JWT_SEGMENT_MAX + 2
_REDACTION_MARGIN = _LONGEST_DEFAULT_MATCH + 2_048
TRUNCATED = "...[truncated]"


_AUTHORITY_END = re.compile(r"[/\s?#\[\]\"'<>]")


def _userinfo_across(text: str, cut: int) -> int | None:
    """Where a URL's userinfo starts if it runs past ``cut`` (its "@" is not inspected).

    The rules only see ``text[:cut]``, so ``https://user:pw...@host`` straddling the cut
    would keep its credentials. Returns the index just after "://" when the authority
    has neither ended nor reached its "@" before the cut, and an "@" follows after it.
    """
    start = text.rfind("://", 0, cut)
    if start < 0:
        return None
    start += 3
    if _AUTHORITY_END.search(text, start, cut) or text.find("@", start, cut) >= 0:
        return None
    end_match = _AUTHORITY_END.search(text, cut)
    end = end_match.start() if end_match else len(text)
    return start if text.find("@", cut, end) >= 0 else None


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
            cut = MAX_TEXT_LENGTH + _REDACTION_MARGIN
            across = _userinfo_across(text, cut)
            # Fail closed: credentials whose "@" lies past the cut are redacted to the cut.
            text = text[:across] + REDACTED if across is not None else text[:cut]
        for rule in self._rules:
            text = rule.pattern.sub(rule.replacement, text)
        if truncated:
            text = text[:MAX_TEXT_LENGTH] + TRUNCATED
        return text

    def matches(self, text: str) -> list[str]:
        """Names of the rules that match ``text``. Never returns the matched values."""
        cut = MAX_TEXT_LENGTH + _REDACTION_MARGIN
        found = [rule.name for rule in self._rules if rule.pattern.search(text[:cut])]
        if len(text) > cut and "url_userinfo" not in found and _userinfo_across(text, cut) is not None:
            found.append("url_userinfo")
        return found


default_redactor = Redactor()


def redact(text: str) -> str:
    return default_redactor.redact(text)
