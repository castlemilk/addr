// Recapture carry end to end on scratch copies (written 2026-09-30, not run that night: it needs the deferred app wiring of
// src/lib/scene-frame-transform.ts into scene-store loadScene, scene-feedback recoverSceneReview and the editor banner, and a
// host with memory). qa_recap_src_<ts> copies a cohort scene with its saved review (default roof_training_250-30381: confirmed,
// rejected and needs_adjustment decisions, roof_annotation / roof_parts / roof_lines in metres); qa_recap_dst_<ts> copies the
// snapshot and image of the registered frame that holds it (default mooroolbark-50011498-17-118443-80417). The target's own
// review.json is never copied: the scratch recapture starts without a review, and the real target's decisions stay where they
// are. The scratch recapture records the frame_transform that scene_recapture.measured_transform measures between the two
// copies, which is what `scene_lot --capture --carry-from` records on a new lot-<address_id> scene.
// Wiring contract this check codes against (docs/recapture-carry-2026-09-30.md section 6): for a recapture with no review.json
// of its own whose frame_transform.source_scene_id has a saved review, GET /api/scenes answers review null, stale true and a
// recovery { method: 'georeferenced-affine', review, carried_objects, new_candidates, dropped, clipped, ... } built from the
// source scene's review. Today loadScene sets stale only when the scene's own review sits on another snapshot and builds a
// recovery only then, and the editor renders the recovery notice and its "Carry edits forward" button only under data.stale.
// The Python verifier must accept the pair; then the editor must name the re-projection in its banner, the recovery must hold
// no carried object as confirmed or rejected, and "Carry edits forward" must write only the scratch recapture (revision 1,
// stale false afterwards), with every metre-frame field translated by metre_offset (z unchanged) and every unclipped pixel
// ring mapped through pixel_affine.
// Cleanup: both scratch folders are removed, then every check is computed and printed before any fails (a cleanup problem
// never hides an earlier failure): no real latest.json or confidence.json written during the check, and the real source and
// target latest.json, confidence.json, model snapshots and images byte-identical to the baseline. Their review.json files are
// the reviewer's (saved live through the app): a save there during the check is printed as a note, never failed.
// Every figure here is agreement between two frames, never accuracy; a carried verdict is a hint, never a label.
// Usage: APP_URL=http://localhost:3008 node scripts/verify-scene-recapture.mjs   (task verify:scene-recapture)
// Env: RECAP_SOURCE / RECAP_TARGET (scene ids); RECAP_JOBGATE_MB (default 1500; 0 skips the .run/jobgate.sh memory gate).
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, rmSync, statSync } from 'node:fs';
import puppeteer from 'puppeteer-core';

const base = process.env.APP_URL || 'http://localhost:3008';
const PYTHON = 'scripts/ml/.venv/bin/python';
const SOURCE = process.env.RECAP_SOURCE || 'roof_training_250-30381';
const TARGET = process.env.RECAP_TARGET || 'mooroolbark-50011498-17-118443-80417';
const SCENE_ID = /^[a-zA-Z0-9_-]{1,100}$/, SCRATCH = /^qa_recap_(src|dst)_\d+$/;
const stamp = Date.now(), src = `qa_recap_src_${stamp}`, dst = `qa_recap_dst_${stamp}`, scratch = [src, dst];
const budget = Number(process.env.RECAP_JOBGATE_MB ?? 1500);
assert(SCENE_ID.test(SOURCE) && SCENE_ID.test(TARGET) && SOURCE !== TARGET, 'RECAP_SOURCE and RECAP_TARGET are two scene ids');
assert(scratch.every(id => SCRATCH.test(id)), 'scratch ids only');
assert([SOURCE, TARGET].every(id => existsSync(`data/scenes/${id}/latest.json`)), 'both real scenes exist under data/scenes/ (run from the repo root)');
assert(Number.isInteger(budget) && budget >= 0, 'RECAP_JOBGATE_MB is a whole number of MB');

