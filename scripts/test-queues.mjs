// Shared review-queue data layer (src/lib/queues.ts): live status derivation ported from
// scripts/ml/reference_calibration.classify, Wilson intervals matching scene_confidence.wilson,
// band aggregation, the calibration/checkpoint/self-review readers over an in-memory IO, and the
// hidden-roof row shape. Nothing here writes; every number is a model estimate or a human decision.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const load = file => {
  const exports = {};
  const code = ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => require(id));
  return exports;
};
const q = load('src/lib/queues.ts');
const close = (a, b, tolerance = 1e-12) => assert(Math.abs(a - b) <= tolerance, `${a} vs ${b}`);

// --- Wilson: same numbers as scripts/ml/scene_confidence.wilson -----------------------------------
let interval = q.wilson(19, 20);
close(interval[0], 0.763868806553258); close(interval[1], 0.9911185511992047);
interval = q.wilson(0, 20); close(interval[0], 0); close(interval[1], 0.16112515805281938);
interval = q.wilson(20, 20); close(interval[0], 0.8388748419471806); close(interval[1], 1);
interval = q.wilson(10, 30); close(interval[0], 0.19230498083676134); close(interval[1], 0.5121994835545616);
assert.equal(q.wilson(0, 0), null, 'no groups means no interval');
for (const [s, n] of [[21, 20], [-1, 5], [1.5, 3], ['1', 2]]) assert.throws(() => q.wilson(s, n), /valid binomial counts/);
assert.equal(q.MIN_CALIBRATION_GROUPS, 20);
assert.equal(q.IOU_SUCCESS, 0.85);

// --- Ring comparison and pixel IoU ------------------------------------------------------------------
const square = (x, y, size = 100) => [[x, y], [x + size, y], [x + size, y + size], [x, y + size]];
assert.equal(q.sameRing(square(0, 0), square(0, 0)), true);
assert.equal(q.sameRing(square(0, 0), square(0, 0).map(([x, y]) => [x + 5e-7, y])), true, 'np.allclose atol 1e-6');
assert.equal(q.sameRing(square(0, 0), square(0, 0).map(([x, y]) => [x + 1e-3, y])), false);
assert.equal(q.sameRing(square(0, 0), square(0, 0).slice(0, 3)), false, 'different shapes differ');
assert.equal(q.sameRing(square(0, 0), null), false);
assert.equal(q.sameRing([[0, 0], [1, 'x'], [2, 2]], [[0, 0], [1, 1], [2, 2]]), false);
close(q.ringIou({ ring: square(0, 0) }, { ring: square(0, 0) }), 1, 1e-9);
close(q.ringIou({ ring: square(0, 0) }, { ring: square(10, 0) }), 9000 / 11000, 0.004, 'shifted squares');
close(q.ringIou({ ring: square(0, 0) }, { ring: square(10, 10) }), 8100 / 11900, 0.004, 'diagonal shift');
close(q.ringIou({ ring: square(0, 0) }, { ring: square(0, 0), holes: [square(40, 40, 20)] }), 0.96, 0.004, 'holes are subtracted');
close(q.ringIou({ ring: [[0, 0], [100, 0], [100, 100], [50, 100], [50, 50], [0, 50]] }, { ring: square(0, 0) }), 0.75, 0.004, 'concave outlines');
close(q.ringIou({ ring: square(0, 0) }, { ring: square(500, 500) }), 0, 1e-9, 'disjoint');
// x overlap 3.1..38.2 (35.1), y overlap 0.7..38.6 (37.9): the smaller square sits inside the taller one vertically.
close(q.ringIou({ ring: square(0.3, 0.7, 37.9) }, { ring: square(3.1, -2.2, 41.3) }), (35.1 * 37.9) / (37.9 ** 2 + 41.3 ** 2 - 35.1 * 37.9), 0.004, 'fractional coordinates');
for (const bad of [{ ring: [[0, 0], [1, 1]] }, { ring: null }, {}, { ring: [[0, 0], [1, 'a'], [2, 2]] }, { ring: square(0, 0), holes: [[[0, 0], [1, NaN], [2, 2]]] }]) {
  assert.throws(() => q.ringIou({ ring: square(0, 0) }, bad), /ring/i, `invalid ring ${JSON.stringify(bad)}`);
}
assert.equal(q.ringIou({ ring: square(0, 0), holes: [[[1, 1]]] }, { ring: square(0, 0) }), 1, 'holes with fewer than three points are dropped, like Python');

// --- classify: the reference_calibration.classify rules ---------------------------------------------
const SIG = 'a'.repeat(64), OTHER = 'b'.repeat(64);
const latest = (objects, model_signature = SIG) => ({ id: 's', model_signature, objects });
const review = (objects, model_signature = SIG) => ({ revision: 1, model_signature, objects });
const model = { id: 'roof', kind: 'building', ring: square(100, 100), status: 'candidate', geometry_reviewed: false };
const reviewed = (status, ring = square(100, 100), extra = {}) => ({ id: 'roof', kind: 'building', ring, status, ...extra });
const expect = (result, status, counted, success, detail) => {
  assert.equal(result.status, status, `status for ${detail}`);
  assert.equal(result.counted, counted, `counted for ${detail}`);
  assert.equal(result.success, success, `success for ${detail}`);
  assert.equal(result.detail, detail);
  assert(['confirmed', 'rejected', 'needs_adjustment', 'pending', 'stale'].includes(result.status));
};
expect(q.classify(null, null, 'roof'), 'pending', false, null, 'latest_unavailable');
expect(q.classify('not a dict', review([]), 'roof'), 'pending', false, null, 'latest_unavailable');
expect(q.classify(latest([model]), null, 'roof'), 'pending', false, null, 'no_review');
expect(q.classify(latest([model]), review([reviewed('confirmed')], OTHER), 'roof'), 'stale', false, null, 'review_model_signature_differs_from_latest');
expect(q.classify({ objects: [model] }, { objects: [reviewed('confirmed', undefined, { geometry_reviewed: true })] }, 'roof'), 'confirmed', true, true, 'ring_unchanged');
expect(q.classify(latest([]), review([reviewed('confirmed')]), 'roof'), 'pending', false, null, 'object_not_in_latest');
expect(q.classify(latest([model]), review([]), 'roof'), 'pending', false, null, 'object_not_in_review');
expect(q.classify(latest([model]), review([reviewed('rejected')]), 'roof'), 'rejected', true, false, 'rejected_by_reviewer');
expect(q.classify(latest([model]), review([reviewed('rejected', [[1, 1]])]), 'roof'), 'rejected', true, false, 'rejected_by_reviewer');
let result = q.classify(latest([model]), review([reviewed('confirmed', undefined, { geometry_reviewed: true })]), 'roof');
expect(result, 'confirmed', true, true, 'ring_unchanged');
assert.equal(result.iou, 1); assert.equal(result.ring_edited, false); assert.equal(result.review_status, 'confirmed');
assert.equal(result.model_signature, SIG); assert.equal(result.review_model_signature, SIG);
result = q.classify(latest([model]), review([reviewed('confirmed', square(102, 100), { geometry_reviewed: true })]), 'roof');
expect(result, 'confirmed', true, true, 'ring_edited');
assert(result.ring_edited === true && result.iou > 0.95 && result.iou < 0.97, `small edit IoU ${result.iou}`);
result = q.classify(latest([model]), review([reviewed('confirmed', square(130, 100), { geometry_reviewed: true })]), 'roof');
expect(result, 'confirmed', true, false, 'ring_edited');
assert(result.iou > 0.53 && result.iou < 0.54, `large edit IoU ${result.iou}`);
expect(q.classify(latest([model]), review([reviewed('confirmed')]), 'roof'), 'pending', false, null, 'confirmed_without_geometry_reviewed');
expect(q.classify(latest([model]), review([reviewed('confirmed', undefined, { geometry_reviewed: 'yes' })]), 'roof'), 'pending', false, null, 'confirmed_without_geometry_reviewed');
expect(q.classify(latest([model]), review([reviewed('needs_adjustment')]), 'roof'), 'needs_adjustment', false, null, 'ring_unedited: tier-3 observation, not counted until the ring is edited');
expect(q.classify(latest([model]), review([reviewed('needs_adjustment', square(101, 100))]), 'roof'), 'needs_adjustment', true, true, 'ring_edited');
expect(q.classify(latest([model]), review([reviewed('needs_adjustment', square(160, 100))]), 'roof'), 'needs_adjustment', true, false, 'ring_edited');
expect(q.classify(latest([model]), review([reviewed('confirmed', [[1, 1], [2, 2]], { geometry_reviewed: true })]), 'roof'), 'pending', false, null, 'invalid_ring');
expect(q.classify(latest([model]), review([reviewed('needs_adjustment', 'garbage')]), 'roof'), 'pending', false, null, 'invalid_ring');
expect(q.classify(latest([model]), review([reviewed('candidate')]), 'roof'), 'pending', false, null, 'status:candidate');
expect(q.classify(latest([model]), review([reviewed('superseded')]), 'roof'), 'pending', false, null, 'status:superseded');
// Holes count as ring edits.
const holed = { ...model, holes: [square(120, 120, 10)] };
result = q.classify(latest([holed]), review([reviewed('confirmed', undefined, { geometry_reviewed: true, holes: [square(120, 120, 10)] })]), 'roof');
expect(result, 'confirmed', true, true, 'ring_unchanged');
result = q.classify(latest([holed]), review([reviewed('confirmed', undefined, { geometry_reviewed: true })]), 'roof');
expect(result, 'confirmed', true, true, 'ring_edited');
assert(result.iou > 0.98 && result.iou < 1, 'filled hole is a small edit');
assert.equal(q.classify(latest([model]), review([reviewed('confirmed')]), 'roof', 'ignored').status, 'pending');
assert.equal(q.classify({ objects: [model] }, { objects: [reviewed('confirmed')] }, 'roof').detail, 'confirmed_without_geometry_reviewed', 'missing signatures on both sides are equal, not stale');
assert.equal(q.classify(latest([model], null), { objects: [reviewed('confirmed')] }, 'roof').detail, 'confirmed_without_geometry_reviewed', 'null and missing signature compare equal like Python None');
assert.equal(q.classify(latest([model]), review([reviewed('confirmed')], null), 'roof').status, 'stale');
assert.equal(q.classify(latest([model]), { objects: [reviewed('confirmed')] }, 'roof').status, 'stale', 'a review without a signature is stale against a signed model');

// --- Band aggregation -------------------------------------------------------------------------------
const rows = [];
for (let i = 0; i < 20; i++) rows.push({ band: 'low', counted: true, success: i !== 0, status: i ? 'confirmed' : 'rejected' });
for (let i = 0; i < 19; i++) rows.push({ band: 'medium', counted: true, success: true, status: 'confirmed' });
rows.push({ band: 'medium', counted: false, success: null, status: 'pending' }, { band: 'medium', counted: false, success: null, status: 'stale' });
const bands = q.aggregateBands(rows, ['low', 'medium', 'high'], 20);
assert.deepEqual(Object.keys(bands), ['low', 'medium', 'high']);
assert.equal(bands.low.groups_total, 20); assert.equal(bands.low.groups_reviewed, 20); assert.equal(bands.low.successes, 19); assert.equal(bands.low.failures, 1);
assert.deepEqual(bands.low.interval, q.wilson(19, 20)); assert.equal(bands.low.minimum_groups, 20);
assert.equal(bands.medium.groups_total, 21); assert.equal(bands.medium.groups_reviewed, 19); assert.equal(bands.medium.interval, null, 'no interval below the minimum');
assert.equal(bands.medium.remaining_to_interval, 1);
assert.deepEqual(bands.medium.statuses, { confirmed: 19, rejected: 0, needs_adjustment: 0, pending: 1, stale: 1 });
assert.deepEqual(bands.high, { groups_total: 0, groups_reviewed: 0, successes: 0, failures: 0, interval: null, minimum_groups: 20, remaining_to_interval: 20,
  statuses: { confirmed: 0, rejected: 0, needs_adjustment: 0, pending: 0, stale: 0 } });

// --- Calibration queue over an in-memory IO ---------------------------------------------------------
const PLAN = '7cfed4d9e02f1c8a';
const files = new Map();
const io = {
  readText: p => (files.has(p) ? files.get(p) : null),
  listDir: p => [...new Set([...files.keys()].filter(k => k.startsWith(p + '/')).map(k => k.slice(p.length + 1).split('/')[0]))],
  exists: p => files.has(p) || [...files.keys()].some(k => k.startsWith(p + '/')),
};
const sceneA = 'mooroolbark-50011498-17-118440-80408', sceneB = 'house-30286', sceneC = 'mooroolbark-50011498-17-118441-80408';
const planRows = [
  { band: 'low', group: 'z17:118440:80408', scene_id: sceneA, object_id: 'roof', review_url: `/scenes?scene=${sceneA}&object=roof&kind=building&status=all&layer=model`, area_m2: 63.379, detector_score: 0.5012, sample_key: 'x', group_candidates: 12 },
  { band: 'medium', group: 'z17:118441:80408', scene_id: sceneB, object_id: 'roof', review_url: `/scenes?scene=${sceneB}&object=roof&kind=building&status=all&layer=model`, area_m2: 120, detector_score: 0.8 },
  { band: 'medium', group: 'z17:118442:80408', scene_id: sceneC, object_id: 'roof', review_url: `/scenes?scene=${sceneC}&object=roof&kind=building&status=all&layer=model`, area_m2: null, detector_score: null },
  { band: 'high', group: 'z17:118443:80408', scene_id: '../etc', object_id: 'roof', review_url: '/scenes', area_m2: 1, detector_score: 1 },
];
files.set(`data/reviews/eyes/calibration/${PLAN}/plan.json`, JSON.stringify({
  version: 'reference-calibration-v1', plan_id: PLAN, created_at: '2026-09-25T14:05:04.554677+00:00',
  policy: 'object-centroid core tile; calibration only; SAM3 encoded 64 px of neighbouring tiles',
  statement: 'sampled-roof protocol; boundary IoU >= 0.85 only; not apex/height/recall; assumes independent representative areas',
  iou_success: 0.85, bands: ['low', 'medium', 'high'], label_budget: { per_band_minimum: 20 }, rows: planRows,
}));
files.set(`data/reviews/eyes/calibration/${PLAN}/addresses.json`, JSON.stringify({ plan_id: PLAN, resolved: {
  [`${sceneA}/roof`]: { status: 'matched', address_id: 31114, full_address: '19 LARNOOK CRESCENT, Mooroolbark VIC 3138', vicmap_address: '19 LARNOOK CRESCENT MOOROOLBARK 3138' },
  [`${sceneB}/roof`]: { status: 'matched', address_id: null, full_address: null, vicmap_address: '9 BATTERBEE DRIVE MOOROOLBARK 3138' },
} }));
files.set(`data/scenes/${sceneA}/latest.json`, JSON.stringify(latest([model])));
files.set(`data/scenes/${sceneB}/latest.json`, JSON.stringify(latest([model])));
files.set(`data/scenes/${sceneB}/review.json`, JSON.stringify(review([reviewed('confirmed', undefined, { geometry_reviewed: true })])));
files.set(`data/scenes/${sceneC}/latest.json`, JSON.stringify(latest([model])));
files.set(`data/scenes/${sceneC}/review.json`, JSON.stringify(review([reviewed('confirmed')], OTHER)));

