import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

// Agents never edit a review project (an id ending in -review): the main Worker's /mcp proxy and the hosted MCP gateway refuse
// write tools aimed at one, in any spelling, and leave reads and the browser's REST / Connect saves alone.
const load = async (entry, stubs = {}) => {
  const bundle = await build({
    entryPoints: [fileURLToPath(new URL(entry, import.meta.url))],
    bundle: true, write: false, format: "esm", platform: "browser", target: "es2022",
    plugins: [{
      name: "stubs",
      setup(build) {
        for (const [module, contents] of Object.entries(stubs)) {
          build.onResolve({ filter: new RegExp(`^${module.replace(/[/@.]/g, "\\$&")}$`) }, () => ({ path: module, namespace: "stub" }));
        }
        build.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({ contents: stubs[args.path] }));
      },
    }],
  });
  return import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
};
const guard = await load("./mcp-guard.ts");
const worker = (await load("./worker.ts", {
  "@cloudflare/containers": "export class Container { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }",
})).default;
const { handleFieldworkMcp } = await load("./mcp.ts");

const REVIEW_REFUSAL = "agents never edit a review project";
const SPELLINGS = ["mooroolbark-review", "MOOROOLBARK-REVIEW", "Mooroolbark-Review", "mooroolbark-review ", "\tmooroolbark-review\n",
  "mooroolbark－review", "Ｍｏｏｒｏｏｌｂａｒｋ-review", "mooroolbark‐review",
  "mooroolbark−review", "mooroolbark-review​", "mooroolbark-rev­iew", "mooroolbark-review ", "mooroolbark-review　"];

test("project ids are normalised before the -review check", () => {
  for (const id of SPELLINGS) {
    assert.equal(guard.normalizeProjectId(id), "mooroolbark-review", JSON.stringify(id));
    assert.equal(guard.isReviewProject(id), true, JSON.stringify(id));
  }
  for (const id of ["mooroolbark", "MOOROOLBARK", "review", "mooroolbark-reviews", "x-ʀeview", 7, null]) {
    assert.equal(guard.isReviewProject(id), false, JSON.stringify(id));
  }
  assert.equal(guard.normalizeProjectId("Kelvinſ"), "kelvins");
});

test("only the canonical agent-assessment tool may write separate reviews", async () => {
  const lookup = fakeLookup();
  assert.equal(await guard.reviewToolRefusal("save_agent_review", { itemId: "r1008-1", expectedRevision: 2 }, lookup), null);
  assert.deepEqual(lookup.seen, []);
});

test("fields are read in every key spelling the Go decoder accepts", () => {
  const args = { Item: { ProjectID: "a", projectid: "b", "projectİd": "not-folded" }, item: { projectId: "c" }, "itemIK": "x" };
  assert.deepEqual(guard.fieldStrings(args, "item", "projectId").sort(), ["a", "b", "c"]);
  assert.deepEqual(guard.fieldValues({ "argumentſ": 1, ARGUMENTS: 2 }, "arguments").sort(), [1, 2]);
});

function fakeLookup(items = {}, labels = {}) {
  const seen = [];
  return {
    seen,
    async itemProject(id) {
      seen.push(["item", id]);
      if (!(id in items)) throw new Error("not found");
      return items[id];
    },
    async labelReviewProject(id) {
      seen.push(["label", id]);
      if (labels[id] === "fail") throw new Error("unavailable");
      return labels[id] ?? null;
    },
  };
}

