"""Generic HTTP adapter: any custom agent that exposes `POST /turn` (JSON in, text or SSE out).

Request:  {"session_key", "prompt", "kind", "resume_id", "context": {...}}
Response: application/json {"text", "session_id"?}  or  text/event-stream of `data: {"delta": "..."}` lines
          ending with `data: {"done": true, "text"?: "...", "session_id"?: "..."}`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

import httpx

from promptli_gateway.adapter import (
    AdapterCapabilities,
    GatewayContext,
    TextDelta,
    TurnChunk,
    TurnDone,
    TurnError,
    TurnKind,
)
from promptli_gateway.tools import ToolSpec


class GenericHttpAdapter:
    name = "generic-http"
    capabilities = AdapterCapabilities(streams_text=True, native_tools=False, resumable_sessions=True)

    def __init__(self, url: str, token: str | None = None, timeout: float = 120.0) -> None:
        self.url = url
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout
        self.ctx: GatewayContext | None = None
        self._client = httpx.AsyncClient(timeout=timeout)

    async def start(self, ctx: GatewayContext, tools: list[ToolSpec]) -> None:
        self.ctx = ctx

    async def run_turn(
        self, session_key: str, prompt: str, *, kind: TurnKind, resume_id: str | None
    ) -> AsyncIterator[TurnChunk]:
        payload: dict[str, Any] = {
            "session_key": session_key,
            "prompt": prompt,
            "kind": str(kind),
            "resume_id": resume_id,
            "context": asdict(self.ctx) if self.ctx else {},
        }
        try:
            async with self._client.stream("POST", self.url, json=payload, headers=self.headers) as res:
                if res.status_code >= 400:
                    yield TurnError(
                        f"agent endpoint returned HTTP {res.status_code}", retryable=res.status_code >= 500
                    )
                    return
                if "text/event-stream" not in res.headers.get("content-type", ""):
                    body = json.loads(await res.aread())
                    yield TurnDone(text=str(body.get("text", "")), session_id=body.get("session_id"))
                    return
                parts: list[str] = []
                async for line in res.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    frame = json.loads(line[5:].strip() or "{}")
                    if frame.get("delta"):
                        parts.append(frame["delta"])
                        yield TextDelta(frame["delta"])
                    if frame.get("done"):
                        yield TurnDone(
                            text=frame.get("text") or "".join(parts), session_id=frame.get("session_id")
                        )
                        return
                yield TurnDone(text="".join(parts))
        except httpx.HTTPError as err:
            yield TurnError(f"agent endpoint unreachable: {err}", retryable=True)

    async def interrupt(self, session_key: str) -> None:
        return None

    async def reset_session(self, session_key: str) -> None:
        return None

    async def close(self) -> None:
        await self._client.aclose()
