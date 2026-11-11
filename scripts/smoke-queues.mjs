// Read-only real-data smoke: the transpiled calibration handler and the hidden-roof CLI against this repo, with timings.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import ts from 'typescript';
const require = createRequire(import.meta.url);
const transpile = f => ts.transpileModule(readFileSync(f, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true } }).outputText;
const queues = {}; new Function('exports', 'require', transpile('src/lib/queues.ts'))(queues, id => require(id));
const route = {}; new Function('exports', 'require', transpile('src/app/api/queues/calibration/route.ts'))(route, id => id === 'next/server' ? { NextResponse: { json: (b, i) => Response.json(b, i) } } : id === '@/lib/queues' ? queues : require(id));
const req = plan => ({ nextUrl: { searchParams: new URLSearchParams({ plan }) } });
for (let i = 0; i < 3; i++) {
  const t0 = performance.now();
  const response = await route.GET(req('7cfed4d9e02f1c8a'));
  const body = await response.json();
  const ms = performance.now() - t0;
  const status = {}; for (const r of body.rows) status[r.status] = (status[r.status] || 0) + 1;
  const detail = {}; for (const r of body.rows) detail[r.detail] = (detail[r.detail] || 0) + 1;
  console.log(`run ${i}: ${ms.toFixed(0)} ms, http ${response.status}, rows ${body.rows.length}, statuses ${JSON.stringify(status)}, details ${JSON.stringify(detail)}`);
  if (i === 0) {
    console.log('bands', JSON.stringify(Object.fromEntries(Object.entries(body.bands).map(([b, v]) => [b, [v.groups_total, v.groups_reviewed, v.interval]]))));
    console.log('addresses', body.rows.filter(r => r.address).length, 'with address_id', body.rows.filter(r => r.address_id !== null).length);
    console.log('first row', JSON.stringify(body.rows[0]));
    // Compare with the python progress.json written for this plan.
    const progress = JSON.parse(readFileSync('data/reviews/eyes/calibration/7cfed4d9e02f1c8a/progress.json', 'utf8'));
    const mismatches = progress.rows.filter((p, k) => p.status !== body.rows[k].status || p.detail !== body.rows[k].detail || p.scene_id !== body.rows[k].scene_id);
    console.log('rows differing from progress.json:', mismatches.length);
    const mine = JSON.stringify(body).includes('/Users'); console.log('leaks /Users:', mine);
  }
}
const t1 = performance.now(); const r404 = await route.GET(req('ffffffffffffffff')); console.log('404 status', r404.status, (performance.now() - t1).toFixed(1), 'ms');
const t2 = performance.now(); const r400 = await route.GET(req('../x')); console.log('400 status', r400.status, (performance.now() - t2).toFixed(1), 'ms');
// Hidden-roof CLI (read-only): rows where a low roof is hidden under a medium/high capture.
import { execFileSync } from 'node:child_process';
const t3 = performance.now();
const hidden = JSON.parse(execFileSync('scripts/ml/.venv/bin/python', ['-m', 'scripts.ml.cross_capture', '--hidden-low'], { encoding: 'utf8' }));
const ms = performance.now() - t3;
const shaped = queues.hiddenQueue(hidden);
console.log(`hidden CLI: ${ms.toFixed(0)} ms, count ${hidden.count}, shaped rows ${shaped.count}, other bands ${JSON.stringify(shaped.rows.reduce((m, r) => ({ ...m, [r.other_band]: (m[r.other_band] || 0) + 1 }), {}))}`);
console.log('first hidden row', JSON.stringify(shaped.rows[0]));
// Summary and checkpoint routes on real data (review-feedback is replaced by a plain read of the same store).
const feedbackStub = {
  readFeedback: async file => JSON.parse(readFileSync(file, 'utf8')),
  feedbackFor: (store, cohort, id) => store[`${cohort}:${id}`] ?? (cohort === 'suburban_benchmark' ? store[String(id)] : undefined),
};
const routeWith = f => { const m = {}; new Function('exports', 'require', transpile(f))(m, id => id === 'next/server' ? { NextResponse: { json: (b, i) => Response.json(b, i) } } : id === '@/lib/queues' ? queues : id === '@/lib/review-feedback' ? feedbackStub : require(id)); return m; };
const summary = routeWith('src/app/api/queues/summary/route.ts'), checkpoints = routeWith('src/app/api/checkpoints/route.ts'), checkpoint = routeWith('src/app/api/checkpoints/[id]/route.ts');
let t = performance.now(); let res = await summary.GET(); let body = await res.json();
console.log(`summary: ${(performance.now() - t).toFixed(0)} ms, http ${res.status}`, JSON.stringify({ ...body, checkpoints: body.checkpoints.map(c => c.id), note: undefined }));
t = performance.now(); res = await checkpoints.GET(); body = await res.json();
console.log(`checkpoints: ${(performance.now() - t).toFixed(0)} ms, http ${res.status}`, body.checkpoints.map(c => `${c.id} ${c.created_at} sha ${c.report_sha256.slice(0, 8)} review ${c.review_path}`));
t = performance.now(); res = await checkpoint.GET({}, { params: Promise.resolve({ id: 'CP2' }) }); body = await res.json();
console.log(`checkpoint CP2: ${(performance.now() - t).toFixed(0)} ms, http ${res.status}, markdown ${body.markdown.length} chars, report keys ${Object.keys(body.report)}`);
res = await checkpoint.GET({}, { params: Promise.resolve({ id: 'CP9' }) }); console.log('checkpoint CP9 http', res.status);