test("tool calls are refused by declared project, item lookup and label lookup", async () => {
  const lookup = fakeLookup({ "r1008-1": "mooroolbark-review", "addr-scene-1": "mooroolbark" }, { "mooroolbark-review-proposed": "mooroolbark-review", broken: "fail" });
  const refused = [
    ["save_item", { item: { id: "r1008-new", projectId: "MOOROOLBARK-REVIEW" }, expectedRevision: 0 }],
    ["save_item", { item: { id: "r1008-new", projectId: "mooroolbark", ProjectId: "mooroolbark－review" }, expectedRevision: 0 }],
    ["save_label", { label: { id: "l", PROJECTID: "x-review " }, expectedRevision: 0 }],
    ["save_project", { project: { Id: "Mooroolbark-Review" }, expectedRevision: 0 }],
    ["save_annotation", { itemId: "r1008-1", annotation: { id: "a" }, expectedRevision: 2 }],
    ["delete_annotation", { ItemId: "r1008-1", itemId: "addr-scene-1", id: "a", expectedRevision: 2 }],
    ["delete_item", { id: "r1008-1", expectedRevision: 2 }],
    ["delete_label", { id: "mooroolbark-review-proposed", expectedRevision: 1 }],
    // An update naming another project would still move the stored review item (or label) out of the review project.
    ["save_item", { item: { id: "r1008-1", projectId: "mooroolbark" }, expectedRevision: 3 }],
    ["save_label", { label: { id: "mooroolbark-review-proposed", projectId: "mooroolbark" }, expectedRevision: 2 }],
    ["unknown_write", { projectId: "mooroolbark-review" }],
  ];
  for (const [tool, args] of refused) {
    const refusal = await guard.reviewToolRefusal(tool, args, lookup);
    assert.ok(refusal?.includes(REVIEW_REFUSAL), `${tool} ${JSON.stringify(args)}: ${refusal}`);
  }
  // Lookups that cannot answer refuse rather than guess.
  for (const [tool, args, why] of [
    ["save_annotation", { itemId: "missing", annotation: {}, expectedRevision: 1 }, "cannot check which project item missing belongs to"],
    ["delete_label", { id: "broken", expectedRevision: 1 }, "cannot check whether label broken belongs to a review project"],
  ]) {
    assert.match(await guard.reviewToolRefusal(tool, args, lookup), new RegExp(why));
  }
  lookup.seen.length = 0;
  const allowed = [
    ["get_item", { id: "r1008-1" }], ["list_items", { projectId: "mooroolbark-review" }], ["export_project", { projectId: "mooroolbark-review" }],
    ["save_item", { item: { id: "addr-scene-2", projectId: "mooroolbark" }, expectedRevision: 0 }],
    ["save_item", { item: { id: "addr-scene-1", projectId: "mooroolbark" }, expectedRevision: 4 }],
    ["save_annotation", { itemId: "addr-scene-1", annotation: { id: "a" }, expectedRevision: 2 }],
    ["delete_label", { id: "mooroolbark-building", expectedRevision: 1 }],
  ];
  for (const [tool, args] of allowed) assert.equal(await guard.reviewToolRefusal(tool, args, lookup), null, `${tool} ${JSON.stringify(args)}`);
  // Creates and reads need no lookup.
  assert.deepEqual(lookup.seen, [["item", "addr-scene-1"], ["item", "addr-scene-1"], ["label", "mooroolbark-building"]]);
});

// ---------------------------------------------------------------------------------------------- main Worker /mcp proxy

const token = "fixture-workspace-token-0123456789abcdef";
const origin = "https://fieldwork.example";
function backendFixture(options = {}) {
  const requests = [];
  const fetch = async (request) => {
    const url = new URL(request.url);
    const text = request.method === "GET" ? "" : await request.text();
    requests.push({ path: url.pathname, method: request.method, authorization: request.headers.get("Authorization"), text });
    if (url.pathname === "/mcp") return Response.json({ jsonrpc: "2.0", id: 1, result: { content: [], forwarded: true } });
    const body = text ? JSON.parse(text) : {};
    switch (url.pathname) {
      case "/api/v1/rpc/GetItem": {
        if (options.getItemStatus) return Response.json({ code: "unavailable" }, { status: options.getItemStatus });
        const projectId = body.id.startsWith("r1008-") ? "mooroolbark-review" : body.id.startsWith("addr-scene-") ? "mooroolbark" : null;
        return projectId ? Response.json({ item: { id: body.id, projectId, revision: 2 }, labels: [] }) : Response.json({ code: "not_found" }, { status: 404 });
      }
      case "/api/v1/rpc/ListProjects":
        return Response.json({ projects: [{ id: "mooroolbark" }, { id: "mooroolbark-review" }], nextCursor: "" });
      case "/api/v1/rpc/ListLabels":
        return Response.json({ labels: body.projectId === "mooroolbark-review" ? [{ id: "mooroolbark-review-proposed" }] : [], nextCursor: "" });
      default:
        return Response.json({ path: url.pathname, forwarded: true });
    }
  };
  const env = {
    ANNOTATION_TOKEN: token, DATABASE_URL: "postgres://fixture", ANNOTATION_ALLOWED_ORIGINS: origin,
    ANNOTATION_BACKEND: { getByName: (name) => { assert.equal(name, "fieldwork"); return { fetch }; } },
    ASSETS: { fetch: async () => new Response("spa") },
  };
  const send = (path, body, init = {}) => worker.fetch(new Request(`${origin}${path}`, {
    method: init.method ?? "POST", body: init.method === "GET" ? undefined : typeof body === "string" ? body : JSON.stringify(body),
    headers: { Authorization: init.authorization ?? `Bearer ${token}`, "Content-Type": "application/json",
      Accept: "application/json, text/event-stream", ...init.headers },
  }), env);
  const forwarded = () => requests.filter((r) => r.path === "/mcp" || !r.path.startsWith("/api/v1/rpc/Get") && !r.path.startsWith("/api/v1/rpc/List"));
  return { send, requests, forwarded };
}
const call = (id, name, args, keys = {}) => ({ jsonrpc: "2.0", id, [keys.method ?? "method"]: "tools/call",
  [keys.params ?? "params"]: { [keys.name ?? "name"]: name, [keys.arguments ?? "arguments"]: args } });

