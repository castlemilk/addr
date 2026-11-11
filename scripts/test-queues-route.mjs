// GET-only review-queue routes over stubbed fs / exec / sqlite: id validation (400), missing data (404),
// happy-path shapes, no-store headers, and error bodies that never leak local paths or tracebacks.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const transpile = file => ts.transpileModule(readFileSync(file, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const root = process.cwd();
const files = new Map();
const rel = p => path.relative(root, p);
const enoent = () => Object.assign(new Error('ENOENT: no such file, open /Users/private/secret.json'), { code: 'ENOENT' });
const fakeFs = {
  readFileSync: (p, encoding) => { const key = rel(p); if (!files.has(key)) throw enoent(); assert.equal(encoding, 'utf8'); return files.get(key); },
  readdirSync: p => { const key = rel(p); const names = [...new Set([...files.keys()].filter(k => k.startsWith(key + '/')).map(k => k.slice(key.length + 1).split('/')[0]))]; if (!names.length) throw enoent(); return names; },
  existsSync: p => { const key = rel(p); return files.has(key) || [...files.keys()].some(k => k.startsWith(key + '/')); },
};
let execCalls = [], execStdout = null, execFail = false;
const execute = async (file, args, options) => {
  execCalls.push({ file, args, options });
  if (execFail) throw Object.assign(new Error('Traceback (most recent call last) /Users/private/x.py'), { code: 1, stderr: 'Traceback' });
  return { stdout: execStdout };
};
let sqliteRows = 55, sqliteFail = false, sqliteOpens = [], sqliteHiddenRows = [];
class DatabaseSync {
  constructor(file, options) { sqliteOpens.push({ file, options }); if (sqliteFail) throw new Error('SQLITE_CANTOPEN: unable to open database file /Users/private/eyes.sqlite'); }
  prepare(sql) { assert.match(sql, /cross_capture/); assert.match(sql, /'low'/); assert.match(sql, /'medium'/); assert.match(sql, /'high'/); assert(!/INSERT|UPDATE|DELETE|CREATE|DROP/i.test(sql), 'read-only SQL only'); return { get: version => { assert.equal(version, 'cross-capture-v1'); return { n: sqliteRows }; }, all: version => { assert.equal(version, 'cross-capture-v1'); return sqliteHiddenRows; } }; }
  close() { this.closed = true; }
}
// Real store shape: numeric keys belong to suburban_benchmark only; the training cohort's decisions are namespaced.
const feedback = { 'roof_training_250:1': { status: 'approved' }, 'roof_training_250:2': { status: 'needs_adjustment' }, 'roof_training_250:3': { status: 'unreviewed' },
  '4': { status: 'rejected' }, 'suburban_benchmark:5': { status: 'approved' }, 'roof_training_250:9': { status: 'rejected' } };
const reviewFeedback = {
  readFeedback: async file => { assert.match(file, /data\/reviews\/feedback\.json$/); return feedback; },
  feedbackFor: (store, cohort, id) => store[`${cohort}:${id}`] ?? (cohort === 'suburban_benchmark' ? store[String(id)] : undefined),
};
const queues = {};
new Function('exports', 'require', transpile('src/lib/queues.ts'))(queues, id => require(id));
const load = file => {
  const exports = {};
  new Function('exports', 'require', transpile(file))(exports, id => {
    if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
    if (id === 'node:fs') return fakeFs;
    if (id === 'node:util') return { promisify: () => execute };
    if (id === 'node:child_process') return { execFile: () => { throw new Error('not used directly'); } };
    if (id === 'node:sqlite') return { DatabaseSync };
    if (id === '@/lib/queues') return queues;
    if (id === '@/lib/review-feedback') return reviewFeedback;
    return require(id);
  });
  return exports;
};
const calibration = load('src/app/api/queues/calibration/route.ts');
const hidden = load('src/app/api/queues/hidden/route.ts');
const summary = load('src/app/api/queues/summary/route.ts');
const checkpoints = load('src/app/api/checkpoints/route.ts');
const checkpoint = load('src/app/api/checkpoints/[id]/route.ts');
for (const [name, route] of Object.entries({ calibration, hidden, summary, checkpoints, checkpoint })) {
  assert.equal(route.dynamic, 'force-dynamic', `${name} is dynamic`);
  assert.equal(typeof route.GET, 'function');
  for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.equal(route[verb], undefined, `${name} is GET-only (${verb})`);
}
const request = params => ({ nextUrl: { searchParams: new URLSearchParams(params) } });
const clean = async response => {
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const text = await response.text();
  assert(!text.includes('/Users') && !text.includes('Traceback') && !text.includes('ENOENT'), `no local details in ${text.slice(0, 200)}`);
  return JSON.parse(text);
};

