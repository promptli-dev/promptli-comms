"""SupervisorDriver: steer hosted calls (Mode A) from any runtime (docs/16 §6).

The Promptli assistant talks; the runtime watches. Supervisor turns run in the caller's contact session (the
same one email, SMS and Mode C calls use), so the agent brings everything it knows about the person:

- at the start: enrich the brief (call_add_fact) or NO_REPLY;
- when the assistant asks (call.ask_supervisor): the turn's text is the answer (NO_REPLY = no answer);
- every few caller turns: a batched review (facts / guidance) or NO_REPLY;
- after a take-over (call_take_over tool or REST): Mode C voice turns, streamed, until it hands back.

Turns for one call never overlap, and the call never waits for them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from promptli.calls_live import LiveCall
from promptli_gateway.adapter import RuntimeAdapter, TextDelta, TurnDone, TurnKind
from promptli_gateway.framing import (
    frame_call_start,
    frame_call_turn,
    frame_post_call,
    frame_supervise_question,
    frame_supervise_review,
    frame_supervise_start,
    is_no_reply,
)
from promptli_gateway.voice import CallTurnDriver

log = logging.getLogger("promptli.gateway.supervisor")

REVIEW_EVERY = 3  # caller turns between periodic reviews


class SupervisorDriver:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        voice: CallTurnDriver,
        *,
        get_session: Callable[[str], str | None],
        set_session: Callable[[str, str | None], None],
        review_every: int = REVIEW_EVERY,
        post_call_turn: bool = True,
    ) -> None:
        self.adapter = adapter
        self.voice = voice  # reused for take-over (Mode C turns)
        self.get_session = get_session
        self.set_session = set_session
        self.review_every = review_every
        self.post_call_turn = post_call_turn
        self.active: dict[str, LiveCall] = {}
        self.turns_completed = 0
        self._bg: set[asyncio.Future[Any]] = set()

    def live_call(self, call_id: str) -> LiveCall | None:
        return self.active.get(call_id)

    async def on_supervise(self, call: LiveCall) -> None:
        key = call.contact_key
        warm: Any = getattr(self.adapter, "warm", None)
        if callable(warm):
            with contextlib.suppress(Exception):
                pending: Any = warm(key, self.get_session(key))
                fut = asyncio.ensure_future(pending)
                self._bg.add(fut)
                fut.add_done_callback(self._bg.discard)
        await call.accept()
        self.active[call.call_id] = call
        info = {**call.incoming}
        lines: list[str] = []
        lock = asyncio.Lock()  # one supervisor turn at a time per call
        talking = False
        running: asyncio.Task[None] | None = None
        since_review = 0
        tasks: set[asyncio.Task[Any]] = set()

        def spawn(coro: Any) -> None:
            t = asyncio.ensure_future(coro)
            tasks.add(t)
            t.add_done_callback(tasks.discard)

        async def supervise(prompt: str, *, answer_to: str | None = None) -> None:
            async with lock:
                text = await self._turn(key, prompt)
                if answer_to is not None and text and not is_no_reply(text):
                    await call.answer(answer_to, text.strip())

        spawn(supervise(frame_supervise_start(info, call.brief)))
        try:
            async for frame in call.frames():
                kind = frame.get("type")
                if kind == "call.observed":
                    who = "caller" if frame.get("party") == "caller" else "assistant"
                    lines.append(f"{who}: {frame.get('text')}")
                    if who == "caller" and not talking:
                        since_review += 1
                        if since_review >= self.review_every and not lock.locked():
                            since_review = 0
                            spawn(supervise(frame_supervise_review(lines)))
                elif kind == "call.ask_supervisor":
                    question = str(frame.get("question") or "")
                    spawn(
                        supervise(
                            frame_supervise_question(question, lines), answer_to=str(frame["request_id"])
                        )
                    )
                elif kind == "call.start" and frame.get("take_over"):
                    talking = True  # we speak every turn now (Mode C), billed Standard
                    lines.append("(you took over the call)")
                elif kind == "call.handed_back":
                    talking = False
                    lines.append("(the assistant took the call back)")
                elif kind == "call.transcript" and frame.get("is_final") and frame.get("turn_id") and talking:
                    if running is not None and not running.done():
                        await self.adapter.interrupt(key)
                        running.cancel()
                    prompt = frame_call_turn(str(frame.get("text") or ""))
                    if len(lines) and lines[-1] == "(you took over the call)":
                        prompt = (
                            frame_call_start(info, greet=False, identity_name=self.voice.identity_name)
                            + "\n\n"
                            + prompt
                        )
                    running = asyncio.create_task(
                        self.voice.voice_turn(call, key, str(frame["turn_id"]), prompt)
                    )
                elif kind == "call.barge_in" and running is not None and not running.done():
                    await self.adapter.interrupt(key)
        finally:
            self.active.pop(call.call_id, None)
            for t in list(tasks):
                t.cancel()
            if running is not None and not running.done():
                running.cancel()
        if self.post_call_turn:
            with contextlib.suppress(Exception):
                await self._turn(key, frame_post_call(info, call.stop_reason), kind=TurnKind.POST_CALL)

    async def _turn(self, key: str, prompt: str, *, kind: TurnKind = TurnKind.SUPERVISOR) -> str:
        text = ""
        final = ""
        try:
            async for chunk in self.adapter.run_turn(key, prompt, kind=kind, resume_id=self.get_session(key)):
                if isinstance(chunk, TextDelta):
                    text += chunk.text
                elif isinstance(chunk, TurnDone):
                    if chunk.session_id:
                        self.set_session(key, chunk.session_id)
                    final = chunk.text
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("supervisor turn failed for %s", key)
            return ""
        self.turns_completed += 1
        return final or text
