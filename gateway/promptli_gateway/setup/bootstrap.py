"""Non-interactive, idempotent setup shared by every plugin (`<plugin> bootstrap --json`).

Given any key (admin, identity-scoped or provisional) it ensures the identity exists, optionally a
number, and stores ONLY an identity-scoped key locally (admin keys are used once to mint it, never saved).
Re-running converges to the same state (no duplicate identities, numbers or keys).
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any

from promptli import AsyncPromptli, PromptliError
from promptli_gateway.config import DEFAULT_BASE_URL, GatewayConfig


@dataclass
class BootstrapResult:
    status: str  # ok | requires_human | error
    identity: str | None = None
    email_address: str | None = None
    phone_number: str | None = None
    env: str | None = None
    key_kind: str | None = None
    actions: list[str] = field(default_factory=list[str])
    next_step: str | None = None
    error: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


async def bootstrap(
    *,
    runtime: str,
    api_key: str,
    identity: str | None,
    base_url: str = DEFAULT_BASE_URL,
    project_dir: str | None = None,
    phone_region: str | None = None,
    want_number: bool = False,
    extra: dict[str, Any] | None = None,
) -> BootstrapResult:
    res = BootstrapResult(status="ok")
    try:
        async with AsyncPromptli(
            api_key, base_url=base_url, user_agent=f"promptli-gateway/bootstrap {runtime}"
        ) as p:
            who = await p.whoami()
            res.env = who.env
            res.key_kind = who.key_kind
            stored_key = api_key
            if who.identity:
                handle = who.identity.handle
                if identity and identity not in (handle, who.identity.id):
                    return BootstrapResult(
                        status="error",
                        error={
                            "code": "identity_mismatch",
                            "message": f"This key belongs to @{handle}, not {identity}.",
                        },
                    )
            else:
                if not identity:
                    return BootstrapResult(
                        status="error",
                        error={
                            "code": "identity_required",
                            "message": "Organization keys need --identity <handle>.",
                        },
                    )
                up = await p.identities.upsert(identity)
                handle = up.identity.handle
                res.actions.append("created identity" if up.created else "identity exists")
                existing = GatewayConfig.load(runtime)
                if existing and existing.identity == handle and _has_valid_key(existing):
                    stored_key = existing.api_key()
                    res.actions.append("reused stored identity key")
                else:
                    minted = await p.api_keys.create(
                        f"{runtime} plugin @{socket.gethostname().split('.')[0]}",
                        identity=up.identity.id,
                        description="Minted by promptli-gateway bootstrap",
                    )
                    stored_key = minted.key
                    res.actions.append("minted identity-scoped key (admin key not stored)")

            async with AsyncPromptli(stored_key, base_url=base_url) as scoped:
                me = scoped.identity(handle)
                ident = await me.get()
                res.identity, res.email_address = handle, ident.email_address
                if who.key_kind == "provisional":
                    res.status = "requires_human"
                    res.next_step = (
                        "Ask your human for the 6-digit code emailed to them, then run "
                        f"`{runtime} verify <code>`."
                    )
                if want_number and who.key_kind != "provisional":
                    try:
                        num = (
                            await (p if not who.identity else scoped)
                            .identity(handle)
                            .provision_phone_number(region=phone_region)
                        )
                        res.phone_number = num.number
                        res.actions.append("phone number ready")
                    except PromptliError as e:
                        res.actions.append(f"phone number skipped: {e.code}")
                else:
                    st = await me.channel_status()
                    res.phone_number = st.sms.number

            cfg = GatewayConfig.load(runtime) or GatewayConfig(runtime=runtime, identity=handle)
            cfg.identity, cfg.base_url = handle, base_url
            if project_dir:
                cfg.project_dir = project_dir
            if extra:
                cfg.extra.update(extra)
            cfg.save(api_key=stored_key)
            res.actions.append(f"saved config to {cfg.dir}")
            return res
    except PromptliError as e:
        return BootstrapResult(status="error", error={"code": e.code, "message": e.message})


def _has_valid_key(cfg: GatewayConfig) -> bool:
    try:
        return cfg.api_key().startswith("psk_")
    except RuntimeError:
        return False
