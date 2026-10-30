"""Classify provider exceptions so the fallback chain knows how to react.

The classifier is deliberately duck-typed: it looks at HTTP-ish status codes,
exception class names and message text rather than importing every provider
SDK, so it keeps working when a provider is not installed or changes its
exception hierarchy.
"""

from __future__ import annotations

import re
from enum import Enum


class ErrorKind(Enum):
    """What the fallback chain should do after an exception."""

    # Rate limit / overload / timeout / 5xx: retry this model (if allowed), then move on.
    TRANSIENT = "transient"
    # The model id is unknown, deprecated or not enabled: skip it for a long time.
    MODEL_UNAVAILABLE = "model_unavailable"
    # Bad or missing credentials: every model of this provider will fail, skip the provider.
    AUTH = "auth"
    # Bad request (invalid tool schema, malformed input, ...): fail fast, do not fall back.
    FATAL = "fatal"


_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_AUTH_STATUS = {401, 403}
_NOT_FOUND_STATUS = {404}

_TRANSIENT_PATTERNS = re.compile(
    r"rate.?limit|too many requests|quota|resource.?exhausted|overloaded|"
    r"unavailable|deadline|timed?.?out|timeout|connection|temporarily|"
    r"internal (server )?error|server error|try again|capacity|503|502|429",
    re.IGNORECASE,
)
_AUTH_PATTERNS = re.compile(
    r"api.?key|unauthori[sz]ed|unauthenticated|permission.?denied|forbidden|"
    r"invalid.?credential|authentication",
    re.IGNORECASE,
)
_MODEL_UNAVAILABLE_PATTERNS = re.compile(
    r"(model|models/)[^\n]{0,80}(not.?found|not.?exist|not.?supported|deprecated|"
    r"decommissioned|unknown|invalid|no longer|retired)|"
    r"(not.?found|unknown|deprecated|decommissioned|unsupported)[^\n]{0,40}model|"
    r"model_not_found|model_decommissioned",
    re.IGNORECASE,
)
_TRANSIENT_CLASS_NAMES = re.compile(
    r"RateLimit|ResourceExhausted|ServiceUnavailable|InternalServerError|"
    r"DeadlineExceeded|Timeout|APIConnection|Connect|Overloaded|TooManyRequests|"
    r"ServerError|Unavailable|Aborted|RemoteProtocol|ReadError|WriteError",
)
_AUTH_CLASS_NAMES = re.compile(r"Authentication|PermissionDenied|Unauthenticated|Unauthorized|Forbidden")
_NOT_FOUND_CLASS_NAMES = re.compile(r"NotFound")
_FATAL_CLASS_NAMES = re.compile(r"BadRequest|InvalidArgument|UnprocessableEntity|ValidationError")


def extract_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across provider SDKs."""
    for attr in ("status_code", "code", "http_status", "status"):
        value = getattr(exc, attr, None)
        code = _as_http_code(value)
        if code is not None:
            return code
    response = getattr(exc, "response", None)
    if response is not None:
        code = _as_http_code(getattr(response, "status_code", None))
        if code is not None:
            return code
    # google.api_core exceptions expose grpc_status_code; map a few common ones.
    grpc = getattr(exc, "grpc_status_code", None)
    if grpc is not None:
        name = getattr(grpc, "name", str(grpc)).upper()
        return {
            "RESOURCE_EXHAUSTED": 429,
            "UNAVAILABLE": 503,
            "DEADLINE_EXCEEDED": 504,
            "INTERNAL": 500,
            "UNAUTHENTICATED": 401,
            "PERMISSION_DENIED": 403,
            "NOT_FOUND": 404,
            "INVALID_ARGUMENT": 400,
        }.get(name)
    return None


def _as_http_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    if isinstance(value, str) and value.isdigit():
        code = int(value)
        if 100 <= code <= 599:
            return code
    return None


def classify_error(exc: BaseException) -> ErrorKind:
    """Map an exception raised by a chat model call to an :class:`ErrorKind`."""
    # Walk the chain so wrapped exceptions (e.g. langchain re-raises) still classify.
    seen: set[int] = set()
    current: BaseException | None = exc
    text_parts: list[str] = []
    class_names: list[str] = []
    status: int | None = None
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        class_names.append(type(current).__name__)
        text_parts.append(str(current))
        if status is None:
            status = extract_status_code(current)
        current = current.__cause__ or current.__context__

    message = " | ".join(text_parts)
    names = " ".join(class_names)

    if status is not None:
        if status in _AUTH_STATUS:
            return ErrorKind.AUTH
        if status in _NOT_FOUND_STATUS:
            return ErrorKind.MODEL_UNAVAILABLE
        if status in _TRANSIENT_STATUS:
            return ErrorKind.TRANSIENT
        if status == 400:
            if _AUTH_PATTERNS.search(message):
                return ErrorKind.AUTH
            if _MODEL_UNAVAILABLE_PATTERNS.search(message):
                return ErrorKind.MODEL_UNAVAILABLE
            return ErrorKind.FATAL
        if 400 <= status < 500:
            return ErrorKind.FATAL
        return ErrorKind.TRANSIENT

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return ErrorKind.TRANSIENT
    if _AUTH_CLASS_NAMES.search(names) or _AUTH_PATTERNS.search(message):
        return ErrorKind.AUTH
    if _NOT_FOUND_CLASS_NAMES.search(names) or _MODEL_UNAVAILABLE_PATTERNS.search(message):
        return ErrorKind.MODEL_UNAVAILABLE
    if _TRANSIENT_CLASS_NAMES.search(names) or _TRANSIENT_PATTERNS.search(message):
        return ErrorKind.TRANSIENT
    if _FATAL_CLASS_NAMES.search(names):
        return ErrorKind.FATAL
    # Unknown errors: trying the next model is cheap and the final
    # AllModelsFailedError still carries every underlying exception.
    return ErrorKind.TRANSIENT


class AllModelsFailedError(RuntimeError):
    """Raised when every candidate model in the chain failed."""

    def __init__(self, failures: list[tuple[str, BaseException]]):
        self.failures = failures
        lines = [f"All {len(failures)} LLM attempt(s) failed:"]
        for name, exc in failures:
            lines.append(f"  - {name}: {type(exc).__name__}: {str(exc)[:300]}")
        super().__init__("\n".join(lines))

    @property
    def last_exception(self) -> BaseException | None:
        return self.failures[-1][1] if self.failures else None
