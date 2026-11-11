// Pure server-side slicing of the cached processing inventory (src/lib/scene-processing-slice.ts):
// filter by band / review / address / free text, stable sorts, bounded paging, trimmed rows,
// one full roof by key, and the low-roofs-hidden-under-higher-bands list. No network, no React.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const modules = new Map();
function load(file) {
  const resolved = path.resolve(file);
  if (modules.has(resolved)) return modules.get(resolved);
  const exports = {};
  modules.set(resolved, exports);
  const code = ts.transpileModule(readFileSync(resolved, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  new Function('exports', 'require', code)(exports, id => {
    if (id.startsWith('./')) return load(path.join(path.dirname(resolved), id + '.ts'));
    if (id.startsWith('@/')) return load(path.join('src', id.slice(2) + '.ts'));
    return require(id);
  });
  return exports;
}
const slice = load('src/lib/scene-processing-slice.ts');
const { buildingAddressTitle } = load('src/lib/building-addresses.ts');

// 50 roofs: bands cycle through low/medium/high/null/missing, every fifth lacks eyes, every
// seventh lacks search_text, every ninth lacks an address, areas repeat so sorts must tie-break.
const bands = ['low', 'medium', 'high', null, undefined];
const reviews = ['candidate', 'candidate', 'needs_adjustment', 'confirmed', 'stale', 'rejected'];
const addresses = ['matched', 'ambiguous', 'shared', 'unmatched'];
const feature = i => {
  const key = `scene-${i % 4}/obj-${String(i).padStart(2, '0')}`;
  const properties = {
    key, scene_id: `scene-${i % 4}`, object_id: `obj-${String(i).padStart(2, '0')}`, model_signature: 'sig',
    label: `Building ${i}`, capture_title: `${i} Cambridge Road`, band: bands[i % 5], band_label: 'x', color: '#000',
    reasons: [], area_m2: 100 + (i % 10) * 10, detector_score: i % 6 === 0 ? null : Math.round((i % 9) / 9 * 100) / 100,
    review_status: reviews[i % reviews.length], issue_count: 0, review_url: `/scenes?scene=scene-${i % 4}&object=obj-${i}`,
    interval: null, calibration_groups: 0,
    property_address: i % 9 === 0 ? null : { status: addresses[i % 4], full_address: `${(i % 13) + 1} GREENVILLE STREET`, candidates: [], reason: '' },
    hidden_overlaps: undefined,
  };
  if (i % 5 !== 0) properties.eyes = { diagnostics: { planes: i } };
  if (i % 7 !== 0) properties.search_text = `Building ${i} ${i} Cambridge Road scene-${i % 4} obj-${i} ${(i % 13) + 1} GREENVILLE STREET`;
  if (bands[i % 5] === undefined) delete properties.band;
  return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[145, -37], [145.001, -37], [145.001, -37.001], [145, -37]]] }, properties };
};
const features = Array.from({ length: 50 }, (_, i) => feature(i));
const inventory = { features, counts: { low: 10, medium: 10, high: 10 }, address_counts: { matched: 12 }, fetched_at: 123, duplicates: 4 };
const keys = rows => rows.map(r => r.properties.key);

// Defaults: page 1 of 50, low evidence first, detector ascending with unknown scores last, trimmed rows.
let page = slice.sliceRoofs(inventory, {});
assert.equal(page.total, 50); assert.equal(page.page, 1); assert.equal(page.size, 50); assert.equal(page.pages, 1);
assert.equal(page.rows.length, 50); assert.equal(page.sort, 'evidence');
assert.deepEqual(page.counts, inventory.counts); assert.deepEqual(page.address_counts, inventory.address_counts); assert.equal(page.fetched_at, 123);
assert(page.rows.every(r => !('search_text' in r.properties)), 'rows never carry the search text');
assert(page.rows.every(r => r.geometry && r.properties.key && 'review_status' in r.properties), 'rows keep geometry and review state');
assert.equal(page.rows.filter(r => r.properties.eyes).length, 40, 'eyes and hidden overlaps travel with the row when present');
assert(features.every(f => typeof f.properties.band !== 'string' || f.properties.band !== 'unassessed'), 'fixture bands are never explicitly unassessed');
const bandOrder = page.rows.map(r => r.properties.band ?? 'unassessed');
assert.deepEqual([...new Set(bandOrder)], ['low', 'medium', 'high', 'unassessed'], 'evidence sort walks low to high with unknown bands last');
const lows = page.rows.filter(r => r.properties.band === 'low').map(r => r.properties.detector_score);
assert.deepEqual(lows, [...lows.filter(v => v != null).sort((a, b) => a - b), ...lows.filter(v => v == null)], 'within a band, weaker detector first, unknown last');