// One heavy job at a time on this host: a headless browser beside another job, or on a swap-starved host, is refused.
if (budget > 0 && existsSync('.run/jobgate.sh')) {
  const gate = spawnSync('/bin/bash', ['-c', `. .run/jobgate.sh && jobgate ${budget}`], { encoding: 'utf8' });
  console.log((gate.stdout || '').trim());
  assert.equal(gate.status, 0, `the host job gate is closed, so this check does not run now:\n${gate.stdout}${gate.stderr}`);
}

const sha = file => createHash('sha256').update(readFileSync(file)).digest('hex');
const readJson = file => JSON.parse(readFileSync(file, 'utf8'));
// The real files this check reads and must leave byte-identical: each real scene's latest.json, confidence.json, model snapshots
// and images. Its review.json is the reviewer's (saved live through the app), so it is hashed apart and a save there during the
// check is printed as a note, never failed as a write by this check.
const realFiles = id => {
  const folder = `data/scenes/${id}`, models = `${folder}/models`;
  return [`${folder}/latest.json`, `${folder}/confidence.json`, ...readdirSync(folder).filter(f => f.endsWith('.png')).map(f => `${folder}/${f}`),
    ...(existsSync(models) ? readdirSync(models).filter(f => f.endsWith('.json')).map(f => `${models}/${f}`) : [])].filter(f => existsSync(f));
};
const REAL_FILES = [SOURCE, TARGET].flatMap(realFiles), REVIEW_FILES = [SOURCE, TARGET].map(id => `data/scenes/${id}/review.json`);
const hashes = files => Object.fromEntries(files.map(f => [f, existsSync(f) ? sha(f) : null]));
const baseline = () => hashes(REAL_FILES), reviewHashes = () => hashes(REVIEW_FILES);
/** Real latest.json / confidence.json files written since `t` (the reviewer's own review.json saves are not writes to police). */
const realWritesSince = t => readdirSync('data/scenes').filter(id => !id.startsWith('qa_') && !id.startsWith('.'))
  .flatMap(id => ['latest.json', 'confidence.json'].map(f => `data/scenes/${id}/${f}`)).filter(f => existsSync(f) && statSync(f).mtimeMs > t);
/** The cleanup verdict, every check computed before any is reported: scratch folders gone, no real latest.json or confidence.json
 * written since `startedAt`, the real source and target files byte-identical to `before`. A changed real review.json is the
 * reviewer's own save: a note, never a problem. */
function cleanupReport(before, reviewsBefore, startedAt) {
  const after = baseline(), reviewsAfter = reviewHashes();
  return {
    checked: Object.keys(after).length,
    notes: REVIEW_FILES.filter(f => reviewsAfter[f] !== reviewsBefore[f])
      .map(f => `the reviewer saved ${f} during the check (their own save through the app, not a write by this check)`),
    problems: [
      ...scratch.filter(id => existsSync(`data/scenes/${id}`)).map(id => `scratch folder data/scenes/${id} was not removed`),
      ...realWritesSince(startedAt).map(f => `a real latest.json or confidence.json was written during the check: ${f}`),
      ...Object.keys(before).filter(f => after[f] !== before[f]).map(f => `a real file of the source or target scene changed: ${f}`),
    ],
  };
}
const api = async id => {
  const response = await fetch(`${base}/api/scenes?id=${encodeURIComponent(id)}`, { cache: 'no-store' });
  return { status: response.status, body: response.status === 200 ? await response.json() : await response.text() };
};
const tally = values => values.reduce((out, v) => ({ ...out, [v]: (out[v] || 0) + 1 }), {});

