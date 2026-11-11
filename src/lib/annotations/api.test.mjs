import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);
const connect = require("@connectrpc/connect");
const code = ts.transpileModule(readFileSync("src/lib/annotations/api.ts", "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function fixture(handlers, firebaseToken = async () => "", recheckRefusedSignIn = async () => "") {
  const records = new Map(), calls = [], exports = {};
  globalThis.window = { location: { origin: "https://server-a.example" } };
  globalThis.localStorage = globalThis.sessionStorage = {
    getItem: key => records.get(key) ?? null,
    setItem: (key, value) => records.set(key, value), removeItem: key => records.delete(key),
  };
  new Function("exports", "require", code)(exports, id => {
    if (id === "./firebase") return { firebaseToken, recheckRefusedSignIn };
    if (id === "@connectrpc/connect-web") return { createConnectTransport: options => options };
    if (id.includes("annotation_connect")) return { AnnotationService: {} };
    if (id === "@connectrpc/connect") return {
      ...connect,
      createPromiseClient: (_, transport) => Object.fromEntries(Object.entries(handlers).map(([name, handler]) => [name, async request => {
        // Exercise the real auth interceptor before allowing a mock network call.
        const next = async req => {
          calls.push({ name, origin: transport.baseUrl, token: req.header.get("Authorization") });
          const data = await handler(request.data.toJson());
          return { data: { toJson: () => data } };
        };
        return transport.interceptors.reduceRight((next, intercept) => intercept(next), next)({ header: new Headers() });
      }])),
    };
    return require(id);
  });
  return { ...exports, calls };
}

const tooLarge = async () => { throw new connect.ConnectError("export exceeds 32 MiB; use paginated ListChanges", connect.Code.ResourceExhausted); };
test("large export replays updates and deletions across pages", async () => {
  const cursors = [];
  const client = fixture({ exportProject: tooLarge, listChanges: async ({cursor}) => {
    cursors.push(cursor);
    return cursor === "0" ? {
      changes: [
        { kind: "project", id: "p", operation: "create", document: { id: "p" } },
        { kind: "item", id: "one", operation: "create", document: { id: "one", revision: 1 } },
        { kind: "item", id: "gone", operation: "create", document: { id: "gone" } },
      ], nextCursor: "3", hasMore: true,
    } : { changes: [
      { kind: "item", id: "one", operation: "update", document: { id: "one", revision: 2 } },
      { kind: "item", id: "gone", operation: "delete", document: null },
    ], nextCursor: "5", hasMore: false };
  }});
  const result = await client.api.exportProject("p");
  assert.deepEqual(result.items, [{ id: "one", revision: 2 }]);
  assert.deepEqual(result.project, { id: "p" });
  assert.equal(result.cursor, "5"); assert.deepEqual(cursors, ["0", "3"]);
});

test("a connection change stops paginated exports before another credential is used", async () => {
  const client = fixture({ exportProject: tooLarge, listChanges: async () => {
    client.setConnection("https://server-b.example", "new-token");
    return { changes: [], nextCursor: "1", hasMore: true };
  }});
  await assert.rejects(() => client.api.exportProject("p"), error => error.code === connect.Code.Canceled);
  assert.equal(client.calls.length, 2);
  assert.ok(client.calls.every(call => call.origin === "https://server-a.example"));
});

test("API requests obtain a fresh Firebase token and preserve explicit workspace credentials", async () => {
  let generation = 0;
  const client = fixture({ getInfo: async () => ({authRequired:true}) }, async () => `google-${++generation}`);
  await client.api.info(); await client.api.info();
  client.setConnection("", "explicit-workspace-token");
  await client.api.info();
  assert.deepEqual(client.calls.map(call => call.token), ["Bearer google-1", "Bearer google-2", "Bearer explicit-workspace-token"]);
  assert.equal(generation, 2);
});

test("changing the connection during token refresh cancels the pending request", async () => {
  const client = fixture({ getInfo: async () => ({}) }, async () => {
    client.setConnection("https://server-b.example", "new-token"); return "old-google-token";
  });
  await assert.rejects(() => client.api.info(), error => error.code === connect.Code.Canceled);
  assert.equal(client.calls.length, 0);
});

test("a refused Google token gets one second look; a confirmed refusal is not retried; workspace tokens and other failures never do", async () => {
  const looked = [];
  let failure = connect.Code.Unauthenticated;
  const client = fixture({ getInfo: async () => { throw new connect.ConnectError("refused", failure); } },
    async () => "google-token", async (server, token) => { looked.push([server, token]); return ""; });
  await assert.rejects(() => client.api.info(), error => error.code === connect.Code.Unauthenticated);
  assert.deepEqual(looked, [["", "google-token"]]);
  assert.equal(client.calls.length, 1, "refused on the second look too: no retry");
  failure = connect.Code.PermissionDenied;
  await assert.rejects(() => client.api.info(), error => error.code === connect.Code.PermissionDenied);
  assert.equal(looked.length, 2);
  for (const other of [connect.Code.Unavailable, connect.Code.NotFound, connect.Code.Aborted]) {
    failure = other;
    await assert.rejects(() => client.api.info(), error => error.code === other);
  }
  assert.equal(looked.length, 2, "only 401/403 answers get a second look");
  client.setConnection("", "explicit-workspace-token");
  failure = connect.Code.Unauthenticated;
  await assert.rejects(() => client.api.info(), error => error.code === connect.Code.Unauthenticated);
  assert.equal(looked.length, 2, "a workspace token is never treated as a Google sign-in");
  assert.equal(client.calls.at(-1).token, "Bearer explicit-workspace-token");
});

test("a passing refusal (accepted on the second look) retries the request once with the fresh token", async () => {
  let refusals = 1;
  const saved = [];
  const client = fixture({ saveItem: async request => {
    if (refusals-- > 0) throw new connect.ConnectError("transient", connect.Code.Unauthenticated);
    saved.push(request.item.id); return { item: request.item };
  } }, async () => "google-token", async () => "fresh-google-token");
  const result = await client.api.saveItem({ id: "item-1", tags: ["roof"], revision: 3 });
  assert.equal(result.item.id, "item-1");
  assert.deepEqual(client.calls.map(call => call.token), ["Bearer google-token", "Bearer fresh-google-token"]);
  assert.deepEqual(saved, ["item-1"]);
  // Refused again after the retry: the error reaches the page; there is no second retry.
  refusals = 2;
  await assert.rejects(() => client.api.saveItem({ id: "item-2", tags: [], revision: 1 }), error => error.code === connect.Code.Unauthenticated);
  assert.equal(client.calls.length, 4);
});

test("a refusal that arrives after the connection changed gets no second look and no retry", async () => {
  const looked = [];
  const client = fixture({ getInfo: async () => {
    client.setConnection("https://server-b.example", "");
    throw new connect.ConnectError("refused", connect.Code.Unauthenticated);
  } }, async () => "google-token", async (...args) => { looked.push(args); return "fresh"; });
  await assert.rejects(() => client.api.info(), error => error.code === connect.Code.Unauthenticated);
  assert.deepEqual(looked, []);
  assert.equal(client.calls.length, 1);
  // The connection changes while the second look is under way: no retry under the new connection.
  const late = fixture({ getInfo: async () => { throw new connect.ConnectError("refused", connect.Code.Unauthenticated); } },
    async () => "google-token", async () => { late.setConnection("https://server-b.example", "other-token"); return "fresh"; });
  await assert.rejects(() => late.api.info(), error => error.code === connect.Code.Unauthenticated);
  assert.equal(late.calls.length, 1);
});
