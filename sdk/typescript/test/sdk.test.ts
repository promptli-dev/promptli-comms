import { readFileSync } from "node:fs";
import { describe, expect, test } from "vitest";
import { computeSignature, Promptli, PromptliError, verifyWebhook } from "../src/index";

const KEY = "psk_test_abcdefghijkl_" + "a".repeat(43);
const vectors = JSON.parse(readFileSync(new URL("../../../spec/fixtures/signing/vectors.json", import.meta.url), "utf8")) as {
  vectors: { secret: string; id: string; timestamp: string; body: string; signature_header: string }[];
};

type Seen = { method: string; url: string; headers: Record<string, string>; body: unknown };

function fakeFetch(responses: (Response | Error)[], seen: Seen[]): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.push({
      method: init?.method ?? "GET",
      url: String(input),
      headers: init?.headers as Record<string, string>,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const next = responses.shift();
    if (!next) throw new Error("no more responses");
    if (next instanceof Error) throw next;
    return next;
  }) as typeof fetch;
}

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

describe("webhooks", () => {
  test("signing vectors shared with the server and the Python SDK", async () => {
    for (const v of vectors.vectors) {
      const sig = await computeSignature(v.secret, v.id, v.timestamp, v.body);
      expect(v.signature_header.split(" ")).toContain(`v1=${sig}`);
      const headers = { "X-Promptli-Id": v.id, "X-Promptli-Timestamp": v.timestamp, "X-Promptli-Signature": v.signature_header };
      expect(await verifyWebhook(v.body, headers, v.secret, { now: Number(v.timestamp) })).toBe(true);
      expect(await verifyWebhook(v.body + " ", headers, v.secret, { now: Number(v.timestamp) })).toBe(false);
      expect(await verifyWebhook(v.body, headers, ["whsec_old", v.secret], { now: Number(v.timestamp) })).toBe(true); // rotation
      expect(await verifyWebhook(v.body, headers, v.secret, { now: Number(v.timestamp) + 3600 })).toBe(false); // replay window
    }
  });

  test("works with a Headers object and rejects missing headers", async () => {
    const v = vectors.vectors[0]!;
    const h = new Headers({ "x-promptli-id": v.id, "x-promptli-timestamp": v.timestamp, "x-promptli-signature": v.signature_header });
    expect(await verifyWebhook(v.body, h, v.secret, { now: Number(v.timestamp) })).toBe(true);
    expect(await verifyWebhook(v.body, {}, v.secret)).toBe(false);
  });
});

describe("client", () => {
  test("retries 503 with the same idempotency key, then succeeds", async () => {
    const seen: Seen[] = [];
    const p = new Promptli({
      apiKey: KEY,
      baseUrl: "https://api.test.invalid",
      fetch: fakeFetch([json(503, { error: { code: "provider_unavailable", message: "busy", retryable: true } }), json(201, { id: "txt_1" })], seen),
    });
    const t = await p.identity("glow").sendText("+15550104242", "Hi");
    expect(t).toMatchObject({ id: "txt_1" });
    expect(seen).toHaveLength(2);
    expect(seen[0]!.url).toBe("https://api.test.invalid/v1/identities/glow/texts");
    expect(seen[0]!.body).toEqual({ to: "+15550104242", body: "Hi" });
    expect(seen[0]!.headers["Idempotency-Key"]).toBe(seen[1]!.headers["Idempotency-Key"]);
    expect(seen[0]!.headers.Authorization).toBe(`Bearer ${KEY}`);
  });

  test("errors carry the API's machine-readable code; 4xx is not retried", async () => {
    const seen: Seen[] = [];
    const p = new Promptli({
      apiKey: KEY,
      baseUrl: "https://api.test.invalid",
      fetch: fakeFetch([json(422, { error: { code: "recipient_opted_out", message: "STOP", request_id: "req_1" } })], seen),
    });
    const err = await p.identity("glow").sendText("+1555", "x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(PromptliError);
    expect(err).toMatchObject({ status: 422, code: "recipient_opted_out", requestId: "req_1" });
    expect(seen).toHaveLength(1);
  });

  test("hosted call steering and addFact retry on a version conflict", async () => {
    const seen: Seen[] = [];
    const p = new Promptli({
      apiKey: KEY,
      baseUrl: "https://api.test.invalid",
      fetch: fakeFetch(
        [
          json(201, { id: "call_1", mode: "hosted" }),
          json(200, { version: 1 }),
          json(409, { error: { code: "brief_version_conflict", message: "stale" } }),
          json(200, { version: 2 }),
          json(200, { version: 3, brief: {} }),
          json(200, { call_id: "call_1", applied: true, when: "now" }),
          json(200, { plan: "free", pool: {}, tiers: [] }),
        ],
        seen,
      ),
    });
    await p.identity("glow").placeCall("+15550104242", { reason: "Confirm Friday", mode: "hosted" });
    expect(seen[0]!.body).toEqual({ to: "+15550104242", reason: "Confirm Friday", mode: "hosted" });
    const b = await p.calls.addFact("call_1", "Friday 4pm is free");
    expect(b.version).toBe(3);
    expect(seen[4]!.body).toEqual({ base_version: 2, ops: [{ op: "add", path: "/facts/-", value: { text: "Friday 4pm is free" } }] });
    await p.calls.say("call_1", "One moment", "now");
    expect(seen[5]!.url).toMatch(/\/v1\/calls\/call_1\/say$/);
    await p.usage("2026-10");
    expect(seen[6]!.url).toBe("https://api.test.invalid/v1/usage?period=2026-10");
  });

  test("an API key is required", () => {
    expect(() => new Promptli({ apiKey: "" })).toThrow(/API key/);
  });
});
