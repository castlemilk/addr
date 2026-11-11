import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { mkdtemp, mkdir, readFile, writeFile, rm, readdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import { after, test } from "node:test";
import ts from "typescript";

const repository = process.cwd(), require = createRequire(import.meta.url), modules = new Map();
const directory = await mkdtemp(path.join(tmpdir(), "addr-feedback-test-"));
process.chdir(directory);
after(async () => { process.chdir(repository); await rm(directory, { recursive: true, force: true }); });
function load(file) {
  const resolved = path.resolve(repository, file);
  if (modules.has(resolved)) return modules.get(resolved);
  const exports = {}; modules.set(resolved, exports);
  const code = ts.transpileModule(readFileSync(resolved, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
  new Function("exports", "require", code)(exports, id => id.startsWith(".") ? load(path.resolve(path.dirname(resolved), `${id}.ts`)) : id.startsWith("@/") ? load(`src/${id.slice(2)}.ts`) : require(id));
  return exports;
}
const core = load("src/lib/annotations/feedback.ts");
const store = load("src/lib/annotations/feedback-store.ts");
const scenes = load("src/lib/scene-store.ts");
const { objectGeometry, updateSceneObject, sceneObjects } = load("src/lib/annotations/addr-scene.ts");
const principal = { principal: "local-test-reviewer", scopes: ["crm.read", "crm.roof.write", "crm.tree.write", "crm.object.write"] };
const hash = value => createHash("sha256").update(value).digest("hex");
const wrap = value => { const json = JSON.stringify(value); return { json, sha256: hash(json) }; };
const ref = document => ({ url: `/api/v1/assets/${document.sha256}`, sha256: document.sha256, contentType: "application/json" });
const ring = [[10, 10], [90, 10], [90, 90], [10, 90]];
const holes = [[[30, 30], [40, 30], [40, 40], [30, 40]]];
async function fixture(id, objectOverrides = {}) {
  const image = Buffer.from(`Synthetic image bytes for ${id}`), imageSha256 = hash(image);
  const object = { id: "roof", kind: "building", label: "Roof", ring, holes, status: "confirmed", geometry_reviewed: true, visibility: "full", source: "sam3-text-onnx", notes: "Source note", provenance: { source: "machine:preserved-test" },
    model: { height_m: 4, base_m: 0, height_source: "estimate", height_verified: true, width_m: 80, length_m: 80, area_m2: 6300, rotation_deg: 0, color: "#445566" },
    roof_parts: [{ ring_meters: ring, ring_z: [4, 4, 4, 4] }], roof_lines: [{ line_meters: [[10, 50], [90, 50]], kind: "ridge" }],
    roof_feedback: { outline: { ring, holes }, family: "gabled" }, ...objectOverrides };
  const model = { schema_version: 1, id, title: id, address_id: null, cohort: null, centroid: { lon: 145.3, lat: -37.8 }, image_sha256: imageSha256, image_width: 100, image_height: 100,
    pixel_to_meters: [[1, 0], [0, 1], [0, 0]], meters_to_pixel: [[1, 0], [0, 1], [0, 0]], model_signature: hash(`model:${id}`),
    objects: [object], groups: [], scans: {}, height_note: "Estimates" };
  const review = { revision: 7, model_signature: model.model_signature, objects: structuredClone(model.objects), groups: [], reviewed_classes: ["building"], basemap_mode: "raw", updated_by: "original-actor", updated_at: "2026-09-27T00:00:00Z", history: [] };
  const modelDoc = wrap(model), reviewDoc = wrap(review);
  const base = { id: `addr-scene-${id}`, projectId: "test-project", title: id, adapter: "addr-scene", revision: 0,
    source: { sceneId: id, model: ref(modelDoc), review: ref(reviewDoc), reviewModel: ref(modelDoc), image: { url: `/api/v1/assets/${imageSha256}`, sha256: imageSha256 }, modelSignature: model.model_signature, imageSha256, width: 100, height: 100 },
    metadata: { addr: { sourceReviewRevision: 7, snapshotId: "snapshot-test" } }, status: "in_review", tags: [], notes: "",
    annotations: [{ id: "roof", labelId: "test-project-kind-building", geometry: objectGeometry(object, model), properties: { addr: { objectId: "roof", layer: "review", source: object.source, provenance: object.provenance } } }] };
  const folder = path.join(directory, "data/scenes", id); await mkdir(folder, { recursive: true });
  await writeFile(path.join(folder, "latest.json"), modelDoc.json); await writeFile(path.join(folder, "review.json"), reviewDoc.json);
  await writeFile(path.join(folder, `${imageSha256}.png`), image);
  return { id, folder, model, review, base, object, modelDoc, reviewDoc };
}
function packet(fixtures, remoteItems, cursor = "30") {
  const bundle = { schemaVersion: 1, origin: "https://fieldwork.example", projectId: "test-project", snapshotId: "snapshot-test", manifestSha256: hash("manifest"), cursor,
    entries: fixtures.map((f, i) => ({ itemId: f.base.id, sceneId: f.id, baseItem: wrap(f.base), item: wrap(remoteItems[i]), model: f.modelDoc, review: f.reviewDoc, reviewModel: f.modelDoc, actor: "workspace-token", timestamp: "2026-09-27T01:00:00Z" })), quarantined: [] };
  return { ...bundle, digest: core.feedbackBundleDigest(bundle) };
}
function remote(f, change, revision = 2, prior = f.base) {
  const current = sceneObjects(prior, f.model, f.review)[0];
  return { ...updateSceneObject(prior, f.object, change(structuredClone(current)), f.model), revision };
}
const object = review => review.objects.find(o => o.id === "roof");
async function saved(f) { return (await scenes.loadScene(f.id)).review; }
const apply = (bundle, plan, options) => store.applyFeedback(bundle, plan.planId, principal, options);

test("preview is read-only; notes merge preserves unrelated local edits, roofs and approvals; repeated apply and remote revert", async () => {
  const f = await fixture("notes");
  const local = structuredClone(f.review); object(local).label = "Local label";
  await scenes.saveScene(f.id, local, principal);
  const initialBytes = await readFile(path.join(f.folder, "review.json"));
  const incoming = remote(f, o => ({ ...o, notes: "Cloud note" }));
  const bundle = packet([f], [incoming]);
  const preview = await store.previewFeedback(bundle, principal);
  assert.equal(preview.canApply, true); assert.equal(preview.counts.notesOnly, 1); assert.equal(preview.counts.drafts, 0);
  assert.deepEqual(await readFile(path.join(f.folder, "review.json")), initialBytes);
  await assert.rejects(readdir(path.join(directory, "data/reviews/fieldwork-feedback")), /ENOENT/);
  const result = await apply(bundle, preview); assert.equal(result.complete, true);
  const first = await saved(f); assert.equal(first.revision, 9);
  assert.equal(first.basemap_mode, "raw", "reconciliation preserves which imagery was used for local feedback");
  assert.equal(object(first).label, "Local label"); assert.equal(object(first).notes, "Cloud note");
  assert.equal(object(first).geometry_reviewed, true); assert.equal(object(first).model.height_verified, true);
  assert.deepEqual(object(first).roof_parts, f.object.roof_parts); assert.deepEqual(object(first).holes, holes);
  assert.deepEqual(object(first).provenance, f.object.provenance); assert.deepEqual(object(first).roof_feedback, f.object.roof_feedback);
  assert.equal((await apply(bundle, preview)).applied[0].alreadyApplied, true); assert.equal((await saved(f)).revision, 9);
  const revert = packet([f], [{ ...structuredClone(f.base), revision: 3 }], "31");
  const next = await store.previewFeedback(revert, principal); assert.equal(next.canApply, true);
  await apply(revert, next); assert.equal(object(await saved(f)).notes, "Source note"); assert.equal(object(await saved(f)).label, "Local label");
  const oldRetry = await apply(bundle, preview); assert.equal(oldRetry.complete, true); assert.equal((await saved(f)).revision, 10, "older retry cannot overwrite a newer receipt or bump revision");
});

test("substantive feedback and remote rejection become pending drafts; normal explicit local approval clears pending without restoring roof targets", async () => {
  const f = await fixture("draft");
  const cloud = remote(f, o => ({ ...o, ring: [[9.5, 10], ...o.ring.slice(1)], status: "rejected" }));
  const bundle = packet([f], [cloud]), plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true); assert.equal(plan.counts.drafts, 1);
  await apply(bundle, plan);
  const draft = await saved(f), mark = object(draft);
  assert.equal(mark.status, "needs_adjustment"); assert.equal(mark.geometry_reviewed, false); assert.equal(mark.model.height_verified, false);
  assert.equal(mark.fieldwork_import.pending, true); assert.deepEqual(mark.provenance, f.object.provenance);
  assert.equal(mark.roof_feedback, undefined); assert.equal(mark.roof_parts, undefined); assert.deepEqual(draft.reviewed_classes, []);
  delete mark.fieldwork_import; mark.notes = "Local note still pending";
  const notesOnly = await scenes.saveScene(f.id, draft, principal); assert.equal(object(notesOnly).fieldwork_import.pending, true, "omitted marker cannot bypass pending state");
  object(notesOnly).status = "confirmed"; object(notesOnly).geometry_reviewed = true; delete object(notesOnly).fieldwork_import;
  const approved = await scenes.saveScene(f.id, notesOnly, principal);
  assert.equal(object(approved).fieldwork_import.pending, false); assert.equal(object(approved).fieldwork_import.accepted_by, principal.principal);
  assert.equal(object(approved).roof_feedback, undefined, "outline confirmation cannot approve an imported roof-family target");
  assert.deepEqual(object(approved).provenance, f.object.provenance, "local acceptance does not erase machine source provenance");
  const updated = remote(f, o => ({ ...o, notes: "Next cloud note" }), 3, cloud);
  const nextPacket = packet([f], [updated], "32"), next = await store.previewFeedback(nextPacket, principal);
  assert.equal(next.canApply, false, "overlapping local/remote notes conflict");
  assert(next.scenes[0].conflicts.some(message => message.includes("notes")));
});

test("new cloud objects import as drafts and support later receipt-based edits", async () => {
  const f = await fixture("new-object");
  const fresh = { ...structuredClone(f.object), id: "fieldwork-new", source: "fieldwork-user-feedback", label: "New roof", status: "confirmed", geometry_reviewed: true };
  delete fresh.provenance;
  let cloud = updateSceneObject(f.base, { id: fresh.id }, fresh, f.model); cloud.revision = 2;
  let bundle = packet([f], [cloud]), plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan)); await apply(bundle, plan);
  let review = await saved(f), added = review.objects.find(o => o.id === fresh.id);
  assert.equal(added.status, "needs_adjustment"); assert.equal(added.fieldwork_import.pending, true); assert.equal(added.roof_feedback, undefined);
  const effective = sceneObjects(cloud, f.model, f.review).find(o => o.id === fresh.id);
  cloud = { ...updateSceneObject(cloud, { id: fresh.id }, { ...effective, notes: "New-object cloud note" }, f.model), revision: 3 };
  bundle = packet([f], [cloud], "33"); plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan)); await apply(bundle, plan); review = await saved(f);
  added = review.objects.find(o => o.id === fresh.id); assert.equal(added.notes, "New-object cloud note"); assert.equal(added.fieldwork_import.pending, true);
  const collision = await fixture("collision"); const local = structuredClone(collision.review); local.objects.push(fresh); await scenes.saveScene(collision.id, local, principal);
  const collisionItem = { ...updateSceneObject(collision.base, { id: fresh.id }, fresh, collision.model), revision: 2 };
  assert.equal((await store.previewFeedback(packet([collision], [collisionItem]), principal)).canApply, false);
});

