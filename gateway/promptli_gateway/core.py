# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (event payloads are JSON dicts by design; the typed models live in the SDK)
"""The gateway loop: Agent Link events → one runtime session per person → reply on the same channel.

- Dedupe by event id (local sqlite) and ack a *low watermark* of fully processed seqs, so a crash in
  the middle of a turn never skips an event on restart.
- One FIFO worker per contact_key; bursts of texts from one person within `batch_window_s` become one turn.
- If the agent already replied with a tool (send_email / send_text / reply_email) to that person during
  the turn, the automatic reply is skipped (tool-delivery marking).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from promptli import AsyncPromptli, PromptliError
from promptli.link import AgentLink
from promptli_gateway.adapter import (
    GatewayContext,
    RuntimeAdapter,
    TextDelta,
    TurnDone,
    TurnError,
    TurnKind,
)
from promptli_gateway.config import GatewayConfig
from promptli_gateway.framing import frame_email, frame_texts, is_no_reply, split_sms, strip_markdown
from promptli_gateway.state import GatewayState
from promptli_gateway.tools.comms import comms_tools

log = logging.getLogger("promptli.gateway")

TURN_EVENTS = {"message.received", "text.received"}


@dataclass
class _Job:
    session_key: str
    channel: str  # email | sms
    events: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])


class Gateway:
    def __init__(
        self, config: GatewayConfig, adapter: RuntimeAdapter, client: AsyncPromptli | None = None
    ) -> None:
        self.cfg = config
        self.adapter = adapter
        self.client = client or AsyncPromptli(
            config.api_key(), base_url=config.base_url, user_agent=f"promptli-gateway {adapter.name}"
        )
        self.state = GatewayState(config.state_path)
        self.identity_address: str | None = None
        self.identity_number: str | None = None
        self.link: AgentLink | None = None
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._pending: set[int] = set()
        self._max_seen = 0
        self._sent_marks: dict[tuple[str, str], float] = {}
        self.turns_completed = 0
        self._stopping = asyncio.Event()

    # ---- lifecycle ----------------------------------------------------------------------------
    async def prepare(self) -> GatewayContext:
        me = self.client.identity(self.cfg.identity)
        identity = await me.get()
        status = await me.channel_status()
        self.identity_address = identity.email_address
        self.identity_number = status.sms.number
        ctx = GatewayContext(
            self.cfg.identity, identity.email_address, status.sms.number, self.cfg.project_dir
        )
        await self.adapter.start(ctx, comms_tools(self.client, self.cfg.identity, self._mark_sent))
        return ctx

    async def run(self) -> None:
        await self.prepare()
        self.link = self.client.identity(self.cfg.identity).link(
            instance=self.cfg.instance_id,
            capabilities=["channel_turns"],
            user_agent=f"promptli-gateway {self.adapter.name}",
        )
        async with self.link as link:
            log.info("gateway connected as @%s (cursor %s)", self.cfg.identity, link.hello.get("cursor"))
            consumer = asyncio.create_task(self._consume(link))
            await self._stopping.wait()
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
        await self.shutdown()

    def stop(self) -> None:
        self._stopping.set()

    async def shutdown(self) -> None:
        for t in self._workers.values():
            t.cancel()
        await self.adapter.close()
        await self.client.close()
        self.state.close()

    # ---- event intake -------------------------------------------------------------------------
    async def _consume(self, link: AgentLink) -> None:
        async for event in link:
            if "seq" not in event or "id" not in event:
                continue  # non-event frames (call frames arrive in M3)
            await self.handle_event(event)

    async def handle_event(self, event: dict[str, Any]) -> None:
        seq = int(event["seq"])
        self._max_seen = max(self._max_seen, seq)
        if self.state.seen(event["id"]):
            await self._advance()
            return
        job = self._route(event)
        if job is None:
            self.state.mark_processed(event["id"])
            await self._advance()
            return
        self._pending.add(seq)
        q = self._queues.setdefault(job.session_key, asyncio.Queue())
        await q.put(event)
        if job.session_key not in self._workers or self._workers[job.session_key].done():
            self._workers[job.session_key] = asyncio.create_task(self._worker(job.session_key, job.channel))

    def _route(self, event: dict[str, Any]) -> _Job | None:
        kind = event["type"]
        data = event.get("data", {})
        if kind == "message.received":
            msg = data["message"]
            if msg.get("folder") not in (None, "inbox"):
                return None
            if self.identity_address and msg["from"].lower() == self.identity_address.lower():
                return None  # our own loopback copy
            key = data.get("contact_key") or f"email:{msg['from'].lower()}"
            return _Job(key, "email")
        if kind == "text.received":
            if data.get("keyword"):
                return None  # STOP/START/HELP are handled by the platform
            conv = data.get("conversation") or {}
            key = (
                f"sms-group:{conv['id']}"
                if conv.get("is_group")
                else (data.get("contact_key") or f"sms:{data['text']['from']}")
            )
            return _Job(key, "sms")
        return None

    async def _advance(self) -> None:
        """Ack the highest seq below which everything is processed."""
        watermark = (min(self._pending) - 1) if self._pending else self._max_seen
        if watermark > self.state.cursor():
            self.state.set_cursor(watermark)
            if self.link is not None:
                await self.link.ack(watermark)

    # ---- per-contact worker ----------------------------------------------------------------------
    async def _worker(self, session_key: str, channel: str) -> None:
        q = self._queues[session_key]
        while True:
            try:
                first = await asyncio.wait_for(q.get(), timeout=300)
            except TimeoutError:
                return
            batch = [first]
            if channel == "sms":
                await asyncio.sleep(self.cfg.batch_window_s)
                while not q.empty():
                    batch.append(q.get_nowait())
            try:
                await self._run_turn(session_key, channel, batch)
            except Exception:
                log.exception("turn failed for %s", session_key)
            finally:
                for e in batch:
                    self.state.mark_processed(e["id"])
                    self._pending.discard(int(e["seq"]))
                await self._advance()

    def _mark_sent(self, channel: str, recipient: str) -> None:
        self._sent_marks[(channel, recipient)] = time.monotonic()

    async def _run_turn(self, session_key: str, channel: str, batch: list[dict[str, Any]]) -> None:
        if channel == "email":
            prompt = frame_email(batch[0], self.identity_address)
            reply_target = batch[0]["data"]["message"]["from"].lower()
        else:
            prompt = frame_texts(batch, self.identity_number)
            reply_target = batch[0]["data"]["text"]["from"]
        started = time.monotonic()
        text_parts: list[str] = []
        final = ""
        async for chunk in self.adapter.run_turn(
            session_key, prompt, kind=TurnKind.CHANNEL, resume_id=self.state.session(session_key)
        ):
            if isinstance(chunk, TextDelta):
                text_parts.append(chunk.text)
            elif isinstance(chunk, TurnDone):
                final = chunk.text or "".join(text_parts)
                if chunk.session_id:
                    self.state.set_session(session_key, chunk.session_id)
            elif isinstance(chunk, TurnError):
                log.warning("turn error for %s: %s", session_key, chunk.message)
                return
            else:
                log.debug("tool %s %s", chunk.name, chunk.status)
        self.turns_completed += 1
        if not self.cfg.auto_reply or is_no_reply(final):
            return
        replied = self._sent_marks.get((channel, reply_target), 0.0) >= started
        if replied:
            return
        try:
            await self._deliver(channel, batch, final)
        except PromptliError as err:
            log.warning("auto-reply failed (%s): %s", err.code, err.message)

    async def _deliver(self, channel: str, batch: list[dict[str, Any]], text: str) -> None:
        me = self.client.identity(self.cfg.identity)
        if channel == "email":
            await me.reply(batch[-1]["data"]["message"]["id"], text=text.strip())
            return
        data = batch[-1]["data"]
        conv = data.get("conversation") or {}
        recipients = conv.get("remotes") if conv.get("is_group") else [data["text"]["from"]]
        for part in split_sms(strip_markdown(text)):
            await me.send_text(recipients or [data["text"]["from"]], part)