// --- Fixtures -----------------------------------------------------------------------------------------
const PLAN = '7cfed4d9e02f1c8a', SIG = 'a'.repeat(64);
const square = (x, y, size = 100) => [[x, y], [x + size, y], [x + size, y + size], [x, y + size]];
const sceneA = 'mooroolbark-50011498-17-118440-80408', sceneB = 'house-30286';
const row = (band, scene_id) => ({ band, group: `z17:${scene_id.length}:1`, scene_id, object_id: 'roof', review_url: `/scenes?scene=${scene_id}&object=roof&kind=building&status=all&layer=model`, area_m2: 63.4, detector_score: 0.5 });
files.set(`data/reviews/eyes/calibration/${PLAN}/plan.json`, JSON.stringify({ version: 'reference-calibration-v1', plan_id: PLAN, created_at: '2026-09-25T14:05:04+00:00',
  policy: 'object-centroid core tile; calibration only; SAM3 encoded 64 px of neighbouring tiles', statement: 'sampled-roof protocol; boundary IoU >= 0.85 only; not apex/height/recall; assumes independent representative areas',
  iou_success: 0.85, bands: ['low', 'medium', 'high'], label_budget: { per_band_minimum: 20 }, rows: [row('low', sceneA), row('medium', sceneB)] }));
files.set(`data/reviews/eyes/calibration/${PLAN}/addresses.json`, JSON.stringify({ plan_id: PLAN, resolved: { [`${sceneA}/roof`]: { address_id: 31114, full_address: '19 LARNOOK CRESCENT, Mooroolbark VIC 3138' } } }));
const model = { id: 'roof', kind: 'building', ring: square(100, 100), status: 'candidate' };
files.set(`data/scenes/${sceneA}/latest.json`, JSON.stringify({ model_signature: SIG, objects: [model] }));
files.set(`data/scenes/${sceneB}/latest.json`, JSON.stringify({ model_signature: SIG, objects: [model] }));
files.set(`data/scenes/${sceneB}/review.json`, JSON.stringify({ model_signature: SIG, objects: [{ ...model, status: 'confirmed', geometry_reviewed: true }] }));
const report = (id, computed_at) => JSON.stringify({ version: 'crm-checkpoint-v1', computed_at, checkpoint: { id, title: `EYES ${id}`, summary: `${id} summary`, address_id: 29790,
  address_text: '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138', latitude: -37.79033, longitude: 145.31259, report_url: `data/references/eyes-checkpoints/${id}/REVIEW.md` } });
files.set('data/references/eyes-checkpoints/CP1/report.json', report('CP1', '2026-09-25T13:37:28+00:00'));
files.set('data/references/eyes-checkpoints/CP1/REVIEW.md', '# EYES CP1\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n');
files.set('data/references/eyes-checkpoints/CP1/dry-run/report.json', report('CP1', '2026-09-25T13:00:00+00:00'));
files.set('data/references/eyes-checkpoints/CP2/report.json', report('CP2', '2026-09-25T15:51:14+00:00'));
files.set('data/references/eyes-checkpoints/CP2/REVIEW.md', '# EYES CP2\n');
files.set('data/references/eyes-checkpoints/lower/report.json', report('lower', '2026-09-27T00:00:00+00:00'));
files.set('data/roof-review-loop/active.json', JSON.stringify({ run: '1fe92abe90ebd534' }));
files.set('data/roof-review-loop/runs/1fe92abe90ebd534/report.json', JSON.stringify({ run: '1fe92abe90ebd534', status: 'complete', total: 12,
  rows: [...Array(7).fill({ status: 'no_safe_gain' }), ...Array(5).fill({ status: 'needs_evidence' })] }));
files.set('data/cohorts/roof_training_250.json', JSON.stringify({ name: 'roof_training_250', entries: [{ address_id: 1 }, { address_id: 2 }, { address_id: 3 }, { address_id: 4 }, { address_id: 5 }, { address_id: 'x' }] }));
files.set('data/cohorts/suburban_benchmark.json', JSON.stringify({ name: 'suburban_benchmark', entries: [{ address_id: 4 }, { address_id: 5 }] }));

