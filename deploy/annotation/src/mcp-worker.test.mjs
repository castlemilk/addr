import assert from "node:assert/strict";
import { createHash, randomBytes } from "node:crypto";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const origin = "https://addr-crm-mcp.ben-ebsworth.workers.dev";
const editorOrigin = "https://fieldwork-annotations.ben-ebsworth.workers.dev";
const token = "fixture-gateway-workspace-secret-never-returned";
const ownerEmail = "owner@example.test";
const audience = "fixture-gateway-access-audience";
const callback = "https://client.example/oauth/callback";
const bundle = await build({
  entryPoints: [fileURLToPath(new URL("./mcp-worker.ts", import.meta.url))],
  bundle: true, write: false, format: "esm", platform: "neutral", target: "es2022",
  mainFields: ["module", "main"], external: ["cloudflare:*"],
  plugins: [{
    name: "isolated-mcp-routing-fixture",
    setup(build) {
      // Keep the real Worker and real OAuth provider; replace only the SDK
      // adapter to inspect the identity, scope and backend binding it receives.
      build.onResolve({ filter: /^\.\/mcp$/ }, () => ({ path: "mcp", namespace: "routing-fixture" }));
      build.onLoad({ filter: /.*/, namespace: "routing-fixture" }, () => ({
        contents: `import { insufficientScope } from '@cloudflare/workers-oauth-provider';
          export async function handleFieldworkMcp(request, env, ctx, access) {
            if (request.headers.get('X-Fixture-Throw') === 'yes') throw new Error('Unexpected ' + access.upstreamAuthorization);
            const required = request.headers.get('X-Fixture-Operation') === 'write'
              ? ['fieldwork:read', 'fieldwork:write'] : ['fieldwork:read'];
            if (access.scopes !== undefined && !required.every(scope => access.scopes.includes(scope))) {
              return insufficientScope({ audience: '${origin}/mcp', token: '', scope: access.scopes }, required);
            }
            const upstream = await env.ANNOTATION_BACKEND.getByName('fieldwork').fetch(new Request(env.FIELDWORK_EDITOR_ORIGIN + '/api/v1/rpc/GetInfo', {
              method: 'POST', headers: { Authorization: access.upstreamAuthorization, 'Content-Type': 'application/json' }, body: '{}'
            }));
            const backend = await upstream.json();
            return Response.json({ legacy: access.scopes === undefined, scopes: access.scopes ?? null, backend, editorOrigin: env.FIELDWORK_EDITOR_ORIGIN });
          }`,
        loader: "js", resolveDir: fileURLToPath(new URL(".", import.meta.url)),
      }));
    },
  }],
});

