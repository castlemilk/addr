// Recently opened scenes: a small localStorage list behind the /scenes hub.
// Checks the contract SceneStart relies on: record/read round-trip, dedupe by id,
// newest first, a cap of 8, and corrupt or foreign storage contents reading as [].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const load = file => {
  const exports = {};
  const code = ts.transpileModule(readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  new Function('exports', 'require', code)(exports, () => { throw new Error('recent-scenes must not import anything'); });
  return exports;
};
const { RECENT_SCENES_KEY, RECENT_SCENES_LIMIT, readRecentScenes, recordRecentScene } = load('src/lib/recent-scenes.ts');

/** Minimal Storage stand-in: getItem/setItem over a Map, so no DOM is needed. */
const storage = (initial = {}) => {
  const map = new Map(Object.entries(initial));
  return { getItem: key => (map.has(key) ? map.get(key) : null), setItem: (key, value) => { map.set(key, String(value)); }, map };
};
let tick = 0;
const now = () => new Date(Date.UTC(2026, 8, 26, 10, 0, tick++)).toISOString();

assert.equal(RECENT_SCENES_LIMIT, 8, 'the hub shows at most eight recent scenes');
assert.equal(typeof RECENT_SCENES_KEY, 'string');

// Empty and absent storage read as [].
assert.deepEqual(readRecentScenes(storage()), []);
assert.deepEqual(readRecentScenes(null), [], 'no storage (server render) reads as []');
assert.deepEqual(readRecentScenes(undefined), []);

// Round trip.
let store = storage();
recordRecentScene('house-30286', '29 Cambridge Road', store, now);
assert.deepEqual(readRecentScenes(store), [{ id: 'house-30286', title: '29 Cambridge Road', opened: '2026-09-26T10:00:00.000Z' }]);
assert.equal(typeof store.getItem(RECENT_SCENES_KEY), 'string', 'the list is stored as JSON under the documented key');

// Newest first, and re-opening an existing scene moves it to the top without duplicating it.
recordRecentScene('house-25822', '7 Greenville Street', store, now);
recordRecentScene('roof_training_250-29790', 'Cohort roof 29790', store, now);
assert.deepEqual(readRecentScenes(store).map(s => s.id), ['roof_training_250-29790', 'house-25822', 'house-30286'], 'newest first');
recordRecentScene('house-30286', '29 Cambridge Road (renamed)', store, now);
const afterReopen = readRecentScenes(store);
assert.deepEqual(afterReopen.map(s => s.id), ['house-30286', 'roof_training_250-29790', 'house-25822'], 'reopening dedupes and moves to the top');
assert.equal(afterReopen[0].title, '29 Cambridge Road (renamed)', 'the latest title wins');
assert.equal(afterReopen[0].opened, '2026-09-26T10:00:03.000Z', 'the opened time is refreshed');

// Cap of 8: recording twelve scenes keeps the eight most recent.
store = storage();
for (let i = 1; i <= 12; i++) recordRecentScene(`scene-${i}`, `Scene ${i}`, store, now);
const capped = readRecentScenes(store);
assert.equal(capped.length, 8);
assert.deepEqual(capped.map(s => s.id), ['scene-12', 'scene-11', 'scene-10', 'scene-9', 'scene-8', 'scene-7', 'scene-6', 'scene-5']);

// record returns the new list, so callers can update state without a second read.
assert.deepEqual(recordRecentScene('scene-13', 'Scene 13', store, now).map(s => s.id)[0], 'scene-13');

// Corrupt or foreign storage never throws and reads as []; recording afterwards replaces it.
for (const bad of ['{not json', '{"id":"x"}', '"a string"', '42', 'null']) {
  store = storage({ [RECENT_SCENES_KEY]: bad });
  assert.deepEqual(readRecentScenes(store), [], `corrupt value ${bad} reads as []`);
  recordRecentScene('house-1', 'House 1', store, now);
  assert.deepEqual(readRecentScenes(store).map(s => s.id), ['house-1'], `recording over ${bad} recovers`);
}
// Entries without a string id/title are dropped; valid siblings survive.
store = storage({ [RECENT_SCENES_KEY]: JSON.stringify([{ id: 'ok', title: 'Kept', opened: '2026-01-01T00:00:00.000Z' }, { id: 7, title: 'Bad id' }, { title: 'No id' }, 'text', null]) });
assert.deepEqual(readRecentScenes(store).map(s => s.id), ['ok']);
// Ignored ids: blank or non-string arguments record nothing.
store = storage();
recordRecentScene('', 'Blank', store, now);
recordRecentScene('   ', 'Spaces', store, now);
assert.deepEqual(readRecentScenes(store), []);
// Untitled scenes fall back to their id so the row is never blank.
recordRecentScene('house-2', '', store, now);
assert.equal(readRecentScenes(store)[0].title, 'house-2');

// A storage that throws (quota, private mode) is tolerated: read gives [], record does not throw.
const throwing = { getItem: () => { throw new Error('denied'); }, setItem: () => { throw new Error('quota'); } };
assert.deepEqual(readRecentScenes(throwing), []);
assert.doesNotThrow(() => recordRecentScene('house-3', 'House 3', throwing, now));

console.log('Recent scenes: round trip, dedupe, newest first, cap of 8, corrupt JSON -> [] and throwing storage passed.');
