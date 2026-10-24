# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Call tools: place calls, read past calls, and control a live call (end, transfer, keypad)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from promptli import AsyncPromptli, PromptliError
from promptli.calls_live import LiveCall
from promptli_gateway.tools import STR, ToolSpec, obj


def _err(e: PromptliError) -> dict[str, Any]:
    return {"ok": False, "error": e.code, "message": e.message}


def call_tools(
    client: AsyncPromptli,
    identity: str,
    live_call: Callable[[str], LiveCall | None],
    supervised_call: Callable[[str], LiveCall | None] | None = None,
) -> list[ToolSpec]:
    me = client.identity(identity)

    def _no_call(_cid: str) -> LiveCall | None:
        return None

    watching = supervised_call or _no_call

    async def place_call(a: dict[str, Any]) -> dict[str, Any]:
        try:
            c = await me.place_call(
                a["to"],
                reason=a.get("reason"),
                opening=a.get("opening"),
                context=a.get("context"),
                voicemail_message=a.get("voicemail_message"),
                mode="hosted" if a.get("hosted") else None,
            )
            return {"ok": True, "call_id": c.id, "status": c.status}
        except PromptliError as e:
            return _err(e)

    async def list_calls(a: dict[str, Any]) -> dict[str, Any]:
        rows = await me.calls(limit=int(a.get("limit", 10)))
        return {"ok": True, "calls": [r.model_dump(mode="json", by_alias=True) for r in rows]}

    async def get_call_transcript(a: dict[str, Any]) -> dict[str, Any]:
        try:
            t = await client.calls.transcript(a["call_id"])
            return {"ok": True, "segments": [{"party": s.party, "text": s.text} for s in t.segments]}
        except PromptliError as e:
            return _err(e)

    def _live(a: dict[str, Any]) -> LiveCall | None:
        return live_call(str(a.get("call_id") or ""))

    async def end_call(a: dict[str, Any]) -> dict[str, Any]:
        call = _live(a)
        if call is None:
            return {"ok": False, "error": "call_not_active"}
        await call.hangup(say=a.get("say"))
        return {"ok": True}

    async def transfer_call(a: dict[str, Any]) -> dict[str, Any]:
        call = _live(a)
        if call is None:
            return {"ok": False, "error": "call_not_active"}
        await call.transfer(a["to"], say=a.get("say"))
        return {"ok": True}

    async def send_dtmf(a: dict[str, Any]) -> dict[str, Any]:
        call = _live(a)
        if call is None:
            return {"ok": False, "error": "call_not_active"}
        await call.dtmf(a["digits"])
        return {"ok": True}

    # ---- supervising hosted calls (the Promptli assistant talks; you steer) -----------------------------
    def _sup(a: dict[str, Any]) -> LiveCall | None:
        return watching(str(a.get("call_id") or ""))

    async def call_get_brief(a: dict[str, Any]) -> dict[str, Any]:
        try:
            b = await client.calls.brief(a["call_id"])
            return {"ok": True, "version": b.version, "brief": b.brief}
        except PromptliError as e:
            return _err(e)

    async def call_add_fact(a: dict[str, Any]) -> dict[str, Any]:
        try:
            b = await client.calls.add_fact(a["call_id"], str(a["text"]))
            return {"ok": True, "version": b.version}
        except PromptliError as e:
            return _err(e)

    async def call_guidance(a: dict[str, Any]) -> dict[str, Any]:
        call = _sup(a)
        if call is not None:
            await call.guide(str(a["text"]))
            return {"ok": True}
        try:
            await client.calls.guide(a["call_id"], str(a["text"]))
            return {"ok": True}
        except PromptliError as e:
            return _err(e)

    async def call_say(a: dict[str, Any]) -> dict[str, Any]:
        now = bool(a.get("now"))
        call = _sup(a)
        if call is not None:
            await call.speak(str(a["text"]), now=now)
            return {"ok": True}
        try:
            await client.calls.say(a["call_id"], str(a["text"]), mode="now" if now else "next")
            return {"ok": True}
        except PromptliError as e:
            return _err(e)

    async def call_take_over(a: dict[str, Any]) -> dict[str, Any]:
        call = _sup(a)
        if call is None:
            return {"ok": False, "error": "not_supervising"}
        await call.take_over()
        return {"ok": True, "note": "You talk from the caller's next turn; call_hand_back when done."}

    async def call_hand_back(a: dict[str, Any]) -> dict[str, Any]:
        call = _sup(a)
        if call is None:
            return {"ok": False, "error": "not_supervising"}
        await call.hand_back()
        return {"ok": True}

    supervise_tools = [
        ToolSpec(
            "call_get_brief",
            "Read the brief the hosted voice assistant works from (goal, facts, guidance, policies).",
            obj({"call_id": STR}, ["call_id"]),
            call_get_brief,
            read_only=True,
            tags=("voice", "supervise"),
        ),
        ToolSpec(
            "call_add_fact",
            "Give the hosted assistant a fact it can rely on for the rest of the call "
            "(e.g. 'Friday 4pm is free').",
            obj({"call_id": STR, "text": STR}, ["call_id", "text"]),
            call_add_fact,
            tags=("voice", "supervise"),
        ),
        ToolSpec(
            "call_guidance",
            "Private guidance for the assistant's next reply (e.g. 'offer the 4pm slot'). "
            "The caller never hears it.",
            obj({"call_id": STR, "text": STR}, ["call_id", "text"]),
            call_guidance,
            tags=("voice", "supervise"),
        ),
        ToolSpec(
            "call_say",
            "Say exact words on the call: after the current sentence, or now=true to interrupt. "
            "Use sparingly.",
            obj({"call_id": STR, "text": STR, "now": {"type": "boolean"}}, ["call_id", "text"]),
            call_say,
            tags=("voice", "supervise"),
        ),
        ToolSpec(
            "call_take_over",
            "Take over the call and speak every turn yourself (billed Standard). Hand back when done.",
            obj({"call_id": STR}, ["call_id"]),
            call_take_over,
            tags=("voice", "supervise"),
        ),
        ToolSpec(
            "call_hand_back",
            "Let the hosted assistant continue the call after a take-over.",
            obj({"call_id": STR}, ["call_id"]),
            call_hand_back,
            tags=("voice", "supervise"),
        ),
    ]

    base_tools = [
        ToolSpec(
            "place_call",
            "Phone someone. You run the conversation live when they answer (or hosted=true: the Promptli "
            "assistant talks from a brief built from reason/context and you supervise). `opening` is spoken "
            "first; `voicemail_message` is left if a machine answers.",
            obj(
                {
                    "to": STR,
                    "reason": STR,
                    "opening": STR,
                    "context": STR,
                    "voicemail_message": STR,
                    "hosted": {"type": "boolean"},
                },
                ["to", "reason"],
            ),
            place_call,
            tags=("voice",),
        ),
        ToolSpec(
            "list_calls",
            "Recent calls with status, outcome and duration.",
            obj({"limit": {"type": "integer"}}),
            list_calls,
            read_only=True,
            tags=("voice",),
        ),
        ToolSpec(
            "get_call_transcript",
            "The transcript of a call (caller and agent turns).",
            obj({"call_id": STR}, ["call_id"]),
            get_call_transcript,
            read_only=True,
            tags=("voice",),
        ),
        ToolSpec(
            "end_call",
            "Hang up the live call, optionally saying a last line first (e.g. a goodbye).",
            obj({"call_id": STR, "say": STR}, ["call_id"]),
            end_call,
            tags=("voice",),
        ),
        ToolSpec(
            "transfer_call",
            "Transfer the live call to another phone number (E.164), optionally saying something first.",
            obj({"call_id": STR, "to": STR, "say": STR}, ["call_id", "to"]),
            transfer_call,
            tags=("voice",),
        ),
        ToolSpec(
            "send_dtmf",
            "Press keypad keys on the live call (navigate a phone menu), e.g. '1' or '1234#'.",
            obj({"call_id": STR, "digits": STR}, ["call_id", "digits"]),
            send_dtmf,
            tags=("voice",),
        ),
    ]
    return [*supervise_tools, *base_tools]