test("already matching local geometry with a new cloud note does not revoke local review", async () => {
  const f = await fixture("matching-geometry");
  const changed = { ...f.object, ring: [[9, 10], ...ring.slice(1)] };
  const local = structuredClone(f.review); local.objects[0] = { ...changed, roof_feedback: undefined }; await scenes.saveScene(f.id, local, principal);
  const cloud = { ...structuredClone(f.base), revision: 3 };
  cloud.annotations[0].geometry = objectGeometry(changed, f.model);
  cloud.annotations[0].properties.addr.overrides = { notes: "Only this note differs locally" };
  const bundle = packet([f], [cloud]), plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan)); assert.equal(plan.counts.drafts, 0);
  await apply(bundle, plan); const result = object(await saved(f));
  assert.equal(result.status, "confirmed"); assert.equal(result.geometry_reviewed, true); assert.equal(result.fieldwork_import, undefined);
});

test("class corrections clear incompatible shape profiles while preserving remote intent in the packet", async () => {
  const f = await fixture("class-profile");
  for (const target of [f.model.objects[0], f.review.objects[0]]) {
    target.kind = "tree"; target.model.tree = { crown_profile: "rounded", crown_base_ratio: 0.2 };
    delete target.roof_parts; delete target.roof_lines; delete target.roof_feedback;
  }
  f.object = f.review.objects[0]; f.review.reviewed_classes = ["tree"];
  f.modelDoc = wrap(f.model); f.reviewDoc = wrap(f.review);
  f.base.source.model = ref(f.modelDoc); f.base.source.reviewModel = ref(f.modelDoc); f.base.source.review = ref(f.reviewDoc);
  f.base.annotations[0].labelId = "test-project-kind-tree";
  await writeFile(path.join(f.folder, "latest.json"), f.modelDoc.json); await writeFile(path.join(f.folder, "review.json"), f.reviewDoc.json);
  const cloud = remote(f, o => ({ ...o, kind: "building" }));
  assert(sceneObjects(cloud, f.model, f.review)[0].model.tree, "fixture reproduces the cloud class selector retaining a tree profile");
  const bundle = packet([f], [cloud]), plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan)); await apply(bundle, plan);
  const result = object(await saved(f)); assert.equal(result.kind, "building"); assert.equal(result.model.tree, undefined); assert.equal(result.fieldwork_import.pending, true);
});

test("notes-only crash after canonical save is recovered from history, without a second revision", async () => {
  const f = await fixture("crash-notes"); const bundle = packet([f], [remote(f, o => ({ ...o, notes: "Crash-safe note" }))]);
  const plan = await store.previewFeedback(bundle, principal);
  await assert.rejects(apply(bundle, plan, { afterSceneSave: async () => { throw new Error("simulated process interruption"); } }), /simulated/);
  assert.equal((await saved(f)).revision, 8);
  const local = await saved(f); object(local).label = "Later local edit"; await scenes.saveScene(f.id, local, principal);
  const result = await apply(bundle, plan); assert.equal(result.complete, true); assert.equal(result.applied[0].alreadyApplied, true);
  assert.equal((await saved(f)).revision, 9); assert.equal(object(await saved(f)).label, "Later local edit");
});

test("multi-scene interruption resumes original plan; stale plans and changed source bytes fail closed", async () => {
  const a = await fixture("multi-a"), b = await fixture("multi-b");
  const bundle = packet([a, b], [remote(a, o => ({ ...o, notes: "A" })), remote(b, o => ({ ...o, notes: "B" }))]);
  const plan = await store.previewFeedback(bundle, principal);
  await assert.rejects(apply(bundle, plan, { afterSceneSave: async id => { if (id === a.id) throw new Error("interrupted first scene"); } }), /interrupted/);
  const resumed = await apply(bundle, plan); assert.equal(resumed.complete, true); assert.equal((await saved(a)).revision, 8); assert.equal((await saved(b)).revision, 8);
  const c = await fixture("stale-plan"); const packetC = packet([c], [remote(c, o => ({ ...o, notes: "C" }))]); const planC = await store.previewFeedback(packetC, principal);
  const local = await saved(c); object(local).notes = "Local newer note"; await scenes.saveScene(c.id, local, principal);
  await assert.rejects(apply(packetC, planC), /changed since preview/); assert.equal((await saved(c)).revision, 8);
  await writeFile(path.join(a.folder, `${a.model.image_sha256}.png`), "changed image");
  const retry = await apply(bundle, plan); assert.equal(retry.complete, false); assert.match(retry.error, /image bytes/);
  assert.equal(retry.canReplan, undefined, "source uncertainty must retain the original plan");
});

test("a durable prefix can replan when a later local edit precedes every remaining journal", async () => {
  const a = await fixture("replan-a"), b = await fixture("replan-b");
  const bundle = packet([a, b], [remote(a, o => ({ ...o, notes: "Cloud A" })), remote(b, o => ({ ...o, notes: "Cloud B" }))]);
  const plan = await store.previewFeedback(bundle, principal);
  const interrupted = await apply(bundle, plan, { afterSceneSave: async id => {
    if (id !== a.id) return;
    const local = await saved(b); object(local).label = "Unrelated local label";
    await scenes.saveScene(b.id, local, principal);
  } });
  assert.equal(interrupted.complete, false); assert.equal(interrupted.status, 409); assert.equal(interrupted.canReplan, true);
  assert.equal(interrupted.retryPlanId, plan.planId); assert.equal(interrupted.applied.length, 1);
  assert.equal(interrupted.applied[0].itemId, a.base.id);
  const receiptPath = path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), bundle.projectId, "receipts", `${hash(a.base.id)}.json`);
  const receipt = JSON.parse(await readFile(receiptPath, "utf8"));
  assert.equal(receipt.remote.sha256, bundle.entries[0].item.sha256, "completed prefix has durable evidence");
  const savedA = await readFile(path.join(a.folder, "review.json"));
  const next = await store.previewFeedback(bundle, principal);
  assert.notEqual(next.planId, plan.planId); assert.equal(next.canApply, true);
  const result = await apply(bundle, next); assert.equal(result.complete, true);
  assert.deepEqual(await readFile(path.join(a.folder, "review.json")), savedA, "replan does not save A twice");
  const finalB = await saved(b); assert.equal(finalB.revision, 9);
  assert.equal(object(finalB).notes, "Cloud B"); assert.equal(object(finalB).label, "Unrelated local label");
});