let queue = q.buildCalibrationQueue(io, PLAN);
assert.equal(queue.plan_id, PLAN);
assert.equal(queue.policy, 'object-centroid core tile; calibration only; SAM3 encoded 64 px of neighbouring tiles');
assert.match(queue.statement, /boundary IoU >= 0.85 only/);
assert.equal(queue.iou_success, 0.85);
assert.equal(queue.rows.length, 4);
assert.deepEqual(Object.keys(queue.bands), ['low', 'medium', 'high']);
const [a, b, c, d] = queue.rows;
assert.deepEqual(Object.fromEntries(Object.entries(a).filter(([k]) => !['detail', 'iou'].includes(k))), {
  band: 'low', group: 'z17:118440:80408', scene_id: sceneA, object_id: 'roof', review_url: planRows[0].review_url, area_m2: 63.379, detector_score: 0.5012,
  status: 'pending', counted: false, success: null, address: '19 LARNOOK CRESCENT, Mooroolbark VIC 3138', address_id: 31114 });
assert.equal(a.detail, 'no_review');
assert.equal(b.status, 'confirmed'); assert.equal(b.counted, true); assert.equal(b.success, true);
assert.equal(b.address, '9 BATTERBEE DRIVE MOOROOLBARK 3138', 'vicmap address is the fallback'); assert.equal(b.address_id, null);
assert.equal(c.status, 'stale'); assert.equal(c.address, null); assert.equal(c.address_id, null); assert.equal(c.area_m2, null);
assert.equal(d.status, 'pending'); assert.equal(d.detail, 'invalid_scene_id');
assert.equal(queue.bands.low.groups_total, 1); assert.equal(queue.bands.medium.groups_reviewed, 1); assert.equal(queue.bands.medium.successes, 1);
assert.equal(queue.bands.medium.interval, null); assert.equal(queue.bands.high.groups_total, 1);
assert.match(queue.note, /model estimate/i, 'the queue says bands are model estimates, not accuracy');
assert.doesNotMatch(JSON.stringify(queue), /\/Users|data\/scenes/, 'no local paths in the payload');
for (const bad of ['', '..', 'ABCDEF0123456789', '7cfed4d9e02f1c8', 'x'.repeat(16), '7cfed4d9e02f1c8a/..']) {
  assert.throws(() => q.buildCalibrationQueue(io, bad), error => error instanceof q.QueueError && error.status === 400, `bad plan id ${bad}`);
}
assert.throws(() => q.buildCalibrationQueue(io, 'ffffffffffffffff'), error => error instanceof q.QueueError && error.status === 404);
files.set('data/reviews/eyes/calibration/ffffffffffffffff/plan.json', JSON.stringify({ plan_id: PLAN, rows: [] }));
assert.throws(() => q.buildCalibrationQueue(io, 'ffffffffffffffff'), error => error.status === 404, 'plan.json must carry the same plan id');
files.delete('data/reviews/eyes/calibration/ffffffffffffffff/plan.json');
// A missing addresses.json only removes addresses.
files.delete(`data/reviews/eyes/calibration/${PLAN}/addresses.json`);
queue = q.buildCalibrationQueue(io, PLAN);
assert.equal(queue.rows[0].address, null); assert.equal(queue.rows[0].address_id, null);
// latest.json is only parsed when a review exists; a missing latest.json is still latest_unavailable.
files.delete(`data/scenes/${sceneA}/latest.json`);
assert.equal(q.buildCalibrationQueue(io, PLAN).rows[0].detail, 'latest_unavailable');
files.set(`data/scenes/${sceneA}/latest.json`, JSON.stringify(latest([model])));
assert.equal(q.latestPlanId(io), PLAN);
files.set('data/reviews/eyes/calibration/0123456789abcdef/plan.json', JSON.stringify({ plan_id: '0123456789abcdef', created_at: '2026-09-27T00:00:00+00:00', rows: [] }));
files.set('data/reviews/eyes/calibration/not-a-plan/plan.json', '{}');
assert.equal(q.latestPlanId(io), '0123456789abcdef', 'newest plan by created_at wins');
files.delete('data/reviews/eyes/calibration/0123456789abcdef/plan.json');
assert.equal(q.latestPlanId({ readText: () => null, listDir: () => [], exists: () => false }), null);

// --- Self-review compact fields (mirrors roof_review_loop.compact counts) --------------------------
assert.deepEqual(q.selfReviewSummary(io), { run: null, status: 'idle', total: 0, completed: 0, outcomes: { no_safe_gain: 0, needs_evidence: 0, candidate_ready: 0 }, counts: {} });
files.set('data/roof-review-loop/active.json', JSON.stringify({ run: '1fe92abe90ebd534' }));
files.set('data/roof-review-loop/runs/1fe92abe90ebd534/report.json', JSON.stringify({ run: '1fe92abe90ebd534', status: 'complete', total: 12,
  rows: [...Array(7).fill({ status: 'no_safe_gain' }), ...Array(4).fill({ status: 'needs_evidence' }), { status: 'skipped' }] }));
assert.deepEqual(q.selfReviewSummary(io), { run: '1fe92abe90ebd534', status: 'complete', total: 12, completed: 12,
  outcomes: { no_safe_gain: 7, needs_evidence: 4, candidate_ready: 0 }, counts: { no_safe_gain: 7, needs_evidence: 4, skipped: 1 } });
files.set('data/roof-review-loop/active.json', JSON.stringify({ run: '../../etc' }));
assert.equal(q.selfReviewSummary(io).status, 'idle', 'a malformed run id is never used as a path');
files.set('data/roof-review-loop/active.json', JSON.stringify({ run: 'deadbeefdeadbeef' }));
assert.deepEqual(q.selfReviewSummary(io), { run: 'deadbeefdeadbeef', status: 'unavailable', total: 0, completed: 0, outcomes: { no_safe_gain: 0, needs_evidence: 0, candidate_ready: 0 }, counts: {} });

// --- Checkpoints ------------------------------------------------------------------------------------
const sha = text => 'sha:' + text.length;
const report = (id, computed_at, extra = {}) => JSON.stringify({ version: 'crm-checkpoint-v1', computed_at, checkpoint: { id, title: `EYES ${id}`, summary: `${id} summary`,
  address_id: 29790, address_text: '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138', latitude: -37.79033, longitude: 145.31259, report_url: `data/references/eyes-checkpoints/${id}/REVIEW.md`, ...extra } });
files.set('data/references/eyes-checkpoints/CP1/report.json', report('CP1', '2026-09-25T13:37:28+00:00'));
files.set('data/references/eyes-checkpoints/CP1/REVIEW.md', '# EYES CP1\n');
files.set('data/references/eyes-checkpoints/CP1/dry-run/report.json', report('CP1', '2026-09-25T13:35:00+00:00'));
files.set('data/references/eyes-checkpoints/CP2/report.json', report('CP2', '2026-09-25T15:51:14+00:00', { address_id: null, latitude: 'x' }));
files.set('data/references/eyes-checkpoints/CP2-CALIB/report.json', report('CP2-CALIB', '2026-09-25T15:47:21+00:00'));
files.set('data/references/eyes-checkpoints/CP2-CALIB/REVIEW.md', '# calib\n');
files.set('data/references/eyes-checkpoints/lower-case/report.json', report('lower-case', '2026-09-26T00:00:00+00:00'));
files.set('data/references/eyes-checkpoints/EMPTY/REVIEW.md', 'no report');
files.set('data/references/eyes-checkpoints/BROKEN/report.json', '{not json');
const checkpoints = q.listCheckpoints(io, sha);
assert.deepEqual(checkpoints.map(c => c.id), ['CP2', 'CP2-CALIB', 'CP1'], 'newest first; dry-run, lower-case, reportless and broken folders are skipped');
assert.deepEqual(checkpoints[2], { id: 'CP1', title: 'EYES CP1', created_at: '2026-09-25T13:37:28+00:00', summary: 'CP1 summary', address_text: '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138',
  address_id: 29790, latitude: -37.79033, longitude: 145.31259, report_sha256: sha(files.get('data/references/eyes-checkpoints/CP1/report.json')),
  review_path: 'data/references/eyes-checkpoints/CP1/REVIEW.md', report_url: 'data/references/eyes-checkpoints/CP1/REVIEW.md' });
assert.equal(checkpoints[0].address_id, null); assert.equal(checkpoints[0].latitude, null); assert.equal(checkpoints[0].review_path, null, 'no REVIEW.md means no review path');
const detail = q.readCheckpoint(io, 'CP1');
assert.equal(detail.id, 'CP1'); assert.equal(detail.markdown, '# EYES CP1\n'); assert.equal(detail.report.checkpoint.title, 'EYES CP1');
assert.equal(q.readCheckpoint(io, 'CP2').markdown, '', 'a report without REVIEW.md renders empty markdown');
assert.equal(q.readCheckpoint(io, 'CP9'), null);
assert.equal(q.readCheckpoint(io, 'EMPTY'), null, 'REVIEW.md without report.json is not a checkpoint');
for (const bad of ['', 'cp1', '../CP1', 'CP1/dry-run', '-CP1', 'C'.repeat(33), 'CP 1']) {
  assert.throws(() => q.readCheckpoint(io, bad), error => error instanceof q.QueueError && error.status === 400, `bad checkpoint id ${bad}`);
}

// --- Hidden roofs: the CLI document becomes rows with a fixed shape ---------------------------------
const hidden = q.hiddenQueue({ version: 'cross-capture-v1', count: 1, rows: [{ scene_id: 'tile', object_id: 'low-roof', band: 'low', detector_score: 0.58, observed_segments: 4,
  other_scene_id: 'house', other_object_id: 'med-roof', other_band: 'medium', other_detector_score: 0.8, iou: 0.8913,
  hidden_url: '/scenes?scene=tile&object=low-roof&kind=building&status=all&layer=model', shown_url: '/scenes?scene=house&object=med-roof&kind=building&status=all&layer=model', extra: 'dropped' }] }, null);
assert.equal(hidden.version, 'cross-capture-v1'); assert.equal(hidden.count, 1);
assert.deepEqual(hidden.rows[0], { scene_id: 'tile', object_id: 'low-roof', band: 'low', detector_score: 0.58, observed_segments: 4, other_scene_id: 'house', other_object_id: 'med-roof',
  other_band: 'medium', other_detector_score: 0.8, iou: 0.8913, hidden_url: '/scenes?scene=tile&object=low-roof&kind=building&status=all&layer=model',
  shown_url: '/scenes?scene=house&object=med-roof&kind=building&status=all&layer=model', address: null });
assert.match(hidden.note, /not accuracy/);
assert.throws(() => q.hiddenQueue({ rows: 'nope' }), /rows/);
assert.throws(() => q.hiddenQueue('not json'), /rows/);
assert.equal(q.hiddenQueue({ version: 'cross-capture-v1', rows: [{ scene_id: 'a', object_id: 'b', other_scene_id: 'c', other_object_id: 'd' }] }, null).rows[0].iou, null, 'missing numbers are null');

// --- Building class hints (building-flags-v2) attached from the eyes index -----------------------------
// Hints are agent hints, never labels: the row keeps every existing field and gains hints + class_hint only
// when the index holds a building-flags row for that object.
assert.equal(q.HINTS_VERSION, 'building-flags-v2');
assert.match(q.HINTS_NOTE, /not labels?/i);
const treePayload = { kind: 'building', area_m2: 708.26, class_hints: { version: 'building-flags-v2', note: 'agent hint, not a label', class_hint: 'tree-like',
  flags: [{ flag: 'tree-like', evidence: { exg_green_share: 0.62, solidity: 0.71, tree_overlap_iou: 0.35, texture: 1200.5, vertex_density: 11.6, rule: 'exg_green_share >= 0.45 and (solidity <= 0.80 or tree_overlap_iou >= 0.3)' } }] } };
