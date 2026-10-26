// HTTP transport: auth, JSON, retries with backoff (429 / 5xx / network), Retry-After, idempotency keys.

export const DEFAULT_BASE_URL = "https://api.comms.promptli.com";

export class PromptliError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
    readonly retryable = false,
    readonly details?: unknown,
  ) {
    super(message);
    this.name = "PromptliError";
  }
}

export type TransportOptions = {
  apiKey: string;
  baseUrl?: string;
  maxRetries?: number;
  timeoutMs?: number;
  fetch?: typeof fetch;
  userAgent?: string;
};

type Query = Record<string, string | number | boolean | undefined | null>;

export type RequestOptions = { query?: Query; body?: unknown; idempotencyKey?: string; raw?: boolean };

// 409 is not here: conflicts (e.g. brief_version_conflict) need the caller; idempotency_in_progress says retryable.
const RETRY_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

function idempotencyKey(): string {
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

export class Transport {
  readonly baseUrl: string;
  private readonly key: string;
  private readonly maxRetries: number;
  private readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;
  private readonly userAgent: string;

  constructor(opts: TransportOptions) {
    if (!opts.apiKey) throw new PromptliError(0, "invalid_api_key", "An API key is required (psk_live_… or psk_test_…).");
    this.key = opts.apiKey;
    this.baseUrl = (opts.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.maxRetries = opts.maxRetries ?? 3;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.fetchImpl = opts.fetch ?? globalThis.fetch.bind(globalThis);
    this.userAgent = opts.userAgent ?? "promptli-ts/0.1.0";
  }

  async request<T = unknown>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const url = new URL(this.baseUrl + path);
    for (const [k, v] of Object.entries(opts.query ?? {})) if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    const headers: Record<string, string> = { Authorization: `Bearer ${this.key}`, Accept: "application/json", "User-Agent": this.userAgent };
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }
    // Same key on every retry, so a POST that timed out after the server acted is never applied twice.
    if (method === "POST") headers["Idempotency-Key"] = opts.idempotencyKey ?? idempotencyKey();
    for (let attempt = 0; ; attempt++) {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), this.timeoutMs);
      let res: Response;
      try {
        res = await this.fetchImpl(url, { method, headers, body, signal: ctl.signal });
      } catch (e) {
        clearTimeout(timer);
        if (attempt < this.maxRetries) {
          await sleep(backoff(attempt));
          continue;
        }
        throw new PromptliError(0, "network_error", `Could not reach Promptli: ${String(e)}`, undefined, true);
      }
      clearTimeout(timer);
      if (res.ok) {
        if (res.status === 204) return undefined as T;
        if (opts.raw) return (await res.arrayBuffer()) as T;
        const text = await res.text();
        return (text ? JSON.parse(text) : undefined) as T;
      }
      const err = await toError(res);
      if (attempt < this.maxRetries && (err.retryable || RETRY_STATUS.has(res.status)) && err.code !== "idempotency_key_reused") {
        const after = Number(res.headers.get("retry-after"));
        await sleep(Number.isFinite(after) && after > 0 ? Math.min(after * 1000, 30_000) : backoff(attempt));
        continue;
      }
      throw err;
    }
  }
}

async function toError(res: Response): Promise<PromptliError> {
  let body: { error?: { code?: string; message?: string; request_id?: string; retryable?: boolean; details?: unknown } } = {};
  try {
    body = (await res.json()) as typeof body;
  } catch {
    /* not JSON */
  }
  const e = body.error ?? {};
  return new PromptliError(res.status, e.code ?? `http_${res.status}`, e.message ?? res.statusText, e.request_id, !!e.retryable, e.details);
}

function backoff(attempt: number): number {
  return Math.min(250 * 2 ** attempt, 4000) * (0.5 + Math.random() / 2);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
