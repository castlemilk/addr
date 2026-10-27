import { createRemoteJWKSet, jwtVerify, type JWTVerifyGetKey } from "jose";

export type FirebaseEnv = {
  FIREBASE_WEB_CONFIG?: string;
  FIREBASE_ALLOWED_EMAILS?: string;
  ANNOTATION_TOKEN: string;
  ANNOTATION_ALLOWED_ORIGINS: string;
  // "firebase" hands the browser the stored authDomain (<project>.firebaseapp.com); anything else (default) hands it this
  // Worker's own host, which serves Firebase's sign-in helper under /__/auth/ (firebaseHelperRoute).
  FIREBASE_AUTH_DOMAIN_MODE?: string;
  // The OAuth web client ID (public) for quick sign-in with the browser's Google account (Google Identity Services).
  GOOGLE_OAUTH_CLIENT_ID?: string;
  FIELDWORK_SHARED_WORKSPACES?: string;
  FIELDWORK_IDENTITY_BRIDGE_KEY?: string;
};
type WebConfig = { apiKey: string; authDomain: string; projectId: string; appId: string };

// Google's signing keys, never a URL supplied by the client or token header.
const googleKeys = createRemoteJWKSet(new URL("https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"));
export type ServiceIdentity = { uid:string; email:string; emailVerified:boolean; provider:string };
const verifiedRequests = new WeakMap<Request,ServiceIdentity>();
// This is process-local verification state, never a request-selected identity.
export const verifiedIdentity = (request:Request) => verifiedRequests.get(request);

export async function verifyServiceIdentity(token:string,env:FirebaseEnv,keys:JWTVerifyGetKey=googleKeys):Promise<ServiceIdentity> {
  const config=firebaseConfig(env);
  if(!config || token.length>16384)throw new Error("Sign-in is not configured.");
  const {payload}=await jwtVerify(token,judgedKeys(keys),{issuer:`https://securetoken.google.com/${config.projectId}`,audience:config.projectId,algorithms:["RS256"],requiredClaims:["exp","iat","sub","auth_time"],clockTolerance:30});
  const now=Math.floor(Date.now()/1000),provider=(payload.firebase as {sign_in_provider?:unknown}|undefined)?.sign_in_provider;
  if(typeof payload.sub!=="string" || !payload.sub || payload.sub.length>128 || typeof payload.iat!=="number" || payload.iat>now+30 || typeof payload.auth_time!=="number" || payload.auth_time<=0 || payload.auth_time>now+30 || !["google.com","password"].includes(String(provider)) || typeof payload.email!=="string")throw new Error("A normal Fieldwork account is required.");
  return {uid:payload.sub,email:payload.email,emailVerified:payload.email_verified===true,provider:String(provider)};
}

/** Google's signing keys could not be loaded (timeout, network, bad answer): the token was not judged at all. */
export class SigningKeysUnavailable extends Error {}
// Key lookups that fail because of the token itself (unknown or ambiguous key id) stay refusals.
const tokenKeyFaults = new Set(["ERR_JWKS_NO_MATCHING_KEY", "ERR_JWKS_MULTIPLE_MATCHING_KEYS"]);
function judgedKeys(keys: JWTVerifyGetKey): JWTVerifyGetKey {
  return async (header, token) => {
    try { return await keys(header, token); }
    catch (error) {
      if (tokenKeyFaults.has(String((error as { code?: unknown })?.code))) throw error;
      throw new SigningKeysUnavailable("Google's signing keys could not be loaded.");
    }
  };
}

export function firebaseConfig(env: FirebaseEnv): WebConfig | null {
  try {
    const value = JSON.parse(env.FIREBASE_WEB_CONFIG || "null");
    if (!value || !["apiKey", "authDomain", "projectId", "appId"].every(k => typeof value[k] === "string" && value[k].length > 0)) return null;
    if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(value.projectId) || !/^[a-z0-9.-]+$/.test(value.authDomain)) return null;
    // Return only the public Firebase web configuration, never admin credentials.
    return { apiKey: value.apiKey, authDomain: value.authDomain, projectId: value.projectId, appId: value.appId };
  } catch { return null; }
}

/** GOOGLE_OAUTH_CLIENT_ID when it is a well-formed OAuth web client ID, else null (then quick sign-in is not offered). */
export function googleClientId(env: FirebaseEnv): string | null {
  const value = env.GOOGLE_OAUTH_CLIENT_ID ?? "";
  return value.length <= 128 && /^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$/.test(value) ? value : null;
}

