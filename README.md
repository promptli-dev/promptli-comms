# promptli-comms

SDKs, CLI and the agent-plugin gateway for **Promptli Comms**: email, SMS and phone calls for AI agents.

| Path | What |
|---|---|
| `sdk/python` | `pip install promptli`: async-first client (`AsyncPromptli`) + sync `Promptli`, typed models, webhook verification, Agent Link client, `promptli` CLI |
| `gateway` | `promptli-gateway`: runtime-agnostic plugin core (Agent Link → per-contact agent sessions, setup/bootstrap/doctor, autostart) |
| `spec` | Synced from the server's `contracts/` (OpenAPI, errors, Agent Link protocol, signing vectors); never edit by hand |

## Quick start
```python
from promptli import Promptli

with Promptli() as p:  # PROMPTLI_API_KEY from the environment
    glow = p.identity("glow")
    glow.send_email("jane@example.com", "Your booking", text="Confirmed for 3pm")
    glow.provision_phone_number(region="NY")
    glow.send_text("+15551234567", "Reminder: 3pm tomorrow")
```

```python
import asyncio
from promptli import AsyncPromptli


async def main():
    async with AsyncPromptli() as p:
        async with p.identity("glow").link(instance="laptop") as link:  # outbound WebSocket, no public URL
            async for event in link:
                print(event["type"], event["data"])
                await link.ack(event)


asyncio.run(main())
```

Log in: `promptli login` opens your browser, where you pick or create an agent (add `--number` to get it a
phone number, `--no-browser` on SSH). Nothing to copy or paste; the login refreshes itself and can be revoked
under **API keys → Connected apps**. `promptli login --api-key` still works, and so does `PROMPTLI_API_KEY`.
In code, `AsyncPromptli(credential_auth(path))` uses a saved login.

CLI: `promptli login`, `promptli logout`, `promptli whoami`, `promptli email send -i glow --to jane@example.com --subject Hi --text Hello`,
`promptli text send -i glow --to +15551234567 --text Hi`, `promptli events tail -i glow`, `promptli doctor -i glow`.

Webhooks: `from promptli import verify_webhook` → `verify_webhook(raw_body, headers, "whsec_…")`.

## Development
```bash
uv sync
./scripts/generate_models.sh        # after syncing spec/
uv run pytest                       # unit + conformance
PROMPTLI_API_KEY=psk_test_… PROMPTLI_BASE_URL=http://localhost:8000 uv run pytest -m live tests/live
```
