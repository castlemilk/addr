// GET /api/scenes/hints over a seeded temporary eyes index: id validation (400), no row of any kind (404), both
// hint versions and the decision-policy-v1 decision in one body, malformed payloads degrading to null, a missing
// decision_policy table leaving the other hints intact, an unbuilt index or a Node without node:sqlite answering
// the same shape with source "unavailable", a corrupt index answering 500 without local details, GET-only,
// force-dynamic and no-store. Then static pins on the scene editor's "Use machine label" button. Nothing here
// writes to the real index.
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const transpile = file => ts.transpileModule(readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const queues = {};
new Function('exports', 'require', transpile('src/lib/queues.ts'))(queues, id => require(id));
// The route joins EYES_INDEX onto process.cwd(); each load points it at a different temp file (repo-relative).
const load = indexFile => {
  const exports = {};
  new Function('exports', 'require', transpile('src/app/api/scenes/hints/route.ts'))(exports, id => {
    if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
    if (id === '@/lib/queues') return { ...queues, EYES_INDEX: path.relative(process.cwd(), indexFile) };
    return require(id);
  });
  return exports;
};
const request = params => ({ nextUrl: { searchParams: new URLSearchParams(params) } });
const clean = async response => {
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const text = await response.text();
  assert(!text.includes('/Users') && !text.includes(tmpdir()) && !text.includes('SQLITE') && !text.includes('Traceback') && !text.includes('ENOENT'), `no local details in ${text.slice(0, 200)}`);
  return JSON.parse(text);
};

// --- Seed: one object with both rows, one with class hints only, one with structure only, malformed rows ------
const { DatabaseSync } = process.getBuiltinModule('node:sqlite');
const folder = mkdtempSync(`${tmpdir()}/scene-hints-`);
const indexFile = `${folder}/eyes.sqlite`;
const seed = new DatabaseSync(indexFile);
seed.exec('CREATE TABLE object_quality(scene_id TEXT, object_id TEXT, model_signature TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, model_signature, version))');
const insert = seed.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)');
const SCENE = 'mooroolbark-50011498-17-118440-80408';
const flagsPayload = { kind: 'building', area_m2: 63.379, version: 'building-flags-v2', class_hints: { version: 'building-flags-v2', note: 'agent hint, not a label', class_hint: 'outbuilding',
  flags: [{ flag: 'secondary-structure', evidence: { area_m2: 63.379, largest_same_property_area: 151.325, ratio: 2.3876, source: 'vicmap-property', property_pfi: '1348074', rule: 'r' } }] } };
const structurePayload = { version: 'structure-kinds-v1', note: 'agent hint, not a label', kind: 'building', area_m2: 14.9, sub_kind: 'shipping-container', confidence: 'moderate', rule: 'aspect >= 2.2',
  evidence: { geometry: { area_m2: 14.9, rect_width_m: 2.4, rect_length_m: 6.1, aspect: 2.54, rectilinearity: 0.96, circularity: 0.62, vertex_count: 5 },
    appearance: { brightness_median: 141, saturation_median: 22, rib_anisotropy: 0.42, rib_period_m: 0.28, rib_axis_alignment: 0.91, interior_relative_variance: 0.08 },
    context: { attached_to_larger_roof: false, distance_to_largest_roof_m: 9.4, largest_same_property_area: 151.3, secondary_structure_flag: true, touches_image_edge: false },
    sam3: { prompts: { house: { best_iou: 0.31, score: 0.12 } }, image: 'original' } }, computed_at: '2026-09-26T09:00:00+00:00', code_sha256: 'd'.repeat(64) };