test("the main Worker refuses review-project MCP writes before they reach the backend", async () => {
  const cases = [
    call(1, "save_annotation", { itemId: "r1008-1-shadow-002", annotation: { id: "a" }, expectedRevision: 2 }),
    call(2, "save_item", { item: { id: "r1008-x", projectId: "MOOROOLBARK-REVIEW " }, expectedRevision: 0 }),
    call(3, "save_item", { item: { id: "r1008-x", ProjectId: "mooroolbark－review" }, expectedRevision: 0 }, { method: "Method", params: "PARAMS", name: "Name" }),
    call(4, "delete_item", { id: "r1008-1-shadow-002", expectedRevision: 2 }, { arguments: "argumentſ" }),
    call(5, "delete_label", { id: "mooroolbark-review-proposed", expectedRevision: 1 }),
    call(6, "save_item", { item: { id: "r1008-1-shadow-002", projectId: "mooroolbark" }, expectedRevision: 3 }),
  ];
  for (const message of cases) {
    const { send, requests, forwarded } = backendFixture();
    const response = await send("/mcp", message);
    assert.equal(response.status, 200);
    const payload = await response.json();
    assert.equal(payload.id, message.id);
    assert.equal(payload.result.isError, true);
    assert.match(payload.result.content[0].text, new RegExp(REVIEW_REFUSAL));
    assert.deepEqual(forwarded(), [], JSON.stringify(message));
    assert.ok(requests.every((r) => r.authorization === `Bearer ${token}`));
  }
});

test("the main Worker refuses a batch holding one review-project write, whole", async () => {
  const read = call(11, "get_item", { id: "r1008-1-shadow-002" });
  const write = call(12, "save_annotation", { itemId: "r1008-1-shadow-002", annotation: { id: "a" }, expectedRevision: 2 });
  const { send, forwarded } = backendFixture();
  for (const batch of [[read, write], [read, [write]]]) {
    const payload = await (await send("/mcp", batch)).json();
    assert.deepEqual(payload.map((entry) => entry.id), [11, 12]);
    assert.ok(payload.every((entry) => entry.result.isError && entry.result.content[0].text.startsWith("refusing the whole MCP batch")));
  }
  const { id: _, ...notification } = write;
  const quiet = await send("/mcp", [notification]);
  assert.equal(quiet.status, 202);
  assert.equal(await quiet.text(), "");
  assert.deepEqual(forwarded(), []);
});