const shedPayload = { kind: 'building', area_m2: 63.38, class_hints: { class_hint: 'outbuilding', flags: [{ flag: 'secondary-structure', evidence: { area_m2: 63.379, largest_same_property_area: 258.1, ratio: 4.0724, source: 'vicmap-property', property_pfi: '1348074', largest_object: 'tile/b6e8', rule: 'r' } }] } };
const shopPayload = { kind: 'building', area_m2: 462.4, class_hints: { class_hint: 'commercial', flags: [{ flag: 'non-residential-context', evidence: { poi_within_30m: [{ name: 'Kiloran Park', category: 'park', distance_m: 16.47 }], landuse: [{ tag: 'leisure=park', name: 'Kiloran Park', distance_m: 0 }, { tag: 'leisure=pitch', name: null, distance_m: 1.52 }], landuse_status: 'available', address_kind: 'business', site_names: ['KILORAN PARK'], area_m2: 462.386, rule: 'r' } }] } };
const housePayload = { kind: 'building', area_m2: 180, class_hints: { class_hint: 'house', flags: [] } };
let parsed = q.parseClassHints(treePayload);
assert.deepEqual(parsed, { hints: [{ flag: 'tree-like', evidence: treePayload.class_hints.flags[0].evidence }], class_hint: 'tree-like' });
assert.deepEqual(q.parseClassHints(JSON.stringify(shedPayload)), { hints: shedPayload.class_hints.flags, class_hint: 'outbuilding' }, 'payload text is parsed');
assert.deepEqual(q.parseClassHints(housePayload), { hints: [], class_hint: 'house' });
for (const bad of [null, 'nope', 42, {}, { class_hints: 'x' }, { class_hints: { flags: 'x', class_hint: 3 } }, { class_hints: { flags: [{ flag: 7 }, 'junk', { flag: 'tree-like' }] } }]) {
  const got = q.parseClassHints(bad);
  assert(Array.isArray(got.hints) && got.hints.every(h => typeof h.flag === 'string' && typeof h.evidence === 'object'), `malformed hints are dropped: ${JSON.stringify(bad)}`);
  assert(got.class_hint === null || typeof got.class_hint === 'string');
}
assert.deepEqual(q.parseClassHints({ class_hints: { flags: [{ flag: 7 }, 'junk', { flag: 'tree-like' }], class_hint: 'zebra' } }), { hints: [{ flag: 'tree-like', evidence: {} }], class_hint: null }, 'unknown class hints are null, hints without evidence keep an empty evidence object');
// Badges: one per flag plus Commercial from the class hint; labels in sentence case; titles carry the evidence and the not-a-label copy.
let badges = q.hintBadges(q.parseClassHints(shopPayload));
assert.deepEqual(badges.map(b => b.label), ['Non-residential context', 'Commercial']);
assert(badges[0].title.startsWith('Agent hint, not a label.'), badges[0].title);
assert(badges[0].title.includes('Kiloran Park (park) 16 m') && badges[0].title.includes('leisure=park Kiloran Park 0 m') && badges[0].title.includes('leisure=pitch 2 m') && badges[0].title.includes('address kind business') && badges[0].title.includes('site name Kiloran Park'), badges[0].title);
assert(badges[1].title.includes('462 m²') && /non-residential context/.test(badges[1].title), badges[1].title);
badges = q.hintBadges(q.parseClassHints(treePayload));
assert.deepEqual(badges.map(b => b.label), ['Tree-like']);
assert(badges[0].title.includes('green share 0.62') && badges[0].title.includes('solidity 0.71') && badges[0].title.includes('tree overlap IoU 0.35'), badges[0].title);
badges = q.hintBadges(q.parseClassHints(shedPayload));
assert.deepEqual(badges.map(b => b.label), ['Secondary structure']);
assert(badges[0].title.includes('63 m² vs 258 m² largest roof on the same property') && badges[0].title.includes('ratio 4.1') && badges[0].title.includes('Vicmap property 1348074'), badges[0].title);
assert.deepEqual(q.hintBadges(q.parseClassHints(housePayload)), [], 'a house without flags has no badge');
assert.deepEqual(q.hintBadges({ hints: [{ flag: 'secondary-structure', evidence: { area_m2: 40, largest_same_property_area: 200, ratio: 5, source: 'proximity-25m', property_pfi: null, largest_object: 'b1' } }], class_hint: 'outbuilding' }).map(b => b.title.includes('largest roof within 25 m')), [true], 'the proximity proxy is named as such');
assert.equal(q.hintBadges({ hints: [{ flag: 'mystery', evidence: { a: 1 } }], class_hint: null })[0].label, 'mystery', 'unknown flags still render with their raw name');
for (const badge of q.hintBadges(q.parseClassHints(shopPayload))) assert(!/[A-Z]{4,}/.test(badge.title), `no shouting in titles: ${badge.title}`);
assert(q.describeHint({ flag: 'non-residential-context', evidence: { poi_within_30m: [], landuse: [], landuse_status: 'outside coverage', address_kind: null, site_names: ['ESTHER PARK'] } }).endsWith('site name Esther Park; outside the land-use and places coverage area'), 'a capture outside the fetched box says so instead of implying land use was checked');
assert(q.describeHint({ flag: 'non-residential-context', evidence: { poi_within_30m: [], landuse: [], landuse_status: 'unavailable', address_kind: 'business', site_names: [] } }).endsWith('address kind business; land use unavailable offline'));
assert.equal(q.isFlagged(q.parseClassHints(housePayload)), false);
assert.equal(q.isFlagged(q.parseClassHints(shopPayload)), true);
assert.equal(q.isFlagged({ hints: [], class_hint: 'commercial' }), true, 'a commercial class hint counts as flagged');
assert(q.hintBadges({ hints: [], class_hint: 'commercial' })[0].title.includes('Roof of 400 m² or more'), 'a commercial hint without flag evidence still explains the rule');
// Index source over a database-like object: read-only SQL on object_quality, keyed by version, scene and object.
const indexRows = new Map([['tile/low-roof', JSON.stringify(shedPayload)], ['house/med-roof', JSON.stringify(shopPayload)], ['tile/broken', '{not json'], ['tile/no-hints', JSON.stringify({ kind: 'building' })]]);
// structure-kinds-v1 rows: written by a separate module, keyed the same way; absent for most objects, malformed for some.
const containerPayload = { version: 'structure-kinds-v1', note: 'agent hint, not a label', kind: 'building', area_m2: 14.9, sub_kind: 'shipping-container', confidence: 'moderate',
  rule: 'aspect >= 2.2 and rib_anisotropy >= 0.35 and rectilinearity >= 0.9', evidence: { geometry: { area_m2: 14.9, rect_width_m: 2.4, rect_length_m: 6.1, aspect: 2.54, rectilinearity: 0.96, circularity: 0.62, vertex_count: 5 },
  appearance: { brightness_median: 141, saturation_median: 22, rib_anisotropy: 0.42, rib_period_m: 0.28, rib_axis_alignment: 0.91, interior_relative_variance: 0.08 },
  context: { attached_to_larger_roof: false, distance_to_largest_roof_m: 9.4, largest_same_property_area: 151.3, secondary_structure_flag: true, touches_image_edge: false },
  sam3: { prompts: { house: { best_iou: 0.31, score: 0.12 }, 'shipping container': { best_iou: 0.78, score: 0.66 } }, image: 'clahe' } }, computed_at: '2026-09-26T09:00:00+00:00', code_sha256: 'd'.repeat(64) };
const fragmentPayload = { ...containerPayload, sub_kind: 'fragment', confidence: 'weak', evidence: { geometry: { area_m2: 3.1 }, context: { attached_to_larger_roof: true, touches_image_edge: true }, sam3: null } };
const structureRows = new Map([['tile/low-roof', JSON.stringify(containerPayload)], ['tile/no-hints', JSON.stringify({ sub_kind: 'zebra', confidence: 'strong' })], ['tile/broken', '{not json'],
  ['tile/structure-only', JSON.stringify(fragmentPayload)], ['house/med-roof', JSON.stringify({ ...containerPayload, sub_kind: 'not-secondary', confidence: 'strong' })]]);
// structure-kinds-v3 rows win over v1 rows for the same object; v1 rows are still read where no v2 row exists yet.
const stackPayload = { ...containerPayload, version: 'structure-kinds-v3', sub_kind: 'container-stack', confidence: 'strong', evidence: { ...containerPayload.evidence, geometry: { ...containerPayload.evidence.geometry, rect_width_m: 4.9, rect_length_m: 12.2, container_widths: 2 } } };
const structureRowsV2 = new Map([['tile/stacked', JSON.stringify(stackPayload)], ['tile/broken', JSON.stringify({ sub_kind: 'zebra' })]]);
// decision-policy-v1 rows live in their own table (decision_policy), written by yet another module; the fake routes that SQL to its own map.
const decisionPayload = { version: 'decision-policy-v1', note: 'machine label and assessment; auto tier proceeds without a human, assist and review need one', tier: 'auto', confidence: 0.91,
  machine_label: { kind: 'building', structure_sub_kind: 'garage', class_hint: 'outbuilding', roof_form: 'pitched', roof_type: 'gabled' }, reasons: ['small footprint beside the driveway', 'strong structure hint'],
  signals: { area_m2: 36.2 }, thresholds_version: 'thresholds-v1', computed_at: '2026-09-26T10:00:00+00:00', code_sha256: 'e'.repeat(64) };
// v1 rows (no intervals) are read where no v2 row exists; a v2 row for the same object wins.
const decisionRows = new Map([['tile/decided', JSON.stringify(decisionPayload)], ['house/med-roof', JSON.stringify({ ...decisionPayload, tier: 'review', confidence: 0.42, machine_label: { kind: 'building', structure_sub_kind: null, class_hint: 'commercial', roof_form: 'flat', roof_type: null }, reasons: [] })],
  ['tile/broken', '{not json'], ['tile/no-hints', JSON.stringify({ ...decisionPayload, tier: 'maybe' })], ['tile/upgraded', JSON.stringify(decisionPayload)]]);
const decisionPayloadV2 = { ...decisionPayload, version: 'decision-policy-v2', tier: 'assist', confidence: 0.88,
  interval: { kind: 'wilson-95', stratum: 'auto/garage', confirmed: 6, judged: 9, lower: 0.3536, upper: 0.8791, note: 'reviewer decisions in this stratum' },
  score_interval: { lower: 0.8612, upper: 0.9488, note: 'model estimate range' }, signals: { review: { status: 'confirmed', present: true } } };
const decisionRowsV2 = new Map([['tile/upgraded', JSON.stringify(decisionPayloadV2)], ['tile/broken', JSON.stringify({ tier: 'auto' })]]);
// object-hints rows (scene_object_hints): measured hints with a squared outline candidate; keyed the same way, absent for most objects.
// object-hints-v2 is current. 'tile/v1-only' has only an object-hints-v1 row (served as the fallback, stale when a signature is named);
// 'tile/low-roof' also has a v1 row, which its v2 row shadows.
const objectHintsPayload = { version: 'object-hints-v2', note: 'agent hint, not a label', kind: 'building', area_m2: 87.05,
  hints: [{ code: 'noisy_outline', severity: 'review', sentence: "54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side.", evidence: { vertices: 54, off_axis_edges: 49, vertices_per_metre: 0.489, wonky_share: 0.907 } },
    { code: 'lot_incomplete_in_frame', severity: 'context', sentence: 'Lot 2 PS747728 leaves this image.', evidence: { share: 0.62 } },
    { code: 'frame_cut', severity: 'review', sentence: 'Touches the image edge; the frame cut it, not the building.', evidence: { edge_px: 0 } }],
  squared_candidate: { refused: 'iou 0.81 below 0.85' }, lot_ref: 'f'.repeat(64), code_sha256: 'a'.repeat(64), computed_at: '2026-09-30T08:00:00+00:00' };
const objectHintRows = new Map([['tile/low-roof', JSON.stringify(objectHintsPayload)], ['tile/broken', '{not json'], ['tile/hinted', JSON.stringify({ ...objectHintsPayload, hints: [{ code: 'pool_not_blue', severity: 'review', sentence: 'Model says pool; the interior reads green 100%, blue 0%, like grass. Check the image.', evidence: { green_share: 1, blue_share: 0 } }], squared_candidate: null })]]);
const objectHintsV1Payload = { ...objectHintsPayload, version: 'object-hints-v1', hints: [objectHintsPayload.hints[2]], squared_candidate: null };
const objectHintRowsV1 = new Map([['tile/v1-only', JSON.stringify(objectHintsV1Payload)], ['tile/low-roof', JSON.stringify({ ...objectHintsV1Payload, hints: [] })]]);
const sqls = [];
let closed = 0, prepared = 0;
const fakeDb = { prepare: sql => { sqls.push(sql); prepared++; assert(!/INSERT|UPDATE|DELETE|CREATE|DROP|ATTACH/i.test(sql), 'read-only SQL only'); return { get: (version, scene, object, signature) => {
  assert(['building-flags-v2', 'structure-kinds-v3', 'structure-kinds-v2', 'structure-kinds-v1', 'decision-policy-v2', 'decision-policy-v1', 'object-hints-v2', 'object-hints-v1', 'lot-tags-v1'].includes(version), `versions read: ${version}`);
  assert.equal(/decision_policy/.test(sql), version.startsWith('decision-policy'), `the decision versions are read from decision_policy and the hint versions from object_quality (${version})`);
  if (/object_quality/.test(sql)) { assert.match(sql, /ORDER BY \(model_signature = \?\) DESC/, 'object_quality rows prefer the caller\'s signature'); assert.equal(typeof signature, 'string', 'the signature parameter is always bound (empty when the caller has none)'); }
  else assert.equal(signature, undefined, 'the decision statement takes three parameters');
  const payload = (version === 'building-flags-v2' ? indexRows : version === 'structure-kinds-v3' ? structureRowsV2 : version === 'structure-kinds-v2' ? new Map() : version === 'decision-policy-v2' ? decisionRowsV2 : version === 'decision-policy-v1' ? decisionRows : version === 'object-hints-v2' ? objectHintRows : version === 'object-hints-v1' ? objectHintRowsV1 : version === 'lot-tags-v1' ? new Map() : structureRows).get(`${scene}/${object}`); return payload === undefined ? undefined : { payload, model_signature: 'sig-a' }; } }; }, close: () => { closed++; } };