// --- /api/queues/calibration --------------------------------------------------------------------------
let response = await calibration.GET(request({ plan: PLAN }));
assert.equal(response.status, 200);
let body = await clean(response);
assert.equal(body.plan_id, PLAN);
assert.deepEqual(Object.keys(body.bands), ['low', 'medium', 'high']);
assert.deepEqual(Object.keys(body.bands.low).sort(), ['failures', 'groups_reviewed', 'groups_total', 'interval', 'minimum_groups', 'remaining_to_interval', 'statuses', 'successes']);
assert.equal(body.rows.length, 2);
assert.equal(body.rows[0].status, 'pending'); assert.equal(body.rows[0].address, '19 LARNOOK CRESCENT, Mooroolbark VIC 3138'); assert.equal(body.rows[0].address_id, 31114);
assert.equal(body.rows[1].status, 'confirmed'); assert.equal(body.rows[1].counted, true); assert.equal(body.rows[1].success, true); assert.equal(body.rows[1].address, null);
for (const key of ['band', 'group', 'scene_id', 'object_id', 'review_url', 'area_m2', 'detector_score', 'status', 'counted', 'success', 'address', 'address_id']) assert(key in body.rows[0], key);
assert.equal(body.bands.medium.groups_reviewed, 1); assert.equal(body.bands.medium.interval, null);
assert.equal(body.iou_success, 0.85); assert.match(body.statement, /IoU >= 0.85/); assert.match(body.policy, /calibration only/);
for (const bad of [{}, { plan: '' }, { plan: '../x' }, { plan: 'ABCDEF0123456789' }, { plan: PLAN.slice(1) }, { plan: `${PLAN}/plan` }]) {
  response = await calibration.GET(request(bad));
  assert.equal(response.status, 400, `bad plan ${JSON.stringify(bad)}`);
  body = await clean(response); assert.equal(typeof body.error, 'string');
}
response = await calibration.GET(request({ plan: 'ffffffffffffffff' }));
assert.equal(response.status, 404); body = await clean(response); assert.match(body.error, /plan/i);
files.set(`data/scenes/${sceneB}/review.json`, '{broken');
response = await calibration.GET(request({ plan: PLAN }));
assert.equal(response.status, 200, 'a corrupt review.json is a pending row, not a failure');
assert.equal((await clean(response)).rows[1].status, 'pending');
files.set(`data/scenes/${sceneB}/review.json`, JSON.stringify({ model_signature: SIG, objects: [{ ...model, status: 'confirmed', geometry_reviewed: true }] }));
const realRead = fakeFs.readFileSync;
fakeFs.readFileSync = () => { throw Object.assign(new Error('EACCES: permission denied, open /Users/private/plan.json'), { code: 'EACCES' }); };
response = await calibration.GET(request({ plan: PLAN }));
assert.equal(response.status, 500); body = await clean(response); assert.match(body.error, /could not be read/i);
fakeFs.readFileSync = realRead;

// --- /api/queues/hidden -------------------------------------------------------------------------------
// Primary path: the eyes index read with Node's SQLite (no Python spawn). The stubbed prepare() already pins
// read-only SQL over cross_capture filtered to hidden low / shown medium-high rows.
const INDEX_KEY = 'data/reviews/eyes/eyes.sqlite';
const hiddenRecord = { scene_id: 'tile', object_id: 'low-roof', other_scene_id: 'house', other_object_id: 'med-roof',
  payload: JSON.stringify({ band: 'low', other_band: 'medium', score: 0.58, other_score: 0.8, segments: 4, other_segments: 10, iou: 0.8913 }) };
