/** Read-only production smoke test; never saves records, assets or OAuth grants. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { Client } from "../deploy/annotation/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js";
import { StreamableHTTPClientTransport } from "../deploy/annotation/node_modules/@modelcontextprotocol/sdk/dist/esm/client/streamableHttp.js";

const origin = "https://addr-crm-mcp.ben-ebsworth.workers.dev";
const backend = "https://fieldwork-annotations.ben-ebsworth.workers.dev";
const itemId = "addr-scene-house-29785-me373cb0a9d78-r0";
const credential = (await readFile(new URL("../deploy/annotation/admin-token.txt", import.meta.url), "utf8")).trim();
const headers = { Authorization: `Bearer ${credential}`, "User-Agent": "Mozilla/5.0" };
const parse = (result) => {
  assert.ok(!result.isError, "The CRM tool refused the read");
  return result.structuredContent ?? JSON.parse(result.content.find((block) => block.type === "text").text);
};
const rpc = async (method, params) => {
  const response = await fetch(`${backend}/mcp`, {
    method: "POST", headers: { ...headers, "Content-Type": "application/json", Accept: "application/json, text/event-stream" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.ok(!result.error);
  return result.result;
};

const metadataResponse = await fetch(`${origin}/.well-known/oauth-protected-resource/mcp`);
assert.equal(metadataResponse.status, 200);
const metadata = await metadataResponse.json();
assert.equal(metadata.resource, `${origin}/mcp`);
const oauthResponse = await fetch(`${origin}/.well-known/oauth-authorization-server`);
assert.equal(oauthResponse.status, 200);
const oauth = await oauthResponse.json();
assert.equal(oauth.issuer, origin);
assert.ok(oauth.code_challenge_methods_supported.includes("S256"));
const challenge = await fetch(`${origin}/mcp`, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream" }, body: "{}" });
assert.equal(challenge.status, 401);
assert.ok(challenge.headers.get("WWW-Authenticate").includes("oauth-protected-resource/mcp"));

const before = parse(await rpc("tools/call", { name: "get_item", arguments: { id: itemId } })).item;
const legacyTools = (await rpc("tools/list", {})).tools;
const client = new Client({ name: "addr-hosted-read-only-smoke", version: "1" });
const transport = new StreamableHTTPClientTransport(new URL(`${origin}/mcp`), { requestInit: { headers } });
let report;
try {
  await client.connect(transport);
  const tools = (await client.listTools()).tools;
  assert.equal(tools.length, 17);
  for (const legacy of legacyTools) {
    const current = tools.find((tool) => tool.name === legacy.name);
    if (legacy.name === "search_mentions") continue;
    assert.deepEqual(current.inputSchema, legacy.inputSchema);
    assert.deepEqual(current.annotations, legacy.annotations);
  }
  const info = parse(await client.callTool({ name: "get_info", arguments: {} }));
  const item = parse(await client.callTool({ name: "get_item", arguments: { id: itemId } })).item;
  assert.deepEqual(item, before);
  const mentions = parse(await client.callTool({ name: "search_mentions", arguments: { query: "mooroolbark" } }));
  assert.ok(mentions.items.length > 0);
  const project = mentions.items.find((link) => link.uri.startsWith("fieldwork://project/"));
  assert.ok(project);
  await client.readResource({ uri: project.uri });
  const resource = await client.readResource({ uri: `fieldwork://item/${itemId}` });
  assert.equal(JSON.parse(resource.contents[0].text).item.revision, before.revision);
  const workspace = parse(await client.callTool({ name: "open_crm_workspace", arguments: { itemId } }));
  assert.equal(workspace.item.id, itemId);
  assert.equal(workspace.editorUrl, `${backend}/`);
  const ui = await client.readResource({ uri: "ui://addr/crm-workspace" });
  assert.equal(ui.contents[0].mimeType, "text/html;profile=mcp-app");
  assert.ok(ui.contents[0].text.includes("Addr CRM"));
  assert.ok(!ui.contents[0].text.includes(credential));
  const imageResult = await client.callTool({ name: "get_item_image", arguments: { id: itemId, variant: "source" } });
  assert.ok(!imageResult.isError);
  const image = imageResult.content.find((block) => block.type === "image");
  assert.ok(image);
  const bytes = Buffer.from(image.data, "base64");
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  assert.equal(sha256, before.source.imageSha256);
  const after = parse(await rpc("tools/call", { name: "get_item", arguments: { id: itemId } })).item;
  assert.deepEqual(after, before);
  report = { checkedAt: new Date().toISOString(), endpoint: `${origin}/mcp`, server: client.getServerVersion(),
    tools: tools.map((tool) => tool.name), legacySchemasPreserved: true,
    oauthDiscovery: true, unauthenticatedChallenge: true, pkce: true,
    mentions: mentions.items.length, projectResource: true, itemResource: true,
    sdkAppBytes: Buffer.byteLength(ui.contents[0].text),
    sourceImage: { mimeType: image.mimeType, bytes: bytes.length, sha256 },
    item: { id: itemId, revisionBefore: before.revision, revisionAfter: after.revision },
    domainMutations: 0, workspaceInfoRead: !!info };
} finally {
  await client.close();
}
assert.ok(!JSON.stringify(report).includes(credential));
if (process.argv[2]) await writeFile(process.argv[2], `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify(report, null, 2));