let source = q.indexHintSource(fakeDb);
assert.deepEqual(source.hintFor('tile', 'low-roof'), { hints: shedPayload.class_hints.flags, class_hint: 'outbuilding', structure: q.parseStructureKind(containerPayload), object_hints: q.parseObjectHints(objectHintsPayload), object_hints_served_version: 'object-hints-v2' }, 'class, structure and object-hint versions are read for one object, with the version the object hints came from');
assert.deepEqual(source.hintFor('tile', 'hinted'), { hints: [], class_hint: null, object_hints: { hints: [{ code: 'pool_not_blue', severity: 'review', sentence: 'Model says pool; the interior reads green 100%, blue 0%, like grass. Check the image.', evidence: { green_share: 1, blue_share: 0 } }], squared_candidate: null }, object_hints_served_version: 'object-hints-v2' }, 'an object-hints row alone is still a hint row');
assert.equal(source.hintFor('tile', 'missing'), null, 'no row of either version means no hints, not empty hints');
assert.equal(source.hintFor('tile', 'broken'), null, 'unparseable payloads are skipped');
assert.deepEqual(source.hintFor('tile', 'no-hints'), { hints: [], class_hint: null }, 'a row without class_hints is still a row; an unknown sub-kind adds no structure key, an unknown tier no decision key');
assert.deepEqual(source.hintFor('tile', 'structure-only'), { hints: [], class_hint: null, structure: q.parseStructureKind(fragmentPayload) }, 'a structure row alone is still a hint (v1 row read as a fallback)');
assert.deepEqual(source.hintFor('tile', 'stacked'), { hints: [], class_hint: null, structure: q.parseStructureKind(stackPayload) }, 'a v2 row is read first');
assert.deepEqual([...q.STRUCTURE_FALLBACK_VERSIONS], ['structure-kinds-v2', 'structure-kinds-v1'], 'v3 first, then v2, then v1');
assert.deepEqual(source.hintFor('house', 'med-roof').structure.sub_kind, 'not-secondary', 'not-secondary is kept as data (it just earns no badge)');
assert.deepEqual(source.hintFor('tile', 'decided'), { hints: [], class_hint: null, decision: q.parseDecision(decisionPayload) }, 'a decision row alone is still a hint row');
assert.equal(source.hintFor('house', 'med-roof').decision.tier, 'review', 'class hints and a decision read together');
assert.deepEqual(source.hintFor('tile', 'upgraded').decision, q.parseDecision(decisionPayloadV2), 'a v2 decision row wins over the v1 row for the same object');
assert.equal(source.hintFor('tile', 'decided').decision.interval, null, 'a v1-only object reads its v1 row (no intervals) so nothing vanishes during a recompute');
assert.equal(q.DECISION_VERSION, 'decision-policy-v2'); assert.deepEqual(q.DECISION_FALLBACK_VERSIONS, ['decision-policy-v1']);
assert.equal(prepared, 8, 'one prepared statement per version (class hints, structure v3, structure v2 and v1 fallbacks, decision v2, decision v1 fallback, object hints v2 and its v1 fallback), reused across objects');
for (const sql of sqls) { assert.match(sql, /version\s*=\s*\?/); assert.match(sql, /scene_id\s*=\s*\?/); assert.match(sql, /object_id\s*=\s*\?/); }
assert.equal(sqls.filter(sql => /object_quality/.test(sql)).length, 6, "class hints, three structure versions and two object-hint versions read object_quality"); assert.equal(sqls.filter(sql => /decision_policy/.test(sql)).length, 2, 'the decision statements read decision_policy');
source.close(); assert.equal(closed, 1);
const attached = q.attachHints([{ scene_id: 'tile', object_id: 'low-roof', band: 'low' }, { scene_id: 'tile', object_id: 'missing', band: 'low' }, { scene_id: 'house', object_id: 'med-roof', band: 'high' }], q.indexHintSource(fakeDb));
assert.deepEqual(attached[0], { scene_id: 'tile', object_id: 'low-roof', band: 'low', hints: shedPayload.class_hints.flags, class_hint: 'outbuilding', structure: q.parseStructureKind(containerPayload), object_hints: q.parseObjectHints(objectHintsPayload), object_hints_served_version: 'object-hints-v2' }, 'attachHints places the structure hint on the row as structure and the object hints as object_hints');
assert.equal(attached[1].structure, undefined, 'rows without a structure row carry no structure key');
assert.deepEqual(attached[1], { scene_id: 'tile', object_id: 'missing', band: 'low' }, 'rows without an index row keep their exact shape');
assert.equal(attached[2].class_hint, 'commercial');
assert.equal(attached[2].decision.tier, 'review', 'attachHints places the decision on the row as decision');
assert.equal(attached[0].decision, undefined, 'rows without a decision row carry no decision key');
assert.equal(closed, 2, 'attachHints closes the source it was given');
// A database error on one version never hides the other version's hint.
const flaky = { prepare: () => ({ get: version => { if (version === 'structure-kinds-v1') throw new Error('SQLITE_ERROR: no such column'); return { payload: JSON.stringify(shedPayload) }; } }), close: () => {} };
assert.deepEqual(q.indexHintSource(flaky).hintFor('tile', 'low-roof'), { hints: shedPayload.class_hints.flags, class_hint: 'outbuilding' }, 'a failing structure query leaves the class hints intact');
const flakyFlags = { prepare: () => ({ get: version => { if (version === 'building-flags-v2') throw new Error('SQLITE_ERROR'); return { payload: JSON.stringify(fragmentPayload) }; } }), close: () => {} };
assert.deepEqual(q.indexHintSource(flakyFlags).hintFor('tile', 'x'), { hints: [], class_hint: null, structure: q.parseStructureKind(fragmentPayload) }, 'a failing class query leaves the structure hint intact');
assert.throws(() => q.indexHintReader(flaky).read('tile', 'low-roof'), /SQLITE_ERROR/, 'the reader itself reports database errors so a route can answer 500');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'structure-only'), { class_hints: null, structure: q.parseStructureKind(fragmentPayload), decision: null, object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null }, 'the reader separates the versions');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'missing'), { class_hints: null, structure: null, decision: null, object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null });
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'decided'), { class_hints: null, structure: null, decision: q.parseDecision(decisionPayload), object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null });
// Staleness: the fake serves every row under 'sig-a'; a caller naming that signature is current, another signature is stale, no signature never is.
assert.equal(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-a').stale, false, 'rows under the caller\'s signature are current');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-b').stale, true, 'a served row under another signature marks the answer stale');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-b').stale_reasons, ['model_signature'], 'the reason names the image version, not the rules (the v2 row was served)');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-a').stale_reasons, [], 'current rows under the current rules: no reason');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'missing', 'sig-b').stale, false, 'nothing served, nothing stale');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'low-roof').stale, false, 'without a caller signature staleness is not judged');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-a').object_hints, q.parseObjectHints(objectHintsPayload), 'the reader serves the object hints');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'broken').object_hints, null, 'an unparseable object-hints payload is null, not fatal');
assert.equal(q.OBJECT_HINTS_VERSION, 'object-hints-v2'); assert.deepEqual([...q.OBJECT_HINTS_FALLBACK_VERSIONS], ['object-hints-v1']); assert.equal(q.LOT_TAGS_VERSION, 'lot-tags-v1');
// object-hints-v2 over v1: an object with only a v1 row is still served (nothing vanishes during a recompute) and the answer is stale under a
// named signature (the row predates the current rules); where both exist the v2 row wins and nothing is stale.
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'v1-only', 'sig-a').object_hints, q.parseObjectHints(objectHintsV1Payload), 'a v1-only object serves its v1 row');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'v1-only', 'sig-a').stale, true, 'an older object-hints version marks the answer stale even under the caller\'s signature');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'v1-only').stale, true, 'an older object-hints version is stale whether or not the caller names a signature');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'v1-only').stale_reasons, ['object_hints_version']);
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'v1-only', 'sig-a').stale_reasons, ['object_hints_version'], 'the rows sit under the caller\'s signature: only the rules are older');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'v1-only', 'sig-b').stale_reasons, ['model_signature', 'object_hints_version'], 'both reasons, image version first');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'v1-only', 'sig-a').object_hints_served_version, 'object-hints-v1', 'the served version is the row\'s, not the module constant');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-a').object_hints_served_version, 'object-hints-v2');
assert.equal(q.indexHintReader(fakeDb).read('tile', 'missing').object_hints_served_version, null, 'no object-hints row, no served version');
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-a').object_hints.hints.map(h => h.code), ['noisy_outline', 'lot_incomplete_in_frame', 'frame_cut'], 'the v2 row wins over the v1 row');
assert.deepEqual(q.indexHintSource(fakeDb).hintFor('tile', 'v1-only'), { hints: [], class_hint: null, object_hints: q.parseObjectHints(objectHintsV1Payload), object_hints_served_version: 'object-hints-v1' }, 'a v1 row alone is still a hint row for /queues, and says which version it came from');
// The decision_policy table may not exist yet: its statement fails on prepare, and neither the source nor the reader lets that hide the other hints.
let decisionPrepares = 0;
const noDecisionTable = { prepare: sql => { if (/decision_policy/.test(sql)) { decisionPrepares++; throw new Error('SQLITE_ERROR: no such table: decision_policy'); } return fakeDb.prepare(sql); }, close: () => {} };
assert.deepEqual(q.indexHintSource(noDecisionTable).hintFor('tile', 'low-roof'), { hints: shedPayload.class_hints.flags, class_hint: 'outbuilding', structure: q.parseStructureKind(containerPayload), object_hints: q.parseObjectHints(objectHintsPayload), object_hints_served_version: 'object-hints-v2' }, 'a missing decision_policy table hides neither class, structure nor object hints');
assert.equal(q.indexHintSource(noDecisionTable).hintFor('tile', 'decided'), null, 'without the table a decision-only object has no hints');
assert.deepEqual(q.indexHintReader(noDecisionTable).read('tile', 'low-roof'), { class_hints: q.parseClassHints(shedPayload), structure: q.parseStructureKind(containerPayload), decision: null, object_hints: q.parseObjectHints(objectHintsPayload), lot: null, stale: false, stale_reasons: [], object_hints_served_version: 'object-hints-v2' }, 'the reader guards the decision on its own so the route still answers 200');
assert.throws(() => q.indexHintReader(noDecisionTable).decision('tile', 'low-roof'), /no such table/, 'the decision reader itself still reports the database error');
assert(decisionPrepares >= 1, 'the decision statement was attempted');
const flakyDecision = { prepare: sql => ({ get: () => { if (/decision_policy/.test(sql)) return { payload: JSON.stringify(decisionPayload) }; throw new Error('SQLITE_ERROR'); } }), close: () => {} };
assert.deepEqual(q.indexHintSource(flakyDecision).hintFor('tile', 'x'), { hints: [], class_hint: null, decision: q.parseDecision(decisionPayload) }, 'failing hint queries leave the decision intact');
assert.deepEqual(q.attachHints([{ scene_id: 'tile', object_id: 'low-roof' }], null), [{ scene_id: 'tile', object_id: 'low-roof' }], 'no source, no change');
// The calibration reader picks the source up from its IO; the hidden reader takes it as an argument.
const ioWithHints = { ...io, hints: () => q.indexHintSource(fakeDb) };
files.set(`data/scenes/tile/latest.json`, JSON.stringify(latest([model])));
files.set(`data/reviews/eyes/calibration/${PLAN}/plan.json`, JSON.stringify({ version: 'reference-calibration-v1', plan_id: PLAN, created_at: '2026-09-25T14:05:04.554677+00:00', iou_success: 0.85, bands: ['low', 'medium', 'high'],
  rows: [{ band: 'low', group: 'g', scene_id: 'tile', object_id: 'low-roof', area_m2: 63.379, detector_score: 0.5 }, { band: 'low', group: 'g2', scene_id: 'tile', object_id: 'missing', area_m2: 100, detector_score: 0.6 }] }));
queue = q.buildCalibrationQueue(ioWithHints, PLAN);
assert.equal(queue.hints_version, 'building-flags-v2'); assert.match(queue.hints_note, /not labels?/i);
assert.deepEqual(queue.rows[0].hints, shedPayload.class_hints.flags); assert.equal(queue.rows[0].class_hint, 'outbuilding');
assert.equal(queue.rows[0].structure.sub_kind, 'shipping-container', 'calibration rows carry the structure hint');
assert.equal(queue.rows[0].status, 'pending', 'the hint never changes the live status');
assert(!('hints' in queue.rows[1]) && !('class_hint' in queue.rows[1]), 'rows without an index row carry no hint keys');
queue = q.buildCalibrationQueue(io, PLAN);
assert(!('hints' in queue.rows[0]), 'an IO without an index yields no hint keys');
assert.equal(queue.hints_version, 'building-flags-v2');
const hiddenWithHints = q.hiddenQueue({ version: 'cross-capture-v1', rows: [{ scene_id: 'tile', object_id: 'low-roof', other_scene_id: 'house', other_object_id: 'med-roof' }, { scene_id: 'tile', object_id: 'missing', other_scene_id: 'house', other_object_id: 'x' }] }, q.indexHintSource(fakeDb));
assert.equal(hiddenWithHints.rows[0].class_hint, 'outbuilding'); assert.deepEqual(hiddenWithHints.rows[0].hints.map(h => h.flag), ['secondary-structure']);
assert.equal(hiddenWithHints.rows[0].structure.confidence, 'moderate', 'hidden rows carry the structure hint');
assert(!('hints' in hiddenWithHints.rows[1]), 'hidden rows without an index row keep the documented shape');
assert.match(hiddenWithHints.hints_note, /not labels?/i);
// pickHints reads hints back off an API document for the client, keyed by scene/object.
const picked = q.pickHints({ rows: [{ scene_id: 'tile', object_id: 'low-roof', hints: shedPayload.class_hints.flags, class_hint: 'outbuilding' }, { scene_id: 'tile', object_id: 'plain' }, { scene_id: 'tile', object_id: 'odd', hints: 'x', class_hint: 5 }, 'junk'] });
assert.deepEqual([...picked.keys()], ['tile/low-roof', 'tile/odd']);
assert.deepEqual(picked.get('tile/low-roof'), { hints: shedPayload.class_hints.flags, class_hint: 'outbuilding' });
assert.deepEqual(picked.get('tile/odd'), { hints: [], class_hint: null });
assert.equal(q.pickHints(null).size, 0);
const pickedStructure = q.pickHints({ rows: [{ scene_id: 'tile', object_id: 'c', structure: q.parseStructureKind(containerPayload) }, { scene_id: 'tile', object_id: 'bad', structure: { sub_kind: 'zebra' } }, { scene_id: 'tile', object_id: 'both', hints: shedPayload.class_hints.flags, class_hint: 'outbuilding', structure: q.parseStructureKind(fragmentPayload) }] });
assert.deepEqual(pickedStructure.get('tile/c'), { hints: [], class_hint: null, structure: q.parseStructureKind(containerPayload) }, 'pickHints carries structure back to the client');
assert.deepEqual(pickedStructure.get('tile/bad'), { hints: [], class_hint: null }, 'a malformed structure is dropped, not thrown');
assert.equal(pickedStructure.get('tile/both').structure.sub_kind, 'fragment');
const pickedServed = q.pickHints({ rows: [{ scene_id: 'tile', object_id: 'v1', object_hints: objectHintsV1Payload, object_hints_served_version: 'object-hints-v1' }, { scene_id: 'tile', object_id: 'bad', object_hints: objectHintsV1Payload, object_hints_served_version: 42 }] });
assert.equal(pickedServed.get('tile/v1').object_hints_served_version, 'object-hints-v1', 'pickHints carries the served object-hints version back to /queues');
assert(!('object_hints_served_version' in pickedServed.get('tile/bad')), 'a malformed version is dropped');

