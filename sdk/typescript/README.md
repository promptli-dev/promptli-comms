# promptli (TypeScript / JavaScript SDK)

Email, SMS and phone calls for AI agents: the TypeScript / JavaScript SDK for [Promptli Comms](https://comms.promptli.com).
No runtime dependencies (standard `fetch` + Web Crypto), so it runs on Node 18+, Bun, Deno, Workers and browsers.

```ts
import { Promptli, verifyWebhook } from "promptli";

const p = new Promptli({ apiKey: process.env.PROMPTLI_API_KEY });
const glow = p.identity("glow");

await glow.sendEmail({ to: ["jane@example.com"], subject: "Friday", text: "See you at 4pm." });
await glow.sendText("+14155550100", "Reminder: Friday 4pm");

// A call the Promptli assistant runs from a brief ($0.10/min); steer it live.
const call = await glow.placeCall("+14155550100", { reason: "Confirm Friday's haircut", mode: "hosted" });
await p.calls.addFact(call.id, "Friday 4pm is booked with Priya");
await p.calls.say(call.id, "Quick note: parking is free out back.");

console.log(await p.usage()); // minutes per tier, included pool, overage

// Webhooks: pass the raw body exactly as received.
const ok = await verifyWebhook(rawBody, request.headers, process.env.PROMPTLI_WEBHOOK_SECRET!);
```

Errors are `PromptliError` with a machine-readable `code` (e.g. `recipient_opted_out`, `insufficient_minutes`),
`status` and `requestId`. Retries (429, 5xx, network) reuse the same `Idempotency-Key`, so a POST is never
applied twice. Any route not wrapped here: `p.request("GET", "/v1/numbers")`, fully typed via `paths`.
