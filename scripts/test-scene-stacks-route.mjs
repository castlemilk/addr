import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import ts from 'typescript';

// GET /api/scenes/stacks?scene=… lists scene_local_stack reports bound to the scene's current saved PNG.
// Read-only, no-store, ids validated; the payload only describes display views (never evidence).
const require = createRequire(import.meta.url);
class ReviewError extends Error { constructor(message, status = 400) { super(message); this.status = status; } }
const code = ts.transpileModule(readFileSync('src/app/api/scenes/stacks/route.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const exports = {};
new Function('exports', 'require', code)(exports, id => {
  if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
  if (id === '@/lib/review-feedback') return { ReviewError };
  if (id === '@/lib/scene-store') return {
    scenePath: scene => {
      if (typeof scene !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(scene)) throw new ReviewError('Choose a valid scene.');
      return path.join(process.cwd(), 'data/scenes', scene);
    },
  };
  return require(id);
});
const request = params => ({ nextUrl: { searchParams: new URLSearchParams(params) } });
const SCENE = 'mooroolbark-50011498-17-118442-80417', VERSION = '1'.repeat(64), OTHER_VERSION = '2'.repeat(64);
const root = mkdtempSync(path.join(tmpdir(), 'addr-stacks-route-'));
const previous = process.cwd();
const captures = [
  { index: 0, acquisition_id: 'a', date: '2025-11-06', sensor: 'Vantor', status: 'accepted', registration: { coverage: 0.7191 } },
  { index: 1, acquisition_id: 'b', date: '2025-09-28', sensor: 'Vantor', status: 'rejected', registration: { coverage: 0.0755 } },
  { index: 2, acquisition_id: 'c', date: '2023-12-20', sensor: 'Maxar', status: 'rejected', registration: { coverage: 0.1917 } },
];
const writeReport = (stack, report) => {
  const folder = path.join(root, 'data/roof-review-loop/multidate/stacks', stack);
  mkdirSync(folder, { recursive: true });
  writeFileSync(path.join(folder, 'report.json'), typeof report === 'string' ? report : JSON.stringify({
    version: 'scene-local-stack-v1', computed_at: '2026-09-26T00:39:36+00:00', stack_id: stack, status: 'pair', scene_id: SCENE,
    source_image_sha256: VERSION, n_accepted: 1, n_views: 2, captures, stack: { mode: 'pair', views: 2 },
    assets: Object.fromEntries(['edge-consensus.png', 'valid-count.png', 'enhanced-pair-rgb.png', 'aligned-0.png', 'valid-0.png', 'shift-field-0.png'].map(n => [n, { path: n, sha256: 'a'.repeat(64) }])),
    ...report,
  }));
};
try {
  mkdirSync(path.join(root, 'data/scenes', SCENE), { recursive: true });
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: VERSION }));
  process.chdir(root);

  // No stacks folder yet: an empty, honest list.
  let response = await exports.GET(request({ scene: SCENE }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  let body = await response.json();
  assert.deepEqual(body.stacks, []);
  assert.equal(body.scene_id, SCENE);
  assert.equal(body.image_sha256, VERSION);

  writeReport('c26ee81c761dd075', {});                                                  // the pair for this saved image
  writeReport('0db29a38f39f7420', { status: 'not_available', assets: {}, computed_at: '2026-09-26T00:10:00+00:00' }); // an honest negative, same image
  writeReport('72e328fbc57ca21a', { scene_id: 'house-30286' });                           // another scene
  writeReport('dde125b138d603c4', { source_image_sha256: OTHER_VERSION });                // registered onto other saved pixels
  writeReport('aaaaaaaaaaaaaaaa', '{not json');                                           // corrupt report is skipped, not fatal
  writeReport('bbbbbbbbbbbbbbbb', { status: 'available', n_accepted: 2, n_views: 3, stack: { mode: 'stack', views: 3 }, computed_at: '2026-09-26T01:00:00+00:00',
    captures: [captures[0], { ...captures[1], status: 'accepted', registration: { coverage: 0.5 } }, captures[2]],
    assets: { 'enhanced-median-rgb.png': { path: 'enhanced-median-rgb.png', sha256: 'b'.repeat(64) }, 'aligned-0.png': { path: 'aligned-0.png', sha256: 'c'.repeat(64) }, 'aligned-1.png': { path: 'aligned-1.png', sha256: 'd'.repeat(64) } } });
  mkdirSync(path.join(root, 'data/roof-review-loop/multidate/stacks', 'not-a-stack-id'), { recursive: true });
  mkdirSync(path.join(root, 'data/roof-review-loop/multidate/stacks', 'cccccccccccccccc'), { recursive: true }); // folder without a report

  response = await exports.GET(request({ scene: SCENE }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  body = await response.json();
  assert.equal(body.scene_id, SCENE);
  assert.equal(body.image_sha256, VERSION);
  assert.deepEqual(body.stacks.map(s => s.stack_id), ['bbbbbbbbbbbbbbbb', 'c26ee81c761dd075', '0db29a38f39f7420'], 'available first, then pair, then negatives; only this scene and saved image');
  const pair = body.stacks[1];
  assert.equal(pair.status, 'pair');
  assert.equal(pair.mode, 'pair');
  assert.equal(pair.n_accepted, 1);
  assert.deepEqual(pair.dates, [
    { index: 0, date: '2025-11-06', sensor: 'Vantor', accepted: true, coverage: 0.7191 },
    { index: 1, date: '2025-09-28', sensor: 'Vantor', accepted: false, coverage: 0.0755 },
    { index: 2, date: '2023-12-20', sensor: 'Maxar', accepted: false, coverage: 0.1917 },
  ]);
  assert.deepEqual(pair.assets, ['aligned-0.png', 'edge-consensus.png', 'enhanced-pair-rgb.png', 'shift-field-0.png', 'valid-0.png', 'valid-count.png'], 'asset names sorted');
  assert.equal(pair.composite, 'enhanced-pair-rgb.png');
  const full = body.stacks[0];
  assert.equal(full.status, 'available');
  assert.equal(full.mode, 'stack');
  assert.equal(full.n_accepted, 2);
  assert.equal(full.composite, 'enhanced-median-rgb.png');
  assert.equal(full.dates.filter(d => d.accepted).length, 2);
  const negative = body.stacks[2];
  assert.equal(negative.status, 'not_available');
  assert.equal(negative.composite, null);
  assert.deepEqual(negative.assets, []);
  assert.match(body.note, /display|not an evidence image/i, 'the payload says what these views are');
  for (const stack of body.stacks) for (const key of Object.keys(stack)) assert(['stack_id', 'status', 'mode', 'n_accepted', 'dates', 'assets', 'composite', 'computed_at', 'reason'].includes(key), `unexpected key ${key}`);
  assert(!JSON.stringify(body).includes(root), 'no local paths leak');

  // The scene's current image moved on: stacks bound to the old PNG are no longer listed.
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: OTHER_VERSION }));
  response = await exports.GET(request({ scene: SCENE }));
  body = await response.json();
  assert.deepEqual(body.stacks.map(s => s.stack_id), ['dde125b138d603c4']);
  assert.equal(body.image_sha256, OTHER_VERSION);

  // Validation and missing scenes.
  for (const bad of ['', '../etc', '-flag', 'a'.repeat(101), 'bad id']) {
    response = await exports.GET(request({ scene: bad }));
    assert.equal(response.status, 400, `scene id rejected: ${JSON.stringify(bad)}`);
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal((await response.json()).error, 'Choose a valid scene.');
  }
  response = await exports.GET(request({ scene: 'missing-scene' }));
  assert.equal(response.status, 404);
  assert.equal(response.headers.get('cache-control'), 'no-store');
  body = await response.json();
  assert.match(body.error, /scene/i);
  assert(!body.error.includes('/'), 'error bodies never leak paths');
  console.log('scene stacks route ok');
} finally {
  process.chdir(previous);
  rmSync(root, { recursive: true, force: true });
}
