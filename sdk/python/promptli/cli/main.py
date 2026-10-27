"""`promptli` command line. Every command supports --json for scripting.

Credentials: PROMPTLI_API_KEY / PROMPTLI_BASE_URL env vars, else ~/.promptli/credentials
(written by `promptli login`).
"""

from __future__ import annotations

import asyncio
import enum
import json
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from promptli import AsyncPromptli, PromptliError
from promptli._http import DEFAULT_BASE_URL
from promptli.auth import OAuthCredential, OAuthToken, browser_login, credential_auth, save_credential

app = typer.Typer(help="Promptli Comms: email, SMS and phone calls for AI agents.", no_args_is_help=True)
identities_app = typer.Typer(help="Agent identities", no_args_is_help=True)
email_app = typer.Typer(help="Email", no_args_is_help=True)
text_app = typer.Typer(help="SMS / MMS", no_args_is_help=True)
number_app = typer.Typer(help="Phone numbers", no_args_is_help=True)
events_app = typer.Typer(help="Events (poll or live via Agent Link)", no_args_is_help=True)
webhooks_app = typer.Typer(help="Webhook subscriptions and deliveries", no_args_is_help=True)
keys_app = typer.Typer(help="API keys", no_args_is_help=True)
contacts_app = typer.Typer(help="Contacts", no_args_is_help=True)
sandbox_app = typer.Typer(help="Test-mode simulators (psk_test_ keys)", no_args_is_help=True)
signup_app = typer.Typer(help="Agent self-signup", no_args_is_help=True)
call_app = typer.Typer(help="Phone calls (voice)", no_args_is_help=True)
for sub, name in [
    (identities_app, "identities"),
    (email_app, "email"),
    (text_app, "text"),
    (number_app, "number"),
    (events_app, "events"),
    (webhooks_app, "webhooks"),
    (keys_app, "keys"),
    (contacts_app, "contacts"),
    (sandbox_app, "sandbox"),
    (signup_app, "signup"),
    (call_app, "call"),
]:
    app.add_typer(sub, name=name)

T = TypeVar("T")


class DeliveryStatus(enum.StrEnum):
    pending = "pending"
    succeeded = "succeeded"
    retrying = "retrying"
    failed = "failed"


console = Console()
CRED_PATH = Path.home() / ".promptli" / "credentials"
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output")]
IdentityOpt = Annotated[str, typer.Option("--identity", "-i", help="Handle or idn_ id")]


# ---- plumbing --------------------------------------------------------------------------------


def _base_url() -> str | None:
    base = os.environ.get("PROMPTLI_BASE_URL")
    if not base and CRED_PATH.exists():
        stored: dict[str, Any] = json.loads(CRED_PATH.read_text())
        oauth = OAuthCredential.from_dict(stored)
        base = oauth.base_url if oauth else stored.get("base_url")
    return base


def _client() -> AsyncPromptli:
    auth = credential_auth(CRED_PATH, env_key=os.environ.get("PROMPTLI_API_KEY"))
    if auth is None:
        console.print("[red]Not logged in.[/] Run `promptli login` (or set PROMPTLI_API_KEY).")
        raise typer.Exit(2)
    return AsyncPromptli(auth, base_url=_base_url(), user_agent="promptli-cli")


