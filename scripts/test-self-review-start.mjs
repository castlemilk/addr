import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';

const require = createRequire(import.meta.url);
class ReviewError extends Error { constructor(message, status = 400) { super(message); this.status = status; } }
let allowed = true, calls = [], fail = false;
const execute = async (_file, args, options) => {
  calls.push({ args, options });
  // Selection can take longer than a status read. Simulate that slow path
  // without a wall-clock delay or any production batch/model writes.
  if (fail || (args.includes('--start') && options.timeout < 45000)) {
    throw Object.assign(new Error('private worker details'), { killed: true, signal: 'SIGTERM' });
  }
  return { stdout: JSON.stringify({ run: 'a'.repeat(16), status: 'pending', rows: [] }) };
};
const exports = {};
const code = ts.transpileModule(readFileSync('src/app/api/scenes/self-review/route.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
new Function('exports', 'require', code)(exports, id => {
  if (id === 'next/server') return { NextResponse: { json: (body, init) => Response.json(body, init) } };
  if (id === 'node:util') return { promisify: () => execute };
  if (id === '@/lib/scene-store') return { scenePrincipal: async () => ({ scopes: allowed ? ['crm.roof.write'] : [] }) };
  if (id === '@/lib/review-feedback') return { ReviewError };
  return require(id);
});
const request = body => ({ json: async () => body });
let response = await exports.POST(request({ limit: 12 }));
assert.equal(response.status, 200, 'Slow preparation must have time to return a queued batch');
assert.equal((await response.json()).status, 'pending');
assert(calls[0].args.includes('--start'));
const before = calls.length;
allowed = false;
assert.equal((await exports.POST(request({ limit: 12 }))).status, 403);
allowed = true;
assert.equal((await exports.POST(request({ limit: 99 }))).status, 400);
assert.equal(calls.length, before, 'Invalid or unauthorized starts must not launch a worker');
fail = true;
const log = console.error, logs = [];
try {
  console.error = (...args) => logs.push(args);
  response = await exports.POST(request({ limit: 12 }));
} finally { console.error = log; }
assert.equal(response.status, 503);
const body = await response.json();
assert.match(body.error, /Refresh the queue/);
assert(!body.error.includes('private worker details'));
assert.equal(logs.length, 1);
console.log('Self-review start: slow preparation, auth/input protection and actionable timeout responses passed.');
