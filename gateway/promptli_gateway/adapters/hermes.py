"""Hermes Agent: `hermes -z PROMPT --continue <name>` per turn (one named session per person).

The one-shot mode prints the final reply on stdout. Promptli tools reach Hermes through its own MCP config
(`hermes mcp add promptli -- python -m promptli_gateway.mcp_stdio --runtime hermes`), which
`promptli-agent setup --runtime hermes` prints; Hermes's own permission settings apply.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from promptli_gateway.adapter import AdapterCapabilities, TextDelta, TurnChunk, TurnDone, TurnKind
from promptli_gateway.adapters.cli_base import CliAdapter, session_name


class HermesAdapter(CliAdapter):
    name = "hermes"
    binary = "hermes"
    capabilities = AdapterCapabilities(streams_text=False, interruptible=True, voice=False, native_tools=True, resumable_sessions=True)

    def __init__(self, *, model: str | None = None, provider: str | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.model = model
        self.provider = provider

    def command(self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None) -> list[str]:
        name = resume_id or session_name(session_key)
        text = prompt if resume_id else self.preamble() + prompt
        argv = [self.bin, "-z", text, "--continue", name]
        if self.model:
            argv += ["-m", self.model]
        if self.provider:
            argv += ["--provider", self.provider]
        return [*argv, *self.extra_args]

    async def parse(self, lines: AsyncIterator[str], session_key: str) -> AsyncIterator[TurnChunk]:
        out: list[str] = []
        async for line in lines:
            out.append(line)
        text = "\n".join(out).strip()
        if text:
            yield TextDelta(text)
        yield TurnDone(text=text, session_id=session_name(session_key))
