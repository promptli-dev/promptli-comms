// Verify webhook deliveries: X-Promptli-Id / X-Promptli-Timestamp / X-Promptli-Signature: v1=<hex> [v1=<hex>]
// v1 = HMAC-SHA256(secret without "whsec_", `${id}.${timestamp}.` + raw body). Uses Web Crypto (Node 18+,
// Bun, Deno, Workers, browsers).

import type { components } from "./schema";

export type EventEnvelope = components["schemas"]["EventEnvelope"];

const enc = new TextEncoder();

async function hmacHex(secret: string, data: Uint8Array<ArrayBuffer>): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret.replace(/^whsec_/, "")), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, data));
  return Array.from(sig, (b) => b.toString(16).padStart(2, "0")).join("");
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export async function computeSignature(secret: string, eventId: string, timestamp: string, body: string | Uint8Array): Promise<string> {
  const raw = typeof body === "string" ? enc.encode(body) : body;
  return hmacHex(secret, concat(enc.encode(`${eventId}.${timestamp}.`), raw));
}

type HeaderBag = Headers | Record<string, string | string[] | undefined>;

function header(headers: HeaderBag, name: string): string {
  if (typeof (headers as Headers).get === "function") return (headers as Headers).get(name) ?? "";
  const bag = headers as Record<string, string | string[] | undefined>;
  const key = Object.keys(bag).find((k) => k.toLowerCase() === name);
  const v = key ? bag[key] : undefined;
  return Array.isArray(v) ? (v[0] ?? "") : (v ?? "");
}

/**
 * True when the request was signed by Promptli with `secret` (or any secret in a list, during rotation).
 * Pass the raw body exactly as received; don't re-serialize parsed JSON.
 */
export async function verifyWebhook(
  body: string | Uint8Array,
  headers: HeaderBag,
  secret: string | string[],
  opts: { toleranceSeconds?: number; now?: number } = {},
): Promise<boolean> {
  const id = header(headers, "x-promptli-id");
  const ts = header(headers, "x-promptli-timestamp");
  const sig = header(headers, "x-promptli-signature");
  if (!id || !/^\d+$/.test(ts) || !sig) return false;
  const now = opts.now ?? Date.now() / 1000;
  if (Math.abs(now - Number(ts)) > (opts.toleranceSeconds ?? 300)) return false;
  const provided = sig.split(/\s+/).filter((p) => p.startsWith("v1=")).map((p) => p.slice(3));
  for (const s of Array.isArray(secret) ? secret : [secret]) {
    const expected = await computeSignature(s, id, ts, body);
    if (provided.some((p) => safeEqual(p, expected))) return true;
  }
  return false;
}

/** Parse a webhook body (or Agent Link event) into the event envelope. */
export function parseEvent(body: string | Uint8Array): EventEnvelope {
  return JSON.parse(typeof body === "string" ? body : new TextDecoder().decode(body)) as EventEnvelope;
}
