"""Live calls over Agent Link (voice, Mode C): your agent produces every word; Promptli does the phone.

    async def on_call(call: LiveCall) -> None:
        await call.accept()
        async for frame in call.frames():
            if frame["type"] == "call.start" and frame.get("greet"):
                await call.say("Hi, thanks for calling!", turn_id="t0")
            elif frame["type"] == "call.transcript" and frame.get("is_final"):
                await call.say_stream(my_llm_stream(frame["text"]), turn_id=frame["turn_id"])

    calls = CallDispatcher(link, on_call)
    async for frame in link:
        if not await calls.handle(frame):
            ...  # a normal event

Frames are documented in spec/agent-link.md ("Live calls").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from typing import Any, cast

from promptli.link import AgentLink

log = logging.getLogger("promptli.calls")


class LiveCall:
    def __init__(self, link: AgentLink, incoming: dict[str, Any]) -> None:
        self.link = link
        self.incoming = incoming
        self.call_id: str = str(incoming["call_id"])
        self.ended = False
        self.accepted = False
        self.stop_reason: str | None = None
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    # ---- what we know about the call ---------------------------------------------------------------
    @property
    def direction(self) -> str:
        return str(self.incoming.get("direction") or "inbound")

    @property
    def remote_number(self) -> str:
        """The other party: the caller (inbound) or who we called (outbound)."""
        key = "from" if self.direction == "inbound" else "to"
        return str(self.incoming.get(key) or "")

    @property
    def contact(self) -> dict[str, Any] | None:
        c: Any = self.incoming.get("contact")
        return cast("dict[str, Any]", c) if isinstance(c, dict) else None

    @property
    def contact_key(self) -> str:
        return str(self.incoming.get("contact_key") or f"phone:{self.remote_number}")

    # ---- control --------------------------------------------------------------------------------------
    async def _send(self, frame: dict[str, Any]) -> None:
        if self.ended:
            return
        with contextlib.suppress(Exception):
            await self.link.send({**frame, "call_id": self.call_id})

    async def accept(self) -> None:
        self.accepted = True
        await self._send({"type": "call.accept"})

    async def decline(self) -> None:
        await self._send({"type": "call.decline"})

    async def text(self, turn_id: str, delta: str, *, done: bool = False) -> None:
        await self._send({"type": "call.text", "turn_id": turn_id, "delta": delta, "done": done})

    async def say(self, text: str, *, turn_id: str) -> None:
        """Speak a whole reply for a turn (no streaming)."""
        await self.text(turn_id, text, done=True)

    async def say_stream(self, chunks: AsyncIterable[str], *, turn_id: str) -> str:
        """Stream a reply as it is generated (the caller hears the first words right away). Returns it."""
        parts: list[str] = []
        async for chunk in chunks:
            if chunk:
                parts.append(chunk)
                await self.text(turn_id, chunk)
        await self.text(turn_id, "", done=True)
        return "".join(parts)

    async def filler(self, phrase: str | None = None) -> None:
        """Play a short filler now ("One moment.") before slow work."""
        await self._send({"type": "call.filler", **({"phrase": phrase} if phrase else {})})

    async def hangup(self, say: str | None = None) -> None:
        await self._send({"type": "call.hangup", **({"say": say} if say else {})})

    async def transfer(self, to: str, say: str | None = None) -> None:
        await self._send({"type": "call.transfer", "to": to, **({"say": say} if say else {})})

    async def dtmf(self, digits: str) -> None:
        """Press keys (navigate a phone menu)."""
        await self._send({"type": "call.dtmf", "digits": digits})

    async def set_voice(self, voice_id: str | None = None, speed: float | None = None) -> None:
        await self._send(
            {
                "type": "call.voice",
                **({"voice_id": voice_id} if voice_id else {}),
                **({"speed": speed} if speed else {}),
            }
        )

    # ---- incoming frames ---------------------------------------------------------------------------------
    def _push(self, frame: dict[str, Any]) -> None:
        self._queue.put_nowait(frame)
        if frame.get("type") == "call.stop":
            self.stop_reason = str(frame.get("reason") or "")
            self._queue.put_nowait(None)

    async def frames(self) -> AsyncIterator[dict[str, Any]]:
        """call.start, call.transcript, call.barge_in, call.tts.played, call.dtmf, call.machine, call.stop."""
        while True:
            frame = await self._queue.get()
            if frame is None:
                self.ended = True
                return
            yield frame


OnCall = Callable[[LiveCall], Awaitable[None]]


class CallDispatcher:
    """Routes call frames from an AgentLink to LiveCall objects; runs `on_call` for each new call."""

    def __init__(self, link: AgentLink, on_call: OnCall) -> None:
        self.link = link
        self.on_call = on_call
        self.calls: dict[str, LiveCall] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, frame: dict[str, Any]) -> bool:
        """Returns True when the frame was a call frame (handled here)."""
        kind = str(frame.get("type") or "")
        # Persisted events (call.started / call.ended envelopes) carry a seq; live call frames never do.
        if not kind.startswith("call.") or "seq" in frame:
            return False
        call_id = str(frame.get("call_id") or "")
        if kind == "call.incoming":
            call = LiveCall(self.link, frame)
            self.calls[call_id] = call
            task = asyncio.create_task(self._run(call))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return True
        call = self.calls.get(call_id)
        if call is not None:
            call._push(frame)  # pyright: ignore[reportPrivateUsage]
            if kind == "call.stop":
                self.calls.pop(call_id, None)
        return True

    async def _run(self, call: LiveCall) -> None:
        try:
            await self.on_call(call)
        except Exception:
            log.exception("call handler failed for %s", call.call_id)
        finally:
            self.calls.pop(call.call_id, None)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