test("existing current or suffix journals prevent stale-plan replanning even when their JSON is null", async () => {
  for (const suffix of [false, true]) {
    const prefix = suffix ? "suffix-journal" : "current-journal";
    const a = await fixture(`${prefix}-a`), b = await fixture(`${prefix}-b`), c = await fixture(`${prefix}-c`);
    const bundle = packet([a, b, c], [a, b, c].map(f => remote(f, o => ({ ...o, notes: `Cloud ${f.id}` }))));
    const plan = await store.previewFeedback(bundle, principal);
    const result = await apply(bundle, plan, { afterSceneSave: async id => {
      if (id !== a.id) return;
      const local = await saved(b); object(local).label = "Concurrent label";
      await scenes.saveScene(b.id, local, principal);
      const itemId = suffix ? c.base.id : b.base.id;
      const journalPath = path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), bundle.projectId, "runs", plan.planId, `${hash(itemId)}.json`);
      await writeFile(journalPath, "null");
    } });
    assert.equal(result.complete, false); assert.equal(result.status, 409); assert.equal(result.applied.length, 1);
    assert.equal(result.canReplan, undefined, "existing evidence cannot be treated as an absent journal");
    assert.equal((await saved(a)).revision, 8); assert.equal((await saved(b)).revision, 8); assert.equal((await saved(c)).revision, 7);
  }
});

test("packet hashes, unmapped metadata, deleted objects and permissions reject; queue-only edits are explicit receipts", async () => {
  const f = await fixture("validation"); const cloud = remote(f, o => ({ ...o, notes: "Feedback" }));
  const bundle = packet([f], [cloud]), corrupt = structuredClone(bundle); corrupt.entries[0].item.json += " ";
  await assert.rejects(store.previewFeedback(corrupt, principal), /hash/);
  await assert.rejects(store.previewFeedback(bundle, { principal: "no-read", scopes: [] }), /reading access/);
  const plan = await store.previewFeedback(bundle, principal);
  await assert.rejects(store.applyFeedback(bundle, plan.planId, { principal: "reader", scopes: ["crm.read"] }), /crm.roof.write/);
  for (const change of [item => { item.metadata.changed = true; }, item => { item.annotations = []; }, item => { item.annotations[0].properties.addr.overrides.provenance = { source: "human" }; }]) {
    const value = structuredClone(cloud); change(value);
    assert.equal((await store.previewFeedback(packet([f], [value]), principal)).canApply, false);
  }
  const onlyQueue = { ...structuredClone(f.base), revision: 2, notes: "Scene-wide note", tags: ["follow-up"] };
  const queuePacket = packet([f], [onlyQueue]), queuePlan = await store.previewFeedback(queuePacket, principal);
  assert.equal(queuePlan.counts.receiptOnly, 1); assert.equal(queuePlan.scenes[0].changes[0].receiptOnly, true);
  await apply(queuePacket, queuePlan); assert.equal((await saved(f)).revision, 7);
  const { POST } = load("src/app/api/scenes/feedback/route.ts");
  const { NextRequest } = require("next/server");
  const originalFetch = globalThis.fetch;
  try {
    let calls = 0; globalThis.fetch = async () => { calls++; return new Response("{}", { status: 401 }); };
    for (const mode of ["preview", "apply"]) {
      const response = await POST(new NextRequest("http://localhost:3008/api/scenes/feedback", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bundle, mode, planId: plan.planId }) }));
      assert.equal(response.status, 401);
    }
    assert.equal(calls, 2, "both modes authenticate through CRM");
  } finally { globalThis.fetch = originalFetch; }
});

test("a Fieldwork decision-bar save imports as a pending draft; the incoming status survives only in the receipt", async () => {
  // The source object starts as an unreviewed candidate so the Fieldwork "Looks right to me" is a real status change.
  const f = await fixture("decision", { status: "candidate", geometry_reviewed: false });
  // Step 1 mirrors a vertex drag (AddrSceneEditor.updateGeometry); step 2 mirrors "Save outline · Looks right to me".
  const dragged = remote(f, o => ({ ...o, ring: [[9.5, 10], ...o.ring.slice(1)], status: "needs_adjustment", geometry_reviewed: false }));
  const cloud = remote(f, o => ({ ...o, status: "confirmed", geometry_reviewed: true }), 3, dragged);
  const overrides = cloud.annotations[0].properties.addr.overrides;
  assert.deepEqual(overrides.ring, [[9.5, 10], ...ring.slice(1)]); assert.equal(overrides.status, "confirmed"); assert.equal(overrides.geometry_reviewed, true);
  const bundle = packet([f], [cloud]);
  const prepared = core.prepareFeedbackScene(bundle, bundle.entries[0], await scenes.loadScene(f.id));
  assert.equal(prepared.summary.status, "ready"); assert.deepEqual(prepared.summary.conflicts, []);
  const change = prepared.summary.changes.find(entry => entry.objectId === "roof");
  assert.equal(change.substantive, true);
  assert.deepEqual(change.fields.find(field => field.field === "status"), { field: "status", before: "candidate", local: "candidate", incoming: "confirmed" }, "the Fieldwork decision is retained in the receipt fields");
  assert.deepEqual(change.fields.find(field => field.field === "geometry_reviewed"), { field: "geometry_reviewed", before: false, local: false, incoming: true });
  const planned = object(prepared.review);
  assert.equal(planned.status, "needs_adjustment", "import policy: a Fieldwork status is never a local decision");
  assert.equal(planned.geometry_reviewed, false); assert.equal(planned.model.height_verified, false); assert.equal(planned.fieldwork_import.pending, true);
  assert.deepEqual(planned.ring, [[9.5, 10], ...ring.slice(1)]);
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true); assert.equal(plan.counts.drafts, 1); assert.equal(plan.scenes[0].status, "ready");
  await apply(bundle, plan);
  const stored = object(await saved(f));
  assert.equal(stored.status, "needs_adjustment"); assert.equal(stored.geometry_reviewed, false); assert.equal(stored.fieldwork_import.pending, true);
  assert.equal(stored.roof_parts, undefined); assert.equal(stored.roof_annotation, null);
});

// ---- Spec section 4 (pull with push receipts): schema 2 packets, the metadata allowlist and old-frame recovery ----
const { shiftSceneObject } = load("src/lib/scene-feedback.ts");
const { footprintMetrics } = load("src/lib/scene-types.ts");
const runId = "20260929T000000Z-deadbeef";
function packet2(fixtures, remoteItems, bindings, cursor = "540") {
  const bundle = { schemaVersion: 2, origin: "https://fieldwork.example", projectId: "test-project", snapshotId: "snapshot-test", manifestSha256: hash("manifest"), cursor,
    entries: fixtures.map((f, i) => ({ itemId: f.base.id, sceneId: f.id, baseItem: wrap(f.base), item: wrap(remoteItems[i]), model: f.modelDoc, review: f.reviewDoc, reviewModel: f.modelDoc, actor: "workspace-token", timestamp: "2026-09-29T01:00:00Z", binding: bindings[i] })), quarantined: [] };
  return { ...bundle, digest: core.feedbackBundleDigest(bundle) };
}
// Exactly what internal/annotation/push/apply.go writes when it marks a superseded item: a tag plus two metadata.addr keys.
const marked = (base, newId) => ({ ...structuredClone(base), revision: 2, tags: [...base.tags, "superseded-frame"], metadata: { ...base.metadata, addr: { ...base.metadata.addr, supersededBy: { itemId: newId, runId }, stale: true } } });
const generationId = (f, reviewRevision = 7) => `addr-scene-${f.id}-m${f.model.model_signature.slice(0, 12)}-r${reviewRevision}`;
// The pristine document a push created for a scene generation (plan.go metadata + ProjectScene ExtraBindings).
function generationItem(f, reviewRevision = 7) {
  return { ...structuredClone(f.base), id: generationId(f, reviewRevision), revision: 1, tags: ["test-project", "pushed", "gen", "review"],
    metadata: { addr: { schemaVersion: 2, kind: "generation", sceneId: f.id, generation: { modelSignature: f.model.model_signature, reviewRevision, imageSha256: f.model.image_sha256, width: f.model.image_width, height: f.model.image_height, contextExpansion: null, inventory: "frozen",
      supersedes: [{ itemId: f.base.id, revision: 1, modelSignature: f.model.model_signature, imageSha256: f.model.image_sha256 }] }, push: { runId, pushedAt: "2026-09-29T00:00:00Z", tool: "annotate push", mirrorCursor: "538" } } },
    annotations: f.base.annotations.map(mark => ({ ...mark, properties: { addr: { ...mark.properties.addr, status: f.object.status, geometry_reviewed: f.object.geometry_reviewed, has_roof_line_edits: false } } })) };
}

