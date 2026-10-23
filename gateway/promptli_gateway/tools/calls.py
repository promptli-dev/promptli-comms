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
    client: AsyncPromptli, identity: str, live_call: Callable[[str], LiveCall | None]
) -> list[ToolSpec]:
    me = client.identity(identity)

    async def place_call(a: dict[str, Any]) -> dict[str, Any]:
        try:
            c = await me.place_call(
                a["to"],
                reason=a.get("reason"),
                opening=a.get("opening"),
                context=a.get("context"),
                voicemail_message=a.get("voicemail_message"),
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

    return [
        ToolSpec(
            "place_call",
            "Phone someone. You will run the conversation live when they answer. `opening` is spoken first; "
            "`voicemail_message` is left if a machine answers.",
            obj(
                {"to": STR, "reason": STR, "opening": STR, "context": STR, "voicemail_message": STR},
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