files.set(INDEX_KEY, '');
sqliteHiddenRows = [hiddenRecord, { ...hiddenRecord, object_id: 'bad-payload', payload: 'not json' }];
sqliteOpens = []; execCalls = [];
response = await hidden.GET(request({}));
assert.equal(response.status, 200); body = await clean(response);
assert.equal(body.version, 'cross-capture-v1'); assert.equal(body.count, 1, 'a corrupt payload row is skipped, not fatal');
assert.deepEqual(Object.keys(body.rows[0]).sort(), ['address', 'band', 'detector_score', 'hidden_url', 'iou', 'object_id', 'observed_segments', 'other_band', 'other_detector_score', 'other_object_id', 'other_scene_id', 'scene_id', 'shown_url']);
assert.equal(body.rows[0].address, null);
assert.equal(body.rows[0].detector_score, 0.58); assert.equal(body.rows[0].other_detector_score, 0.8); assert.equal(body.rows[0].observed_segments, 4); assert.equal(body.rows[0].iou, 0.8913);
assert.equal(body.rows[0].hidden_url, '/scenes?scene=tile&object=low-roof&kind=building&status=all&layer=model');
assert.equal(body.rows[0].shown_url, '/scenes?scene=house&object=med-roof&kind=building&status=all&layer=model');
assert.match(body.note, /not accuracy/);
assert.equal(execCalls.length, 0, 'the hidden queue never spawns Python when the index is readable');
assert.equal(sqliteOpens.length, 1); assert.match(sqliteOpens[0].file, /data\/reviews\/eyes\/eyes\.sqlite$/); assert.deepEqual(sqliteOpens[0].options, { readOnly: true });
// Fallback path: an unreadable index hands over to the Python CLI (same document shape).
sqliteFail = true;
execStdout = JSON.stringify({ version: 'cross-capture-v1', count: 1, index_available: true, rows: [{ scene_id: 'tile', object_id: 'low-roof', band: 'low', detector_score: 0.58, observed_segments: 4,
  other_scene_id: 'house', other_object_id: 'med-roof', other_band: 'medium', other_detector_score: 0.8, iou: 0.8913, hidden_url: '/scenes?scene=tile&object=low-roof&kind=building&status=all&layer=model',
  shown_url: '/scenes?scene=house&object=med-roof&kind=building&status=all&layer=model' }] }) + '\n';
response = await hidden.GET(request({}));
assert.equal(response.status, 200); body = await clean(response); assert.equal(body.count, 1);
assert.equal(execCalls.length, 1);
assert.match(execCalls[0].file, /scripts\/ml\/\.venv\/bin\/python$/);
assert.deepEqual(execCalls[0].args, ['-m', 'scripts.ml.cross_capture', '--hidden-low']);
assert.equal(execCalls[0].options.cwd, process.cwd());
assert(execCalls[0].options.timeout >= 5000 && execCalls[0].options.timeout <= 60000);
execStdout = 'not json\n';
response = await hidden.GET(request({}));
assert.equal(response.status, 502); body = await clean(response); assert.match(body.error, /could not be read/i);
execFail = true;
response = await hidden.GET(request({}));
assert.equal(response.status, 502); await clean(response);
execFail = false; sqliteFail = false;
// Absent index: unavailable, never "0 hidden roofs".
files.delete(INDEX_KEY);
response = await hidden.GET(request({}));
assert.equal(response.status, 503, 'an absent eyes index is unavailable, never "0 hidden roofs"');
body = await clean(response); assert.match(body.error, /eyes index/i); assert.match(body.error, /not been built/i);
files.set(INDEX_KEY, '');
sqliteHiddenRows = [];
response = await hidden.GET(request({}));
assert.equal(response.status, 200, 'an index with no hidden low roofs is an honest empty queue');
body = await clean(response); assert.equal(body.count, 0); assert.deepEqual(body.rows, []);

// --- /api/queues/summary ------------------------------------------------------------------------------
execCalls = []; sqliteOpens = [];
response = await summary.GET(request({}));
assert.equal(response.status, 200); body = await clean(response);
assert.equal(execCalls.length, 0, 'the summary never shells out');
assert.deepEqual(Object.keys(body).sort(), ['calibration', 'checkpoints', 'hidden', 'note', 'roof_review', 'self_review']);
assert.equal(body.calibration.plan_id, PLAN); assert.equal(body.calibration.rows, 2);
assert.deepEqual(body.calibration.groups_reviewed_by_band, { low: 0, medium: 1, high: 0 });
assert.deepEqual(body.calibration.intervals_by_band, { low: null, medium: null, high: null });
assert.deepEqual(body.calibration.groups_total_by_band, { low: 1, medium: 1, high: 0 });
assert.deepEqual(body.hidden, { count: 55 });
assert.equal(sqliteOpens.length, 1); assert.match(sqliteOpens[0].file, /data\/reviews\/eyes\/eyes\.sqlite$/); assert.deepEqual(sqliteOpens[0].options, { readOnly: true });
assert.deepEqual(body.self_review, { run: '1fe92abe90ebd534', status: 'complete', total: 12, completed: 12, outcomes: { no_safe_gain: 7, needs_evidence: 5, candidate_ready: 0 }, counts: { no_safe_gain: 7, needs_evidence: 5 } });
// The hub's "Roof training review" line links to /reviews?cohort=roof_training_250, so its denominator is that cohort
// (scripts/verify-queues.mjs pins "N of 250 reviewed"); numeric-key benchmark feedback never leaks into the count.
assert.deepEqual(body.roof_review, { cohort: 'roof_training_250', reviewed: 2, total: 5 }, 'training cohort: unreviewed, missing and benchmark-only feedback are not reviewed; non-integer ids are not roofs');
assert.deepEqual(body.checkpoints, [{ id: 'CP2', title: 'EYES CP2', created_at: '2026-09-25T15:51:14+00:00', summary: 'CP2 summary' }, { id: 'CP1', title: 'EYES CP1', created_at: '2026-09-25T13:37:28+00:00', summary: 'CP1 summary' }]);
assert.match(body.note, /model estimate/i);
sqliteFail = true;
response = await summary.GET(request({}));
assert.equal(response.status, 200); body = await clean(response);
assert.deepEqual(body.hidden, { count: null }, 'a missing eyes index is null, never a fabricated zero');
sqliteFail = false;
files.delete(`data/reviews/eyes/calibration/${PLAN}/plan.json`);
files.delete(`data/reviews/eyes/calibration/${PLAN}/addresses.json`);
files.delete('data/cohorts/roof_training_250.json');
response = await summary.GET(request({}));
assert.equal(response.status, 200); body = await clean(response);
assert.equal(body.calibration, null, 'no plan on disk means no calibration section');
assert.deepEqual(body.roof_review, { cohort: 'roof_training_250', reviewed: 0, total: 0 }, 'a missing cohort file is 0 of 0, never a fabricated total');
files.set(`data/reviews/eyes/calibration/${PLAN}/plan.json`, JSON.stringify({ plan_id: PLAN, bands: ['low'], rows: [row('low', sceneA)] }));