test("schema 2: a push supersede mark is a receipt-only change, any other metadata change still conflicts, and the digest binds the entry binding", async () => {
  const f = await fixture("marked-frozen");
  const cloud = marked(f.base, generationId(f));
  const legacy = packet([f], [cloud]);
  assert.equal(legacy.schemaVersion, 1); assert.equal((await store.previewFeedback(legacy, principal)).canApply, true, "the allowlist also applies to a schema-1 packet");
  const bundle = packet2([f], [cloud], [{ kind: "frozen" }]);
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts));
  assert.equal(plan.counts.drafts, 0); assert.equal(plan.counts.receiptOnly, 1); assert.equal(plan.scenes[0].status, "ready"); assert.equal(plan.scenes[0].recoveredFrom, undefined);
  assert.deepEqual(plan.scenes[0].changes[0].fields.map(field => field.field).sort(), ["item.metadata", "item.tags"], "the mark is retained in the receipt, not applied locally");
  const result = await apply(bundle, plan); assert.equal(result.complete, true); assert.equal(result.applied[0].receiptOnly, true);
  assert.equal((await saved(f)).revision, 7, "a mark never touches the local review");
  const unrecovered = { ...structuredClone(cloud), revision: 3 }; unrecovered.metadata.addr.unrecoveredFieldwork = [{ itemId: f.base.id, revision: 2 }];
  assert.equal((await store.previewFeedback(packet2([f], [unrecovered], [{ kind: "frozen" }], "541"), principal)).canApply, true, "unrecoveredFieldwork is the third allowlisted key");
  for (const change of [item => { item.metadata.addr.sourceReviewRevision = 8; }, item => { item.metadata.addr.generation = { modelSignature: "other" }; }, item => { item.metadata.changed = true; }, item => { delete item.metadata.addr.snapshotId; }]) {
    const value = { ...structuredClone(cloud), revision: 3 }; change(value);
    const next = await store.previewFeedback(packet2([f], [value], [{ kind: "frozen" }], "541"), principal);
    assert.equal(next.canApply, false); assert(next.scenes[0].conflicts.some(message => /metadata/.test(message)), JSON.stringify(next.scenes[0].conflicts));
  }
  const rebound = structuredClone(bundle); rebound.entries[0].binding.kind = "push";
  await assert.rejects(store.previewFeedback(rebound, principal), /digest/, "the schema-2 digest covers the binding kind");
  const unbound = structuredClone(bundle); delete unbound.entries[0].binding; unbound.digest = core.feedbackBundleDigest(unbound);
  await assert.rejects(store.previewFeedback(unbound, principal), /packet schema/, "every schema-2 entry carries a binding");
  const schema1 = { ...structuredClone(bundle), schemaVersion: 1 }; schema1.digest = core.feedbackBundleDigest(schema1);
  await assert.rejects(store.previewFeedback(schema1, principal), /packet schema/, "a schema-1 entry carries no binding");
  await assert.rejects(store.previewFeedback(packet2([f], [cloud], [{ kind: "push", receiptSha256: "not-a-digest" }]), principal), /packet schema/);
  await assert.rejects(store.previewFeedback(packet2([f], [cloud], [{ kind: "frozen", extra: 1 }]), principal), /packet schema/);
  await assert.rejects(store.previewFeedback({ ...structuredClone(bundle), schemaVersion: 3 }, principal), /Invalid Fieldwork feedback packet/);
  assert.notEqual(core.feedbackBundleDigest({ ...bundle, schemaVersion: 1, entries: bundle.entries.map(entry => Object.fromEntries(Object.entries(entry).filter(([key]) => key !== "binding"))) }), bundle.digest, "schema 1 and schema 2 digests differ for the same evidence");
});

test("schema 2: a Fieldwork edit on the current-frame generation item (push binding) imports as a pending draft", async () => {
  const f = await fixture("generation-current");
  const g = { ...f, base: generationItem(f) };
  const cloud = remote(g, o => ({ ...o, ring: [[9.5, 10], ...o.ring.slice(1)], notes: "Edited on the generation item" }));
  // The full Go feedback.Binding shape (pushBinding): receipt identity plus the pushed frame.
  const bundle = packet2([g], [cloud], [{ kind: "push", runId, receiptSha256: hash(`receipt:${g.base.id}`), receiptRevision: 1,
    generation: { modelSignature: f.model.model_signature, reviewRevision: 7, reviewHeadSha256: f.reviewDoc.sha256, imageSha256: f.model.image_sha256, width: 100, height: 100 } }]);
  assert.equal(bundle.entries[0].itemId, g.base.id); assert.match(g.base.id, /^addr-scene-generation-current-m[a-f0-9]{12}-r7$/);
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts)); assert.equal(plan.counts.drafts, 1); assert.equal(plan.scenes[0].recoveredFrom, undefined);
  await apply(bundle, plan);
  const draft = object(await saved(f));
  assert.equal(draft.status, "needs_adjustment"); assert.equal(draft.geometry_reviewed, false); assert.equal(draft.model.height_verified, false);
  assert.deepEqual(draft.fieldwork_import, { pending: true, origin: bundle.origin, projectId: "test-project", itemId: g.base.id, revision: 2, cursor: "540" });
  assert.deepEqual(draft.ring, [[9.5, 10], ...ring.slice(1)]); assert.equal(draft.notes, "Edited on the generation item"); assert.deepEqual(draft.provenance, f.object.provenance);
  const receipt = JSON.parse(await readFile(path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), "test-project", "receipts", `${hash(g.base.id)}.json`), "utf8"));
  assert.equal(receipt.remote.sha256, bundle.entries[0].item.sha256); assert.equal(receipt.recoveredFrom, undefined);
});

// Frozen item and packet bound to the 512 frame; the local scene is the verified 512->1024 context expansion.
// `extra` adds frozen objects; `expand` maps the shifted frozen objects to the 1024 model's objects (model drift, replacements); `groups` are the 1024 model's.
async function expandedFixture(id, { object: objectOverrides = {}, extra = [], expand = objects => objects, groups = [] } = {}) {
  const oldImage = Buffer.from(`Old 512 image for ${id}`), oldSha = hash(oldImage);
  const source = { id: "roof", kind: "building", label: "Roof", ring, holes, status: "confirmed", geometry_reviewed: true, visibility: "full", source: "sam3-text-onnx", notes: "Source note", provenance: { source: "machine:preserved-test" },
    model: { height_m: 4, base_m: 0, height_source: "estimate", height_verified: true, width_m: 80, length_m: 80, area_m2: 6300, rotation_deg: 0, color: "#445566" },
    roof_parts: [{ ring_meters: ring, ring_z: [4, 4, 4, 4] }], roof_lines: [{ line_meters: [[10, 50], [90, 50]], kind: "ridge" }], ...objectOverrides };
  const region = { provider: "google", requested_bounds: [0, 0, 1, 1], bounds: [0, 0, 1, 1], imagery_zoom: 20, span_m: 512, native_meters_per_pixel: 1, spatial_group: "g", split: "train" };
  const old = { schema_version: 1, id, title: id, address_id: null, cohort: null, centroid: { lon: 145.3, lat: -37.8 }, region, image_sha256: oldSha, image_width: 512, image_height: 512,
    pixel_to_meters: [[1, 0], [0, 1], [0, 0]], meters_to_pixel: [[1, 0], [0, 1], [0, 0]], model_signature: hash(`model:${id}:512`), objects: [source, ...extra], groups: [], scans: {}, height_note: "Estimates" };
  const oldReview = { revision: 3, model_signature: old.model_signature, objects: structuredClone(old.objects), groups: [], reviewed_classes: [], basemap_mode: "raw", updated_by: "original-actor", updated_at: "2026-09-20T00:00:00Z", history: [] };
  const modelDoc = wrap(old), reviewDoc = wrap(oldReview);
  const base = { id: `addr-scene-${id}`, projectId: "test-project", title: id, adapter: "addr-scene", revision: 0,
    source: { sceneId: id, model: ref(modelDoc), review: ref(reviewDoc), reviewModel: ref(modelDoc), image: { url: `/api/v1/assets/${oldSha}`, sha256: oldSha }, modelSignature: old.model_signature, imageSha256: oldSha, width: 512, height: 512 },
    metadata: { addr: { sourceReviewRevision: 3, snapshotId: "snapshot-test" } }, status: "in_review", tags: [], notes: "",
    annotations: old.objects.map(o => ({ id: o.id, labelId: `test-project-kind-${o.kind}`, geometry: objectGeometry(o, old), properties: { addr: { objectId: o.id, layer: "review", source: o.source, provenance: o.provenance } } })) };
  const newImage = Buffer.from(`Expanded 1024 image for ${id}`), newSha = hash(newImage);
  const expanded = { ...structuredClone(old), model_signature: hash(`model:${id}:1024`), image_sha256: newSha, image_width: 1024, image_height: 1024,
    pixel_to_meters: [[1, 0], [0, 1], [-256, -256]], meters_to_pixel: [[1, 0], [0, 1], [256, 256]], objects: expand(old.objects.map(o => shiftSceneObject(o, 256))), groups,
    context_expansion: { source_model_signature: old.model_signature, source_image_sha256: oldSha, offset_pixels: [256, 256], source_size: [512, 512], preserved_pixels_sha256: hash("preserved") } };
  const localReview = { revision: 7, model_signature: expanded.model_signature, objects: structuredClone(expanded.objects), groups: structuredClone(groups), reviewed_classes: [], basemap_mode: "raw", updated_by: "local", updated_at: "2026-09-28T00:00:00Z", history: [] };
  const folder = path.join(directory, "data/scenes", id); await mkdir(path.join(folder, "models"), { recursive: true });
  await writeFile(path.join(folder, "latest.json"), JSON.stringify(expanded)); await writeFile(path.join(folder, "review.json"), JSON.stringify(localReview));
  await writeFile(path.join(folder, "models", `${old.model_signature}.json`), modelDoc.json); await writeFile(path.join(folder, `${newSha}.png`), newImage);
  return { id, folder, model: old, expanded, review: oldReview, base, object: source, modelDoc, reviewDoc };
}
const shifted = points => points.map(([x, y]) => [x + 256, y + 256]);