// --- Structure kinds (structure-kinds-v3): parse, badges, titles, flagged semantics, summaries -------------
assert.equal(q.STRUCTURE_VERSION, 'structure-kinds-v3');
assert.match(q.STRUCTURE_NOTE, /not labels?/i); assert(q.STRUCTURE_NOTE.includes('structure-kinds-v3'));
assert.deepEqual(q.STRUCTURE_KINDS, ['shipping-container', 'container-stack', 'shed', 'garage', 'carport', 'attached-cover', 'shade-sail', 'pergola-or-shade', 'water-tank', 'portable-classroom', 'pool-cover', 'fragment', 'outbuilding', 'not-secondary']);  // shade-sail added 2026-10-01 (Codex shade-sail session)
let structure = q.parseStructureKind(containerPayload);
assert.deepEqual(structure, { sub_kind: 'shipping-container', confidence: 'moderate', rule: containerPayload.rule, evidence: containerPayload.evidence });
assert.deepEqual(q.parseStructureKind(JSON.stringify(containerPayload)), structure, 'payload text is parsed');
assert.equal(q.parseStructureKind({ sub_kind: 'shed' }).confidence, 'weak', 'a missing confidence is the weakest claim');
assert.deepEqual(q.parseStructureKind({ sub_kind: 'shed', confidence: 'certain', rule: 7, evidence: 'x' }), { sub_kind: 'shed', confidence: 'weak', rule: '', evidence: {} }, 'unknown confidence, non-string rule and non-object evidence degrade, never throw');
for (const bad of [null, undefined, 'nope', '{not json', 42, {}, [], { sub_kind: 'zebra', confidence: 'strong' }, { sub_kind: 7 }, { kind: 'building', class_hints: {} }]) assert.equal(q.parseStructureKind(bad), null, `unknown or malformed structure is null: ${JSON.stringify(bad)}`);
for (const kind of q.STRUCTURE_KINDS) assert.equal(q.parseStructureKind({ sub_kind: kind, confidence: 'strong' }).sub_kind, kind);
// Badges: one per resolved sub-kind, sentence-case labels, titles start with the not-a-label copy then confidence and plain-words evidence.
badges = q.hintBadges({ hints: [], class_hint: null, structure });
assert.equal(badges.length, 1);
assert.deepEqual({ key: badges[0].key, label: badges[0].label, group: badges[0].group }, { key: 'shipping-container', label: 'Shipping container', group: 'structure' });
assert.equal(badges[0].title, 'Agent hint, not a label. Shipping container, moderate confidence: 2.4 by 6.1 m, rib anisotropy 0.42, not attached to a larger roof, SAM3 house score 0.12.');
const labels = { 'shipping-container': 'Shipping container', 'container-stack': 'Container stack', shed: 'Shed', garage: 'Garage', carport: 'Carport', 'attached-cover': 'Patio or carport cover', 'shade-sail': 'Fabric shade sail', 'pergola-or-shade': 'Pergola or shade', 'water-tank': 'Water tank', 'portable-classroom': 'Portable classroom', 'pool-cover': 'Pool cover', fragment: 'Fragment' };
assert.deepEqual(Object.keys(labels).sort(), q.STRUCTURE_KINDS.filter(kind => !['outbuilding', 'not-secondary'].includes(kind)).sort(), 'every kind that says something has a label');
for (const [kind, label] of Object.entries(labels)) {
  const [badge] = q.hintBadges({ hints: [], class_hint: null, structure: { ...structure, sub_kind: kind, confidence: 'strong' } });
  assert.equal(badge.label, label); assert.equal(badge.key, kind); assert.equal(badge.group, 'structure');
  assert(badge.title.startsWith(`Agent hint, not a label. ${label}, strong confidence: `), badge.title);
  assert(!/[A-Z]{4,}/.test(badge.title) && !badge.title.includes(' · ') && !badge.title.includes('→'), `quiet title: ${badge.title}`);
}
for (const kind of ['outbuilding', 'not-secondary']) assert.deepEqual(q.hintBadges({ hints: [], class_hint: null, structure: { ...structure, sub_kind: kind } }), [], `${kind} earns no badge`);
badges = q.hintBadges({ ...q.parseClassHints(shedPayload), structure });
assert.deepEqual(badges.map(b => [b.label, b.group]), [['Secondary structure', 'class'], ['Shipping container', 'structure']], 'class badges keep their order; the structure badge follows');
const fragmentBadge = q.hintBadges({ hints: [], class_hint: null, structure: q.parseStructureKind(fragmentPayload) })[0];
assert.equal(fragmentBadge.key, 'fragment', 'fragment badges carry a semantic key for quiet styling');
assert.equal(fragmentBadge.title, 'Agent hint, not a label. Fragment, weak confidence: 3 m², attached to a larger roof.', 'without a rectangle the area stands in; no SAM3 means no score');
assert.equal(q.describeStructure({ sub_kind: 'shed', confidence: 'weak', rule: 'r', evidence: {} }), 'rule r', 'empty evidence falls back to the rule');
// v2 kinds carry their own measures: container widths for a stack, the blue share for a pool cover, the driveway share and attachment for a carport.
assert.equal(q.describeStructure(q.parseStructureKind(stackPayload)), '4.9 by 12.2 m, about 2 container widths, rib anisotropy 0.42, not attached to a larger roof, SAM3 house score 0.12', 'a container stack states the rectangle and how many container widths');
assert.equal(q.describeStructure({ sub_kind: 'container-stack', confidence: 'weak', rule: '', evidence: { geometry: { rect_width_m: 2.5, rect_length_m: 6 } } }), '2.5 by 6.0 m, about 1 container width', 'without a recorded count the width stands in (2.44 m per container)');
assert.equal(q.describeStructure({ sub_kind: 'pool-cover', confidence: 'moderate', rule: '', evidence: { geometry: { rect_width_m: 4, rect_length_m: 9 }, appearance: { blue_share: 0.72 }, context: { attached_to_larger_roof: false } } }), '4.0 by 9.0 m, blue share 0.72 of pixels, not attached to a larger roof', 'a pool cover states its blue share');
assert.equal(q.describeStructure({ sub_kind: 'carport', confidence: 'weak', rule: '', evidence: { geometry: { rect_width_m: 3, rect_length_m: 5.5 }, appearance: { driveway_like_share_3m: 0.45, see_through_ratio: 0.3 }, context: { attached_to_larger_roof: true } } }), '3.0 by 5.5 m, driveway-like share 0.45 within 3 m, see-through ratio 0.30, attached to a larger roof', 'a carport states the driveway share and attachment');
assert.equal(q.describeStructure({ sub_kind: 'shed', confidence: 'weak', rule: '', evidence: { appearance: { blue_share: 0.9, driveway_like_share_3m: 0.9 }, geometry: { area_m2: 12 } } }), '12 m²', 'kind-specific measures stay with their kind');
assert.equal(q.describeStructure({ sub_kind: 'portable-classroom', confidence: 'strong', rule: '', evidence: { geometry: { rect_width_m: 7.2, rect_length_m: 14.4 } } }), '7.2 by 14.4 m');
const stackBadge = q.hintBadges({ hints: [], class_hint: null, structure: q.parseStructureKind(stackPayload) })[0];
assert.equal(stackBadge.title, 'Agent hint, not a label. Container stack, strong confidence: 4.9 by 12.2 m, about 2 container widths, rib anisotropy 0.42, not attached to a larger roof, SAM3 house score 0.12.');
// isFragment: only the fragment sub-kind; /queues uses it to keep fragment rows out of view by default.
assert.equal(q.isFragment({ structure: q.parseStructureKind(fragmentPayload) }), true);
assert.equal(q.isFragment({ structure }), false); assert.equal(q.isFragment({ structure: null }), false); assert.equal(q.isFragment({}), false);
assert.equal(q.describeStructure({ sub_kind: 'shed', confidence: 'weak', rule: '', evidence: { appearance: { see_through_ratio: 0.4 } } }), 'see-through ratio 0.40');
assert.equal(q.describeStructure({ sub_kind: 'shed', confidence: 'weak', rule: '', evidence: { sam3: { prompts: { 'house roof': { best_iou: 0.5 } } } } }), 'SAM3 house score 0.50', 'best_iou stands in when a house prompt has no score');
// isFlagged: a resolved sub-kind counts as flagged so Hide flagged removes it; the neutral kinds do not.
assert.equal(q.isFlagged({ hints: [], class_hint: null, structure }), true);
assert.equal(q.isFlagged({ hints: [], class_hint: null, structure: q.parseStructureKind(fragmentPayload) }), true);
assert.equal(q.isFlagged({ hints: [], class_hint: 'house', structure: { ...structure, sub_kind: 'not-secondary' } }), false);
assert.equal(q.isFlagged({ hints: [], class_hint: 'outbuilding', structure: { ...structure, sub_kind: 'outbuilding' } }), false);
assert.equal(q.isFlagged({ hints: [], class_hint: null, structure: null }), false);
assert.equal(q.isFlagged({ hints: [], class_hint: null }), false);
// structureSummary: only non-zero kinds, sentence case, plurals.
const rowsFor = kinds => kinds.map(kind => ({ hints: [], class_hint: null, structure: kind ? { ...structure, sub_kind: kind } : undefined }));
assert.equal(q.structureSummary(rowsFor(['shipping-container', 'shipping-container', 'shed', 'garage', 'garage', 'garage', 'carport', 'fragment', 'not-secondary', 'outbuilding', null])), 'Structure hints: 2 shipping containers, 1 shed, 3 garages, 1 carport, 1 fragment');
assert.equal(q.structureSummary(rowsFor(['water-tank', 'pergola-or-shade', 'pergola-or-shade'])), 'Structure hints: 2 pergola or shade structures, 1 water tank');
assert.equal(q.structureSummary(rowsFor(['pool-cover', 'container-stack', 'container-stack', 'portable-classroom', 'shipping-container'])), 'Structure hints: 1 shipping container, 2 container stacks, 1 portable classroom, 1 pool cover', 'the count line covers the v2 kinds');
assert.equal(q.structureSummary(rowsFor(['not-secondary', null])), null, 'nothing to count means no line');
assert.equal(q.structureSummary([]), null);
// The editor's line and the route body coercion.
assert.equal(q.describeObjectHints({ class_hints: q.parseClassHints(shedPayload), structure }), 'Secondary structure. Shipping container (moderate). Not labels; change the Class above if you disagree.');
assert.equal(q.describeObjectHints({ class_hints: null, structure: q.parseStructureKind(fragmentPayload) }), 'Fragment (weak). Not labels; change the Class above if you disagree.');
assert.equal(q.describeObjectHints({ class_hints: q.parseClassHints(housePayload), structure: { ...structure, sub_kind: 'not-secondary' } }), null, 'a house with nothing to flag renders nothing');
assert.equal(q.describeObjectHints({ class_hints: null, structure: null }), null);
const none = { class_hints: null, structure: null, decision: null, object_hints: null, lot: null, stale: false, stale_reasons: [], object_hints_served_version: null, lot_frame: null };
assert.deepEqual(q.coerceObjectHints({ scene_id: 's', object_id: 'o', class_hints: { hints: shedPayload.class_hints.flags, class_hint: 'outbuilding' }, structure: containerPayload }), { ...none, class_hints: q.parseClassHints(shedPayload), structure });
assert.deepEqual(q.coerceObjectHints({ class_hints: null, structure: null }), none);
assert.deepEqual(q.coerceObjectHints('junk'), none);
assert.deepEqual(q.coerceObjectHints({ class_hints: 'x', structure: { sub_kind: 'zebra' }, decision: { tier: 'auto' } }), none, 'a decision without a confidence is null');
assert.deepEqual(q.coerceObjectHints({ class_hints: null, structure: null, decision: decisionPayload }), { ...none, decision: q.parseDecision(decisionPayload) });
// The scene-level lot-frame sentence (lot-tags-v1 "_scene" row): text only, parsed from the payload or the route body, never from an error.
const lotFrameText = 'A lot frame for 30371 would be 88 m at 0.17 m/px (Google z20, 512 px), covering Lots 1-4 + CM1 of PS824596; coarser than this 0.065 m/px crop.';
assert.equal(q.SCENE_ROW_ID, '_scene');
assert.equal(q.parseLotFrameSentence({ version: 'lot-tags-v1', scene: { address_id: 30371, lot_frame: { version: 'lot-frame-v1', sentence: lotFrameText } } }), lotFrameText);
assert.equal(q.parseLotFrameSentence(JSON.stringify({ scene: { lot_frame: { sentence: lotFrameText } } })), lotFrameText, 'JSON text is parsed too');
assert.equal(q.parseLotFrameSentence({ scene: { lot_frame: { error: 'Address not found' } } }), null, 'a recorded error is not a sentence');
assert.equal(q.parseLotFrameSentence({ scene: { lot_frame: null } }), null); assert.equal(q.parseLotFrameSentence({ tag: { flags: [] } }), null); assert.equal(q.parseLotFrameSentence('junk'), null);
assert.deepEqual(q.coerceObjectHints({ class_hints: null, structure: null, lot_frame: lotFrameText }), { ...none, lot_frame: lotFrameText }, 'the route body carries the sentence as text');
assert.deepEqual(q.coerceObjectHints({ class_hints: null, structure: null, lot_frame: 7 }), none, 'a non-string lot_frame is null');
// Staleness reasons and the served object-hints version ride on the route body and survive the coercer; unknown reasons and malformed versions drop.
assert.deepEqual(q.HINT_STALE_REASONS, ['model_signature', 'object_hints_version']);
assert.deepEqual(q.coerceObjectHints({ class_hints: null, structure: null, stale: true, stale_reasons: ['object_hints_version', 'zebra', 7, 'model_signature'], object_hints_served_version: 'object-hints-v1' }),
  { ...none, stale: true, stale_reasons: ['model_signature', 'object_hints_version'], object_hints_served_version: 'object-hints-v1' }, 'known reasons in their fixed order');
