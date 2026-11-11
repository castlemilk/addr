import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const origin = "https://addr-crm-mcp.ben-ebsworth.workers.dev";
const resource = `${origin}/mcp`;
const workspaceToken = "fixture-workspace-token-with-32-characters";
const ownerEmail = "owner@example.test";
const audience = "fixture-fieldwork-access-audience";
const userId = "fixture-access-owner";
const callback = "https://client.example/oauth/callback";
const fixture = await build({
  stdin: {
    contents: `import { createFieldworkOAuth } from './oauth.ts';
      import { insufficientScope } from '@cloudflare/workers-oauth-provider';
      export default createFieldworkOAuth({
        async fetch(request, env, ctx) {
          const needed = new URL(request.url).pathname === '/mcp/write'
            ? ['fieldwork:read', 'fieldwork:write'] : ['fieldwork:read'];
          if (!needed.every(scope => ctx.auth.scope.includes(scope))) return insufficientScope(ctx.auth, needed);
          return Response.json({ props: ctx.props, scope: ctx.auth.scope });
        }
      }, { fetch: () => new Response('unchanged fallback', { status: 404 }) });`,
    resolveDir: new URL(".", import.meta.url).pathname,
    sourcefile: "oauth-fixture.mjs",
    loader: "js",
  },
  bundle: true,
  platform: "neutral",
  mainFields: ["module", "main"],
  target: "es2022",
  format: "esm",
  external: ["cloudflare:*"],
  write: false,
});

