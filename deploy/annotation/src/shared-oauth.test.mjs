import assert from "node:assert/strict";
import { test } from "node:test";
import { createHash, randomBytes } from "node:crypto";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions, Log, LogLevel } from "miniflare";
import { generateKeyPair, exportJWK, SignJWT, jwtVerify } from "jose";

const origin = "https://addr-crm-mcp.ben-ebsworth.workers.dev";
const product = "https://fieldwork-annotations.ben-ebsworth.workers.dev";
const callback = "https://assistant.example/callback";
const project = "fixture-fieldwork";
const { privateKey, publicKey } = await generateKeyPair("RS256");
const jwk = await exportJWK(publicKey);
jwk.kid = "fixture-key";
jwk.alg = "RS256";
jwk.use = "sig";
const jwt = async (uid) =>
  new SignJWT({
    email: `${uid}@example.test`,
    auth_time: Math.floor(Date.now() / 1000),
    email_verified: true,
    firebase: { sign_in_provider: "password" },
  })
    .setProtectedHeader({ alg: "RS256", kid: "fixture-key" })
    .setSubject(uid)
    .setIssuedAt()
    .setExpirationTime("1h")
    .setAudience(project)
    .setIssuer(`https://securetoken.google.com/${project}`)
    .sign(privateKey);
const identities = { alpha: await jwt("alpha"), beta: await jwt("beta") };
const calls = [];
const disabled = new Set();
const fixture = await build({
  entryPoints: [new URL("./mcp-worker.ts", import.meta.url).pathname],
  bundle: true,
  platform: "neutral",
  mainFields: ["module", "main"],
  target: "es2022",
  format: "esm",
  external: ["cloudflare:*"],
  write: false,
  plugins: [
    {
      name: "synthetic-domain-reader",
      setup(build) {
        build.onResolve({ filter: /^\.\/mcp$/ }, () => ({
          path: "fixture",
          namespace: "synthetic",
        }));
        build.onLoad({ filter: /.*/, namespace: "synthetic" }, () => ({
          contents: `export async function handleFieldworkMcp(request,env,ctx,access){
 if(access.scopes?.includes('fieldwork:write'))return new Response('unexpected write',{status:500});
 const response=await env.ANNOTATION_BACKEND.getByName('fieldwork').fetch(new Request(env.FIELDWORK_EDITOR_ORIGIN+'/api/v1/info',{headers:{Authorization:access.upstreamAuthorization}}));
 return response;
 }`,
          loader: "js",
        }));
      },
    },
  ],
});

async function outgoing(request) {
  const url = new URL(request.url);
  if (
    url.hostname === "www.googleapis.com" &&
    url.pathname.includes("securetoken@system.gserviceaccount.com")
  ) {
    return Response.json(
      { keys: [jwk] },
      { headers: { "Cache-Control": "public,max-age=3600" } },
    );
  }
  if (url.hostname === "securetoken.googleapis.com") {
    const body = new URLSearchParams(await request.text());
    const refresh = body.get("refresh_token");
    const uid = refresh?.replace(/^fixture-refresh-/, "");
    calls.push({ operation: "renew", uid });
    if (!identities[uid] || disabled.has(uid))
      return Response.json(
        { error: { message: "INVALID_REFRESH_TOKEN" } },
        { status: 400 },
      );
    return Response.json({
      user_id: uid,
      id_token: identities[uid],
      refresh_token: refresh,
      expires_in: "3600",
    });
  }
  throw new Error("Unexpected outbound service request");
}

function cookieJar() {
  const cookies = new Map();
  return {
    absorb(response) {
      for (const cookie of response.headers.getSetCookie()) {
        const pair = cookie.split(";", 1)[0];
        const index = pair.indexOf("=");
        if (index > 0) {
          const key = pair.slice(0, index),
            value = pair.slice(index + 1);
          if (value) cookies.set(key, value);
          else cookies.delete(key);
        }
      }
    },
    header() {
      return [...cookies].map(([k, v]) => `${k}=${v}`).join("; ");
    },
  };
}

