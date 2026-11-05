/** Per-grant OAuth state and serialized issuance for the pinned provider. */
type Entry = { value: string; expiresAt?: number };
type Recovery = { credential: string; fingerprint: string; accessTokenId: string; issuedAt: number; until: number; iv: number[]; ciphertext: number[] };
type GrantState = {
  version: number;
  revoked: boolean;
  grantKey: string;
  grant?: Entry;
  tokens: Record<string, Entry>;
  retired?: Record<string, number>;
  revokedUntil?: number;
  consumedCode?: string;
  recovery?: Recovery;
};

type CoordinatorEnv = { OAUTH_KV: KVNamespace; OAUTH_GRANTS: DurableObjectNamespace };
type Exchange = (request: Request, env: CoordinatorEnv, ctx: ExecutionContext) => Promise<Response>;
const MAX_BODY = 32 * 1024;
const MAX_STATE = 1024 * 1024; // below SQLite's 2 MB key/value limit
const RECOVERY_MS = 5 * 60 * 1000;
const TOMBSTONE_MS = 48 * 60 * 60 * 1000; // well beyond the configured access-token lifetimes
const noStore = { "Cache-Control": "no-store", "Content-Type": "application/json" };
const invalid = () => new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400, headers: noStore });
const unavailable = () => new Response(JSON.stringify({ error: "temporarily_unavailable" }), { status: 503, headers: { ...noStore, "Retry-After": "2" } });

function family(key: string): string | undefined {
  const pieces = key.split(":");
  if (!((pieces[0] === "grant" && pieces.length === 3) || (pieces[0] === "token" && pieces.length === 4))) return;
  if (!pieces[1] || !pieces[2] || (pieces[0] === "token" && !pieces[3]) || pieces[1].length > 256 || pieces[2].length > 256) return;
  return `${pieces[1]}:${pieces[2]}`;
}

function valueFor(entry: Entry | undefined, options: unknown): unknown {
  if (!entry || (entry.expiresAt !== undefined && entry.expiresAt <= Date.now())) return null;
  const kind = typeof options === "string" ? options : (options as { type?: string } | undefined)?.type;
  if (kind === "json") return JSON.parse(entry.value);
  if (kind && kind !== "text") throw new Error("Unsupported OAuth record format");
  return entry.value;
}

function entry(value: string, options?: KVNamespacePutOptions): Entry {
  const expiresAt = options?.expiration ? options.expiration * 1000 : options?.expirationTtl ? Date.now() + options.expirationTtl * 1000 : undefined;
  return { value, ...(expiresAt ? { expiresAt } : {}) };
}

async function digest(value: string): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("");
}

async function recoveryKey(credential: string): Promise<CryptoKey> {
  const material = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`mcp-refresh-recovery-v1:${credential}`));
  return crypto.subtle.importKey("raw", material, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function seal(credential: string, text: string): Promise<Pick<Recovery, "iv" | "ciphertext">> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await recoveryKey(credential), new TextEncoder().encode(text));
  return { iv: Array.from(iv), ciphertext: Array.from(new Uint8Array(ciphertext)) };
}

async function unseal(credential: string, recovery: Recovery): Promise<string> {
  const plaintext = await crypto.subtle.decrypt({ name: "AES-GCM", iv: new Uint8Array(recovery.iv) }, await recoveryKey(credential), new Uint8Array(recovery.ciphertext));
  return new TextDecoder().decode(plaintext);
}

function stub(env: CoordinatorEnv, name: string): DurableObjectStub {
  return env.OAUTH_GRANTS.get(env.OAUTH_GRANTS.idFromName(name));
}