test("the companion gateway composes legacy and scoped OAuth routes", async (t) => {
  const backendCalls = [];
  const mf = new Miniflare(convertV4MiniflareOptions({
    modules: true, script: bundle.outputFiles[0].text,
    compatibilityDate: "2026-10-02", compatibilityFlags: ["global_fetch_strictly_public"],
    kvNamespaces: ["OAUTH_KV"],
    bindings: { ANNOTATION_TOKEN: token, FIELDWORK_EDITOR_ORIGIN: editorOrigin, ACCESS_AUD: audience, FIELDWORK_OWNER_EMAIL: ownerEmail },
    access: { aud: audience, identity: { email: ownerEmail, user_uuid: "gateway-fixture-owner" } },
    serviceBindings: {
      FIELDWORK: async (request) => {
        assert.equal(new URL(request.url).origin, editorOrigin);
        assert.equal(request.headers.get("Authorization"), `Bearer ${token}`);
        assert.equal(await request.text(), "{}");
        backendCalls.push(new URL(request.url).pathname);
        return Response.json({ authenticated: true, workspace: "fieldwork-fixture" });
      },
    },
  }));
  t.after(() => mf.dispose());
  const send = (path, options = {}) => mf.dispatchFetch(`${origin}${path}`, { redirect: "manual", ...options });
  const authorized = (credential, headers = {}) => ({ headers: { Authorization: `Bearer ${credential}`, ...headers } });
  const grant = async (scopes) => {
    const registration = await send("/oauth/register", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_name: "Gateway fixture", redirect_uris: [callback], token_endpoint_auth_method: "none", grant_types: ["authorization_code", "refresh_token"], response_types: ["code"] }),
    });
    assert.equal(registration.status, 201, await registration.clone().text());
    const client = await registration.json();
    const verifier = randomBytes(32).toString("base64url");
    const query = new URLSearchParams({
      client_id: client.client_id, redirect_uri: callback, response_type: "code", state: "gateway-fixture",
      code_challenge: createHash("sha256").update(verifier).digest("base64url"), code_challenge_method: "S256",
      scope: scopes.join(" "), resource: `${origin}/mcp`,
    });
    const page = await send(`/authorize?${query}`);
    assert.equal(page.status, 200, await page.clone().text());
    assert.equal(page.headers.get("Referrer-Policy"), "same-origin");
    assert.match(page.headers.get("Content-Security-Policy"), /form-action 'self' https:\/\/client\.example;/);
    const html = await page.text();
    const handle = html.match(/name="handle" value="([^"]+)"/)?.[1];
    assert.ok(handle);
    const approved = await send("/authorize", {
      method: "POST", headers: { Origin: origin, "Content-Type": "application/x-www-form-urlencoded", Cookie: page.headers.get("Set-Cookie").split(";")[0] },
      body: new URLSearchParams([["handle", handle], ["decision", "approve"], ...scopes.map(scope => ["scope", scope])]),
    });
    assert.equal(approved.status, 302, await approved.clone().text());
    const code = new URL(approved.headers.get("Location")).searchParams.get("code");
    const response = await send("/oauth/token", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "authorization_code", client_id: client.client_id, redirect_uri: callback, code_verifier: verifier, code, resource: `${origin}/mcp` }),
    });
    assert.equal(response.status, 200, await response.clone().text());
    const tokens = await response.json();
    return tokens.access_token;
  };

  await t.test("health and informational routes need no workspace credential", async () => {
    const health = await send("/healthz");
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { status: "ok", service: "addr-crm-mcp" });
    const info = await send("/");
    assert.equal(info.status, 200);
    assert.match(await info.text(), /Connect your client to \/mcp using OAuth/);
    assert.equal((await send("/api/v1/rpc/GetInfo")).status, 404, "the companion does not expose backend REST routes");
    assert.equal(backendCalls.length, 0);
  });

  await t.test("legacy workspace clients retain access through the original service binding", async () => {
    const response = await send("/mcp", authorized(token, { Origin: editorOrigin, "X-Fixture-Operation": "write" }));
    assert.equal(response.status, 200, await response.clone().text());
    const value = await response.json();
    assert.deepEqual(value, { legacy: true, scopes: null, backend: { authenticated: true, workspace: "fieldwork-fixture" }, editorOrigin });
    assert.equal(JSON.stringify(value).includes(token), false);
    assert.deepEqual(backendCalls, ["/api/v1/rpc/GetInfo"]);
    const descendant = await send("/mcp/other", authorized(token));
    assert.equal(descendant.status, 401, "the legacy bypass is limited to the existing exact /mcp route");
  });

  await t.test("invalid workspace and OAuth bearers get a discoverable OAuth challenge", async () => {
    for (const credential of [null, "invalid-fixture-token", `${token}suffix`]) {
      const response = await send("/mcp", credential ? authorized(credential) : {});
      assert.equal(response.status, 401);
      const challenge = response.headers.get("WWW-Authenticate");
      assert.match(challenge, /resource_metadata="https:\/\/addr-crm-mcp\.ben-ebsworth\.workers\.dev\/\.well-known\/oauth-protected-resource\/mcp"/);
      assert.match(challenge, /scope="fieldwork:read"/);
      assert.equal((await response.text()).includes(token), false);
      assert.equal(challenge.includes(token), false);
    }
    assert.equal(backendCalls.length, 1);
  });

  await t.test("hostile origins cannot reach legacy or OAuth protected routes", async () => {
    for (const path of ["/mcp", "/healthz", "/oauth/register", "/authorize"]) {
      for (const rejectedOrigin of ["https://hostile.example", "null"]) {
        const response = await send(path, authorized(token, { Origin: rejectedOrigin }));
        assert.equal(response.status, 403);
        assert.equal((await response.text()).includes(token), false);
      }
    }
    assert.equal(backendCalls.length, 1);
  });

  await t.test("verified OAuth identity passes exact read and write scopes into the adapter", async () => {
    const readToken = await grant(["fieldwork:read"]);
    const read = await send("/mcp", authorized(readToken));
    assert.equal(read.status, 200);
    assert.equal((await read.clone().text()).includes(readToken), false);
    assert.deepEqual(await read.json(), { legacy: false, scopes: ["fieldwork:read"], backend: { authenticated: true, workspace: "fieldwork-fixture" }, editorOrigin });
    const beforeWrite = backendCalls.length;
    const hostile = await send("/mcp", authorized(readToken, { Origin: "https://hostile.example" }));
    assert.equal(hostile.status, 403);
    assert.equal(backendCalls.length, beforeWrite, "a valid OAuth token does not bypass the origin policy");
    const writeDenied = await send("/mcp", authorized(readToken, { "X-Fixture-Operation": "write" }));
    assert.equal(writeDenied.status, 403);
    assert.match(writeDenied.headers.get("WWW-Authenticate"), /insufficient_scope/);
    assert.equal(backendCalls.length, beforeWrite, "no backend dispatch after a scope refusal");
    const writeToken = await grant(["fieldwork:read", "fieldwork:write"]);
    const write = await send("/mcp", authorized(writeToken, { Origin: origin, "X-Fixture-Operation": "write" }));
    assert.equal(write.status, 200);
    assert.deepEqual((await write.json()).scopes, ["fieldwork:read", "fieldwork:write"]);
  });

  await t.test("unexpected adapter failures return a generic response for both authentication paths", async () => {
    const oauthToken = await grant(["fieldwork:read"]);
    for (const credential of [oauthToken, token]) {
      const response = await send("/mcp", authorized(credential, { "X-Fixture-Throw": "yes" }));
      assert.equal(response.status, 503, await response.clone().text());
      const body = await response.text();
      assert.equal(body.includes(token), false);
      assert.equal(body.includes(credential), false);
      assert.deepEqual(JSON.parse(body), { message: "The CRM connection is temporarily unavailable." });
      assert.equal(response.headers.get("Cache-Control"), "no-store");
      assert.equal(response.headers.get("Retry-After"), "5");
    }
  });
});
