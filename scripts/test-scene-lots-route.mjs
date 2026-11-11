// GET /api/scenes/lots over a seeded temporary eyes index: every lot-tags-v1 row of one scene in one answer (the "_scene" row as
// the address, the object rows as dominant lot, flags and lot shares), the signature parameter choosing between rows of two image
// versions, stale when a served object row came from another version, malformed payloads and unsafe ids skipped, an unknown scene
// as an empty map (200), id validation (400), an unbuilt index or a Node without node:sqlite answering the same shape with source
// "unavailable", a corrupt index answering 500 without local details, GET-only, force-dynamic and no-store. Nothing here writes to
// the real index. The reader itself (readSceneLotTags in src/lib/queues.ts) is also run over a test double.
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
  new Function('exports', 'require', transpile('src/app/api/scenes/lots/route.ts'))(exports, id => {
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

// --- Seed: lot-30300 in miniature -------------------------------------------------------------------------------------------
const { DatabaseSync } = process.getBuiltinModule('node:sqlite');
const folder = mkdtempSync(`${tmpdir()}/scene-lots-`);
const indexFile = `${folder}/eyes.sqlite`;
const seed = new DatabaseSync(indexFile);
seed.exec('CREATE TABLE object_quality(scene_id TEXT, object_id TEXT, model_signature TEXT, version TEXT, payload TEXT, PRIMARY KEY(scene_id, object_id, model_signature, version))');
const insert = seed.prepare('INSERT INTO object_quality VALUES (?, ?, ?, ?, ?)');
const SCENE = 'lot-30300', SIG_OLD = '1'.repeat(64), SIG_NEW = '2'.repeat(64);
const tag = (dominant, lots, flags, extra = {}) => JSON.stringify({ version: 'lot-tags-v1', note: 'agent hint, not a label', tag: { kind: 'building', flags, edge_px: 8, lots, dominant, sentences: ['x.'], address_plan: 'PS631347', lot_complete_in_image: true, ...extra }, parcels_sha256: 'b'.repeat(64) });
const scene = (sceneFields) => JSON.stringify({ version: 'lot-tags-v1', note: 'agent hint, not a label', scene: { address_method: 'address-point', address_group_key: 'plan:PS631347', unit_site_sentence: 'u', registration: { ok: true }, lot_frame: { version: 'lot-frame-v1', sentence: 'frame' }, ...sceneFields }, parcels_sha256: 'b'.repeat(64) });
insert.run(SCENE, '_scene', SIG_NEW, 'lot-tags-v1', scene({ address_lot: '3', address_plan: 'PS631347', address_id: 30300 }));
const houseDominant = { kind: 'lot', lot: '3', pfi: '214342494', plan: 'PS631347', share: 0.9578 };
const houseLots = [{ lot: '3', plan: 'PS631347', pfi: '214342494', share: 0.9578, kind: 'lot', complete_in_image: true, key: 'k1', stacked_lots: [] }, { lot: 'CM1', plan: 'PS631347', pfi: '214342495', share: 0.0352, kind: 'common_property', complete_in_image: false, key: 'k2', stacked_lots: [] }];
insert.run(SCENE, '8130e74685e42d9e', SIG_NEW, 'lot-tags-v1', tag(houseDominant, houseLots, []));
// Vegetation on the school land: rows under two image versions, the newer one with one more flag so the choice is visible.
const vegDominant = { kind: 'lot', lot: '1', pfi: '1353120', plan: 'TP334258', share: 1 };
insert.run(SCENE, '4476ca392b82da51', SIG_OLD, 'lot-tags-v1', tag(vegDominant, [{ lot: '1', plan: 'TP334258', share: 1, kind: 'lot' }], ['frame_cut']));
insert.run(SCENE, '4476ca392b82da51', SIG_NEW, 'lot-tags-v1', tag(vegDominant, [{ lot: '1', plan: 'TP334258', share: 1, kind: 'lot' }], ['frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated']));
// The footpath: a row under the older image version only, so a newer signature finds it stale.
insert.run(SCENE, '486588f72a9bc859', SIG_OLD, 'lot-tags-v1', tag({ kind: 'lot', lot: '2', pfi: '1329926', plan: 'PS327018', share: 1 }, [{ lot: '2', plan: 'PS327018', share: 1, kind: 'lot' }], ['frame_cut', 'lot_unassociated']));
// No dominant lot (nothing under it), a malformed payload, a payload with no tag at all, an unsafe object id, another version, another scene.
insert.run(SCENE, 'nodominant', SIG_NEW, 'lot-tags-v1', tag(null, [], ['lot_unassociated']));
insert.run(SCENE, 'broken', SIG_NEW, 'lot-tags-v1', '{not json');
insert.run(SCENE, 'tagless', SIG_NEW, 'lot-tags-v1', JSON.stringify({ version: 'lot-tags-v1', note: 'no tag here' }));
insert.run(SCENE, '__proto__', SIG_NEW, 'lot-tags-v1', tag(houseDominant, houseLots, []));
insert.run(SCENE, 'a/b', SIG_NEW, 'lot-tags-v1', tag(houseDominant, houseLots, []));
insert.run(SCENE, '8130e74685e42d9e', SIG_NEW, 'object-hints-v2', JSON.stringify({ version: 'object-hints-v2', hints: [] }));
insert.run('other-scene', 'elsewhere', SIG_NEW, 'lot-tags-v1', tag(vegDominant, [], []));
// A subject window: its scene row names no plan; an object row under the current signature, the scene row under an older one.
insert.run('win-1', '_scene', SIG_OLD, 'lot-tags-v1', scene({ address_id: 30371, address_lot: null, address_plan: null }));
insert.run('win-1', 'roof', SIG_NEW, 'lot-tags-v1', tag({ kind: 'lot', lot: '7', pfi: '99', plan: 'LP34966', share: 0.97 }, [{ lot: '7', plan: 'LP34966', share: 0.97, kind: 'lot' }], []));
// A scene with object rows and no scene row at all.
insert.run('no-scene-row', 'roof', SIG_NEW, 'lot-tags-v1', tag(houseDominant, houseLots, []));
seed.close();

const route = load(indexFile);
assert.equal(route.dynamic, 'force-dynamic');
assert.equal(typeof route.GET, 'function');
for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.equal(route[verb], undefined, `GET-only (${verb})`);

// --- The whole scene under the current signature --------------------------------------------------------------------------
let response = await route.GET(request({ scene: SCENE, signature: SIG_NEW }));
assert.equal(response.status, 200);
let body = await clean(response);
assert.deepEqual(Object.keys(body).sort(), ['address', 'lot_tags_version', 'objects', 'scene_id', 'source', 'stale']);
assert.equal(body.scene_id, SCENE); assert.equal(body.lot_tags_version, 'lot-tags-v1'); assert.equal(body.source, 'eyes-index');
assert.deepEqual(body.address, { lot: '3', plan: 'PS631347', address_id: 30300 }, 'the "_scene" row is the address');
assert.deepEqual(Object.keys(body.objects).sort(), ['4476ca392b82da51', '486588f72a9bc859', '8130e74685e42d9e', 'nodominant'], 'every usable lot-tags-v1 object row of the scene; broken, tagless, unsafe ids, other versions and other scenes are out');
assert.deepEqual(body.objects['8130e74685e42d9e'], { dominant: { lot: '3', plan: 'PS631347', pfi: '214342494', share: 0.9578, kind: 'lot' }, flags: [],
  lots: [{ lot: '3', plan: 'PS631347', share: 0.9578, kind: 'lot' }, { lot: 'CM1', plan: 'PS631347', share: 0.0352, kind: 'common_property' }] }, 'dominant, flags and the lot shares with their kind; nothing else from the payload');
assert.deepEqual(body.objects['4476ca392b82da51'].dominant, { lot: '1', plan: 'TP334258', pfi: '1353120', share: 1, kind: 'lot' });
assert.deepEqual(body.objects['4476ca392b82da51'].flags, ['frame_cut', 'lot_incomplete_in_frame', 'lot_unassociated'], 'the row under the named signature is served');
assert.deepEqual(body.objects.nodominant, { dominant: null, flags: ['lot_unassociated'], lots: [] }, 'no dominant lot is served as null, never dropped');
assert.equal(body.stale, true, 'the footpath row exists only under the older signature: stale');
assert.equal(Object.getPrototypeOf(body.objects), Object.prototype, 'the __proto__ row never became the map prototype');
assert.equal(body.objects.__proto__, Object.prototype, 'and is not a key either');
// --- The older signature, no signature, a malformed signature -----------------------------------------------------------------
body = await clean(response = await route.GET(request({ scene: SCENE, signature: SIG_OLD })));
assert.equal(response.status, 200); assert.deepEqual(body.objects['4476ca392b82da51'].flags, ['frame_cut'], 'the older row is served under the older signature');
assert.equal(body.stale, true, 'the house row exists only under the newer signature: stale'); assert.deepEqual(body.address, { lot: '3', plan: 'PS631347', address_id: 30300 }, 'the scene row is served whatever its signature');
body = await clean(response = await route.GET(request({ scene: SCENE })));
assert.equal(body.stale, false, 'without a signature the image version is not judged'); assert.equal(Object.keys(body.objects).length, 4); assert.equal(body.objects['4476ca392b82da51'].dominant.plan, 'TP334258');
body = await clean(response = await route.GET(request({ scene: SCENE, signature: 'not-hex' })));
assert.equal(response.status, 200); assert.equal(body.stale, false, 'a malformed signature is ignored, never an error');
body = await clean(response = await route.GET(request({ scene: SCENE, signature: '3'.repeat(64) })));
assert.equal(body.stale, true, 'no row under the named signature: whatever is served is stale'); assert.equal(Object.keys(body.objects).length, 4, 'and still served');
// --- A subject window: the scene row carries no plan and its own signature never counts towards stale ---------------------------
body = await clean(response = await route.GET(request({ scene: 'win-1', signature: SIG_NEW })));
assert.equal(response.status, 200); assert.deepEqual(body.address, { lot: null, plan: null, address_id: 30371 }, 'a scene row without a plan is still the address row (the editor falls back to the planned lot)');
assert.equal(body.stale, false, 'the scene row describes the frame, not an outline: its older signature does not make the answer stale');
assert.deepEqual(Object.keys(body.objects), ['roof']);
body = await clean(response = await route.GET(request({ scene: 'no-scene-row', signature: SIG_NEW })));
assert.equal(body.address, null, 'no scene row: address null'); assert.equal(body.objects.roof.dominant.lot, '3'); assert.equal(body.stale, false);
// --- An unknown scene is an empty map, not a 404 ------------------------------------------------------------------------------
body = await clean(response = await route.GET(request({ scene: 'no-such-scene', signature: SIG_NEW })));
assert.equal(response.status, 200); assert.deepEqual(body, { scene_id: 'no-such-scene', lot_tags_version: 'lot-tags-v1', source: 'eyes-index', address: null, objects: {}, stale: false });
// --- Id validation ------------------------------------------------------------------------------------------------------------
for (const bad of [{}, { scene: '' }, { scene: '../x' }, { scene: '-flag' }, { scene: 'a b' }, { scene: 'x'.repeat(101) }, { scene: '_scene' }, { scene: 'a/b' }]) {
  response = await route.GET(request(bad));
  assert.equal(response.status, 400, `bad ids ${JSON.stringify(bad)}`);
  body = await clean(response); assert.equal(typeof body.error, 'string'); assert.deepEqual(Object.keys(body), ['error']);
}
// --- Unbuilt index and a Node without node:sqlite: same shape, empty, source unavailable ----------------------------------------
const absent = load(`${folder}/does-not-exist/eyes.sqlite`);
body = await clean(response = await absent.GET(request({ scene: SCENE, signature: SIG_NEW })));
assert.equal(response.status, 200, 'an unbuilt index is unavailable, never an error the editor has to handle');
assert.deepEqual(body, { scene_id: SCENE, lot_tags_version: 'lot-tags-v1', source: 'unavailable', address: null, objects: {}, stale: false });
const realBuiltin = process.getBuiltinModule;
process.getBuiltinModule = id => (id === 'node:sqlite' ? undefined : realBuiltin.call(process, id));
try {
  body = await clean(response = await route.GET(request({ scene: SCENE, signature: SIG_NEW })));
  assert.equal(response.status, 200); assert.equal(body.source, 'unavailable'); assert.deepEqual(body.objects, {}); assert.equal(body.address, null);
} finally { process.getBuiltinModule = realBuiltin; }
body = await clean(response = await route.GET(request({ scene: SCENE, signature: SIG_NEW })));
assert.equal(body.source, 'eyes-index', 'sqlite is back');
// --- Corrupt index: 500 with a safe message ---------------------------------------------------------------------------------------
const corruptFile = `${folder}/corrupt/eyes.sqlite`;
mkdirSync(path.dirname(corruptFile));
writeFileSync(corruptFile, 'this is not a database file at all, just text that is long enough to be read as a header');
body = await clean(response = await load(corruptFile).GET(request({ scene: SCENE, signature: SIG_NEW })));
assert.equal(response.status, 500); assert.match(body.error, /could not be read/i); assert.match(body.error, /unchanged/i); assert.deepEqual(Object.keys(body), ['error']);
// An index whose object_quality table is missing is a database error too (500), not a silent empty map.
const tablelessFile = `${folder}/tableless/eyes.sqlite`;
mkdirSync(path.dirname(tablelessFile));
new DatabaseSync(tablelessFile).close();
body = await clean(response = await load(tablelessFile).GET(request({ scene: SCENE })));
assert.equal(response.status, 500);

// --- The reader over a test double ------------------------------------------------------------------------------------------------
assert.equal(queues.LOT_TAG_LIST_SQL, 'SELECT object_id, payload, model_signature FROM object_quality WHERE version = ? AND scene_id = ? ORDER BY object_id', 'one query for the scene');
const calls = [];
const double = { prepare(sql) { calls.push(sql); return { get() { return undefined; }, all(...params) { calls.push(params); return [
  { object_id: '_scene', payload: scene({ address_lot: '3', address_plan: 'PS631347', address_id: 30300 }), model_signature: SIG_NEW },
  { object_id: 'a', payload: tag(houseDominant, houseLots, []), model_signature: SIG_OLD },
  { object_id: 'a', payload: tag(vegDominant, [], ['frame_cut']), model_signature: SIG_NEW },
  { object_id: 'b', payload: 42, model_signature: SIG_NEW },
  'not a row',
]; } }; }, close() { calls.push('close'); } };
let read = queues.readSceneLotTags(double, SCENE, SIG_NEW);
assert.deepEqual(calls, [queues.LOT_TAG_LIST_SQL, ['lot-tags-v1', SCENE]], 'one prepare, one all() with the version and the scene; the reader does not close the database');
assert.deepEqual(read.address, { lot: '3', plan: 'PS631347', address_id: 30300 });
assert.deepEqual(read.objects.a.dominant.plan, 'TP334258', 'the row under the current signature wins whatever the order');
assert.equal(read.stale, false); assert.deepEqual(Object.keys(read.objects), ['a']);
read = queues.readSceneLotTags(double, SCENE, SIG_OLD);
assert.equal(read.objects.a.dominant.plan, 'PS631347'); assert.equal(read.stale, false, 'the served row sits under the named signature');
read = queues.readSceneLotTags(double, SCENE, '3'.repeat(64));
assert.equal(read.stale, true, 'neither row matches: stale');
read = queues.readSceneLotTags({ prepare() { return { get() { return undefined; } }; }, close() {} }, SCENE, SIG_NEW);
assert.deepEqual(read, { address: null, objects: {}, stale: false }, 'a statement without all() reads as an empty scene');
assert.deepEqual(queues.parseSceneLotAddress({ scene: { address_lot: 3, address_plan: 'PS1', address_id: '7' } }), { lot: '3', plan: 'PS1', address_id: 7 }, 'numbers and numeric strings are read as text and number');
assert.equal(queues.parseSceneLotAddress({ tag: {} }), null, 'an object row is not an address');
assert.equal(queues.parseSceneLotAddress('{not json'), null);
assert.equal(queues.parseSceneLotEntry({ version: 'lot-tags-v1' }), null, 'a payload without a tag is null');
assert.deepEqual(queues.parseSceneLotEntry(tag(null, [{ lot: '1', plan: 'P', share: 'bad' }, { lot: '2', plan: 'P', share: 0.5, kind: 'zebra' }], ['f'])), { dominant: null, flags: ['f'], lots: [{ lot: '2', plan: 'P', share: 0.5, kind: null }] }, 'a share that is not a number drops the lot; an unknown kind reads as null');
// --- The client coercer over a route body ----------------------------------------------------------------------------------------
body = await clean(await route.GET(request({ scene: SCENE, signature: SIG_NEW })));
let coerced = queues.coerceSceneLotTags(body);
assert.deepEqual(coerced.address, { lot: '3', plan: 'PS631347', address_id: 30300 }); assert.equal(coerced.stale, true);
assert.deepEqual(Object.keys(coerced.objects).sort(), ['4476ca392b82da51', '486588f72a9bc859', '8130e74685e42d9e', 'nodominant']);
assert.deepEqual(coerced.objects['8130e74685e42d9e'], body.objects['8130e74685e42d9e'], 'a served entry reads back unchanged');
assert.deepEqual(coerced.objects.nodominant, { dominant: null, flags: ['lot_unassociated'], lots: [] });
coerced = queues.coerceSceneLotTags({ ...body, objects: { ...body.objects, '__proto__': body.objects['8130e74685e42d9e'], 'a/b': body.objects['8130e74685e42d9e'], junk: 'not an entry', empty: {} } });
assert.deepEqual(Object.keys(coerced.objects).sort(), ['4476ca392b82da51', '486588f72a9bc859', '8130e74685e42d9e', 'nodominant'], 'unsafe keys and malformed entries are dropped on the client too');
assert.equal(Object.getPrototypeOf(coerced.objects), Object.prototype);
assert.equal(queues.coerceSceneLotTags(await clean(await absent.GET(request({ scene: SCENE })))), null, 'an unavailable answer coerces to null: the scope stays inert');
for (const bad of [null, undefined, 'text', 42, {}, { source: 'eyes-index' }, { source: 'eyes-index', objects: [] }, { error: 'x' }]) assert.equal(queues.coerceSceneLotTags(bad), null, `not a body: ${JSON.stringify(bad)}`);
assert.deepEqual(queues.coerceSceneLotTags({ source: 'eyes-index', objects: {}, address: null }), { address: null, objects: {}, stale: false }, 'an empty map with no address is a real answer (nothing to filter)');
console.log('scene lots route: GET-only, force-dynamic, no-store, the whole scene from one query, the "_scene" address, the signature choosing between image versions, stale, malformed and unsafe rows skipped, an empty map for an unknown scene, 400/500 without leaks, the unavailable shape and the reader over a double passed.');