insert.run(SCENE, '7fdd138f819cb06c', 'sig', 'building-flags-v2', JSON.stringify(flagsPayload));
insert.run(SCENE, '7fdd138f819cb06c', 'sig', 'structure-kinds-v1', JSON.stringify(structurePayload));
insert.run(SCENE, '7fdd138f819cb06c', 'sig', 'object-quality-v1', JSON.stringify({ kind: 'building', score: 0.5 }));
insert.run(SCENE, 'flags-only', 'sig', 'building-flags-v2', JSON.stringify({ kind: 'building', class_hints: { class_hint: 'house', flags: [] } }));
insert.run(SCENE, 'structure-only', 'sig', 'structure-kinds-v1', JSON.stringify({ ...structurePayload, sub_kind: 'fragment', confidence: 'weak' }));
insert.run(SCENE, 'broken', 'sig', 'building-flags-v2', '{not json');
insert.run(SCENE, 'broken', 'sig', 'structure-kinds-v1', JSON.stringify({ sub_kind: 'zebra', confidence: 'strong' }));
insert.run(SCENE, 'half-broken', 'sig', 'building-flags-v2', JSON.stringify(flagsPayload));
insert.run(SCENE, 'half-broken', 'sig', 'structure-kinds-v1', 'not json at all');
insert.run(SCENE, 'quality-only', 'sig', 'object-quality-v1', JSON.stringify({ kind: 'tree' }));
insert.run(SCENE, 'broken-decision', 'sig', 'building-flags-v2', JSON.stringify(flagsPayload));
// object-hints-v2 (scene_object_hints) and lot-tags-v1 (scene_lot_tags) rows: measured hints, never labels. 'squared' holds rows under two
// model signatures (an earlier image version and the current one) so the signature parameter can be seen choosing, plus an object-hints-v1
// row the v2 rows shadow; 'hints-v1-only' has nothing but an object-hints-v1 row (served as the fallback, stale under a named signature).
const SIG_OLD = '1'.repeat(64), SIG_NEW = '2'.repeat(64);
const hintsOld = { version: 'object-hints-v2', note: 'agent hint, not a label', kind: 'building', hints: [{ code: 'frame_cut', severity: 'review', sentence: 'Cut by the earlier 512 px frame; nothing replaced it when the scene was widened.', evidence: { clipped_by_frame: [256, 256, 768, 768] } }],
  squared_candidate: { refused: 'iou 0.79 below 0.85' }, code_sha256: 'a'.repeat(64), computed_at: '2026-09-30T08:00:00+00:00' };
const hintsNew = { ...hintsOld, hints: [{ code: 'noisy_outline', severity: 'review', sentence: "54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side.", evidence: { vertices: 54, off_axis_edges: 49, vertices_per_metre: 0.489, wonky_share: 0.907, orthogonalise: 'passed' } }],
  squared_candidate: { ring: [[10, 10], [110, 12], [108, 90], [9, 88]], iou: 0.91, hausdorff_m: 0.6 } };
insert.run(SCENE, 'squared', SIG_OLD, 'object-hints-v2', JSON.stringify(hintsOld));
insert.run(SCENE, 'squared', SIG_NEW, 'object-hints-v2', JSON.stringify(hintsNew));
const hintsV1 = { ...hintsOld, version: 'object-hints-v1' };
insert.run(SCENE, 'squared', SIG_NEW, 'object-hints-v1', JSON.stringify({ ...hintsV1, hints: [{ code: 'pool_not_blue', severity: 'review', sentence: 'Shadowed by the v2 row.', evidence: {} }] }));
insert.run(SCENE, 'hints-v1-only', SIG_NEW, 'object-hints-v1', JSON.stringify(hintsV1));
insert.run(SCENE, 'hints-v1-old-sig', SIG_OLD, 'object-hints-v1', JSON.stringify(hintsV1));  // both reasons at once: an older version under an older signature
insert.run(SCENE, 'squared', SIG_NEW, 'lot-tags-v1', JSON.stringify({ version: 'lot-tags-v1', note: 'agent hint, not a label', tag: { dominant: { pfi: '99', lot: '7', plan: 'LP34966', share: 0.97, kind: 'lot' }, lots: [{ lot: '7', plan: 'LP34966', share: 0.97 }], flags: ['lot_unassociated'], sentences: ['On neighbouring Lot 7 LP34966, not the address lot; cut by the frame.'], lot_complete_in_image: false, address_plan: 'PS824596' }, parcels_sha256: 'b'.repeat(64) }));
// The scene-level lot-tags-v1 row (object_id '_scene'): the lot-frame plan sentence for the whole scene, text only.
const LOT_FRAME = 'A lot frame for 30371 would be 88 m at 0.17 m/px (Google z20, 512 px), covering Lots 1-4 + CM1 of PS824596; coarser than this 0.065 m/px crop.';
insert.run(SCENE, '_scene', SIG_NEW, 'lot-tags-v1', JSON.stringify({ version: 'lot-tags-v1', note: 'agent hint, not a label', scene: { address_id: 30371, address_plan: 'PS824596', unit_site: true, lot_frame: { version: 'lot-frame-v1', span_m: 87.85, m_per_px: 0.1716, group_kind: 'plan-block', sentence: LOT_FRAME } }, parcels_sha256: 'b'.repeat(64) }));
insert.run(SCENE, 'hints-only', 'sig', 'object-hints-v2', JSON.stringify({ ...hintsOld, hints: [{ code: 'pool_not_blue', severity: 'review', sentence: 'Model says pool; the interior reads green 100%, blue 0%, like grass. Check the image.', evidence: { green_share: 1, blue_share: 0 } }], squared_candidate: null }));
insert.run(SCENE, 'half-broken', 'sig', 'object-hints-v2', '{not json either');
insert.run(SCENE, 'flags-only', 'sig', 'object-hints-v2', JSON.stringify({ version: 'object-hints-v2', hints: 'not a list' }));
// decision_policy: the third table, written by the decision-policy module; version 'decision-policy-v1', payload as documented.
seed.exec('CREATE TABLE decision_policy(scene_id TEXT, object_id TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, version))');
const decisionPayload = { version: 'decision-policy-v1', note: 'machine label and assessment; auto tier proceeds without a human, assist and review need one', tier: 'assist', confidence: 0.73,
  machine_label: { kind: 'building', structure_sub_kind: 'shipping-container', class_hint: 'outbuilding', roof_form: 'flat', roof_type: 'flat' }, reasons: ['secondary structure on the property', 'ribbed roof'],
  signals: { area_m2: 14.9, rib_anisotropy: 0.42 }, thresholds_version: 'thresholds-v1', computed_at: '2026-09-26T10:00:00+00:00', code_sha256: 'e'.repeat(64) };
