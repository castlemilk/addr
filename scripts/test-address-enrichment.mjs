// Per-address enrichment (address-enrichment-v1): the lenient parser in src/lib/address-enrichment.ts over valid and
// malformed documents, the plain-words copy the sidebar prints, and GET /api/address/enrichment over a temporary
// directory: 400 for a non-integer, 404 when no document exists, 500 for an unreadable one, 200 with the validated
// document, GET-only, force-dynamic, no-store, and never a local path in a body. Nothing here touches data/.
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
const lib = {};
new Function('exports', 'require', transpile('src/lib/address-enrichment.ts'))(lib, id => require(id));
// The route joins ENRICHMENT_ROOT onto process.cwd(); each load points it at a temp folder (repo-relative).
const loadRoute = root => {
  const exports = {};
  new Function('exports', 'require', transpile('src/app/api/address/enrichment/route.ts'))(exports, id => {
    if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
    if (id === '@/lib/address-enrichment') return { ...lib, ENRICHMENT_ROOT: path.relative(process.cwd(), root) };
    return require(id);
  });
  return exports;
};

// --- A complete document, exactly the pipeline's schema -------------------------------------------------------------
const doc = {
  version: 'address-enrichment-v1', note: 'agent hints and model estimates, not labels or accuracy', address_id: 29790, full_address: '27 CAMBRIDGE ROAD, Mooroolbark VIC 3138',
  property_pfi: '1348074', lot: { plan: 'LP12345', lot: '7' },
  allocation: { status: 'matched', method: 'vicmap-property-lot', confidence: 'moderate', reason: 'roof centroid inside the property lot', alternatives: [{ address_id: 29791, full_address: '3 HERMES CLOSE' }] },
  primary_roof: { scene_id: 'house-29790', object_id: '7fdd138f819cb06c', area_m2: 181.4, roof_type: 'hip', band: 'medium', review_url: '/scenes?scene=house-29790&object=7fdd138f819cb06c&kind=building&status=all&layer=model' },
  structures: [
    { scene_id: 'house-29790', object_id: 'g1', sub_kind: 'garage', confidence: 'strong', area_m2: 63.379, review_url: '/scenes?scene=house-29790&object=g1' },
    { scene_id: 'house-29790', object_id: 's1', sub_kind: 'shed', confidence: 'weak', area_m2: 9.2, review_url: 'https://evil.example/phish' },
    { scene_id: 'house-29790', object_id: 'c1', sub_kind: 'container-stack', confidence: 'moderate', area_m2: 60, review_url: null },
  ],
  solar: { count: 2, area_m2: 18.4 }, trees: { count_on_lot: 3 }, pool: { present: false }, roofs_total: 4, computed_at: '2026-09-26T09:00:00+00:00',
};
let parsed = lib.parseEnrichment(doc);
assert.equal(parsed.address_id, 29790); assert.equal(parsed.version, 'address-enrichment-v1'); assert.equal(parsed.property_pfi, '1348074');
assert.deepEqual(parsed.lot, { plan: 'LP12345', lot: '7' });
assert.deepEqual(parsed.allocation, doc.allocation);
assert.deepEqual(parsed.primary_roof, doc.primary_roof);
assert.equal(parsed.structures.length, 3);
assert.equal(parsed.structures[1].review_url, null, 'an off-site review link is dropped, the structure kept');
assert.deepEqual(parsed.solar, { count: 2, area_m2: 18.4 }); assert.deepEqual(parsed.trees, { count_on_lot: 3 }); assert.deepEqual(parsed.pool, { present: false });
assert.equal(parsed.roofs_total, 4); assert.equal(parsed.computed_at, doc.computed_at);
assert.deepEqual(lib.parseEnrichment(JSON.stringify(doc)), parsed, 'JSON text parses the same');
assert.equal(lib.ENRICHMENT_ROOT, 'data/reviews/eyes/address-enrichment/addresses');
assert.equal(lib.ENRICHMENT_VERSION, 'address-enrichment-v1');
assert.match(lib.ENRICHMENT_NOTE, /not labels/);