def _dump(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", by_alias=True)
    if isinstance(obj, list):
        return [_dump(x) for x in obj]  # pyright: ignore[reportUnknownVariableType]
    return obj


def _out(obj: Any, as_json: bool, columns: list[str] | None = None, title: str | None = None) -> None:
    data = _dump(obj)
    if as_json or columns is None:
        console.print_json(json.dumps(data, default=str))
        return
    rows: list[dict[str, Any]] = data if isinstance(data, list) else [data]  # pyright: ignore[reportUnknownVariableType]
    table = Table(title=title, header_style="bold #F04A28")
    for c in columns:
        table.add_column(c.rstrip("_"))  # python-safe names like from_ are serialized as "from"
    for r in rows:
        values = [r.get(c, r.get(c.rstrip("_"))) for c in columns]
        table.add_row(*["" if v is None else str(v) for v in values])
    console.print(table)


def _run(coro: Awaitable[T]) -> T:
    try:
        return asyncio.run(coro)  # type: ignore[arg-type]
    except PromptliError as err:
        console.print(
            f"[red]{err.code}[/]: {err.message}" + (f"  [dim]({err.request_id})[/]" if err.request_id else "")
        )
        raise typer.Exit(1) from err


async def _with(fn: Callable[[AsyncPromptli], Awaitable[T]]) -> T:
    async with _client() as p:
        return await fn(p)


# ---- account ---------------------------------------------------------------------------------


@app.command()
def login(
    api_key: Annotated[
        bool, typer.Option("--api-key", help="Paste an API key instead of logging in with the browser")
    ] = False,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the login link and paste the code (SSH, headless)")
    ] = False,
    identity: Annotated[str | None, typer.Option("--identity", "-i", help="Agent to preselect")] = None,
    number: Annotated[
        bool, typer.Option("--number", help="Offer to get a phone number for the agent")
    ] = False,
    test: Annotated[
        bool, typer.Option("--test", help="Start in Test mode (simulated, nothing is sent)")
    ] = False,
    base_url: Annotated[str | None, typer.Option(help="API base URL (default production)")] = None,
) -> None:
    """Log in with your browser and pick (or create) an agent. Saves ~/.promptli/credentials (mode 600)."""
    base = (base_url or os.environ.get("PROMPTLI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    if api_key:
        key = typer.prompt("API key (psk_live_… or psk_test_…, input hidden)", hide_input=True).strip()

        async def check_key() -> Any:
            async with AsyncPromptli(key, base_url=base, user_agent="promptli-cli") as p:
                return await p.whoami()

        who = _run(check_key())
        save_credential(CRED_PATH, {"api_key": key, **({"base_url": base} if base_url else {})})
        console.print(f"Logged in to [bold]{who.org_name}[/] ({who.env}). Saved to {CRED_PATH}")
        return

    def show(url: str) -> None:
        if no_browser:
            console.print(f"Open this link on any device and approve:\n\n  {url}\n")
        else:
            console.print(f"Opening your browser to log in…\nIf it doesn't open, visit:\n  [dim]{url}[/]\n")

    async def paste() -> str:
        return await asyncio.to_thread(typer.prompt, "Paste the code shown after you approve")

    cred = _run(
        browser_login(
            base,
            client_name=f"Promptli CLI on {socket.gethostname().split('.')[0]}",
            open_browser=not no_browser,
            login_hint=identity,
            want_number=number,
            env="test" if test else None,
            on_url=show,
            paste_code=paste if no_browser else None,
        )
    )
    save_credential(CRED_PATH, cred)
    console.print(
        f"[green]Logged in[/] as agent [bold]@{cred.identity}[/] ({cred.env}). Saved to {CRED_PATH}\n"
        "[dim]Disconnect anytime: `promptli logout`, or API keys → Connected apps in the console.[/]"
    )


@app.command()
def logout() -> None:
    """Revoke this machine's browser login (or forget the stored API key) and delete the credentials."""
    if not CRED_PATH.exists():
        console.print("Not logged in.")
        return
    auth = credential_auth(CRED_PATH)
    if isinstance(auth, OAuthToken):
        _run(auth.revoke())
    CRED_PATH.unlink()
    console.print("Logged out." + (" The browser login was revoked." if isinstance(auth, OAuthToken) else ""))


@app.command()
def whoami(as_json: JsonOpt = False) -> None:
    """Show the org, environment and scopes of the current key."""
    _out(
        _run(_with(lambda p: p.whoami())),
        as_json,
        ["org_id", "org_name", "env", "key_id", "scopes", "identity"],
    )


def mmss(seconds: float | None) -> str:
    """Seconds as m:ss, always in minutes (300:00, not 5:00:00): call time is sold in minutes."""
    s = max(0, round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


@app.command()
def usage(
    period: Annotated[str | None, typer.Option(help="Billing period YYYY-MM (default: current)")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Call minutes this period per tier, included minutes left and overage."""
    u = _run(_with(lambda p: p.usage(period)))
    if as_json:
        _out(u, True)
        return
    table = Table(title=f"Call minutes {u.period.key} · {u.plan} · {u.env}", header_style="bold #F04A28")
    for col in ("tier", "price", "calls", "talk time", "billed", "counts as"):
        table.add_column(col, justify="left" if col in ("tier", "price") else "right")
    for t in u.tiers:
        table.add_row(
            t.label,
            f"${t.price_cents_per_minute / 100:.2f}/min",
            str(t.calls),
            mmss(t.seconds),
            mmss(t.billed_seconds),
            mmss(t.pool_seconds),
        )
    console.print(table)
    if not u.metered:
        console.print("Test mode: tracked, never billed.")
    elif u.pool.included_seconds is None:
        console.print(f"Included minutes: unlimited · used {mmss(u.pool.used_seconds)}")
    else:
        left = u.pool.remaining_seconds or 0
        console.print(
            f"Included minutes: {mmss(u.pool.used_seconds)} of {mmss(u.pool.included_seconds)} used · "
            f"{mmss(left)} left (≈ {left // 60} Standard / {left // 120} Hosted / {left // 300} Premium min)"
        )
    if u.overage.pool_seconds:
        console.print(f"Overage: {mmss(u.overage.pool_seconds)} · ${u.overage.amount_cents / 100:.2f}")


@app.command()
def doctor(identity: IdentityOpt, as_json: JsonOpt = False) -> None:
    """Explain what works for an identity (email, SMS, voice, Agent Link, webhooks) and what to fix."""
    st = _run(_with(lambda p: p.identity(identity).channel_status()))
    if as_json:
        _out(st, True)
        return
    ok = "[green]✓[/]"
    no = "[red]✗[/]"
    console.print(f"[bold]@{st.identity['handle']}[/]")
    console.print(f"  {ok if st.email.ready else no} email   {st.email.address or ''}")
    console.print(
        f"  {ok if st.sms.ready else no} sms     {st.sms.number or 'no number'} {st.sms.status or ''}"
    )
    voice_note = st.voice.number or "no number"
    if st.voice.status == "not_available_yet":
        voice_note = "not available yet"
    console.print(f"  {ok if st.voice.ready else no} voice   {voice_note}")
    console.print(
        f"  {ok if st.agent_link.connected else no} link    {len(st.agent_link.sessions)} session(s)"
    )
    console.print(f"  {ok if st.webhooks.active else no} webhooks {st.webhooks.active} active")
    for issue in st.issues:
        console.print(f"  [yellow]![/] {issue.code}: {issue.message}")


# ---- identities ------------------------------------------------------------------------------


@identities_app.command("list", hidden=True)
@identities_app.command("ls")
def identities_ls(as_json: JsonOpt = False) -> None:
    _out(
        _run(_with(lambda p: p.identities.list())),
        as_json,
        ["handle", "display_name", "email_address", "env", "id"],
    )


@identities_app.command("create")
def identities_create(
    handle: str, display_name: Annotated[str | None, typer.Option()] = None, as_json: JsonOpt = False
) -> None:
    _out(
        _run(_with(lambda p: p.identities.create(handle, display_name=display_name))),
        as_json,
        ["handle", "email_address", "id"],
    )


@identities_app.command("show")
def identities_show(handle: str, as_json: JsonOpt = False) -> None:
    _out(_run(_with(lambda p: p.identity(handle).get())), True)


@identities_app.command("delete")
def identities_delete(handle: str, yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    if not yes:
        typer.confirm(f"Delete @{handle}? Keys are revoked and webhooks disabled.", abort=True)
    _run(_with(lambda p: p.identity(handle).delete()))
    console.print(f"Deleted @{handle}")


# ---- email -----------------------------------------------------------------------------------


@email_app.command("send")
def email_send(
    identity: IdentityOpt,
    to: Annotated[list[str], typer.Option("--to")],
    subject: Annotated[str, typer.Option()] = "",
    text: Annotated[str | None, typer.Option("--text")] = None,
    html: Annotated[str | None, typer.Option()] = None,
    attach: Annotated[list[Path], typer.Option("--attach")] = [],  # noqa: B006
    as_json: JsonOpt = False,
) -> None:
    msg = _run(
        _with(
            lambda p: p.identity(identity).send_email(to, subject, text=text, html=html, attachments=attach)
        )
    )
    _out(msg, as_json, ["id", "status", "to", "subject"])


@email_app.command("list", hidden=True)
@email_app.command("ls")
def email_ls(
    identity: IdentityOpt,
    folder: Annotated[str | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option()] = 20,
    as_json: JsonOpt = False,
) -> None:
    async def go(p: AsyncPromptli) -> list[Any]:
        out: list[Any] = []
        async for msg in p.identity(identity).emails(folder=folder, limit=limit):  # type: ignore[arg-type]
            out.append(msg)
            if len(out) >= limit:
                break
        return out

    _out(_run(_with(go)), as_json, ["id", "direction", "from_", "subject", "status", "created_at"])


@email_app.command("read")
def email_read(identity: IdentityOpt, message_id: str, as_json: JsonOpt = False) -> None:
    msg = _run(_with(lambda p: p.identity(identity).email(message_id)))
    if as_json:
        _out(msg, True)
        return
    console.print(
        f"[bold]{msg.subject}[/]\nFrom: {msg.from_}\nTo: {', '.join(msg.to)}\n\n{msg.text or msg.html or ''}"
    )


@email_app.command("reply")
def email_reply(
    identity: IdentityOpt,
    message_id: str,
    text: Annotated[str, typer.Option("--text")],
    all_: Annotated[bool, typer.Option("--all")] = False,
    as_json: JsonOpt = False,
) -> None:
    _out(
        _run(_with(lambda p: p.identity(identity).reply(message_id, text=text, all=all_))),
        as_json,
        ["id", "status", "to", "subject"],
    )


# ---- calls ------------------------------------------------------------------------------------

CALL_COLUMNS = ["id", "direction", "from_", "to", "status", "outcome", "duration_sec", "started_at"]


@call_app.command("place")
def call_place(
    identity: IdentityOpt,
    to: Annotated[str, typer.Option("--to", help="E.164 number")],
    reason: Annotated[str | None, typer.Option(help="Why the agent is calling")] = None,
    opening: Annotated[str | None, typer.Option(help="Spoken first when they answer")] = None,
    voicemail: Annotated[str | None, typer.Option(help="Message to leave on a machine")] = None,
    hosted: Annotated[
        bool, typer.Option(help="The Promptli assistant talks ($0.10/min); your agent supervises")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Call a number; your connected agent runs the conversation (or --hosted: the Promptli assistant)."""
    _out(
        _run(
            _with(
                lambda p: p.identity(identity).place_call(
                    to,
                    reason=reason,
                    opening=opening,
                    voicemail_message=voicemail,
                    mode="hosted" if hosted else None,
                )
            )
        ),
        as_json,
        CALL_COLUMNS,
    )


@call_app.command("list", hidden=True)
@call_app.command("ls")
def call_ls(
    identity: IdentityOpt, limit: Annotated[int, typer.Option()] = 20, as_json: JsonOpt = False
) -> None:
    _out(_run(_with(lambda p: p.identity(identity).calls(limit=limit))), as_json, CALL_COLUMNS)


@call_app.command("show")
def call_show(call_id: str, as_json: JsonOpt = False) -> None:
    call = _run(_with(lambda p: p.calls.get(call_id)))
    if as_json:
        _out(call, True)
        return
    _out(call, False, [*CALL_COLUMNS, "ended_by", "tier"])
    if call.billing:
        for seg in call.billing.segments:
            extra = f" (counts as {mmss(seg.pool_seconds)})" if seg.pool_seconds != seg.billed_seconds else ""
            talk, billed = mmss(seg.seconds), mmss(seg.billed_seconds)
            console.print(f"{seg.tier.capitalize()} · talk {talk} · billed {billed}{extra}")
        if not call.billing.metered:
            console.print("Test mode: not billed.")


@call_app.command("transcript")
def call_transcript(call_id: str, as_json: JsonOpt = False) -> None:
    t = _run(_with(lambda p: p.calls.transcript(call_id)))
    if as_json:
        _out(t, True)
        return
    for seg in t.segments:
        colour = {"caller": "cyan", "agent": "#F04A28", "system": "dim"}.get(seg.party, "white")
        cut = " [dim](interrupted)[/]" if seg.interrupted else ""
        console.print(f"[{colour}]{seg.party:>6}[/]  {seg.text}{cut}")


@call_app.command("guide")
def call_guide(
    call_id: str, text: Annotated[str, typer.Option("--text", help="What the agent should do")]
) -> None:
    """Steer a live call (the caller never hears the guidance itself)."""
    _out(_run(_with(lambda p: p.calls.guide(call_id, text))), True)


@call_app.command("brief")
def call_brief(call_id: str, as_json: JsonOpt = False) -> None:
    """The brief the hosted assistant works from (goal, facts, guidance, policies)."""
    b = _run(_with(lambda p: p.calls.brief(call_id)))
    if as_json:
        _out(b, True)
        return
    brief: dict[str, Any] = b.brief
    console.print(f"[bold]Brief v{b.version}[/] (updated by {b.updated_by})")
    console.print(f"Goal: {(brief.get('task') or {}).get('goal')}")
    facts: list[dict[str, Any]] = brief.get("facts") or []
    guidance: list[dict[str, Any]] = brief.get("guidance") or []
    for f in facts:
        console.print(f"  • {f.get('text')}")
    for g in guidance:
        console.print(f"  → {g.get('text')} [dim](next {g.get('ttl_turns', 1)} turn)[/]")


@call_app.command("fact")
def call_fact(call_id: str, text: Annotated[str, typer.Option("--text")]) -> None:
    """Give the hosted assistant a fact for the rest of the call."""
    b = _run(_with(lambda p: p.calls.add_fact(call_id, text)))
    console.print(f"Brief v{b.version}")


@call_app.command("answer")
def call_answer(
    call_id: str,
    text: Annotated[str, typer.Option("--text")],
    request: Annotated[str | None, typer.Option(help="act_… id (default: latest question)")] = None,
) -> None:
    """Answer the hosted assistant's question; it tells the caller."""
    _out(_run(_with(lambda p: p.calls.answer(call_id, text, request_id=request))), True)


@call_app.command("say")
def call_say(
    call_id: str,
    text: Annotated[str, typer.Option("--text")],
    now: Annotated[bool, typer.Option(help="Interrupt and speak immediately")] = False,
) -> None:
    """Speak exact words on a hosted call."""
    _out(_run(_with(lambda p: p.calls.say(call_id, text, mode="now" if now else "next"))), True)


@call_app.command("take-over")
def call_take_over(call_id: str) -> None:
    """Your connected agent takes over a hosted call (billed Standard)."""
    _out(_run(_with(lambda p: p.calls.take_over(call_id))), True)


@call_app.command("hand-back")
def call_hand_back(call_id: str) -> None:
    """Hand a taken-over call back to the Promptli assistant."""
    _out(_run(_with(lambda p: p.calls.hand_back(call_id))), True)


@call_app.command("hangup")
def call_hangup(call_id: str, as_json: JsonOpt = False) -> None:
    _out(_run(_with(lambda p: p.calls.hangup(call_id))), as_json, CALL_COLUMNS)


@call_app.command("voice")
def call_voice(
    identity: IdentityOpt,
    incoming: Annotated[str | None, typer.Option(help="agent | forward | auto_reject")] = None,
    forward_to: Annotated[str | None, typer.Option()] = None,
    voice_id: Annotated[str | None, typer.Option()] = None,
    language: Annotated[
        str | None,
        typer.Option(help="BCP-47 (en, en-IN, te, hi); picks a matching voice unless --voice-id"),
    ] = None,
    recording: Annotated[bool | None, typer.Option("--recording/--no-recording")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show (or change) how calls are handled and how the agent sounds."""
    changes = {
        k: v
        for k, v in {
            "incoming_action": incoming,
            "forward_to": forward_to,
            "voice_id": voice_id,
            "language": language,
            "recording": recording,
        }.items()
        if v is not None
    }
    if language and not voice_id:
        base = language.split("-")[0]
        voices = _run(_with(lambda p: p.calls.voices(language=base)))
        if base != "en" and voices:
            changes["voice_id"] = voices[0].id
            console.print(f"[dim]voice: {voices[0].name}[/]")

    async def go(p: AsyncPromptli) -> Any:
        me = p.identity(identity)
        return await (me.update_voice_settings(**changes) if changes else me.voice_settings())

    _out(_run(_with(go)), True)


@call_app.command("voices")
def call_voices(
    language: Annotated[str | None, typer.Option()] = "en",
    q: Annotated[str | None, typer.Option(help="Search")] = None,
    as_json: JsonOpt = False,
) -> None:
    _out(_run(_with(lambda p: p.calls.voices(language=language, q=q))), as_json, ["id", "name", "gender"])


# ---- texts & numbers -------------------------------------------------------------------------


@text_app.command("send")
def text_send(
    identity: IdentityOpt,
    to: Annotated[list[str], typer.Option("--to")],
    text: Annotated[str, typer.Option("--text")],
    as_json: JsonOpt = False,
) -> None:
    _out(
        _run(_with(lambda p: p.identity(identity).send_text(to, text))),
        as_json,
        ["id", "status", "to", "body"],
    )


@text_app.command("list", hidden=True)
@text_app.command("ls")
def text_ls(
    identity: IdentityOpt, limit: Annotated[int, typer.Option()] = 20, as_json: JsonOpt = False
) -> None:
    _out(
        _run(_with(lambda p: p.identity(identity).texts(limit=limit))),
        as_json,
        ["id", "direction", "from_", "to", "body", "status", "created_at"],
    )


@number_app.command("provision")
def number_provision(
    identity: IdentityOpt,
    region: Annotated[str | None, typer.Option()] = None,
    area_code: Annotated[str | None, typer.Option()] = None,
    as_json: JsonOpt = False,
) -> None:
    _out(
        _run(
            _with(lambda p: p.identity(identity).provision_phone_number(region=region, area_code=area_code))
        ),
        as_json,
        ["number", "sms_status", "id"],
    )


@number_app.command("release")
def number_release(identity: IdentityOpt, yes: Annotated[bool, typer.Option("--yes")] = False) -> None:
    if not yes:
        typer.confirm(f"Release @{identity}'s number? It cannot be recovered.", abort=True)
    _run(_with(lambda p: p.identity(identity).release_phone_number()))
    console.print("Released.")


@number_app.command("list", hidden=True)
@number_app.command("ls")
def number_ls(as_json: JsonOpt = False) -> None:
    _out(_run(_with(lambda p: p.numbers.list())), as_json, ["number", "identity", "sms_status", "id"])


# ---- events ----------------------------------------------------------------------------------


@events_app.command("list", hidden=True)
@events_app.command("ls")
def events_ls(
    identity: IdentityOpt, after: Annotated[int, typer.Option()] = 0, as_json: JsonOpt = False
) -> None:
    page = _run(_with(lambda p: p.identity(identity).events(after=after)))
    _out(page.data, as_json, ["seq", "type", "id", "created_at"])


@events_app.command("tail")
def events_tail(identity: IdentityOpt, instance: Annotated[str | None, typer.Option()] = None) -> None:
    """Stream events live over Agent Link (Ctrl-C to stop). Acks each event."""

    async def go(p: AsyncPromptli) -> None:
        inst = instance or f"cli-{socket.gethostname()}"
        async with p.identity(identity).link(instance=inst, user_agent="promptli-cli") as link:
            console.print(f"[dim]connected (cursor {link.hello.get('cursor')})[/]")
            async for evt in link:
                console.print_json(json.dumps(evt))
                await link.ack(evt)

    try:
        _run(_with(go))
    except KeyboardInterrupt:
        pass


# ---- webhooks, keys, contacts ----------------------------------------------------------------


@webhooks_app.command("list", hidden=True)
@webhooks_app.command("ls")
def webhooks_ls(
    identity: Annotated[str | None, typer.Option("--identity", "-i")] = None, as_json: JsonOpt = False
) -> None:
    _out(_run(_with(lambda p: p.webhooks.list(identity))), as_json, ["id", "url", "event_types", "status"])


@webhooks_app.command("create")
def webhooks_create(
    identity: IdentityOpt,
    url: Annotated[str, typer.Option()],
    events: Annotated[list[str], typer.Option("--event")] = ["*"],  # noqa: B006
    as_json: JsonOpt = False,
) -> None:
    res = _run(_with(lambda p: p.webhooks.subscribe(identity, url, events)))
    if as_json:
        _out(res, True)
        return
    console.print(f"Subscription {res.subscription.id} → {res.subscription.url}")
    if res.signing_secret:
        console.print(f"Signing secret (shown once): [bold]{res.signing_secret}[/]")


@webhooks_app.command("deliveries")
def webhooks_deliveries(
    status: Annotated[DeliveryStatus | None, typer.Option()] = None, as_json: JsonOpt = False
) -> None:
    _out(
        _run(_with(lambda p: p.webhooks.deliveries(status=status.value if status else None))),
        as_json,
        ["id", "event_type", "status", "http_status", "attempt", "created_at"],
    )


@webhooks_app.command("replay")
def webhooks_replay(delivery_id: str, as_json: JsonOpt = False) -> None:
    _out(_run(_with(lambda p: p.webhooks.replay(delivery_id))), as_json, ["id", "event_id", "status"])


@keys_app.command("list", hidden=True)
@keys_app.command("ls")
def keys_ls(as_json: JsonOpt = False) -> None:
    _out(
        _run(_with(lambda p: p.api_keys.list())),
        as_json,
        ["key_id", "name", "identity_handle", "env", "last_used_at"],
    )


@keys_app.command("create")
def keys_create(name: str, identity: Annotated[str | None, typer.Option("--identity", "-i")] = None) -> None:
    key = _run(_with(lambda p: p.api_keys.create(name, identity=identity)))
    console.print(f"{key.key_id}: [bold]{key.key}[/]  (shown once)")


@keys_app.command("revoke")
def keys_revoke(key_id: str) -> None:
    _run(_with(lambda p: p.api_keys.revoke(key_id)))
    console.print(f"Revoked {key_id}")


@contacts_app.command("list", hidden=True)
@contacts_app.command("ls")
def contacts_ls(q: Annotated[str | None, typer.Option()] = None, as_json: JsonOpt = False) -> None:
    _out(_run(_with(lambda p: p.contacts.list(q=q))), as_json, ["id", "name", "emails", "phones", "source"])


@contacts_app.command("add")
def contacts_add(
    name: Annotated[str, typer.Option()],
    email: Annotated[list[str], typer.Option("--email")] = [],  # noqa: B006
    phone: Annotated[list[str], typer.Option("--phone")] = [],  # noqa: B006
    as_json: JsonOpt = False,
) -> None:
    _out(
        _run(_with(lambda p: p.contacts.create(name=name, emails=email, phones=phone))),
        as_json,
        ["id", "name"],
    )


# ---- sandbox & signup ------------------------------------------------------------------------


@sandbox_app.command("inbound-email")
def sandbox_inbound_email(
    identity: IdentityOpt,
    subject: Annotated[str, typer.Option()] = "Hello",
    text: Annotated[str, typer.Option("--text")] = "Simulated email",
) -> None:
    _out(_run(_with(lambda p: p.sandbox.inbound_email(identity, subject=subject, text=text))), True)


@sandbox_app.command("inbound-call")
def sandbox_inbound_call(
    identity: IdentityOpt,
    say: Annotated[list[str], typer.Option("--say", help="Caller lines, in order")] = [],  # noqa: B006
    from_number: Annotated[str, typer.Option("--from")] = "+15550109999",
) -> None:
    """Simulate a phone call to the identity (test mode). Prints the call id (see `call transcript`)."""
    script: list[dict[str, Any]] | None = None
    if say:
        script = [{"wait_for_agent": True}]
        for line in say:
            script += [{"say": line}, {"wait_for_agent": True}]
        script.append({"hangup": True})
    cid = _run(_with(lambda p: p.sandbox.inbound_call(identity, script=script, from_number=from_number)))
    console.print(cid)


@sandbox_app.command("inbound-text")
def sandbox_inbound_text(
    identity: IdentityOpt,
    body: Annotated[str, typer.Option("--text")],
    from_number: Annotated[str, typer.Option("--from")] = "+15550109999",
) -> None:
    _out(_run(_with(lambda p: p.sandbox.inbound_text(identity, body, from_number=from_number))), True)


@signup_app.command("create")
def signup_create(
    human_email: str,
    handle: str,
    test_mode: Annotated[bool, typer.Option("--test")] = False,
    base_url: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Create an org + identity for an agent; the human receives a 6-digit code."""
    res = _run(AsyncPromptli.create_signup(human_email, handle, test_mode=test_mode, base_url=base_url))
    console.print(f"Provisional key: [bold]{res.api_key}[/]\n{res.next_step}")


@signup_app.command("verify")
def signup_verify(code: str) -> None:
    ok = _run(_with(lambda p: p.signup.verify(code)))
    console.print("Verified ✓" if ok else "Not verified")


if __name__ == "__main__":
    app()
