import assert from "node:assert/strict";
import { readFile, writeFile, mkdir, rm } from "node:fs/promises";
import { createHash } from "node:crypto";

const base = process.env.APP_URL || "http://localhost:3008";
const id = `qa_address_resolution_${Date.now()}`;
const directory = `data/scenes/${id}`;
const sourcePath = "data/scenes/mooroolbark-50011498-17-118446-80409/latest.json";
const original = await readFile(sourcePath);
const model = JSON.parse(original);
model.id = id;
const roof = model.objects.find(o => o.id === "cc22cc433bafe31a");
assert(roof);
delete roof.address_id;
model.objects = [roof];
model.groups = [];
model.owned_object_ids = [roof.id];
await mkdir(directory, { recursive: true });
const fixture = JSON.stringify(model);
const key = createHash("sha256").update(JSON.stringify([id, roof.id])).digest("hex");
const decision = `data/address-evidence/decisions/${key}`;
const url = `${base}/api/scenes/address-resolution?scene=${id}&object=${roof.id}`;
const get = async () => { const r = await fetch(url); assert.equal(r.status, 200, await r.clone().text()); return r.json(); };
const post = (body, headers = {}) => fetch(`${base}/api/scenes/address-resolution`, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: JSON.stringify(body) });
try {
  await writeFile(`${directory}/latest.json`, fixture);
  const initial = await get();
  assert.equal(initial.resolved.full_address, "118 HAYRICK LANE MOOROOLBARK 3138");
  const candidate = initial.evidence.candidates[0];
  const payload = { scene_id: id, object_id: roof.id, binding: initial.binding, revision: 0, action: "confirm", scope: "site",
    address_keys: [`${candidate.property_pfi}/${candidate.address_pfi}`], source_kind: "local_knowledge", note: "Synthetic QA decision on a disposable scene only." };
  assert.equal((await post(payload, { Origin: "https://untrusted.example" })).status, 403);
  for (const change of [{ address_keys: ["unknown/unknown"] }, { source_kind: "model_guess" }, { note: "" }, { scope: "ownership" }]) {
    assert.equal((await post({ ...payload, ...change })).status, 400);
  }
  assert.equal((await post({ ...payload, binding: "changed" })).status, 409);
  const saved = await post(payload); assert.equal(saved.status, 200, await saved.clone().text());
  const reloaded = await get();
  assert.equal(reloaded.resolved.status, "reviewed");
  assert.equal(reloaded.history_count, 1);
  assert.equal((await post(payload)).status, 409);
  const addressResponse = await fetch(`${base}/api/scenes/addresses?id=${id}`);
  assert.equal(addressResponse.status, 200);
  assert.equal((await addressResponse.json()).property_addresses[roof.id].status, "reviewed");
  const changed = structuredClone(model);
  changed.objects.find(o => o.id === roof.id).ring[0][0] += 1;
  await writeFile(`${directory}/latest.json`, JSON.stringify(changed));
  const stale = await get();
  assert.equal(stale.decision_stale, true);
  assert.notEqual(stale.resolved.status, "reviewed");
  await writeFile(`${directory}/latest.json`, fixture);
  assert.equal((await post({ ...payload, revision: 1, action: "defer", note: "Need a new title plan before confirming this fixture." })).status, 200);
  assert.equal((await get()).resolved.decision_state, "deferred");
  assert.equal((await post({ ...payload, revision: 2, action: "clear", note: "Clear the disposable test decision; keep its history." })).status, 200);
  const cleared = await get();
  assert.equal(cleared.history_count, 3);
  assert.equal(cleared.decision, null);
  assert.equal(await readFile(`${directory}/latest.json`, "utf8"), fixture);
  assert.deepEqual(await readFile(sourcePath), original);
  console.log("Address resolution API: authorization, candidate validation, concurrency, persistence, stale geometry, defer/clear history and immutable models passed.");
} finally {
  await rm(directory, { recursive: true, force: true });
  await rm(`${decision}.json`, { force: true });
  await rm(`${decision}.lock`, { force: true });
}
