import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import ts from 'typescript';

const require = createRequire(import.meta.url);
let calls = 0, complete, lastArgs = [];
const execute = (_file, _args, options) => {
  calls++; lastArgs = _args;
  assert(options.timeout > 30000, 'A busy full-suburb read must survive the old 30-second deadline');
  assert.equal(options.signal, undefined, 'One polling client cannot cancel a shared inventory read');
  return new Promise((resolve, reject) => { complete = { resolve, reject }; });
};
const libs = new Map();
function lib(file) {
  const resolved = path.resolve(file);
  if (libs.has(resolved)) return libs.get(resolved);
  const exports = {};
  libs.set(resolved, exports);
  const code = ts.transpileModule(readFileSync(resolved, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => id.startsWith('./') ? lib(path.join(path.dirname(resolved), id + '.ts')) : id.startsWith('@/') ? lib(path.join('src', id.slice(2) + '.ts')) : require(id));
  return exports;
}
function route() {
  const exports = {};
  const code = ts.transpileModule(readFileSync('src/app/api/scenes/processing/route.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
    if (id === 'node:util') return { promisify: () => execute };
    if (id.startsWith('@/')) return lib(path.join('src', id.slice(2) + '.ts'));
    return require(id);
  });
  return exports;
}
const api = route();
const first = api.GET(), second = api.GET();
assert.equal(calls, 1, 'Concurrent tabs must coalesce their expensive inventory read');
complete.resolve({ stdout: JSON.stringify({ fetched_at: 1, counts: { complete: 257 } }) });
assert.deepEqual(await (await first).json(), await (await second).json());
assert.equal((await api.GET()).status, 200);
assert.equal(calls, 1, 'An immediate reconnect reuses the short-lived snapshot');
const failureApi = route();
const failed = failureApi.GET();
complete.reject(new Error('private process details'));
const response = await failed;
assert.equal(response.status, 503);
assert(!(await response.text()).includes('private process details'));
const retry = failureApi.GET();
assert.equal(calls, 3, 'A failed read must clear the in-flight state for retry');
complete.resolve({ stdout: '{"counts":{"complete":257}}' });
assert.equal((await retry).status, 200);
console.log('Processing polling: shared reads, busy-workload deadline, reconnect reuse and failure retry passed.');

// --- parts: summary-first, server-side slices from one cached inventory, background refresh ---
const req = query => ({ nextUrl: new URL(`http://localhost/api/scenes/processing${query}`) });
const roof = (i, extra = {}) => ({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[145, -37], [145.001, -37], [145.001, -37.001], [145, -37]]] },
  properties: { key: `s/o${i}`, scene_id: 's', object_id: `o${i}`, model_signature: 'sig', label: `Building ${i}`, capture_title: 'Cambridge Road', band: ['low', 'medium', 'high'][i % 3],
    band_label: 'x', color: '#000', reasons: [], area_m2: 100 + i, detector_score: 0.5, review_status: 'candidate', issue_count: 0, review_url: `/scenes?scene=s&object=o${i}`,
    interval: null, calibration_groups: 0, search_text: `building ${i} cambridge s/o${i}`, property_address: { status: 'matched', full_address: `${i} CAMBRIDGE ROAD`, candidates: [], reason: '' }, ...extra } });
const inventory = { type: 'FeatureCollection', fetched_at: 2, counts: { low: 2, medium: 2, high: 1 }, address_counts: { matched: 5 }, duplicates: 1,
  features: [roof(0), roof(1, { hidden_overlaps: [{ scene_id: 'scan', object_id: 'hidden-low', band: 'low', iou: 0.9, detector_score: null, image_line_mismatch_m: null, observed_segments: null, clipped: null }] }), roof(2), roof(3), roof(4)],
  suburb: { run: 'a'.repeat(16), total: 3 }, queue: [], objects: { building: 5 } };
const summary = { suburb: { run: 'a'.repeat(16), total: 3, counts: { complete: 3 } }, queue: [], objects: { building: 5 }, counts: { low: 2 }, fetched_at: 5 };