// Paging bounds: invalid or oversized values are clamped, never rejected.
page = slice.sliceRoofs(inventory, { size: '7', page: '8' });
assert.equal(page.pages, 8); assert.equal(page.page, 8); assert.equal(page.rows.length, 1);
assert.equal(slice.sliceRoofs(inventory, { size: 7, page: 99 }).page, 8, 'a page past the end lands on the last page');
assert.equal(slice.sliceRoofs(inventory, { size: 7, page: 0 }).page, 1);
assert.equal(slice.sliceRoofs(inventory, { size: 7, page: 'abc' }).page, 1);
assert.equal(slice.sliceRoofs(inventory, { size: -3 }).size, 1);
assert.equal(slice.sliceRoofs(inventory, { size: 10000 }).size, 200);
assert.equal(slice.sliceRoofs(inventory, { size: 'x' }).size, 50);
assert.equal(slice.sliceRoofs(inventory, { size: '3.9' }).size, 3, 'sizes are whole numbers');
const pageTwo = slice.sliceRoofs(inventory, { size: 10, page: 2 });
assert.deepEqual(keys(pageTwo.rows), keys(slice.sliceRoofs(inventory, { size: 50 }).rows).slice(10, 20), 'pages are windows over one stable order');
assert.equal(slice.sliceRoofs({ ...inventory, features: [] }, {}).pages, 1, 'an empty inventory still has one page');

// Filters: null and missing bands are "unassessed"; review and address status; free text over search_text.
assert.equal(slice.sliceRoofs(inventory, { band: 'unassessed' }).total, 20);
assert.equal(slice.sliceRoofs(inventory, { band: 'low' }).total, 10);
assert.equal(slice.sliceRoofs(inventory, { band: 'nonsense' }).total, 50, 'an unknown band filter is ignored');
assert.equal(slice.sliceRoofs(inventory, { review: 'confirmed' }).total, features.filter(f => f.properties.review_status === 'confirmed').length);
assert.equal(slice.sliceRoofs(inventory, { address: 'matched' }).total, features.filter(f => f.properties.property_address?.status === 'matched').length);
assert.equal(slice.sliceRoofs(inventory, { address: 'unmatched' }).total, features.filter(f => f.properties.property_address?.status === 'unmatched').length, 'a missing address is not "unmatched"');
assert.deepEqual(keys(slice.sliceRoofs(inventory, { q: ' OBJ-07 ' }).rows), ['scene-3/obj-07'], 'search is trimmed and case-insensitive over search_text');
assert.deepEqual(keys(slice.sliceRoofs(inventory, { q: 'Building 14' }).rows), ['scene-2/obj-14'], 'a roof without search_text still matches on its label, capture and key');
assert.equal(slice.sliceRoofs(inventory, { q: 'zzz' }).total, 0);
assert.equal(slice.sliceRoofs(inventory, { q: 'zzz' }).rows.length, 0);
assert.equal(slice.sliceRoofs(inventory, { band: 'low', review: 'candidate', address: 'matched', q: 'greenville' }).total,
  features.filter(f => f.properties.band === 'low' && f.properties.review_status === 'candidate' && f.properties.property_address?.status === 'matched').length, 'filters combine');
assert.deepEqual(slice.sliceRoofs(inventory, { band: 'low' }).counts, inventory.counts, 'counts stay inventory-wide denominators, not the filtered total');

