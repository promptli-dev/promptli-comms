// Promptli Comms client: `new Promptli({ apiKey })`, then `p.identity("glow").sendText(...)` etc.
// Types come from the generated OpenAPI schema; `p.request()` reaches any route not wrapped here.

import { type RequestOptions, Transport, type TransportOptions } from "./http.js";
import type { components } from "./schema.js";

type S = components["schemas"];
export type Identity = S["Identity"];
export type Message = S["Message"];
export type Text = S["Text"];
export type Call = S["Call"];
export type Transcript = S["Transcript"];
export type BriefView = S["BriefView"];
export type SteerResult = S["SteerResult"];
export type Usage = S["Usage"];
export type Contact = S["Contact"];
export type BillingAccount = S["BillingAccount"];
export type HostedSettings = S["HostedSettings"];
export type VoiceSettings = S["VoiceSettings"];

export type PromptliOptions = Partial<TransportOptions> & { apiKey?: string };

const enc = encodeURIComponent;

function envKey(): string | undefined {
  const g = globalThis as { process?: { env?: Record<string, string | undefined> } };
  return g.process?.env?.PROMPTLI_API_KEY;
}

export class Promptli {
  readonly http: Transport;
  readonly calls: Calls;
  readonly contacts: Contacts;
  readonly identities: Identities;

  constructor(opts: PromptliOptions = {}) {
    this.http = new Transport({ ...opts, apiKey: opts.apiKey ?? envKey() ?? "" });
    this.calls = new Calls(this.http);
    this.contacts = new Contacts(this.http);
    this.identities = new Identities(this.http);
  }

  /** An agent identity by handle or idn_ id. */
  identity(ref: string): AgentIdentity {
    return new AgentIdentity(this.http, ref);
  }

  whoami(): Promise<S["WhoamiResponse"]> {
    return this.http.request("GET", "/v1/whoami");
  }

  /** Call minutes this period per tier (Standard / Hosted / Premium), included pool and overage. */
  usage(period?: string): Promise<Usage> {
    return this.http.request("GET", "/v1/usage", { query: { period } });
  }

  billing(): Promise<BillingAccount> {
    return this.http.request("GET", "/v1/billing");
  }

  /** Any route: `p.request("GET", "/v1/numbers")`. */
  request<T = unknown>(method: string, path: string, opts?: RequestOptions): Promise<T> {
    return this.http.request<T>(method, path, opts);
  }
}

class Identities {
  constructor(private readonly t: Transport) {}

  async list(): Promise<Identity[]> {
    return (await this.t.request<S["IdentityList"]>("GET", "/v1/identities")).data;
  }

  create(body: S["CreateIdentity"]): Promise<Identity> {
    return this.t.request("POST", "/v1/identities", { body });
  }

  /** Create or update by handle (idempotent: safe to run on every deploy). */
  upsert(handle: string, body: S["UpsertIdentity"]): Promise<S["IdentityUpsertResult"]> {
    return this.t.request("PUT", `/v1/identities/${enc(handle)}`, { body });
  }

  me(): Promise<Identity> {
    return this.t.request("GET", "/v1/identities/self");
  }
}

export class AgentIdentity {
  constructor(
    private readonly t: Transport,
    readonly ref: string,
  ) {}

  private p(path: string): string {
    return `/v1/identities/${enc(this.ref)}${path}`;
  }

  get(): Promise<Identity> {
    return this.t.request("GET", this.p(""));
  }

  channelStatus(): Promise<S["ChannelStatus"]> {
    return this.t.request("GET", this.p("/channel-status"));
  }

  // ---- email ----
  sendEmail(body: S["SendEmail"], opts: { idempotencyKey?: string } = {}): Promise<Message> {
    return this.t.request("POST", this.p("/mail/messages"), { body, ...opts });
  }

  async emails(opts: { limit?: number; direction?: "inbound" | "outbound"; folder?: string } = {}): Promise<Message[]> {
    return (await this.t.request<S["Page_Message_"]>("GET", this.p("/mail/messages"), { query: opts })).data;
  }

  reply(messageId: string, body: S["ReplyEmail"]): Promise<Message> {
    return this.t.request("POST", this.p(`/mail/messages/${enc(messageId)}/reply`), { body });
  }

  // ---- texts ----
  sendText(to: string, body: string, opts: { media?: string[]; idempotencyKey?: string } = {}): Promise<Text> {
    return this.t.request("POST", this.p("/texts"), { body: { to, body, ...(opts.media ? { media: opts.media } : {}) }, idempotencyKey: opts.idempotencyKey });
  }

  async texts(opts: { limit?: number; conversation_id?: string } = {}): Promise<Text[]> {
    return (await this.t.request<S["Page_Text_"]>("GET", this.p("/texts"), { query: opts })).data;
  }

  // ---- calls ----
  /** Call a number. `mode: "hosted"` lets the Promptli assistant talk from the call brief ($0.10/min). */
  placeCall(to: string, opts: Omit<S["PlaceCall"], "to"> = {}): Promise<Call> {
    return this.t.request("POST", this.p("/calls"), { body: { to, ...opts } });
  }

  async calls(limit = 25): Promise<Call[]> {
    return (await this.t.request<S["CallPage"]>("GET", this.p("/calls"), { query: { limit } })).data;
  }

  voiceSettings(): Promise<VoiceSettings> {
    return this.t.request("GET", this.p("/voice"));
  }