// --- Plain-words copy --------------------------------------------------------------------------------------------------
assert.equal(lib.describeAllocation(parsed.allocation), 'Allocated to this address by Vicmap property lot; moderate. Alternatives: 3 HERMES CLOSE.');
assert.equal(lib.describeAllocation({ status: 'suggested', method: 'nearest_gnaf_point', confidence: 'weak', reason: '', alternatives: [] }), 'Suggested for this address by nearest G-NAF point; weak.');
assert.equal(lib.describeAllocation({ status: 'ambiguous', method: 'vicmap-property-lot', confidence: 'weak', reason: '', alternatives: [{ address_id: 1, full_address: '3 HERMES CLOSE' }, { address_id: null, full_address: '5 HERMES CLOSE' }] }), 'Allocation is ambiguous by Vicmap property lot; weak. Alternatives: 3 HERMES CLOSE, 5 HERMES CLOSE.');
assert.equal(lib.describeAllocation({ status: 'shared', method: 'vicmap-property-lot', confidence: 'strong', reason: '', alternatives: [{ address_id: 2, full_address: '1/27 CAMBRIDGE ROAD' }] }), 'Shared with a neighbouring address by Vicmap property lot; strong. Alternatives: 1/27 CAMBRIDGE ROAD.');
assert.equal(lib.describeAllocation({ status: 'unmatched', method: '', confidence: 'weak', reason: '', alternatives: [] }), 'No roof allocated to this address; weak.');
assert.equal(lib.describeAllocation(null), null);
for (const line of [lib.describeAllocation(parsed.allocation)]) assert(!line.includes(' · ') && !line.includes('→') && !/[A-Z]{4,}(?![A-Z ])/.test(line.replace(/HERMES CLOSE/g, '')), `quiet copy: ${line}`);
assert.equal(lib.structureLine(parsed.structures[0]), 'Garage, 63 m² (strong)');
assert.equal(lib.structureLine(parsed.structures[2]), 'Container stack, 60 m² (moderate)');
assert.equal(lib.structureLine({ sub_kind: 'pool-cover', confidence: null, area_m2: null, scene_id: null, object_id: null, review_url: null }), 'Pool cover');
assert.equal(lib.structureLine({ sub_kind: 'mystery_kind', confidence: 'weak', area_m2: 4.6, scene_id: null, object_id: null, review_url: null }), 'Mystery kind, 5 m² (weak)', 'unknown kinds read in sentence case');
assert.equal(lib.primaryRoofLine(parsed.primary_roof), 'Hip roof, 181 m², medium band (model estimate)');
assert.equal(lib.primaryRoofLine({ scene_id: null, object_id: null, area_m2: null, roof_type: null, band: null, review_url: null }), 'Roof');
assert.equal(lib.primaryRoofLine(null), null);
assert.equal(lib.solarLine(parsed.solar), 'Solar: 2 detections, 18 m²');
assert.equal(lib.solarLine({ count: 1, area_m2: 0 }), 'Solar: 1 detection');
assert.equal(lib.solarLine({ count: 0, area_m2: 0 }), null, 'no solar means no line');
assert.deepEqual(lib.lotLines(parsed), ['Trees on the lot: 3', 'Pool: none detected']);
assert.deepEqual(lib.lotLines({ ...parsed, trees: { count_on_lot: null }, pool: { present: true } }), ['Pool: present (model estimate)']);
assert.deepEqual(lib.lotLines({ ...parsed, trees: { count_on_lot: null }, pool: { present: null } }), []);
assert.equal(lib.structureSummary(parsed), '4 roofs: 1 garage, 1 shed, 1 container stack');
assert.equal(lib.structureSummary({ ...parsed, structures: [] }), '4 roofs, no other structures');
assert.equal(lib.structureSummary({ ...parsed, structures: [], roofs_total: null, primary_roof: null }), null);
assert.equal(lib.structureSummary({ ...parsed, roofs_total: null }), '4 roofs: 1 garage, 1 shed, 1 container stack', 'roofs are counted from the roof and structures when the total is missing');
assert.equal(lib.structureSummary({ ...parsed, roofs_total: 1, structures: [parsed.structures[0], parsed.structures[0]] }), '1 roof: 2 garages');
assert.equal(lib.methodWords('vicmap_property-lot'), 'Vicmap property lot'); assert.equal(lib.methodWords('osm.building'), 'OpenStreetMap building');