const decisionPayloadV2 = { ...decisionPayload, version: 'decision-policy-v2', tier: 'auto', confidence: 0.91,
  interval: { kind: 'wilson-95', stratum: 'auto/shipping-container', confirmed: 6, judged: 9, lower: 0.3536, upper: 0.8791, note: 'reviewer decisions in this stratum' },
  score_interval: { lower: 0.8612, upper: 0.9488, note: 'model estimate range' }, signals: { review: { status: null, present: false } } };
const decide = seed.prepare('INSERT INTO decision_policy VALUES (?, ?, ?, ?)');
decide.run(SCENE, '7fdd138f819cb06c', 'decision-policy-v1', JSON.stringify(decisionPayload));
decide.run(SCENE, 'upgraded', 'decision-policy-v1', JSON.stringify({ ...decisionPayload, tier: 'review' }));
decide.run(SCENE, 'upgraded', 'decision-policy-v2', JSON.stringify(decisionPayloadV2));
decide.run(SCENE, 'unjudged', 'decision-policy-v2', JSON.stringify({ ...decisionPayloadV2, interval: { ...decisionPayloadV2.interval, confirmed: 0, judged: 0, lower: null, upper: null }, signals: { review: { status: 'confirmed', present: true } } }));
decide.run(SCENE, 'decision-only', 'decision-policy-v1', JSON.stringify({ ...decisionPayload, tier: 'auto', confidence: 0.95, machine_label: { kind: 'building', structure_sub_kind: 'garage', class_hint: null, roof_form: 'pitched', roof_type: 'gabled' }, reasons: [] }));
decide.run(SCENE, 'broken-decision', 'decision-policy-v1', JSON.stringify({ ...decisionPayload, tier: 'maybe' }));
decide.run(SCENE, 'bad-tier-only', 'decision-policy-v1', JSON.stringify({ ...decisionPayload, confidence: 1.5 }));
decide.run(SCENE, 'flags-only', 'decision-policy-v0', JSON.stringify(decisionPayload));
seed.close();
// A second index built before the decision-policy module ran: object_quality only, no decision_policy table at all.
const olderFile = `${folder}/older/eyes.sqlite`;
mkdirSync(path.dirname(olderFile));
const older = new DatabaseSync(olderFile);
older.exec('CREATE TABLE object_quality(scene_id TEXT, object_id TEXT, model_signature TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, model_signature, version))');
older.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)').run(SCENE, '7fdd138f819cb06c', 'sig', 'building-flags-v2', JSON.stringify(flagsPayload));
older.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)').run(SCENE, '7fdd138f819cb06c', 'sig', 'structure-kinds-v1', JSON.stringify(structurePayload));
older.close();

const route = load(indexFile);
assert.equal(route.dynamic, 'force-dynamic');
assert.equal(typeof route.GET, 'function');
for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.equal(route[verb], undefined, `GET-only (${verb})`);

