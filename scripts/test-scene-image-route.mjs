import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import ts from 'typescript';

// GET /api/scenes/image views: roof-detail-v1 (sharp CLAHE display view, Python CLI fallback), multidate-median-v1,
// multidate-aligned and multidate-composite (scene_local_stack assets bound to the saved PNG identity), crop-v1.
// The plain branch (no view) is untouched.
const require = createRequire(import.meta.url);
class ReviewError extends Error { constructor(message, status = 400) { super(message); this.status = status; } }
const code = ts.transpileModule(readFileSync('src/app/api/scenes/image/route.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
let execCalls = 0, execCall = null, sharpBroken = false, sharpCalls = 0;
const execute = async (file, args, options) => { execCalls += 1; execCall = { file, args, options }; return { stdout: OTHER_PNG }; };
const realSharp = require('sharp');
const countingSharp = (...args) => { sharpCalls += 1; return realSharp(...args); };
const exports = {};
new Function('exports', 'require', code)(exports, id => {
  if (id === 'next/server') return {};
  if (id === 'sharp' && sharpBroken) return { __esModule: true, default: () => { throw new Error('sharp unavailable'); } };
  if (id === 'sharp') return { __esModule: true, default: countingSharp };
  if (id === 'node:util') return { promisify: () => execute };
  if (id === '@/lib/scene-store') return {
    scenePath: scene => {
      if (typeof scene !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(scene)) throw new ReviewError('Choose a valid scene.');
      return path.join(process.cwd(), 'data/scenes', scene);
    },
  };
  return require(id);
});
const request = params => ({ nextUrl: { searchParams: new URLSearchParams(params) }, signal: undefined });
const sha256 = bytes => createHash('sha256').update(bytes).digest('hex');
// A 1x1 PNG is enough: the route streams bytes and checks their sha256 against the report.
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64');
const OTHER_PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==', 'base64');
const SCENE = 'mooroolbark-50011498-17-118442-80417', STACK = '0123456789abcdef', OTHER_STACK = 'fedcba9876543210';
const VERSION = sha256(PNG);

// Synthetic repo root: data/scenes/<id>/{latest.json,<sha>.png} and one stack folder written the way scene_local_stack does.
const root = mkdtempSync(path.join(tmpdir(), 'addr-image-route-'));
const previous = process.cwd();
const writeStack = (stack, report, png = PNG) => {
  const folder = path.join(root, 'data/roof-review-loop/multidate/stacks', stack);
  mkdirSync(folder, { recursive: true });
  writeFileSync(path.join(folder, 'enhanced-median-rgb.png'), png);
  writeFileSync(path.join(folder, 'report.json'), JSON.stringify({
    version: 'scene-local-stack-v1', code_sha256: 'c'.repeat(64), computed_at: '2026-09-26T00:00:00+00:00', stack_id: stack,
    status: 'available', scene_id: SCENE, source_image_sha256: VERSION, n_accepted: 2, n_views: 3,
    assets: { 'enhanced-median-rgb.png': { path: 'enhanced-median-rgb.png', sha256: sha256(PNG) }, 'stack-median.png': { path: 'stack-median.png', sha256: 'a'.repeat(64) } },
    note: 'display/review view; not an evidence image', ...report,
  }));
};
try {
  mkdirSync(path.join(root, 'data/scenes', SCENE), { recursive: true });
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: VERSION, image_width: 1, image_height: 1 }));
  writeFileSync(path.join(root, 'data/scenes', SCENE, `${VERSION}.png`), PNG);
  writeStack(STACK, {});
  process.chdir(root);

  // 200: the composite streams with an immutable cache policy keyed by the asset sha (ETag) and an explicit role header.
  let response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: STACK }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-type'), 'image/png');
  assert.equal(response.headers.get('cache-control'), 'public,max-age=31536000,immutable');
  assert.equal(response.headers.get('etag'), `"${sha256(PNG)}"`);
  assert.match(response.headers.get('x-image-role'), /not an evidence image/);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), PNG);
  assert.equal(execCalls, 0, 'The multidate view never runs the roof-detail Python helper');

  // 400: malformed ids never touch the filesystem layout.
  for (const bad of [{ stack: 'zz' }, { stack: '../etc/passwd' }, { stack: '0123456789abcde' }, { stack: '0123456789ABCDEF' }, { stack: '' }, {}]) {
    response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', ...bad }));
    assert.equal(response.status, 400, `stack id rejected: ${JSON.stringify(bad)}`);
  }
  response = await exports.GET(request({ id: '../etc', version: VERSION, view: 'multidate-median-v1', stack: STACK }));
  assert.equal(response.status, 400, 'scene id validated before any read');
  response = await exports.GET(request({ id: SCENE, version: 'nothex', view: 'multidate-median-v1', stack: STACK }));
  assert.equal(response.status, 400, 'version keeps the existing 64-hex gate');
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'other-view', stack: STACK }));
  assert.equal(response.status, 400, 'unknown views are still rejected');

  // 404: missing stack, stack for another scene, another saved image, not available, tampered or unlisted asset.
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: OTHER_STACK }));
  assert.equal(response.status, 404, 'missing stack folder');
  writeStack(OTHER_STACK, { scene_id: 'house-30286' });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: OTHER_STACK }));
  assert.equal(response.status, 404, 'stack belongs to another scene');
  writeStack(OTHER_STACK, { source_image_sha256: 'f'.repeat(64) });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: OTHER_STACK }));
  assert.equal(response.status, 404, 'stack registered onto other saved pixels');
  writeStack(OTHER_STACK, { status: 'not_available', assets: {} });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: OTHER_STACK }));
  assert.equal(response.status, 404, 'not_available stack has no composite');
  writeStack(OTHER_STACK, {}, OTHER_PNG);
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: OTHER_STACK }));
  assert.equal(response.status, 404, 'asset bytes must match the recorded sha256');
  writeStack(OTHER_STACK, { assets: { 'enhanced-median-rgb.png': { path: '../enhanced-median-rgb.png', sha256: sha256(PNG) } } });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: OTHER_STACK }));
  assert.equal(response.status, 404, 'asset path must be the plain file name');
  // The scene's current image differs from the requested version: the stack no longer describes the saved PNG.
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: 'e'.repeat(64) }));
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-median-v1', stack: STACK }));
  assert.equal(response.status, 404, 'version must be the scene\'s current image_sha256');
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: VERSION }));

  // The plain branch is unchanged: the saved PNG streams by version, unknown ids stay 404.
  response = await exports.GET(request({ id: SCENE, version: VERSION }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('cache-control'), 'public,max-age=31536000,immutable');
  assert.equal(response.headers.get('etag'), null, 'the plain branch keeps its original headers');
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), PNG);
  response = await exports.GET(request({ id: 'missing-scene', version: VERSION }));
  assert.equal(response.status, 404);
  response = await exports.GET(request({ id: SCENE, version: 'x' }));
  assert.equal(response.status, 400);
  assert.equal(execCalls, 0);
  // roof-detail-v1 is now rendered in Node (sharp CLAHE) as a display view: same size, distinct bytes, no Python,
  // original headers, cached by id:version:view.
  const sharp = require('sharp');
  const gradient = Buffer.alloc(64 * 48 * 3);
  for (let y = 0; y < 48; y++) for (let x = 0; x < 64; x++) { const i = (y * 64 + x) * 3; gradient[i] = 60 + x; gradient[i + 1] = 70 + y; gradient[i + 2] = (x * y) % 40 + 50; }
  const BIG = await sharp(gradient, { raw: { width: 64, height: 48, channels: 3 } }).png().toBuffer();
  const BIG_SCENE = 'house-99999', BIG_VERSION = sha256(BIG);
  mkdirSync(path.join(root, 'data/scenes', BIG_SCENE), { recursive: true });
  writeFileSync(path.join(root, 'data/scenes', BIG_SCENE, `${BIG_VERSION}.png`), BIG);
  writeFileSync(path.join(root, 'data/scenes', BIG_SCENE, 'latest.json'), JSON.stringify({ id: BIG_SCENE, image_sha256: BIG_VERSION, image_width: 64, image_height: 48 }));
  response = await exports.GET(request({ id: BIG_SCENE, version: BIG_VERSION, view: 'roof-detail-v1' }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-type'), 'image/png');
  assert.equal(response.headers.get('cache-control'), 'public,max-age=31536000,immutable');
  assert.equal(response.headers.get('etag'), null, 'the roof-detail branch keeps its original headers');
  const detail = Buffer.from(await response.arrayBuffer());
  const detailMeta = await sharp(detail).metadata();
  assert.deepEqual([detailMeta.width, detailMeta.height], [64, 48], 'the contrast view keeps the saved size and registration');
  assert(!detail.equals(BIG), 'the contrast view is a distinct photometric rendering');
  assert.equal(execCalls, 0, 'roof-detail-v1 no longer needs the Python helper when sharp works');
  response = await exports.GET(request({ id: BIG_SCENE, version: BIG_VERSION, view: 'roof-detail-v1' }));
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), detail, 'the roof-detail bytes are cached per id:version:view');
  assert.equal(execCalls, 0);
  // Regression: the view must visibly lift local contrast. A sharp CLAHE with maxSlope 3 rendered the 27 Cambridge tile
  // within 1% of the original (gray std x1.01, mean |diff| 0.75/255), i.e. "Enhance roof detail" showed nothing.
  // A low-contrast textured image (gray 100..140) must come out with at least 1.3x the gray standard deviation.
  const lowStd = raw => { let s = 0, s2 = 0; for (const v of raw) { s += v; s2 += v * v; } const m = s / raw.length; return Math.sqrt(s2 / raw.length - m * m); };
  const LOW_W = 160, LOW_H = 120, low = Buffer.alloc(LOW_W * LOW_H * 3);
  for (let y = 0; y < LOW_H; y++) for (let x = 0; x < LOW_W; x++) {
    const i = (y * LOW_W + x) * 3, v = 100 + ((Math.floor(x / 10) + Math.floor(y / 10)) % 2) * 18 + ((x * 7 + y * 13) % 11) + Math.round(y / 12);
    low[i] = v; low[i + 1] = v; low[i + 2] = v;
  }
  const LOW = await sharp(low, { raw: { width: LOW_W, height: LOW_H, channels: 3 } }).png().toBuffer();
  const LOW_SCENE = 'house-99998', LOW_VERSION = sha256(LOW);
  mkdirSync(path.join(root, 'data/scenes', LOW_SCENE), { recursive: true });
  writeFileSync(path.join(root, 'data/scenes', LOW_SCENE, `${LOW_VERSION}.png`), LOW);
  response = await exports.GET(request({ id: LOW_SCENE, version: LOW_VERSION, view: 'roof-detail-v1' }));
  assert.equal(response.status, 200);
  const lowDetail = Buffer.from(await response.arrayBuffer());
  const lowMeta = await sharp(lowDetail).metadata();
  assert.deepEqual([lowMeta.width, lowMeta.height], [LOW_W, LOW_H], 'the contrast view keeps the saved size');
  assert(lowMeta.channels <= 2, 'the contrast view is a gray rendering (like the cv2 view it replaces)');
  const stdBefore = lowStd(await sharp(LOW).greyscale().raw().toBuffer()), stdAfter = lowStd(await sharp(lowDetail).greyscale().raw().toBuffer());
  assert(stdAfter >= 1.3 * stdBefore, `the contrast view must lift local contrast: gray std ${stdBefore.toFixed(1)} -> ${stdAfter.toFixed(1)}`);
  assert.equal(execCalls, 0);
  // Concurrent requests for the same uncached view share one render (the editor prefetches, then the <image> loads).
  const DUP_SCENE = 'house-99997', DUP_VERSION = sha256(LOW);
  mkdirSync(path.join(root, 'data/scenes', DUP_SCENE), { recursive: true });
  writeFileSync(path.join(root, 'data/scenes', DUP_SCENE, `${DUP_VERSION}.png`), LOW);
  sharpCalls = 0;
  const [first, second] = await Promise.all([
    exports.GET(request({ id: DUP_SCENE, version: DUP_VERSION, view: 'roof-detail-v1' })),
    exports.GET(request({ id: DUP_SCENE, version: DUP_VERSION, view: 'roof-detail-v1' })),
  ]);
  assert.equal(first.status, 200); assert.equal(second.status, 200);
  assert.deepEqual(Buffer.from(await first.arrayBuffer()), Buffer.from(await second.arrayBuffer()));
  assert(sharpCalls <= 2, `concurrent identical requests render once (sharp constructed ${sharpCalls} times)`);
  assert.equal(execCalls, 0);
  // When sharp throws the route falls back to the Python CLI with its original arguments and limits.
  sharpBroken = true;
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'roof-detail-v1' }));
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-type'), 'image/png');
  assert.equal(response.headers.get('cache-control'), 'public,max-age=31536000,immutable');
  assert.equal(response.headers.get('etag'), null);
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), OTHER_PNG);
  assert.equal(execCalls, 1, 'the Python CLI is the fallback only');
  assert.match(execCall.file, /scripts\/ml\/\.venv\/bin\/python$/);
  assert.deepEqual(execCall.args, ['-m', 'scripts.ml.roof_line_fit', '--image', path.join(process.cwd(), 'data/scenes', SCENE, `${VERSION}.png`), '--sha256', VERSION]);
  assert.equal(execCall.options.timeout, 10000);
  assert.equal(execCall.options.maxBuffer, 8 * 1024 * 1024);
  assert.equal(execCall.options.encoding, 'buffer');
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'roof-detail-v1' }));
  assert.equal(response.status, 200);
  assert.equal(execCalls, 1, 'fallback bytes are cached per id:version:view too');
  sharpBroken = false;
  response = await exports.GET(request({ id: 'missing-scene', version: VERSION, view: 'roof-detail-v1' }));
  assert.equal(response.status, 404, 'a missing saved PNG is 404 for the contrast view');
  assert.equal(execCalls, 1);

  // multidate-aligned / multidate-composite stream scene_local_stack assets bound to the report (pair or available).
  const ALIGNED = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/58BAAAGAgH/7FLnHwAAAABJRU5ErkJggg==', 'base64');
  const PAIR = '00000000deadbeef';
  const writePair = (stack, report = {}, files = {}) => {
    const folder = path.join(root, 'data/roof-review-loop/multidate/stacks', stack);
    mkdirSync(folder, { recursive: true });
    const assets = { 'aligned-0.png': ALIGNED, 'valid-0.png': PNG, 'enhanced-pair-rgb.png': OTHER_PNG, 'edge-consensus.png': PNG, ...files };
    for (const [name, bytes] of Object.entries(assets)) writeFileSync(path.join(folder, name), bytes);
    writeFileSync(path.join(folder, 'report.json'), JSON.stringify({
      version: 'scene-local-stack-v1', stack_id: stack, status: 'pair', scene_id: SCENE, source_image_sha256: VERSION, n_accepted: 1, n_views: 2,
      captures: [{ index: 0, date: '2025-11-06', sensor: 'Vantor', status: 'accepted', registration: { coverage: 0.7191 } }, { index: 1, date: '2025-09-28', sensor: 'Vantor', status: 'rejected', registration: { coverage: 0.0755 } }],
      assets: Object.fromEntries(Object.keys(assets).map(name => [name, { path: name, sha256: sha256(assets[name]) }])),
      stack: { mode: 'pair', views: 2 }, ...report,
    }));
  };
  writePair(PAIR);
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, index: '0' }));
  assert.equal(response.status, 200, 'an accepted aligned date streams from a pair stack');
  assert.equal(response.headers.get('content-type'), 'image/png');
  assert.equal(response.headers.get('cache-control'), 'public,max-age=31536000,immutable');
  assert.equal(response.headers.get('etag'), `"${sha256(ALIGNED)}"`, 'immutable cache keyed by the asset sha');
  assert.equal(response.headers.get('x-image-role'), 'display-view');
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), ALIGNED);
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-composite', stack: PAIR }));
  assert.equal(response.status, 200, 'the pair composite streams');
  assert.equal(response.headers.get('etag'), `"${sha256(OTHER_PNG)}"`);
  assert.equal(response.headers.get('x-image-role'), 'display-view');
  assert.deepEqual(Buffer.from(await response.arrayBuffer()), OTHER_PNG);
  // An available stack serves its median composite through the same view.
  writeStack(OTHER_STACK, { captures: [] });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-composite', stack: OTHER_STACK }));
  assert.equal(response.status, 200, 'available stacks expose enhanced-median-rgb.png as the composite');
  assert.equal(response.headers.get('etag'), `"${sha256(PNG)}"`);
  for (const bad of [{}, { index: '1.5' }, { index: '-1' }, { index: 'a' }, { index: '100' }]) {
    response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, ...bad }));
    assert.equal(response.status, 400, `aligned index validated: ${JSON.stringify(bad)}`);
  }
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: 'zz', index: '0' }));
  assert.equal(response.status, 400, 'aligned stack id validated');
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-composite', stack: '../x' }));
  assert.equal(response.status, 400, 'composite stack id validated');
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, index: '1' }));
  assert.equal(response.status, 404, 'a rejected date has no aligned asset');
  writePair(PAIR, { status: 'not_available' });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, index: '0' }));
  assert.equal(response.status, 404, 'not_available stacks stream nothing');
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-composite', stack: PAIR }));
  assert.equal(response.status, 404);
  writePair(PAIR, { scene_id: 'house-30286' });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, index: '0' }));
  assert.equal(response.status, 404, 'aligned view checks the report scene');
  writePair(PAIR, { source_image_sha256: 'f'.repeat(64) });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-composite', stack: PAIR }));
  assert.equal(response.status, 404, 'composite view checks the saved-image identity');
  writePair(PAIR);
  writeFileSync(path.join(root, 'data/roof-review-loop/multidate/stacks', PAIR, 'aligned-0.png'), PNG);
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, index: '0' }));
  assert.equal(response.status, 404, 'tampered aligned bytes never stream');
  writePair(PAIR, { assets: {} });
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-composite', stack: PAIR }));
  assert.equal(response.status, 404, 'a report without a composite asset is 404');
  assert.equal(execCalls, 1, 'stack views never run the Python helper');
  // Multi-date views bind to the scene's current image too.
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: 'e'.repeat(64) }));
  writePair(PAIR);
  response = await exports.GET(request({ id: SCENE, version: VERSION, view: 'multidate-aligned', stack: PAIR, index: '0' }));
  assert.equal(response.status, 404, 'aligned view requires the current image_sha256');
  writeFileSync(path.join(root, 'data/scenes', SCENE, 'latest.json'), JSON.stringify({ id: SCENE, image_sha256: VERSION }));
  // crop-v1: a downscaled display crop of the saved PNG (sharp), never touching the plain branch.
  response = await exports.GET(request({ id: BIG_SCENE, version: BIG_VERSION, view: 'crop-v1', x: '8', y: '4', w: '32', h: '24', size: '16' }));
  assert.equal(response.status, 200, 'a valid crop streams');
  assert.equal(response.headers.get('content-type'), 'image/jpeg', 'display crops are JPEG, never an evidence PNG');
  assert.equal(response.headers.get('cache-control'), 'public,max-age=31536000,immutable');
  assert.equal(response.headers.get('x-image-role'), 'display-crop');
  const cropped = await sharp(Buffer.from(await response.arrayBuffer())).metadata();
  assert.deepEqual([cropped.width, cropped.height], [16, 12], 'the crop keeps its aspect ratio at the requested size');
  response = await exports.GET(request({ id: BIG_SCENE, version: BIG_VERSION, view: 'crop-v1', x: '8', y: '4', w: '32', h: '24', size: '128' }));
  assert.deepEqual([(await sharp(Buffer.from(await response.arrayBuffer())).metadata()).width], [32], 'crops are never upscaled');
  for (const bad of [{ x: '40', y: '4', w: '32', h: '24', size: '16' }, { x: '8', y: '4', w: '4', h: '24', size: '16' }, { x: '8', y: '4', w: '32', h: '24', size: '9999' }, { x: '-1', y: '4', w: '32', h: '24', size: '16' }, { x: '1.5', y: '4', w: '32', h: '24', size: '16' }]) {
    response = await exports.GET(request({ id: BIG_SCENE, version: BIG_VERSION, view: 'crop-v1', ...bad }));
    assert.equal(response.status, 400, `crop rejects ${JSON.stringify(bad)}`);
  }
  response = await exports.GET(request({ id: 'missing-scene', version: BIG_VERSION, view: 'crop-v1', x: '0', y: '0', w: '8', h: '8', size: '16' }));
  assert.equal(response.status, 404);
  assert.equal(execCalls, 1, 'crops never run the Python helper');
  console.log('scene image route ok');
} finally {
  process.chdir(previous);
  rmSync(root, { recursive: true, force: true });
}
