// Proof of record-chat-feedback.mjs preview_id: the reviewer approves an agent-drawn outline in chat, on a qa_chatring_<scene> scratch copy the app reads.
// Usage: APP_URL=http://localhost:3008 node scripts/test-record-chat-feedback-ring.mjs
// Needs the running app (GET/POST /api/scenes). Writes only data/scenes/qa_chatring_<scene> (deleted at the end) and a work folder
// (CHATRING_WORK, kept; else a temp folder, deleted). The real scene and the sealed preview are only read.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const base = process.env.APP_URL || process.env.ADDR_WEB || 'http://localhost:3008';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = process.env.CHATRING_SCENE || 'win-69468b2b783cd964', object = process.env.CHATRING_OBJECT || '547f6490e66df5d0';
const previewId = process.env.CHATRING_PREVIEW || '2a9d3cec06f824c9ca01769940d6447ea2c7940ae82820c4fa2ca8b7619d84b3';
const scratch = `qa_chatring_${source}`, folder = path.join(root, 'data/scenes', scratch), real = path.join(root, 'data/scenes', source);
const previewDir = path.join(root, 'data/reviews/eyes/polygon-preview', previewId);
const ownWork = !process.env.CHATRING_WORK, work = path.resolve(process.env.CHATRING_WORK || fs.mkdtempSync(path.join(os.tmpdir(), 'chatring-')));
fs.mkdirSync(work, { recursive: true });
const sha = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const savedReview = () => JSON.parse(fs.readFileSync(path.join(folder, 'review.json'), 'utf8'));
const reviewBytes = () => fs.readFileSync(path.join(folder, 'review.json'));
const preview = JSON.parse(fs.readFileSync(path.join(previewDir, 'manifest.json'), 'utf8')).content, binding = preview.binding;
const realSha = sha(path.join(real, 'review.json')), previewSha = sha(path.join(previewDir, 'manifest.json'));
const sentence = 'Yes, use the agent outline for that tree.';
const checks = [];
let n = 0;
function run(decisions, { apply = false, env = {} } = {}) {
  const file = path.join(work, `decisions-${++n}.json`);
  fs.writeFileSync(file, JSON.stringify({ decisions }, null, 1));
  const r = spawnSync(process.execPath, [path.join(root, 'scripts/record-chat-feedback.mjs'), file, ...(apply ? ['--apply'] : [])],
    { encoding: 'utf8', env: { ...process.env, ADDR_WEB: base, ...env }, maxBuffer: 64 << 20 });
  fs.writeFileSync(file.replace(/\.json$/, '.out.json'), r.stdout || r.stderr);
  assert.equal(r.status, 0, r.stderr);
  return JSON.parse(r.stdout);
}
const ok = (name, detail) => { checks.push({ name, ...detail }); console.error(`ok ${name}`); };
async function getScene() {
  const response = await fetch(`${base}/api/scenes?id=${scratch}`);
  assert.equal(response.status, 200, `GET ${scratch}`);
  return response.json();
}
assert.ok(!fs.existsSync(folder), `${scratch} already exists; remove the stale scratch copy first`);
try {
  // byte copy of the real scene folder; when the real scene moved past the preview's revision, rebuild that revision from its history
  fs.cpSync(real, folder, { recursive: true, filter: p => !/review\.lock$|\.tmp$/.test(p) });
  let review = savedReview();
  if (review.revision !== binding.revision) {
    const body = (review.history || []).find(h => h.revision === binding.revision);
    assert.ok(body, `revision ${binding.revision} is not in ${source} history`);
    fs.writeFileSync(path.join(folder, 'review.json'), JSON.stringify({ ...body, history: review.history.filter(h => h.revision < binding.revision) }, null, 2));
    review = savedReview();
  }
  let data = await getScene();
  assert.equal(data.stale, false); assert.equal(data.review.revision, binding.revision);
  const target = data.review.objects.find(o => o.id === object);
  assert.deepEqual({ ring: target.ring, holes: target.holes || [] }, binding.outline_pixels, 'scratch outline is the preview binding');
  ok('scratch copy read by the app', { scene: scratch, revision: data.review.revision, status: target.status, geometry_reviewed: target.geometry_reviewed, vertices: target.ring.length });
  const other = data.review.objects.find(o => o.id !== object && o.kind === target.kind && !['rejected', 'superseded'].includes(o.status)) || data.review.objects.find(o => o.id !== object);
  const good = { scene: scratch, object, preview_id: previewId, sentence };
  const before = reviewBytes();
  const refused = (name, decisions, pattern, env) => {
    const out = run(decisions, { env });
    assert.equal(out.scenes.length, 0, `${name}: nothing to save`); assert.equal(out.failed.length, 1); assert.match(String(out.failed[0].reason), pattern);
    assert.deepEqual(reviewBytes(), before, `${name}: review untouched`);
    ok(name, { reason: out.failed[0].reason });
  };
  refused('mismatched object refused', [{ ...good, object: other.id }], /bound to object/);
  refused('preview approval without the reviewer words refused', [{ ...good, sentence: undefined }], /reviewer's words/);
  refused('class_only with a preview refused', [{ ...good, class_only: true }], /class_only/);
  refused('rejected status with a preview refused', [{ ...good, status: 'rejected' }], /cannot be recorded/);
  // an altered sealed preview (copied out of the store, ring nudged) is refused before any binding check
  const altered = path.join(work, 'previews', previewId);
  fs.mkdirSync(altered, { recursive: true }); fs.copyFileSync(path.join(previewDir, 'card.png'), path.join(altered, 'card.png'));
  const manifest = JSON.parse(fs.readFileSync(path.join(previewDir, 'manifest.json'), 'utf8'));
  manifest.content.proposed_outline_pixels.ring[0][0] += 1; fs.writeFileSync(path.join(altered, 'manifest.json'), JSON.stringify(manifest));
  refused('altered preview refused', [good], /altered/, { ADDR_POLYGON_PREVIEW_DIR: path.join(work, 'previews') });
  // dry run prints the change and writes nothing
  let out = run([good]);
  assert.equal(out.failed.length, 0, JSON.stringify(out.failed)); assert.equal(out.scenes[0].saved, null);
  const change = out.scenes[0].changes[0];
  assert.equal(change.outline.after.vertices, preview.proposed_outline_pixels.ring.length); assert.equal(change.after.geometry_reviewed, true);
  assert.equal(change.after.status, target.status, 'status unchanged by default');
  assert.deepEqual(reviewBytes(), before, 'dry run writes nothing');
  ok('dry run prints the change', { change });
  // stale: the saved revision moves on (the unchanged review saved once through the app) and the same approval is refused
  let response = await fetch(`${base}/api/scenes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: scratch, review: data.review }) });
  assert.equal(response.status, 200, await response.clone().text());
  assert.equal((await response.json()).review.revision, binding.revision + 1);
  const bumped = reviewBytes();
  out = run([good]);
  assert.equal(out.scenes.length, 0); assert.match(out.failed[0].reason, /stale preview.*revision/); assert.deepEqual(reviewBytes(), bumped);
  ok('stale preview (revision changed) refused', { reason: out.failed[0].reason });
  fs.writeFileSync(path.join(folder, 'review.json'), before); // scratch copy back at the preview's revision
  data = await getScene(); assert.equal(data.review.revision, binding.revision);
  // apply: saved through POST /api/scenes; the saved ring is the preview ring
  // the same batch carries a plain chat decision (no preview) on another object: the existing path is unchanged beside it
  const plain = { scene: scratch, object: other.id, notes: 'Chat regression note', sentence: 'That one is fine as it is.' };
  const otherBefore = data.review.objects.find(o => o.id === other.id);
  const others = JSON.stringify(data.review.objects.filter(o => ![object, other.id].includes(o.id)));
  out = run([good, plain], { apply: true });
  assert.equal(out.failed.length, 0, JSON.stringify(out.failed)); assert.equal(out.scenes[0].saved, true); assert.equal(out.scenes[0].revision, binding.revision + 1);
  assert.equal(out.scenes[0].changes[0].outline.saved_matches_preview, true);
  review = savedReview();
  const saved = review.objects.find(o => o.id === object);
  assert.equal(review.revision, binding.revision + 1);
  assert.deepEqual(saved.ring, preview.proposed_outline_pixels.ring, 'saved ring equals the preview ring');
  assert.deepEqual(saved.holes || [], preview.proposed_outline_pixels.holes);
  assert.equal(saved.geometry_reviewed, true); assert.equal(saved.status, target.status);
  assert.ok(Math.abs(saved.model.footprint_area_px - preview.comparison.proposed_area_pixels) < 0.01, 'footprint area recomputed from the new ring');
  const hd = saved.provenance.human_decision;
  assert.equal(hd.source, 'human:chat'); assert.equal(hd.sentence, sentence); assert.equal(hd.preview_id, previewId); assert.equal(hd.drawn_by, 'agent');
  assert.equal(hd.fields[0], 'ring'); assert.ok(hd.fields.includes('geometry_reviewed'));
  assert.equal(JSON.stringify(review.objects.filter(o => ![object, other.id].includes(o.id))), others, 'other objects untouched');
  const plainSaved = review.objects.find(o => o.id === other.id), plainHd = plainSaved.provenance.human_decision;
  assert.deepEqual(plainSaved.ring, otherBefore.ring); assert.equal(plainSaved.status, otherBefore.status); assert.equal(plainSaved.geometry_reviewed, otherBefore.geometry_reviewed);
  assert.deepEqual(plainHd.fields, ['notes']); assert.equal(plainHd.sentence, plain.sentence); assert.equal(plainHd.preview_id, undefined); assert.equal(plainHd.drawn_by, undefined);
  assert.ok(plainSaved.notes.endsWith('Chat regression note'));
  data = await getScene();
  assert.deepEqual(data.review.objects.find(o => o.id === object).ring, preview.proposed_outline_pixels.ring);
  ok('apply saves the preview ring', { revision: review.revision, vertices: saved.ring.length, area_px: saved.model.footprint_area_px, area_m2: saved.model.area_m2, human_decision: hd });
  // the same approval again is stale: revision and outline both moved
  const after = reviewBytes();
  out = run([good], { apply: true });
  assert.equal(out.scenes.length, 0); assert.match(out.failed[0].reason, /stale preview/); assert.deepEqual(reviewBytes(), after);
  ok('re-apply of the used preview refused', { reason: out.failed[0].reason });
} finally {
  fs.rmSync(folder, { recursive: true, force: true });
  if (ownWork) fs.rmSync(work, { recursive: true, force: true });
}
assert.ok(!fs.existsSync(folder), 'scratch copy removed');
assert.equal(sha(path.join(real, 'review.json')), realSha, 'real scene review untouched');
assert.equal(sha(path.join(previewDir, 'manifest.json')), previewSha, 'sealed preview untouched');
ok('scratch removed; real scene and preview untouched', { scratch_exists: false });
console.log(JSON.stringify({ ok: true, base, scratch, checks }, null, 1));