test("an old-frame Fieldwork edit is carried through the verified 512->1024 expansion as a pending draft; roof line metres stay invariant", async () => {
  const f = await expandedFixture("expansion");
  const dragged = [[9.5, 10], ...ring.slice(1)];
  const lines = { schema_version: 1, outline: { ring: dragged, holes }, baseline: { vertices: [], edges: [] }, vertices: [[20, 50], [80, 50]], edges: [{ a: 0, b: 1, kind: "ridge" }], checked: true, notes: "" };
  const cloud = remote(f, o => ({ ...o, ring: dragged, status: "confirmed", geometry_reviewed: true, notes: "Old-frame note", roof_line_edits: lines }));
  assert.deepEqual(cloud.annotations[0].properties.addr.overrides.ring, dragged, "the Fieldwork edit is expressed in the 512 frame");
  const bundle = packet([f], [cloud]);
  const before = await readFile(path.join(f.folder, "review.json"));
  const prepared = core.prepareFeedbackScene(bundle, bundle.entries[0], await scenes.loadScene(f.id));
  assert.equal(prepared.summary.status, "ready", JSON.stringify(prepared.summary.conflicts));
  const recoveredFrom = { modelSignature: f.model.model_signature, imageSha256: f.model.image_sha256, method: "expansion-256" };
  assert.deepEqual(prepared.summary.recoveredFrom, recoveredFrom);
  const planned = object(prepared.review);
  assert.deepEqual(planned.ring, shifted(dragged)); assert.deepEqual(planned.holes, holes.map(shifted));
  assert.equal(planned.status, "needs_adjustment", "a carried Fieldwork decision is never a local decision"); assert.equal(planned.geometry_reviewed, false); assert.equal(planned.model.height_verified, false);
  assert.equal(planned.notes, "Old-frame note"); assert.deepEqual(planned.provenance, f.object.provenance);
  assert.equal(planned.fieldwork_import.pending, true); assert.deepEqual(planned.fieldwork_import.recoveredFrom, recoveredFrom);
  assert.deepEqual(planned.roof_line_edits.vertices, lines.vertices, "roof line vertices are metres and do not shift"); assert.deepEqual(planned.roof_line_edits.edges, lines.edges);
  assert.deepEqual(planned.roof_line_edits.outline.ring, planned.ring, "the captured outline follows the shifted ring"); assert.equal(planned.roof_line_edits.checked, false);
  assert.equal(planned.roof_parts, undefined); assert.equal(planned.roof_annotation, null);
  assert(prepared.summary.changes[0].implication.includes("expansion-256"));
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts)); assert.equal(plan.counts.drafts, 1); assert.deepEqual(plan.scenes[0].recoveredFrom, recoveredFrom);
  assert.deepEqual(await readFile(path.join(f.folder, "review.json")), before, "preview writes nothing");
  const result = await apply(bundle, plan); assert.equal(result.complete, true);
  const stored = await saved(f); assert.equal(stored.revision, 8); assert.equal(stored.model_signature, f.expanded.model_signature);
  const draft = object(stored);
  assert.equal(draft.status, "needs_adjustment"); assert.equal(draft.geometry_reviewed, false); assert.equal(draft.model.height_verified, false);
  assert.deepEqual(draft.ring, planned.ring); assert.deepEqual(draft.fieldwork_import.recoveredFrom, recoveredFrom); assert.deepEqual(draft.roof_line_edits.vertices, lines.vertices);
  const oldFrameMetres = footprintMetrics(dragged, f.model.pixel_to_meters, holes), newFrameMetres = footprintMetrics(draft.ring, f.expanded.pixel_to_meters, draft.holes);
  assert.deepEqual(newFrameMetres, oldFrameMetres, "footprint metres recomputed on the current frame equal the old-frame edit's metres");
  assert.equal(draft.model.area_m2, newFrameMetres.area_m2);
  const receipt = JSON.parse(await readFile(path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), "test-project", "receipts", `${hash(f.base.id)}.json`), "utf8"));
  assert.equal(receipt.sourceModelSha256, f.modelDoc.sha256, "the receipt binds to the superseded model the edit was made on"); assert.deepEqual(receipt.recoveredFrom, recoveredFrom);
  assert.equal((await apply(bundle, plan)).applied[0].alreadyApplied, true);
  // A later local re-decision on the current frame overlaps a further old-frame status edit: conflict, not silent overwrite.
  const local = await saved(f); object(local).status = "confirmed"; object(local).geometry_reviewed = true; delete object(local).fieldwork_import; await scenes.saveScene(f.id, local, principal);
  const again = remote(f, o => ({ ...o, status: "rejected" }), 3, cloud);
  const next = await store.previewFeedback(packet([f], [again], "31"), principal);
  assert.equal(next.canApply, false); assert(next.scenes[0].conflicts.some(message => /roof\.status: local and Fieldwork edits overlap/.test(message)), JSON.stringify(next.scenes[0].conflicts));
});

test("an edit on the frozen item of a same-image promotion lands via same-frame recovery as a pending draft", async () => {
  const f = await fixture("promotion");
  const promoted = { ...structuredClone(f.model), model_signature: hash("model:promotion:promoted") };
  const localReview = { ...structuredClone(f.review), model_signature: promoted.model_signature, objects: structuredClone(promoted.objects), updated_by: "local" };
  await writeFile(path.join(f.folder, "latest.json"), JSON.stringify(promoted)); await writeFile(path.join(f.folder, "review.json"), JSON.stringify(localReview));
  const cloud = remote(f, o => ({ ...o, label: "Main roof (Fieldwork)", notes: "Looks right (old model)" }));
  const bundle = packet([f], [cloud]); const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts)); assert.equal(plan.counts.drafts, 1);
  const recoveredFrom = { modelSignature: f.model.model_signature, imageSha256: f.model.image_sha256, method: "same-frame" };
  assert.deepEqual(plan.scenes[0].recoveredFrom, recoveredFrom);
  await apply(bundle, plan); const stored = await saved(f), draft = object(stored);
  assert.equal(stored.model_signature, promoted.model_signature);
  assert.equal(draft.status, "needs_adjustment"); assert.equal(draft.geometry_reviewed, false); assert.equal(draft.model.height_verified, false);
  assert.equal(draft.label, "Main roof (Fieldwork)"); assert.equal(draft.notes, "Looks right (old model)"); assert.deepEqual(draft.ring, ring, "an identical frame shifts nothing");
  assert.deepEqual(draft.fieldwork_import.recoveredFrom, recoveredFrom);
});