test("the main Worker forwards reads, other projects, other routes and the browser's saves unchanged", async () => {
  const { send, requests, forwarded } = backendFixture();
  const passes = [
    ["/mcp", call(21, "get_item", { id: "r1008-1-shadow-002" })],
    ["/mcp", call(22, "list_items", { projectId: "mooroolbark-review" })],
    ["/mcp", { jsonrpc: "2.0", id: 23, method: "tools/list" }],
    ["/mcp", call(24, "save_annotation", { itemId: "addr-scene-x", annotation: { id: "a" }, expectedRevision: 2 })],
    ["/mcp", [call(25, "get_item", { id: "addr-scene-x" }), call(26, "save_item", { item: { id: "addr-scene-y", projectId: "mooroolbark" }, expectedRevision: 0 })]],
    // The reviewer's own saves go through REST / Connect: never inspected here.
    ["/api/v1/rpc/SaveItem", { item: { id: "r1008-1-shadow-002", projectId: "mooroolbark-review" }, expectedRevision: 2 }],
    ["/annotation.v1.AnnotationService/SaveAnnotation", { itemId: "r1008-1-shadow-002", annotation: { id: "a" }, expectedRevision: 2 }],
  ];
  for (const [path, body] of passes) {
    const response = await send(path, body);
    assert.equal(response.status, 200, path);
    const payload = await response.json();
    assert.equal(payload.forwarded ?? payload.result?.forwarded, true, path);
  }
  const sent = forwarded();
  assert.equal(sent.filter((r) => r.path === "/mcp").length, 5);
  assert.deepEqual(sent.filter((r) => r.path !== "/mcp").map((r) => r.path), ["/api/v1/rpc/SaveItem", "/annotation.v1.AnnotationService/SaveAnnotation"]);
  // The forwarded body is the client's own bytes; only the save to another project needed a lookup.
  assert.equal(sent[0].text, JSON.stringify(passes[0][1]));
  assert.deepEqual(requests.filter((r) => r.path === "/api/v1/rpc/GetItem").map((r) => JSON.parse(r.text).id), ["addr-scene-x"]);
  const get = await send("/mcp", undefined, { method: "GET" });
  assert.equal(get.status, 200);
  assert.equal(forwarded().at(-1).method, "GET");
});

test("the main Worker refuses a tools/call that repeats its arguments, and forwards a tools/call exactly as it checked it", async () => {
  // Go's encoding/json merges every key that folds to "arguments" into one map; JSON.parse keeps only the last copy.
  const write = JSON.stringify({ item: { id: "r1008-x", projectId: "mooroolbark-review", title: "t" }, expectedRevision: 0 });
  // The same key twice: the guard checks the last copy, and the backend receives only that copy (never the review write).
  const duplicated = [
    `{"jsonrpc":"2.0","id":41,"method":"tools/call","params":{"name":"save_item","arguments":${write},"arguments":{}}}`,
    `{"jsonrpc":"2.0","id":42,"method":"tools/call","params":{"name":"save_annotation","arguments":{"itemId":"r1008-1-shadow-002","annotation":{"id":"a"},"expectedRevision":2},"arguments":{}}}`,
  ];
  for (const body of duplicated) {
    const { send, forwarded } = backendFixture();
    await send("/mcp", body);
    const sent = forwarded().filter((r) => r.path === "/mcp");
    assert.ok(sent.every((r) => r.text === JSON.stringify(JSON.parse(body)) && !r.text.includes("r1008-")), body);
  }
  // Two spellings the backend folds together: refused, nothing forwarded.
  const respelled = [
    `{"jsonrpc":"2.0","id":43,"method":"tools/call","params":{"name":"save_item","arguments":${write},"ARGUMENTS":{}}}`,
    `{"jsonrpc":"2.0","id":44,"method":"tools/call","params":{"name":"save_item","arguments":{"item":{"id":"r1008-1-shadow-002","projectId":"mooroolbark"}},"Arguments":{"expectedRevision":3}}}`,
  ];
  for (const body of respelled) {
    const { send, forwarded } = backendFixture();
    const payload = await (await send("/mcp", body)).json();
    assert.equal(payload.result.isError, true, body);
    assert.match(payload.result.content[0].text, /refusing/);
    assert.deepEqual(forwarded(), [], body);
  }
  // A read that repeats a key the guard reads last-wins (JSON.parse) is forwarded re-serialised: the backend sees one copy only.
  const { send, forwarded } = backendFixture();
  const read = `{"jsonrpc":"2.0","id":45,"method":"tools/call","params":{"name":"get_item","arguments":{"id":"r1008-1-shadow-002"}},"params":{"name":"get_item","arguments":{"id":"addr-scene-x"}}}`;
  assert.equal((await send("/mcp", read)).status, 200);
  const [sent] = forwarded();
  assert.equal(sent.text, JSON.stringify(JSON.parse(read)));
  assert.equal(sent.text.split('"params"').length - 1, 1);
  // Anything that is not a tools/call keeps the client's own bytes.
  const list = `{"jsonrpc":"2.0", "id":46, "method":"tools/list"}`;
  await send("/mcp", list);
  assert.equal(forwarded().at(-1).text, list);
});

