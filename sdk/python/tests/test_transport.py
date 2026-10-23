from __future__ import annotations

import httpx
import pytest
import respx

from promptli import AsyncPromptli, Promptli
from promptli.errors import AuthenticationError, InvalidRequestError, RateLimitError

BASE = "https://api.test.invalid"
KEY = "psk_test_abcdefghijkl_" + "a" * 43


def ident() -> dict[str, object]:
    return {
        "id": "idn_1",
        "handle": "glow",
        "display_name": "Glow",
        "description": None,
        "status": "active",
        "env": "test",
        "email_address": "glow@sandbox.agents.promptli.com",
        "mailbox_id": "mbx_1",
        "rule_modes": {
            "email": {"inbound": "blacklist", "outbound": "blacklist"},
            "phone": {"inbound": "blacklist", "outbound": "blacklist"},
        },
        "signing_key_configured": False,
        "created_at": "2026-10-23T00:00:00Z",
    }


@respx.mock
async def test_retries_reuse_idempotency_key() -> None:
    route = respx.post(f"{BASE}/v1/identities").mock(
        side_effect=[
            httpx.Response(503, json={"error": {"code": "provider_unavailable"}}),
            httpx.Response(201, json=ident()),
        ]
    )
    async with AsyncPromptli(KEY, base_url=BASE, max_retries=2) as p:
        out = await p.identities.create("glow")
    assert out.handle == "glow"
    keys = {c.request.headers["Idempotency-Key"] for c in route.calls}
    assert len(route.calls) == 2 and len(keys) == 1


@respx.mock
async def test_rate_limit_honours_retry_after_then_raises() -> None:
    respx.get(f"{BASE}/v1/whoami").mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "0"}, json={"error": {"code": "rate_limited"}}
        )
    )
    async with AsyncPromptli(KEY, base_url=BASE, max_retries=1) as p:
        with pytest.raises(RateLimitError):
            await p.whoami()


@respx.mock
async def test_errors_are_typed_and_not_retried() -> None:
    r = respx.get(f"{BASE}/v1/whoami").mock(
        return_value=httpx.Response(
            401, json={"error": {"code": "api_key_revoked", "message": "revoked", "request_id": "req_1"}}
        )
    )
    async with AsyncPromptli(KEY, base_url=BASE) as p:
        with pytest.raises(AuthenticationError) as e:
            await p.whoami()
    assert e.value.code == "api_key_revoked" and e.value.request_id == "req_1"
    assert len(r.calls) == 1


@respx.mock
async def test_policy_error_details() -> None:
    respx.post(f"{BASE}/v1/identities/glow/texts").mock(
        return_value=httpx.Response(
            422,
            json={
                "error": {
                    "code": "recipient_opted_out",
                    "message": "STOP",
                    "details": {"recipient": "+15550100000"},
                }
            },
        )
    )
    async with AsyncPromptli(KEY, base_url=BASE) as p:
        with pytest.raises(InvalidRequestError) as e:
            await p.identity("glow").send_text("+15550100000", "hi")
    assert e.value.code == "recipient_opted_out"
    assert e.value.details["recipient"] == "+15550100000"


def test_link_url_derivation() -> None:
    from promptli._http import Transport

    assert (
        Transport(api_key=KEY, base_url="https://api.comms.promptli.com").link_url
        == "wss://link.comms.promptli.com/v1/connect"
    )
    assert (
        Transport(api_key=KEY, base_url="http://localhost:8000").link_url == "ws://localhost:8100/v1/connect"
    )


@respx.mock
def test_sync_client_and_pagination() -> None:
    page1 = {"data": [{**_msg("msg_1")}], "next_cursor": "c2"}
    page2 = {"data": [{**_msg("msg_2")}], "next_cursor": None}
    respx.get(f"{BASE}/v1/identities/glow/mail/messages").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )
    with Promptli(KEY, base_url=BASE) as p:
        ids = [m.id for m in p.identity("glow").emails()]
    assert ids == ["msg_1", "msg_2"]


def _msg(mid: str) -> dict[str, object]:
    return {
        "id": mid,
        "thread_id": "thr_1",
        "direction": "inbound",
        "folder": "inbox",
        "status": "received",
        "from": "a@example.com",
        "from_name": None,
        "to": ["glow@x"],
        "cc": [],
        "bcc": [],
        "reply_to": None,
        "subject": "s",
        "snippet": "s",
        "text": None,
        "html": None,
        "rfc_message_id": "<m@x>",
        "in_reply_to": None,
        "references": [],
        "attachments": [],
        "read": False,
        "contacts": [],
        "error_code": None,
        "bounce_type": None,
        "created_at": "2026-10-23T00:00:00Z",
    }