// house-25822 shape: the frozen review left the object unedited (candidate, byte-equal to the 512 model); the 1024 model re-proposes the
// same outline under the same id but assigns a parent, a group and a base height, and a fuller detection takes over a clipped seed.
test("old-frame recovery imports only what changed on the old frame: the current model's parent, group and base on a shared id are model drift, never a Fieldwork edit", async () => {
  const candidateModel = { height_m: 4, base_m: 0, height_source: "estimate", height_verified: false, width_m: 80, length_m: 80, area_m2: 6300, rotation_deg: 0, color: "#445566" };
  const seed = { id: "seed", kind: "building", label: "Seed", ring: [[400, 400], [450, 400], [450, 450], [400, 450]], status: "candidate", geometry_reviewed: false, visibility: "truncated", source: "sam3-text-onnx", notes: "",
    model: { ...candidateModel, height_m: 3, width_m: 50, length_m: 50, area_m2: 2500, color: "#111111" } };
  const house = { id: "house", kind: "building", label: "House", ring: [[300, 300], [700, 300], [700, 700], [300, 700]], status: "candidate", geometry_reviewed: false, visibility: "full", source: "sam3-text-onnx", notes: "",
    model: { ...candidateModel, height_m: 5, width_m: 400, length_m: 400, area_m2: 160000, color: "#000000" } };
  const f = await expandedFixture("expansion-drift", { object: { status: "candidate", geometry_reviewed: false, model: candidateModel }, extra: [seed], groups: [{ id: "house", label: "House" }],
    expand: ([roof, shiftedSeed]) => [{ ...roof, parent_id: "house", group_id: "house", model: { ...roof.model, base_m: 3.2 } }, house,
      { ...shiftedSeed, id: "whole", label: "Whole", ring: [[640, 640], [760, 640], [760, 760], [640, 760]], replaces: ["seed"] }] });
  const drift = o => ({ parent_id: o.parent_id, group_id: o.group_id, base_m: o.model.base_m });
  assert.deepEqual(drift((await scenes.loadScene(f.id)).review.objects[0]), { parent_id: "house", group_id: "house", base_m: 3.2 });
  // 1. Notes only on the drifted object: the note lands, nothing else moves, no draft is fabricated.
  const cloud = remote(f, o => ({ ...o, notes: "Fieldwork: looks like a shed" }));
  assert.deepEqual(cloud.annotations[0].properties.addr.overrides, { notes: "Fieldwork: looks like a shed" }, "Fieldwork wrote only a note");
  const bundle = packet([f], [cloud]);
  const prepared = core.prepareFeedbackScene(bundle, bundle.entries[0], await scenes.loadScene(f.id));
  assert.equal(prepared.summary.status, "ready", JSON.stringify(prepared.summary.conflicts)); assert.equal(prepared.summary.recoveredFrom.method, "expansion-256");
  assert.deepEqual(prepared.summary.changes.map(c => [c.objectId, c.substantive, c.fields.map(x => x.field)]), [["roof", false, ["notes"]]], JSON.stringify(prepared.summary.changes));
  const planned = object(prepared.review);
  assert.equal(planned.notes, "Fieldwork: looks like a shed"); assert.deepEqual(drift(planned), { parent_id: "house", group_id: "house", base_m: 3.2 });
  assert.equal(planned.status, "candidate"); assert.equal(planned.fieldwork_import, undefined);
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts)); assert.equal(plan.counts.notesOnly, 1); assert.equal(plan.counts.drafts, 0);
  await apply(bundle, plan);
  let stored = await saved(f);
  assert.equal(object(stored).notes, "Fieldwork: looks like a shed"); assert.deepEqual(drift(object(stored)), { parent_id: "house", group_id: "house", base_m: 3.2 }); assert.equal(object(stored).status, "candidate");
  assert.deepEqual(stored.objects.map(o => o.id).sort(), ["house", "roof", "whole"], "recovery adds no objects the Fieldwork item did not draw");
  assert.equal(stored.objects.find(o => o.id === "whole").notes, "", "an untouched replacement inherits nothing");
  // 2. A note on the replaced seed moves onto the detection that took it over as a note; its label and status are not rewritten.
  const seedCurrent = sceneObjects(cloud, f.model, f.review).find(o => o.id === "seed");
  const cloud2 = { ...updateSceneObject(cloud, seed, { ...structuredClone(seedCurrent), notes: "Fieldwork: seed note" }, f.model), revision: 3 };
  const bundle2 = packet([f], [cloud2], "31"), plan2 = await store.previewFeedback(bundle2, principal);
  assert.equal(plan2.canApply, true, JSON.stringify(plan2.scenes[0].conflicts));
  assert.deepEqual(plan2.scenes[0].changes.map(c => [c.objectId, c.substantive, c.fields.map(x => x.field)]), [["whole", false, ["notes"]]], JSON.stringify(plan2.scenes[0].changes));
  await apply(bundle2, plan2); stored = await saved(f);
  const whole = stored.objects.find(o => o.id === "whole");
  assert.equal(whole.notes, "Fieldwork: seed note"); assert.equal(whole.label, "Whole"); assert.equal(whole.status, "candidate"); assert.equal(whole.fieldwork_import, undefined);
  // 3. A ring drag on the drifted object shifts by +256 and lands as a draft; the current model's parent, group and base still stand.
  const dragged = [[9.5, 10], ...ring.slice(1)];
  const cloud3 = remote(f, o => ({ ...o, ring: dragged }), 4, cloud2);
  const bundle3 = packet([f], [cloud3], "32"), plan3 = await store.previewFeedback(bundle3, principal);
  assert.equal(plan3.canApply, true, JSON.stringify(plan3.scenes[0].conflicts)); assert.equal(plan3.counts.drafts, 1);
  const fields3 = plan3.scenes[0].changes.map(c => [c.objectId, c.fields.map(x => x.field)]);
  assert.equal(fields3.length, 1); assert.equal(fields3[0][0], "roof"); assert(fields3[0][1].includes("ring"), JSON.stringify(fields3));
  // The Fieldwork client clears derived roof state with an outline change (updateSceneObject); the drift paths never appear.
  const outlineEdit = ["ring", "model.rotation_deg", "roof_parts", "roof_lines", "roof_annotation", "roof_geometry_source", "roof_geometry_note"];
  assert(fields3[0][1].every(field => outlineEdit.includes(field)), `only the dragged outline and what the client clears with it changed: ${JSON.stringify(fields3)}`);
  assert(!fields3[0][1].some(field => ["parent_id", "group_id", "model.base_m", "status", "geometry_reviewed"].includes(field)), JSON.stringify(fields3));
  await apply(bundle3, plan3); stored = await saved(f);
  const draft = object(stored);
  assert.deepEqual(draft.ring, shifted(dragged)); assert.deepEqual(draft.holes, holes.map(shifted)); assert.deepEqual(drift(draft), { parent_id: "house", group_id: "house", base_m: 3.2 });
  assert.equal(draft.status, "needs_adjustment"); assert.equal(draft.geometry_reviewed, false); assert.equal(draft.fieldwork_import.pending, true); assert.equal(draft.fieldwork_import.recoveredFrom.method, "expansion-256");
});

// 253 mooroolbark-* promotions: same image, new model signature, roof pipeline state present only in the local model; 188 have no review.json.
test("same-frame recovery never deletes roof state the frozen model lacks: a notes-only edit on the frozen item of a promoted scene lands as notes only", async () => {
  const f = await fixture("promotion-drift", { status: "candidate", geometry_reviewed: false, roof_parts: undefined, roof_lines: undefined, roof_feedback: undefined,
    model: { height_m: 4, base_m: 0, height_source: "estimate", height_verified: false, width_m: 80, length_m: 80, area_m2: 6300, rotation_deg: 0, color: "#445566" } });
  const roofState = { roof_type: "gabled", roof_parts: [{ ring_meters: ring, ring_z: [4, 4, 4, 4] }], roof_lines: [{ line_meters: [[10, 50], [90, 50]], kind: "ridge" }], roof_geometry_note: "auto",
    roof_annotation: { schema_version: 1, roof_type: "gabled", vertices: [[10, 10, 3], [90, 10, 3], [90, 90, 3], [10, 90, 3], [10, 50, 4], [90, 50, 4]], faces: [[0, 1, 5, 4], [4, 5, 2, 3]], edges: [{ a: 4, b: 5, kind: "ridge" }] } };
  const promoted = { ...structuredClone(f.model), model_signature: hash("model:promotion-drift:promoted"), objects: [{ ...structuredClone(f.object), ...roofState }] };
  await writeFile(path.join(f.folder, "latest.json"), JSON.stringify(promoted)); await rm(path.join(f.folder, "review.json"));
  const cloud = remote(f, o => ({ ...o, notes: "Fieldwork: note only" }));
  assert.deepEqual(cloud.annotations[0].properties.addr.overrides, { notes: "Fieldwork: note only" }, "Fieldwork wrote only a note");
  const bundle = packet([f], [cloud]), plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts)); assert.equal(plan.counts.notesOnly, 1); assert.equal(plan.counts.drafts, 0);
  assert.equal(plan.scenes[0].recoveredFrom.method, "same-frame");
  assert.deepEqual(plan.scenes[0].changes.map(c => [c.objectId, c.substantive, c.fields.map(x => x.field)]), [["roof", false, ["notes"]]], JSON.stringify(plan.scenes[0].changes));
  await apply(bundle, plan);
  const stored = await saved(f), draft = object(stored);
  assert.equal(stored.model_signature, promoted.model_signature); assert.equal(draft.notes, "Fieldwork: note only"); assert.equal(draft.status, "candidate"); assert.equal(draft.fieldwork_import, undefined);
  for (const key of Object.keys(roofState)) assert.deepEqual(draft[key], roofState[key], `${key} present only in the local model is not a Fieldwork edit and stays`);
});