// --- Both rows ---------------------------------------------------------------------------------------------
let response = await route.GET(request({ scene: SCENE, object: '7fdd138f819cb06c' }));
assert.equal(response.status, 200);
let body = await clean(response);
assert.deepEqual(Object.keys(body).sort(), ['class_hints', 'decision', 'decision_version', 'hints_version', 'lot', 'lot_frame', 'lot_tags_version', 'note', 'object_hints', 'object_hints_served_version', 'object_hints_version', 'object_id', 'scene_id', 'source', 'stale', 'stale_reasons', 'structure', 'structure_version']);
assert.deepEqual(body.stale_reasons, [], 'nothing stale, no reason'); assert.equal(body.object_hints_served_version, null, 'no object-hints row served, no served version');
assert.equal(body.object_hints_version, 'object-hints-v2'); assert.equal(body.lot_tags_version, 'lot-tags-v1'); assert.equal(body.object_hints, null); assert.equal(body.lot, null); assert.equal(body.stale, false, 'no signature named, nothing stale');
assert.equal(body.scene_id, SCENE); assert.equal(body.object_id, '7fdd138f819cb06c');
assert.equal(body.hints_version, 'building-flags-v2'); assert.equal(body.structure_version, 'structure-kinds-v3'); assert.equal(body.decision_version, 'decision-policy-v2'); assert.equal(body.source, 'eyes-index');
assert.deepEqual(body.decision, { tier: 'assist', confidence: 0.73, machine_label: decisionPayload.machine_label, reasons: decisionPayload.reasons, interval: null, score_interval: null, review: null }, 'a v1 decision is read where no v2 row exists: tier, confidence, machine label and reasons, no intervals');
assert.match(body.note, /not labels?/i);
assert.deepEqual(body.class_hints, { hints: flagsPayload.class_hints.flags, class_hint: 'outbuilding' });
assert.deepEqual(body.structure, { sub_kind: 'shipping-container', confidence: 'moderate', rule: 'aspect >= 2.2', evidence: structurePayload.evidence });
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), 'Secondary structure. Shipping container (moderate). Not labels; change the Class above if you disagree. Machine label: shipping container, assist tier, confidence 0.73.', 'the editor line reads straight off the body, machine label last');
assert.equal(queues.machineLabelName(queues.coerceObjectHints(body).decision.machine_label), 'Shipping container', 'the label "Use machine label" would apply');
// --- One row of either version, or a decision alone --------------------------------------------------------
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'flags-only' })));
assert.equal(response.status, 200); assert.deepEqual(body.class_hints, { hints: [], class_hint: 'house' }); assert.equal(body.structure, null);
assert.equal(body.decision, null, 'a decision row of another version is not read');
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), null, 'a plain house has nothing for the editor to say');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'decision-only' })));
assert.equal(response.status, 200, 'a decision alone is a hint row, not a 404');
assert.equal(body.class_hints, null); assert.equal(body.structure, null);
assert.deepEqual(body.decision, { tier: 'auto', confidence: 0.95, machine_label: { kind: 'building', structure_sub_kind: 'garage', class_hint: null, roof_form: 'pitched', roof_type: 'gabled' }, reasons: [], interval: null, score_interval: null, review: null });
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), 'Machine label: garage, auto tier, confidence 0.95.', 'the editor line stands alone on a decision');
// --- v2 rows win over v1 rows and carry the intervals into the editor line -------------------------------------
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'upgraded' })));
assert.equal(response.status, 200); assert.equal(body.decision.tier, 'auto', 'the v2 row wins over the v1 row (review tier)');
assert.deepEqual(body.decision.interval, decisionPayloadV2.interval); assert.deepEqual(body.decision.score_interval, decisionPayloadV2.score_interval); assert.deepEqual(body.decision.review, { status: null });
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), 'Machine label: shipping container, auto tier, confidence 0.91 (model range 0.86 to 0.95). Human agreement for auto/shipping-container: 6 of 9 confirmed, 95% interval 0.35 to 0.88.', 'the editor line carries the model range and the agreement interval');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'unjudged' })));
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), 'Machine label: shipping container, auto tier, confidence 0.91 (model range 0.86 to 0.95). Human agreement for auto/shipping-container: not yet judged by a reviewer.', 'null bounds read as not yet judged');
assert.deepEqual(body.decision.review, { status: 'confirmed' });
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'structure-only' })));
assert.equal(response.status, 200); assert.equal(body.class_hints, null); assert.equal(body.structure.sub_kind, 'fragment'); assert.equal(body.structure.confidence, 'weak');
// --- Malformed payloads degrade to null; when both are unusable the object has no hints (404) -------------------
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'half-broken' })));
assert.equal(response.status, 200); assert.equal(body.class_hints.class_hint, 'outbuilding'); assert.equal(body.structure, null, 'an unparseable structure payload is null, not fatal');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'broken-decision' })));
assert.equal(response.status, 200); assert.equal(body.class_hints.class_hint, 'outbuilding'); assert.equal(body.decision, null, 'an unknown tier is null, not fatal');
for (const object of ['broken', 'quality-only', 'missing', 'bad-tier-only']) {
  response = await route.GET(request({ scene: SCENE, object }));
  assert.equal(response.status, 404, `${object}: neither usable row means 404`);
  body = await clean(response); assert.match(body.error, /no agent hints/i);
}
// --- object-hints-v2 and lot-tags-v1 rows, the signature parameter and staleness ---------------------------------------------------
assert.equal(body = await clean(response = await route.GET(request({ scene: SCENE, object: 'half-broken' }))), body); assert.equal(body.object_hints, null, 'an unparseable object-hints payload is null while the class hints stay'); assert.equal(body.class_hints.class_hint, 'outbuilding');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'flags-only' })));
assert.equal(body.object_hints, null, 'a payload whose hints are not a list is null'); assert.equal(body.class_hints.class_hint, 'house');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'hints-only' })));
assert.equal(response.status, 200, 'an object-hints row alone is a hint row, not a 404'); assert.equal(body.object_hints_served_version, 'object-hints-v2'); assert.equal(body.class_hints, null); assert.equal(body.structure, null); assert.equal(body.decision, null); assert.equal(body.lot, null);
assert.deepEqual(body.object_hints, { hints: [{ code: 'pool_not_blue', severity: 'review', sentence: 'Model says pool; the interior reads green 100%, blue 0%, like grass. Check the image.', evidence: { green_share: 1, blue_share: 0 } }], squared_candidate: null });
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), 'Model says pool; the interior reads green 100%, blue 0%, like grass. Check the image. Not labels; change the Class above if you disagree.', 'the editor line reads the object hint straight off the body');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'squared', signature: SIG_NEW })));
assert.equal(response.status, 200); assert.equal(body.stale, false, 'rows under the named signature are current'); assert.deepEqual(body.stale_reasons, []);
assert.deepEqual(body.object_hints.hints.map(h => h.code), ['noisy_outline'], 'the row computed under the named signature is served');
assert.deepEqual(body.object_hints.squared_candidate, { ring: [[10, 10], [110, 12], [108, 90], [9, 88]], iou: 0.91, hausdorff_m: 0.6, refused: null }, 'the squared candidate rides along with its agreement numbers');
assert.deepEqual(body.lot.dominant, { pfi: '99', lot: '7', plan: 'LP34966', share: 0.97, kind: 'lot' }); assert.deepEqual(body.lot.flags, ['lot_unassociated']); assert.equal(body.lot.lot_complete_in_image, false);
assert.equal(body.lot_frame, LOT_FRAME, "the scene row's lot-frame sentence rides along on every object answer of its scene");
// The scene row on its own: the one object id with a leading underscore; nulls elsewhere, never stale, and no rescue for unknown objects.
const sceneRowResponse = await route.GET(request({ scene: SCENE, object: '_scene', signature: SIG_OLD })), sceneRow = await clean(sceneRowResponse);
assert.equal(sceneRowResponse.status, 200, 'the scene row is reachable on its own'); assert.equal(sceneRow.lot_frame, LOT_FRAME); assert.equal(sceneRow.lot, null); assert.equal(sceneRow.object_hints, null); assert.equal(sceneRow.class_hints, null); assert.equal(sceneRow.structure, null);
assert.equal(sceneRow.stale, false, 'the scene row describes the frame, not an outline: it never judges staleness');
assert.equal((await route.GET(request({ scene: SCENE, object: '_other' }))).status, 400, 'only the scene row may start with an underscore');
assert.equal((await route.GET(request({ scene: 'no-such-scene', object: '_scene' }))).status, 404, 'a scene without the row has no lot-frame sentence');
assert.equal(queues.describeObjectHints(queues.coerceObjectHints(body)), "54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side. On neighbouring Lot 7 LP34966, not the address lot; cut by the frame. Not labels; change the Class above if you disagree.", 'object sentence then lot sentence');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'squared', signature: SIG_OLD })));
assert.equal(body.stale, true, 'the lot row exists only under the newer signature, so naming the older one serves a row from another version: stale');
assert.deepEqual(body.stale_reasons, ['model_signature'], 'an earlier image version, not earlier rules: the object hints came from a v2 row'); assert.equal(body.object_hints_served_version, 'object-hints-v2');
assert.deepEqual(body.object_hints.hints.map(h => h.code), ['frame_cut'], 'the object-hints row under the named (older) signature is served');
assert.deepEqual(body.object_hints.squared_candidate, { ring: null, iou: null, hausdorff_m: null, refused: 'iou 0.79 below 0.85' });
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'squared', signature: '3'.repeat(64) })));
assert.equal(body.stale, true, 'no row under the named signature: whatever is served is stale'); assert.deepEqual(body.stale_reasons, ['model_signature']); assert.equal(body.object_hints.hints.length, 1);
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'squared' })));
assert.equal(body.stale, false, 'without a signature the image version is not judged, and the v2 row is current rules'); assert.deepEqual(body.stale_reasons, []); assert.equal(body.object_hints.hints.length, 1, 'one row is served');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'squared', signature: 'not-hex' })));
assert.equal(response.status, 200); assert.equal(body.stale, false, 'a malformed signature is ignored, never an error');
// object-hints-v1 rows after the v2 bump: served where no v2 row exists and stale with the reason object_hints_version (named signature or not),
// the served version reported as the row's own; never an error or a 404.
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'hints-v1-only', signature: SIG_NEW })));
assert.equal(response.status, 200, 'an object with only an object-hints-v1 row still answers'); assert.equal(body.object_hints_version, 'object-hints-v2', 'the current rules version');
assert.equal(body.object_hints_served_version, 'object-hints-v1', 'the version of the row actually served');
assert.deepEqual(body.object_hints.hints.map(h => h.code), ['frame_cut'], 'the v1 row is served where no v2 row exists');
assert.equal(body.stale, true, 'a row of an older object-hints version predates the current rules: stale even under the current signature');
assert.deepEqual(body.stale_reasons, ['object_hints_version'], 'the rows sit under the named signature: only the rules are older');
assert.deepEqual(queues.hintStaleNotes(queues.coerceObjectHints(body), 'solar_panel'), ['Object hints predate the current rules (object-hints-v1): the solar-panel check is not included.'], 'the editor names the missing check on a solar panel');
assert.deepEqual(queues.hintStaleNotes(queues.coerceObjectHints(body), 'building'), [], 'and says nothing on a building, whose v1 hints are what v2 would say');
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'hints-v1-only' })));
assert.equal(body.stale, true, 'the rules are judged without a signature too'); assert.deepEqual(body.stale_reasons, ['object_hints_version']); assert.equal(body.object_hints_served_version, 'object-hints-v1'); assert.equal(body.object_hints.hints.length, 1);
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'hints-v1-old-sig', signature: SIG_NEW })));
assert.deepEqual(body.stale_reasons, ['model_signature', 'object_hints_version'], 'an older version under an older signature: both reasons, image version first'); assert.equal(body.stale, true);
body = await clean(response = await route.GET(request({ scene: SCENE, object: 'squared', signature: SIG_NEW })));
assert.deepEqual(body.object_hints.hints.map(h => h.code), ['noisy_outline'], 'where both versions exist the v2 row wins'); assert.equal(body.stale, false); assert.equal(body.object_hints_served_version, 'object-hints-v2');
body = await clean(response = await route.GET(request({ scene: SCENE, object: '7fdd138f819cb06c', signature: SIG_NEW })));
assert.equal(body.stale, true, 'the class and structure rows sit under "sig", not the named signature: stale, and still served'); assert.equal(body.class_hints.class_hint, 'outbuilding');
assert.deepEqual(body.stale_reasons, ['model_signature']); assert.equal(body.object_hints_served_version, null);
// --- Id validation --------------------------------------------------------------------------------------------
for (const bad of [{}, { scene: SCENE }, { object: 'x' }, { scene: '', object: 'x' }, { scene: '../x', object: 'x' }, { scene: '-flag', object: 'x' }, { scene: SCENE, object: 'a/b' }, { scene: SCENE, object: 'x'.repeat(101) }, { scene: 'a b', object: 'x' }]) {
  response = await route.GET(request(bad));
  assert.equal(response.status, 400, `bad ids ${JSON.stringify(bad)}`);
  body = await clean(response); assert.equal(typeof body.error, 'string');
}
// --- An index without the decision_policy table: class and structure hints answer as before, decision null ---------
body = await clean(response = await load(olderFile).GET(request({ scene: SCENE, object: '7fdd138f819cb06c' })));
assert.equal(response.status, 200, 'a missing decision_policy table is not an error');
assert.equal(body.class_hints.class_hint, 'outbuilding'); assert.equal(body.structure.sub_kind, 'shipping-container'); assert.equal(body.decision, null); assert.equal(body.decision_version, 'decision-policy-v2'); assert.equal(body.source, 'eyes-index');
// --- Unbuilt index and a Node without node:sqlite: same shape, nulls, source unavailable ---------------------------
const absent = load(`${folder}/does-not-exist/eyes.sqlite`);
body = await clean(response = await absent.GET(request({ scene: SCENE, object: '7fdd138f819cb06c' })));
assert.equal(response.status, 200, 'an unbuilt index is unavailable, never an error the editor has to handle');
assert.deepEqual(body, { scene_id: SCENE, object_id: '7fdd138f819cb06c', class_hints: null, structure: null, decision: null, object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null, lot_frame: null, hints_version: 'building-flags-v2', structure_version: 'structure-kinds-v3', decision_version: 'decision-policy-v2', object_hints_version: 'object-hints-v2', lot_tags_version: 'lot-tags-v1', source: 'unavailable', note: body.note });
const realBuiltin = process.getBuiltinModule;
process.getBuiltinModule = id => (id === 'node:sqlite' ? undefined : realBuiltin.call(process, id));
try {
  body = await clean(response = await route.GET(request({ scene: SCENE, object: '7fdd138f819cb06c' })));
  assert.equal(response.status, 200); assert.equal(body.source, 'unavailable'); assert.equal(body.class_hints, null); assert.equal(body.structure, null); assert.equal(body.decision, null);
} finally { process.getBuiltinModule = realBuiltin; }
body = await clean(response = await route.GET(request({ scene: SCENE, object: '7fdd138f819cb06c' })));
assert.equal(body.source, 'eyes-index', 'sqlite is back');
// --- Corrupt index: 500 with a safe message ------------------------------------------------------------------------
const corruptFile = `${folder}/corrupt/eyes.sqlite`;
mkdirSync(path.dirname(corruptFile));
writeFileSync(corruptFile, 'this is not a database file at all, just text that is long enough to be read as a header');
body = await clean(response = await load(corruptFile).GET(request({ scene: SCENE, object: '7fdd138f819cb06c' })));
assert.equal(response.status, 500); assert.match(body.error, /could not be read/i); assert.match(body.error, /unchanged/i);

