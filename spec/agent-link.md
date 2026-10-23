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
- **Latency.** About 0.5 s from event to frame. Clients must ignore frame `type`s they do not know.

## Live calls (voice, Mode C)

Connect with `capabilities=voice` to receive calls. Your agent produces every word; we do telephony,
speech-to-text, text-to-speech, turn-taking, fillers and barge-in. Call frames are live only (not replayed);
the call's record, transcript and `call.started` / `call.ended` events are persisted as usual.

| Direction | Frame | Meaning |
|---|---|---|
| server → client | `{"type":"call.incoming","call_id","direction","from","to","identity","contact","contact_key","reason","opening","context","voice"}` | A call needs an agent. Reply `call.accept` within 4 s or it goes to the next session, then voicemail. |
| client → server | `{"type":"call.accept","call_id"}` / `{"type":"call.decline","call_id"}` | Take (or refuse) the call. |
| server → client | `{"type":"call.start","call_id","turn_id":"t0","greet":bool,"opening"}` | The call is live. `greet: true` = speak first now with `turn_id` `t0`. `opening` = what we already said (outbound). |
| server → client | `{"type":"call.transcript","call_id","party":"caller","turn_id","text","is_final"}` | Caller speech. `is_final: true` (with a `turn_id`) is a complete turn: reply to it. Interim frames have no `turn_id`. |
| client → server | `{"type":"call.text","call_id","turn_id","delta","done"}` | Your reply, streamed. Send deltas as you generate them; `done: true` ends the turn. Not streaming? Send one frame with the whole text and `done: true`. |
| server → client | `{"type":"call.barge_in","call_id","turn_id","heard","spoken_so_far"}` | The caller talked over you. Speech stopped; `spoken_so_far` is exactly what they heard. Stop generating that turn. |
| server → client | `{"type":"call.tts.played","call_id","turn_id","text"}` | The caller heard the whole reply. |
| server → client | `{"type":"call.dtmf","call_id","digits"}` | The caller pressed keys. |
| server → client | `{"type":"call.machine","call_id","answered_by"}` | Outbound call reached an answering machine (we leave `voicemail_message` and hang up). |
| server → client | `{"type":"call.stop","call_id","reason"}` | The call ended. |
| client → server | `{"type":"call.filler","call_id","phrase"?}` | Play a short filler now (before a slow tool call). |
| client → server | `{"type":"call.hangup","call_id","say"?}` | Hang up, optionally after saying `say`. |
| client → server | `{"type":"call.transfer","call_id","to","say"?}` | Transfer to an E.164 number. |
| client → server | `{"type":"call.dtmf","call_id","digits"}` | Send keypad tones (e.g. to navigate a phone menu). |
| client → server | `{"type":"call.voice","call_id","voice_id"?,"speed"?}` | Change voice or speed from the next reply. |

While you think, we play short fillers ("Mm-hmm, one sec.") after 0.7 s, progress lines from 4 s, and a
graceful message at 45 s. Your first delta stops them. Tip: stream a short spoken acknowledgment first, then
do tool work, then answer in one or two sentences. Never send markdown, code or URLs to speak.

## Close codes
`4001` auth/scope error · `4002` replaced · `4003` revoked/closed · `4004` idle · `4400` bad request