assert.deepEqual(q.coerceObjectHints({ class_hints: null, structure: null, stale_reasons: 'model_signature', object_hints_served_version: 'v1; drop table' }), none, 'a non-array reason list and a malformed version are dropped');
// The editor's stale notices: one per reason; the rules notice only where the older version's rules differ for the object's kind.
assert.deepEqual(q.hintStaleNotes({ stale: false }), []);
assert.deepEqual(q.hintStaleNotes({ stale: true, stale_reasons: ['model_signature'] }, 'building'), ['Hints computed on an earlier image version.']);
assert.deepEqual(q.hintStaleNotes({ stale: true }, 'building'), ['Hints computed on an earlier image version.'], 'an answer without reasons (an older server) reads as the image-version notice');
const v1Answer = { stale: true, stale_reasons: ['object_hints_version'], object_hints_served_version: 'object-hints-v1' };
assert.deepEqual(q.hintStaleNotes(v1Answer, 'solar_panel'), ['Object hints predate the current rules (object-hints-v1): the solar-panel check is not included.']);
assert.deepEqual(q.hintStaleNotes(v1Answer, 'building'), [], 'object-hints-v2 changed nothing for buildings: a v1 row says what v2 would, so no notice');
assert.deepEqual(q.hintStaleNotes(v1Answer), ['Object hints predate the current rules (object-hints-v1): the solar-panel check is not included.'], 'an unknown kind gets the notice');
assert.deepEqual(q.hintStaleNotes({ stale: true, stale_reasons: ['model_signature', 'object_hints_version'], object_hints_served_version: 'object-hints-v0' }, 'building'),
  ['Hints computed on an earlier image version.', 'Object hints predate the current rules (object-hints-v0).'], 'a version the table does not describe is noted for every kind');
assert.deepEqual(Object.keys(q.OBJECT_HINTS_RULE_CHANGES), [...q.OBJECT_HINTS_FALLBACK_VERSIONS], 'every fallback version says what it lacks under the current rules');
assert(q.hintStaleNotes(v1Answer, 'solar_panel').every(n => !/accura|label/i.test(n)), 'notices say when hints were computed, never how good they are');
assert.equal(q.indexHintReader(fakeDb).lotFrame('tile'), null, 'no scene row in the index, no lot-frame sentence');
assert.equal(q.indexHintReader(fakeDb).lotFrame('tile', 'sig-a'), null);
assert.deepEqual(q.indexHintReader(fakeDb).read('tile', 'low-roof', 'sig-a').lot_frame, undefined, 'read() is unchanged; the route composes lot_frame beside it');
// --- object-hints-v2 and lot-tags-v1: parsers, sentences, badges, staleness on the body ------------------------------------
const parsedHints = q.parseObjectHints(objectHintsPayload);
assert.deepEqual(parsedHints, { hints: objectHintsPayload.hints, squared_candidate: { ring: null, iou: null, hausdorff_m: null, refused: 'iou 0.81 below 0.85' } }, 'hints and the refused candidate read back');
assert.deepEqual(q.parseObjectHints(JSON.stringify(objectHintsPayload)), parsedHints, 'JSON text parses the same');
assert.deepEqual(q.parseObjectHints({ hints: [{ code: 'frame_cut' }, { code: 7 }, 'x', { code: 'noisy_outline', severity: 'loud', sentence: 3 }], squared_candidate: { ring: [[0, 0], [10, 0], [10, 10], [0, 10]], iou: 0.91, hausdorff_m: 0.6 } }),
  { hints: [{ code: 'frame_cut', severity: 'context', sentence: '', evidence: {} }, { code: 'noisy_outline', severity: 'context', sentence: '', evidence: {} }], squared_candidate: { ring: [[0, 0], [10, 0], [10, 10], [0, 10]], iou: 0.91, hausdorff_m: 0.6, refused: null } }, 'malformed hints drop, an unknown severity reads as context, a ring candidate keeps its numbers');
assert.equal(q.parseObjectHints({ hints: 'x' }), null, 'no hints array and no candidate is no row');
assert.equal(q.parseObjectHints('{not json'), null); assert.equal(q.parseObjectHints(null), null); assert.equal(q.parseObjectHints([]), null);
assert.deepEqual(q.parseObjectHints({ hints: [], squared_candidate: { ring: [[0, 0], [1, 'x'], [2, 2], [3, 3]] } }), { hints: [], squared_candidate: null }, 'a ring with a non-numeric pair is no candidate');
assert.deepEqual(q.parseSquaredCandidate({ ring: [[0, 0], [1, 1]] }), null, 'two points and no refusal is nothing');
assert.deepEqual(q.parseSquaredCandidate({ ring: [[0, 0], [1, 0], [1, 1]], iou: 1.5, hausdorff_m: -1 }), { ring: [[0, 0], [1, 0], [1, 1]], iou: null, hausdorff_m: null, refused: null }, 'out-of-range agreement numbers read as null');
assert.deepEqual(q.objectHintSentences(objectHintsPayload.hints), ["54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side.", 'Touches the image edge; the frame cut it, not the building.', 'Lot 2 PS747728 leaves this image.'], 'review sentences first, then context, order kept within each');
assert.deepEqual(q.objectHintSentences([{ code: 'a', severity: 'review', sentence: ' no stop ', evidence: {} }, { code: 'b', severity: 'review', sentence: 'no stop.', evidence: {} }, { code: 'c', severity: 'context', sentence: '', evidence: {} }]), ['no stop.'], 'trimmed, one full stop, repeats and blanks dropped');
assert.deepEqual(q.objectHintSentences(null), []);
const lotPayload = { version: 'lot-tags-v1', tag: { dominant: { pfi: '1348074', lot: '7', plan: 'LP34966', share: 0.97, kind: 'lot' }, lots: [{ lot: '7', plan: 'LP34966', share: 0.97 }, { lot: '8', plan: 'LP34966', share: 0.03 }], lot_complete_in_image: false, address_plan: 'PS824596', flags: ['frame_cut', 'lot_unassociated'],
  sentences: ['Touches the image edge; the frame cut it, not the building.', 'On neighbouring Lot 7 LP34966, not the address lot; cut by the frame'] }, parcels_sha256: 'b'.repeat(64) };
const lot = q.parseLotTag(lotPayload);
assert.deepEqual(lot, { dominant: { pfi: '1348074', lot: '7', plan: 'LP34966', share: 0.97, kind: 'lot' }, lots: lotPayload.tag.lots, flags: ['frame_cut', 'lot_unassociated'], sentences: lotPayload.tag.sentences, lot_complete_in_image: false, address_plan: 'PS824596' });
assert.deepEqual(q.parseLotTag(lot), lot, 'the route\'s flat shape reads back to itself');
assert.deepEqual(q.parseLotTag({ tag: { dominant: { kind: 'strata' }, lots: [{ share: 2 }], flags: [3, 'x'], sentence: 'One sentence' } }), { dominant: { pfi: null, lot: null, plan: null, share: null, kind: null }, lots: [], flags: ['x'], sentences: ['One sentence'], lot_complete_in_image: null, address_plan: null }, 'unknown kinds, out-of-range shares and non-strings degrade to null or drop');
assert.equal(q.parseLotTag({ version: 'lot-tags-v1' }), null, 'a record without any tag field is null'); assert.equal(q.parseLotTag('junk'), null); assert.equal(q.parseLotTag(null), null);
assert.equal(q.lotSentence(lot), 'Touches the image edge; the frame cut it, not the building. On neighbouring Lot 7 LP34966, not the address lot; cut by the frame.');
assert.equal(q.lotSentence({ ...lot, sentences: [] }), null); assert.equal(q.lotSentence(null), null);
assert.equal(q.describeObjectHints({ class_hints: q.parseClassHints(shedPayload), structure, object_hints: parsedHints, lot }),
  "Secondary structure. Shipping container (moderate). 54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side. Touches the image edge; the frame cut it, not the building. Lot 2 PS747728 leaves this image. Touches the image edge; the frame cut it, not the building. On neighbouring Lot 7 LP34966, not the address lot; cut by the frame. Not labels; change the Class above if you disagree.",
  'class parts, then review then context object sentences, then the lot sentence, closed by the not-labels line');
assert.equal(q.describeObjectHints({ class_hints: null, structure: null, decision: null, object_hints: parsedHints, lot: null }), "54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side. Touches the image edge; the frame cut it, not the building. Lot 2 PS747728 leaves this image. Not labels; change the Class above if you disagree.", 'object hints alone still close with the not-labels line');
assert.equal(q.describeObjectHints({ class_hints: null, structure: null, decision: null, object_hints: { hints: [], squared_candidate: null }, lot: null }), null, 'a row with no hint and no lot says nothing');
assert.equal(q.describeObjectHints({ class_hints: null, structure: null, decision: null, object_hints: null, lot }), 'Touches the image edge; the frame cut it, not the building. On neighbouring Lot 7 LP34966, not the address lot; cut by the frame. Not labels; change the Class above if you disagree.', 'a lot sentence alone');
for (const line of [q.describeObjectHints({ class_hints: null, structure: null, decision: null, object_hints: parsedHints, lot })]) assert(!line.includes(' · ') && !line.includes('→') && !/[A-Z]{4,}/.test(line.replace(/LP\d+|PS\d+/g, '')), `quiet copy: ${line}`);
badges = q.hintBadges({ hints: [], class_hint: null, object_hints: parsedHints });
assert.deepEqual(badges.map(b => [b.key, b.label, b.group]), [['noisy_outline', 'Noisy outline', 'object'], ['frame_cut', 'Frame cut', 'object']], 'badges for the /queues codes only (lot_incomplete_in_frame stays in the editor list)');
assert.equal(badges[0].title, "Agent hint, not a label. 54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side.");
for (const code of ['frame_cut', 'noisy_outline', 'pool_not_blue', 'building_blends_into_surroundings', 'canopy_fragment_inside', 'secondary_by_lot', 'spans_lots', 'solar_fixture_like']) {
  const [badge] = q.hintBadges({ hints: [], class_hint: null, object_hints: { hints: [{ code, severity: 'review', sentence: '', evidence: {} }], squared_candidate: null } });
  assert.equal(badge.key, code); assert.equal(badge.label, q.OBJECT_HINT_LABELS[code]); assert.equal(badge.group, 'object'); assert.equal(badge.title, `Agent hint, not a label. ${code.replace(/_/g, ' ')}`, 'an empty sentence falls back to the code in words');
}
assert.deepEqual(q.hintBadges({ hints: [], class_hint: null, object_hints: { hints: [{ code: 'tiny_area_for_kind', severity: 'review', sentence: 'x', evidence: {} }, { code: 'noisy_outline', severity: 'review', sentence: 'a', evidence: {} }, { code: 'noisy_outline', severity: 'review', sentence: 'b', evidence: {} }], squared_candidate: null } }).map(b => b.key), ['noisy_outline'], 'codes without a label earn no badge; a repeated code earns one');
badges = q.hintBadges({ ...q.parseClassHints(shedPayload), structure, object_hints: parsedHints, decision: q.parseDecision(decisionPayload) });
assert.equal(q.OBJECT_HINT_LABELS.solar_fixture_like, 'Panel reads bright or irregular', 'a measurement, like "Pool reads green", never what the object might be');
// Object hints served from an older version say so in their badge titles; titles from current rows are unchanged.
const olderBadges = q.hintBadges({ hints: [], class_hint: null, object_hints: parsedHints, object_hints_served_version: 'object-hints-v1' });
assert.equal(olderBadges[0].title, "Agent hint, not a label, from earlier hint rules (object-hints-v1). 54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side.");
assert.equal(q.hintBadges({ hints: [], class_hint: null, object_hints: parsedHints, object_hints_served_version: 'object-hints-v2' })[0].title, "Agent hint, not a label. 54 corners along 43 m of edge, 49 of them off the roof's axes; roof edges are usually straight. Check the shadow side.");
assert.deepEqual(badges.map(b => b.group), ['class', 'structure', 'object', 'object', 'decision'], 'class, structure, object and decision badges in that order');

