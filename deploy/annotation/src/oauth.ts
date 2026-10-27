import {
  AuthorizationError,
  CimdFetchError,
  OAuthProvider,
  OAuthError,
  authorizationErrorRedirect,
  type ConsentDescription,
  type OAuthHelpers,
  type OAuthResourceContext,
} from "@cloudflare/workers-oauth-provider";
import type { McpEnvironment } from "./mcp-env";
import { sharedAuthorization,sharedConnection,sharedFailure,renewAccount,type RuntimeEnv } from "./shared-oauth";
import type { AccountSession } from "./shared-firebase";

export const FIELDWORK_ORIGIN = "https://addr-crm-mcp.ben-ebsworth.workers.dev";
export const FIELDWORK_RESOURCE = `${FIELDWORK_ORIGIN}/mcp`;
export const FIELDWORK_SCOPES = ["fieldwork:read", "fieldwork:write"] as const;

// An OAuth grant identifies the workspace, never its bearer credential. The
// resource handler obtains the upstream credential from the Worker secret.
export interface FieldworkOAuthProps {
  userId: string;
  workspace: "fieldwork";
}

export interface FieldworkOAuthHandler {
  fetch(request: Request, env: McpEnvironment, ctx: OAuthResourceContext<FieldworkOAuthProps|AccountSession>): Response | Promise<Response>;
}

type AuthorizationEnv = McpEnvironment & {
  OAUTH_PROVIDER: OAuthHelpers;
  ACCESS_AUD?: string;
  FIELDWORK_OWNER_EMAIL?: string;
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => `&#${character.charCodeAt(0)};`);
}

function privateHeaders(headers?: Headers): Headers {
  const result = new Headers(headers);
  result.set("Cache-Control", "no-store");
  result.set("Pragma", "no-cache");
  // Browsers serialize a form POST's Origin as "null" under no-referrer.
  // Preserve the same-origin POST identity without leaking the OAuth request
  // URL when returning to a client on another origin.
  result.set("Referrer-Policy", "same-origin");
  result.set("X-Content-Type-Options", "nosniff");
  result.set("X-Frame-Options", "DENY");
  result.set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
  return result;
}

function localError(message: string, status = 400): Response {
  const headers = privateHeaders();
  headers.set("Content-Type", "text/plain; charset=utf-8");
  return new Response(message, { status, headers });
}

async function readAuthorizationForm(request: Request): Promise<string | null> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 8192) {
        await reader.cancel();
        return null;
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(bytes);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(body);
}