function allowedEmails(env: FirebaseEnv): Set<string> {
  return new Set((env.FIREBASE_ALLOWED_EMAILS || "").split(",").map(email => email.trim().toLowerCase()).filter(email => /^[^\s@*]+@[^\s@*]+\.[^\s@*]+$/.test(email)));
}

export async function verifyGoogleIdentity(token: string, env: FirebaseEnv, keys: JWTVerifyGetKey = googleKeys) {
  const config = firebaseConfig(env), allowed = allowedEmails(env);
  if (!config || !allowed.size || token.length > 16384) throw new Error("Sign-in is not configured.");
  const { payload } = await jwtVerify(token, judgedKeys(keys), {
    issuer: `https://securetoken.google.com/${config.projectId}`, audience: config.projectId,
    algorithms: ["RS256"], requiredClaims: ["exp", "iat", "sub", "auth_time"], clockTolerance: 30,
  });
  const now = Math.floor(Date.now()/1000);
  const firebase = payload.firebase as { sign_in_provider?: unknown } | undefined;
  if (typeof payload.sub !== "string" || !payload.sub || payload.sub.length > 128 || typeof payload.iat !== "number" || payload.iat > now + 30 ||
      typeof payload.auth_time !== "number" || payload.auth_time <= 0 || payload.auth_time > now + 30 ||
      payload.email_verified !== true || firebase?.sign_in_provider !== "google.com" ||
      typeof payload.email !== "string" || !allowed.has(payload.email.toLowerCase())) throw new Error("This Google account does not have workspace access.");
  return { uid: payload.sub, email: payload.email };
}

const headers = { "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff" };
function denied(status = 401, responseHeaders: HeadersInit = headers) {
  return Response.json({ code: status === 403 ? "permission_denied" : "unauthenticated", message: "Sign in to Fieldwork or use a valid workspace access token." }, { status, headers: responseHeaders });
}
// A token that could not be checked is not refused: 503 tells the browser to keep its remembered sign-in and retry later.
function notChecked(error: unknown, responseHeaders: Headers) {
  if (!(error instanceof SigningKeysUnavailable)) return denied(401, responseHeaders);
  responseHeaders.set("Retry-After", "5");
  return Response.json({ code: "unavailable", message: "Sign-in could not be checked right now. Retry shortly." }, { status: 503, headers: responseHeaders });
}
function authHeaders(request: Request) {
  const result = new Headers(headers);
  const origin = request.headers.get("Origin");
  if (origin) { result.set("Access-Control-Allow-Origin", origin); result.set("Vary", "Origin"); }
  return result;
}
function originAllowed(request: Request, env: FirebaseEnv) {
  const origin = request.headers.get("Origin");
  return !origin || origin === new URL(request.url).origin || env.ANNOTATION_ALLOWED_ORIGINS.split(",").map(s=>s.trim()).includes(origin);
}
async function workspaceToken(token: string, expected: string) {
  if (!token || !expected || token.length > 16384) return false;
  const encode = new TextEncoder();
  const [a, b] = await Promise.all([token, expected].map(s=>crypto.subtle.digest("SHA-256",encode.encode(s))));
  const left = new Uint8Array(a), right = new Uint8Array(b);
  let diff = 0; for (let i=0; i<left.length; i++) diff |= left[i]^right[i];
  return diff === 0;
}

