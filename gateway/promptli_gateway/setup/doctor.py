"""`doctor`: verify config, key, channels, Agent Link reachability, clock skew and autostart."""

from __future__ import annotations

import asyncio
import email.utils
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from promptli import AsyncPromptli, PromptliError
from promptli_gateway.config import GatewayConfig
from promptli_gateway.setup.autostart import autostart_status


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list[Check])

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [c.__dict__ for c in self.checks]}


async def doctor(
    runtime: str,
    *,
    runtime_check: Callable[[], Awaitable[tuple[bool, str]]] | None = None,
    check_link: bool = True,
) -> DoctorReport:
    rep = DoctorReport()
    cfg = GatewayConfig.load(runtime)
    if cfg is None:
        rep.add("config", False, f"not configured: run `{runtime} setup` or `{runtime} bootstrap`")
        return rep
    rep.add("config", True, str(cfg.dir))
    try:
        key = cfg.api_key()
    except RuntimeError as e:
        rep.add("api key", False, str(e))
        return rep
    async with AsyncPromptli(key, base_url=cfg.base_url) as p:
        try:
            who = await p.whoami()
            rep.add(
                "api key",
                who.key_kind != "provisional",
                f"{who.env} key for @{who.identity.handle if who.identity else '?'}"
                + (" (provisional: verify the signup code)" if who.key_kind == "provisional" else ""),
            )
        except PromptliError as e:
            rep.add("api key", False, f"{e.code}: {e.message}")
            return rep
        try:
            st = await p.identity(cfg.identity).channel_status()
            rep.add("email", st.email.ready, st.email.address or "no mailbox")
            rep.add("sms", st.sms.ready, f"{st.sms.number or 'no number'} {st.sms.status or ''}".strip())
            for issue in st.issues:
                if issue.code != "no_event_delivery":
                    rep.add(f"issue:{issue.code}", False, issue.message)
        except PromptliError as e:
            rep.add("channels", False, f"{e.code}: {e.message}")
        if check_link:
            link = p.identity(cfg.identity).link(instance=f"{cfg.instance_id}-doctor", user_agent="doctor")
            try:
                await asyncio.wait_for(link.start(), timeout=15)
                rep.add("agent link", True, f"connected (latest seq {link.hello.get('latest_seq')})")
            except (TimeoutError, PromptliError) as e:
                rep.add("agent link", False, str(e))
            finally:
                await link.close()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(cfg.base_url.rstrip("/") + "/health")
            server = (
                email.utils.parsedate_to_datetime(r.headers["date"]).timestamp()
                if "date" in r.headers
                else None
            )
            if server:
                skew = abs(server - time.time())
                rep.add(
                    "clock",
                    skew < 60,
                    f"skew {skew:.0f}s" + ("" if skew < 60 else " (webhook signatures will fail)"),
                )
        except httpx.HTTPError as e:
            rep.add("api reachable", False, str(e))
    installed, detail = autostart_status(runtime)
    rep.add("autostart", installed, detail)
    if runtime_check:
        ok, detail = await runtime_check()
        rep.add("runtime", ok, detail)
    return rep
