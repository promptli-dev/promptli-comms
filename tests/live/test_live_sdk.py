"""Live SDK test against a running Promptli API + realtime (test-mode admin key).

PROMPTLI_API_KEY=psk_test_… PROMPTLI_BASE_URL=http://localhost:8000 uv run pytest -m live tests/live
"""

from __future__ import annotations

import asyncio
import os
import secrets

import pytest

from promptli import AsyncPromptli, InvalidRequestError

pytestmark = pytest.mark.live


@pytest.fixture
def key() -> str:
    k = os.environ.get("PROMPTLI_API_KEY", "")
    if not k.startswith("psk_test_"):
        pytest.skip("needs a psk_test_ admin key")
    return k


async def test_sdk_end_to_end(key: str) -> None:
    tag = secrets.token_hex(3)
    async with AsyncPromptli(key) as p:
        a = await p.identities.create(f"sdk-a-{tag}")
        b = await p.identities.create(f"sdk-b-{tag}")
        try:
            na = await p.identity(a.handle).provision_phone_number(region="CA")
            nb = await p.identity(b.handle).provision_phone_number()
            assert na.sms_status == "ready"
            b_key = await p.api_keys.create("sdk-link", identity=b.id)
            async with AsyncPromptli(b_key.key) as pb:
                async with pb.identity(b.handle).link(instance=f"sdk-{tag}") as link:
                    sent = await p.identity(a.handle).send_text(nb.number, f"hello {tag}")
                    assert sent.status == "sent"
                    evt = await asyncio.wait_for(anext(aiter(link)), timeout=15)
                    assert evt["type"] == "text.received"
                    assert evt["data"]["text"]["body"] == f"hello {tag}"
                    await link.ack(evt)
                st = await pb.identity(b.handle).channel_status()
                assert st.sms.ready
            msg = await p.identity(a.handle).send_email(b.email_address or "", f"Subject {tag}", text="Body")
            assert msg.status == "sent"
            with pytest.raises(InvalidRequestError) as e:
                await p.identity(a.handle).send_text("12", "bad")
            assert e.value.code == "validation_error"
        finally:
            for h in (a.handle, b.handle):
                await p.identity(h).release_phone_number()
                await p.identity(h).delete()
