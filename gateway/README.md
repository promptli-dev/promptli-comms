# promptli-gateway

Runtime-agnostic core that turns any agent runtime into a Promptli-connected agent:

- **Agent Link** consumer (outbound WebSocket; no public URL) with local dedupe + low-watermark acks.
- **One runtime session per person** (`contact_key`) across email and SMS; bursts of texts become one turn.
- **Replies on the same channel** (threaded email reply / SMS, group-aware), skipped if the agent already
  replied via a tool during the turn.
- **Untrusted content framing**: inbound bodies are wrapped as external data (prompt-injection guard).
- **Tools** (`ToolSpec`): send/reply email, send text, conversations, contacts, channel status, exposed to
  runtimes natively or via `python -m promptli_gateway.mcp_stdio --runtime <name>`.
- **Setup**: `bootstrap()` (idempotent; mints an identity-scoped key from an admin key and stores only that),
  `doctor()`, macOS launchd autostart.

Implement `RuntimeAdapter` (see `adapter.py`) for a new runtime; `adapters/echo.py` and
`adapters/generic_http.py` are references. Voice turns (Mode C) and supervisor turns (Mode A) arrive in P3.