// --- Malformed documents degrade, never throw ---------------------------------------------------------------------------
for (const bad of [null, undefined, 'nope', '{not json', 42, [], {}, { address_id: 'abc' }, { address_id: 1.5 }, { address_id: -1 }, { version: 'address-enrichment-v1' }]) {
  assert.equal(lib.parseEnrichment(bad), null, `no integer address_id means no document: ${JSON.stringify(bad)}`);
}
parsed = lib.parseEnrichment({ address_id: '29790', allocation: { status: 'zebra' }, primary_roof: 'x', structures: [{ sub_kind: 7 }, 'junk', { sub_kind: 'garage', confidence: 9, area_m2: 'big' }], solar: { count: -3, area_m2: 'x' }, trees: 5, pool: { present: 'yes' }, roofs_total: '2', lot: { plan: 4 } });
assert.equal(parsed.address_id, 29790, 'a numeric string id is accepted');
assert.equal(parsed.allocation, null, 'an unknown allocation status is null');
assert.equal(parsed.primary_roof, null); assert.equal(parsed.version, null); assert.equal(parsed.full_address, null);
assert.deepEqual(parsed.structures, [{ scene_id: null, object_id: null, sub_kind: 'garage', confidence: null, area_m2: null, review_url: null }], 'structures without a sub_kind are skipped, bad fields are null');
assert.deepEqual(parsed.solar, { count: 0, area_m2: 0 }); assert.deepEqual(parsed.trees, { count_on_lot: null }); assert.deepEqual(parsed.pool, { present: null });
assert.equal(parsed.roofs_total, 2); assert.equal(parsed.lot, null, 'a lot with nothing readable is null');
parsed = lib.parseEnrichment({ address_id: 1, allocation: { status: 'matched', confidence: 'certain', alternatives: [{ full_address: 7 }, { address_id: 2, full_address: '9 X ST' }, 'junk'] } });
assert.deepEqual(parsed.allocation, { status: 'matched', method: '', confidence: 'weak', reason: '', alternatives: [{ address_id: 2, full_address: '9 X ST' }] }, 'unknown confidence is the weakest claim; alternatives without an address are dropped');
assert.equal(lib.describeAllocation(parsed.allocation), 'Allocated to this address; weak. Alternatives: 9 X ST.');
assert.equal(lib.structureSummary(parsed), null);

// --- The route over a temporary folder --------------------------------------------------------------------------------
const folder = mkdtempSync(`${tmpdir()}/address-enrichment-`);
const root = `${folder}/addresses`; mkdirSync(root);
writeFileSync(`${root}/29790.json`, JSON.stringify(doc));
writeFileSync(`${root}/7.json`, '{not json');
writeFileSync(`${root}/8.json`, JSON.stringify({ version: 'address-enrichment-v1' }));
mkdirSync(`${root}/9.json`);
const route = loadRoute(root);
assert.equal(route.dynamic, 'force-dynamic');
assert.equal(typeof route.GET, 'function');
for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.equal(route[verb], undefined, `GET-only (${verb})`);
const request = params => ({ nextUrl: { searchParams: new URLSearchParams(params) } });
const clean = async response => {
  assert.equal(response.headers.get('cache-control'), 'no-store');
  const body = await response.text();
  assert(!body.includes('/Users') && !body.includes(tmpdir()) && !body.includes('ENOENT') && !body.includes('EISDIR') && !body.includes('addresses/'), `no local details in ${body.slice(0, 200)}`);
  return JSON.parse(body);
};
let response = await route.GET(request({ address: '29790' }));
assert.equal(response.status, 200);
let body = await clean(response);
assert.deepEqual(body, lib.parseEnrichment(doc), 'the body is the validated document');
assert.equal(body.structures[1].review_url, null, 'validation runs on the way out');
for (const bad of [{}, { address: '' }, { address: 'abc' }, { address: '29790abc' }, { address: '-1' }, { address: '1.5' }, { address: '../29790' }, { address: '1'.repeat(16) }, { address: '29790 ' + 'x' }]) {
  response = await route.GET(request(bad));
  assert.equal(response.status, 400, `non-integer address ${JSON.stringify(bad)}`);
  body = await clean(response); assert.equal(typeof body.error, 'string');
}
response = await route.GET(request({ address: ' 29790 ' }));
assert.equal(response.status, 200, 'surrounding whitespace is trimmed');
for (const missing of ['1', '0', '999999999999999']) {
  response = await route.GET(request({ address: missing }));
  assert.equal(response.status, 404, `no document means 404 (${missing})`);
  body = await clean(response); assert.match(body.error, /no enrichment/i);
}
response = await route.GET(request({ address: '7' })); assert.equal(response.status, 500, 'unparseable JSON is a 500, not a 404');
body = await clean(response); assert.match(body.error, /malformed/i); assert.match(body.error, /unchanged/i);
response = await route.GET(request({ address: '8' })); assert.equal(response.status, 500, 'a document without an address id is malformed');
response = await route.GET(request({ address: '9' })); assert.equal(response.status, 500, 'a directory in place of the file cannot be read');
body = await clean(response); assert.match(body.error, /could not be read/i);
const absent = loadRoute(`${folder}/does-not-exist/addresses`);
response = await absent.GET(request({ address: '29790' })); assert.equal(response.status, 404, 'an unbuilt enrichment folder answers 404 like a missing file');
console.log('address enrichment: lenient parser over valid and malformed documents, plain-words allocation, roof, structure, solar and lot copy, and GET /api/address/enrichment (GET-only, force-dynamic, no-store, 400/404/500 without leaks, 200 validated) passed.');