test("an edit on a frame that is neither the verified expansion nor identical reports frame_changed_unrecoverable and applies nothing", async () => {
  const f = await fixture("recapture");
  const image = Buffer.from("Recaptured 1024 image"), sha = hash(image);
  const recaptured = { ...structuredClone(f.model), model_signature: hash("model:recapture:1024"), image_sha256: sha, image_width: 1024, image_height: 1024, objects: f.model.objects.map(o => shiftSceneObject(o, 256)) };
  const localReview = { ...structuredClone(f.review), model_signature: recaptured.model_signature, objects: structuredClone(recaptured.objects) };
  await writeFile(path.join(f.folder, "latest.json"), JSON.stringify(recaptured)); await writeFile(path.join(f.folder, "review.json"), JSON.stringify(localReview)); await writeFile(path.join(f.folder, `${sha}.png`), image);
  const before = await readFile(path.join(f.folder, "review.json"));
  const cloud = remote(f, o => ({ ...o, notes: "Made on the old frame" }));
  const bundle = packet([f], [cloud]); const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, false); assert.equal(plan.scenes[0].status, "conflict"); assert.equal(plan.scenes[0].recoveredFrom, undefined); assert.deepEqual(plan.scenes[0].changes, []);
  assert.match(plan.scenes[0].conflicts[0], /^frame_changed_unrecoverable: the Fieldwork edit on addr-scene-recapture was made on an older model\/image frame that cannot be recovered automatically; it stays on that item and in the change feed\.$/);
  await assert.rejects(apply(bundle, plan), /Resolve the preview conflicts/);
  assert.deepEqual(await readFile(path.join(f.folder, "review.json")), before, "nothing applied");
  await assert.rejects(readFile(path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), "test-project", "receipts", `${hash(f.base.id)}.json`)), /ENOENT/, "no receipt for an unrecovered edit");
  // A local review that is itself stale on the current frame also fails closed before any recovery attempt.
  const stale = await fixture("stale-local");
  await writeFile(path.join(stale.folder, "latest.json"), JSON.stringify({ ...structuredClone(stale.model), model_signature: hash("model:stale-local:next") }));
  const stalePlan = await store.previewFeedback(packet([stale], [remote(stale, o => ({ ...o, notes: "x" }))]), principal);
  assert.equal(stalePlan.canApply, false); assert.match(stalePlan.scenes[0].conflicts[0], /stale/);
});

// ---- Follow-ups: two entries for one scene in a schema-2 packet, and old-frame recovery across a snapshot chain ----
// The live shape after a push on a same-image promotion: the marked frozen item (bound to the frozen model) and the generation
// item (bound to the promoted model the scene now shows) can both carry Fieldwork edits; one packet delivers both entries.
async function promotedPair(id) {
  const f = await fixture(id);
  const promoted = { ...structuredClone(f.model), model_signature: hash(`model:${id}:promoted`) };
  const promotedReview = { ...structuredClone(f.review), model_signature: promoted.model_signature, objects: structuredClone(promoted.objects), updated_by: "local" };
  await writeFile(path.join(f.folder, "latest.json"), JSON.stringify(promoted)); await writeFile(path.join(f.folder, "review.json"), JSON.stringify(promotedReview));
  const g = { ...f, model: promoted, review: promotedReview, modelDoc: wrap(promoted), reviewDoc: wrap(promotedReview) };
  g.base = { ...generationItem(g), source: { ...f.base.source, model: ref(g.modelDoc), review: ref(g.reviewDoc), reviewModel: ref(g.modelDoc), modelSignature: promoted.model_signature } };
  const bindings = {
    generation: { kind: "push", runId, receiptSha256: hash(`receipt:${g.base.id}`), receiptRevision: 1, generation: { modelSignature: promoted.model_signature, reviewRevision: 7, imageSha256: promoted.image_sha256, width: 100, height: 100 } },
    frozen: { kind: "push", runId, receiptSha256: hash(`receipt:${f.base.id}`), receiptRevision: 2 },
  };
  return { f, g, bindings };
}

test("schema 2: two entries for one scene apply in order (generation item on the current frame, then the marked frozen item through recovery), each with its own receipt", async () => {
  const { f, g, bindings } = await promotedPair("two-entries");
  const genCloud = remote(g, o => ({ ...o, notes: "Generation note" }));
  const frozenCloud = remote(f, o => ({ ...o, label: "Frozen label" }), 3, marked(f.base, g.base.id));
  // The packet lists the frozen entry first on purpose: the store orders entries, not the packet.
  const bundle = packet2([f, g], [frozenCloud, genCloud], [bindings.frozen, bindings.generation]);
  assert.deepEqual(bundle.entries.map(e => e.sceneId), [f.id, f.id]);
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes.map(s => s.conflicts)));
  assert.deepEqual(plan.scenes.map(s => s.itemId), [g.base.id, f.base.id], "current-frame generation entry first, then the frozen entry through recovery");
  assert.deepEqual(plan.scenes.map(s => [s.status, s.localRevision, s.recoveredFrom?.method]), [["ready", 7, undefined], ["ready", 8, "same-frame"]], "the frozen entry is planned against the review the generation entry commits");
  assert.equal(plan.counts.scenes, 2); assert.equal(plan.counts.notesOnly, 1); assert.equal(plan.counts.drafts, 1); assert.equal(plan.counts.receiptOnly, 1);
  let saves = 0;
  await assert.rejects(apply(bundle, plan, { afterSceneSave: async () => { if (++saves === 1) throw new Error("interrupted after the generation entry"); } }), /interrupted/);
  assert.equal((await saved(f)).revision, 8, "the generation entry committed before the interruption");
  const result = await apply(bundle, plan); assert.equal(result.complete, true);
  assert.deepEqual(result.applied.map(a => [a.itemId, a.alreadyApplied, a.localRevision]), [[g.base.id, true, 8], [f.base.id, false, 9]]);
  const stored = await saved(f), roof = object(stored);
  assert.equal(stored.revision, 9); assert.equal(roof.notes, "Generation note"); assert.equal(roof.label, "Frozen label");
  assert.equal(roof.status, "needs_adjustment"); assert.equal(roof.geometry_reviewed, false); assert.equal(roof.model.height_verified, false);
  assert.equal(roof.fieldwork_import.pending, true); assert.equal(roof.fieldwork_import.itemId, f.base.id); assert.equal(roof.fieldwork_import.recoveredFrom.method, "same-frame");
  const receipts = path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), "test-project", "receipts");
  const genReceipt = JSON.parse(await readFile(path.join(receipts, `${hash(g.base.id)}.json`), "utf8")), frozenReceipt = JSON.parse(await readFile(path.join(receipts, `${hash(f.base.id)}.json`), "utf8"));
  assert.equal(genReceipt.localRevision, 8); assert.equal(genReceipt.recoveredFrom, undefined); assert.equal(genReceipt.remote.sha256, bundle.entries[1].item.sha256);
  assert.equal(frozenReceipt.localRevision, 9); assert.equal(frozenReceipt.recoveredFrom.method, "same-frame"); assert.equal(frozenReceipt.remote.sha256, bundle.entries[0].item.sha256);
  assert.equal((await apply(bundle, plan)).applied.every(a => a.alreadyApplied), true, "a repeated apply is a no-op for both entries");
  assert.equal((await saved(f)).revision, 9);
  // Overlapping edits on the two items conflict on the second (frozen) entry instead of silently overwriting the first.
  const pair = await promotedPair("two-entries-overlap");
  const overlap = packet2([pair.g, pair.f], [remote(pair.g, o => ({ ...o, notes: "From the generation item" })), remote(pair.f, o => ({ ...o, notes: "From the frozen item" }), 3, marked(pair.f.base, pair.g.base.id))], [pair.bindings.generation, pair.bindings.frozen]);
  const conflicted = await store.previewFeedback(overlap, principal);
  assert.equal(conflicted.canApply, false); assert.deepEqual(conflicted.scenes.map(s => [s.itemId, s.status]), [[pair.g.base.id, "ready"], [pair.f.base.id, "conflict"]]);
  assert(conflicted.scenes[1].conflicts.some(m => /roof\.notes: local and Fieldwork edits overlap/.test(m)), JSON.stringify(conflicted.scenes[1].conflicts));
  await assert.rejects(apply(overlap, conflicted), /Resolve the preview conflicts/); assert.equal((await saved(pair.f)).revision, 7, "nothing applied while the scene conflicts");
  // Item ids must still be distinct.
  const duplicate = structuredClone(bundle); duplicate.entries[1] = structuredClone(duplicate.entries[0]); duplicate.digest = core.feedbackBundleDigest(duplicate);
  await assert.rejects(store.previewFeedback(duplicate, principal), /distinct/);
});

// house-25833 shape: frozen 512 model -> same-image re-extraction (promotion: same image sha, new signature, model drift) -> 1024
// expansion whose context_expansion names the intermediate signature. Snapshots of every hop live under data/scenes/<id>/models/.
const seedObject = { id: "seed", kind: "building", label: "Seed", ring: [[400, 400], [450, 400], [450, 450], [400, 450]], status: "candidate", geometry_reviewed: false, visibility: "truncated", source: "sam3-text-onnx", notes: "",
  model: { height_m: 3, base_m: 0, height_source: "estimate", height_verified: false, width_m: 50, length_m: 50, area_m2: 2500, rotation_deg: 0, color: "#111111" } };
