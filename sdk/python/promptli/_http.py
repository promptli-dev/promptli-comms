"""HTTP transport: auth header, retries with backoff, Idempotency-Key, error mapping."""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any

import httpx

from promptli import __version__
from promptli.auth import StaticToken, TokenSource
from promptli.errors import ConnectionError_, PromptliError, RateLimitError, error_from_response

DEFAULT_BASE_URL = "https://api.comms.promptli.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class Transport:
    def __init__(
        self,
        *,
        api_key: str | TokenSource,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise PromptliError("No API key: pass api_key=… or set PROMPTLI_API_KEY.", code="invalid_api_key")
        # A plain key, or a token source (browser login) that refreshes short-lived tokens on its own.
        self.auth: TokenSource = StaticToken(api_key) if isinstance(api_key, str) else api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        ua = f"promptli-python/{__version__}" + (f" {user_agent}" if user_agent else "")
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        self._headers = {"User-Agent": ua, "Accept": "application/json"}

    @property
    def api_key(self) -> str:
        """The static key (only for API-key auth; browser logins have no long-lived key)."""
        if isinstance(self.auth, StaticToken):
            return self.auth.key
        raise PromptliError("This client uses a browser login, not an API key.", code="invalid_api_key")

    @property
    def link_url(self) -> str:
        # api.comms.promptli.com → link.comms.promptli.com; localhost:8000 → localhost:8100.
        if "://api." in self.base_url:
            return self.base_url.replace("://api.", "://link.").replace("http", "ws", 1) + "/v1/connect"
        if self.base_url.endswith(":8000"):
            return self.base_url.replace("http", "ws", 1)[: -len(":8000")] + ":8100/v1/connect"
        return self.base_url.replace("http", "ws", 1) + "/v1/connect"

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auto_idempotency: bool = True,
        follow_redirects: bool = False,
    ) -> Any:
        headers = dict(self._headers)
        # POSTs get an Idempotency-Key so automatic retries can never double-send.
        if method == "POST" and (idempotency_key or auto_idempotency):
            headers["Idempotency-Key"] = idempotency_key or f"sdk-{uuid.uuid4()}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        retryable_method = method in ("GET", "DELETE", "PUT") or "Idempotency-Key" in headers
        attempt = 0
        reauthed = False
        while True:
            headers["Authorization"] = f"Bearer {await self.auth.token()}"
            try:
                res = await self._client.request(
                    method,
                    self.base_url + path,
                    json=json,
                    params=clean_params,
                    headers=headers,
                    follow_redirects=follow_redirects,
                )
            except httpx.TransportError as err:
                if retryable_method and attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(self._backoff(attempt, None))
                    continue
                raise ConnectionError_(str(err), code="connection_error", retryable=True) from err
            if res.status_code < 400:
                if res.status_code == 204 or not res.content:
                    return None
                ctype = res.headers.get("content-type", "")
                return res.json() if "json" in ctype else res.content
            try:
                body: Any = res.json()
            except ValueError:
                body = {"error": {"message": res.text[:200]}}
            err = error_from_response(res.status_code, body, res.headers.get("retry-after"))
            if res.status_code == 401 and not reauthed and not isinstance(self.auth, StaticToken):
                # The access token was rotated or revoked under us: refresh once and retry.
                reauthed = True
                await self.auth.invalidate()
                continue
            if res.status_code in RETRY_STATUSES and retryable_method and attempt < self.max_retries:
                attempt += 1
                await asyncio.sleep(
                    self._backoff(attempt, err.retry_after if isinstance(err, RateLimitError) else None)
                )
                continue
            raise err

    @staticmethod
    def _backoff(attempt: int, retry_after: float | None) -> float:
        if retry_after:
            return min(retry_after, 30.0)
        return min(0.5 * 2 ** (attempt - 1), 8.0) * (0.5 + random.random())  # noqa: S311 - jitter