// Build the scratch pair with the repository's own code: the source snapshot the saved review sits on (hash-checked by
// carry_source), the ids rewritten, both model signatures recomputed from content as save_scene would, and the target's
// frame_transform measured from the two images' bytes against DESIGN.json. Writes only data/scenes/qa_recap_{src,dst}_<ts>/.
const PREPARE = String.raw`
import json, re, sys
from pathlib import Path
from scripts.ml import scene_recapture as sr
source_id, target_id, src, dst = sys.argv[1:5]
assert all(re.fullmatch(r'qa_recap_(src|dst)_\d+', s) for s in (src, dst)), 'scratch ids only'
scenes = Path('data/scenes')
assert not (scenes/src).exists() and not (scenes/dst).exists(), 'a scratch folder already exists'
snapshot, source_png = sr.carry_source(Path('.'), source_id)
review = json.loads((scenes/source_id/'review.json').read_text())
assert review.get('model_signature') == snapshot['model_signature'], 'the source review sits on another snapshot'
target = json.loads((scenes/target_id/'latest.json').read_text())
target_png = (scenes/target_id/f"{target['image_sha256']}.png").read_bytes()
def write(scene_id, scene, image):
    folder = scenes/scene_id
    (folder/'models').mkdir(parents=True)
    body = json.dumps(scene)
    (folder/'latest.json').write_text(body)
    (folder/'models'/f"{scene['model_signature']}.json").write_text(body)
    (folder/f"{scene['image_sha256']}.png").write_bytes(image)
source = {**snapshot, 'id': src}
source['model_signature'] = sr.content_signature(source)
write(src, source, source_png)
(scenes/src/'review.json').write_text(json.dumps({**review, 'model_signature': source['model_signature']}))
recapture = {k: v for k, v in target.items() if k not in ('frame_transform', 'model_signature')}
recapture['id'] = dst
recapture['frame_transform'] = sr.measured_transform(source, recapture, source_png, target_png, sr.load_design(Path('.')))
recapture['model_signature'] = sr.content_signature(recapture)
write(dst, recapture, target_png)
print(json.dumps({'source_signature': source['model_signature'], 'target_signature': recapture['model_signature'],
                  'frame_transform': recapture['frame_transform']}))
`;
const python = (args, label) => {
  const started = Date.now(), result = spawnSync(PYTHON, args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 16 << 20 });
  console.log(`${label}: exit ${result.status} in ${((Date.now() - started) / 1000).toFixed(1)} s`);
  return result;
};

// Metre-frame fields of one object, by path (the fields the carry translates by metre_offset; z is kept).
const metreFields = o => [
  ['roof_annotation.vertices', o.roof_annotation?.vertices], ['roof_line_edits.vertices', o.roof_line_edits?.vertices],
  ...(o.roof_lines || []).map((line, i) => [`roof_lines[${i}].line_meters`, line?.line_meters]),
  ...(o.roof_parts || []).flatMap((part, i) => [[`roof_parts[${i}].ring_meters`, part?.ring_meters], [`roof_parts[${i}].ring`, part?.ring],
    [`roof_parts[${i}].coords`, part?.coords]]),
].filter(([, points]) => Array.isArray(points) && points.length && points.every(p => Array.isArray(p) && p.length >= 2));
const applyAffine = (m, p) => [p[0] * m[0][0] + p[1] * m[1][0] + m[2][0], p[0] * m[0][1] + p[1] * m[1][1] + m[2][1]];