const partsApi = route();
calls = 0;
const summaryA = partsApi.GET(req('?part=summary')), summaryB = partsApi.GET(req('?part=summary'));
assert.equal(calls, 1, 'Concurrent summary polls share one cheap read');
assert(lastArgs.includes('--summary'), 'The summary part asks Python for progress and queue only');
complete.resolve({ stdout: JSON.stringify(summary) });
const summaryBody = await (await summaryA).json();
assert.deepEqual(summaryBody, await (await summaryB).json());
assert.equal(summaryBody.inventory, 'warming', 'Before the first inventory read the summary says the inventory is warming');
assert.deepEqual(summaryBody.suburb, summary.suburb);
assert.equal((await partsApi.GET(req('?part=summary'))).status, 200);
assert.equal(calls, 1, 'A summary within its five-second window is served from its own cache');

const pageTwo = partsApi.GET(req('?part=roofs&page=2&size=2&sort=area')), pageOne = partsApi.GET(req('?part=roofs'));
assert.equal(calls, 2, 'Roof pages coalesce onto one inventory read, separate from the summary');
assert(!lastArgs.includes('--summary'), 'The roofs part reads the full inventory');
complete.resolve({ stdout: JSON.stringify(inventory) });
const two = await (await pageTwo).json();
assert.equal(two.page, 2); assert.equal(two.size, 2); assert.equal(two.total, 5); assert.equal(two.pages, 3); assert.equal(two.sort, 'area');
assert.deepEqual(two.rows.map(r => r.properties.key), ['s/o2', 's/o3']);
assert(two.rows.every(r => !('search_text' in r.properties)), 'Rows never carry the server-side search text');
assert.deepEqual(two.counts, inventory.counts); assert.deepEqual(two.address_counts, inventory.address_counts);
assert.equal(two.fetched_at, 2); assert.equal(two.inventory, 'ready');
const one = await (await pageOne).json();
assert.equal(one.size, 50); assert.equal(one.rows.length, 5); assert.equal(one.rows[0].properties.band, 'low', 'default order is low evidence first');
assert.equal((await (await partsApi.GET(req('?part=roofs&band=low&q=BUILDING%203'))).json()).total, 1, 'Filters apply on the server');
assert.equal((await (await partsApi.GET(req('?part=summary'))).json()).inventory, 'ready', 'Once the inventory is cached the summary says so');

const full = await partsApi.GET(req('?part=roof&key=s%2Fo1'));
assert.equal(full.status, 200);
const fullBody = await full.json();
assert.equal(fullBody.roof.properties.search_text, 'building 1 cambridge s/o1', 'One roof by key is the complete feature');
assert.equal(fullBody.inventory, 'ready');
const missing = await partsApi.GET(req('?part=roof&key=nope'));
assert.equal(missing.status, 404);
assert((await missing.json()).error.includes('no longer in the current inventory'));
assert.equal((await partsApi.GET(req('?part=roof'))).status, 400, 'a roof read without a key is a bad request, not a vanished roof');

const hidden = await (await partsApi.GET(req('?part=hidden-low'))).json();
assert.equal(hidden.total, 1); assert.equal(hidden.rows[0].key, 'scan/hidden-low'); assert.equal(hidden.rows[0].hidden_by.band, 'medium');
assert.equal((await partsApi.GET(req('?part=nope'))).status, 400);
assert.deepEqual(await (await partsApi.GET()).json(), inventory, 'The default GET still returns the whole inventory');
assert.equal(calls, 2, 'Every slice is served from the one cached inventory');