async function operation(env: CoordinatorEnv, name: string, data: unknown): Promise<Response> {
  return stub(env, name).fetch("https://grant.internal/__grant_operation", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
}

/** Adapt only provider grant/token keys; registration and browser state remain in KV. */
export function grantKV(env: CoordinatorEnv): KVNamespace {
  const kv = env.OAUTH_KV;
  const adapter = {
    async get(key: string, options?: unknown) {
      const name = family(key);
      if (!name) return kv.get(key, options as "text");
      const result = await operation(env, name, { op: "get", key });
      if (!result.ok) throw new Error("Grant store unavailable");
      const data = await result.json() as { initialized: boolean; record?: Entry };
      if (!data.initialized) return kv.get(key, options as "text"); // legacy access tokens only
      return valueFor(data.record, options);
    },
    async put(key: string, value: string, options?: KVNamespacePutOptions) {
      const name = family(key);
      if (!name) return kv.put(key, value, options);
      if (!key.startsWith("grant:")) throw new Error("Token issuance must run in the grant coordinator");
      const parsed = JSON.parse(value) as Record<string, unknown>;
      if (!parsed.authCodeWrappedKey) throw new Error("Only a new authorization can create a grant");
      // KV is an index for provider list/revoke helpers, never the authority for
      // a migrated grant. Keep it until explicit revocation/garbage collection.
      const index = Object.fromEntries(["id", "userId", "clientId", "scope", "metadata", "createdAt", "resource", "redirectUri"]
        .filter(field => parsed[field] !== undefined).map(field => [field, parsed[field]]));
      await kv.put(key, JSON.stringify(index), { metadata: options?.metadata });
      const result = await operation(env, name, { op: "create", key, record: entry(value, options) });
      if (!result.ok) throw new Error("Grant store could not create authorization");
    },
    async delete(key: string) {
      const name = family(key);
      if (!name) return kv.delete(key);
      const result = await operation(env, name, { op: "delete", key });
      if (!result.ok) throw new Error("Grant store could not revoke authorization");
      await kv.delete(key);
    },
    list: kv.list.bind(kv),
    async getWithMetadata(key: string, options?: unknown) {
      if (family(key)) throw new Error("Grant metadata must be read through the coordinator");
      return kv.getWithMetadata(key, options as "text");
    },
  };
  return adapter as unknown as KVNamespace;
}

/** Route valid-shaped code and refresh credentials to their one grant object. */
export async function routeExchange(request: Request, env: CoordinatorEnv): Promise<Response | undefined> {
  if (request.method !== "POST" || new URL(request.url).pathname !== "/oauth/token") return;
  if (!request.headers.get("Content-Type")?.toLowerCase().startsWith("application/x-www-form-urlencoded")) return;
  const body = await boundedText(request.clone());
  if (body === null) return new Response(null, { status: 413, headers: noStore });
  const form = new URLSearchParams(body);
  if (form.getAll("grant_type").length === 0) return; // provider's revocation endpoint
  if (form.getAll("grant_type").length !== 1) return invalid();
  const type = form.get("grant_type");
  if (type !== "refresh_token" && type !== "authorization_code") return;
  const field = type === "refresh_token" ? "refresh_token" : "code";
  if (form.getAll(field).length !== 1) return invalid();
  const credential = form.get(field) || "";
  const parts = credential.split(":");
  if (parts.length !== 3 || !parts.every(p => p && p.length <= 1024)) return invalid();
  const name = family(`grant:${parts[0]}:${parts[1]}`);
  if (!name) return invalid();
  return stub(env, name).fetch(request);
}

async function boundedText(request: Request): Promise<string | null> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.length;
      if (size > MAX_BODY) { void reader.cancel().catch(() => undefined); return null; }
      chunks.push(part.value);
    }
  } finally { reader.releaseLock(); }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { all.set(chunk, offset); offset += chunk.length; }
  return new TextDecoder().decode(all);
}

class RecoveryResult extends Error {}
class ConsumedCode extends Error {}
class ReplayDetected extends Error {}

// The provider calls the grant lookup only after authenticating the client.
// Decode its claimed ID so a different authenticated client cannot revoke a
// grant merely by presenting a credential for it.
function requestClientId(form: URLSearchParams, authorization: string | null): string | undefined {
  const basic = authorization?.match(/^basic[ \t]+([^ \t]+)$/i);
  if (basic) {
    try { const decoded = atob(basic[1]); const colon = decoded.indexOf(":"); return colon < 0 ? undefined : decodeURIComponent(decoded.slice(0, colon).replace(/\+/g, " ")); }
    catch { return; }
  }
  const explicit = form.get("client_id");
  if (explicit) return explicit;
  const assertion = form.get("client_assertion");
  if (!assertion) return;
  try {
    const payload = assertion.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const value = JSON.parse(atob(payload)) as { iss?: unknown };
    return typeof value.iss === "string" ? value.iss : undefined;
  } catch { return; }
}

