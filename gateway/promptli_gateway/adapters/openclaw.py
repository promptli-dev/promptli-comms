"""OpenClaw: `openclaw agent --session-id <name> --message PROMPT --json` per turn (via its Gateway, or the
embedded agent with --local). One explicit session id per person, so no resume token is needed.

Promptli tools reach OpenClaw through its MCP/skills config (see `promptli-agent setup --runtime openclaw`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from promptli_gateway.adapter import AdapterCapabilities, TextDelta, TurnChunk, TurnDone, TurnError, TurnKind
from promptli_gateway.adapters.cli_base import CliAdapter, session_name

TEXT_KEYS = ("reply", "text", "output", "content", "message", "response", "result")


def reply_text(doc: Any) -> str:
    """OpenClaw's JSON result: take the reply text from the usual keys (nested `result`/`payload` too)."""
    if isinstance(doc, str):
        return doc
    if isinstance(doc, list):
        parts = [reply_text(x) for x in doc]  # pyright: ignore[reportUnknownVariableType]
        return "\n".join(p for p in parts if p)
    if isinstance(doc, dict):
        for k in TEXT_KEYS:
            if k in doc:
                got = reply_text(doc[k])  # pyright: ignore[reportUnknownArgumentType]
                if got:
                    return got
        for k in ("payload", "data", "messages"):
            if k in doc:
                got = reply_text(doc[k])  # pyright: ignore[reportUnknownArgumentType]
                if got:
                    return got
    return ""


class OpenClawAdapter(CliAdapter):
    name = "openclaw"
    binary = "openclaw"
    capabilities = AdapterCapabilities(streams_text=False, interruptible=True, voice=False, native_tools=True, resumable_sessions=True)

    def __init__(self, *, agent: str | None = None, model: str | None = None, local: bool = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.agent = agent
        self.model = model
        self.local = local

    def command(self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None) -> list[str]:
        text = prompt if resume_id else self.preamble() + prompt
        argv = [self.bin, "agent", "--session-id", resume_id or session_name(session_key), "--message", text, "--json"]
        if self.agent:
            argv += ["--agent", self.agent]
        if self.model:
            argv += ["--model", self.model]
        if self.local:
            argv.append("--local")
        return [*argv, *self.extra_args]

    async def parse(self, lines: AsyncIterator[str], session_key: str) -> AsyncIterator[TurnChunk]:
        raw = [line async for line in lines]
        body = "\n".join(raw)
        start = body.find("{")
        doc: Any = None
        if start >= 0:
            try:
                doc = json.loads(body[start:])
            except json.JSONDecodeError:
                doc = None
        if doc is None:
            yield TurnError(f"openclaw: no JSON result ({body[-300:].strip()})", retryable=True)
            return
        if isinstance(doc, dict) and (doc.get("error") or doc.get("ok") is False):  # pyright: ignore[reportUnknownMemberType]
            yield TurnError(f"openclaw: {str(doc.get('error'))[:300]}", retryable=True)  # pyright: ignore[reportUnknownMemberType]
            return
        text = reply_text(doc).strip()
        if text:
            yield TextDelta(text)
        yield TurnDone(text=text, session_id=session_name(session_key))
