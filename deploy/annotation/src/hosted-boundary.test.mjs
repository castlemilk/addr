import assert from "node:assert/strict";
import { test } from "node:test";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { generateKeyPair, exportJWK, SignJWT } from "jose";
import { randomUUID, createHash } from "node:crypto";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const backendURL = process.env.FIELDWORK_TEST_BACKEND_URL;
test(
  "real edge identity to PostgreSQL and private R2 workspace boundaries",
  { skip: !backendURL },
  async (t) => {
    const backend = new URL(backendURL);
    assert.equal(backend.protocol, "http:");
    assert.ok(
      ["127.0.0.1", "localhost"].includes(backend.hostname),
      "test backend must be disposable loopback",
    );
    const origin = "https://fieldwork.example",
      project = "fieldwork-test";
    const { privateKey, publicKey } = await generateKeyPair("RS256");
    const jwk = await exportJWK(publicKey);
    jwk.kid = "fixture";
    jwk.alg = "RS256";
    const uidA = "ordinary-a-" + randomUUID(),
      uidB = "ordinary-b-" + randomUUID();
    const jwt = (uid) =>
      new SignJWT({
        email: uid + "@example.test",
        email_verified: true,
        firebase: { sign_in_provider: "password" },
        auth_time: Math.floor(Date.now() / 1000),
      })
        .setSubject(uid)
        .setAudience(project)
        .setIssuer("https://securetoken.google.com/" + project)
        .setIssuedAt()
        .setExpirationTime("1h")
        .setProtectedHeader({ alg: "RS256", kid: "fixture" })
        .sign(privateKey);
    const [a, b] = await Promise.all([jwt(uidA), jwt(uidB)]);
    const fixture = await build({
      stdin: {
        contents: `import worker from './worker.ts';export default {fetch(request,env,ctx){return worker.fetch(request,{...env,ANNOTATION_BACKEND:{getByName:()=>env.BACKEND}},ctx)}};`,
        resolveDir: new URL(".", import.meta.url).pathname,
        loader: "js",
      },
      bundle: true,
      write: false,
      format: "esm",
      platform: "neutral",
      target: "es2022",
      mainFields: ["module", "main"],
      external: ["cloudflare:*"],
      plugins: [
        {
          name: "no-container-lifecycle",
          setup(build) {
            build.onResolve({ filter: /^@cloudflare\/containers$/ }, () => ({
              path: "container",
              namespace: "fixture",
            }));
            build.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
              contents: "export class Container{}",
              loader: "js",
            }));
          },
        },
      ],
    });
    const mf = new Miniflare(
      convertV4MiniflareOptions({
        modules: true,
        script: fixture.outputFiles[0].text,
        compatibilityDate: "2026-10-02",
        compatibilityFlags: ["nodejs_compat", "global_fetch_strictly_public"],
        r2Buckets: ["ANNOTATION_BLOBS"],
        bindings: {
          DATABASE_URL: "disposable-backend-through-fixture-binding",
          ANNOTATION_TOKEN: "synthetic-legacy-workspace-token-at-least-24",
          FIELDWORK_SHARED_WORKSPACES: "true",
          FIELDWORK_IDENTITY_BRIDGE_KEY:
            "synthetic-identity-bridge-key-at-least-32-bytes",
          ANNOTATION_ALLOWED_ORIGINS: origin,
          FIREBASE_WEB_CONFIG: JSON.stringify({
            apiKey: "public-fixture-config",
            authDomain: "fieldwork-test.firebaseapp.com",
            projectId: project,
            appId: "fixture",
          }),
        },
        outboundService: (request) => {
          assert.equal(new URL(request.url).hostname, "www.googleapis.com");
          return Response.json(
            { keys: [jwk] },
            { headers: { "Cache-Control": "public,max-age=3600" } },
          );
        },
        serviceBindings: {
          BACKEND: async (request) => {
            // Preserve the final signed path, query and bytes, without involving a
            // container lifecycle or any real Cloudflare/Google account.
            const url = new URL(request.url);
            return fetch(
              new Request(new URL(url.pathname + url.search, backend), request),
            );
          },
        },
      }),
    );
    t.after(() => mf.dispose());
    const send = (token, path, options = {}) =>
      mf.dispatchFetch(origin + path, {
        ...options,
        headers: {
          Authorization: "Bearer " + token,
          "Content-Type": "application/json",
          ...options.headers,
        },
      });
    const read = async (token, path) => {
      const r = await send(token, path);
      assert.equal(r.status, 200, await r.clone().text());
      return r.json();
    };
    const save = async (token, path, body) => {
      const r = await send(token, path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      assert.equal(r.status, 200, await r.clone().text());
      return r.json();
    };
    const [infoA, infoB] = await Promise.all([
      read(a, "/api/v1/info"),
      read(b, "/api/v1/info"),
    ]);
    assert.notEqual(infoA.workspace.id, infoB.workspace.id);
    assert.notEqual(infoA.workspace.id, "legacy");
    for (const [token, name] of [
      [a, "alpha"],
      [b, "beta"],
    ])
      await save(token, "/api/v1/projects", {
        project: { id: "same-project", name },
        expectedRevision: 0,
      });
    assert.equal((await read(a, "/api/v1/projects")).projects[0].name, "alpha");
    assert.equal((await read(b, "/api/v1/projects")).projects[0].name, "beta");
    await save(a, "/api/v1/items", {
      item: {
        id: "private-a",
        projectId: "same-project",
        adapter: "image",
        title: "Synthetic private alpha",
        source: {
          url: "https://example.test/photo.jpg",
          width: 100,
          height: 100,
        },
        annotations: [],
        status: "unreviewed",
      },
      expectedRevision: 0,
    });
    assert.equal(
      (
        await send(b, "/api/v1/items/private-a", {
          headers: {
            "X-Fieldwork-Workspace": infoA.workspace.id,
            "X-Fieldwork-Identity": "forged",
          },
        })
      ).status,
      404,
    );
    const bytes = Buffer.from("synthetic private image evidence"),
      digest = createHash("sha256").update(bytes).digest("hex");
    const asset = "/api/v1/assets/" + digest;
    const upload = await send(a, asset, {
      method: "PUT",
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Length": String(bytes.length),
      },
      body: bytes,
    });
    assert.equal(upload.status, 201, await upload.clone().text());
    assert.equal((await send(b, asset)).status, 404);
    assert.equal(
      (
        await send(b, asset, {
          method: "HEAD",
          headers: { "X-Fieldwork-Workspace": infoA.workspace.id },
        })
      ).status,
      404,
    );
    assert.deepEqual(
      Buffer.from(await (await send(a, asset)).arrayBuffer()),
      bytes,
    );
    const client = async (token) => {
      const c = new Client({
        name: "ordinary-user-verification",
        version: "1",
      });
      await c.connect(
        new StreamableHTTPClientTransport(new URL(origin + "/mcp"), {
          requestInit: { headers: { Authorization: "Bearer " + token } },
          fetch: (url, init) => mf.dispatchFetch(url, init),
        }),
      );
      t.after(() => c.close());
      return c;
    };
    const [ca, cb] = await Promise.all([client(a), client(b)]);
    const pa = await ca.callTool({ name: "list_projects", arguments: {} }),
      pb = await cb.callTool({ name: "list_projects", arguments: {} });
    assert.match(JSON.stringify(pa), /alpha/);
    assert.doesNotMatch(JSON.stringify(pa), /beta/);
    assert.match(JSON.stringify(pb), /beta/);
    const denied = await cb.callTool({
      name: "get_item",
      arguments: { id: "private-a" },
    });
    assert.equal(denied.isError, true);
    assert.doesNotMatch(JSON.stringify(denied), /Synthetic private alpha/);
  },
);