/** Subclass in the Worker entrypoint and supply the application's provider. */
export class GrantCoordinator {
  private tail: Promise<void> = Promise.resolve();
  private state: DurableObjectState;
  private env: CoordinatorEnv;
  private exchange: Exchange;
  constructor(state: DurableObjectState, env: CoordinatorEnv, exchange: Exchange) {
    this.state = state; this.env = env; this.exchange = exchange;
  }

  async fetch(request: Request): Promise<Response> {
    if (new URL(request.url).pathname === "/__grant_operation") return this.operation(request);
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise(resolve => { release = resolve; });
    await previous;
    try { return await this.runExchange(request); }
    finally { release(); }
  }

  private async operation(request: Request): Promise<Response> {
    const data = await request.json() as { op: string; key: string; record?: Entry };
    if (!family(data.key)) return new Response(null, { status: 400 });
    if (data.op === "get") {
      const state = await this.state.storage.get<GrantState>("grant");
      const record = state && !state.revoked ? (data.key === state.grantKey ? state.grant : state.tokens[data.key]) : undefined;
      return Response.json({ initialized: !!state, record });
    }
    const ok = await this.state.storage.transaction(async tx => {
      const state = await tx.get<GrantState>("grant");
      if (data.op === "create") {
        if (state || !data.record || !data.key.startsWith("grant:")) return false;
        await tx.put("grant", { version: 1, revoked: false, grantKey: data.key, grant: data.record, tokens: {} } satisfies GrantState);
        if (data.record.expiresAt) await tx.setAlarm(data.record.expiresAt);
        return true;
      }
      if (data.op === "delete") {
        const next: GrantState = state || { version: 0, revoked: true, grantKey: `grant:${family(data.key)}`, tokens: {} };
        next.version++;
        if (data.key.startsWith("grant:") || !state) {
          next.revoked = true; next.revokedUntil = Date.now() + TOMBSTONE_MS; delete next.grant; next.tokens = {}; delete next.recovery; delete next.retired;
          await tx.setAlarm(next.revokedUntil);
        } else {
          delete next.tokens[data.key];
          if (next.recovery && data.key === `token:${family(next.grantKey)}:${next.recovery.accessTokenId}`) delete next.recovery;
        }
        await tx.put("grant", next);
        return true;
      }
      return false;
    });
    return new Response(null, { status: ok ? 204 : 409 });
  }

  async alarm(): Promise<void> {
    const now = Date.now();
    const state = await this.state.storage.transaction(async tx => {
      const current = await tx.get<GrantState>("grant");
      if (!current) return undefined;
      if (!current.revoked && (!current.grant?.expiresAt || current.grant.expiresAt > now)) {
        if (current.grant?.expiresAt) await tx.setAlarm(current.grant.expiresAt);
        return undefined;
      }
      if (!current.revoked) {
        current.version++; current.revoked = true; current.revokedUntil = now + TOMBSTONE_MS;
        delete current.grant; current.tokens = {}; delete current.recovery; delete current.retired;
        await tx.put("grant", current);
      }
      return current;
    });
    if (!state) return;
    try { await this.env.OAUTH_KV.delete(state.grantKey); }
    catch { await this.state.storage.setAlarm(Date.now() + 5 * 60 * 1000); return; }
    await this.state.storage.transaction(async tx => {
      const current = await tx.get<GrantState>("grant");
      if (!current || !current.revoked || current.version !== state.version) return;
      if ((current.revokedUntil ?? 0) > Date.now()) await tx.setAlarm(current.revokedUntil!);
      else await tx.delete("grant");
    });
  }

