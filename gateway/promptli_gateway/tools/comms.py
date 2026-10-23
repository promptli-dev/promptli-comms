# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# (event payloads are JSON dicts by design; the typed models live in the SDK)
"""Communication tools (email, SMS, contacts, conversations) built on the SDK.

Each send records a "delivered to <contact>" mark so the gateway does not also auto-reply.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from promptli import AsyncPromptli, PromptliError
from promptli_gateway.tools import STR, STR_LIST, ToolSpec, obj


def _err(e: PromptliError) -> dict[str, Any]:
    return {"ok": False, "error": e.code, "message": e.message, "details": e.details}


def comms_tools(client: AsyncPromptli, identity: str, on_send: Callable[[str, str], None]) -> list[ToolSpec]:
    """on_send(channel, recipient) is called after every successful send (tool-delivery marking)."""
    me = client.identity(identity)

    async def send_email(a: dict[str, Any]) -> dict[str, Any]:
        try:
            to = a["to"] if isinstance(a["to"], list) else [a["to"]]
            m = await me.send_email(
                to, a.get("subject", ""), text=a.get("text"), html=a.get("html"), cc=a.get("cc")
            )
            for r in to:
                on_send("email", r.lower())
            return {"ok": True, "message_id": m.id, "status": m.status}
        except PromptliError as e:
            return _err(e)

    async def reply_email(a: dict[str, Any]) -> dict[str, Any]:
        try:
            m = await me.reply(
                a["message_id"], text=a.get("text"), html=a.get("html"), all=bool(a.get("reply_all"))
            )
            for r in m.to:
                on_send("email", r.lower())
            return {"ok": True, "message_id": m.id, "status": m.status}
        except PromptliError as e:
            return _err(e)

    async def send_text(a: dict[str, Any]) -> dict[str, Any]:
        try:
            to = a["to"] if isinstance(a["to"], list) else [a["to"]]
            t = await me.send_text(to, a["text"])
            for r in to:
                on_send("sms", r)
            return {"ok": True, "text_id": t.id, "status": t.status}
        except PromptliError as e:
            return _err(e)

    async def list_conversations(a: dict[str, Any]) -> dict[str, Any]:
        rows = await me.conversations(limit=int(a.get("limit", 20)))
        return {"ok": True, "conversations": [r.model_dump(mode="json") for r in rows]}

    async def get_conversation(a: dict[str, Any]) -> dict[str, Any]:
        try:
            c = await me.conversation(a["contact_key"], limit=int(a.get("limit", 30)))
            return {"ok": True, **c.model_dump(mode="json")}
        except PromptliError as e:
            return _err(e)

    async def read_email(a: dict[str, Any]) -> dict[str, Any]:
        try:
            m = await me.email(a["message_id"])
            return {"ok": True, **m.model_dump(mode="json", by_alias=True, exclude={"html"})}
        except PromptliError as e:
            return _err(e)

    async def search_email(a: dict[str, Any]) -> dict[str, Any]:
        rows = await me.search_email(a["query"], limit=int(a.get("limit", 10)))
        return {
            "ok": True,
            "messages": [r.model_dump(mode="json", by_alias=True, exclude={"html", "text"}) for r in rows],
        }

    async def lookup_contact(a: dict[str, Any]) -> dict[str, Any]:
        c = await client.contacts.lookup(email=a.get("email"), phone=a.get("phone"))
        return {"ok": True, "contact": c.model_dump(mode="json") if c else None}

    async def add_contact_memory(a: dict[str, Any]) -> dict[str, Any]:
        try:
            c = await client.contacts.add_memory(a["contact_id"], a["memory"])
            return {"ok": True, "contact": c.model_dump(mode="json")}
        except PromptliError as e:
            return _err(e)

    async def channel_status(_: dict[str, Any]) -> dict[str, Any]:
        st = await me.channel_status()
        return {"ok": True, **st.model_dump(mode="json")}

    return [
        ToolSpec(
            "send_email",
            "Send a new email from your agent mailbox.",
            obj({"to": STR_LIST, "subject": STR, "text": STR, "html": STR, "cc": STR_LIST}, ["to", "text"]),
            send_email,
            tags=("email",),
        ),
        ToolSpec(
            "reply_email",
            "Reply to an email (threaded). Use reply_all to include everyone.",
            obj(
                {"message_id": STR, "text": STR, "html": STR, "reply_all": {"type": "boolean"}},
                ["message_id", "text"],
            ),
            reply_email,
            tags=("email",),
        ),
        ToolSpec(
            "send_text",
            "Send an SMS from your agent's phone number (E.164 recipients; a list = group text).",
            obj({"to": STR_LIST, "text": STR}, ["to", "text"]),
            send_text,
            tags=("sms",),
        ),
        ToolSpec(
            "list_conversations",
            "People you talk to, most recent first, across email, SMS and calls.",
            obj({"limit": {"type": "integer"}}),
            list_conversations,
            read_only=True,
        ),
        ToolSpec(
            "get_conversation",
            "Full timeline (email, SMS, calls) and contact card for one person.",
            obj({"contact_key": STR, "limit": {"type": "integer"}}, ["contact_key"]),
            get_conversation,
            read_only=True,
        ),
        ToolSpec(
            "read_email",
            "Read one email including its text body.",
            obj({"message_id": STR}, ["message_id"]),
            read_email,
            read_only=True,
        ),
        ToolSpec(
            "search_email",
            "Full-text search your mailbox.",
            obj({"query": STR, "limit": {"type": "integer"}}, ["query"]),
            search_email,
            read_only=True,
        ),
        ToolSpec(
            "lookup_contact",
            "Find a contact by email or phone.",
            obj({"email": STR, "phone": STR}),
            lookup_contact,
            read_only=True,
        ),
        ToolSpec(
            "add_contact_memory",
            "Remember a fact about a contact (shared with every agent and calls).",
            obj({"contact_id": STR, "memory": STR}, ["contact_id", "memory"]),
            add_contact_memory,
        ),
        ToolSpec(
            "channel_status",
            "Check which channels (email, SMS, voice, Agent Link) are ready.",
            obj({}),
            channel_status,
            read_only=True,
        ),
    ]
