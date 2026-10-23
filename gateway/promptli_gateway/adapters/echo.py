"""Echo adapter: deterministic stand-in runtime for tests, demos and `doctor`."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from promptli_gateway.adapter import (
    AdapterCapabilities,
    GatewayContext,
    TextDelta,
    TurnChunk,
    TurnDone,
    TurnKind,
)
from promptli_gateway.tools import ToolSpec


class EchoAdapter:
    name = "echo"
    capabilities = AdapterCapabilities(streams_text=True, native_tools=False, resumable_sessions=True)

    def __init__(self, prefix: str = "Echo: ") -> None:
        self.prefix = prefix
        self.prompts: list[tuple[str, str, TurnKind]] = []
        self.tools: list[ToolSpec] = []
        self.ctx: GatewayContext | None = None

    async def start(self, ctx: GatewayContext, tools: list[ToolSpec]) -> None:
        self.ctx = ctx
        self.tools = tools

    async def run_turn(
        self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None
    ) -> AsyncIterator[TurnChunk]:
        self.prompts.append((session_key, prompt, kind))
        m = re.search(r"<external_content[^>]*>\n(.*?)\n</external_content>", prompt, re.S)
        if kind is TurnKind.POST_CALL:
            body, reply = "", "NO_REPLY"
        elif kind is TurnKind.VOICE and not m:
            body, reply = "", "Hello, this is the echo agent."
        else:
            body = (m.group(1) if m else prompt).strip()
            reply = f"{self.prefix}{body}"
        for word in reply.split(" "):
            yield TextDelta(word + " ")
        yield TurnDone(text=reply, session_id=resume_id or f"echo-{session_key}")

    async def interrupt(self, session_key: str) -> None:
        return None

    async def reset_session(self, session_key: str) -> None:
        return None

    async def close(self) -> None:
        return None
