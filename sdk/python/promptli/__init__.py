"""Promptli Comms Python SDK: email, SMS and phone calls for AI agents."""

__version__ = "0.2.0"

from promptli.auth import (
    OAuthCredential,
    OAuthToken,
    StaticToken,
    TokenSource,
    browser_login,
    credential_auth,
    save_credential,
)
from promptli.client import AsyncPromptli, Promptli
from promptli.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    PromptliError,
    ProviderError,
    RateLimitError,
)
from promptli.identity import AsyncIdentity
from promptli.link import AgentLink
from promptli.webhooks import parse_event, verify_webhook

__all__ = [
    "AgentLink",
    "AsyncIdentity",
    "AsyncPromptli",
    "AuthenticationError",
    "ConflictError",
    "InvalidRequestError",
    "NotFoundError",
    "OAuthCredential",
    "OAuthToken",
    "PermissionDeniedError",
    "Promptli",
    "PromptliError",
    "ProviderError",
    "RateLimitError",
    "StaticToken",
    "TokenSource",
    "browser_login",
    "credential_auth",
    "parse_event",
    "save_credential",
    "verify_webhook",
]
