import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { test } from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url), modules = new Map();
function load(file) {
  const resolved = path.resolve(file);
  if (modules.has(resolved)) return modules.get(resolved);
  const exports = {}; modules.set(resolved, exports);
  const code = ts.transpileModule(readFileSync(resolved, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
  new Function("exports", "require", code)(exports, (id) => id.startsWith(".") ? load(path.resolve(path.dirname(resolved), `${id}.ts`)) : require(id));
  return exports;
}
const { sceneObjects, objectGeometry, updateSceneObject, sameSceneFrame, outlinePendingFor, focusObjectId, decideSceneObject, SCENE_DECISIONS, sceneColourMode, sceneColourKey, isReviewProject, objectLabelIds, vegetationLabel, vegetationLabelType, isVegetationLabel, editedItemStatus, nextOpenObjectId, vegetationNeedsType, vegetationProposal, nearlySameGeometry, normalizeProjectId, reviewFamily, isVegetationItem } = load("src/lib/annotations/addr-scene.ts");
const { insertVertex, moveEdge, removeVertex, vertices } = load("src/lib/annotations/model.ts");
const { polygonIssues } = load("src/lib/scene-types.ts");
const ring = [[10, 10], [90, 10], [90, 90], [10, 90]];
const holes = [[[30, 30], [40, 30], [40, 40], [30, 40]]];
const original = { id: "roof-1", kind: "building", label: "Machine confirmed roof", ring, holes, status: "confirmed", geometry_reviewed: true, source: "sam3", provenance: { source: "machine:decision-policy-v2" }, model: { height_m: 4, height_verified: true }, roof_parts: [{ ring_meters: ring, z: 4 }], roof_lines: [{ line_meters: [[10, 50], [90, 50]], kind: "ridge" }], roof_geometry_source: "image-guided-prior", roof_annotation: { schema_version: 1, vertices: [[10, 50, 4], [90, 50, 4]], edges: [], faces: [] }, roof_line_edits: { outline: { ring, holes }, vertices: [[10, 50], [90, 50]], edges: [{ a: 0, b: 1, kind: "ridge" }], checked: true }, custom_analysis: { untouched: true } };
const model = { model_signature: "bound", image_sha256: "image", image_width: 100, image_height: 100, pixel_to_meters: [[1, 0], [0, 1], [0, 0]], meters_to_pixel: [[1, 0], [0, 1], [0, 0]], objects: [original] };
const item = { id: "item", status: "unreviewed", annotations: [{ id: original.id, labelId: "building", geometry: objectGeometry(original, model), properties: { addr: { objectId: original.id, layer: "review" }, custom: "preserved" } }] };

test("untouched source state, holes, machine provenance and unknown analysis remain exact", () => {
  const result = sceneObjects(item, model)[0];
  assert.deepEqual(result, original);
  assert.equal(result.ring, original.ring);
  assert.equal(result.holes, original.holes);
  assert.equal(item.annotations[0].geometry.coordinates.length, 2);
});
test("Go wire key order and a notes-only edit preserve exact source roof geometry and optional holes", () => {
  for (const sourceObject of [original, { ...original, holes: undefined }]) {
    const sourceModel = { ...model, objects: [sourceObject] };
    const geometry = objectGeometry(sourceObject, sourceModel);
    const wireGeometry = JSON.parse(JSON.stringify({ coordinates: geometry.coordinates, type: geometry.type }));
    const wireItem = { ...item, annotations: [{ ...item.annotations[0], geometry: wireGeometry }] };
    const effective = sceneObjects(wireItem, sourceModel)[0];
    assert.deepEqual(effective, sourceObject, "wire key order cannot mutate the source outline");
    assert.equal(effective.ring, sourceObject.ring);
    assert.equal(effective.holes, sourceObject.holes);
    const edited = updateSceneObject(wireItem, sourceObject, { ...effective, notes: "Notes only" }, sourceModel);
    assert.deepEqual(edited.annotations[0].properties.addr.overrides, { notes: "Notes only" });
    assert.deepEqual(edited.annotations[0].properties.addr.removedFields, []);
    const after = sceneObjects(edited, sourceModel)[0];
    assert.deepEqual(after.roof_parts, sourceObject.roof_parts);
    assert.deepEqual(after.roof_annotation, sourceObject.roof_annotation);
    assert.equal(after.geometry_reviewed, true);
    assert.equal(after.model.height_verified, true);
    assert.equal(after.holes, sourceObject.holes);
  }
});
test("outline change invalidates derived roof; subsequent label edit does not resurrect it", () => {
  const first = updateSceneObject(item, original, { ...original, ring: [[9, 10], ...ring.slice(1)], status: "needs_adjustment" }, model);
  const changed = sceneObjects(first, model)[0];
  assert.equal(changed.roof_parts, undefined);
  assert.equal(changed.roof_lines, undefined);
  assert.equal(changed.roof_annotation, null);
  assert.equal(changed.geometry_reviewed, false);
  assert.equal(changed.model.height_verified, false);
  assert.deepEqual(changed.roof_line_edits.outline, { ring, holes }, "old line feedback must retain its original binding");
  const second = updateSceneObject(first, original, { ...changed, label: "Edited name", notes: "Check eave" }, model);
  const after = sceneObjects(second, model)[0];
  assert.equal(after.roof_parts, undefined);
  assert.equal(after.roof_lines, undefined);
  assert.equal(after.roof_geometry_source, undefined);
  assert.equal(after.label, "Edited name");
  assert.deepEqual(after.holes, holes);
  assert.deepEqual(after.custom_analysis, original.custom_analysis);
  assert.equal(second.annotations[0].properties.custom, "preserved");
  assert.equal(original.geometry_reviewed, true, "immutable source is untouched");
});
test("new Fieldwork object stays in the separate layer through later edits", () => {
  const object = { ...original, id: "fieldwork-new", source: "fieldwork-user-feedback" };
  const first = updateSceneObject({ ...item, annotations: [] }, { id: object.id }, object, model);
  const effective = sceneObjects(first, model).find((entry) => entry.id === object.id);
  const next = updateSceneObject(first, { id: object.id }, { ...effective, notes: "New object" }, model);
  assert.equal(sceneObjects(next, model).find((entry) => entry.id === object.id).notes, "New object");
});
test("API object notes, explicit empty clear and class overrides appear without changing source or roofs", () => {
  const baseline = { ...original, notes: "Source note" };
  const frame = { ...model, objects: [baseline] };
  for (const notes of ["Saved through API", ""]) {
    const edited = structuredClone(item);
    edited.annotations[0].properties.addr.overrides = { notes };
    const after = sceneObjects(edited, frame)[0];
    assert.equal(after.notes, notes);
    assert.equal(after.roof_parts, baseline.roof_parts);
    assert.equal(after.geometry_reviewed, true);
    assert.equal(after.holes, baseline.holes);
    assert.equal(baseline.notes, "Source note");
  }
  const reclassified = structuredClone(item);
  reclassified.annotations[0].properties.addr.overrides = { kind: "tree", status: "needs_adjustment" };
  assert.equal(sceneObjects(reclassified, model)[0].kind, "tree");
  assert.equal(reclassified.annotations[0].labelId, "building", "labelId records the source class");
});
test("API normalized geometry controls existing and new objects even with stale pixel-ring overrides", () => {
  const edited = structuredClone(item);
  const changed = { ...original, ring: [[9, 10], ...ring.slice(1)] };
  edited.annotations[0].geometry = objectGeometry(changed, model);
  edited.annotations[0].properties.addr.overrides = { ring: original.ring, holes: [], notes: "Changed via API" };
  const after = sceneObjects(edited, model)[0];
  assert.deepEqual(after.ring, changed.ring);
  assert.deepEqual(after.holes, holes);
  assert.ok(after.model.area_m2 > 0);
  edited.annotations[0].geometry = objectGeometry(original, model);
  edited.annotations[0].properties.addr.overrides.ring = changed.ring;
  assert.equal(sceneObjects(edited, model)[0].ring, original.ring, "restored API geometry wins over stale pixel overrides");
  edited.annotations[0].id = "new-api-object";
  edited.annotations[0].properties.addr.objectId = "new-api-object";
  edited.annotations[0].properties.addr.overrides = { ...original, ring: undefined };
  edited.annotations[0].geometry = objectGeometry(changed, model);
  const created = sceneObjects(edited, model).find((entry) => entry.id === "new-api-object");
  assert.deepEqual(created.ring, changed.ring);
  assert.deepEqual(created.holes, holes);
});
test("API-authored normalized coordinates stay bit-exact through notes and height edits", () => {
  const frame = { ...model, image_width: 1536, image_height: 1024 };
  const x = 0.0016998300169983002;
  assert.notEqual(x * frame.image_width / frame.image_width, x, "fixture must expose round-trip drift");
  const geometry = { type: "Polygon", coordinates: [
    [[x, 0.01], [0.8, 0.01], [0.8, 0.8], [x, 0.8], [x, 0.01]],
    [[0.3, 0.3], [0.4, 0.3], [0.4, 0.4], [0.3, 0.4], [0.3, 0.3]],
  ] };
  for (const newObject of [false, true]) {
    const edited = structuredClone(item);
    const mark = edited.annotations[0];
    mark.geometry = structuredClone(geometry);
    if (newObject) {
      mark.id = "api-added-roof";
      mark.properties.addr = { objectId: mark.id, overrides: structuredClone(original) };
    }
    const baseline = newObject ? { id: mark.id } : original;
    const effective = sceneObjects(edited, frame).find((object) => object.id === mark.id);
    const notes = updateSceneObject(edited, baseline, { ...structuredClone(effective), notes: "Notes only" }, frame);
    assert.deepEqual(notes.annotations[0].geometry, geometry);
    const afterNotes = sceneObjects(notes, frame).find((object) => object.id === mark.id);
    assert.equal(afterNotes.geometry_reviewed, true);
    assert.equal(afterNotes.model.height_verified, true);
    assert.deepEqual(afterNotes.roof_parts, effective.roof_parts);
    assert.deepEqual(afterNotes.roof_annotation, effective.roof_annotation);
    const height = updateSceneObject(notes, baseline, { ...afterNotes, model: { ...afterNotes.model, height_m: 5 } }, frame);
    assert.deepEqual(height.annotations[0].geometry, geometry);
    assert.equal(sceneObjects(height, frame).find((object) => object.id === mark.id).model.height_m, 5);

    const moved = structuredClone(afterNotes);
    moved.holes[0][0][0] += 1e-10;
    const outline = updateSceneObject(notes, baseline, moved, frame);
    assert.notDeepEqual(outline.annotations[0].geometry, geometry, "even a tiny actual hole edit must persist");
    const afterOutline = sceneObjects(outline, frame).find((object) => object.id === mark.id);
    assert.equal(afterOutline.geometry_reviewed, false);
    assert.equal(afterOutline.model.height_verified, false);
    assert.equal(afterOutline.roof_parts, undefined);
  }
});
test("frame comparisons reject image identity or metric transform changes", () => {
  assert.equal(sameSceneFrame(model, structuredClone(model)), true);
  assert.equal(sameSceneFrame(model, { ...model, image_sha256: "different" }), false);
  assert.equal(sameSceneFrame(model, { ...model, pixel_to_meters: [[2, 0], [0, 1], [0, 0]] }), false);
});
test("outlinePendingFor reports only unsaved ring or hole overrides for the named object", () => {
  assert.equal(outlinePendingFor(item, item, "roof-1"), false);
  assert.equal(outlinePendingFor(item, undefined, "roof-1"), false, "no overrides on either side");
  assert.equal(outlinePendingFor({ ...item, annotations: [] }, item, "roof-1"), false, "a missing mark counts as no override");
  const notes = updateSceneObject(item, original, { ...original, notes: "Check eave" }, model);
  assert.equal(outlinePendingFor(notes, item, "roof-1"), false, "notes-only edits never make an outline pending");
  const moved = updateSceneObject(item, original, { ...original, ring: [[9, 10], ...ring.slice(1)] }, model);
  assert.equal(outlinePendingFor(moved, item, "roof-1"), true);
  assert.equal(outlinePendingFor(moved, undefined, "roof-1"), true);
  assert.equal(outlinePendingFor(moved, item, "other-object"), false, "another object's outline is not pending");
  assert.equal(outlinePendingFor(moved, structuredClone(moved), "roof-1"), false, "a saved copy carrying the same override clears it");
  // The decision bar: same outline, explicit status + geometry_reviewed. Not an outline change.
  const decided = updateSceneObject(moved, original, { ...sceneObjects(moved, model)[0], status: "confirmed", geometry_reviewed: true }, model);
  assert.equal(outlinePendingFor(decided, moved, "roof-1"), false, "a status decision on the same outline is not an outline change");
  assert.equal(sceneObjects(decided, model)[0].status, "confirmed");
  assert.equal(sceneObjects(decided, model)[0].geometry_reviewed, true, "the Fieldwork decision is kept in the Fieldwork layer");
  assert.deepEqual(sceneObjects(decided, model)[0].ring, [[9, 10], ...ring.slice(1)]);
  assert.equal(outlinePendingFor(item, moved, "roof-1"), true, "returning to the source ring after a saved override is pending again");
  const holed = updateSceneObject(item, original, { ...original, holes: [[[31, 30], [40, 30], [40, 40], [30, 40]]] }, model);
  assert.equal(outlinePendingFor(holed, item, "roof-1"), true, "hole edits count as outline edits");
});
test("insertVertex adds a corner after an index, keeps the ring closed and leaves holes and input alone", () => {
  const square = { type: "Polygon", coordinates: [[[.2, .2], [.8, .2], [.8, .8], [.2, .8], [.2, .2]], [[.4, .4], [.5, .4], [.5, .5], [.4, .4]]] };
  const added = insertVertex(square, 1, [.8, .5]);
  assert.deepEqual(vertices(added), [[.2, .2], [.8, .2], [.8, .5], [.8, .8], [.2, .8]]);
  assert.deepEqual(added.coordinates[0][0], added.coordinates[0][5], "ring stays closed");
  assert.deepEqual(added.coordinates[1], square.coordinates[1], "holes are untouched");
  assert.equal(square.coordinates[0].length, 5, "input is not mutated");
  const wrap = insertVertex(square, 3, [.2, .5]);
  assert.deepEqual(vertices(wrap)[4], [.2, .5], "the closing edge (last point back to the first) takes its new point at the end");
  const line = { type: "LineString", coordinates: [[0, 0], [1, 1]] };
  assert.deepEqual(insertVertex(line, 0, [.5, .5]).coordinates, [[0, 0], [.5, .5], [1, 1]]);
  assert.equal(insertVertex(line, 1, [2, 2]), line, "a line has no edge after its last point");
  assert.equal(insertVertex(square, 9, [0, 0]), square, "an out-of-range index is ignored");
});

test("moveEdge slides one side along its normal so a right-angle box stays right-angled", () => {
  const box = { type: "Polygon", coordinates: [[[.2, .2], [.8, .2], [.8, .8], [.2, .8], [.2, .2]]] };
  const moved = moveEdge(box, 1, [.1, .3]);
  assert.deepEqual(vertices(moved).map((p) => p.map((v) => Math.round(v * 1e9) / 1e9)), [[.2, .2], [.9, .2], [.9, .8], [.2, .8]],
    "the east side moves out by the normal part of the drag only");
  const closing = moveEdge(box, 3, [-.1, 0]);
  assert.deepEqual(vertices(closing).map((p) => p.map((v) => Math.round(v * 1e9) / 1e9)), [[.1, .2], [.8, .2], [.8, .8], [.1, .8]],
    "the closing edge moves its two end points");
  assert.deepEqual(closing.coordinates[0][0], closing.coordinates[0][4], "ring stays closed");
  assert.equal(moveEdge(box, 7, [.1, .1]), box, "an out-of-range index is ignored");
  assert.equal(moveEdge({ type: "Point", coordinates: [0, 0] }, 0, [.1, .1]).type, "Point");
});

test("removeVertex keeps the closing point, refuses below 3 points and yields a valid polygon", () => {
  const geometry = objectGeometry(original, model);
  const removed = removeVertex(geometry, 1);
  assert.equal(removed.type, "Polygon");
  assert.equal(removed.coordinates[0].length, 4, "3 points plus the closing point");
  assert.deepEqual(removed.coordinates[0][0], removed.coordinates[0][3], "ring stays closed");
  assert.deepEqual(vertices(removed), [[.1, .1], [.9, .9], [.1, .9]]);
  assert.deepEqual(removed.coordinates[1], geometry.coordinates[1], "holes are untouched");
  assert.equal(geometry.coordinates[0].length, 5, "input is not mutated");
  assert.equal(removeVertex(removed, 0), removed, "a triangle keeps its 3 points (same reference)");
  assert.equal(removeVertex(geometry, 7), geometry, "an out-of-range index is ignored");
  assert.equal(removeVertex({ type: "Point", coordinates: [0, 0] }, 0).type, "Point");
  const pentagon = { type: "Polygon", coordinates: [[[.25, .25], [.75, .25], [.75, .75], [.5, .875], [.25, .75], [.25, .25]]] };
  const square = removeVertex(pentagon, 3);
  const rings = square.coordinates.map((entry) => entry.slice(0, -1).map(([x, y]) => [x * model.image_width, y * model.image_height]));
  assert.deepEqual(rings[0], [[25, 25], [75, 25], [75, 75], [25, 75]]);
  assert.deepEqual(polygonIssues(rings[0], model.image_width, model.image_height, rings.slice(1)), []);
});

// ---- Review-queue helpers (9 Oct 2026) ----
// A review item like the 8 Oct queue: a CURRENT outline and an agent PROPOSED outline, statuses on the source objects.
function reviewItem(currentStatus, extra = []) {
  const current = { ...original, id: "cur-1", kind: "other", label: "CURRENT saved outline", status: currentStatus, geometry_reviewed: false, source: "sam3-text-onnx" };
  const proposed = { ...original, id: "new-1", kind: "tree", label: "PROPOSED trim · line 002", status: "candidate", geometry_reviewed: false, source: "agent-proposal:shadow-trim-v1" };
  const objects = [current, proposed, ...extra];
  const frame = { ...model, objects };
  const labels = { "cur-1": "mooroolbark-review-current", "new-1": "mooroolbark-review-proposed" };
  const document = { id: "r1008-1-shadow-002", projectId: "mooroolbark-review", status: "unreviewed", annotations: objects.map((object) => ({ id: object.id, labelId: labels[object.id] || "mooroolbark-review-veg-tree", geometry: objectGeometry(object, frame), properties: { addr: { objectId: object.id, layer: "review-proposal", status: object.status, source: object.source } } })) };
  return { document, frame, objects };
}

test("the focus outline is the review item's one agent proposal, whatever its status; never CURRENT or a PIECE; none elsewhere", () => {
  const confirmedCurrent = reviewItem("confirmed");
  assert.equal(focusObjectId(confirmedCurrent.document, sceneObjects(confirmedCurrent.document, confirmedCurrent.frame)), "new-1");
  const bothCandidates = reviewItem("candidate");
  assert.equal(focusObjectId(bothCandidates.document, sceneObjects(bothCandidates.document, bothCandidates.frame)), "new-1", "the agent proposal, not the candidate CURRENT");
  const twoProposals = reviewItem("candidate", [{ ...original, id: "new-2", status: "candidate", source: "agent-proposal:roof-join-run-v2" }]);
  assert.equal(focusObjectId(twoProposals.document, sceneObjects(twoProposals.document, twoProposals.frame)), undefined, "two proposals: no guess");
  const noProposal = reviewItem("candidate"); noProposal.frame.objects[1].source = "sam3-text-onnx";
  assert.equal(focusObjectId(noProposal.document, sceneObjects(noProposal.document, noProposal.frame)), undefined, "no proposal: CURRENT is never selected");
  // Revisit: once PROPOSED has an answer (any of the three), it is still the focus, so the bar shows and changes that answer; the
  // candidate CURRENT is never selected (answering it would replace the earlier answer with an unread change).
  for (const current of ["candidate", "confirmed"]) {
    const base = reviewItem(current);
    for (const decision of ["right", "partly", "wrong"]) {
      const answered = decideSceneObject(base.document, base.objects[1], sceneObjects(base.document, base.frame)[1], decision, base.frame);
      assert.equal(focusObjectId(answered, sceneObjects(answered, base.frame)), "new-1", `${current} CURRENT, PROPOSED answered ${decision}`);
    }
  }
  // A join item: two PIECEs and the JOINED proposal.
  const pieces = [0, 1].map((index) => ({ ...original, id: `piece-${index}`, status: "candidate", source: "sam3-text-onnx" }));
  const join = reviewItem("candidate", pieces); join.frame.objects[0].source = "sam3-text-onnx";
  assert.equal(focusObjectId(join.document, sceneObjects(join.document, join.frame)), "new-1");
  // Main projects select nothing on open (the original behaviour).
  const main = { ...confirmedCurrent.document, projectId: "mooroolbark" };
  assert.equal(focusObjectId(main, sceneObjects(main, confirmedCurrent.frame)), undefined);
  // Vegetation items select nothing: every outline is answered on its own.
  const vegItem = { ...bothCandidates.document, metadata: { addr: { family: "veg" } } };
  assert.equal(focusObjectId(vegItem, sceneObjects(vegItem, bothCandidates.frame)), undefined);
  assert.equal(focusObjectId({ ...item, annotations: [] }, []), undefined);
});

test("decision-bar actions write status and geometry_reviewed overrides only, one draft each, outline kept", () => {
  assert.deepEqual(SCENE_DECISIONS, { right: { status: "confirmed", geometry_reviewed: true }, partly: { status: "needs_adjustment", geometry_reviewed: false }, wrong: { status: "rejected", geometry_reviewed: false } });
  const { document, frame, objects } = reviewItem("candidate");
  for (const [decision, expected] of Object.entries(SCENE_DECISIONS)) {
    const decided = decideSceneObject(document, objects[1], sceneObjects(document, frame)[1], decision, frame);
    const mark = decided.annotations.find((entry) => entry.id === "new-1");
    // updateSceneObject nulls roof_annotation on every edit of a non-building object; the pull reads a null override as no change.
    const { roof_annotation: nulled, ...overrides } = mark.properties.addr.overrides;
    assert.equal(nulled, null);
    assert.deepEqual(overrides, decision === "partly" ? { status: "needs_adjustment" } : decision === "wrong" ? { status: "rejected" } : { status: "confirmed", geometry_reviewed: true }, `${decision}: only fields that differ from the source are written`);
    assert.equal(sceneObjects(decided, frame)[1].status, expected.status);
    assert.equal(sceneObjects(decided, frame)[1].geometry_reviewed, expected.geometry_reviewed);
    assert.deepEqual(mark.geometry, document.annotations[1].geometry, "the outline is untouched");
    assert.equal(mark.labelId, "mooroolbark-review-proposed", "labelId is immutable");
    assert.deepEqual(mark.properties.addr.status, "candidate", "the immutable source binding is untouched");
    assert.equal(decided.annotations[0], document.annotations[0], "other outlines are untouched");
    assert.equal(decided.status, "in_review");
  }
  // An edited outline keeps its ring through the decision.
  const moved = updateSceneObject(item, original, { ...original, ring: [[9, 10], ...ring.slice(1)] }, model);
  const decided = decideSceneObject(moved, original, sceneObjects(moved, model)[0], "right", model);
  assert.deepEqual(sceneObjects(decided, model)[0].ring, [[9, 10], ...ring.slice(1)]);
  assert.equal(outlinePendingFor(decided, moved, "roof-1"), false);
});

test("an explicit item status survives object edits in a review queue; main projects keep the original reset to in review", () => {
  assert.equal(editedItemStatus("unreviewed"), "in_review");
  for (const status of ["in_review", "approved", "rejected"]) assert.equal(editedItemStatus(status), status);
  for (const status of ["unreviewed", "in_review", "approved", "rejected"]) assert.equal(editedItemStatus(status, false), "in_review");
  const mainItem = { ...item, status: "approved" };
  assert.equal(updateSceneObject(mainItem, original, { ...original, notes: "n" }, model).status, "in_review", "a main-project edit puts the item back in review, as before");
  const { document, frame, objects } = reviewItem("confirmed");
  const approved = { ...document, status: "approved" };
  assert.equal(decideSceneObject(approved, objects[1], sceneObjects(approved, frame)[1], "wrong", frame).status, "approved", "the reviewer's item answer is kept; the pull lists the disagreement");
});

test("colour mode: stored choice wins, review queues default to label, main projects to class", () => {
  assert.equal(isReviewProject("mooroolbark-review"), true);
  assert.equal(isReviewProject("mooroolbark"), false);
  assert.equal(isReviewProject(""), false);
  for (const spelling of ["Mooroolbark-REVIEW", " mooroolbark-review ", "mooroolbark－review", "mooroolbark\u2010review", "mooroolbark-re\u200bview"]) assert.equal(isReviewProject(spelling), true, JSON.stringify(spelling));
  assert.equal(normalizeProjectId(" ＭＯＯＲＯＯＬＢＡＲＫ\u2212review\u00ad "), "mooroolbark-review");
  assert.equal(sceneColourMode("mooroolbark-review", null), "label");
  assert.equal(sceneColourMode("mooroolbark", null), "class");
  assert.equal(sceneColourMode("mooroolbark", "label"), "label");
  assert.equal(sceneColourMode("mooroolbark-review", "class"), "class");
  assert.equal(sceneColourMode("mooroolbark", "garbage"), "class");
  assert.equal(sceneColourKey("mooroolbark-review"), "fieldwork.sceneColour.mooroolbark-review");
  const { document } = reviewItem("confirmed");
  assert.deepEqual([...objectLabelIds(document)], [["cur-1", "mooroolbark-review-current"], ["new-1", "mooroolbark-review-proposed"]]);
});

test("vegetation picker words: the Label states the type with the outline number, matches the pull's word rule, and changes nothing else", () => {
  // scripts/ml/fieldwork_review.py VEG_LABEL, copied: the pull reads these Label words.
  const pull = /^\s*(grass|hedge|shrub|tree)\s*(?:[·.:\-]?\s*\d{1,4})?\s*$/i;
  assert.equal(vegetationLabel("grass? · 0001", "tree"), "tree · 0001");
  assert.equal(vegetationLabel("tree or shrub? · 0115", "shrub"), "shrub · 0115");
  assert.equal(vegetationLabel("Vegetation", "grass"), "grass");
  for (const label of ["tree · 0001", "shrub · 0115", "grass"]) assert.match(label, pull);
  assert.equal(vegetationLabelType("tree · 0001"), "tree");
  assert.equal(vegetationLabelType("grass? · 0001"), undefined, "a proposal is not an answer");
  assert.equal(isVegetationLabel("grass? · 0001"), true);
  assert.equal(isVegetationLabel("type? · 0430"), true);
  assert.equal(isVegetationLabel("tree or shrub? · 0003"), true);
  assert.equal(isVegetationLabel("hedge · 0009"), true);
  assert.equal(isVegetationLabel("PROPOSED trim · line 002"), false, "shadow and cut proposals carry no type picker");
  const veg = { ...original, id: "3d57525cdf857d61", kind: "vegetation", label: "grass? · 0001", status: "candidate", geometry_reviewed: false, source: "sam3-text-onnx" };
  const frame = { ...model, objects: [veg] };
  const document = { id: "veg", projectId: "mooroolbark-review", status: "unreviewed", annotations: [{ id: veg.id, labelId: "mooroolbark-review-veg-grass", geometry: objectGeometry(veg, frame), properties: { addr: { objectId: veg.id, status: "candidate" } } }] };
  const effective = sceneObjects(document, frame)[0];
  const typed = updateSceneObject(document, veg, { ...effective, label: vegetationLabel(effective.label, "tree") }, frame);
  assert.deepEqual(typed.annotations[0].properties.addr.overrides, { label: "tree · 0001", roof_annotation: null }, "the picker writes properties.addr.overrides.label (plus the non-building roof_annotation null) only");
  assert.equal(sceneObjects(typed, frame)[0].status, "candidate", "the picker never changes status");
  assert.equal(sceneObjects(typed, frame)[0].vegetation_type, undefined, "no vegetation_type: feedback.ts does not map it");
  const cleared = updateSceneObject(typed, veg, { ...sceneObjects(typed, frame)[0], label: veg.label }, frame);
  assert.deepEqual(cleared.annotations[0].properties.addr.overrides, { roof_annotation: null }, "restoring the proposal label removes the label override");
});

test("in a many-outline item the decision bar moves to the next open outline, wrapping, skipping answered ones", () => {
  const outlines = Array.from({ length: 10 }, (_, index) => ({ ...original, id: `v${index}`, kind: "vegetation", label: `grass? · 000${index}`, status: "candidate", geometry_reviewed: false, source: "sam3" }));
  const frame = { ...model, objects: outlines };
  const document = { id: "veg", projectId: "mooroolbark-review", status: "unreviewed", annotations: outlines.map((object) => ({ id: object.id, labelId: "mooroolbark-review-veg-grass", geometry: objectGeometry(object, frame), properties: { addr: { objectId: object.id } } })) };
  let objects = sceneObjects(document, frame);
  assert.equal(nextOpenObjectId(document, objects, "v3"), "v4");
  assert.equal(nextOpenObjectId(document, objects, "v9"), "v0", "wraps to the start");
  assert.equal(nextOpenObjectId(document, objects, "missing"), "v0");
  let decided = decideSceneObject(document, outlines[4], objects[4], "right", frame);
  decided = updateSceneObject(decided, outlines[5], { ...sceneObjects(decided, frame)[5], label: vegetationLabel(outlines[5].label, "tree") }, frame);
  objects = sceneObjects(decided, frame);
  assert.equal(nextOpenObjectId(decided, objects, "v3"), "v6", "a decided outline and a typed outline are skipped");
  const onlyOne = outlines.map((object, index) => index === 2 ? object : { ...object, status: "confirmed" });
  assert.equal(nextOpenObjectId(document, sceneObjects(document, { ...frame, objects: onlyOne }), "v2"), undefined, "nothing open: the item is done");
  const { document: small, frame: smallFrame } = reviewItem("candidate");
  assert.equal(nextOpenObjectId(small, sceneObjects(small, smallFrame), "new-1"), undefined, "shadow, cut and join items move to the next item");
  // By family, not by count: a vegetation item with only 3 outlines still moves outline by outline; a main project never does.
  const three = { ...document, metadata: { addr: { family: "veg" } }, annotations: document.annotations.slice(0, 3) };
  const threeObjects = sceneObjects(three, { ...frame, objects: outlines.slice(0, 3) });
  assert.equal(isVegetationItem(three, threeObjects), true);
  assert.equal(nextOpenObjectId(three, threeObjects, "v0"), "v1");
  assert.equal(nextOpenObjectId({ ...document, projectId: "mooroolbark" }, objects, "v3"), undefined);
  assert.equal(reviewFamily({ metadata: { addr: { family: "cut" } } }), "cut");
  assert.equal(reviewFamily({ metadata: {} }), undefined);
});

test("vegetation: Right alone stores nothing on 'tree or shrub?' and 'type?' outlines until a type word is picked", () => {
  assert.equal(vegetationProposal("tree or shrub? · 0115"), "tree or shrub");
  assert.equal(vegetationProposal("type? · 0430"), "type");
  assert.equal(vegetationProposal("grass? · 0001"), "grass");
  assert.equal(vegetationProposal("tree · 0001"), undefined);
  assert.equal(vegetationNeedsType("type? · 0430", "type? · 0430"), true);
  assert.equal(vegetationNeedsType("tree or shrub? · 0115", "tree or shrub? · 0115"), true);
  assert.equal(vegetationNeedsType("tree or shrub? · 0115", "shrub · 0115"), false, "a picked type answers it");
  assert.equal(vegetationNeedsType("grass? · 0001", "grass? · 0001"), false, "Right keeps the proposed grass");
});

test("a handle released where it was picked up is not an outline edit", () => {
  const geometry = objectGeometry(original, model);
  const noisy = { ...geometry, coordinates: geometry.coordinates.map((ring) => ring.map(([x, y]) => [x + 1e-12, y - 1e-12])) };
  assert.equal(nearlySameGeometry(geometry, noisy), true);
  const moved = { ...geometry, coordinates: [[[0.11, 0.1], ...geometry.coordinates[0].slice(1)], ...geometry.coordinates.slice(1)] };
  assert.equal(nearlySameGeometry(geometry, moved), false);
  assert.equal(nearlySameGeometry(undefined, geometry), false);
});
