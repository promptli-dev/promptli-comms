"""Live gateway test: bootstrap → run gateway (echo adapter) → inbound email/SMS → auto-replies.

PROMPTLI_API_KEY=psk_test_<admin> PROMPTLI_BASE_URL=http://localhost:8000 uv run pytest -m live tests/live
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path

import pytest

from promptli import AsyncPromptli
from promptli_gateway.adapters.echo import EchoAdapter
from promptli_gateway.config import GatewayConfig
from promptli_gateway.core import Gateway
from promptli_gateway.setup.bootstrap import bootstrap
from promptli_gateway.setup.doctor import doctor

pytestmark = pytest.mark.live


async def wait_until(pred, timeout: float = 20.0):  # type: ignore[no-untyped-def]
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        got = await pred()
        if got:
            return got
        await asyncio.sleep(0.5)
    return None


async def test_gateway_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = os.environ.get("PROMPTLI_API_KEY", "")
    base = os.environ.get("PROMPTLI_BASE_URL", "http://localhost:8000")
    if not admin.startswith("psk_test_"):
        pytest.skip("needs a psk_test_ admin key")
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))
    monkeypatch.delenv("PROMPTLI_API_KEY")  # the gateway must use its own stored identity key
    handle = f"gw-{secrets.token_hex(3)}"
    async with AsyncPromptli(admin, base_url=base) as p:
        try:
            res = await bootstrap(
                runtime="echo", api_key=admin, identity=handle, base_url=base, want_number=True
            )
            assert res.status == "ok", res
            assert "minted identity-scoped key (admin key not stored)" in res.actions
            again = await bootstrap(
                runtime="echo", api_key=admin, identity=handle, base_url=base, want_number=True
            )
            assert "identity exists" in again.actions and "reused stored identity key" in again.actions
            cfg = GatewayConfig.load("echo")
            assert cfg is not None and cfg.api_key() != admin

            gw = Gateway(cfg, EchoAdapter())
            task = asyncio.create_task(gw.run())
            await asyncio.sleep(3)

            await p.sandbox.inbound_email(
                handle, subject="Booking", text="Can I move to 4pm?", from_address="jane@example.com"
            )
            replied = await wait_until(lambda: _first_outbound(p, handle))
            assert replied is not None and replied.subject.startswith("Re: Booking")
            full = await p.identity(handle).email(replied.id)
            assert (full.text or "").startswith("Echo: Can I move to 4pm?")

            await p.sandbox.inbound_text(handle, "Running late", from_number="+15550104242")

            async def sms_reply():  # type: ignore[no-untyped-def]
                texts = await p.identity(handle).texts()
                return [t for t in texts if t.direction == "outbound" and "Running late" in t.body]

            assert await wait_until(sms_reply)

            gw.stop()
            await asyncio.wait_for(task, timeout=15)

            # Restart: nothing is replied to twice.
            cfg2 = GatewayConfig.load("echo")
            assert cfg2 is not None
            gw2 = Gateway(cfg2, EchoAdapter())
            task2 = asyncio.create_task(gw2.run())
            await asyncio.sleep(5)
            outbound = [m async for m in p.identity(handle).emails(direction="outbound")]
            assert len(outbound) == 1
            assert gw2.turns_completed == 0

            report = await doctor("echo")
            names = {c.name: c.ok for c in report.checks}
            assert names["api key"] and names["email"] and names["sms"] and names["agent link"], (
                report.as_dict()
            )
            gw2.stop()
            await asyncio.wait_for(task2, timeout=15)
        finally:
            await p.identity(handle).release_phone_number()
            await p.identity(handle).delete()


async def _first_outbound(p: AsyncPromptli, handle: str):  # type: ignore[no-untyped-def]
    async for m in p.identity(handle).emails(direction="outbound"):
        return m
    return None