export async function firebaseAuthRoute(request: Request, env: FirebaseEnv, keys?: JWTVerifyGetKey): Promise<Response> {
  if (!originAllowed(request, env)) return denied(403);
  const cors = authHeaders(request);
  if (request.method === "OPTIONS") {
    cors.set("Access-Control-Allow-Methods", "GET, OPTIONS");
    cors.set("Access-Control-Allow-Headers", "Authorization");
    return new Response(null, { status: 204, headers: cors });
  }
  if (request.method !== "GET") { cors.set("Allow", "GET, OPTIONS"); return new Response(null, {status:405,headers:cors}); }
  if (new URL(request.url).pathname === "/api/auth/config") {
    const config = firebaseConfig(env);
    // Same-origin sign-in helper: with a firebaseapp.com authDomain the popup's result is handed to a third-party iframe,
    // which Chrome's partitioned storage cuts off, so signInWithPopup ends as auth/popup-closed-by-user ("Sign-in was cancelled").
    // Google must list https://<this host>/__/auth/handler as an authorized redirect URI of the project's OAuth web client.
    if (config && env.FIREBASE_AUTH_DOMAIN_MODE !== "firebase") config.authDomain = new URL(request.url).host;
    // googleClientId turns on the browser's quick sign-in; Google accepts it only from the client's Authorized JavaScript origins.
    const clientId = googleClientId(env);
    const shared=env.FIELDWORK_SHARED_WORKSPACES==="true" && (env.FIELDWORK_IDENTITY_BRIDGE_KEY?.length??0)>=32;
    return Response.json(config && (shared || allowedEmails(env).size) ? { enabled:true, sharedWorkspaces:shared, firebase:config, ...(clientId ? { googleClientId:clientId } : {}) } : { enabled:false }, {headers:cors});
  }
  try {
    const match = /^Bearer ([^\s]+)$/.exec(request.headers.get("Authorization") || "");
    if (!match) return denied(401,cors);
    const user = env.FIELDWORK_SHARED_WORKSPACES==="true" ? await verifyServiceIdentity(match[1],env,keys) : await verifyGoogleIdentity(match[1],env,keys);
    return Response.json({user},{headers:cors});
  } catch (error) {return notChecked(error,cors);}
}

/** Authenticate at the edge, then use the existing private backend credential.
 * No bearer credential, cookie, or claimed user identity is returned to clients.
 * The backend's existing shared workspace audit actor is intentionally preserved.
 */
export async function authenticateFirebaseRequest(request: Request, env: FirebaseEnv, keys?: JWTVerifyGetKey): Promise<Request | Response> {
  if (!originAllowed(request,env)) return denied(403);
  if (request.method === "OPTIONS") return request;
  const cors = authHeaders(request);
  const match = /^Bearer ([^\s]+)$/.exec(request.headers.get("Authorization") || "");
  if (!match) return denied(401,cors);
  if (await workspaceToken(match[1],env.ANNOTATION_TOKEN)) return request;
  try {
    if(env.FIELDWORK_SHARED_WORKSPACES==="true"){
      if((env.FIELDWORK_IDENTITY_BRIDGE_KEY?.length??0)<32 || env.FIELDWORK_IDENTITY_BRIDGE_KEY===env.ANNOTATION_TOKEN)return denied(503,cors);
      const identity=await verifyServiceIdentity(match[1],env,keys);
      const forwarded=new Request(request);
      forwarded.headers.delete("X-Fieldwork-Identity");forwarded.headers.delete("Cookie");
      verifiedRequests.set(forwarded,identity);
      return forwarded;
    }
    await verifyGoogleIdentity(match[1],env,keys);
    if (!env.ANNOTATION_TOKEN || env.ANNOTATION_TOKEN.length < 24) return denied(401,cors);
    const forwarded = new Request(request);
    forwarded.headers.set("Authorization",`Bearer ${env.ANNOTATION_TOKEN}`);
    forwarded.headers.delete("Cookie");
    return forwarded;
  } catch (error) {return notChecked(error,cors);}
}

/** Firebase's sign-in helper pages, served from this origin (Firebase "proxy auth requests" option).
 * Only GET/HEAD of /__/auth/* and /__/firebase/init.json, only to <projectId>.firebaseapp.com (projectId is validated
 * in firebaseConfig), never forwarding credentials or cookies, never passing a Set-Cookie back. */
export function isFirebaseHelperPath(path: string): boolean {
  return path.startsWith("/__/auth/") || path === "/__/firebase/init.json";
}

export async function firebaseHelperRoute(request: Request, env: FirebaseEnv, upstreamFetch: typeof fetch = fetch): Promise<Response> {
  const config = firebaseConfig(env);
  const url = new URL(request.url);
  if (!config || !isFirebaseHelperPath(url.pathname)) return new Response("Not found", { status: 404, headers: { "Cache-Control": "no-store" } });
  if (request.method !== "GET" && request.method !== "HEAD") return new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } });
  const upstream = new URL(url.pathname + url.search, `https://${config.projectId}.firebaseapp.com`);
  const forward = new Headers();
  for (const name of ["Accept", "Accept-Language", "User-Agent"]) { const value = request.headers.get(name); if (value) forward.set(name, value); }
  const response = await upstreamFetch(upstream.toString(), { method: request.method, headers: forward, redirect: "manual" });
  const headers = new Headers(response.headers);
  headers.delete("Set-Cookie");
  return new Response(request.method === "HEAD" ? null : response.body, { status: response.status, statusText: response.statusText, headers });
}