test("real Worker OAuth, protocol, account isolation and read boundaries", async (t) => {
  const mf = new Miniflare(
    convertV4MiniflareOptions({
      modules: true,
      script: fixture.outputFiles[0].text,
      compatibilityDate: "2026-10-02",
      compatibilityFlags: ["nodejs_compat", "global_fetch_strictly_public"],
      kvNamespaces: ["OAUTH_KV"],
      bindings: {
        MCP_ORIGIN: origin,
        APPLICATION_ORIGIN: product,
        FIREBASE_PROJECT_ID: project,
        FIREBASE_API_KEY: "fixture-public-configuration",
        FIELDWORK_SHARED_WORKSPACES: "true",
        FIELDWORK_EDITOR_ORIGIN: product,
        ANNOTATION_TOKEN: "legacy-owner-credential-not-used-for-users",
      },
      serviceBindings: {
        FIELDWORK: async (request) => {
          const token = request.headers
            .get("Authorization")
            ?.replace(/^Bearer /, "");
          assert.notEqual(token, "legacy-owner-credential-not-used-for-users");
          const { payload } = await jwtVerify(token, publicKey, {
            issuer: `https://securetoken.google.com/${project}`,
            audience: project,
          });
          return Response.json({
            account: payload.sub,
            workspace: `private-${payload.sub}`,
          });
        },
      },
      outboundService: outgoing,
      log: new Log(LogLevel.ERROR),
    }),
  );
  t.after(() => mf.dispose());
  const send = (path, init = {}) =>
    mf.dispatchFetch(`${origin}${path}`, { redirect: "manual", ...init });
  const registration = async () => {
    const r = await send("/oauth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_name: "Synthetic MCP reviewer",
        redirect_uris: [callback],
        token_endpoint_auth_method: "none",
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
      }),
    });
    assert.equal(r.status, 201, await r.clone().text());
    return r.json();
  };
  const registered = await registration();
  const begin = async (scope = "fieldwork:read") => {
    const jar = cookieJar(),
      verifier = randomBytes(32).toString("base64url");
    const query = new URLSearchParams({
      client_id: registered.client_id,
      redirect_uri: callback,
      response_type: "code",
      state: "fixture-client-state",
      code_challenge: createHash("sha256").update(verifier).digest("base64url"),
      code_challenge_method: "S256",
      resource: `${origin}/mcp`,
      scope,
    });
    const r = await send(`/oauth/authorize?${query}`);
    jar.absorb(r);
    return { r, jar, verifier };
  };
  const continueSignin = async (flow) => {
    const html = await flow.r.text();
    const handle = /name="handle" value="([^"]+)"/.exec(html)?.[1];
    assert.ok(handle);
    const r = await send("/oauth/authorize", {
      method: "POST",
      headers: {
        Origin: origin,
        Cookie: flow.jar.header(),
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: new URLSearchParams({ handle, decision: "approve" }),
    });
    assert.equal(r.status, 303, await r.clone().text());
    flow.jar.absorb(r);
    const next = new URL(r.headers.get("Location"));
    assert.equal(next.origin, product);
    flow.state = next.searchParams.get("state");
    assert.ok(flow.state);
    return flow;
  };
  const complete = async (flow, uid, patch = {}, headers = {}) => {
    const r = await send("/connect/complete", {
      method: "POST",
      headers: {
        Origin: product,
        Cookie: flow.jar.header(),
        "Content-Type": "application/json",
        ...headers,
      },
      body: JSON.stringify({
        state: flow.state,
        decision: "approve",
        idToken: identities[uid],
        refreshToken: `fixture-refresh-${uid}`,
        ...patch,
      }),
    });
    flow.jar.absorb(r);
    return r;
  };
  const exchange = async (flow, code) => {
    const r = await send("/oauth/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        client_id: registered.client_id,
        redirect_uri: callback,
        code_verifier: flow.verifier,
        resource: `${origin}/mcp`,
      }),
    });
    assert.equal(r.status, 200, await r.clone().text());
    return r.json();
  };
  const grant = async (uid) => {
    const flow = await continueSignin(await begin());
    const info = await send(`/connect/info?state=${flow.state}`, {
      headers: { Origin: product },
    });
    assert.equal(info.status, 200);
    assert.equal((await info.json()).scope, "fieldwork:read");
    const r = await complete(flow, uid);
    assert.equal(r.status, 200, await r.clone().text());
    const destination = new URL((await r.json()).redirectTo);
    assert.equal(destination.origin, new URL(callback).origin);
    assert.equal(destination.searchParams.get("state"), "fixture-client-state");
    return {
      flow,
      tokens: await exchange(flow, destination.searchParams.get("code")),
    };
  };
  await t.test(
    "public metadata and missing credentials give a real OAuth challenge",
    async () => {
      for (const path of [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
      ]) {
        const r = await send(path);
        assert.equal(r.status, 200);
        const body = await r.json();
        assert.ok(JSON.stringify(body).includes(origin));
      }
      const r = await send("/mcp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      assert.equal(r.status, 401);
      assert.match(r.headers.get("WWW-Authenticate"), /resource_metadata/);
    },
  );
  await t.test("unknown/write scopes never become a grant", async () => {
    const flow = await begin("fieldwork:write");
    assert.notEqual(flow.r.status, 200);
  });
  await t.test(
    "wrong origin and missing browser cookie are refused before account renewal",
    async () => {
      const flow = await continueSignin(await begin());
      const before = calls.length;
      assert.equal(
        (
          await complete(
            flow,
            "alpha",
            {},
            { Origin: "https://hostile.example" },
          )
        ).status,
        403,
      );
      assert.equal(
        (await complete(flow, "alpha", {}, { Cookie: "" })).status,
        400,
      );
      assert.equal(calls.length, before);
    },
  );
  await t.test(
    "cancel does not issue credentials or read account data",
    async () => {
      const flow = await continueSignin(await begin());
      const before = calls.length;
      const r = await complete(flow, "alpha", { decision: "deny" });
      assert.equal(r.status, 200);
      assert.equal(
        new URL((await r.json()).redirectTo).searchParams.get("error"),
        "access_denied",
      );
      assert.equal(calls.length, before);
    },
  );
  await t.test(
    "renewable credential from another identity is rejected",
    async () => {
      const flow = await continueSignin(await begin());
      const r = await complete(flow, "alpha", {
        refreshToken: "fixture-refresh-beta",
      });
      assert.equal(r.status, 400);
      assert.match((await r.json()).error, /another service account/);
    },
  );
  const alpha = await grant("alpha"),
    beta = await grant("beta");

  await t.test(
    "independent renewable grants forward their own identities, never the owner credential",
    async () => {
      const read = async (token) => {
        const r = await send("/mcp", {
          headers: { Authorization: `Bearer ${token}` },
        });
        assert.equal(r.status, 200, await r.clone().text());
        return r.json();
      };
      const [a, b] = await Promise.all([
        read(alpha.tokens.access_token),
        read(beta.tokens.access_token),
      ]);
      assert.deepEqual(a, { account: "alpha", workspace: "private-alpha" });
      assert.deepEqual(b, { account: "beta", workspace: "private-beta" });
    },
  );
  await t.test(
    "renewal preserves identity and service revocation refuses new access",
    async () => {
      const refresh = async (token) =>
        send("/oauth/token", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            grant_type: "refresh_token",
            refresh_token: token,
            client_id: registered.client_id,
            resource: `${origin}/mcp`,
          }),
        });
      const r = await refresh(alpha.tokens.refresh_token);
      assert.equal(r.status, 200, await r.clone().text());
      const renewed = await r.json();
      assert.notEqual(renewed.refresh_token, alpha.tokens.refresh_token);
      disabled.add("alpha");
      const revoked = await refresh(renewed.refresh_token);
      assert.equal(revoked.status, 400);
      assert.equal((await revoked.json()).error, "invalid_grant");
    },
  );
});
