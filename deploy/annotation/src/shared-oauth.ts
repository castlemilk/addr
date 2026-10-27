import {
  authorizationErrorRedirect,
  type OAuthHelpers,
} from "@cloudflare/workers-oauth-provider";
import { renewAccount, verifyIdentity } from "./shared-firebase";

export const READ_SCOPE = "fieldwork:read";
export type RuntimeEnv = McpEnvironment & {
  FIREBASE_API_KEY: string;
  FIREBASE_PROJECT_ID: string;
  APPLICATION_ORIGIN: string;
  MCP_ORIGIN: string;
};
import type { McpEnvironment } from "./mcp-env";
type AuthorizationEnv = RuntimeEnv & { OAUTH_PROVIDER: OAuthHelpers };
const escape = (value: string) =>
  value.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const digest = async (value: string) =>
  Array.from(
    new Uint8Array(
      await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
    ),
    (b) => b.toString(16).padStart(2, "0"),
  ).join("");
const noStore = {
  "Cache-Control": "private, no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};
const fail = (message: string, status = 400) =>
  Response.json({ error: message }, { status, headers: noStore });

function formPage(
  name: string,
  publisher: string | undefined,
  callback: string,
  handle: string,
): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect Fieldwork</title>
<style>body{font:16px system-ui;margin:0;padding:48px 20px;background:#f5f7fa;color:#16253a}main{max-width:540px;margin:auto;background:white;padding:32px;border:1px solid #dae1ea;border-radius:16px}p,li{line-height:1.6}button{font:inherit;padding:12px 18px;margin:12px 8px 0 0;border:1px solid #9aa6b7;border-radius:8px}button[value=approve]{background:#214db3;color:white}</style></head>
<body><main><h1>Connect ${escape(name)}</h1><p>${publisher ? `Published by <strong>${escape(publisher)}</strong>.` : "This app registered its own name; its publisher is not verified."} Access returns to <strong>${escape(callback)}</strong>.</p>
<p>This connection can read your private Fieldwork workspace, projects, items, labels and images. It cannot change annotations, approve human reviews or access local files.</p>
<p>Next, sign in to Fieldwork and confirm the account. Access tokens last 45 minutes. Renewal continues while used; 30 days of inactivity requires a new sign-in. You can revoke the connection.</p>
<form method="post" action="/oauth/authorize"><input type="hidden" name="handle" value="${escape(handle)}"><button type="submit" name="decision" value="approve">Continue to sign in</button><button type="submit" name="decision" value="deny">Cancel</button></form></main></body></html>`;
}

async function boundedBody(request: Request): Promise<string | null> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      total += chunk.value.length;
      if (total > 48 * 1024) {
        await reader.cancel();
        return null;
      }
      parts.push(chunk.value);
    }
  } finally {
    reader.releaseLock();
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return new TextDecoder().decode(joined);
}

function frontResponse(
  request: Request,
  env: RuntimeEnv,
  response: Response,
): Response {
  const headers = new Headers(response.headers);
  if (request.headers.get("Origin") === env.APPLICATION_ORIGIN) {
    headers.set("Access-Control-Allow-Origin", env.APPLICATION_ORIGIN);
    headers.set("Access-Control-Allow-Credentials", "true");
    headers.set("Vary", "Origin");
    headers.set("Access-Control-Expose-Headers", "X-Request-ID");
  }
  return new Response(response.body, { status: response.status, headers });
}

async function authorization(
  request: Request,
  env: AuthorizationEnv,
): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  if (request.method === "GET") {
    const parsed = await oauth.parseAuthRequest(request);
    const scope = parsed.scope.length ? parsed.scope : [READ_SCOPE];
    if (scope.some((s) => s !== READ_SCOPE))
      return new Response(null, {
        status: 302,
        headers: {
          ...noStore,
          Location: authorizationErrorRedirect(parsed, "invalid_scope"),
        },
      });
    const approved = { ...parsed, scope };
    const details = await oauth.describeConsent(approved);
    const consent = await oauth.beginConsent(approved);
    const headers = new Headers(consent.headers);
    // same-origin is needed for the form POST's real Origin in Chrome.
    headers.set("Referrer-Policy", "same-origin");
    headers.set("Content-Type", "text/html; charset=utf-8");
    headers.set(
      "Content-Security-Policy",
      "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    );
    return new Response(
      formPage(
        details.clientName,
        details.clientDomain,
        details.redirectHost,
        consent.handle,
      ),
      { headers },
    );
  }
  if (request.method !== "POST") return fail("Method not allowed.", 405);
  if (request.headers.get("Origin") !== env.MCP_ORIGIN)
    return fail("Start this connection from Fieldwork.", 403);
  if (
    !request.headers
      .get("Content-Type")
      ?.startsWith("application/x-www-form-urlencoded")
  )
    return fail("Submit the connection form.", 415);
  const body = await boundedBody(request);
  if (body === null) return fail("Connection form too large.", 413);
  const form = new URLSearchParams(body);
  if (
    form.getAll("handle").length !== 1 ||
    form.getAll("decision").length !== 1
  )
    return fail("Start a fresh connection.");
  const handle = form.get("handle")!;
  if (form.get("decision") === "deny") {
    const denied = await oauth.denyConsent(request, handle);
    return new Response(null, { status: 302, headers: denied.headers });
  }
  if (form.get("decision") !== "approve")
    return fail("Choose whether to continue.");
  const approved = await oauth.approveConsent(request, handle);
  if (
    approved.request.scope.length !== 1 ||
    approved.request.scope[0] !== READ_SCOPE
  )
    return fail("Unsupported access. Start again.");
  const details = await oauth.describeConsent(approved.request);
  const upstream = await oauth.beginUpstream(approved.request, {
    headers: approved.headers,
  });
  await env.OAUTH_KV.put(
    `signin-info:${await digest(upstream.state)}`,
    JSON.stringify({
      clientName: details.clientName,
      callbackHost: details.redirectHost,
      scope: READ_SCOPE,
    }),
    { expirationTtl: 600 },
  );
  const headers = new Headers(upstream.headers);
  headers.set(
    "Location",
    `${env.APPLICATION_ORIGIN}/mcp/connect?state=${encodeURIComponent(upstream.state)}`,
  );
  return new Response(null, { status: 303, headers });
}

async function connection(
  request: Request,
  env: AuthorizationEnv,
): Promise<Response> {
  const url = new URL(request.url);
  const origin = request.headers.get("Origin");
  if (origin !== env.APPLICATION_ORIGIN)
    return fail("Use the Fieldwork sign-in page.", 403);
  if (request.method === "OPTIONS")
    return frontResponse(
      request,
      env,
      new Response(null, {
        status: 204,
        headers: {
          ...noStore,
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
          "Access-Control-Allow-Headers": "Content-Type",
        },
      }),
    );
  if (url.pathname === "/connect/info" && request.method === "GET") {
    const state = url.searchParams.get("state") ?? "";
    if (!/^[a-zA-Z0-9_-]{32,256}$/.test(state))
      return frontResponse(request, env, fail("Invalid connection request."));
    const info = await env.OAUTH_KV.get(
      `signin-info:${await digest(state)}`,
      "json",
    );
    return frontResponse(
      request,
      env,
      info
        ? Response.json(info, { headers: noStore })
        : fail(
            "Connection request expired. Start again from your assistant.",
            410,
          ),
    );
  }
  if (url.pathname !== "/connect/complete" || request.method !== "POST")
    return frontResponse(request, env, fail("Method not allowed.", 405));
  if (!request.headers.get("Content-Type")?.startsWith("application/json"))
    return frontResponse(
      request,
      env,
      fail("Send a connection decision.", 415),
    );
  const raw = await boundedBody(request);
  if (raw === null)
    return frontResponse(
      request,
      env,
      fail("Connection response too large.", 413),
    );
  let body: Record<string, unknown>;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error();
    body = value as Record<string, unknown>;
  } catch {
    return frontResponse(request, env, fail("Invalid connection response."));
  }
  if (
    typeof body.state !== "string" ||
    !/^[a-zA-Z0-9_-]{32,256}$/.test(body.state) ||
    !["approve", "deny"].includes(String(body.decision))
  )
    return frontResponse(request, env, fail("Choose whether to connect."));
  const resumedUrl = new URL("/connect/complete", env.MCP_ORIGIN);
  resumedUrl.searchParams.set("state", body.state);
  const resumed = await env.OAUTH_PROVIDER.finishUpstream(
    new Request(resumedUrl, { headers: request.headers }),
  );
  await env.OAUTH_KV.delete(`signin-info:${await digest(body.state)}`);
  if (body.decision === "deny")
    return frontResponse(
      request,
      env,
      Response.json(
        {
          redirectTo: authorizationErrorRedirect(
            resumed.request,
            "access_denied",
          ),
        },
        { headers: resumed.headers },
      ),
    );
  if (
    resumed.request.scope.length !== 1 ||
    resumed.request.scope[0] !== READ_SCOPE
  )
    return frontResponse(request, env, fail("Unsupported connection scope."));
  if (typeof body.idToken !== "string" || typeof body.refreshToken !== "string")
    return frontResponse(
      request,
      env,
      fail("Sign in to the intended service account again.", 401),
    );
  const uid = await verifyIdentity(body.idToken, env);
  // Validate the renewable credential belongs to the SAME signed-in identity.
  // This is a user grant, never a saved desktop/owner service credential.
  const session = await renewAccount(
    { uid, idToken: body.idToken, refreshToken: body.refreshToken },
    env,
  );
  const result = await env.OAUTH_PROVIDER.completeAuthorization({
    request: resumed.request,
    userId: uid,
    scope: [READ_SCOPE],
    metadata: { service: "Fieldwork", access: "read-only" },
    props: session,
  });
  return frontResponse(
    request,
    env,
    Response.json(
      { redirectTo: result.redirectTo },
      { headers: resumed.headers },
    ),
  );
}

export { renewAccount } from "./shared-firebase";
export { authorization as sharedAuthorization, connection as sharedConnection };
export const sharedFailure = (
  request: Request,
  env: RuntimeEnv,
  message: string,
  status = 400,
) => frontResponse(request, env, fail(message, status));
