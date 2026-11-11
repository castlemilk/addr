import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

// GET /api/scenes/eyes?scene=…&object=… reads the eyes sidecar index through the
// Python CLI. Rows are diagnostics and reference agreement, never accuracy or labels.
const require = createRequire(import.meta.url);
class ReviewError extends Error { constructor(message, status = 400) { super(message); this.status = status; } }
let calls = [], fail = false;
const execute = async (file, args, options) => {
  calls.push({ file, args, options });
  if (fail) throw Object.assign(new Error('private traceback with /Users paths'), { code: 1, stderr: 'Traceback' });
  return { stdout: JSON.stringify({ version: 'eyes-index-v1', scene_id: args.at(-2), object_id: args.at(-1),
    tables: { agreement: [{ ref_id: 'r', payload: { iou: 0.82, ref_tier: 'T2' } }], roof_diagnostics: [], cross_capture: [], shadow_bands: [] } }) + '\n' };
};
const exports = {};
const code = ts.transpileModule(readFileSync('src/app/api/scenes/eyes/route.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
new Function('exports', 'require', code)(exports, id => {
  if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
  if (id === 'node:util') return { promisify: () => execute };
  if (id === '@/lib/review-feedback') return { ReviewError };
  return require(id);
});
const request = (params) => ({ nextUrl: { searchParams: new URLSearchParams(params) } });

let response = await exports.GET(request({ scene: 'mooroolbark-50011498-17-118442-80417', object: '44c724fd3480c167' }));
assert.equal(response.status, 200);
assert.equal(response.headers.get('cache-control'), 'no-store', 'Sidecar reads are live; never cache them');
let body = await response.json();
assert.equal(body.scene_id, 'mooroolbark-50011498-17-118442-80417');
assert.equal(body.tables.agreement[0].payload.iou, 0.82);
assert.match(body.note, /not accuracy/i, 'The response must say these rows are diagnostics, not accuracy');
assert.match(body.note, /reference agreement|diagnostic/i);
assert.equal(calls.length, 1);
assert.match(calls[0].file, /scripts\/ml\/\.venv\/bin\/python$/, 'Use the repo venv like the other scene routes');
assert.deepEqual(calls[0].args, ['-m', 'scripts.ml.eyes_index', '--object', 'mooroolbark-50011498-17-118442-80417', '44c724fd3480c167']);
assert.equal(calls[0].options.cwd, process.cwd());
assert(calls[0].options.timeout >= 5000 && calls[0].options.timeout <= 60000);

// Scene ids with underscores (roof_training_250-30381) are valid scene directories.
response = await exports.GET(request({ scene: 'roof_training_250-30381', object: 'review-roof-30381-main' }));
assert.equal(response.status, 200);

const before = calls.length;
for (const bad of [{ scene: '../etc', object: 'a' }, { scene: 'ok', object: 'a b' }, { scene: '', object: 'a' }, { object: 'a' }, { scene: 'ok' },
  { scene: 'x'.repeat(101), object: 'a' }, { scene: 'ok', object: '--root' }, { scene: '-leading', object: 'a' }]) {
  response = await exports.GET(request(bad));
  assert.equal(response.status, 400, `Rejected ids: ${JSON.stringify(bad)}`);
  const error = (await response.json()).error;
  assert.equal(typeof error, 'string');
}
assert.equal(calls.length, before, 'Invalid ids must never reach the Python CLI');

fail = true;
response = await exports.GET(request({ scene: 'house-30286', object: '37ff5c02f77869d5' }));
assert.equal(response.status, 502);
const text = await response.text();
assert(!text.includes('/Users') && !text.includes('Traceback'), 'Exec failures must not leak process details');
assert.equal(response.headers.get('cache-control'), 'no-store');
console.log('eyes route ok');

// A CLI that prints something other than JSON (a stray warning, a partial write) is an exec failure, not a 200.
const brokenExecute = async (file, args, options) => { calls.push({ file, args, options }); return { stdout: 'not json at all\n' }; };
const exports2 = {};
new Function('exports', 'require', code)(exports2, id => {
  if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
  if (id === 'node:util') return { promisify: () => brokenExecute };
  if (id === '@/lib/review-feedback') return { ReviewError };
  return require(id);
});
response = await exports2.GET(request({ scene: 'house-30286', object: '37ff5c02f77869d5' }));
assert.equal(response.status, 502, 'Non-JSON CLI output must not be returned as a success');

// The coverage-map popup lines share the same wording rules (model estimate / reference agreement, not accuracy).
const mapExports = {};
const mapCode = ts.transpileModule(readFileSync('src/components/scenes/RoofConfidenceMap.tsx', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
new Function('exports', 'require', mapCode)(mapExports, id => {
  if (id === 'react') return { useEffect() {}, useRef: () => ({ current: null }), useState: v => [v, () => {}] };
  if (id === 'react/jsx-runtime') return { jsx: () => null, jsxs: () => null, Fragment: 'Fragment' };
  if (id === 'maplibre-gl') return { Popup: class {} };
  return require(id);
});
const lines = mapExports.otherCaptureLines({
  hidden_overlaps: [{ scene_id: 'mooroolbark-50011498-17-118442-80417', object_id: '44c724fd3480c167', band: 'low', iou: 0.9017 }],
  eyes: { reference: { tier: 'T2', iou: 0.955 } },
});
assert.equal(lines.length, 2);
assert.match(lines[0], /^Also seen as low evidence in mooroolbark-50011498-17-118442-80417 \(IoU 0\.90\) · hidden on this map$/);
assert.match(lines[1], /^Human reference IoU 0\.95 \(T2\) · reference agreement, not accuracy$/);
assert.deepEqual(mapExports.otherCaptureLines({}), [], 'No overlaps and no reference means no extra lines');
assert.deepEqual(mapExports.otherCaptureLines({ eyes: { reference: { tier: 'T1', iou: null } } }), [], 'A reference without an IoU is not shown as agreement');
for (const line of lines) assert(!/accura(te|cy)\b(?! percentages)/.test(line.replace('not accuracy', '')), `No accuracy claim in popup line: ${line}`);
console.log('eyes popup lines ok');