test("hosted OAuth authorization in the Workers runtime", async (t) => {
  const workerOptions = {
    modules: true,
    script: fixture.outputFiles[0].text,
    compatibilityDate: "2026-10-01",
    compatibilityFlags: ["global_fetch_strictly_public", "nodejs_compat"],
    kvNamespaces: { OAUTH_KV: "shared-oauth-refresh-fixture" },
    bindings: { ANNOTATION_TOKEN: workspaceToken, ACCESS_AUD: audience, FIELDWORK_OWNER_EMAIL: ownerEmail },
  };
  const mf = new Miniflare(convertV4MiniflareOptions({
    workers: [
      { ...workerOptions, name: "interactive", access: { aud: audience, identity: { email: ownerEmail, user_uuid: userId } } },
      { ...workerOptions, name: "background" },
    ],
  }));
  t.after(() => mf.dispose());
  const send = (path, options = {}) => mf.dispatchFetch(`${origin}${path}`, { redirect: "manual", ...options });
  const register = async (name = "Fixture MCP client", redirect = callback) => {
    const response = await send("/oauth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_name: name, redirect_uris: [redirect], token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] }),
    });
    assert.equal(response.status, 201, await response.clone().text());
    return response.json();
  };
  const begin = async (client, scopes = ["fieldwork:read"], extra = {}) => {
    const verifier = randomBytes(32).toString("base64url");
    const query = new URLSearchParams({
      response_type: "code", client_id: client.client_id,
      redirect_uri: callback, state: "fixture-state",
      code_challenge: createHash("sha256").update(verifier).digest("base64url"),
      code_challenge_method: "S256", resource, scope: scopes.join(" "), ...extra,
    });
    const page = await send(`/authorize?${query}`);
    const html = await page.text();
    const handle = html.match(/name="handle" value="([^"]+)"/)?.[1];
    const cookie = page.headers.get("Set-Cookie")?.split(";")[0];
    return { verifier, query, page, html, handle, cookie, client };
  };
  const consent = (flow, selected = ["fieldwork:read"], options = {}) => send("/authorize", {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/x-www-form-urlencoded", Cookie: flow.cookie ?? "", ...options.headers },
    body: new URLSearchParams([
      ["handle", flow.handle ?? ""], ["decision", options.decision ?? "approve"],
      ...selected.map(scope => ["scope", scope]),
    ]),
  });
  const exchange = (flow, code, extra = {}) => send("/oauth/token", {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: flow.client.client_id, redirect_uri: callback, code_verifier: flow.verifier, code, resource, ...extra }),
  });
  const grant = async (flow, scopes = ["fieldwork:read"]) => {
    const approved = await consent(flow, scopes);
    assert.equal(approved.status, 302, await approved.clone().text());
    const url = new URL(approved.headers.get("Location"));
    assert.equal(url.origin, "https://client.example");
    assert.equal(url.searchParams.get("state"), "fixture-state");
    assert.equal(url.searchParams.get("iss"), origin);
    assert.equal(url.toString().includes(workspaceToken), false);
    const result = await exchange(flow, url.searchParams.get("code"));
    assert.equal(result.status, 200, await result.clone().text());
    return result.json();
  };

  await t.test("publishes resource-bound discovery and protects MCP", async () => {
    const protectedResponse = await send("/mcp");
    assert.equal(protectedResponse.status, 401);
    assert.match(protectedResponse.headers.get("WWW-Authenticate"), /scope="fieldwork:read"/);
    const metadata = await (await send("/.well-known/oauth-authorization-server")).json();
    assert.equal(metadata.issuer, origin);
    assert.equal(metadata.authorization_endpoint, `${origin}/authorize`);
    assert.equal(metadata.token_endpoint, `${origin}/oauth/token`);
    assert.equal(metadata.registration_endpoint, `${origin}/oauth/register`);
    assert.deepEqual(metadata.scopes_supported, ["fieldwork:read", "fieldwork:write"]);
    assert.deepEqual(metadata.code_challenge_methods_supported, ["S256"]);
    assert.equal(metadata.client_id_metadata_document_supported, true);
    const resourceMetadata = await (await send("/.well-known/oauth-protected-resource/mcp")).json();
    assert.equal(resourceMetadata.resource, resource);
    assert.deepEqual(resourceMetadata.scopes_supported, ["fieldwork:read"]);
    assert.equal((await send("/mcp-other")).status, 404);
  });

  await t.test("requires a trusted owner identity and the exact Access audience", async () => {
    for (const access of [
      undefined,
      { aud: "another-application", identity: { email: ownerEmail, user_uuid: userId } },
      { aud: audience, identity: { email: "other@example.test", user_uuid: "other-user" } },
      { aud: audience, identity: { user_uuid: userId } },
    ]) {
      const isolated = new Miniflare(convertV4MiniflareOptions({
        modules: true, script: fixture.outputFiles[0].text,
        compatibilityDate: "2026-10-01", compatibilityFlags: ["global_fetch_strictly_public", "nodejs_compat"],
        kvNamespaces: ["OAUTH_KV"],
        bindings: { ANNOTATION_TOKEN: workspaceToken, ACCESS_AUD: audience, FIELDWORK_OWNER_EMAIL: ownerEmail },
        ...(access ? { access } : {}),
      }));
      try {
        const spoofed = {
          "Cf-Access-Authenticated-User-Email": ownerEmail,
          "Cf-Access-Jwt-Assertion": "unverified.header.payload",
          Authorization: `Bearer ${workspaceToken}`,
        };
        const page = await isolated.dispatchFetch(`${origin}/authorize`, { headers: spoofed, redirect: "manual" });
        assert.equal(page.status, 403);
        assert.equal(page.headers.get("Set-Cookie"), null);
        assert.doesNotMatch(await page.text(), /<form|workspace_token|fixture-workspace-token/);
        const post = await isolated.dispatchFetch(`${origin}/authorize`, {
          method: "POST", redirect: "manual",
          headers: { ...spoofed, Origin: origin, "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ handle: "forged", decision: "approve", scope: "fieldwork:read", workspace_token: workspaceToken }),
        });
        assert.equal(post.status, 403);
        assert.equal(post.headers.get("Location"), null);
        const kv = await isolated.getKVNamespace("OAUTH_KV");
        assert.equal((await kv.list()).keys.length, 0, "unverified identity cannot create a consent transaction or grant");
        const discovery = await isolated.dispatchFetch(`${origin}/.well-known/oauth-authorization-server`);
        assert.equal(discovery.status, 200, "MCP clients can discover OAuth without an Access browser session");
      } finally {
        await isolated.dispose();
      }
    }
  });

  await t.test("renders escaped client identity, explicit consent and safe browser cookies", async () => {
    const client = await register('<img src=x onerror="alert(1)">');
    const flow = await begin(client, ["fieldwork:read", "fieldwork:write"]);
    assert.equal(flow.page.status, 200);
    assert.match(flow.html, /&#60;img/);
    assert.doesNotMatch(flow.html, /<img src=x/);
    assert.match(flow.html, /client\.example/);
    assert.match(flow.html, /Signed in as/);
    assert.match(flow.html, /owner@example\.test/);
    assert.doesNotMatch(flow.html, /type="password"|name="workspace_token"|Enter its workspace token/);
    assert.match(flow.html, /value="fieldwork:write">/);
    assert.doesNotMatch(flow.html, /value="fieldwork:write" checked/);
    assert.equal(flow.html.includes(workspaceToken), false);
    assert.match(flow.page.headers.get("Set-Cookie"), /__Host-/);
    assert.match(flow.page.headers.get("Set-Cookie"), /HttpOnly/);
    assert.match(flow.page.headers.get("Set-Cookie"), /Secure/);
    assert.match(flow.page.headers.get("Set-Cookie"), /SameSite=Lax|SameSite=Strict/);
    assert.equal(flow.page.headers.get("Cache-Control"), "no-store");
    assert.equal(flow.page.headers.get("Referrer-Policy"), "same-origin", "the browser must retain the consent form POST Origin without sending its URL to another origin");
    assert.match(flow.page.headers.get("Content-Security-Policy"), /frame-ancestors 'none'/);
    assert.match(flow.page.headers.get("Content-Security-Policy"), /form-action 'self' https:\/\/client\.example;/, "the verified callback is allowed for a browser form redirect");
    assert.doesNotMatch(flow.page.headers.get("Content-Security-Policy"), /form-action[^;]*\*/);
  });

  await t.test("form redirects allow only the verified callback origin including loopback ports", async () => {
    for (const redirect of [
      "http://127.0.0.1:49152/callback",
      "https://client.example/oauth/callback?next=https://hostile.example/x;unsafe=foo",
    ]) {
      const client = await register("Browser callback fixture", redirect);
      const flow = await begin(client, ["fieldwork:read"], { redirect_uri: redirect });
      assert.equal(flow.page.status, 200);
      const policy = flow.page.headers.get("Content-Security-Policy");
      assert.equal(policy.split("; ").find(value => value.startsWith("form-action")), `form-action 'self' ${new URL(redirect).origin}`);
      assert.doesNotMatch(policy, /hostile\.example|\/callback|unsafe=|form-action[^;]*\*/);
    }
    const unsupported = "https://*.client.example/callback";
    const client = await register("Wildcard callback fixture", unsupported);
    const flow = await begin(client, ["fieldwork:read"], { redirect_uri: unsupported });
    assert.equal(flow.page.status, 400);
    assert.equal(flow.page.headers.get("Set-Cookie"), null);
  });

  await t.test("rejects hostile origin and a different browser before authorizing", async () => {
    const client = await register();
    const flow = await begin(client);
    assert.equal((await consent(flow, ["fieldwork:read"], { headers: { Origin: "https://hostile.example" } })).status, 403);
    assert.equal((await consent(flow, ["fieldwork:read"], { headers: { Cookie: "" } })).status, 400);
    assert.equal((await consent(flow, ["fieldwork:read"], { headers: { Origin: "" } })).status, 403);
    assert.equal((await consent(flow, ["fieldwork:read"], { headers: { Origin: "null" } })).status, 403, "an opaque origin cannot authorize a grant");
    const oversized = await send("/authorize", {
      method: "POST", headers: { Origin: origin, "Content-Type": "application/x-www-form-urlencoded" },
      body: `workspace_token=${"x".repeat(8192)}`,
    });
    assert.equal(oversized.status, 413);
    const queryCredential = await send("/authorize?workspace_token=not-a-real-secret");
    assert.equal(queryCredential.status, 400);
    assert.equal((await queryCredential.text()).includes("not-a-real-secret"), false);
  });

  await t.test("read consent grants no write access, and never stores the workspace credential", async () => {
    const client = await register();
    const flow = await begin(client, ["fieldwork:read", "fieldwork:write"]);
    const tokens = await grant(flow);
    assert.equal(tokens.scope, "fieldwork:read");
    assert.equal(tokens.resource, resource);
    assert.equal(tokens.expires_in, 60 * 60);
    assert.ok(tokens.refresh_token);
    assert.equal(JSON.stringify(tokens).includes(workspaceToken), false);
    const read = await send("/mcp", { headers: { Authorization: `Bearer ${tokens.access_token}` } });
    assert.equal(read.status, 200);
    assert.deepEqual(await read.json(), { props: { userId, workspace: "fieldwork" }, scope: ["fieldwork:read"] });
    const write = await send("/mcp/write", { headers: { Authorization: `Bearer ${tokens.access_token}` } });
    assert.equal(write.status, 403);
    assert.match(write.headers.get("WWW-Authenticate"), /insufficient_scope/);
    assert.equal((await consent(flow)).status, 400, "a consent handle must be single-use");
    const kv = await mf.getKVNamespace("OAUTH_KV");
    const keys = await kv.list();
    for (const { name } of keys.keys) {
      assert.equal(name.includes(workspaceToken), false);
      assert.equal((await kv.get(name)).includes(workspaceToken), false);
    }
    const refresh = await send("/oauth/token", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", client_id: client.client_id, refresh_token: tokens.refresh_token, resource }),
    });
    assert.equal(refresh.status, 200, await refresh.clone().text());
    const refreshed = await refresh.json();
    assert.equal(refreshed.scope, "fieldwork:read");
    assert.notEqual(refreshed.refresh_token, tokens.refresh_token);
  });

  await t.test("background refresh needs no Access session, slides idle expiry and rejects an older rotated token", async () => {
    const client = await register();
    const tokens = await grant(await begin(client));
    assert.ok(tokens.refresh_token, "authorization-code exchange must issue a refresh token");
    assert.equal(tokens.expires_in, 60 * 60);
    const background = await mf.getWorker("background");
    const noSession = await background.fetch(`${origin}/authorize`, { redirect: "manual" });
    assert.equal(noSession.status, 403, "this Worker invocation has no trusted Access context");
    const [subject, grantId] = tokens.refresh_token.split(":");
    const key = `grant:${subject}:${grantId}`;
    const kv = await mf.getKVNamespace("OAUTH_KV");
    const originalGrant = await kv.get(key, { type: "json" });
    const now = Math.floor(Date.now() / 1000);
    assert.ok(Math.abs(originalGrant.expiresAt - now - 30 * 24 * 60 * 60) <= 5, "initial grant lifetime is explicitly thirty days");
    // A local fixture nearing idle expiry verifies that refreshing extends the
    // grant rather than retaining its original absolute end date.
    const almostExpired = { ...originalGrant, expiresAt: now + 120 };
    await kv.put(key, JSON.stringify(almostExpired), { expiration: almostExpired.expiresAt });
    const refresh = (refreshToken) => background.fetch(`${origin}/oauth/token`, {
      method: "POST", redirect: "manual", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", client_id: client.client_id, refresh_token: refreshToken, resource }),
    });
    const response = await refresh(tokens.refresh_token);
    assert.equal(response.status, 200, await response.clone().text());
    const renewed = await response.json();
    assert.equal(renewed.expires_in, 60 * 60);
    assert.equal(renewed.scope, "fieldwork:read");
    assert.equal(renewed.resource, resource);
    assert.notEqual(renewed.refresh_token, tokens.refresh_token);
    assert.notEqual(renewed.access_token, tokens.access_token);
    const extended = await kv.get(key, { type: "json" });
    assert.ok(extended.expiresAt > almostExpired.expiresAt);
    assert.ok(Math.abs(extended.expiresAt - Math.floor(Date.now() / 1000) - 30 * 24 * 60 * 60) <= 5, "refresh extends expiry to thirty days from this use");
    const read = await background.fetch(`${origin}/mcp`, { headers: { Authorization: `Bearer ${renewed.access_token}` } });
    assert.equal(read.status, 200);
    assert.deepEqual(await read.json(), { props: { userId, workspace: "fieldwork" }, scope: ["fieldwork:read"] });
    const denied = await background.fetch(`${origin}/mcp/write`, { headers: { Authorization: `Bearer ${renewed.access_token}` } });
    assert.equal(denied.status, 403, "a refresh cannot silently add write access");
    // The provider retains the previous refresh token for a lost-response retry.
    // Once its replacement is itself used to refresh, the older token is stale.
    const next = await refresh(renewed.refresh_token);
    assert.equal(next.status, 200, await next.clone().text());
    const rotatedAgain = await next.json();
    assert.notEqual(rotatedAgain.refresh_token, renewed.refresh_token);
    const replay = await refresh(tokens.refresh_token);
    assert.equal(replay.status, 400);
    assert.equal((await replay.json()).error, "invalid_grant");
    const stillValid = await refresh(rotatedAgain.refresh_token);
    assert.equal(stillValid.status, 200, "a stale-token rejection must not interrupt the current valid connection");
  });

  await t.test("grants write only after explicit selection within requested scopes", async () => {
    const client = await register();
    const readOnly = await begin(client, ["fieldwork:read"]);
    assert.doesNotMatch(readOnly.html, /name="scope" value="fieldwork:write"/);
    assert.equal((await consent(readOnly, ["fieldwork:read", "fieldwork:write"])).status, 400);
    const full = await begin(client, ["fieldwork:read", "fieldwork:write"]);
    const tokens = await grant(full, ["fieldwork:read", "fieldwork:write"]);
    assert.equal((await send("/mcp/write", { headers: { Authorization: `Bearer ${tokens.access_token}` } })).status, 200);
    const background = await mf.getWorker("background");
    const refresh = await background.fetch(`${origin}/oauth/token`, {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", client_id: client.client_id, refresh_token: tokens.refresh_token, resource }),
    });
    assert.equal(refresh.status, 200, await refresh.clone().text());
    const renewed = await refresh.json();
    assert.equal(renewed.scope, "fieldwork:read fieldwork:write");
    const write = await background.fetch(`${origin}/mcp/write`, { headers: { Authorization: `Bearer ${renewed.access_token}` } });
    assert.equal(write.status, 200);
    assert.deepEqual(await write.json(), { props: { userId, workspace: "fieldwork" }, scope: ["fieldwork:read", "fieldwork:write"] });
  });

  await t.test("denial creates no authorization code and needs no workspace credential", async () => {
    const flow = await begin(await register());
    const denied = await consent(flow, [], { decision: "deny" });
    assert.equal(denied.status, 302);
    const url = new URL(denied.headers.get("Location"));
    assert.equal(url.searchParams.get("error"), "access_denied");
    assert.equal(url.searchParams.get("code"), null);
    assert.equal(url.searchParams.get("state"), "fixture-state");
    assert.equal((await consent(flow)).status, 400);
  });

  await t.test("validates exact redirect URIs, S256 PKCE and token audiences", async () => {
    const client = await register();
    const wrongRedirect = await begin(client, ["fieldwork:read"], { redirect_uri: "https://hostile.example/cb" });
    assert.equal(wrongRedirect.page.status, 400);
    assert.equal(wrongRedirect.page.headers.get("Location"), null);
    const missingPkce = await begin(client, ["fieldwork:read"], { code_challenge: "", code_challenge_method: "" });
    assert.notEqual(missingPkce.page.status, 200);
    const plainPkce = await begin(client, ["fieldwork:read"], { code_challenge_method: "plain" });
    assert.notEqual(plainPkce.page.status, 200);
    const flow = await begin(client);
    const approved = await consent(flow);
    assert.equal(approved.status, 302);
    const code = new URL(approved.headers.get("Location")).searchParams.get("code");
    const wrongAudience = await exchange(flow, code, { resource: `${origin}/other-resource` });
    assert.equal(wrongAudience.status, 400);
    assert.equal((await wrongAudience.json()).error, "invalid_target");
    const wrongVerifier = await exchange(flow, code, { code_verifier: "a".repeat(43) });
    assert.equal(wrongVerifier.status, 400);
    const tokens = await grant(await begin(client));
    const retarget = await send("/oauth/token", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", client_id: client.client_id, refresh_token: tokens.refresh_token, resource: `${origin}/other-resource` }),
    });
    assert.equal(retarget.status, 400);
    assert.equal((await retarget.json()).error, "invalid_target");
    assert.equal((await send("/mcp", { headers: { Authorization: `Bearer ${workspaceToken}` } })).status, 401, "OAuth never treats a workspace token as an OAuth access token");
  });
});
