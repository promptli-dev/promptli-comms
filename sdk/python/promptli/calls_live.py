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

Hosted calls (Mode A): the Promptli assistant talks and your agent supervises. Pass `on_supervise` and
accept the seat; you get `call.observed` (both parties), `call.ask_supervisor`, `call.brief.updated` and
`call.stop`, and can `answer()`, `speak()`, `guide()`, `take_over()` / `hand_back()`:

    async def on_supervise(call: LiveCall) -> None:
        await call.accept()
        async for frame in call.frames():
            if frame["type"] == "call.ask_supervisor":
                await call.answer(frame["request_id"], "Yes, Sunday 2pm is free.")

    calls = CallDispatcher(link, on_call, on_supervise=on_supervise)

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
        # "agent": you talk (Mode C). "supervisor": the Promptli assistant talks; you watch and steer.
        self.role: str = "supervisor" if incoming.get("type") == "call.supervise" else "agent"
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

    # ---- supervisor controls (hosted calls) --------------------------------------------------------------
    @property
    def brief(self) -> dict[str, Any] | None:
        """The call brief at the time the seat was offered (call.brief.updated frames carry newer ones)."""
        b: Any = self.incoming.get("brief")
        return cast("dict[str, Any]", b) if isinstance(b, dict) else None

    async def answer(self, request_id: str, text: str) -> None:
        """Answer the assistant's ask_supervisor question; it tells the caller."""
        await self._send({"type": "call.answer", "request_id": request_id, "text": text})

    async def speak(self, text: str, *, now: bool = False) -> None:
        """Exact words on the call: after the current sentence, or right away (`now=True` interrupts)."""
        await self._send({"type": "call.say", "text": text, "mode": "now" if now else "next"})

    async def guide(self, text: str) -> None:
        """Private guidance for the assistant's next reply (never spoken as-is)."""
        await self._send({"type": "call.guidance", "text": text})

    async def take_over(self) -> None:
        """Talk yourself from now on: call.start (take_over) then call.transcript turns, like Mode C."""
        await self._send({"type": "call.take_over"})

    async def hand_back(self) -> None:
        """Let the Promptli assistant continue the call."""
        await self._send({"type": "call.hand_back"})

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

    def __init__(self, link: AgentLink, on_call: OnCall, *, on_supervise: OnCall | None = None) -> None:
        self.link = link
        self.on_call = on_call
        self.on_supervise = on_supervise
        self.calls: dict[str, LiveCall] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, frame: dict[str, Any]) -> bool:
        """Returns True when the frame was a call frame (handled here)."""
        kind = str(frame.get("type") or "")
        # Persisted events (call.started / call.ended envelopes) carry a seq; live call frames never do.
        if not kind.startswith("call.") or "seq" in frame:
            return False
        call_id = str(frame.get("call_id") or "")
        if kind in ("call.incoming", "call.supervise"):
            call = LiveCall(self.link, frame)
            if call.role == "supervisor" and self.on_supervise is None:
                await call.decline()
                return True
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
            handler = self.on_supervise if call.role == "supervisor" and self.on_supervise else self.on_call
            await handler(call)
        except Exception:
            log.exception("call handler failed for %s", call.call_id)
        finally:
            self.calls.pop(call.call_id, None)

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