  private async runExchange(request: Request): Promise<Response> {
    const body = await request.clone().text();
    const form = new URLSearchParams(body);
    const type = form.get("grant_type");
    const credential = form.get(type === "refresh_token" ? "refresh_token" : "code") || "";
    const credentialHash = await digest(credential);
    const clientId = requestClientId(form, request.headers.get("Authorization"));
    // A private_key_jwt client must use a fresh assertion on each retry. The
    // provider authenticates it again before our recovery check, so the
    // assertion itself is excluded from otherwise identical request semantics.
    const semantic = [...form.entries()].filter(([key]) => key !== "client_assertion");
    semantic.sort(([ak, av], [bk, bv]) => ak.localeCompare(bk) || av.localeCompare(bv));
    const fingerprint = await digest(JSON.stringify([request.url, clientId, request.headers.get("Authorization"), request.headers.get("Origin"), request.headers.get("Content-Type"), semantic]));
    const snapshot = await this.state.storage.get<GrantState>("grant");
    // Legacy refresh credentials are not imported from eventually consistent
    // KV: an old replica could otherwise resurrect an already revoked grant.
    if (!snapshot || snapshot.revoked || !valueFor(snapshot.grant, "text")) return invalid();
    const pending = structuredClone(snapshot);
    const cache = pending.recovery;
    let changed = false;
    const raw = this.env.OAUTH_KV;
    const overlay = {
      async get(key: string, options?: unknown) {
        if (key === pending.grantKey) {
          // Provider client authentication ran before this get. Only recover an
          // exact previously authenticated request; no raw tokens are stored.
          if (type === "refresh_token" && cache?.credential === credentialHash) {
            const grant = valueFor(pending.grant, "json") as { clientId?: string } | null;
            if (!clientId || grant?.clientId !== clientId) throw new ConsumedCode();
            if (cache.until > Date.now() && cache.fingerprint === fingerprint) throw new RecoveryResult();
            // Within the recovery window, a differently scoped concurrent
            // request is refused without breaking the already-issued token.
            if (cache.until > Date.now()) throw new ConsumedCode();
          }
          if (type === "refresh_token" && pending.retired?.[credentialHash] !== undefined) {
            const until = pending.retired[credentialHash];
            if (until && until <= Date.now()) throw new ConsumedCode();
            const grant = valueFor(pending.grant, "json") as { clientId?: string } | null;
            if (clientId && grant?.clientId === clientId) throw new ReplayDetected();
            throw new ConsumedCode();
          }
          if (type === "authorization_code" && pending.consumedCode === credentialHash) throw new ConsumedCode();
          return valueFor(pending.grant, options);
        }
        if (family(key) === family(pending.grantKey)) return valueFor(pending.tokens[key], options);
        return raw.get(key, options as "text");
      },
      async put(key: string, value: string, options?: KVNamespacePutOptions) {
        if (key === pending.grantKey) { pending.grant = entry(value, options); changed = true; return; }
        if (family(key) === family(pending.grantKey)) { pending.tokens[key] = entry(value, options); changed = true; return; }
        return raw.put(key, value, options);
      },
      async delete(key: string) {
        if (key === pending.grantKey) { pending.revoked = true; delete pending.grant; pending.tokens = {}; delete pending.recovery; delete pending.retired; changed = true; return; }
        if (family(key) === family(pending.grantKey)) {
          delete pending.tokens[key];
          if (pending.recovery && key === `token:${family(pending.grantKey)}:${pending.recovery.accessTokenId}`) delete pending.recovery;
          changed = true; return;
        }
        return raw.delete(key);
      },
      async list(options?: KVNamespaceListOptions) {
        if (options?.prefix === `token:${family(pending.grantKey)}:`) return { keys: Object.keys(pending.tokens).map(name => ({ name })), list_complete: true, cacheStatus: null };
        return raw.list(options);
      },
      getWithMetadata: raw.getWithMetadata.bind(raw),
    } as unknown as KVNamespace;
    let response: Response;
    try {
      const env = { ...this.env, OAUTH_KV: overlay };
      response = await this.exchange(request, env, this.state as unknown as ExecutionContext);
    } catch (error) {
      if (error instanceof ConsumedCode) return invalid();
      if (error instanceof ReplayDetected) {
        const revoked = await this.state.storage.transaction(async tx => {
          const current = await tx.get<GrantState>("grant");
          if (!current || current.revoked || current.version !== snapshot.version) return false;
          current.version++; current.revoked = true; current.revokedUntil = Date.now() + TOMBSTONE_MS;
          delete current.grant; current.tokens = {}; delete current.recovery; delete current.retired;
          await tx.put("grant", current); await tx.setAlarm(current.revokedUntil);
          return true;
        });
        return revoked ? invalid() : unavailable();
      }
      if (!(error instanceof RecoveryResult) || !cache) return unavailable();
      const current = await this.state.storage.get<GrantState>("grant");
      if (!current || current.revoked || current.version !== snapshot.version) return unavailable();
      try {
        const recovered = JSON.parse(await unseal(credential, cache)) as { body: string; headers: [string, string][]; status: number };
        const payload = JSON.parse(recovered.body) as { expires_in?: unknown };
        if (typeof payload.expires_in === "number") payload.expires_in = Math.max(1, payload.expires_in - Math.floor((Date.now() - cache.issuedAt) / 1000));
        const headers = new Headers(recovered.headers); headers.delete("Content-Length");
        return new Response(JSON.stringify(payload), { status: recovered.status, headers });
      }
      catch { return unavailable(); }
    }
    if (response.ok) {
      if (!changed || pending.revoked || !pending.grant) return unavailable();
      let issued: { access_token?: unknown; refresh_token?: unknown };
      let saved: { refreshTokenId?: unknown };
      try {
        issued = JSON.parse(await response.clone().text()) as typeof issued;
        saved = JSON.parse(pending.grant.value) as typeof saved;
      } catch { return unavailable(); }
      if (typeof issued.access_token !== "string") return unavailable();
      const accessTokenId = await digest(issued.access_token);
      if (!pending.tokens[`token:${family(pending.grantKey)}:${accessTokenId}`]) return unavailable();
      if (issued.refresh_token !== undefined && (typeof issued.refresh_token !== "string" || saved.refreshTokenId !== await digest(issued.refresh_token))) return unavailable();
      if (type === "authorization_code") pending.consumedCode = credentialHash;
      if (type === "refresh_token") {
        const oldGrant = JSON.parse(snapshot.grant!.value) as { expiresAt?: number };
        pending.retired ??= {};
        for (const [hash, until] of Object.entries(pending.retired)) if (until && until <= Date.now()) delete pending.retired[hash];
        pending.retired[credentialHash] = oldGrant.expiresAt ? oldGrant.expiresAt * 1000 : snapshot.grant?.expiresAt ?? 0;
        const packet = JSON.stringify({ body: await response.clone().text(), headers: [...response.headers.entries()], status: response.status });
        const sealed = await seal(credential, packet);
        const issuedAt = Date.now();
        pending.recovery = { credential: credentialHash, fingerprint, accessTokenId, issuedAt, until: issuedAt + RECOVERY_MS, ...sealed };
      }
    }
    if (!changed) return response;
    for (const [key, record] of Object.entries(pending.tokens)) if (record.expiresAt && record.expiresAt <= Date.now()) delete pending.tokens[key];
    if (Object.keys(pending.tokens).length > 128) return unavailable();
    if (new TextEncoder().encode(JSON.stringify(pending)).byteLength > MAX_STATE) return unavailable();
    const committed = await this.state.storage.transaction(async tx => {
      const current = await tx.get<GrantState>("grant");
      if (!current || current.version !== snapshot.version || current.revoked) return false;
      pending.version = current.version + 1;
      if (pending.revoked) pending.revokedUntil = Date.now() + TOMBSTONE_MS;
      await tx.put("grant", pending);
      const alarmAt = pending.revoked ? pending.revokedUntil : pending.grant?.expiresAt;
      if (alarmAt) await tx.setAlarm(alarmAt);
      return true;
    });
    return committed ? response : unavailable();
  }
}
