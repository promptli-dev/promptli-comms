"""`AsyncIdentity`: everything an agent does as one identity (email, SMS, numbers, events, rules)."""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from promptli._generated import models as m
from promptli.link import AgentLink

if TYPE_CHECKING:
    from promptli._http import Transport

Folder = Literal["inbox", "sent", "drafts", "spam", "blocked", "archive", "trash"]
AttachmentLike = str | Path | tuple[str, bytes] | tuple[str, bytes, str]


def _attachment(a: AttachmentLike) -> dict[str, Any]:
    if isinstance(a, (str, Path)):
        p = Path(a)
        data, name = p.read_bytes(), p.name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    elif len(a) == 2:
        name, data = a
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    else:
        name, data, ctype = a
    return {"filename": name, "content_type": ctype, "content_base64": base64.b64encode(data).decode()}


def _list(v: str | Sequence[str] | None) -> list[str]:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


class AsyncIdentity:
    def __init__(self, transport: Transport, ref: str) -> None:
        self._t = transport
        self.ref = ref

    def _p(self, suffix: str = "") -> str:
        return f"/v1/identities/{self.ref}{suffix}"

    # ---- identity ----------------------------------------------------------------------------
    async def get(self) -> m.Identity:
        return m.Identity.model_validate(await self._t.request("GET", self._p()))

    async def update(self, *, display_name: str | None = None, description: str | None = None) -> m.Identity:
        body = {
            k: v
            for k, v in {"display_name": display_name, "description": description}.items()
            if v is not None
        }
        return m.Identity.model_validate(await self._t.request("PATCH", self._p(), json=body))

    async def delete(self) -> None:
        await self._t.request("DELETE", self._p())

    async def channel_status(self) -> m.ChannelStatus:
        return m.ChannelStatus.model_validate(await self._t.request("GET", self._p("/channel-status")))

    # ---- email -------------------------------------------------------------------------------
    async def send_email(
        self,
        to: str | Sequence[str],
        subject: str = "",
        *,
        text: str | None = None,
        html: str | None = None,
        cc: str | Sequence[str] | None = None,
        bcc: str | Sequence[str] | None = None,
        attachments: Sequence[AttachmentLike] = (),
        idempotency_key: str | None = None,
    ) -> m.Message:
        body = {
            "to": _list(to),
            "cc": _list(cc),
            "bcc": _list(bcc),
            "subject": subject,
            "text": text,
            "html": html,
            "attachments": [_attachment(a) for a in attachments],
        }
        return m.Message.model_validate(
            await self._t.request(
                "POST", self._p("/mail/messages"), json=body, idempotency_key=idempotency_key
            )
        )

    async def reply(
        self,
        message_id: str,
        *,
        text: str | None = None,
        html: str | None = None,
        all: bool = False,
        attachments: Sequence[AttachmentLike] = (),
        idempotency_key: str | None = None,
    ) -> m.Message:
        path = self._p(f"/mail/messages/{message_id}/{'reply-all' if all else 'reply'}")
        body = {"text": text, "html": html, "attachments": [_attachment(a) for a in attachments]}
        return m.Message.model_validate(
            await self._t.request("POST", path, json=body, idempotency_key=idempotency_key)
        )

    async def forward(
        self, message_id: str, to: str | Sequence[str], *, text: str | None = None
    ) -> m.Message:
        path = self._p(f"/mail/messages/{message_id}/forward")
        return m.Message.model_validate(
            await self._t.request("POST", path, json={"to": _list(to), "text": text})
        )

    async def emails(
        self,
        *,
        folder: Folder | None = None,
        direction: Literal["inbound", "outbound"] | None = None,
        limit: int = 25,
    ) -> AsyncIterator[m.Message]:
        """Iterate messages newest first (bodies omitted; call `email(id)` for text/html)."""
        cursor: str | None = None
        while True:
            page = await self._t.request(
                "GET",
                self._p("/mail/messages"),
                params={"folder": folder, "direction": direction, "limit": limit, "cursor": cursor},
            )
            for item in page["data"]:
                yield m.Message.model_validate(item)
            cursor = page.get("next_cursor")
            if not cursor:
                return

    async def email(self, message_id: str) -> m.Message:
        return m.Message.model_validate(await self._t.request("GET", self._p(f"/mail/messages/{message_id}")))

    async def mark_email(
        self, message_id: str, *, read: bool | None = None, folder: Folder | None = None
    ) -> m.Message:
        body = {k: v for k, v in {"read": read, "folder": folder}.items() if v is not None}
        return m.Message.model_validate(
            await self._t.request("PATCH", self._p(f"/mail/messages/{message_id}"), json=body)
        )

    async def threads(self, *, folder: Folder | None = None, limit: int = 25) -> list[m.Thread]:
        page = await self._t.request(
            "GET", self._p("/mail/threads"), params={"folder": folder, "limit": limit}
        )
        return [m.Thread.model_validate(x) for x in page["data"]]

    async def thread(self, thread_id: str) -> m.ThreadDetail:
        return m.ThreadDetail.model_validate(
            await self._t.request("GET", self._p(f"/mail/threads/{thread_id}"))
        )

    async def search_email(self, q: str, *, limit: int = 25) -> list[m.Message]:
        res = await self._t.request("GET", self._p("/mail/search"), params={"q": q, "limit": limit})
        return [m.Message.model_validate(x) for x in res["data"]]

    async def create_draft(self, **fields: Any) -> m.Draft:
        return m.Draft.model_validate(await self._t.request("POST", self._p("/mail/drafts"), json=fields))

    async def update_draft(self, draft_id: str, *, generation: int | None = None, **fields: Any) -> m.Draft:
        body = {**fields, **({"generation": generation} if generation is not None else {})}
        return m.Draft.model_validate(
            await self._t.request("PATCH", self._p(f"/mail/drafts/{draft_id}"), json=body)
        )

    async def send_draft(self, draft_id: str) -> m.Message:
        return m.Message.model_validate(
            await self._t.request("POST", self._p(f"/mail/drafts/{draft_id}/send"))
        )

    # ---- phone number & SMS ----------------------------------------------------------------
    async def provision_phone_number(
        self, *, region: str | None = None, area_code: str | None = None, number: str | None = None
    ) -> m.PhoneNumber:
        body = {"region": region, "area_code": area_code, "number": number}
        return m.PhoneNumber.model_validate(
            await self._t.request(
                "POST", self._p("/phone-number"), json=body, idempotency_key=f"provision:{self.ref}"
            )
        )

    async def release_phone_number(self) -> None:
        await self._t.request("DELETE", self._p("/phone-number"))

    async def send_text(
        self,
        to: str | Sequence[str],
        text: str = "",
        *,
        media: Sequence[AttachmentLike] = (),
        idempotency_key: str | None = None,
    ) -> m.Text:
        body = {
            "to": _list(to),
            "body": text,
            "media": [
                {
                    "content_type": a["content_type"],
                    "content_base64": a["content_base64"],
                    "filename": a["filename"],
                }
                for a in (_attachment(x) for x in media)
            ],
        }
        return m.Text.model_validate(
            await self._t.request("POST", self._p("/texts"), json=body, idempotency_key=idempotency_key)
        )

    async def texts(self, *, conversation_id: str | None = None, limit: int = 50) -> list[m.Text]:
        page = await self._t.request(
            "GET", self._p("/texts"), params={"conversation_id": conversation_id, "limit": limit}
        )
        return [m.Text.model_validate(x) for x in page["data"]]

    async def text_conversations(self, *, limit: int = 50) -> list[m.TextConversation]:
        page = await self._t.request("GET", self._p("/texts/conversations"), params={"limit": limit})
        return [m.TextConversation.model_validate(x) for x in page["data"]]

    # ---- calls (voice) --------------------------------------------------------------------------
    async def place_call(
        self,
        to: str,
        *,
        reason: str | None = None,
        opening: str | None = None,
        context: str | None = None,
        voicemail_message: str | None = None,
        mode: Literal["standard", "hosted"] | None = None,
        sandbox_script: Sequence[dict[str, Any]] | None = None,
        idempotency_key: str | None = None,
    ) -> m.Call:
        """Call a number. Your agent (connected over Agent Link with the voice capability) runs the call.

        `opening` is spoken as soon as the callee answers; `voicemail_message` is left on a machine.
        `mode="hosted"` lets the Promptli assistant talk from the call brief (Hosted, $0.10/min) while your
        agent supervises; the default is your agent talking (Standard, $0.05/min).
        In test mode nothing is dialled: `sandbox_script` simulates the callee.
        """
        body: dict[str, Any] = {
            "to": to,
            "reason": reason,
            "opening": opening,
            "context": context,
            "voicemail_message": voicemail_message,
            "mode": mode,
        }
        if sandbox_script is not None:
            body["sandbox_script"] = list(sandbox_script)
        return m.Call.model_validate(
            await self._t.request("POST", self._p("/calls"), json=body, idempotency_key=idempotency_key)
        )

    async def calls(self, *, limit: int = 25) -> list[m.Call]:
        page = await self._t.request("GET", self._p("/calls"), params={"limit": limit})
        return [m.Call.model_validate(x) for x in page["data"]]

    async def search_calls(self, q: str, *, limit: int = 20) -> list[m.TranscriptHit]:
        res = await self._t.request("GET", self._p("/calls/search"), params={"q": q, "limit": limit})
        return [m.TranscriptHit.model_validate(x) for x in res["data"]]

    async def voice_settings(self) -> m.VoiceSettings:
        return m.VoiceSettings.model_validate(await self._t.request("GET", self._p("/voice")))

    async def update_voice_settings(self, **changes: Any) -> m.VoiceSettings:
        """Change some fields (the rest are kept): incoming_action, forward_to, agent_unreachable, voice_id,
        language, speed, recording, fillers, greeting."""
        current = (await self.voice_settings()).model_dump(mode="json")
        current.update(changes)
        return m.VoiceSettings.model_validate(await self._t.request("PUT", self._p("/voice"), json=current))

    # ---- scheduled calls and texts ------------------------------------------------------------
    async def schedule_call(self, to: str, run_at: str, **fields: Any) -> m.Scheduled:
        """Place a call later. `run_at` is ISO 8601 with a timezone; fields as for place_call (reason,
        opening, context, voicemail_message, mode)."""
        body = {"run_at": run_at, "call": {"to": to, **{k: v for k, v in fields.items() if v is not None}}}
        return m.Scheduled.model_validate(await self._t.request("POST", self._p("/scheduled"), json=body))

    async def schedule_text(self, to: str | list[str], body: str, run_at: str) -> m.Scheduled:
        payload = {"run_at": run_at, "text": {"to": to, "body": body}}
        return m.Scheduled.model_validate(await self._t.request("POST", self._p("/scheduled"), json=payload))

    async def scheduled(self, *, include_done: bool = False) -> list[m.Scheduled]:
        res = await self._t.request("GET", self._p("/scheduled"), params={"include_done": include_done})
        return [m.Scheduled.model_validate(x) for x in res["data"]]

    async def cancel_scheduled(self, scheduled_id: str) -> m.Scheduled:
        return m.Scheduled.model_validate(await self._t.request("DELETE", f"/v1/scheduled/{scheduled_id}"))

    async def hosted_settings(self) -> m.HostedSettings:
        """How the Promptli assistant behaves on hosted calls (persona, instructions, facts, tools)."""
        return m.HostedSettings.model_validate(await self._t.request("GET", self._p("/hosted")))

    async def update_hosted_settings(self, **changes: Any) -> m.HostedSettings:
        """Change some fields (the rest are kept): persona, instructions, goal, facts, policies, tools."""
        current = (await self.hosted_settings()).model_dump(mode="json")
        current.update(changes)
        return m.HostedSettings.model_validate(await self._t.request("PUT", self._p("/hosted"), json=current))

    # ---- unified conversations ----------------------------------------------------------------
    async def conversations(self, *, limit: int = 50) -> list[m.ConversationSummary]:
        res = await self._t.request("GET", self._p("/conversations"), params={"limit": limit})
        return [m.ConversationSummary.model_validate(x) for x in res["data"]]

    async def conversation(self, contact_key: str, *, limit: int = 50) -> m.ConversationDetail:
        res = await self._t.request("GET", self._p(f"/conversations/{contact_key}"), params={"limit": limit})
        return m.ConversationDetail.model_validate(res)

    # ---- rules & webhooks ---------------------------------------------------------------------
    async def contact_rules(self, channel: Literal["email", "phone"] | None = None) -> m.ContactRuleList:
        return m.ContactRuleList.model_validate(
            await self._t.request("GET", self._p("/contact-rules"), params={"channel": channel})
        )

    async def add_contact_rule(
        self,
        *,
        channel: Literal["email", "phone"],
        action: Literal["allow", "block"],
        target: str,
        match_type: Literal["exact_email", "domain", "exact_phone"] | None = None,
        direction: Literal["inbound", "outbound", "both"] = "both",
    ) -> m.ContactRule:
        mt = match_type or (
            "exact_phone" if channel == "phone" else ("exact_email" if "@" in target else "domain")
        )
        body = {
            "channel": channel,
            "action": action,
            "target": target,
            "match_type": mt,
            "direction": direction,
        }
        return m.ContactRule.model_validate(
            await self._t.request("POST", self._p("/contact-rules"), json=body)
        )

    async def rotate_signing_key(self, *, grace_hours: int = 24) -> str:
        res = await self._t.request("POST", self._p("/signing-key"), params={"grace_hours": grace_hours})
        return str(res["signing_secret"])

    # ---- events ----------------------------------------------------------------------------
    async def events(
        self, *, after: int = 0, types: Sequence[str] | None = None, limit: int = 100
    ) -> m.EventPage:
        res = await self._t.request(
            "GET",
            "/v1/events",
            params={
                "identity": self.ref,
                "after": after,
                "limit": limit,
                "types": ",".join(types) if types else None,
            },
        )
        return m.EventPage.model_validate(res)

    def link(
        self,
        *,
        instance: str,
        capabilities: Sequence[str] = (),
        resume_after: int | None = None,
        user_agent: str | None = None,
    ) -> AgentLink:
        """Open an Agent Link (use as `async with`). Identity-scoped keys may omit nothing; admin keys
        are routed with ?identity=."""
        return AgentLink(
            self._t.link_url,
            self._t.api_key,
            instance=instance,
            identity=self.ref,
            capabilities=list(capabilities),
            resume_after=resume_after,
            user_agent=user_agent,
        )
