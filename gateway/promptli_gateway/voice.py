"""CallTurnDriver: run a live call (Mode C) with any runtime adapter.

Each final caller utterance becomes a `voice` turn in the same per-person session the agent uses for email
and SMS. The reply streams back as `call.text` deltas (speech-filtered), so the caller hears the first words
while the agent is still writing. A barge-in interrupts the runtime; the next turn tells it what the caller
actually heard. When the call ends, a `post_call` turn lets the agent do its follow-ups with tools.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from promptli.calls_live import LiveCall
from promptli_gateway.adapter import RuntimeAdapter, TextDelta, ToolActivity, TurnDone, TurnKind
from promptli_gateway.framing import (
    frame_call_dtmf,
    frame_call_guidance,
    frame_call_start,
    frame_call_turn,
    frame_post_call,
    is_no_reply,
)
from promptli_gateway.speech import SpeechFilter

log = logging.getLogger("promptli.gateway.voice")

SORRY = "Sorry, I ran into a problem there. Could you say that again?"


class CallTurnDriver:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        *,
        identity_name: str,
        get_session: Callable[[str], str | None],
        set_session: Callable[[str, str | None], None],
        post_call_turn: bool = True,
    ) -> None:
        self.adapter = adapter
        self.identity_name = identity_name
        self.get_session = get_session
        self.set_session = set_session
        self.post_call_turn = post_call_turn
        self.active: dict[str, LiveCall] = {}
        self.turns_completed = 0

    def live_call(self, call_id: str) -> LiveCall | None:
        return self.active.get(call_id)

    async def on_call(self, call: LiveCall) -> None:
        await call.accept()
        self.active[call.call_id] = call
        session_key = call.contact_key
        info = {**call.incoming}
        header: str | None = None  # call context, sent with the first turn
        barge: dict[str, Any] | None = None
        guidance: list[str] = []  # owner steering to fold into the next reply
        running: asyncio.Task[None] | None = None

        async def start_turn(turn_id: str, prompt: str) -> None:
            nonlocal running, header
            if running is not None and not running.done():
                await self.adapter.interrupt(session_key)
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await running
            if header:
                prompt = f"{header}\n\n{prompt}"
                header = None
            running = asyncio.create_task(self._voice_turn(call, session_key, turn_id, prompt))

        try:
            async for frame in call.frames():
                kind = frame.get("type")
                if kind == "call.start":
                    greet = bool(frame.get("greet"))
                    info["opening"] = frame.get("opening") or info.get("opening")
                    start = frame_call_start(info, greet=greet, identity_name=self.identity_name)
                    if greet:
                        await start_turn(str(frame.get("turn_id") or "t0"), start)
                    else:
                        header = start
                elif kind == "call.transcript" and frame.get("is_final") and frame.get("turn_id"):
                    prompt = frame_call_turn(str(frame.get("text") or ""), barge=barge)
                    barge = None
                    if guidance:
                        prompt = "\n".join(frame_call_guidance(g) for g in guidance) + "\n" + prompt
                        guidance.clear()
                    await start_turn(str(frame["turn_id"]), prompt)
                elif kind == "call.guidance" and frame.get("text"):
                    if frame.get("turn_id"):  # the agent is idle: speak now
                        await start_turn(str(frame["turn_id"]), frame_call_guidance(str(frame["text"])))
                    else:  # mid-reply: shape the next one
                        guidance.append(str(frame["text"]))
                elif kind == "call.barge_in":
                    barge = frame
                    if running is not None and not running.done():
                        await self.adapter.interrupt(session_key)
                elif kind == "call.dtmf" and frame.get("digits"):
                    await start_turn(
                        f"d{len(call.call_id)}{frame['digits']}", frame_call_dtmf(str(frame["digits"]))
                    )
        finally:
            self.active.pop(call.call_id, None)
            if running is not None and not running.done():
                await self.adapter.interrupt(session_key)
                running.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await running
        if self.post_call_turn:
            await self._post_call(session_key, info, call.stop_reason)

    async def _voice_turn(self, call: LiveCall, session_key: str, turn_id: str, prompt: str) -> None:
        speech = SpeechFilter()
        sent = False
        filler_sent = False
        final = ""
        try:
            async for chunk in self.adapter.run_turn(
                session_key, prompt, kind=TurnKind.VOICE, resume_id=self.get_session(session_key)
            ):
                if isinstance(chunk, TextDelta):
                    spoken = speech.feed(chunk.text)
                    if spoken.strip() or (sent and spoken):
                        await call.text(turn_id, spoken)
                        sent = True
                elif isinstance(chunk, ToolActivity):
                    if chunk.status == "started" and not sent and not filler_sent:
                        await call.filler()
                        filler_sent = True
                elif isinstance(chunk, TurnDone):
                    if chunk.session_id:
                        self.set_session(session_key, chunk.session_id)
                    final = chunk.text
                else:  # TurnError
                    log.warning("voice turn error on %s: %s", call.call_id, chunk.message)
                    if not sent:
                        await call.say(SORRY, turn_id=turn_id)
                        return
            tail = speech.flush()
            if not sent and final and not is_no_reply(final):
                whole = SpeechFilter()
                await call.say(whole.feed(final) + whole.flush(), turn_id=turn_id)
                return
            await call.text(turn_id, tail, done=True)
            self.turns_completed += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("voice turn failed on %s", call.call_id)
            if not sent:
                await call.say(SORRY, turn_id=turn_id)

    async def _post_call(self, session_key: str, info: dict[str, Any], reason: str | None) -> None:
        try:
            async for chunk in self.adapter.run_turn(
                session_key,
                frame_post_call(info, reason),
                kind=TurnKind.POST_CALL,
                resume_id=self.get_session(session_key),
            ):
                if isinstance(chunk, TurnDone) and chunk.session_id:
                    self.set_session(session_key, chunk.session_id)
        except Exception:
            log.exception("post-call turn failed for %s", info.get("call_id"))