const drifted = roof => ({ ...roof, label: "Roof (re-extracted)", model: { ...roof.model, color: "#123456" } });
async function chainedFixture(id, variant = "good") {
  const f = await expandedFixture(id, { extra: [seedObject], expand: ([roof, shiftedSeed]) => [drifted(roof), { ...shiftedSeed, id: "whole", label: "Whole", ring: [[640, 640], [760, 640], [760, 760], [640, 760]], replaces: ["seed"] }] });
  const promoted = { ...structuredClone(f.model), model_signature: hash(`model:${id}:512:promoted`) };
  promoted.objects[0] = drifted(promoted.objects[0]);
  if (variant === "image") promoted.image_sha256 = hash(`Recaptured 512 image for ${id}`);
  if (variant === "frame") { promoted.pixel_to_meters = [[1, 0], [0, 1], [3, 3]]; promoted.meters_to_pixel = [[1, 0], [0, 1], [-3, -3]]; }
  f.expanded.context_expansion = { ...f.expanded.context_expansion, source_model_signature: promoted.model_signature, source_image_sha256: promoted.image_sha256 };
  await writeFile(path.join(f.folder, "latest.json"), JSON.stringify(f.expanded));
  if (variant !== "missing") await writeFile(path.join(f.folder, "models", `${promoted.model_signature}.json`), JSON.stringify(promoted));
  await writeFile(path.join(f.folder, "models", `${f.expanded.model_signature}.json`), JSON.stringify(f.expanded));
  return { ...f, promoted };
}

test("a frozen-frame edit crosses the chain frozen 512 -> same-image 512 -> 1024 expansion: outlines shift once, roof line metres stay invariant, a replaced seed's note lands on its replacement", async () => {
  const f = await chainedFixture("chain");
  const chainRecord = { modelSignature: f.model.model_signature, imageSha256: f.model.image_sha256, method: "chain", hops: [
    { modelSignature: f.promoted.model_signature, imageSha256: f.promoted.image_sha256, method: "same-frame" },
    { modelSignature: f.expanded.model_signature, imageSha256: f.expanded.image_sha256, method: "expansion-256" }] };
  assert.equal(core.MAX_RECOVERY_HOPS, 4);
  assert.deepEqual(core.resolveRecoveryChain(f.model, f.expanded, [f.expanded, f.promoted, f.promoted]).methods, ["same-frame", "expansion-256"], "duplicate snapshots and the live model in models/ do not change the chain");
  assert.equal(core.resolveRecoveryChain(f.model, f.expanded, []), null, "without the intermediate snapshot the one-hop rule still fails closed");
  const local = (await scenes.loadScene(f.id)).review;
  assert.equal(object(local).label, "Roof (re-extracted)", "the current model carries the re-extraction's drift");
  const dragged = [[9.5, 10], ...ring.slice(1)];
  const lines = { schema_version: 1, outline: { ring: dragged, holes }, baseline: { vertices: [], edges: [] }, vertices: [[20, 50], [80, 50]], edges: [{ a: 0, b: 1, kind: "ridge" }], checked: true, notes: "" };
  const roofEdit = remote(f, o => ({ ...o, ring: dragged, notes: "Chain note", roof_line_edits: lines }));
  const seedCurrent = sceneObjects(roofEdit, f.model, f.review).find(o => o.id === "seed");
  const cloud = { ...updateSceneObject(roofEdit, seedObject, { ...structuredClone(seedCurrent), notes: "Fieldwork: seed note" }, f.model), revision: 2 };
  const bundle = packet([f], [cloud]);
  const before = await readFile(path.join(f.folder, "review.json"));
  const plan = await store.previewFeedback(bundle, principal);
  assert.equal(plan.canApply, true, JSON.stringify(plan.scenes[0].conflicts)); assert.equal(plan.counts.drafts, 1); assert.equal(plan.counts.notesOnly, 1);
  assert.deepEqual(plan.scenes[0].recoveredFrom, chainRecord);
  const byObject = Object.fromEntries(plan.scenes[0].changes.map(c => [c.objectId, c]));
  assert.deepEqual(Object.keys(byObject).sort(), ["roof", "whole"]);
  assert.equal(byObject.whole.substantive, false); assert.deepEqual(byObject.whole.fields.map(x => x.field), ["notes"]);
  assert.equal(byObject.roof.substantive, true); assert(byObject.roof.fields.some(x => x.field === "ring") && byObject.roof.fields.some(x => x.field === "roof_line_edits"), JSON.stringify(byObject.roof.fields.map(x => x.field)));
  assert(!byObject.roof.fields.some(x => ["label", "model.color"].includes(x.field)), "the intermediate re-extraction's drift is never an incoming edit");
  assert(byObject.roof.implication.includes("chain (same-frame -> expansion-256)"), byObject.roof.implication);
  assert.deepEqual(await readFile(path.join(f.folder, "review.json")), before, "preview writes nothing");
  const result = await apply(bundle, plan); assert.equal(result.complete, true);
  const stored = await saved(f), roof = object(stored), whole = stored.objects.find(o => o.id === "whole");
  assert.equal(stored.revision, 8); assert.equal(stored.model_signature, f.expanded.model_signature);
  assert.deepEqual(roof.ring, shifted(dragged), "the outline shifts exactly once, at the expansion hop"); assert.deepEqual(roof.holes, holes.map(shifted));
  assert.equal(roof.notes, "Chain note"); assert.equal(roof.label, "Roof (re-extracted)", "drift on an unedited path stays as the current model has it");
  assert.equal(roof.status, "needs_adjustment"); assert.equal(roof.geometry_reviewed, false); assert.equal(roof.model.height_verified, false);
  assert.deepEqual(roof.fieldwork_import.recoveredFrom, chainRecord); assert.equal(roof.fieldwork_import.pending, true);
  assert.deepEqual(roof.roof_line_edits.vertices, lines.vertices, "roof line vertices are metres and do not shift"); assert.deepEqual(roof.roof_line_edits.edges, lines.edges);
  assert.deepEqual(roof.roof_line_edits.outline.ring, roof.ring, "the captured outline follows the shifted ring"); assert.equal(roof.roof_line_edits.checked, false);
  assert.equal(roof.roof_parts, undefined); assert.equal(roof.roof_annotation, null);
  assert.deepEqual(footprintMetrics(roof.ring, f.expanded.pixel_to_meters, roof.holes), footprintMetrics(dragged, f.model.pixel_to_meters, holes), "footprint metres are unchanged by the chain");
  assert.equal(whole.notes, "Fieldwork: seed note", "the seed's note follows `replaces` onto the detection that took it over"); assert.equal(whole.label, "Whole"); assert.equal(whole.status, "candidate"); assert.equal(whole.fieldwork_import, undefined);
  assert.deepEqual(stored.objects.map(o => o.id).sort(), ["roof", "whole"], "no ghost of the replaced seed and nothing the Fieldwork item did not draw");
  const receipt = JSON.parse(await readFile(path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), "test-project", "receipts", `${hash(f.base.id)}.json`), "utf8"));
  assert.deepEqual(receipt.recoveredFrom, chainRecord); assert.equal(receipt.sourceModelSha256, f.modelDoc.sha256, "the receipt binds to the frozen model the edit was made on");
  assert.equal((await apply(bundle, plan)).applied[0].alreadyApplied, true);
});

test("a broken chain (intermediate image sha or frame mismatch, or a missing snapshot) reports frame_changed_unrecoverable and applies nothing", async () => {
  for (const variant of ["image", "frame", "missing"]) {
    const f = await chainedFixture(`broken-${variant}`, variant);
    const before = await readFile(path.join(f.folder, "review.json"));
    const cloud = remote(f, o => ({ ...o, notes: `Made on the frozen frame (${variant})` }));
    const bundle = packet([f], [cloud]), plan = await store.previewFeedback(bundle, principal);
    assert.equal(plan.canApply, false, variant); assert.equal(plan.scenes[0].status, "conflict"); assert.equal(plan.scenes[0].recoveredFrom, undefined); assert.deepEqual(plan.scenes[0].changes, []);
    assert.match(plan.scenes[0].conflicts[0], /^frame_changed_unrecoverable: the Fieldwork edit on addr-scene-broken-/, variant);
    await assert.rejects(apply(bundle, plan), /Resolve the preview conflicts/);
    assert.deepEqual(await readFile(path.join(f.folder, "review.json")), before, `${variant}: nothing applied`);
    await assert.rejects(readFile(path.join(directory, "data/reviews/fieldwork-feedback", hash(bundle.origin), "test-project", "receipts", `${hash(f.base.id)}.json`)), /ENOENT/, `${variant}: no receipt`);
  }
});
