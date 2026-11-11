import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";

const bundle = await build({
  entryPoints: [fileURLToPath(new URL("./mcp.ts", import.meta.url))],
  bundle: true, write: false, format: "esm", platform: "browser", target: "es2022",
});
const { handleFieldworkMcp, CRM_WORKSPACE_URI } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString("base64")}`);
const upstreamToken = "Bearer fixture-workspace-credential-never-returned";
const editorOrigin = "https://fieldwork.example";
const companionOrigin = "https://addr-crm.example";
const readNames = ["get_info", "list_projects", "list_items", "get_item", "list_labels", "list_changes", "export_project"];
const writeNames = ["save_project", "save_label", "save_item", "save_annotation", "delete_annotation", "delete_item", "delete_label", "save_agent_review"];
const objectSchema = (properties, required = []) => ({ type: "object", properties, required, additionalProperties: false });
const catalogue = [...readNames, ...writeNames].map((name) => ({
  name, title: name.replaceAll("_", " "), description: `Canonical ${name} fixture`,
  inputSchema: objectSchema({}),
  annotations: { readOnlyHint: readNames.includes(name), destructiveHint: !readNames.includes(name), idempotentHint: readNames.includes(name), openWorldHint: false },
}));
const annotationSchema = objectSchema({
  id: { type: "string" }, labelId: { type: "string" },
  geometry: objectSchema({ type: { type: "string", enum: ["Polygon"] }, coordinates: { type: "array" } }, ["type", "coordinates"]),
  notes: { type: "string" }, properties: { type: "object" },
}, ["geometry"]);
catalogue.find((entry) => entry.name === "save_annotation").inputSchema = objectSchema({
  itemId: { type: "string" }, annotation: annotationSchema,
  expectedRevision: { type: "integer", minimum: 1, maximum: Number.MAX_SAFE_INTEGER - 1 },
}, ["itemId", "annotation", "expectedRevision"]);
catalogue.find((entry) => entry.name === "get_item").inputSchema = objectSchema({ id: { type: "string" } }, ["id"]);
catalogue.find((entry) => entry.name === "save_agent_review").inputSchema = objectSchema({
  itemId: { type: "string" }, annotationId: { type: "string" }, expectedRevision: { type: "integer", minimum: 1 },
  reviewer: { type: "string" }, decision: { type: "string", enum: ["approved", "rejected", "needs_adjustment", "uncertain"] }, notes: { type: "string" },
}, ["itemId", "expectedRevision", "reviewer", "decision", "notes"]);
catalogue.find((entry) => entry.name === "list_items").inputSchema = objectSchema({
  projectId: { type: "string" }, query: { type: "string" }, limit: { type: "integer", default: 20, maximum: 500 },
}, ["projectId"]);
const projects = [{ id: "p1", name: "Mooroolbark", revision: 3 }];
const item = {
  id: "i1", projectId: "p1", title: "19 Cambridge Road", revision: 7,
  source: { url: "/api/v1/assets/immutable", imageSha256: "frozen" },
  metadata: { provenance: { source: "SAM3", human_reviewed: false } },
  annotations: [{ id: "a1", geometry: { type: "Polygon", coordinates: [[[0, 0], [1, 0], [1, 1], [0, 0]]] }, properties: { addr: { objectId: "o1", overrides: { label: "Fabric shade sail" } } } }],
};
const success = (value) => ({ content: [{ type: "text", text: JSON.stringify(value) }], structuredContent: value, isError: false });
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+afo4AAAAASUVORK5CYII=", "base64");
const pngDigest = createHash("sha256").update(png).digest("hex");
const imageItem = {
  ...item, adapter: "addr-scene",
  source: { image: { sha256: pngDigest, bytes: png.length, contentType: "image/png", url: "https://hostile.example/credential-trap", path: "untrusted" }, imageSha256: pngDigest, width: 1, height: 1 },
};

function fixture(options = {}) {
  const calls = [];
  const pending = [];
  const context = { waitUntil: (promise) => pending.push(promise) };
  const env = {
    FIELDWORK_EDITOR_ORIGIN: editorOrigin,
    ANNOTATION_BACKEND: { getByName: (name) => {
      assert.equal(name, "fieldwork");
      return { fetch: async (request) => {
        const url = new URL(request.url);
        assert.equal(url.origin, editorOrigin);
        assert.equal(request.headers.get("Authorization"), upstreamToken);
        assert.equal(request.headers.get("Accept"), "application/json, text/event-stream");
        if (request.headers.has("Origin")) assert.equal(request.headers.get("Origin"), editorOrigin);
        if (url.pathname.startsWith("/api/v1/assets/")) {
          assert.equal(url.pathname, `/api/v1/assets/${pngDigest}`);
          assert.equal(request.method, "GET");
          assert.equal(request.redirect, "manual");
          calls.push({ path: url.pathname });
          if (options.assetRedirect) return new Response(null, { status: options.assetRedirect, headers: { Location: "https://hostile.example/credential-trap" } });
          return new Response(options.assetBody ?? png, { headers: {
            "Content-Type": options.assetMime ?? "image/png", "X-Content-SHA256": options.assetDigest ?? pngDigest,
            ...options.assetHeaders,
          } });
        }
        const body = await request.json();
        calls.push({ path: url.pathname, body });
        if (options.discoveryStatus && body.method === "tools/list") {
          return Response.json({ code: "unauthenticated", message: "a valid annotation bearer token is required" }, { status: options.discoveryStatus });
        }
        if (url.pathname === "/mcp") {
          if (body.method === "tools/list") return Response.json({ jsonrpc: "2.0", id: body.id, result: { tools: catalogue } });
          assert.equal(body.method, "tools/call");
          const result = options.callResult ?? success({ item: { ...item, revision: 8 } });
          return Response.json({ jsonrpc: "2.0", id: body.id, result });
        }
        const method = url.pathname.replace("/api/v1/rpc/", "");
        if (options.rpcStatus?.[method]) return Response.json({ message: "fixture refusal" }, { status: options.rpcStatus[method] });
        if (options.legacy && ["GetProject", "SearchItems"].includes(method)) return Response.json({ code: "not_found" }, { status: 404 });
        if (method === "ListProjects") return Response.json({ projects, nextCursor: "" });
        if (method === "GetProject") return Response.json({ project: projects[0] });
        if (method === "GetItem") return Response.json({ item: options.item ?? item, labels: [{ id: "l1", name: "Building" }], labelsNextCursor: "" });
        if (["SearchItems", "ListItems"].includes(method)) return Response.json({ items: [item], nextCursor: "" });
        assert.fail(`unexpected backend RPC ${method}`);
      } };
    } },
  };
  const call = async (method, params = {}, overrides = {}) => {
    const request = new Request(`${companionOrigin}/mcp`, {
      method: "POST", headers: {
        "Content-Type": "application/json", Accept: "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25", Origin: companionOrigin,
      }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    const response = await handleFieldworkMcp(request, env, context, {
      upstreamAuthorization: upstreamToken, ...overrides,
    });
    const payload = await response.json();
    await Promise.all(pending.splice(0));
    assert.ok(!JSON.stringify(payload).includes(upstreamToken));
    return { response, payload };
  };
  return { call, calls };
}

test("SDK initialize and exact backend tool discovery add real OpenAI mentions and App entrypoints", async () => {
  const { call, calls } = fixture();
  const initialized = await call("initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "fixture-client", version: "1" } });
  assert.equal(initialized.response.status, 200);
  assert.equal(initialized.payload.result.serverInfo.name, "addr-crm");
  assert.equal(initialized.response.headers.get("Mcp-Session-Id"), null);
  assert.equal(initialized.response.headers.get("Cache-Control"), "no-store");
  const discovered = await call("tools/list");
  const tools = discovered.payload.result.tools;
  assert.equal(tools.length, 18);
  for (const original of catalogue) assert.deepEqual(tools.find((entry) => entry.name === original.name), original);
  const mentions = tools.find((entry) => entry.name === "search_mentions");
  assert.deepEqual(mentions._meta, { "openai/extensions": { "mentions/search": {} }, ui: { visibility: ["app"] } });
  assert.deepEqual(mentions.inputSchema.required, ["query"]);
  assert.ok(mentions.outputSchema.properties.items);
  const app = tools.find((entry) => entry.name === "open_crm_workspace");
  assert.equal(app._meta.ui.resourceUri, CRM_WORKSPACE_URI);
  assert.deepEqual(app._meta["openai/ui"].entrypoints, [{ type: "global" }, { type: "thread" }]);
  assert.equal(calls.filter((entry) => entry.body.method === "tools/list").length, 2);
});

test("write forwarding keeps exact revision, geometry, custom provenance and canonical success result", async () => {
  const expected = success({ item: { ...item, revision: 8 } });
  const { call, calls } = fixture({ callResult: expected });
  const args = { itemId: item.id, expectedRevision: item.revision, annotation: item.annotations[0] };
  const saved = await call("tools/call", { name: "save_annotation", arguments: args });
  assert.deepEqual(saved.payload.result, expected);
  const forward = calls.find((entry) => entry.body.method === "tools/call");
  assert.deepEqual(forward.body.params, { name: "save_annotation", arguments: args });
});

test("separate agent reviews are forwarded for review projects without human field writes", async () => {
  const { call, calls } = fixture({ item: { ...item, projectId: "mooroolbark-review" } });
  const args = { itemId: item.id, annotationId: "a1", expectedRevision: 7, reviewer: "codex", decision: "uncertain", notes: "The eave is occluded." };
  const result = await call("tools/call", { name: "save_agent_review", arguments: args });
  assert.equal(result.payload.result.isError, false);
  assert.deepEqual(calls.find((entry) => entry.body.method === "tools/call").body.params, { name: "save_agent_review", arguments: args });
  const forged = await call("tools/call", { name: "save_agent_review", arguments: { ...args, humanApproval: true } });
  assert.equal(forged.payload.result.isError, true);
  assert.equal(calls.filter((entry) => entry.body.method === "tools/call").length, 1);
});

test("canonical revision conflicts remain isError results without retries or fallback writes", async () => {
  const conflict = { content: [{ type: "text", text: "aborted: current item revision is 8" }], isError: true };
  const { call, calls } = fixture({ callResult: conflict });
  const result = await call("tools/call", { name: "save_annotation", arguments: { itemId: "i1", expectedRevision: 7, annotation: item.annotations[0] } });
  assert.deepEqual(result.payload.result, conflict);
  assert.equal(calls.filter((entry) => entry.body.method === "tools/call").length, 1);
  // The only other request is the review-project guard's read of the item's project (mcp-guard.ts): no fallback writes.
  assert.deepEqual(calls.filter((entry) => entry.path !== "/mcp").map((entry) => [entry.path, entry.body]), [["/api/v1/rpc/GetItem", { id: "i1" }]]);
});

test("missing revision, unknown fields and invalid geometry fail before reaching a write", async () => {
  for (const args of [
    { itemId: "i1", annotation: item.annotations[0] },
    { itemId: "i1", expectedRevision: 0, annotation: item.annotations[0] },
    { itemId: "i1", expectedRevision: 7, annotation: item.annotations[0], source: { replace: true } },
    { itemId: "i1", expectedRevision: 7, annotation: { geometry: { type: "Point", coordinates: [0, 0] } } },
  ]) {
    const { call, calls } = fixture();
    const result = await call("tools/call", { name: "save_annotation", arguments: args });
    assert.equal(result.payload.result.isError, true);
    assert.equal(calls.filter((entry) => entry.body.method === "tools/call").length, 0);
  }
});

test("optional default limits are not inserted into forwarded arguments", async () => {
  const { call, calls } = fixture();
  await call("tools/call", { name: "list_items", arguments: { projectId: "p1" } });
  assert.deepEqual(calls.find((entry) => entry.body.method === "tools/call").body.params.arguments, { projectId: "p1" });
});

test("read-only OAuth grants hide write tools and reject attempted writes", async () => {
  const { call, calls } = fixture();
  const access = { scopes: ["fieldwork:read"] };
  const listed = await call("tools/list", {}, access);
  assert.equal(listed.payload.result.tools.length, 10);
  assert.ok(listed.payload.result.tools.every((entry) => !writeNames.includes(entry.name)));
  const refused = await call("tools/call", { name: "save_annotation", arguments: { itemId: "i1", expectedRevision: 7, annotation: item.annotations[0] } }, access);
  assert.equal(refused.payload.result.isError, true);
  assert.equal(calls.filter((entry) => entry.body.method === "tools/call").length, 0);
  const before = calls.length;
  const noRead = await call("tools/list", {}, { scopes: ["fieldwork:write"] });
  assert.equal(noRead.response.status, 403);
  assert.equal(calls.length, before);
});

test("backend authentication refusal gates initialize, tool discovery and embedded App resources", async () => {
  for (const [method, params] of [
    ["initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "fixture", version: "1" } }],
    ["tools/list", {}], ["resources/read", { uri: CRM_WORKSPACE_URI }],
  ]) {
    const { call } = fixture({ discoveryStatus: 401 });
    const result = await call(method, params);
    assert.equal(result.response.status, 401);
  }
});

test("mentions and resources return current hosted documents and authenticate every read", async () => {
  const { call, calls } = fixture();
  const result = await call("tools/call", { name: "search_mentions", arguments: { query: "Cambridge" } });
  assert.equal(result.payload.result.content.length, 0);
  assert.deepEqual(result.payload.result.structuredContent.items.map((entry) => entry.uri), ["fieldwork://project/p1", "fieldwork://item/i1"]);
  const read = await call("resources/read", { uri: "fieldwork://item/i1" });
  assert.deepEqual(JSON.parse(read.payload.result.contents[0].text).item, item);
  const project = await call("resources/read", { uri: "fieldwork://project/p1" });
  assert.deepEqual(JSON.parse(project.payload.result.contents[0].text).project, projects[0]);
  for (const uri of ["fieldwork://item/i1?token=secret", "fieldwork://item/%69%31", "fieldwork://item/i1/extra"]) {
    const before = calls.filter((entry) => entry.path.includes("/rpc/")).length;
    const invalid = await call("resources/read", { uri });
    assert.ok(invalid.payload.error);
    assert.equal(calls.filter((entry) => entry.path.includes("/rpc/")).length, before);
  }
});

test("legacy Go read fallbacks are bounded and run only for missing RPCs", async () => {
  const legacy = fixture({ legacy: true });
  const mentions = await legacy.call("tools/call", { name: "search_mentions", arguments: { query: "" } });
  assert.equal(mentions.payload.result.structuredContent.items.length, 2);
  const project = await legacy.call("resources/read", { uri: "fieldwork://project/p1" });
  assert.equal(JSON.parse(project.payload.result.contents[0].text).project.id, "p1");
  assert.ok(legacy.calls.some((entry) => entry.path === "/api/v1/rpc/ListItems"));
  const failed = fixture({ rpcStatus: { SearchItems: 400 } });
  const result = await failed.call("tools/call", { name: "search_mentions", arguments: { query: "" } });
  assert.equal(result.payload.result.isError, true);
  assert.ok(!failed.calls.some((entry) => entry.path === "/api/v1/rpc/ListItems"));
});

test("App launcher uses current hosted state and serves a self-contained token-free SDK App", async () => {
  const { call } = fixture();
  const opened = await call("tools/call", { name: "open_crm_workspace", arguments: { itemId: "i1" } });
  const content = opened.payload.result.structuredContent;
  assert.equal(content.title, "Addr CRM");
  assert.deepEqual(content.item, item);
  assert.equal(content.selectedProjectId, "p1");
  assert.equal(content.editorUrl, `${editorOrigin}/`);
  const loaded = await call("resources/read", { uri: CRM_WORKSPACE_URI });
  const app = loaded.payload.result.contents[0];
  assert.equal(app.mimeType, "text/html;profile=mcp-app");
  assert.ok(app.text.includes("<script>"));
  assert.ok(!app.text.includes(upstreamToken));
  assert.deepEqual(app._meta.ui.csp, { connectDomains: [], resourceDomains: [] });
  assert.equal(app._meta["openai/ui"].preferredDisplayMode, "fullscreen");
});

test("linked item image returns exact native bytes with a current revision and verified digest", async () => {
  const { call, calls } = fixture({ item: imageItem });
  const result = await call("tools/call", { name: "get_item_image", arguments: { id: "i1" } });
  assert.equal(result.payload.result.isError, false);
  assert.deepEqual(Buffer.from(result.payload.result.content[1].data, "base64"), png);
  assert.equal(result.payload.result.content[1].mimeType, "image/png");
  assert.equal(result.payload.result.structuredContent.itemRevision, 7);
  assert.equal(result.payload.result.structuredContent.imageSha256, pngDigest);
  assert.equal(calls.filter((entry) => entry.path.includes("/assets/")).length, 1);
  assert.ok(!JSON.stringify(result.payload.result).includes("hostile.example"));
});

test("image role, source binding, MIME and actual hash checks refuse unrelated or corrupted assets", async () => {
  for (const options of [
    { item },
    { item: { ...imageItem, adapter: "map" } },
    { item: { ...imageItem, source: { ...imageItem.source, imageSha256: "f".repeat(64) } } },
    { item: { ...imageItem, source: { ...imageItem.source, image: { ...imageItem.source.image, contentType: "application/json" } } } },
    { item: imageItem, assetBody: Buffer.from("changed") },
    { item: imageItem, assetMime: "application/json" },
    { item: imageItem, assetDigest: "f".repeat(64) },
  ]) {
    const { call } = fixture(options);
    const result = await call("tools/call", { name: "get_item_image", arguments: { id: "i1" } });
    assert.equal(result.payload.result.isError, true);
    assert.ok(result.payload.result.content.every((entry) => entry.type === "text"));
  }
  const { call, calls } = fixture({ item: imageItem });
  const invalid = await call("tools/call", { name: "get_item_image", arguments: { id: "i1", variant: "model" } });
  assert.equal(invalid.payload.result.isError, true);
  const missing = await call("tools/call", { name: "get_item_image", arguments: { id: "i1", variant: "review" } });
  assert.equal(missing.payload.result.isError, true);
  assert.equal(calls.filter((entry) => entry.path.includes("/assets/")).length, 0);
});

test("review images do not inherit source dimensions or original-pixel geometry coordinates", async () => {
  const reviewedItem = {
    ...imageItem, source: {
      ...imageItem.source, width: 1024, height: 1024,
      reviewImage: { sha256: pngDigest, bytes: png.length, contentType: "image/png" },
    },
  };
  const { call } = fixture({ item: reviewedItem });
  const result = await call("tools/call", { name: "get_item_image", arguments: { id: "i1", variant: "review" } });
  assert.equal(result.payload.result.isError, false);
  const binding = result.payload.result.structuredContent;
  assert.equal(binding.variant, "review");
  assert.equal(binding.coordinateSpace, "review image; use source image for geometry coordinates");
  assert.ok(!Object.hasOwn(binding, "width"));
  assert.ok(!Object.hasOwn(binding, "height"));
  assert.deepEqual(Buffer.from(result.payload.result.content[1].data, "base64"), png);
});

test("image reads enforce declared and streamed size bounds and OAuth read authorization", async () => {
  for (const options of [
    { item: { ...imageItem, source: { ...imageItem.source, image: { ...imageItem.source.image, bytes: 10 * 1024 * 1024 + 1 } } } },
    { item: imageItem, assetHeaders: { "Content-Length": String(10 * 1024 * 1024 + 1) } },
    { item: imageItem, assetBody: Buffer.alloc(10 * 1024 * 1024 + 1) },
  ]) {
    const { call } = fixture(options);
    const result = await call("tools/call", { name: "get_item_image", arguments: { id: "i1" } });
    assert.equal(result.payload.result.isError, true);
    assert.match(result.payload.result.content[0].text, /10 MiB/);
  }
  const { call, calls } = fixture({ item: imageItem, discoveryStatus: 401 });
  const denied = await call("tools/call", { name: "get_item_image", arguments: { id: "i1" } });
  assert.equal(denied.response.status, 401);
  assert.equal(calls.filter((entry) => entry.path.includes("/assets/")).length, 0);
});

test("protected image redirects are refused without following a credentialed URL", async () => {
  for (const assetRedirect of [301, 302, 307, 308]) {
    const { call, calls } = fixture({ item: imageItem, assetRedirect });
    const result = await call("tools/call", { name: "get_item_image", arguments: { id: "i1" } });
    assert.equal(result.payload.result.isError, true);
    assert.match(result.payload.result.content[0].text, /returned a redirect/);
    assert.equal(calls.filter((entry) => entry.path.includes("/assets/")).length, 1);
    assert.ok(result.payload.result.content.every((entry) => entry.type === "text"));
    assert.ok(!JSON.stringify(result.payload.result).includes("hostile.example"));
  }
});

test("SDK transport and extensions execute in the Workers runtime without eval or Node shims", async (t) => {
  const runtimeBundle = await build({
    stdin: {
      contents: `import {handleFieldworkMcp} from './mcp';
        export default {fetch(request, env, context) {return handleFieldworkMcp(request, {
          ANNOTATION_BACKEND: {getByName() {return env.FIXTURE_BACKEND;}},
          FIELDWORK_EDITOR_ORIGIN: ${JSON.stringify(editorOrigin)}
        }, context, {upstreamAuthorization: ${JSON.stringify(upstreamToken)}});}};`,
      resolveDir: fileURLToPath(new URL(".", import.meta.url)),
    }, bundle: true, write: false, format: "esm", platform: "browser", target: "es2022",
  });
  const mf = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    script: runtimeBundle.outputFiles[0].text,
    compatibilityDate: "2026-10-02",
    compatibilityFlags: ["global_fetch_strictly_public"],
    serviceBindings: { FIXTURE_BACKEND: async (request) => {
      assert.equal(request.headers.get("Authorization"), upstreamToken);
      const path = new URL(request.url).pathname;
      if (path === `/api/v1/assets/${pngDigest}`) {
        return new Response(png, { headers: { "Content-Type": "image/png", "X-Content-SHA256": pngDigest } });
      }
      const body = await request.json();
      if (path === "/api/v1/rpc/GetItem") {
        return Response.json({ item: imageItem, labels: [], labelsNextCursor: "" });
      }
      assert.equal(body.method, "tools/list");
      return Response.json({ jsonrpc: "2.0", id: body.id, result: { tools: catalogue } });
    } },
  }));
  t.after(() => mf.dispose());
  for (const [method, params] of [
    ["initialize", { protocolVersion: "2025-11-25", capabilities: {}, clientInfo: { name: "workerd-fixture", version: "1" } }],
    ["tools/list", {}], ["resources/read", { uri: CRM_WORKSPACE_URI }],
    ["tools/call", { name: "get_item_image", arguments: { id: "i1" } }],
  ]) {
    const response = await mf.dispatchFetch(`${companionOrigin}/mcp`, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
    });
    assert.equal(response.status, 200, await response.clone().text());
    const payload = await response.json();
    assert.ok(payload.result);
    assert.ok(!JSON.stringify(payload).includes(upstreamToken));
    if (method === "tools/call") {
      assert.equal(payload.result.isError, false);
      assert.deepEqual(Buffer.from(payload.result.content[1].data, "base64"), png);
    }
  }
});