// Near expiry, a read returns the cached inventory at once and starts exactly one background refresh.
const realNow = Date.now, base = realNow();
Date.now = () => base + 16000;
const warm = partsApi.GET(req('?part=roofs&size=1'));
assert.equal(calls, 3, 'Within five seconds of expiry a read starts one background refresh');
assert(!lastArgs.includes('--summary'));
const refreshCompleter = complete;
const warmBody = await (await warm).json();
assert.equal(warmBody.fetched_at, 2, 'The warm read is served from the cache without waiting');
assert.equal(warmBody.inventory, 'warming', 'While the refresh runs, slices say the inventory is updating');
const warmAgain = partsApi.GET(), warmSummary = partsApi.GET(req('?part=summary'));
assert.equal(calls, 4, 'A second read joins the in-flight refresh; only the expired summary re-reads');
assert(lastArgs.includes('--summary'));
assert.equal((await warmAgain).status, 200);
complete.resolve({ stdout: JSON.stringify({ ...summary, fetched_at: 6 }) });
assert.equal((await (await warmSummary).json()).inventory, 'warming');
Date.now = () => base + 21000;
const afterExpiry = await (await partsApi.GET(req('?part=roofs&size=1'))).json();
assert.equal(calls, 4, 'An expired read joins the refresh already in flight rather than starting another');
assert.equal(afterExpiry.fetched_at, 2, 'An expired inventory is still served at once while its refresh runs, so a page change never waits on Python');
assert.equal(afterExpiry.inventory, 'warming');
refreshCompleter.resolve({ stdout: JSON.stringify({ ...inventory, fetched_at: 3 }) });
await new Promise(r => setImmediate(r));
const refreshed = await (await partsApi.GET(req('?part=roofs&size=1'))).json();
assert.equal(refreshed.fetched_at, 3, 'The refreshed inventory serves the next read');
assert.equal(refreshed.inventory, 'ready');
assert.equal(calls, 4, 'The refreshed cache covers the next twenty seconds');

// The common case for a 20 s poll against a 20 s TTL: the cache expired and nothing is refreshing. The last
// inventory is served at once, exactly one refresh starts, and concurrent slices join it.
Date.now = () => base + 42000;
const staleRoofs = partsApi.GET(req('?part=roofs&size=1')), staleHidden = partsApi.GET(req('?part=hidden-low'));
assert.equal(calls, 5, 'An expired cache with no refresh running starts exactly one');
assert(!lastArgs.includes('--summary'));
const staleRefresh = complete;
const staleBody = await (await staleRoofs).json();
assert.equal(staleBody.fetched_at, 3, 'served from the expired cache without waiting');
assert.equal(staleBody.inventory, 'warming', 'and marked as updating');
assert.equal((await (await staleHidden).json()).fetched_at, 3, 'concurrent slices share the same stale copy');
const staleSummary = partsApi.GET(req('?part=summary'));
assert.equal(calls, 6, 'the summary expired too and re-reads once; the inventory does not');
assert(lastArgs.includes('--summary'));
complete.resolve({ stdout: JSON.stringify({ ...summary, fetched_at: 7 }) });
assert.equal((await (await staleSummary).json()).inventory, 'warming', 'the summary agrees while the refresh runs');
staleRefresh.resolve({ stdout: JSON.stringify({ ...inventory, fetched_at: 4 }) });
await new Promise(r => setImmediate(r));
assert.equal((await (await partsApi.GET(req('?part=roofs&size=1'))).json()).fetched_at, 4, 'the refresh that ran behind the stale read serves the next one');
assert.equal(calls, 6);

// Long past its grace the stale copy is no longer served: the read waits on Python, and a failure is an honest
// 503 (with no-store, like every other answer) rather than data from a minute ago labelled as updating.
Date.now = () => base + 42000 + 20000 + 60000 + 1;
let settled = false;
const blocked = partsApi.GET(req('?part=roofs&size=1')).then(response => { settled = true; return response; });
assert.equal(calls, 7);
await new Promise(r => setImmediate(r));
assert(!settled, 'a long-expired inventory is not served stale');
complete.reject(new Error('private process details'));
const failure = await blocked;
assert.equal(failure.status, 503);
assert.equal(failure.headers.get('cache-control'), 'no-store', 'a failure is never cached by a proxy');
assert(!(await failure.text()).includes('private process details'));
Date.now = realNow;
console.log('Processing parts: separate summary coalescing, cached slices, one roof by key, hidden-low list, unknown part, background refresh, stale-while-refreshing and grace bound passed.');