function consentPage(details: ConsentDescription, handle: string, email: string): string {
  const name = escapeHtml(details.clientName);
  const client = details.clientDomain
    ? `Published by <strong>${escapeHtml(details.clientDomain)}</strong>.`
    : "This app registered its own name; its identity is not verified.";
  const writeRequested = details.scope.includes("fieldwork:write");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Fieldwork</title>
<style>body{font:16px system-ui,sans-serif;background:#f5f4ef;color:#19231f;margin:0;padding:48px 20px}main{max-width:520px;margin:auto;background:white;border:1px solid #deded5;border-radius:16px;padding:32px}h1{font-size:26px;line-height:1.2}p{line-height:1.5}label{display:block;margin:20px 0}button{font:inherit;padding:10px 18px;border:1px solid #aab3a9;border-radius:6px;margin:8px 8px 0 0;background:#f5f4ef}button[value=approve]{background:#244839;color:white}.detail{color:#56635a;font-size:14px}.warning{padding:12px;background:#fff0d4;border-radius:6px}</style></head>
<body><main><h1>Connect ${name} to Fieldwork</h1>
<p>${client} Access will be returned to <strong>${escapeHtml(details.redirectHost)}</strong>.</p>
${details.redirectIsLoopback ? '<p class="warning">This sends access to an app on your computer. Continue only if you just started connecting it.</p>' : ""}
<p>Signed in as <strong>${escapeHtml(email)}</strong>. Choose how this app may access your Fieldwork workspace.</p>
<form method="post" action="/authorize">
<input type="hidden" name="handle" value="${escapeHtml(handle)}">
<label><input type="checkbox" name="scope" value="fieldwork:read" checked required> Read projects, items, annotations and feedback</label>
${writeRequested ? '<label><input type="checkbox" name="scope" value="fieldwork:write"> Create and update items, annotations and feedback</label><p class="detail">Editing is optional and starts disabled. Enable it only if you want this app to save changes.</p>' : '<p class="detail">This connection requests read access. Saving changes requires a separate authorization for editing.</p>'}
<button name="decision" value="approve" type="submit">Allow connection</button><button name="decision" value="deny" type="submit" formnovalidate>Cancel</button>
</form></main></body></html>`;
}

export async function workspaceTokenMatches(provided: string, expected: string): Promise<boolean> {
  if (!provided || !expected || provided.length > 1024) return false;
  // Compare fixed-size digests without branching on credential bytes.
  const encoder = new TextEncoder();
  const [left, right] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(expected)),
  ]);
  const a = new Uint8Array(left);
  const b = new Uint8Array(right);
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a[index] ^ b[index];
  return difference === 0;
}

async function authorize(request: Request, env: AuthorizationEnv, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(request.url);
  if (url.origin !== FIELDWORK_ORIGIN) return localError("Use the canonical Fieldwork authorization address.");
  if (url.searchParams.has("workspace_token") || url.searchParams.has("token")) {
    return localError("Credentials must not appear in connection URLs.");
  }
  // Access authenticates this route before the Worker runs. Its runtime
  // context is trusted; an email/JWT header supplied by a caller is not.
  const access = ctx.access;
  if (!env.ACCESS_AUD || !env.FIELDWORK_OWNER_EMAIL || !access || access.aud !== env.ACCESS_AUD) {
    return localError("Sign in to Fieldwork to authorize this connection.", 403);
  }
  const identity = await access.getIdentity();
  const email = identity?.email?.toLowerCase();
  if (!email || email !== env.FIELDWORK_OWNER_EMAIL.toLowerCase()) {
    return localError("This account cannot authorize access to this workspace.", 403);
  }
  const userId = identity?.user_uuid || email;
  const oauth = env.OAUTH_PROVIDER;
  if (request.method === "GET") {
    try {
      const parsed = await oauth.parseAuthRequest(request);
      const scope = parsed.scope.length ? parsed.scope : ["fieldwork:read"];
      if (scope.some((value) => !FIELDWORK_SCOPES.includes(value as typeof FIELDWORK_SCOPES[number])) || !scope.includes("fieldwork:read")) {
        return new Response(null, {
          status: 302,
          headers: privateHeaders(new Headers({ Location: authorizationErrorRedirect(parsed, "invalid_scope") })),
        });
      }
      const authRequest = { ...parsed, scope };
      const details = await oauth.describeConsent(authRequest);
      const callbackOrigin = new URL(details.redirectUri).origin;
      // CSP host sources cannot safely represent an opaque origin, wildcard
      // hostname or IPv6 literal. Fail before issuing a consent transaction.
      if (!/^https?:\/\/[a-z0-9.-]+(?::[0-9]{1,5})?$/i.test(callbackOrigin)) {
        return localError("This callback address is not supported. Use an HTTPS or IPv4 loopback callback.");
      }
      // The provider binds the stored request to a one-use, ten-minute
      // __Host- HttpOnly/Secure cookie. Client URLs stay out of the form.
      const consent = await oauth.beginConsent(authRequest);
      const headers = privateHeaders(consent.headers);
      // Some browsers apply form-action to the POST's redirect as well. Allow
      // only this provider-validated client's callback origin alongside self;
      // the provider still enforces its exact registered redirect URI.
      headers.set("Content-Security-Policy", `default-src 'none'; style-src 'unsafe-inline'; form-action 'self' ${callbackOrigin}; frame-ancestors 'none'; base-uri 'none'`);
      headers.set("Content-Type", "text/html; charset=utf-8");
      return new Response(consentPage(details, consent.handle, email), { headers });
    } catch (error) {
      if (error instanceof AuthorizationError && error.redirectTo) {
        return new Response(null, { status: 302, headers: privateHeaders(new Headers({ Location: error.redirectTo })) });
      }
      if (error instanceof AuthorizationError || error instanceof CimdFetchError) {
        return localError("This connection request could not be verified. Start again from your app.");
      }
      throw error;
    }
  }
  if (request.method !== "POST") return localError("Method not allowed.", 405);
  if (request.headers.get("Origin") !== FIELDWORK_ORIGIN) return localError("This authorization form must be submitted from Fieldwork.", 403);
  if (!request.headers.get("Content-Type")?.toLowerCase().startsWith("application/x-www-form-urlencoded")) {
    return localError("Submit the authorization form.", 415);
  }
  const body = await readAuthorizationForm(request);
  if (body === null) return localError("The authorization form is too large.", 413);
  const form = new URLSearchParams(body);
  if (form.getAll("handle").length !== 1 || form.getAll("decision").length !== 1) return localError("Start a new connection request.");
  const handle = form.get("handle") ?? "";
  try {
    if (form.get("decision") === "deny") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: privateHeaders(denied.headers) });
    }
    if (form.get("decision") !== "approve") return localError("Choose whether to allow this connection.");
    const selected = [...new Set(form.getAll("scope"))];
    if (!selected.includes("fieldwork:read") || selected.some((value) => !FIELDWORK_SCOPES.includes(value as typeof FIELDWORK_SCOPES[number]))) {
      return localError("Choose a supported access level.");
    }
    // Read the original scopes from the server-held request. Never turn a
    // tampered form into a grant beyond what this client requested.
    const approved = await oauth.approveConsent(request, handle);
    if (selected.some((value) => !approved.request.scope.includes(value))) return localError("The requested access has changed. Start again from your app.");
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId,
      metadata: { workspace: "fieldwork" },
      scope: selected,
      props: { userId, workspace: "fieldwork" } satisfies FieldworkOAuthProps,
    });
    const headers = privateHeaders(approved.headers);
    headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers });
  } catch (error) {
    if (error instanceof AuthorizationError || error instanceof CimdFetchError) {
      return localError("This authorization form expired or could not be verified. Start again from your app.");
    }
    throw error;
  }
}

export function createFieldworkOAuth(
  apiHandler: FieldworkOAuthHandler,
  fallbackHandler: ExportedHandler<McpEnvironment>,
  shared=false,
): OAuthProvider<McpEnvironment> {
  return new OAuthProvider<McpEnvironment>({
    apiRoute: "/mcp",
    apiHandler: {
      fetch(request, env, ctx) {
        // OAuthProvider v1.2 populates auth and props after validating the token;
        // its legacy handler option still declares only ExecutionContext.
        return apiHandler.fetch(request, env, ctx as OAuthResourceContext<FieldworkOAuthProps|AccountSession>);
      },
    },
    defaultHandler: {
      async fetch(request, env, ctx) {
        const path=new URL(request.url).pathname;
        if (path === "/authorize") return authorize(request, env as AuthorizationEnv, ctx);
        if(shared){
          try{
            if(path==="/oauth/authorize")return await sharedAuthorization(request,env as RuntimeEnv & {OAUTH_PROVIDER:OAuthHelpers});
            if(path.startsWith("/connect/"))return await sharedConnection(request,env as RuntimeEnv & {OAUTH_PROVIDER:OAuthHelpers});
          }catch(error){
            if(error instanceof AuthorizationError || error instanceof CimdFetchError)return sharedFailure(request,env as RuntimeEnv,"This connection expired or belongs to another browser. Restart it from the intended assistant account.");
            if(error instanceof OAuthError)return sharedFailure(request,env as RuntimeEnv,error.description,error.statusCode);
            return sharedFailure(request,env as RuntimeEnv,"The service connection could not be completed. Retry shortly.",503);
          }
        }
        if (!fallbackHandler.fetch) return localError("Not found.", 404);
        return fallbackHandler.fetch(request, env, ctx);
      },
    },
    authorizeEndpoint: shared?"/oauth/authorize":"/authorize",
    tokenEndpoint: "/oauth/token",
    clientRegistrationEndpoint: "/oauth/register",
    scopesSupported: [...FIELDWORK_SCOPES],
    requiredScopes: ["fieldwork:read"],
    resourceMetadata: {
      resource: FIELDWORK_RESOURCE,
      authorization_servers: [FIELDWORK_ORIGIN],
      resource_name: "addr Fieldwork CRM",
    },
    clientIdMetadataDocumentEnabled: true,
    accessTokenTTL: 60 * 60,
    refreshTokenTTL: 30 * 24 * 60 * 60,
    // Dot may refresh while its connection is in use. Dormant grants expire.
    refreshTokenIdleTTL: 30 * 24 * 60 * 60,
    tokenExchangeCallback:async options=>{
      const props=options.props as FieldworkOAuthProps|AccountSession|undefined;
      // Older Access-owner grants continue to use only the original workspace.
      if(props && "workspace" in props && props.workspace==="fieldwork" && props.userId===options.userId)return {newProps:props};
      if(!shared || !props || !("uid" in props) || props.uid!==options.userId || typeof props.refreshToken!=="string" || typeof props.idToken!=="string")throw new OAuthError("invalid_grant",{description:"Reconnect Fieldwork using the intended service account."});
      return {newProps:await renewAccount(props,options.env as RuntimeEnv)};
    },
    onError: () => undefined,
  });
}