// Sorts: every key is stable, ties break on the roof key in both directions, unknown sorts fall back.
for (const sort of ['evidence', 'address', 'area', 'review']) {
  const a = keys(slice.sliceRoofs(inventory, { sort }).rows), b = keys(slice.sliceRoofs({ ...inventory, features: [...features].reverse() }, { sort }).rows);
  assert.deepEqual(a, b, `${sort} order does not depend on input order`);
  const desc = keys(slice.sliceRoofs(inventory, { sort: `-${sort}` }).rows);
  assert.notDeepEqual(desc, a, `-${sort} reverses the order`);
  assert.equal(new Set(desc).size, 50);
}
const areas = slice.sliceRoofs(inventory, { sort: 'area' }).rows;
for (let i = 1; i < areas.length; i++) {
  const a = areas[i - 1].properties, b = areas[i].properties;
  assert(a.area_m2 < b.area_m2 || (a.area_m2 === b.area_m2 && a.key < b.key), 'area ascending, ties by key');
}
const areasDesc = slice.sliceRoofs(inventory, { sort: '-area' }).rows;
for (let i = 1; i < areasDesc.length; i++) {
  const a = areasDesc[i - 1].properties, b = areasDesc[i].properties;
  assert(a.area_m2 > b.area_m2 || (a.area_m2 === b.area_m2 && a.key < b.key), 'area descending keeps the key tie-break ascending');
}
const byAddress = slice.sliceRoofs(inventory, { sort: 'address' }).rows.map(r => r.properties.property_address?.full_address || null);
const numbered = byAddress.filter(Boolean).map(a => Number(a.split(' ')[0]));
assert.deepEqual(numbered, [...numbered].sort((a, b) => a - b), 'addresses sort numerically: 7 before 12');
assert.equal(slice.sliceRoofs(inventory, { sort: 'bogus' }).sort, 'evidence');
assert.equal(slice.sliceRoofs(inventory, { sort: '-review' }).sort, '-review');
const reviewOrder = slice.sliceRoofs(inventory, { sort: 'review' }).rows.map(r => r.properties.review_status);
assert.deepEqual([...new Set(reviewOrder)], ['candidate', 'needs_adjustment', 'stale', 'confirmed', 'rejected'], 'review sort puts unreviewed work first');

// One full roof by key keeps everything, including search_text.
const full = slice.roofByKey(features, 'scene-1/obj-01');
assert.equal(full.properties.search_text, features[1].properties.search_text);
assert.equal(slice.roofByKey(features, 'nope'), undefined);
assert.equal(slice.roofByKey(features, ''), undefined);

// Low roofs hidden under a higher band: from hidden_overlaps on the kept roof, or from an eyes
// cross-capture row that says a low roof is hidden by a higher-band capture. Deduplicated by key.
const hidden = [
  { ...feature(100), properties: { ...feature(100).properties, band: 'medium', hidden_overlaps: [
    { scene_id: 'scan', object_id: 'low-a', band: 'low', iou: 0.9, detector_score: 0.5, image_line_mismatch_m: null, observed_segments: 0, clipped: false },
    { scene_id: 'scan', object_id: 'med-b', band: 'medium', iou: 0.8, detector_score: 0.5, image_line_mismatch_m: null, observed_segments: 0, clipped: false }] } },
  { ...feature(101), properties: { ...feature(101).properties, band: 'high', hidden_overlaps: [
    { scene_id: 'scan', object_id: 'low-c', band: 'low', iou: 0.7, detector_score: null, image_line_mismatch_m: null, observed_segments: null, clipped: null }] } },
  { ...feature(102), properties: { ...feature(102).properties, band: 'low', hidden_overlaps: [
    { scene_id: 'scan', object_id: 'low-d', band: 'low', iou: 0.95, detector_score: 0.5, image_line_mismatch_m: null, observed_segments: 0, clipped: false }] } },
  { ...feature(103), properties: { ...feature(103).properties, band: 'low', key: 'scan/low-e', scene_id: 'scan', object_id: 'low-e',
    eyes: { cross_capture: { n_pairs: 2, hidden_by: ['house-1/h1'], bands: [{ scene_id: 'house-1', object_id: 'h1', band: 'medium', iou: 0.88 }, { scene_id: 'house-2', object_id: 'h2', band: 'low', iou: 0.6 }] } } } },
  { ...feature(104), properties: { ...feature(104).properties, band: 'low', key: 'scan/low-f', scene_id: 'scan', object_id: 'low-f',
    eyes: { cross_capture: { n_pairs: 1, hidden_by: ['house-3/h3'], bands: [{ scene_id: 'house-3', object_id: 'h3', band: 'low', iou: 0.7 }] } } } },
  { ...feature(105), properties: { ...feature(105).properties, band: 'medium', hidden_overlaps: [
    { scene_id: 'scan', object_id: 'low-a', band: 'low', iou: 0.66, detector_score: 0.5, image_line_mismatch_m: null, observed_segments: 0, clipped: false }] } },
];
const rows = slice.hiddenLowRoofs([...features, ...hidden]);
assert.deepEqual(rows.map(r => r.key), ['scan/low-c', 'scan/low-a', 'scan/low-e'], 'high hider first, then IoU, deduplicated');
assert.deepEqual(rows.map(r => r.hidden_by.band), ['high', 'medium', 'medium']);
assert.equal(rows[1].iou, 0.9, 'the strongest overlap is kept for a roof hidden twice');
assert.equal(rows[0].review_url, '/scenes?scene=scan&object=low-c&kind=building&status=all&layer=model');
assert.equal(rows[0].hidden_by.key, hidden[1].properties.key);
assert.equal(rows[0].hidden_by.address, '11 GREENVILLE STREET');
assert.equal(rows[2].hidden_by.key, 'house-1/h1');
assert(rows.every(r => r.band === 'low'));
assert.equal(slice.hiddenLowRoofs(features).length, 0, 'no overlaps, no hidden roofs');