// --- Decision policy (decision-policy-v1): parse, describe, badge, count line, auto tier, reviewer roof edits ---------
assert.equal(q.DECISION_VERSION, 'decision-policy-v2');
assert.match(q.DECISION_NOTE, /not a reviewer label/i); assert(q.DECISION_NOTE.includes('decision-policy-v2') && q.DECISION_NOTE.includes('v1 rows'), 'the note names both versions');
assert.deepEqual(q.parseDecision({ tier: 'auto', confidence: 0.5, review: { status: 'confirmed' } }).review, { status: 'confirmed' }, 'a serialised parsed decision (route body) keeps its review status');
assert.deepEqual(q.DECISION_TIERS, ['auto', 'assist', 'review']);
assert.deepEqual(q.TIER_LABELS, { auto: 'Auto', assist: 'Assist', review: 'Review' });
let decision = q.parseDecision(decisionPayload);
assert.deepEqual(decision, { tier: 'auto', confidence: 0.91, machine_label: { kind: 'building', structure_sub_kind: 'garage', class_hint: 'outbuilding', roof_form: 'pitched', roof_type: 'gabled' }, reasons: ['small footprint beside the driveway', 'strong structure hint'], interval: null, score_interval: null, review: null }, 'only the fields the UI needs; a v1 row has no intervals and, without signals.review, no review data');
assert.deepEqual(q.parseDecision(JSON.stringify(decisionPayload)), decision, 'payload text is parsed');
for (const tier of q.DECISION_TIERS) assert.equal(q.parseDecision({ tier, confidence: 0.5 }).tier, tier);
for (const edge of [0, 1, 0.0001]) assert.equal(q.parseDecision({ tier: 'assist', confidence: edge }).confidence, edge, 'the confidence range is closed');
assert.deepEqual(q.parseDecision({ tier: 'review', confidence: 0.3 }), { tier: 'review', confidence: 0.3, machine_label: { kind: '', structure_sub_kind: null, class_hint: null, roof_form: null, roof_type: null }, reasons: [], interval: null, score_interval: null, review: null }, 'a missing label and reasons degrade to empty, never throw');
// v2 intervals: the agreement interval keeps its counts and bounds; null bounds when nothing was judged; malformed parts degrade, never throw.
const v2 = q.parseDecision(decisionPayloadV2);
assert.deepEqual(v2.interval, { kind: 'wilson-95', stratum: 'auto/garage', confirmed: 6, judged: 9, lower: 0.3536, upper: 0.8791, note: 'reviewer decisions in this stratum' });
assert.deepEqual(v2.score_interval, { lower: 0.8612, upper: 0.9488, note: 'model estimate range' });
assert.deepEqual(v2.review, { status: 'confirmed' }, 'signals.review.status is kept for the review-status column');
assert.deepEqual(q.parseDecision({ ...decisionPayloadV2, signals: { review: { status: null, present: false } } }).review, { status: null }, 'a recorded absence of review is a status of null, not missing data');
assert.deepEqual(q.parseAgreementInterval({ kind: 'wilson-95', stratum: 'auto/shed', confirmed: 0, judged: 0, lower: null, upper: null, note: 'n' }), { kind: 'wilson-95', stratum: 'auto/shed', confirmed: 0, judged: 0, lower: null, upper: null, note: 'n' }, 'null bounds when judged is 0');
assert.deepEqual(q.parseAgreementInterval({ stratum: 'auto/shed', confirmed: 3, judged: 0, lower: 0.2, upper: 0.9 }), { kind: 'wilson-95', stratum: 'auto/shed', confirmed: 0, judged: 0, lower: null, upper: null, note: '' }, 'bounds and confirmed are ignored when nothing was judged');
assert.deepEqual(q.parseAgreementInterval({ stratum: 'x', confirmed: 12, judged: 9, lower: 0.5, upper: 0.9 }).confirmed, 9, 'confirmed never exceeds judged');
for (const bad of [{ stratum: 'x', confirmed: 2, judged: 4, lower: 'a', upper: 0.9 }, { stratum: 'x', confirmed: 2, judged: 4, lower: 0.9, upper: 0.5 }, { stratum: 'x', confirmed: 2, judged: 4, lower: -0.1, upper: 0.5 }, { stratum: 'x', confirmed: 2, judged: 4, lower: 0.2 }])
  assert.deepEqual([q.parseAgreementInterval(bad).lower, q.parseAgreementInterval(bad).upper], [null, null], `malformed bounds are null: ${JSON.stringify(bad)}`);
assert.deepEqual(q.parseAgreementInterval({ kind: 7, stratum: 3, confirmed: 'x', judged: -1, note: null }), { kind: 'wilson-95', stratum: '', confirmed: 0, judged: 0, lower: null, upper: null, note: '' }, 'non-string, non-integer fields degrade');
for (const bad of [null, undefined, 'x', 4, [], '{"judged":1}']) assert.equal(q.parseAgreementInterval(bad), null, `a non-object interval is null: ${JSON.stringify(bad)}`);
assert.deepEqual(q.parseScoreInterval({ lower: 0.1, upper: 0.2 }), { lower: 0.1, upper: 0.2, note: '' });
for (const bad of [null, 'x', {}, { lower: 0.5 }, { lower: 0.9, upper: 0.5 }, { lower: -1, upper: 0.5 }, { lower: 0.1, upper: 1.5 }, { lower: '0.1', upper: 0.5 }]) assert.equal(q.parseScoreInterval(bad), null, `malformed score interval is null: ${JSON.stringify(bad)}`);
assert.deepEqual(q.parseDecision({ ...decisionPayloadV2, interval: 'x', score_interval: [] }).interval, null); assert.equal(q.parseDecision({ ...decisionPayloadV2, interval: 'x', score_interval: [] }).score_interval, null);
assert.deepEqual(q.parseDecision({ tier: 'auto', confidence: 0.9, machine_label: { kind: 7, structure_sub_kind: 3, roof_form: 'domed', roof_type: ['x'] }, reasons: ['ok', 4, '', null] }).machine_label, { kind: '', structure_sub_kind: null, class_hint: null, roof_form: null, roof_type: null }, 'non-string label fields and unknown roof forms are null');
assert.deepEqual(q.parseDecision({ tier: 'auto', confidence: 0.9, reasons: ['ok', 4, '', null] }).reasons, ['ok'], 'only non-empty string reasons survive');
for (const bad of [null, undefined, 'nope', '{not json', 42, {}, [], { tier: 'maybe', confidence: 0.9 }, { tier: 'auto' }, { tier: 'auto', confidence: 1.2 }, { tier: 'auto', confidence: -0.1 }, { tier: 'auto', confidence: '0.9' }, { tier: 'auto', confidence: NaN }, { confidence: 0.5 }, { tier: 7, confidence: 0.5 }])
  assert.equal(q.parseDecision(bad), null, `invalid decision is null: ${JSON.stringify(bad)}`);
