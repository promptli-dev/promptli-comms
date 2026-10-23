# Agent Link protocol v1

Plugins and SDKs open an **outbound** WebSocket to receive their identity's events in real time. No
public URL or tunnel is needed. Events are the same envelopes as webhooks and `GET /v1/events`.

## Connect

```
GET wss://link.comms.promptli.com/v1/connect[?identity=<handle>][&capabilities=a,b]
Authorization: Bearer psk_live_…        (identity-scoped key, or an admin key + ?identity=)
X-Link-Instance: <stable install id>    (one per installed plugin; reconnects with the same id resume)
User-Agent: promptli-gateway/0.1 claude-code/0.1
```

The key needs the `events:read` scope. Failures are sent as an `error` frame, then the socket closes
with code 4001.

## Frames (JSON text)

| Direction | Frame | Meaning |
|---|---|---|
| server → client | `{"type":"hello","session_id","protocol":1,"identity":{"id","handle"},"cursor","latest_seq"}` | First frame. `cursor` = last seq this instance acked; replay starts after it. |
| server → client | `{"type":"event","event":<envelope>}` | One event; `event.seq` is per-identity and strictly increasing. |
| server → client | `{"type":"ping"}` | Every 20 s; answer with `pong`. |
| server → client | `{"type":"error","code","message"}` | Then close. |
| client → server | `{"type":"ack","seq":N}` | Persist the cursor (flushed about every 1 s). Ack after you have processed the event. |
| client → server | `{"type":"resume","after":N}` | Optional, sent right after `hello`: replay from an earlier cursor (e.g. the local state is behind). |
| client → server | `{"type":"pong"}` | Keepalive reply. |

## Semantics
- **At-least-once.** Dedupe on `event.id` (or keep `seq` > last processed).
- **Resume.** Reconnecting with the same `X-Link-Instance` continues after the last acked seq. A new instance starts at the latest seq (no backlog); send `resume` to fetch older events, or poll `GET /v1/events?after=`.
- **Takeover.** A new connection with the same instance replaces the old one, which gets `session_replaced` and close code 4002.
- **Revocation.** Revoking the key closes the socket (`session_closed`, code 4003) within about 1 s.
- **Idle.** No client frame for 60 s closes the socket with code 4004. Reconnect with exponential backoff (1 s to 30 s, with jitter).
- **Latency.** About 0.5 s from event to frame. Call frames (voice, M3) are added under protocol 1 with new `type`s, and clients must ignore unknown types.

## Close codes
`4001` auth/scope error · `4002` replaced · `4003` revoked/closed · `4004` idle · `4400` bad request
