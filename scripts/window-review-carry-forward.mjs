#!/usr/bin/env node
// Carry a carry-written recapture-window review forward onto the window's detected model, with the editor's own
// recovery (src/lib/scene-feedback.ts recoverSceneReview), so the review page stops reading it as stale.
//
//   node scripts/window-review-carry-forward.mjs [--dry-run] [--continue] [--no-scratch] [--api URL]
//        [--token-file PATH] [--log PATH] [--registry PATH] <window-id> [<window-id> ...]
//
// For one window (data/scenes/lot-* or win-*): previous = models/<review.model_signature>.json (the capture snapshot the
// review names), current = latest.json (the detection), saved = review.json. Refuses unless the review is still the
// carry's (revision 1, updated_by 'scripts.ml.scene_focus carry'), the image is unchanged (same image_sha256) and the
// recovery is non-null. The merged review keeps every carried copy field by field (asserted), adds the detections as
// candidates only, keeps revision 1 and history, and is validated first by POSTing it to a qa_ scratch COPY of the
// window through the app (/api/scenes runs validateSceneReview; the scratch is deleted afterwards) before the real
// review.json is written directly (review.lock mkdir protocol, temp + rename). One registry row (role carry_forward)
// is appended per written window. --dry-run writes nothing and prints the diff summary.
//
// TypeScript loading follows scripts/test-scene-feedback.mjs (ts.transpileModule into a temp dir, ESM import) and
// scripts/test-scene-recapture.mjs (CommonJS loader for read-only helpers).
import {createHash, randomBytes} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import {appendFile, cp, mkdir, mkdtemp, readFile, rename, rm, rmdir, writeFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import ts from 'typescript';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CARRY_BY = 'scripts.ml.scene_focus carry';
const FORWARD_BY = 'scripts.ml.scene_focus carry-forward';
const ROLE = 'carry_forward';
const REGISTRY_VERSION = 'windows-registry-v1';
const HEX64 = /^[a-f0-9]{64}$/;
const SCENE_ID = /^[a-zA-Z0-9_-]{1,100}$/;

// --- arguments ---------------------------------------------------------------------------------------------------
const args = process.argv.slice(2);
const options = {dryRun: false, continueOnError: false, scratch: true, api: 'http://localhost:3008', tokenFile: null,
  log: path.join(ROOT, '.run/focus-recapture/carry-forward-2026-10-02.log'),
  registry: path.join(ROOT, 'data/reviews/eyes/recapture/windows-2026-10-01.jsonl')};
const ids = [];
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === '--dry-run') options.dryRun = true;
  else if (a === '--continue') options.continueOnError = true;
  else if (a === '--no-scratch') options.scratch = false;
  else if (a === '--api') options.api = args[++i];
  else if (a === '--token-file') options.tokenFile = path.resolve(ROOT, args[++i]);
  else if (a === '--log') options.log = path.resolve(ROOT, args[++i]);
  else if (a === '--registry') options.registry = path.resolve(ROOT, args[++i]);
  else if (a.startsWith('--')) throw new Error(`Unknown option ${a}`);
  else ids.push(a);
}
if (!ids.length) {
  console.error('Usage: node scripts/window-review-carry-forward.mjs [--dry-run] [--continue] [--no-scratch] [--api URL] [--token-file PATH] [--log PATH] [--registry PATH] <window-id>...');
  process.exit(2);
}

// --- logging -----------------------------------------------------------------------------------------------------
await mkdir(path.dirname(options.log), {recursive: true});
const stamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
async function log(line) {
  const text = `${stamp()} ${line}`;
  console.log(text);
  await appendFile(options.log, text + '\n');
}