// machineLabelName: the structure sub-kind first, then the class hint, then the kind, in sentence case.
assert.equal(q.machineLabelName(decision.machine_label), 'Garage');
assert.equal(q.machineLabelName({ kind: 'building', structure_sub_kind: 'shipping-container', class_hint: 'outbuilding', roof_form: null, roof_type: null }), 'Shipping container');
assert.equal(q.machineLabelName({ kind: 'building', structure_sub_kind: 'pergola-or-shade', class_hint: null, roof_form: null, roof_type: null }), 'Pergola or shade');
assert.equal(q.machineLabelName({ kind: 'building', structure_sub_kind: 'not-secondary', class_hint: 'house', roof_form: null, roof_type: null }), 'House', 'a neutral sub-kind defers to the class hint');
assert.equal(q.machineLabelName({ kind: 'building', structure_sub_kind: 'outbuilding', class_hint: null, roof_form: null, roof_type: null }), 'Outbuilding', 'a neutral sub-kind stands in when there is no class hint');
assert.equal(q.machineLabelName({ kind: 'building', structure_sub_kind: null, class_hint: 'non-residential', roof_form: null, roof_type: null }), 'Non-residential');
assert.equal(q.machineLabelName({ kind: 'building', structure_sub_kind: null, class_hint: 'tree-like', roof_form: null, roof_type: null }), 'Tree-like');
assert.equal(q.machineLabelName({ kind: 'lean_to_shed', structure_sub_kind: null, class_hint: null, roof_form: null, roof_type: null }), 'Lean to shed', 'the kind is the last resort');
assert.equal(q.machineLabelName({ kind: '', structure_sub_kind: null, class_hint: null, roof_form: null, roof_type: null }), null, 'an empty label names nothing');
// describeDecision (tooltips) and describeDecisionLine (the editor sentence): plain words, no shouting.
assert.equal(q.describeDecision(decision), 'Machine label: garage; auto tier, confidence 0.91. Reasons: small footprint beside the driveway, strong structure hint.');
assert.equal(q.describeDecision({ ...decision, reasons: ['ends with a stop.'] }), 'Machine label: garage; auto tier, confidence 0.91. Reasons: ends with a stop.', 'no double stop');
assert.equal(q.describeDecision({ tier: 'review', confidence: 0.4, machine_label: { kind: '', structure_sub_kind: null, class_hint: null, roof_form: null, roof_type: null }, reasons: [] }), 'Machine label: unspecified; review tier, confidence 0.40.');
assert.equal(q.describeDecisionLine(decision), 'Machine label: garage, auto tier, confidence 0.91.');
assert.equal(q.describeDecisionLine({ ...decision, tier: 'assist', confidence: 0.5, machine_label: { ...decision.machine_label, structure_sub_kind: 'shipping-container' } }), 'Machine label: shipping container, assist tier, confidence 0.50.');
// With v2 intervals: the model range in brackets, then one human agreement sentence; "not yet judged" when nothing was judged.
assert.equal(q.describeDecisionLine({ ...v2, tier: 'auto', confidence: 0.91 }), 'Machine label: garage, auto tier, confidence 0.91 (model range 0.86 to 0.95). Human agreement for auto/garage: 6 of 9 confirmed, 95% interval 0.35 to 0.88.');
assert.equal(q.describeDecision({ ...v2, tier: 'auto', confidence: 0.91 }), 'Machine label: garage; auto tier, confidence 0.91 (model range 0.86 to 0.95). Human agreement for auto/garage: 6 of 9 confirmed, 95% interval 0.35 to 0.88. Reasons: small footprint beside the driveway, strong structure hint.');
const unjudged = { ...v2, interval: { ...v2.interval, confirmed: 0, judged: 0, lower: null, upper: null } };
assert.equal(q.describeDecisionLine(unjudged), 'Machine label: garage, assist tier, confidence 0.88 (model range 0.86 to 0.95). Human agreement for auto/garage: not yet judged by a reviewer.');
assert.equal(q.agreementText(unjudged), 'not yet judged by a reviewer'); assert.equal(q.agreementText(v2), '6 of 9 confirmed, 95% interval 0.35 to 0.88'); assert.equal(q.agreementText(decision), null, 'a v1 row has no agreement text');
assert.equal(q.modelRangeText(v2), ' (model range 0.86 to 0.95)'); assert.equal(q.modelRangeText(decision), '');
assert.equal(q.describeDecisionLine({ ...v2, score_interval: null }), 'Machine label: garage, assist tier, confidence 0.88. Human agreement for auto/garage: 6 of 9 confirmed, 95% interval 0.35 to 0.88.', 'no model range without a score interval');
assert.equal(q.agreementSentence({ ...v2, interval: { ...v2.interval, stratum: '' } }), 'Human agreement for assist/garage: 6 of 9 confirmed, 95% interval 0.35 to 0.88.', 'an unnamed stratum falls back to tier/label');
assert.equal(q.agreementSentence({ ...v2, interval: { ...v2.interval, kind: 'jeffreys-90' } }), 'Human agreement for auto/garage: 6 of 9 confirmed, jeffreys 90 interval 0.35 to 0.88.', 'another interval kind is named as such');
assert.equal(q.hintBadges({ hints: [], class_hint: null, decision: v2 })[0].title, 'Machine label, not a reviewer label. Machine label: garage; assist tier, confidence 0.88 (model range 0.86 to 0.95). Human agreement for auto/garage: 6 of 9 confirmed, 95% interval 0.35 to 0.88. Reasons: small footprint beside the driveway, strong structure hint.', 'badge titles carry the intervals in one sentence each');
assert.equal(q.describeObjectHints({ class_hints: null, structure: null, decision: v2 }), q.describeDecisionLine(v2), 'the editor line carries the same sentences');
for (const line of [q.describeDecision(decision), q.describeDecisionLine(decision), q.describeDecision(v2), q.describeDecisionLine(unjudged)]) assert(!/[A-Z]{4,}/.test(line) && !line.includes(' · ') && !line.includes('→') && !line.includes('–'), `quiet copy: ${line}`);
// Review status in plain words, and whether a person made it.
assert.deepEqual(q.reviewStatusText(v2), { text: 'Confirmed by reviewer', by_person: true });
assert.deepEqual(q.reviewStatusText({ ...v2, review: { status: null } }), { text: 'Confirmed by machine', by_person: false }, 'no reviewer decision recorded on an auto-tier row');
assert.deepEqual(q.reviewStatusText({ ...v2, review: { status: 'needs_adjustment' } }), { text: 'Needs edit', by_person: true });
assert.deepEqual(q.reviewStatusText({ ...v2, review: { status: 'candidate' } }), { text: 'Candidate', by_person: false });
assert.deepEqual(q.reviewStatusText({ ...v2, review: { status: 'rejected' } }), { text: 'Rejected by reviewer', by_person: true });
assert.deepEqual(q.reviewStatusText({ ...v2, review: { status: 'odd_state' } }), { text: 'odd state', by_person: false });
assert.equal(q.reviewStatusText(decision), null, 'no review data means the column is omitted for that row');
// The Machine labelled queue: the auto tier over the raw list rows, v2 over v1, malformed and other tiers skipped, hints attached, honest denominators.
assert.match(q.DECISION_LIST_SQL, /^SELECT scene_id, object_id, version, payload FROM decision_policy WHERE version = \? OR version = \?/); assert(!/JOIN|object_quality/i.test(q.DECISION_LIST_SQL), 'joined to nothing else');
const autoV2 = { ...decisionPayloadV2, tier: 'auto' }, autoUnjudged = { ...autoV2, interval: { ...autoV2.interval, confirmed: 0, judged: 0, lower: null, upper: null }, signals: { review: { status: null } } };
const listRows = [
  { scene_id: 'tile', object_id: 'a', version: 'decision-policy-v1', payload: JSON.stringify(decisionPayload) },
  { scene_id: 'tile', object_id: 'a', version: 'decision-policy-v2', payload: JSON.stringify(autoV2) },
  { scene_id: 'tile', object_id: 'b', version: 'decision-policy-v1', payload: JSON.stringify({ ...decisionPayload, signals: { review: { status: null } } }) },
  { scene_id: 'tile', object_id: 'c', version: 'decision-policy-v2', payload: JSON.stringify(autoUnjudged) },
  { scene_id: 'tile', object_id: 'd', version: 'decision-policy-v2', payload: JSON.stringify({ ...autoV2, tier: 'assist' }) },
  { scene_id: 'tile', object_id: 'e', version: 'decision-policy-v2', payload: '{not json' },
  { scene_id: 'tile', object_id: 'f', version: 'decision-policy-v0', payload: JSON.stringify(autoV2) },
  { scene_id: 'tile', object_id: 'low-roof', version: 'decision-policy-v2', payload: JSON.stringify({ ...autoV2, signals: { review: { status: 'needs_adjustment' } } }) },
  { scene_id: 'tile', version: 'decision-policy-v2', payload: JSON.stringify(autoV2) }, 'junk', null,
];
let machine = q.machineLabelledQueue(listRows, q.indexHintSource(fakeDb));
assert.deepEqual(machine.rows.map(r => `${r.object_id}:${r.version}`), ['a:decision-policy-v2', 'b:decision-policy-v1', 'c:decision-policy-v2', 'low-roof:decision-policy-v2'], 'only auto rows; v2 wins over v1 for a; assist, malformed, other versions and keyless rows are skipped');
assert.equal(machine.version, 'decision-policy-v2'); assert.deepEqual(machine.versions, ['decision-policy-v2', 'decision-policy-v1']); assert.match(machine.note, /nothing on this table approves/i);
assert.equal(machine.count, 4); assert.equal(machine.reviewed_by_person, 2, 'a (confirmed) and low-roof (needs edit) were reviewed by a person; b (no decision recorded) and c (status null) were not');
assert.equal(q.machineLabelledSummary(machine), '4 machine-labelled roofs, 2 reviewed by a person'); assert.equal(q.machineLabelledSummary({ count: 1, reviewed_by_person: 0 }), '1 machine-labelled roof, 0 reviewed by a person');
const [ra, rb, rc, rl] = machine.rows;
assert.deepEqual([ra.label, ra.review_status, ra.reviewed_by_person, ra.review_url], ['Garage', 'Confirmed by reviewer', true, '/scenes?scene=tile&object=a&kind=building&status=all&layer=model'], 'the row carries the proposed label, the review status in plain words and the editor link with status=all');
assert.deepEqual([rb.review_status, rb.reviewed_by_person, rb.decision.interval], ['Confirmed by machine', false, null], 'a v1 row: no intervals, machine-confirmed');
assert.deepEqual([rc.review_status, q.agreementText(rc.decision)], ['Confirmed by machine', 'not yet judged by a reviewer']);
assert.deepEqual([rl.review_status, rl.class_hint, rl.structure.sub_kind], ['Needs edit', 'outbuilding', 'shipping-container'], 'class and structure hints attach through the source');
assert.equal(rl.decision.tier, 'auto', 'the decision stays the one from the list query even though the source also reads decisions');
assert.equal(ra.hints, undefined, 'rows without an index hint row carry no hint keys');
assert.equal(q.machineLabelledQueue(listRows.map(r => (r && r.object_id === 'a' && r.version === 'decision-policy-v1' ? { ...r, payload: JSON.stringify({ ...decisionPayload, signals: { review: { status: 'rejected' } } }) } : r))).rows[0].review_status, 'Confirmed by reviewer', 'v2 wins whichever order the rows arrive in');
assert.deepEqual(q.machineLabelledQueue(null).rows, []); assert.deepEqual(q.machineLabelledQueue([]).count, 0);
assert.equal(q.machineLabelledQueue([{ scene_id: 'tile', object_id: 'v1', version: 'decision-policy-v1', payload: JSON.stringify(decisionPayload) }]).rows[0].review_status, null, 'no review data recorded: the cell is omitted, not invented');
// The client coercer over the route body: rows survive a JSON round trip; malformed rows drop; no rows at all is an error the page shows.
const coerced = q.coerceMachineLabelledQueue(JSON.parse(JSON.stringify({ ...machine, source: 'eyes-index', rows: [...machine.rows, { scene_id: 'tile', object_id: 'bad', decision: { tier: 'auto' } }, { scene_id: 'tile', object_id: 'assist', decision: { ...v2 } }, 'junk'] })));
assert.deepEqual(coerced.rows.map(r => r.object_id), ['a', 'b', 'c', 'low-roof']); assert.equal(coerced.count, 4); assert.equal(coerced.reviewed_by_person, 2);
assert.deepEqual(coerced.rows[3].structure, rl.structure); assert.equal(coerced.rows[3].class_hint, 'outbuilding'); assert.deepEqual(coerced.rows[0].decision, ra.decision);
assert.throws(() => q.coerceMachineLabelledQueue({ count: 3 }), /no rows/);
assert.equal(q.coerceMachineLabelledQueue({ rows: [] }).count, 0, 'an empty table is a valid, empty queue');
// Tier badge: one per decision, after the class and structure badges, with the tier's label and the machine label in the title.
badges = q.hintBadges({ hints: [], class_hint: null, decision });
assert.deepEqual(badges, [{ key: 'tier-auto', label: 'Auto', group: 'decision', title: 'Machine label, not a reviewer label. Machine label: garage; auto tier, confidence 0.91. Reasons: small footprint beside the driveway, strong structure hint.' }]);
assert.deepEqual(q.decisionBadge(decision), badges[0]);
for (const tier of q.DECISION_TIERS) { const [badge] = q.hintBadges({ hints: [], class_hint: null, decision: { ...decision, tier } }); assert.equal(badge.label, q.TIER_LABELS[tier]); assert.equal(badge.key, `tier-${tier}`); assert.equal(badge.group, 'decision'); }
badges = q.hintBadges({ ...q.parseClassHints(shedPayload), structure, decision });
assert.deepEqual(badges.map(b => [b.label, b.group]), [['Secondary structure', 'class'], ['Shipping container', 'structure'], ['Auto', 'decision']], 'the tier badge comes last');
assert.deepEqual(q.hintBadges({ hints: [], class_hint: null, decision: null }), [], 'a null decision earns no badge');
// A decision never counts as flagged (Hide flagged is about the agent hints); the auto tier has its own view choice.
assert.equal(q.isFlagged({ hints: [], class_hint: null, decision }), false);
assert.equal(q.isAutoTier({ decision }), true); assert.equal(q.isAutoTier({ decision: { ...decision, tier: 'assist' } }), false); assert.equal(q.isAutoTier({ decision: null }), false); assert.equal(q.isAutoTier({}), false);
// decisionSummary: every tier with zeros so the denominators add up; null when no row has a decision.
const decided = tiers => tiers.map(tier => ({ hints: [], class_hint: null, decision: tier ? { ...decision, tier } : undefined }));
assert.equal(q.decisionSummary(decided(['auto', 'auto', 'review', null, 'assist', 'auto'])), 'Machine labels: 3 auto, 1 assist, 1 review');
assert.equal(q.decisionSummary(decided(['review', 'review'])), 'Machine labels: 0 auto, 0 assist, 2 review');
assert.equal(q.decisionSummary(decided([null, null])), null); assert.equal(q.decisionSummary([]), null);
// hasReviewerRoofEdits: any reviewer shaping of the roof stops "Use machine label" from touching roof_type.
assert.equal(q.hasReviewerRoofEdits({}), false);
assert.equal(q.hasReviewerRoofEdits({ roof_annotation: null, roof_geometry_source: 'image-guided-prior' }), false, 'a model proposal is not a reviewer edit');
assert.equal(q.hasReviewerRoofEdits({ roof_line_edits: { edges: [] } }), true);
assert.equal(q.hasReviewerRoofEdits({ roof_annotation: { schema_version: 1 } }), true);
assert.equal(q.hasReviewerRoofEdits({ roof_feedback: { roof_type_ok: false } }), true);
assert.equal(q.hasReviewerRoofEdits({ roof_geometry_source: 'reviewer-template' }), true);
// The editor line gains the machine label sentence after the hint sentence, or stands alone when only a decision exists.
assert.equal(q.describeObjectHints({ class_hints: q.parseClassHints(shedPayload), structure, decision }), 'Secondary structure. Shipping container (moderate). Not labels; change the Class above if you disagree. Machine label: garage, auto tier, confidence 0.91.');
assert.equal(q.describeObjectHints({ class_hints: null, structure: null, decision }), 'Machine label: garage, auto tier, confidence 0.91.');
assert.equal(q.describeObjectHints({ class_hints: q.parseClassHints(housePayload), structure: null, decision: null }), null);
// pickHints carries the decision back to the client and drops a malformed one.
const pickedDecision = q.pickHints({ rows: [{ scene_id: 'tile', object_id: 'd', decision }, { scene_id: 'tile', object_id: 'bad', decision: { tier: 'auto', confidence: 2 } }, { scene_id: 'tile', object_id: 'all', hints: shedPayload.class_hints.flags, class_hint: 'outbuilding', structure: q.parseStructureKind(containerPayload), decision }] });
assert.deepEqual(pickedDecision.get('tile/d'), { hints: [], class_hint: null, decision });
assert.deepEqual(pickedDecision.get('tile/bad'), { hints: [], class_hint: null }, 'a malformed decision is dropped, not thrown');
assert.deepEqual(Object.keys(pickedDecision.get('tile/all')).sort(), ['class_hint', 'decision', 'hints', 'structure']);
// The real node:sqlite source: a temporary index with one building-flags row; a missing file is null, never a throw.
const { DatabaseSync } = process.getBuiltinModule('node:sqlite');
const { mkdtempSync } = await import('node:fs');
const { tmpdir } = await import('node:os');
const tempIndex = `${mkdtempSync(`${tmpdir()}/queues-hints-`)}/eyes.sqlite`;
const seed = new DatabaseSync(tempIndex);
seed.exec('CREATE TABLE object_quality(scene_id TEXT, object_id TEXT, model_signature TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, model_signature, version))');
seed.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)').run('tile', 'low-roof', 'sig', 'building-flags-v2', JSON.stringify(treePayload));
seed.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)').run('tile', 'low-roof', 'sig', 'object-quality-v1', JSON.stringify({ kind: 'tree' }));
seed.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)').run('tile', 'low-roof', 'sig', 'structure-kinds-v1', JSON.stringify(containerPayload));
seed.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)').run('tile', 'shed-only', 'sig', 'structure-kinds-v1', JSON.stringify({ ...containerPayload, sub_kind: 'shed' }));
seed.close();
source = q.sqliteHintSource(tempIndex);
assert(source, 'a readable index yields a source');
assert.deepEqual(source.hintFor('tile', 'low-roof').class_hint, 'tree-like', 'only the building-flags version feeds class hints');
assert.equal(source.hintFor('tile', 'low-roof').structure.sub_kind, 'shipping-container', 'the structure-kinds version feeds structure');
assert.deepEqual(source.hintFor('tile', 'shed-only'), { hints: [], class_hint: null, structure: q.parseStructureKind({ ...containerPayload, sub_kind: 'shed' }) });
assert.equal(source.hintFor('tile', 'other'), null);
assert.equal(source.hintFor('tile', 'low-roof').decision, undefined, 'an index without a decision_policy table reads fine and carries no decision');
source.close();
// The same index once the decision_policy table exists: decisions attach; malformed rows and unknown tiers read as none.
const grow = new DatabaseSync(tempIndex);
grow.exec('CREATE TABLE decision_policy(scene_id TEXT, object_id TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, version))');
grow.prepare('INSERT INTO decision_policy VALUES (?, ?, ?, ?)').run('tile', 'low-roof', 'decision-policy-v1', JSON.stringify(decisionPayload));
grow.prepare('INSERT INTO decision_policy VALUES (?, ?, ?, ?)').run('tile', 'decided', 'decision-policy-v1', JSON.stringify({ ...decisionPayload, tier: 'assist' }));
grow.prepare('INSERT INTO decision_policy VALUES (?, ?, ?, ?)').run('tile', 'shed-only', 'decision-policy-v1', '{broken');
grow.prepare('INSERT INTO decision_policy VALUES (?, ?, ?, ?)').run('tile', 'other', 'decision-policy-v0', JSON.stringify(decisionPayload));
grow.prepare('INSERT INTO decision_policy VALUES (?, ?, ?, ?)').run('tile', 'decided', 'decision-policy-v2', JSON.stringify(decisionPayloadV2));
grow.close();
source = q.sqliteHintSource(tempIndex);
assert.deepEqual(source.hintFor('tile', 'low-roof').decision, decision, 'the v1 decision row attaches to the object while no v2 row exists');
assert.deepEqual(source.hintFor('tile', 'decided').decision, v2, 'the v2 row wins over the v1 row in a real index');
// The list query over the real index (what the machine-labelled route runs): rows of both versions, in one statement.
const listDb = new DatabaseSync(tempIndex, { readOnly: true });
const listed = listDb.prepare(q.DECISION_LIST_SQL).all(q.DECISION_VERSION, q.DECISION_FALLBACK_VERSIONS[0]);
listDb.close();
assert.deepEqual(listed.map(r => `${r.object_id}:${r.version}`), ['decided:decision-policy-v1', 'decided:decision-policy-v2', 'low-roof:decision-policy-v1', 'shed-only:decision-policy-v1'], 'both versions, no v0');
const realMachine = q.machineLabelledQueue(listed, q.sqliteHintSource(tempIndex));
assert.deepEqual(realMachine.rows.map(r => [r.object_id, r.version, r.class_hint ?? null]), [['low-roof', 'decision-policy-v1', 'tree-like']], 'only low-roof is auto (decided is assist in v2, which wins; shed-only is broken); its class hint attached');

assert.equal(source.hintFor('tile', 'shed-only').decision, undefined, 'a broken decision payload reads as no decision');
assert.equal(source.hintFor('tile', 'other'), null, 'another version is not read');
source.close();
assert.equal(q.sqliteHintSource(`${tmpdir()}/does-not-exist-${Date.now()}/eyes.sqlite`), null, 'a missing index is null');
console.log('queues: wilson, ring IoU, classify rules, band aggregation, calibration/self-review/checkpoint readers, hidden rows, class hints, structure kinds v2 (with v1 fallback rows), fragment detection and decision-policy machine labels (v2 intervals with v1 fallback, guarded decision statements, tiers, count line, the Machine labelled queue, reviewer roof edits) passed.');
