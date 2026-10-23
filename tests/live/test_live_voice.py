"""Live voice test: the gateway (echo adapter) answers sandbox calls through the real API/realtime stack.

PROMPTLI_API_KEY=psk_test_<admin> PROMPTLI_BASE_URL=http://localhost:8000 uv run pytest -m live tests/live
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from promptli import AsyncPromptli
from promptli_gateway.adapters.echo import EchoAdapter
from promptli_gateway.config import GatewayConfig
from promptli_gateway.core import Gateway
from promptli_gateway.setup.bootstrap import bootstrap

pytestmark = pytest.mark.live
T = TypeVar("T")


async def wait_until(pred: Callable[[], Awaitable[T]], timeout: float = 45.0) -> T | None:
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        got = await pred()
        if got:
            return got
        await asyncio.sleep(0.5)
    return None


async def test_gateway_answers_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    admin = os.environ.get("PROMPTLI_API_KEY", "")
    base = os.environ.get("PROMPTLI_BASE_URL", "http://localhost:8000")
    if not admin.startswith("psk_test_"):
        pytest.skip("needs a psk_test_ admin key")
    monkeypatch.setenv("PROMPTLI_HOME", str(tmp_path))
    monkeypatch.delenv("PROMPTLI_API_KEY")
    handle = f"gwv-{secrets.token_hex(3)}"
    async with AsyncPromptli(admin, base_url=base) as p:
        me = p.identity(handle)
        try:
            res = await bootstrap(
                runtime="echo", api_key=admin, identity=handle, base_url=base, want_number=True
            )
            assert res.status == "ok", res
            cfg = GatewayConfig.load("echo")
            assert cfg is not None
            gw = Gateway(cfg, EchoAdapter())
            task = asyncio.create_task(gw.run())
            await asyncio.sleep(3)

            async def ended(call_id: str) -> Any:
                c = await p.calls.get(call_id)
                return c if c.ended_at else None

            # Inbound: greeting, one echoed turn, then the caller hangs up; a post-call turn runs.
            cid = await p.sandbox.inbound_call(
                handle,
                script=[
                    {"wait_for_agent": True},
                    {"say": "Hello there"},
                    {"wait_for_agent": True},
                    {"hangup": True},
                ],
            )
            call = await wait_until(lambda: ended(cid))
            assert call is not None and call.outcome == "answered_by_agent", call
            tr = await p.calls.transcript(cid)
            agent = [s.text for s in tr.segments if s.party == "agent"]
            assert agent[0] == "Hello, this is the echo agent.", agent
            assert any("Echo: Hello there" in t for t in agent), agent

            # Outbound with an opening line, answered by the simulated callee.
            out = await me.place_call(
                "+15550107777",
                reason="test",
                opening="Hi, this is a test call.",
                sandbox_script=[
                    {"wait_for_agent": True},
                    {"say": "Who is this"},
                    {"wait_for_agent": True},
                    {"hangup": True},
                ],
            )
            oc = await wait_until(lambda: ended(out.id))
            assert oc is not None and oc.direction == "outbound"
            otr = await p.calls.transcript(out.id)
            texts = [(s.party, s.text) for s in otr.segments]
            assert texts[0] == ("agent", "Hi, this is a test call."), texts
            assert ("agent", "Echo: Who is this") in texts, texts

            # Agent offline → voicemail; when it reconnects, the voicemail becomes a turn.
            gw.stop()
            await asyncio.wait_for(task, timeout=20)
            await asyncio.sleep(2)
            vm = await p.sandbox.inbound_call(
                handle, script=[{"wait_ms": 9000}, {"say": "Please call me back"}, {"wait_ms": 8000}]
            )
            vcall = await wait_until(lambda: ended(vm), timeout=60)
            assert vcall is not None and vcall.outcome == "voicemail", vcall
            cfg2 = GatewayConfig.load("echo")
            assert cfg2 is not None
            adapter = EchoAdapter()
            gw2 = Gateway(cfg2, adapter)
            task2 = asyncio.create_task(gw2.run())

            async def voicemail_turn() -> bool:
                return any(
                    "New voicemail" in prompt and "Please call me back" in prompt
                    for _, prompt, _ in adapter.prompts
                )

            assert await wait_until(voicemail_turn, timeout=20)
            gw2.stop()
            await asyncio.wait_for(task2, timeout=20)
        finally:
            await me.release_phone_number()
            await me.delete()