// --- the editor's modules ----------------------------------------------------------------------------------------
const transpileDir = await mkdtemp(path.join(tmpdir(), 'addr-window-carry-forward-'));
for (const name of ['scene-feedback', 'scene-structure', 'scene-frame-transform']) { // scene-feedback delegates the recapture carry to scene-frame-transform (2026-10-03)
  const code = ts.transpileModule(await readFile(path.join(ROOT, `src/lib/${name}.ts`), 'utf8'),
    {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022}}).outputText
    .replaceAll(/from "\.\/([a-z-]+)"/g, 'from "./$1.mjs"');
  await writeFile(path.join(transpileDir, `${name}.mjs`), code);
}
const {recoverSceneReview} = await import(pathToFileURL(path.join(transpileDir, 'scene-feedback.mjs')));
const req = createRequire(import.meta.url), cjsCache = new Map();
function loadCjs(name) {
  if (cjsCache.has(name)) return cjsCache.get(name);
  const exports = {}; cjsCache.set(name, exports);
  const source = ts.transpileModule(readFileSync(path.join(ROOT, `src/lib/${name}.ts`), 'utf8'),
    {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
  new Function('exports', 'require', source)(exports, id => id.startsWith('./') ? loadCjs(id.slice(2)) : req(id));
  return exports;
}
const {footprintMetrics} = loadCjs('scene-types'); // read-only: reports whether the editor's next save would recompute a copy's metrics

// --- helpers -----------------------------------------------------------------------------------------------------
const sha256 = buffer => createHash('sha256').update(buffer).digest('hex');
const json = value => JSON.stringify(value);
const readJson = async file => JSON.parse(await readFile(file, 'utf8'));
class Refusal extends Error { constructor(reason, detail) { super(detail ? `${reason}: ${detail}` : reason); this.reason = reason; } }

/** Keys whose JSON differs between two objects (field by field; missing and undefined are the same). */
function diffKeys(a, b) {
  const keys = new Set([...Object.keys(a || {}), ...Object.keys(b || {})]);
  return [...keys].filter(k => json(a?.[k] ?? null) !== json(b?.[k] ?? null)).sort();
}

function sortedRow(row) {
  return JSON.stringify(Object.fromEntries(Object.keys(row).sort().map(k => [k, row[k]])));
}

const token = options.tokenFile ? readFileSync(options.tokenFile, 'utf8').trim() : null; // never logged

// --- one window --------------------------------------------------------------------------------------------------
async function forward(id) {
  if (!SCENE_ID.test(id) || !/^(lot|win)-/.test(id)) throw new Refusal('not_a_window_id', id);
  const dir = path.join(ROOT, 'data/scenes', id);
  const reviewPath = path.join(dir, 'review.json');
  if (!existsSync(reviewPath)) throw new Refusal('no_review', `${id} has no review.json`);
  if (existsSync(path.join(dir, 'review.lock'))) throw new Refusal('review_locked', `${id} is saving in the app (review.lock present); nothing written`);
  const reviewBytes = await readFile(reviewPath);
  const review = JSON.parse(reviewBytes.toString('utf8'));
  const latest = await readJson(path.join(dir, 'latest.json'));
  if (review.revision !== 1) throw new Refusal('revision_not_1', `${id} review.json is revision ${review.revision} (the reviewer saved); nothing written`);
  if (review.updated_by !== CARRY_BY) throw new Refusal('not_carry_author', `${id} review.json updated_by is ${JSON.stringify(review.updated_by)}, not ${JSON.stringify(CARRY_BY)}; nothing written`);
  if (!HEX64.test(review.model_signature || '') || !HEX64.test(latest.model_signature || '')) throw new Refusal('invalid_signature', id);
  if (review.model_signature === latest.model_signature) throw new Refusal('already_current', `${id} review already names latest.json's model_signature`);
  if (latest.id !== id) throw new Refusal('id_mismatch', `${id} latest.json id is ${latest.id}`);
  const previousPath = path.join(dir, 'models', `${review.model_signature}.json`);
  const currentPath = path.join(dir, 'models', `${latest.model_signature}.json`);
  if (!existsSync(previousPath)) throw new Refusal('previous_snapshot_missing', `${id} models/${review.model_signature.slice(0, 12)}.json`);
  if (!existsSync(currentPath)) throw new Refusal('current_snapshot_missing', `${id} models/${latest.model_signature.slice(0, 12)}.json`);
  const previous = await readJson(previousPath);
  const currentSnapshot = await readJson(currentPath);
  if (previous.model_signature !== review.model_signature || previous.id !== id) throw new Refusal('previous_snapshot_mismatch', id);
  if (currentSnapshot.model_signature !== latest.model_signature || currentSnapshot.image_sha256 !== latest.image_sha256) throw new Refusal('current_snapshot_mismatch', `${id} models/<latest signature>.json does not match latest.json`);
  if (previous.image_sha256 !== latest.image_sha256) throw new Refusal('image_changed', `${id} capture image ${previous.image_sha256.slice(0, 12)} != detection image ${latest.image_sha256.slice(0, 12)}; the editor's recovery would refuse too`);

  // The editor's own recovery, exactly as src/lib/scene-store.ts loadScene calls it (previous, current, saved).
  const recovery = recoverSceneReview(previous, latest, review);
  if (!recovery) throw new Refusal('recovery_null', `${id}: recoverSceneReview returned null (frame or image changed)`);
  const merged = recovery.review;

  // --- assertions: copies unchanged, detections as candidates only, nothing confirmed, identity kept -------------
  const savedById = new Map(review.objects.map(o => [o.id, o]));
  const mergedById = new Map(merged.objects.map(o => [o.id, o]));
  const problems = [];
  const copies = review.objects.filter(o => o.carried_from && typeof o.carried_from === 'object');
  const nonCopies = review.objects.length - copies.length;
  if (nonCopies) problems.push(`${nonCopies} saved object(s) without carried_from`);
  for (const saved of review.objects) {
    const out = mergedById.get(saved.id);
    if (!out) { problems.push(`copy ${saved.id} missing from the merged review`); continue; }
    const diff = diffKeys(saved, out);
    if (diff.length) problems.push(`copy ${saved.id} changed: ${diff.join(',')}`);
    if (saved.status !== 'needs_adjustment') problems.push(`copy ${saved.id} saved status is ${saved.status}, not needs_adjustment`);
    if (out.status !== saved.status) problems.push(`copy ${saved.id} status ${saved.status} -> ${out.status}`);
  }
  const collisions = latest.objects.filter(o => savedById.has(o.id)).map(o => o.id);
  const replaces = latest.objects.filter(o => Array.isArray(o.replaces) && o.replaces.length).length;
  let candidateDiffs = 0;
  for (const detected of latest.objects) {
    if (savedById.has(detected.id)) continue;
    const out = mergedById.get(detected.id);
    if (!out) { problems.push(`detection ${detected.id} missing from the merged review`); continue; }
    if (out.status !== 'candidate') problems.push(`detection ${detected.id} enters as ${out.status}, not candidate`);
    if (out.geometry_reviewed) problems.push(`detection ${detected.id} enters geometry_reviewed`);
    if (diffKeys(detected, out).length) candidateDiffs++;
  }
  const expectedCount = review.objects.length + latest.objects.length - collisions.length;
  if (merged.objects.length !== expectedCount) problems.push(`merged objects ${merged.objects.length} != copies ${review.objects.length} + detections ${latest.objects.length} - collisions ${collisions.length}`);
  if (recovery.carried_objects !== review.objects.length) problems.push(`recovery.carried_objects ${recovery.carried_objects} != saved objects ${review.objects.length}`);
  if (recovery.new_candidates !== latest.objects.length - collisions.length) problems.push(`recovery.new_candidates ${recovery.new_candidates} != ${latest.objects.length - collisions.length}`);
  if (merged.objects.some(o => o.status === 'confirmed')) problems.push('a merged object is confirmed');
  if (merged.revision !== 1) problems.push(`merged revision ${merged.revision} != 1`);
  if (merged.model_signature !== latest.model_signature) problems.push('merged model_signature is not latest.json\'s');
  const mergedGroups = new Map(merged.groups.map(g => [g.id, g]));
  for (const g of review.groups) if (json(mergedGroups.get(g.id) ?? null) !== json(g)) problems.push(`saved group ${g.id} missing or changed`);
  for (const g of latest.groups) if (!mergedGroups.has(g.id)) problems.push(`detection group ${g.id} missing`);
  if (problems.length) throw new Refusal('assertion_failed', `${id}: ${problems.join('; ')}`);

  // Informational: would the editor's save-time footprintMetrics change a copy's stored model metrics? (It recomputes
  // width/length/area/rotation/footprint_area_px on every save; this script does not, so copies stay byte-equal.)
  let metricsWouldChange = 0;
  for (const copy of copies) {
    const m = footprintMetrics(copy.ring, latest.pixel_to_meters, copy.holes || []);
    if (Object.keys(m).some(k => json(copy.model?.[k] ?? null) !== json(m[k] ?? null))) metricsWouldChange++;
  }

  // --- the review to write: saved key order, carried fields verbatim, new metadata -----------------------------
  const now = stamp();
  const out = {};
  for (const key of Object.keys(review)) {
    if (key === 'revision') out.revision = 1;
    else if (key === 'model_signature') out.model_signature = latest.model_signature;
    else if (key === 'objects') out.objects = merged.objects;
    else if (key === 'groups') out.groups = merged.groups;
    else if (key === 'reviewed_classes') out.reviewed_classes = merged.reviewed_classes;
    else if (key === 'updated_at') out.updated_at = now;
    else if (key === 'updated_by') out.updated_by = FORWARD_BY;
    else out[key] = review[key]; // history, basemap_mode and anything else: unchanged
  }
  out.updated_at = now; out.updated_by = FORWARD_BY;
  const outText = JSON.stringify(out, null, 2);

  const byKind = {};
  for (const o of latest.objects) byKind[o.kind] = (byKind[o.kind] || 0) + 1;
  const summary = {window_scene_id: id, previous_model_signature: review.model_signature, new_model_signature: latest.model_signature,
    previous_review_sha256: sha256(reviewBytes), objects_before: review.objects.length, objects_after: out.objects.length,
    carried_objects: recovery.carried_objects, new_candidates: recovery.new_candidates, groups_before: review.groups.length,
    groups_after: out.groups.length, collisions: collisions.length, replaces, candidate_fields_differ: candidateDiffs,
    copy_metrics_would_change_on_editor_save: metricsWouldChange, candidates_by_kind: byKind, history_entries: Array.isArray(review.history) ? review.history.length : 0};
  await log(`${options.dryRun ? 'DRY-RUN' : 'PLAN'} ${id}: objects ${summary.objects_before} -> ${summary.objects_after} (copies ${summary.carried_objects} unchanged field by field, candidates ${summary.new_candidates}), groups ${summary.groups_before} -> ${summary.groups_after}, collisions ${collisions.length}, replaces ${replaces}, model ${review.model_signature.slice(0, 12)} -> ${latest.model_signature.slice(0, 12)}, metrics-would-change ${metricsWouldChange}, kinds ${json(byKind)}`);
  if (options.dryRun) return {...summary, outcome: 'dry_run'};

  // --- validate through the app on a qa_ scratch copy (the save path runs validateSceneReview) -----------------
  let scratch = null;
  if (options.scratch) {
    const scratchId = `qa_cf_${id.replace(/^(lot|win)-/, '')}_${randomBytes(3).toString('hex')}`;
    if (!SCENE_ID.test(scratchId) || !scratchId.startsWith('qa_')) throw new Refusal('scratch_id', scratchId);
    const scratchDir = path.join(ROOT, 'data/scenes', scratchId);
    try {
      await mkdir(path.join(scratchDir, 'models'), {recursive: true});
      await cp(path.join(dir, 'latest.json'), path.join(scratchDir, 'latest.json'));
      await cp(previousPath, path.join(scratchDir, 'models', path.basename(previousPath)));
      await cp(currentPath, path.join(scratchDir, 'models', path.basename(currentPath)));
      await writeFile(path.join(scratchDir, 'review.json'), reviewBytes);
      const response = await fetch(`${options.api}/api/scenes`, {method: 'POST', headers: {'Content-Type': 'application/json', ...(token ? {Authorization: `Bearer ${token}`} : {})},
        body: JSON.stringify({id: scratchId, review: out}), signal: AbortSignal.timeout(60000)});
      const body = await response.json().catch(() => ({}));
      scratch = {scratch_id: scratchId, status: response.status, error: body.error || null,
        saved_revision: body.review?.revision ?? null, saved_objects: body.review?.objects?.length ?? null, saved_model_signature: body.review?.model_signature?.slice(0, 12) ?? null};
      if (response.status !== 200) throw new Refusal('scratch_save_failed', `${id}: POST /api/scenes on ${scratchId} returned ${response.status} ${body.error || ''}`);
      if (body.review?.revision !== 2 || body.review?.model_signature !== latest.model_signature || body.review?.objects?.length !== out.objects.length)
        throw new Refusal('scratch_save_unexpected', `${id}: scratch save returned revision ${body.review?.revision}, ${body.review?.objects?.length} objects`);
      // What the app's save changed on the copies (expected: only model, the recomputed footprint metrics).
      const appCopies = new Map((body.review.objects || []).map(o => [o.id, o]));
      scratch.copy_keys_changed_by_app_save = [...new Set(review.objects.flatMap(o => diffKeys(o, appCopies.get(o.id))))].sort();
    } finally {
      await rm(scratchDir, {recursive: true, force: true});
      if (existsSync(scratchDir)) throw new Refusal('scratch_cleanup', `${scratchId} still exists`);
    }
    await log(`SCRATCH ${id}: ${scratchId} POST -> ${scratch.status}, app saved revision ${scratch.saved_revision}, ${scratch.saved_objects} objects, copy keys the app's save would touch: ${json(scratch.copy_keys_changed_by_app_save)}; scratch removed`);
  }

  // --- write the real review.json: re-check, lock, temp + rename ----------------------------------------------
  const again = await readFile(reviewPath);
  if (sha256(again) !== summary.previous_review_sha256) throw new Refusal('changed_underfoot', `${id} review.json changed since it was read; nothing written`);
  const check = JSON.parse(again.toString('utf8'));
  if (check.revision !== 1 || check.updated_by !== CARRY_BY) throw new Refusal('changed_underfoot', `${id} review.json is no longer the carry's revision 1; nothing written`);
  const lock = path.join(dir, 'review.lock');
  try { await mkdir(lock); } catch (e) {
    if (e.code === 'EEXIST') throw new Refusal('review_locked', `${id} is saving in the app (review.lock present); retry in a moment, nothing written`);
    throw e;
  }
  const temp = path.join(dir, 'review.carry-forward.tmp');
  let written;
  try {
    await writeFile(temp, outText);
    await rename(temp, reviewPath);
    written = await readFile(reviewPath);
  } finally {
    await rm(temp, {force: true});
    await rmdir(lock);
  }
  const back = JSON.parse(written.toString('utf8'));
  if (back.revision !== 1 || back.updated_by !== FORWARD_BY || back.model_signature !== latest.model_signature || back.objects.length !== out.objects.length)
    throw new Error(`${id}: the written review.json does not read back as expected`);
  summary.new_review_sha256 = sha256(written);
  summary.at = now;

  const row = {version: REGISTRY_VERSION, role: ROLE, window_scene_id: id, previous_model_signature: summary.previous_model_signature,
    new_model_signature: summary.new_model_signature, previous_review_sha256: summary.previous_review_sha256, new_review_sha256: summary.new_review_sha256,
    objects_before: summary.objects_before, objects_after: summary.objects_after, carried_objects: summary.carried_objects, new_candidates: summary.new_candidates, at: now};
  await mkdir(path.dirname(options.registry), {recursive: true});
  await appendFile(options.registry, sortedRow(row) + '\n');
  await log(`WROTE ${id}: review.json ${summary.previous_review_sha256.slice(0, 12)} -> ${summary.new_review_sha256.slice(0, 12)}, revision 1, updated_by '${FORWARD_BY}', ${summary.objects_after} objects; registry row ${ROLE} appended to ${path.relative(ROOT, options.registry)}`);
  return {...summary, outcome: 'forwarded', scratch};
}

// --- run -----------------------------------------------------------------------------------------------------------
await log(`# window review carry-forward ${options.dryRun ? 'DRY RUN' : 'LIVE'} on ${ids.length} window(s); api ${options.api}; scratch ${options.scratch}; registry ${path.relative(ROOT, options.registry)}`);
const results = [];
let failed = 0;
try {
  for (const id of ids) {
    try {
      results.push(await forward(id));
    } catch (e) {
      failed++;
      const reason = e instanceof Refusal ? e.reason : 'error';
      results.push({window_scene_id: id, outcome: 'refused', reason, detail: e.message});
      await log(`REFUSED ${id}: ${e.message}`);
      if (!options.continueOnError) break;
    }
  }
} finally {
  await rm(transpileDir, {recursive: true, force: true});
}
const totals = {
  windows: ids.length,
  forwarded: results.filter(r => r.outcome === 'forwarded').length,
  dry_run: results.filter(r => r.outcome === 'dry_run').length,
  refused: results.filter(r => r.outcome === 'refused').length,
  not_reached: ids.length - results.length,
  objects_before: results.reduce((n, r) => n + (r.objects_before || 0), 0),
  objects_after: results.reduce((n, r) => n + (r.objects_after || 0), 0),
  carried_objects: results.reduce((n, r) => n + (r.carried_objects || 0), 0),
  new_candidates: results.reduce((n, r) => n + (r.new_candidates || 0), 0),
  scratch_200: results.filter(r => r.scratch?.status === 200).length,
};
await log(`# done: ${json(totals)}`);
console.log(JSON.stringify({options: {...options, tokenFile: options.tokenFile ? '<set>' : null}, totals, results}, null, 2));
process.exit(failed && !options.continueOnError ? 1 : 0);