  async updateVoiceSettings(changes: Partial<VoiceSettings>): Promise<VoiceSettings> {
    const current = await this.voiceSettings();
    return this.t.request("PUT", this.p("/voice"), { body: { ...current, ...changes } });
  }

  hostedSettings(): Promise<HostedSettings> {
    return this.t.request("GET", this.p("/hosted"));
  }

  async updateHostedSettings(changes: Partial<HostedSettings>): Promise<HostedSettings> {
    const current = await this.hostedSettings();
    return this.t.request("PUT", this.p("/hosted"), { body: { ...current, ...changes } });
  }

  // ---- scheduled calls and texts ----
  /** Place a call later (`runAt`: ISO 8601 with timezone, or a Date). */
  scheduleCall(to: string, runAt: string | Date, opts: Omit<S["ScheduledCall"], "to"> = {}): Promise<S["Scheduled"]> {
    const run_at = typeof runAt === "string" ? runAt : runAt.toISOString();
    return this.t.request("POST", this.p("/scheduled"), { body: { run_at, call: { to, ...opts } } });
  }

  scheduleText(to: string | string[], body: string, runAt: string | Date): Promise<S["Scheduled"]> {
    const run_at = typeof runAt === "string" ? runAt : runAt.toISOString();
    return this.t.request("POST", this.p("/scheduled"), { body: { run_at, text: { to, body } } });
  }

  async scheduled(includeDone = false): Promise<S["Scheduled"][]> {
    return (await this.t.request<S["ScheduledList"]>("GET", this.p("/scheduled"), { query: { include_done: includeDone } })).data;
  }

  cancelScheduled(id: string): Promise<S["Scheduled"]> {
    return this.t.request("DELETE", `/v1/scheduled/${enc(id)}`);
  }

  // ---- people ----
  async conversations(limit = 50): Promise<S["ConversationSummary"][]> {
    return (await this.t.request<S["ConversationList"]>("GET", this.p("/conversations"), { query: { limit } })).data;
  }

  conversation(contactKey: string, limit = 50): Promise<S["ConversationDetail"]> {
    return this.t.request("GET", this.p(`/conversations/${enc(contactKey)}`), { query: { limit } });
  }
}

class Calls {
  constructor(private readonly t: Transport) {}

  get(callId: string): Promise<Call> {
    return this.t.request("GET", `/v1/calls/${enc(callId)}`);
  }

  transcript(callId: string): Promise<Transcript> {
    return this.t.request("GET", `/v1/calls/${enc(callId)}/transcript`);
  }

  hangup(callId: string): Promise<Call> {
    return this.t.request("POST", `/v1/calls/${enc(callId)}/hangup`);
  }

  /** Steer a live call: guidance the agent works into the conversation (never spoken as-is). */
  guide(callId: string, text: string): Promise<S["GuidanceResult"]> {
    return this.t.request("POST", `/v1/calls/${enc(callId)}/guidance`, { body: { text } });
  }

  // ---- hosted calls: brief and live steering ----
  brief(callId: string, version?: number): Promise<BriefView> {
    return this.t.request("GET", `/v1/calls/${enc(callId)}/brief`, { query: { version } });
  }

  patchBrief(callId: string, baseVersion: number, ops: S["PatchOp"][]): Promise<BriefView> {
    return this.t.request("PATCH", `/v1/calls/${enc(callId)}/brief`, { body: { base_version: baseVersion, ops } });
  }

  /** Read the brief and append a fact (retries once on a version conflict). */
  async addFact(callId: string, text: string): Promise<BriefView> {
    for (let attempt = 0; ; attempt++) {
      const b = await this.brief(callId);
      try {
        return await this.patchBrief(callId, b.version, [{ op: "add", path: "/facts/-", value: { text } }]);
      } catch (e) {
        if (attempt > 0 || (e as { code?: string }).code !== "brief_version_conflict") throw e;
      }
    }
  }

  answer(callId: string, text: string, requestId?: string): Promise<SteerResult> {
    return this.t.request("POST", `/v1/calls/${enc(callId)}/answer`, { body: { text, ...(requestId ? { request_id: requestId } : {}) } });
  }

  say(callId: string, text: string, mode: "next" | "now" = "next"): Promise<SteerResult> {
    return this.t.request("POST", `/v1/calls/${enc(callId)}/say`, { body: { text, mode } });
  }

  takeOver(callId: string): Promise<SteerResult> {
    return this.t.request("POST", `/v1/calls/${enc(callId)}/take-over`);
  }

  handBack(callId: string): Promise<SteerResult> {
    return this.t.request("POST", `/v1/calls/${enc(callId)}/hand-back`);
  }
}

class Contacts {
  constructor(private readonly t: Transport) {}

  lookup(by: { email?: string; phone?: string }): Promise<Contact | null> {
    return this.t.request("GET", "/v1/contacts/lookup", { query: by });
  }

  get(contactId: string): Promise<Contact> {
    return this.t.request("GET", `/v1/contacts/${enc(contactId)}`);
  }

  create(body: S["CreateContact"]): Promise<Contact> {
    return this.t.request("POST", "/v1/contacts", { body });
  }

  remember(contactId: string, text: string): Promise<Contact> {
    return this.t.request("POST", `/v1/contacts/${enc(contactId)}/memories`, { body: { text } });
  }
}