// --- /api/checkpoints ---------------------------------------------------------------------------------
response = await checkpoints.GET(request({}));
assert.equal(response.status, 200); body = await clean(response);
assert.deepEqual(body.checkpoints.map(c => c.id), ['CP2', 'CP1'], 'dry-run and lower-case folders are skipped; newest first');
assert.deepEqual(Object.keys(body.checkpoints[1]).sort(), ['address_id', 'address_text', 'created_at', 'id', 'latitude', 'longitude', 'report_sha256', 'report_url', 'review_path', 'summary', 'title']);
assert.match(body.checkpoints[1].report_sha256, /^[a-f0-9]{64}$/);
assert.equal(body.checkpoints[1].review_path, 'data/references/eyes-checkpoints/CP1/REVIEW.md');
assert.equal(body.checkpoints[1].latitude, -37.79033);
files.delete('data/references/eyes-checkpoints/CP1/report.json'); files.delete('data/references/eyes-checkpoints/CP1/REVIEW.md'); files.delete('data/references/eyes-checkpoints/CP1/dry-run/report.json');
files.delete('data/references/eyes-checkpoints/CP2/report.json'); files.delete('data/references/eyes-checkpoints/CP2/REVIEW.md'); files.delete('data/references/eyes-checkpoints/lower/report.json');
response = await checkpoints.GET(request({}));
assert.equal(response.status, 200); assert.deepEqual((await clean(response)).checkpoints, [], 'no folder means an empty list, not an error');
files.set('data/references/eyes-checkpoints/CP1/report.json', report('CP1', '2026-09-25T13:37:28+00:00'));
files.set('data/references/eyes-checkpoints/CP1/REVIEW.md', '# EYES CP1\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n');

// --- /api/checkpoints/[id] ----------------------------------------------------------------------------
const params = id => ({ params: Promise.resolve({ id }) });
response = await checkpoint.GET(request({}), params('CP1'));
assert.equal(response.status, 200); body = await clean(response);
assert.equal(body.id, 'CP1'); assert.equal(body.markdown, '# EYES CP1\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n'); assert.equal(body.report.checkpoint.title, 'EYES CP1');
response = await checkpoint.GET(request({}), params('CP9'));
assert.equal(response.status, 404); body = await clean(response); assert.match(body.error, /checkpoint/i);
for (const bad of ['', 'cp1', '..', '../CP1', 'CP1/dry-run', '-CP1', 'C'.repeat(33), 'CP 1', '%2e%2e']) {
  response = await checkpoint.GET(request({}), params(bad));
  assert.equal(response.status, 400, `bad checkpoint id ${JSON.stringify(bad)}`);
  await clean(response);
}
response = await checkpoint.GET(request({}), { params: Promise.resolve({}) });
assert.equal(response.status, 400);
console.log('queue routes: GET-only, force-dynamic, no-store, 400/404/500 handling without leaks, and contract shapes for calibration, hidden, summary and checkpoints passed.');
