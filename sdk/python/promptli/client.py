"""Clients: `AsyncPromptli` (async-first) and `Promptli` (sync, runs the async client on a private loop)."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Coroutine, Iterator
from typing import Any, Literal, TypeVar, cast

import httpx

from promptli._generated import models as m
from promptli._http import DEFAULT_BASE_URL, Transport
from promptli.identity import AsyncIdentity

T = TypeVar("T")


class _Namespace:
    def __init__(self, transport: Transport) -> None:
        self._t = transport


class Identities(_Namespace):
    async def list(self) -> list[m.Identity]:
        return [
            m.Identity.model_validate(x) for x in (await self._t.request("GET", "/v1/identities"))["data"]
        ]

    async def create(
        self, handle: str, *, display_name: str | None = None, description: str | None = None
    ) -> m.Identity:
        body = {"handle": handle, "display_name": display_name, "description": description}
        return m.Identity.model_validate(await self._t.request("POST", "/v1/identities", json=body))

    async def upsert(
        self, handle: str, *, display_name: str | None = None, description: str | None = None
    ) -> m.IdentityUpsertResult:
        body = {
            k: v
            for k, v in {"display_name": display_name, "description": description}.items()
            if v is not None
        }
        return m.IdentityUpsertResult.model_validate(
            await self._t.request("PUT", f"/v1/identities/{handle}", json=body)
        )

    async def me(self) -> m.Identity:
        return m.Identity.model_validate(await self._t.request("GET", "/v1/identities/self"))


class ApiKeys(_Namespace):
    async def list(self, *, include_revoked: bool = False) -> list[m.ApiKey]:
        res = await self._t.request("GET", "/v1/api-keys", params={"include_revoked": include_revoked})
        return [m.ApiKey.model_validate(x) for x in res["data"]]

    async def create(
        self,
        name: str,
        *,
        identity: str | None = None,
        scopes: list[str] | None = None,
        description: str | None = None,
    ) -> m.CreatedApiKey:
        body = {"name": name, "identity": identity, "scopes": scopes, "description": description}
        return m.CreatedApiKey.model_validate(await self._t.request("POST", "/v1/api-keys", json=body))

    async def revoke(self, key_id: str) -> None:
        await self._t.request("POST", f"/v1/api-keys/{key_id}/revoke")

    async def revoke_self(self) -> None:
        await self._t.request("POST", "/v1/api-keys/self/revoke")


class Contacts(_Namespace):
    async def list(
        self, *, q: str | None = None, source: Literal["saved", "suggested"] | None = None, limit: int = 50
    ) -> list[m.Contact]:
        res = await self._t.request("GET", "/v1/contacts", params={"q": q, "source": source, "limit": limit})
        return [m.Contact.model_validate(x) for x in res["data"]]

    async def create(
        self,
        *,
        name: str | None = None,
        emails: list[str] | None = None,
        phones: list[str] | None = None,
        company: str | None = None,
        notes: str | None = None,
    ) -> m.Contact:
        body = {
            "name": name,
            "emails": emails or [],
            "phones": phones or [],
            "company": company,
            "notes": notes,
        }
        return m.Contact.model_validate(await self._t.request("POST", "/v1/contacts", json=body))

    async def lookup(self, *, email: str | None = None, phone: str | None = None) -> m.Contact | None:
        res = await self._t.request("GET", "/v1/contacts/lookup", params={"email": email, "phone": phone})
        return m.Contact.model_validate(res) if res else None

    async def add_memory(self, contact_id: str, text: str) -> m.Contact:
        res = await self._t.request("POST", f"/v1/contacts/{contact_id}/memories", json={"text": text})
        return m.Contact.model_validate(res)


class Webhooks(_Namespace):
    async def subscribe(
        self, identity: str, url: str, event_types: list[str] | None = None, *, auth_token: str | None = None
    ) -> m.SubscriptionCreated:
        body = {
            "identity": identity,
            "url": url,
            "event_types": event_types or ["*"],
            "auth_token": auth_token,
        }
        return m.SubscriptionCreated.model_validate(
            await self._t.request("POST", "/v1/webhooks/subscriptions", json=body)
        )

    async def list(self, identity: str | None = None) -> list[m.Subscription]:
        res = await self._t.request("GET", "/v1/webhooks/subscriptions", params={"identity": identity})
        return [m.Subscription.model_validate(x) for x in res["data"]]

    async def delete(self, subscription_id: str) -> None:
        await self._t.request("DELETE", f"/v1/webhooks/subscriptions/{subscription_id}")

    async def test(self, subscription_id: str) -> str:
        return str(
            (await self._t.request("POST", f"/v1/webhooks/subscriptions/{subscription_id}/test"))["event_id"]
        )

    async def deliveries(
        self,
        *,
        subscription: str | None = None,
        status: Literal["pending", "succeeded", "retrying", "failed"] | None = None,
        limit: int = 50,
    ) -> list[m.Delivery]:
        res = await self._t.request(
            "GET",
            "/v1/webhooks/deliveries",
            params={"subscription": subscription, "status": status, "limit": limit},
        )
        return [m.Delivery.model_validate(x) for x in res["data"]]

    async def replay(self, delivery_id: str) -> m.Delivery:
        return m.Delivery.model_validate(
            await self._t.request("POST", f"/v1/webhooks/deliveries/{delivery_id}/replay")
        )

    async def catalog(self) -> list[m.CatalogEntry]:
        return [
            m.CatalogEntry.model_validate(x)
            for x in (await self._t.request("GET", "/v1/webhooks/catalog"))["data"]
        ]


class Numbers(_Namespace):
    async def list(self) -> list[m.PhoneNumber]:
        return [
            m.PhoneNumber.model_validate(x) for x in (await self._t.request("GET", "/v1/numbers"))["data"]
        ]

    async def available(
        self, *, region: str | None = None, area_code: str | None = None
    ) -> list[m.AvailableNumber]:
        res = await self._t.request(
            "GET", "/v1/numbers/available", params={"region": region, "area_code": area_code}
        )
        return [m.AvailableNumber.model_validate(x) for x in res["data"]]


class Calls(_Namespace):
    async def get(self, call_id: str) -> m.Call:
        return m.Call.model_validate(await self._t.request("GET", f"/v1/calls/{call_id}"))

    async def transcript(self, call_id: str) -> m.Transcript:
        return m.Transcript.model_validate(await self._t.request("GET", f"/v1/calls/{call_id}/transcript"))

    async def guide(self, call_id: str, text: str) -> dict[str, Any]:
        """Steer a live call: your agent privately gets `text` and works it into the conversation.
        Returns {"applied": true, "when": "now" | "next_turn"}."""
        res: dict[str, Any] = await self._t.request(
            "POST", f"/v1/calls/{call_id}/guidance", json={"text": text}
        )
        return res

    async def hangup(self, call_id: str) -> m.Call:
        return m.Call.model_validate(await self._t.request("POST", f"/v1/calls/{call_id}/hangup"))

    async def recording(self, call_id: str) -> bytes:
        data = await self._t.request("GET", f"/v1/calls/{call_id}/recording")
        return data if isinstance(data, bytes) else b""

    async def voices(self, *, language: str | None = None, q: str | None = None) -> list[m.Voice]:
        res = await self._t.request("GET", "/v1/voices", params={"language": language, "q": q})
        return [m.Voice.model_validate(x) for x in res["data"]]


class Sandbox(_Namespace):
    """Test mode only (psk_test_ keys): simulate the outside world."""

    async def inbound_call(
        self,
        to_identity: str,
        *,
        script: list[dict[str, Any]] | None = None,
        from_number: str = "+15550109999",
    ) -> str:
        """Simulate a phone call; returns the call id. Script steps: {"say"}, {"wait_ms"},
        {"wait_for_agent": true}, {"interrupt"}, {"dtmf"}, {"hangup": true}."""
        body: dict[str, Any] = {"to_identity": to_identity, "from_number": from_number}
        if script is not None:
            body["script"] = script
        res = await self._t.request("POST", "/v1/sandbox/calls/inbound", json=body)
        return str(res["call_id"])

    async def inbound_email(self, to_identity: str, **fields: Any) -> dict[str, Any]:
        res: dict[str, Any] = await self._t.request(
            "POST", "/v1/sandbox/emails/inbound", json={"to_identity": to_identity, **fields}
        )
        return res

    async def inbound_text(
        self, to_identity: str, body: str, *, from_number: str = "+15550109999"
    ) -> dict[str, Any]:
        res: dict[str, Any] = await self._t.request(
            "POST",
            "/v1/sandbox/texts/inbound",
            json={"to_identity": to_identity, "body": body, "from_number": from_number},
        )
        return res


class Signup(_Namespace):
    async def status(self) -> m.SignupStatus:
        return m.SignupStatus.model_validate(await self._t.request("GET", "/v1/signup/status"))

    async def verify(self, code: str) -> bool:
        return bool((await self._t.request("POST", "/v1/signup/verify", json={"code": code}))["verified"])

    async def resend(self) -> m.SignupStatus:
        return m.SignupStatus.model_validate(await self._t.request("POST", "/v1/signup/resend"))


class AsyncPromptli:
    """Async client. `async with AsyncPromptli() as p: await p.identity("glow").send_text(...)`."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        user_agent: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._t = Transport(
            api_key=api_key or os.environ.get("PROMPTLI_API_KEY", ""),
            base_url=base_url or os.environ.get("PROMPTLI_BASE_URL", DEFAULT_BASE_URL),
            timeout=timeout,
            max_retries=max_retries,
            user_agent=user_agent,
            http_client=http_client,
        )
        self.identities = Identities(self._t)
        self.api_keys = ApiKeys(self._t)
        self.contacts = Contacts(self._t)
        self.webhooks = Webhooks(self._t)
        self.numbers = Numbers(self._t)
        self.calls = Calls(self._t)
        self.sandbox = Sandbox(self._t)
        self.signup = Signup(self._t)

    @property
    def transport(self) -> Transport:
        return self._t

    def identity(self, ref: str) -> AsyncIdentity:
        """Handle or idn_ id. With an identity-scoped key use `await p.identities.me()` to discover it."""
        return AsyncIdentity(self._t, ref)

    async def whoami(self) -> m.WhoamiResponse:
        return m.WhoamiResponse.model_validate(await self._t.request("GET", "/v1/whoami"))

    @staticmethod
    async def create_signup(
        human_email: str,
        handle: str,
        *,
        display_name: str | None = None,
        test_mode: bool = False,
        base_url: str | None = None,
    ) -> m.SignupCreated:
        """Agent self-signup (no key needed). Returns a provisional key; the human gets a code."""
        url = (base_url or os.environ.get("PROMPTLI_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        async with httpx.AsyncClient(timeout=30) as c:
            res = await c.post(
                f"{url}/v1/signup",
                json={
                    "human_email": human_email,
                    "handle": handle,
                    "display_name": display_name,
                    "test_mode": test_mode,
                },
            )
        if res.status_code >= 400:
            from promptli.errors import error_from_response

            raise error_from_response(res.status_code, res.json())
        return m.SignupCreated.model_validate(res.json())

    async def close(self) -> None:
        await self._t.close()

    async def __aenter__(self) -> AsyncPromptli:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


# ---- sync facade ---------------------------------------------------------------------------------


class _Loop:
    """A private event loop on a daemon thread, so the sync client works anywhere (even inside notebooks)."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="promptli-sync", daemon=True)
        self.thread.start()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def iterate(self, agen: AsyncIterator[T]) -> Iterator[T]:
        while True:
            try:
                yield self.run(agen.__anext__())  # type: ignore[arg-type]
            except StopAsyncIteration:
                return


class _SyncProxy:
    """Wraps an async object: coroutine methods become blocking calls, async iterators become iterators."""

    def __init__(self, target: Any, loop: _Loop) -> None:
        self._target = target
        self._loop = loop

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if isinstance(attr, (_Namespace, AsyncIdentity)):
            return _SyncProxy(attr, self._loop)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if asyncio.iscoroutine(result):
                return self._loop.run(result)
            if hasattr(result, "__anext__"):
                agen = cast(AsyncIterator[Any], result)
                return self._loop.iterate(agen)
            if isinstance(result, (_Namespace, AsyncIdentity)):
                return _SyncProxy(result, self._loop)
            return result

        return call


class Promptli(_SyncProxy):
    """Blocking client with the same methods as `AsyncPromptli`.

    with Promptli() as p:
        p.identity("glow").send_email(to="jane@example.com", subject="Hi", text="Hello")
    """

    def __init__(self, api_key: str | None = None, **kwargs: Any) -> None:
        loop = _Loop()

        async def make() -> AsyncPromptli:
            return AsyncPromptli(api_key, **kwargs)

        super().__init__(loop.run(make()), loop)

    def close(self) -> None:
        self._loop.run(self._target.close())

    def __enter__(self) -> Promptli:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
