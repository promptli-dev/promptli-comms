"""Typed errors mirroring the API's `{"error": {...}}` envelope (codes listed in spec/errors.json)."""

from __future__ import annotations

from typing import Any


class PromptliError(Exception):
    """Any error returned by the Promptli API (or raised by the SDK)."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "internal_error",
        status: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.message = message
        self.code = code
        self.status = status
        self.request_id = request_id
        self.retryable = retryable
        self.details = details or {}


class AuthenticationError(PromptliError):
    """401: missing, invalid, revoked or expired credential."""


class PermissionDeniedError(PromptliError):
    """403: valid credential without the needed scope (or a restricted provisional key)."""


class NotFoundError(PromptliError):
    """404: the resource does not exist or is not visible to this credential."""


class ConflictError(PromptliError):
    """409: conflicts with current state (stale draft generation, pending 10DLC, ...)."""


class InvalidRequestError(PromptliError):
    """400/413/422: validation or policy errors (recipient_opted_out, recipient_blocked, ...)."""


class RateLimitError(PromptliError):
    """429: retry after `retry_after` seconds."""

    retry_after: float | None = None


class ProviderError(PromptliError):
    """5xx: upstream provider or server problem (usually retryable)."""


class ConnectionError_(PromptliError):
    """Network failure talking to the API."""


def error_from_response(status: int, body: Any, retry_after: str | None = None) -> PromptliError:
    raw: Any = body.get("error", {}) if isinstance(body, dict) else {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    err: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
    kwargs: dict[str, Any] = {
        "code": str(err.get("code", "internal_error")),
        "status": status,
        "request_id": err.get("request_id"),
        "retryable": bool(err.get("retryable", status >= 500 or status == 429)),
        "details": err.get("details") if isinstance(err.get("details"), dict) else None,
    }
    message = str(err.get("message") or f"HTTP {status}")
    cls: type[PromptliError]
    if status == 401:
        cls = AuthenticationError
    elif status == 403:
        cls = PermissionDeniedError
    elif status == 404:
        cls = NotFoundError
    elif status == 409:
        cls = ConflictError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = ProviderError
    else:
        cls = InvalidRequestError
    exc = cls(message, **kwargs)
    if isinstance(exc, RateLimitError) and retry_after:
        try:
            exc.retry_after = float(retry_after)
        except ValueError:
            exc.retry_after = None
    return exc