/** The carried objects of a review (recovery preview or saved), each checked against the status policy and the transform. */
function checkCarried(review, label, { sourceById, targetModel, transform, clipped }) {
  const carried = review.objects.filter(o => o.carried_from);
  assert(carried.length > 0, `${label}: at least one object is carried`);
  let metreChecks = 0, ringChecks = 0;
  for (const o of carried) {
    const from = o.carried_from, before = sourceById.get(from.object_id);
    assert(!['confirmed', 'rejected'].includes(o.status), `${label}: carried ${o.id} came back ${o.status}`);
    assert(['needs_adjustment', 'superseded'].includes(o.status), `${label}: carried ${o.id} is ${o.status}`);
    assert.equal(o.geometry_reviewed, false, `${label}: carried ${o.id} keeps no outline check`);
    assert.equal(o.model?.height_verified, false, `${label}: carried ${o.id} keeps no height check`);
    assert(!String(o.provenance?.source ?? '').startsWith('machine:'), `${label}: carried ${o.id} carries no machine:* provenance`);
    assert.equal(from.method, 'georeferenced-affine', `${label}: ${o.id} carried_from.method`);
    assert.equal(from.scene_id, src, `${label}: ${o.id} carried_from.scene_id`);
    assert.equal(from.model_signature, sourceById.signature, `${label}: ${o.id} carried_from.model_signature`);
    assert(before, `${label}: ${o.id} names a source object (${from.object_id})`);
    assert.equal(from.status, before.status, `${label}: ${o.id} keeps the earlier verdict only as a hint`);
    assert(typeof from.iou_with_new === 'number' && from.iou_with_new >= 0 && from.iou_with_new <= 1, `${label}: ${o.id} iou_with_new`);
    assert(from.matched_id === null || targetModel.objects.some(m => m.id === from.matched_id), `${label}: ${o.id} matched_id names a new-model object`);
    if (o.roof_line_edits) assert.equal(o.roof_line_edits.checked, false, `${label}: ${o.id} roof-line check reset`);
    const after = new Map(metreFields(o));
    for (const [path, points] of metreFields(before)) {
      const moved = after.get(path);
      if (!moved) continue;
      assert.equal(moved.length, points.length, `${label}: ${o.id} ${path} keeps its points`);
      points.forEach((p, i) => {
        const q = moved[i];
        assert(Math.abs(q[0] - (p[0] + transform.metre_offset[0])) < 1e-5 && Math.abs(q[1] - (p[1] + transform.metre_offset[1])) < 1e-5,
          `${label}: ${o.id} ${path}[${i}] moves by metre_offset: ${JSON.stringify(p)} -> ${JSON.stringify(q)}`);
        assert.deepEqual(q.slice(2), p.slice(2), `${label}: ${o.id} ${path}[${i}] keeps z`);
      });
      metreChecks += 1;
    }
    if (!clipped.includes(o.id)) {
      assert.equal(o.ring.length, before.ring.length, `${label}: unclipped ${o.id} keeps its outline points`);
      o.ring.forEach((p, i) => {
        const q = applyAffine(transform.pixel_affine, before.ring[i]);
        assert(Math.hypot(p[0] - q[0], p[1] - q[1]) < 1e-4, `${label}: ${o.id} ring[${i}] maps through pixel_affine`);
      });
      ringChecks += 1;
    }
  }
  const byId = new Map(review.objects.map(o => [o.id, o]));
  for (const m of targetModel.objects) {
    const kept = byId.get(m.id);
    assert(kept && !kept.carried_from && kept.kind === m.kind, `${label}: new-model object ${m.id} stays under its own id`);
    assert.deepEqual(kept.ring, m.ring, `${label}: new-model object ${m.id} keeps its outline`);
  }
  return { carried, metreChecks, ringChecks };
}