// Degenerate payloads never take the route down: a missing features array, GeoJSON properties: null, a null
// overlap entry and a cross-capture row without bands are skipped, not thrown.
const nullProps = { type: 'Feature', geometry: features[0].geometry, properties: null };
assert.equal(slice.sliceRoofs({}, {}).total, 0, 'no features array is an empty inventory');
assert.equal(slice.sliceRoofs({ features: [nullProps, features[1]] }, {}).total, 1, 'a feature without properties is skipped');
assert.deepEqual(keys(slice.sliceRoofs({ features: [nullProps, features[1]] }, { sort: 'address', q: 'building' }).rows), [features[1].properties.key]);
assert.equal(slice.roofByKey(undefined, 'scene-1/obj-01'), undefined);
assert.equal(slice.roofByKey([nullProps, features[1]], 'scene-1/obj-01'), features[1]);
const broken = [
  { ...feature(200), properties: { ...feature(200).properties, band: 'medium', hidden_overlaps: [null, { scene_id: 'scan', object_id: 'low-z', band: 'low', iou: 0.5 }, { band: 'low', iou: 0.9 }] } },
  { ...feature(201), properties: { ...feature(201).properties, band: 'low', eyes: { cross_capture: { hidden_by: ['house-9/h9'] } } } },
  nullProps,
];
assert.deepEqual(slice.hiddenLowRoofs(broken).map(r => r.key), ['scan/low-z'], 'null and keyless overlap entries and bandless cross-capture rows are skipped');

// "Higher band" means medium or high: a not-assessed keeper or hider never puts a low roof on this list.
const notAssessed = [
  { ...feature(300), properties: { ...feature(300).properties, band: null, hidden_overlaps: [{ scene_id: 'scan', object_id: 'low-n', band: 'low', iou: 0.9 }] } },
  { ...feature(301), properties: { ...feature(301).properties, band: 'low', key: 'scan/low-m', scene_id: 'scan', object_id: 'low-m',
    eyes: { cross_capture: { n_pairs: 1, hidden_by: ['house-4/h4'], bands: [{ scene_id: 'house-4', object_id: 'h4', band: 'unassessed', iou: 0.9 }] } } } },
];
assert.deepEqual(slice.hiddenLowRoofs(notAssessed), [], 'not assessed is unknown, not higher');
assert(slice.hiddenLowRoofs([...features, ...hidden]).every(r => ['medium', 'high'].includes(r.hidden_by.band)));

// hidden_by.address is the footprint's address: the hider's own match when that capture is in the inventory,
// otherwise the hidden roof's match for the same footprint (never an invented one).
const hider = { ...feature(400), properties: { ...feature(400).properties, key: 'house-1/h1', scene_id: 'house-1', object_id: 'h1', band: 'medium',
  property_address: { status: 'matched', full_address: '27 CAMBRIDGE ROAD', candidates: [], reason: '' } } };
assert.equal(slice.hiddenLowRoofs([hider, hidden[3]])[0].hidden_by.address, '27 CAMBRIDGE ROAD', 'the hider in the inventory supplies its address');
assert.equal(slice.hiddenLowRoofs([hidden[3]])[0].hidden_by.address, buildingAddressTitle(hidden[3].properties.property_address), 'otherwise the hidden roof\'s own footprint match');
assert.equal(slice.hiddenLowRoofs([hidden[3]])[0].hidden_by.address, '13 GREENVILLE STREET');
console.log('Processing slice: filters over null bands and missing eyes, bounded paging, stable sorts, trimmed rows, roof by key, hidden-low list and degenerate payloads passed.');
