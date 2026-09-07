"""Swiggy MCP failure classification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Bucket(Enum):
    AUTH = "auth"
    BAD_INPUT = "bad_input"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_ERROR = "upstream_error"
    DOMAIN = "domain"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


RETRYABLE = frozenset({Bucket.UPSTREAM_TIMEOUT, Bucket.UPSTREAM_ERROR, Bucket.INTERNAL})


class SwiggyError(Exception):
    def __init__(self, message: str, bucket: Bucket, *, report_link: str | None = None):
        super().__init__(message)
        self.message = message
        self.bucket = bucket
        self.report_link = report_link

    @property
    def retryable(self) -> bool:
        return self.bucket in RETRYABLE


class SwiggyAuthError(SwiggyError):
    def __init__(self, message: str, *, report_link: str | None = None):
        super().__init__(message, Bucket.AUTH, report_link=report_link)


@dataclass(frozen=True)
class _Rule:
    bucket: Bucket
    pattern: re.Pattern[str]


_MESSAGE_RULES: tuple[_Rule, ...] = (
    _Rule(
        Bucket.AUTH,
        re.compile(
            r"unauthenticat|unauthoriz|token.*expir|session.*(expir|revok)", re.I
        ),
    ),
    _Rule(Bucket.BAD_INPUT, re.compile(r"^\s*(invalid|missing)\b", re.I)),
    _Rule(Bucket.UPSTREAM_TIMEOUT, re.compile(r"timed?.?out|timeout", re.I)),
)

_JSONRPC_BUCKETS = {-32001: Bucket.AUTH, -32603: Bucket.INTERNAL}
_STATUS_BUCKETS = {
    400: Bucket.BAD_INPUT,
    401: Bucket.AUTH,
    403: Bucket.AUTH,
    419: Bucket.AUTH,
    500: Bucket.INTERNAL,
    502: Bucket.UPSTREAM_ERROR,
    503: Bucket.UPSTREAM_ERROR,
    504: Bucket.UPSTREAM_TIMEOUT,
}


def classify(
    message: str,
    *,
    status: int | None = None,
    jsonrpc_code: int | None = None,
    from_envelope: bool = False,
) -> Bucket:
    if jsonrpc_code is not None and jsonrpc_code in _JSONRPC_BUCKETS:
        return _JSONRPC_BUCKETS[jsonrpc_code]
    for rule in _MESSAGE_RULES:
        if rule.pattern.search(message):
            return rule.bucket
    if status is not None and status in _STATUS_BUCKETS:
        return _STATUS_BUCKETS[status]
    if from_envelope:
        return Bucket.DOMAIN
    return Bucket.UNKNOWN


def error_from_envelope(envelope: dict) -> SwiggyError:
    error = envelope.get("error") or {}
    message = error.get("message") or envelope.get("message") or "Swiggy call failed"
    bucket = classify(message, from_envelope=True)
    report_link = error.get("reportLink")
    if bucket is Bucket.AUTH:
        return SwiggyAuthError(message, report_link=report_link)
    return SwiggyError(message, bucket, report_link=report_link)
