import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const require = createRequire(import.meta.url);
class ReviewError extends Error { constructor(message, status = 400) { super(message); this.status = status; } }
let scopes = ['crm.read'], calls = [], mode = 'ok';
const execute = async (file, args, options) => {
  calls.push({ file, args, options });
  if (mode === 'fail') throw new Error('private /Users/path traceback');
  return { stdout: mode === 'invalid' ? 'not json' : JSON.stringify({ schema_version: 1,
    identity: { scene_id: args.at(-2), object_id: args.at(-1), model_signature: 'a'.repeat(64) },
    observations: [{ observed: 'Visible ridge', tier: 3, human_approval: false, training_eligible: false }],
    human_approval: false, training_eligible: false }) };
};
const exports = {};
const code = ts.transpileModule(readFileSync('src/app/api/scenes/roof-observations/route.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
new Function('exports', 'require', code)(exports, id => {
  if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
  if (id === 'node:util') return { promisify: () => execute };
  if (id === '@/lib/review-feedback') return { ReviewError };
  if (id === '@/lib/scene-store') return { scenePrincipal: async () => ({ principal: 'test', scopes }) };
  return require(id);
});
const request = params => ({ nextUrl: { searchParams: new URLSearchParams(params) } });
let response = await exports.GET(request({ scene: 'scene', object: 'roof' }));
assert.equal(response.status, 200);
assert.equal(response.headers.get('cache-control'), 'no-store');
assert.equal((await response.json()).observations[0].tier, 3);
assert.deepEqual(calls[0].args, ['-m', 'scripts.ml.scene_roof_observations', 'scene', 'roof']);
assert.match(calls[0].file, /scripts\/ml\/\.venv\/bin\/python$/);
const before = calls.length;
for (const params of [{ scene: '../etc', object: 'roof' }, { scene: '--root', object: 'roof' }, { scene: 'scene', object: '' }]) {
  response = await exports.GET(request(params)); assert.equal(response.status, 400);
}
scopes = [];
response = await exports.GET(request({ scene: 'scene', object: 'roof' })); assert.equal(response.status, 403);
assert.equal(calls.length, before, 'Rejected ids/scopes must never start a process');
scopes = ['crm.read'];
for (mode of ['fail', 'invalid']) {
  response = await exports.GET(request({ scene: 'scene', object: 'roof' }));
  assert.equal(response.status, 502);
  assert(!/Users|traceback/i.test(await response.text()));
  assert.equal(response.headers.get('cache-control'), 'no-store');
}
console.log('Roof observations route: scoped read, strict ids, no-store and safe failure responses passed.');