const before = baseline(), reviewsBefore = reviewHashes(), started = Date.now();
console.log(`Baseline: ${Object.entries(before).map(([f, h]) => `${f} ${String(h).slice(0, 12)}`).join('; ')}`);
let browser = null, checkError = null;
try {
  // 1. The scratch pair, then the byte-level verifier on it (exit 0 and verified true, or nothing else runs).
  const prepared = python(['-c', PREPARE, SOURCE, TARGET, src, dst], 'prepare scratch pair');
  assert.equal(prepared.status, 0, `the scratch pair could not be built: ${(prepared.stderr || '').slice(-1500)}`);
  const pair = JSON.parse(prepared.stdout.trim().split('\n').at(-1));
  const transform = pair.frame_transform;
  assert.equal(transform.verified, true, `the measured frame_transform passes DESIGN.json (shift ${transform.residual?.shift_m} m, ncc ${transform.residual?.ncc})`);
  const checked = python(['-m', 'scripts.ml.scene_recapture', '--verify-source', `data/scenes/${src}/models/${pair.source_signature}.json`,
    '--verify-target', `data/scenes/${dst}/models/${pair.target_signature}.json`], 'scene_recapture --verify');
  assert.equal(checked.status, 0, `the Python verifier refuses the scratch pair:\n${checked.stderr}`);
  const verdict = JSON.parse(checked.stdout.trim());
  assert(verdict.verified === true && verdict.source_scene_id === src && verdict.target_scene_id === dst, `verifier output: ${checked.stdout}`);
  console.log(`Frame: overlap ${transform.overlap.fraction}, shift ${verdict.shift_m} m, ncc ${verdict.ncc} (agreement against DESIGN.json, never accuracy)`);
  const sourceReview = readJson(`data/scenes/${src}/review.json`), targetModel = readJson(`data/scenes/${dst}/latest.json`);
  const sourceById = Object.assign(new Map(sourceReview.objects.map(o => [o.id, o])), { signature: pair.source_signature });
  const scratchHashes = { review: sha(`data/scenes/${src}/review.json`), latest: sha(`data/scenes/${src}/latest.json`), target: sha(`data/scenes/${dst}/latest.json`) };

  // 2. The wiring: the server offers the georeferenced-affine carry for the scratch recapture, or this check stops here.
  const offered = await api(dst);
  assert.equal(offered.status, 200, `GET /api/scenes?id=${dst} answers 200 (got ${offered.status}: ${String(offered.body).slice(0, 300)})`);
  const recovery = offered.body.recovery;
  assert(recovery?.method === 'georeferenced-affine', 'the app wiring for the georeferenced-affine carry is not live (recovery method ' +
    `${JSON.stringify(recovery?.method ?? null)}, reason ${JSON.stringify(offered.body.recovery_reason ?? null)}): wire ` +
    'src/lib/scene-frame-transform.ts in a reviewer-declared quiet window first (docs/recapture-carry-2026-09-30.md)');
  assert.equal(offered.body.stale, true, 'a recapture with no review of its own whose source scene has a saved review reports stale ' +
    'true, so the editor renders the recovery notice and its Carry edits forward button (page.tsx renders them only under data.stale)');
  assert.equal(offered.body.review, null, 'the scratch recapture has no review of its own before the carry');
  const clipped = recovery.clipped || [];
  const preview = checkCarried(recovery.review, 'recovery preview', { sourceById, targetModel, transform, clipped });
  const earlier = tally(preview.carried.map(o => o.carried_from.status));
  assert((earlier.confirmed || 0) >= 1 && (earlier.rejected || 0) >= 1, `the carry exercises the policy on confirmed and rejected verdicts (${JSON.stringify(earlier)})`);
  assert(preview.metreChecks > 0, 'at least one metre-frame field was compared');
  assert.equal(recovery.carried_objects, preview.carried.length, 'carried_objects counts the carried objects');
  assert.equal(recovery.new_candidates, targetModel.objects.length, 'every new-model object is offered for duplicate review');
  console.log(`Preview: ${preview.carried.length} carried (earlier verdicts ${JSON.stringify(earlier)}), ${preview.metreChecks} metre fields ` +
    `and ${preview.ringChecks} rings checked, ${clipped.length} clipped, ${(recovery.dropped || []).length} dropped`);

  // 3. The editor: the banner names the re-projection; "Carry edits forward" writes only the scratch recapture.
  browser = await puppeteer.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true,
    args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'], protocolTimeout: 240000 });
  const page = await browser.newPage(), errors = [], writes = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => {
    if (!['POST', 'PUT', 'PATCH', 'DELETE'].includes(r.method())) return;
    const url = new URL(r.url());
    let body = null;
    try { body = JSON.parse(r.postData() || 'null'); } catch { body = { unparsable: true }; }
    writes.push({ method: r.method(), path: url.pathname, id: body?.id ?? url.searchParams.get('id') ?? null });
  });
  await page.setViewport({ width: 1400, height: 1000 });
  await page.goto(`${base}/scenes?scene=${dst}&status=all`, { waitUntil: 'networkidle2', timeout: 120000 });
  await page.waitForSelector('.scene-svg', { timeout: 60000 });
  const carryButton = await page.waitForSelector('.review-notice button::-p-text(Carry edits forward)', { timeout: 60000 });
  const banner = await carryButton.evaluate(button => button.closest('.review-notice').innerText.replace(/\s+/g, ' '));
  assert.match(banner, /re-projected through the georeferenced frame/, `the banner names the re-projection: ${banner}`);
  assert.match(banner, /residual [\d.]+ px/, `the banner gives the measured residual: ${banner}`);
  assert.match(banner, /every carried decision returns for a recheck/i, `the banner says every carried decision returns for a recheck: ${banner}`);
  assert(!banner.includes('The image is unchanged.') && !banner.includes('original image pixels were preserved'), `a recapture is never called the same image: ${banner}`);
  assert.deepEqual(writes, [], 'opening the recapture writes nothing');
  await carryButton.click();
  await page.waitForFunction(() => document.body.innerText.includes('Saved revision 1') || document.querySelector('.scene-status-space [role="alert"]')?.textContent,
    { timeout: 60000 });
  const failure = await page.$eval('body', b => b.querySelector('.scene-status-space [role="alert"]')?.textContent || '');
  assert.equal(failure, '', `Carry edits forward saved without an error: ${failure}`);
  assert(writes.length >= 1, 'Carry edits forward posted the carried review');
  assert.deepEqual(writes.filter(w => !(w.path === '/api/scenes' && w.id === dst)), [], `every write went to POST /api/scenes for ${dst} only: ${JSON.stringify(writes)}`);

  // 4. The saved review: same policy, metre fields still translated; the source review and both snapshots untouched.
  const saved = await api(dst);
  assert.equal(saved.status, 200);
  const review = saved.body.review;
  assert(review && review.revision === 1 && review.model_signature === pair.target_signature && saved.body.stale === false,
    `the carried review is saved on the recapture's own snapshot (revision ${review?.revision}, stale ${saved.body.stale})`);
  const stored = checkCarried(review, 'saved review', { sourceById, targetModel, transform, clipped });
  assert(stored.metreChecks > 0, 'metre-frame fields survive the save and stay translated');
  assert.equal(sha(`data/scenes/${src}/review.json`), scratchHashes.review, 'the carry never writes the source review');
  assert.equal(sha(`data/scenes/${src}/latest.json`), scratchHashes.latest, 'the source snapshot is never rewritten');
  assert.equal(sha(`data/scenes/${dst}/latest.json`), scratchHashes.target, 'the recapture snapshot is not rewritten by the save');
  assert.deepEqual(errors, [], `no page errors: ${errors.join('; ')}`);
  await page.screenshot({ path: '/tmp/addr-scene-recapture.png' });
  console.log(`Recapture carry on scratch scenes: banner names the re-projection, ${stored.carried.length} carried objects saved as ` +
    `needs_adjustment or superseded (none confirmed or rejected), ${stored.metreChecks} metre fields translated, writes only ${dst}.`);
} catch (error) {
  checkError = error;
} finally {
  if (browser) await browser.close().catch(error => console.log(`Browser close failed: ${error.message}`));
  for (const id of scratch) if (SCRATCH.test(id)) rmSync(`data/scenes/${id}`, { recursive: true, force: true });
}
// Every cleanup check is computed and printed before any fails, and a cleanup problem never hides the failure above.
let cleanup;
try {
  cleanup = cleanupReport(before, reviewsBefore, started);
} catch (error) {
  cleanup = { checked: 0, notes: [], problems: [`the cleanup checks could not run: ${error.message}`] };
}
for (const note of cleanup.notes) console.log(`Note: ${note}`);
console.log(cleanup.problems.length ? `Cleanup problems:\n- ${cleanup.problems.join('\n- ')}`
  : `Cleanup: scratch folders removed; ${cleanup.checked} real files unchanged; no real latest.json or confidence.json written.`);
if (checkError) throw checkError;
assert.deepEqual(cleanup.problems, [], 'the scratch folders are removed and no real scene file was written or changed during the check');
console.log('Passed: the recapture carry on scratch scenes and the cleanup checks.');