// --- GET /api/queues/machine-labelled: the auto tier from one query over decision_policy, hints attached, same degradations ----
const loadMachine = indexFile => {
  const exports = {};
  new Function('exports', 'require', transpile('src/app/api/queues/machine-labelled/route.ts'))(exports, id => {
    if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
    if (id === '@/lib/queues') return { ...queues, EYES_INDEX: path.relative(process.cwd(), indexFile) };
    return require(id);
  });
  return exports;
};
const machineRoute = loadMachine(indexFile);
assert.equal(machineRoute.dynamic, 'force-dynamic'); assert.equal(typeof machineRoute.GET, 'function');
for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.equal(machineRoute[verb], undefined, `GET-only (${verb})`);
body = await clean(response = await machineRoute.GET());
assert.equal(response.status, 200); assert.equal(body.source, 'eyes-index'); assert.equal(body.version, 'decision-policy-v2'); assert.deepEqual(body.versions, ['decision-policy-v2', 'decision-policy-v1']);
assert.deepEqual(body.rows.map(r => `${r.object_id}:${r.version}`), ['decision-only:decision-policy-v1', 'unjudged:decision-policy-v2', 'upgraded:decision-policy-v2'], 'only the auto tier, v2 over v1, ordered by object; assist, malformed and bad-tier rows are out');
assert.equal(body.count, 3); assert.equal(body.reviewed_by_person, 1, 'unjudged carries a confirmed review status; the others were not reviewed by a person');
const upgradedRow = body.rows.find(r => r.object_id === 'upgraded');
assert.deepEqual([upgradedRow.label, upgradedRow.review_status, upgradedRow.reviewed_by_person, upgradedRow.review_url], ['Shipping container', 'Confirmed by machine', false, `/scenes?scene=${SCENE}&object=upgraded&kind=building&status=all&layer=model`]);
assert.deepEqual(upgradedRow.decision.interval, decisionPayloadV2.interval);
assert.equal(body.rows.find(r => r.object_id === 'decision-only').review_status, null, 'a v1 row without signals.review has no status cell');
assert.equal(body.rows.find(r => r.object_id === 'unjudged').review_status, 'Confirmed by reviewer');
assert.equal(queues.coerceMachineLabelledQueue(body).count, 3, 'the page coercer reads the body back');
assert.match(body.note, /nothing on this table approves/i);
// An index without the table, an unbuilt index and a Node without sqlite: the same shape with no rows and source unavailable; a corrupt file is 500.
for (const file of [olderFile, `${folder}/does-not-exist/eyes.sqlite`]) {
  body = await clean(response = await loadMachine(file).GET());
  assert.equal(response.status, 200); assert.equal(body.source, 'unavailable'); assert.deepEqual(body.rows, []); assert.equal(body.count, 0); assert.equal(body.version, 'decision-policy-v2');
}
process.getBuiltinModule = id => (id === 'node:sqlite' ? undefined : realBuiltin.call(process, id));
try { body = await clean(response = await machineRoute.GET()); assert.equal(body.source, 'unavailable'); assert.deepEqual(body.rows, []); } finally { process.getBuiltinModule = realBuiltin; }
body = await clean(response = await loadMachine(corruptFile).GET());
assert.equal(response.status, 500); assert.match(body.error, /could not be read/i); assert.match(body.error, /unchanged/i);