test("the main Worker refuses when the lookup fails or the body cannot be read, and authenticates first", async () => {
  const failing = backendFixture({ getItemStatus: 503 });
  const payload = await (await failing.send("/mcp", call(31, "delete_annotation", { itemId: "addr-scene-x", id: "a", expectedRevision: 2 }))).json();
  assert.match(payload.result.content[0].text, /cannot check which project item addr-scene-x belongs to/);
  assert.deepEqual(failing.forwarded(), []);
  const { send, requests } = backendFixture();
  const parse = await (await send("/mcp", "{\"jsonrpc\":\"2.0\",")).json();
  assert.equal(parse.error.code, -32700);
  const large = await send("/mcp", "x".repeat(4 * 1024 * 1024 + 1));
  assert.equal(large.status, 413);
  const unauthenticated = await send("/mcp", call(32, "save_annotation", { itemId: "r1008-1", annotation: {}, expectedRevision: 2 }), { authorization: "Bearer wrong" });
  assert.equal(unauthenticated.status, 401);
  assert.deepEqual(requests, []);
});

// --------------------------------------------------------------------------------------------- hosted MCP gateway (mcp.ts)

test("the hosted MCP gateway refuses review-project writes and forwards the rest", async () => {
  const upstream = "Bearer fixture-gateway-upstream";
  const editor = "https://fieldwork-annotations.example";
  const names = ["get_info", "list_projects", "list_items", "get_item", "list_labels", "list_changes", "export_project",
    "save_project", "save_label", "save_item", "save_annotation", "delete_annotation", "delete_item", "delete_label", "save_agent_review"];
  const catalogue = names.map((name) => ({ name, title: name, description: name, inputSchema: { type: "object" },
    annotations: { readOnlyHint: !name.match(/^(save|delete)_/) } }));
  const backend = backendFixture();
  const env = {
    FIELDWORK_EDITOR_ORIGIN: editor,
    ANNOTATION_BACKEND: { getByName: () => ({ fetch: async (request) => {
      assert.equal(request.headers.get("Authorization"), upstream);
      const url = new URL(request.url);
      if (url.pathname === "/mcp") {
        const body = await request.clone().json();
        if (body.method === "tools/list") return Response.json({ jsonrpc: "2.0", id: body.id, result: { tools: catalogue } });
        backend.requests.push({ path: "/mcp", text: JSON.stringify(body.params) });
        return Response.json({ jsonrpc: "2.0", id: body.id, result: { content: [{ type: "text", text: "{}" }], structuredContent: {}, isError: false } });
      }
      return (await backendFixture().send(url.pathname, await request.text())).clone();
    } }) },
  };
  const pending = [];
  const context = { waitUntil: (promise) => pending.push(promise) };
  const tool = async (name, args) => {
    const response = await handleFieldworkMcp(new Request("https://addr-crm.example/mcp", {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/call", params: { name, arguments: args } }),
    }), env, context, { upstreamAuthorization: upstream });
    await Promise.all(pending.splice(0));
    return (await response.json()).result;
  };
  for (const [name, args] of [
    ["save_annotation", { itemId: "r1008-1-shadow-002", annotation: { id: "a" }, expectedRevision: 2 }],
    ["save_item", { item: { id: "r1008-x", PROJECTID: "Mooroolbark-Review" }, expectedRevision: 0 }],
    ["delete_label", { id: "mooroolbark-review-proposed", expectedRevision: 1 }],
    ["delete_item", { id: "r1008-1-shadow-002", expectedRevision: 2 }],
  ]) {
    const result = await tool(name, args);
    assert.equal(result.isError, true, name);
    assert.match(result.content[0].text, new RegExp(REVIEW_REFUSAL), name);
  }
  assert.deepEqual(backend.requests, []);
  assert.equal((await tool("get_item", { id: "r1008-1-shadow-002" })).isError, false);
  assert.equal((await tool("save_annotation", { itemId: "addr-scene-x", annotation: { id: "a" }, expectedRevision: 2 })).isError, false);
  assert.deepEqual(backend.requests.map((r) => JSON.parse(r.text).name), ["get_item", "save_annotation"]);
});