// --- Static pins on the scene editor (its imports are too heavy to render here): the machine label line and its one button ----
const page = readFileSync('src/app/scenes/page.tsx', 'utf8');
assert(page.includes('Agent hints: {hintsStale && <span className="scene-hint-stale">{staleNotes.join(" ")} </span>}{agentHintsText}'), 'the Agent hints line carries the described text (object hints, lot and machine label sentences included by describeObjectHints), prefixed by one notice per stale reason');
assert(page.includes('const staleNotes = servedHints && inspected ? hintStaleNotes(servedHints, inspected.kind) : [];') && page.includes('const hintsStale = staleNotes.length > 0;'), 'the notices come from the shared hintStaleNotes for the inspected kind');
assert(!page.includes('earlier image version or with earlier hint rules'), 'no blanket notice that conflates the two reasons');
assert(page.includes('{(agentHintsText || hintsStale) && <p className="review-muted scene-agent-hints"'), 'a stale notice shows even when the served rows have nothing else to say (a solar panel served from a v1 row)');
assert(page.includes('data-hint-stale-reasons={servedHints?.stale_reasons?.length ? servedHints.stale_reasons.join(" ") : undefined}') && page.includes('data-hints-served-version={servedHints?.object_hints_served_version ?? undefined}'), 'the reasons and the served version are on the line for tests');
assert(page.includes('Thresholds are design values ({OBJECT_HINTS_VERSION}{olderObjectHints ? `; served from ${olderObjectHints} rows` : ""})') && !page.includes('design values (object-hints-v1)'), 'the Why-flagged footer names the current object-hints version and, when older, the version served');
assert(page.includes('&signature=${encodeURIComponent(modelSignature)}'), 'the editor names its model signature so the route prefers rows for this image version');
assert(page.includes('data-testid="scene-object-hints"') && page.includes('data-hint-codes={objectHintList.map(h => h.code).join(" ")}') && page.includes('Why flagged ({objectHintList.length})'), 'the Why-flagged details list the fired codes for tests');
assert(page.includes('mergeObjectHints(servedHints?.object_hints?.hints, localOutlineHints)'), 'served object hints win; the editor-measured outline hints fill in the rest');
assert(page.includes('{ key: "0", kind: "", label: "No sub-kind" }') && !page.includes('"Primary roof"'), 'the empty quick kind is "No sub-kind": no structure row is not a claim');
assert(page.includes('className={`scene-decision not-a-roof${rejectSuggestion && inspected?.id === effective?.id ? " suggested" : ""}`}'), 'a class or size hint pre-highlights the existing Not a roof button');
assert(page.includes('onClick={() => reject()}') && page.includes('onClick={() => relabel(q.kind)}'), 'reject and relabel stay reviewer clicks; a suggestion calls nothing');
assert.equal((page.match(/Use machine label<\/button>/g) || []).length, 1, 'exactly one Use machine label button');
assert(page.includes('title="Applies the machine label to this object\'s Label and roof type; you still save the scene."'), 'the button title says what it does and that the reviewer still saves');
assert(page.includes('className="review-button scene-use-machine-label"') && page.includes('type="button"'), 'a quiet native button in the review style, never a form submit');
const disabled = page.match(/scene-use-machine-label"[\s\S]*?disabled=\{([^}]+)\}/);
assert(disabled, 'the button has a disabled expression');
assert(disabled[1].includes('layer === "model"') && disabled[1].includes('inspected.status === "superseded"'), `disabled on the model layer and for superseded objects: ${disabled[1]}`);
assert(disabled[1].includes('busy') && disabled[1].includes('data.stale'), 'and while busy or stale, like the rest of the fieldset');
assert(page.includes('{machineDecision && <>') && page.includes('agentHints.sceneId === id && agentHints.objectId === inspected.id ? agentHints.decision : null'), 'the button appears only when the hints for this very object carry a decision');
const apply = page.match(/const applyMachineLabel = \(object: SceneObject, decision: Decision\) => \{([\s\S]*?)\n  \};/);
assert(apply, 'applyMachineLabel exists with the object and decision');
assert(apply[1].includes('machineLabelName(decision.machine_label)') && apply[1].includes('label: name'), 'the Label text comes from the shared machineLabelName (structure sub-kind or class hint, sentence case)');
assert(apply[1].includes('!hasReviewerRoofEdits(object)') && apply[1].includes('next.roof_type = roofType'), 'roof_type only when the policy names one and no reviewer has shaped the roof');
assert(apply[1].includes('changeObject(next)'), 'the change goes through changeObject');
assert(!/kind:|status:/.test(apply[1]), 'kind and status are left to changeObject');
assert(!/persist\(|fetch\(|POST/.test(apply[1]), 'nothing is saved by the button');
assert(apply[1].includes('object.status === "superseded" || layer === "model"'), 'the handler refuses the same states the button disables');
assert(page.includes('onClick={() => applyMachineLabel(inspected, machineDecision)}'), 'the click applies to the inspected object');
console.log('scene hints route: GET-only, force-dynamic, no-store, both hint versions and the decision-policy-v1 decision from a seeded index, a missing decision_policy table as decision null, malformed payloads as null, 400/404/500 without leaks, the unavailable shape, v2 intervals with v1 fallback, the machine-labelled route (auto tier, hints, degradations) and the editor\'s Use machine label pins passed.');
