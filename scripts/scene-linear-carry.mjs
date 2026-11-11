#!/usr/bin/env node
// The review carry of a linear-merge promotion (scripts/ml/scene_linear_promote.py step 2), with the editor's own recovery
// (src/lib/scene-feedback.ts recoverSceneReview) loaded through ts.transpileModule exactly as scripts/window-review-carry-forward.mjs
// does. Nothing under data/ is written here: the merged review goes to --out and a stats line to stdout; the Python side
// validates it through the app, writes it under the review lock and keeps the byte copies.
//
//   node scripts/scene-linear-carry.mjs --previous models/<old>.json --current <new latest.json> --saved review.json --out <file>
//        [--updated-by NAME] [--restore-verbatim]
//
// previous = the snapshot the saved review names, current = the promoted snapshot (members dropped, features added with
// `replaces`), saved = review.json. recoverSceneReview is called with {preserveUnchanged: true}: the same-frame branch of the
// recovery (scene-feedback.ts:117-159) returns every carried non-rejected decision as needs_adjustment unless that option keeps a
// confirmed + geometry_reviewed object whose outline the new model re-proposes under the same id (:157). A linear merge is not a
// re-extraction - every non-member object is the same evidence under the same id - so the carry restores verbatim and reports (with
// --restore-verbatim, the Python side's default: the takeover detection of every takeover window carries replaces / derived_from in
// its review copy and the recovery would demote it) or refuses any reviewer-touched non-member object the recovery would still change. Members follow the
// editor's replaced-seed rule (:95-110, :136-148): an untouched candidate member simply leaves the review with the model layer; a
// reviewer-touched member's words move onto the feature, which returns as needs_adjustment (or rejected when every touched member
// was rejected); a machine-only label is dropped. The review layer then keeps every member as status superseded with
// replacement_ids = [feature] (the whole-roof takeover convention and the merge function's own member form): the app's
// lineageIssue (src/lib/scene-instances.ts:37-59) requires a feature's derived_from to name existing objects whose
// replacement_ids point back, so a review that simply dropped the members could never be saved. That derived_from is the REVIEW
// layer's: the model layer records replaces + merged_from only (the canopy precedent, merge_canopy_fragments), so the review's copy
// of each feature is given derived_from = replaces here, and a model-layer feature carrying derived_from is a precondition failure.
import {createHash} from 'node:crypto';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import ts from 'typescript';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DECIDED = new Set(['confirmed', 'needs_adjustment', 'rejected', 'superseded']);

const args = process.argv.slice(2);
const options = {previous: null, current: null, saved: null, out: null, updatedBy: 'scripts.ml.scene_linear_promote', restoreVerbatim: false};
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === '--previous') options.previous = args[++i];
  else if (a === '--current') options.current = args[++i];
  else if (a === '--saved') options.saved = args[++i];
  else if (a === '--out') options.out = args[++i];
  else if (a === '--updated-by') options.updatedBy = args[++i];
  else if (a === '--restore-verbatim') options.restoreVerbatim = true;
  else fail('usage', [`Unknown option ${a}`]);
}
for (const key of ['previous', 'current', 'saved', 'out']) if (!options[key]) fail('usage', [`--${key} is required`]);

function fail(reason, problems) {
  console.log(JSON.stringify({ok: false, reason, problems}));
  process.exit(1);
}
const json = value => JSON.stringify(value);
const sortedJson = value => JSON.stringify(value, (_k, v) => v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.keys(v).sort().map(k => [k, v[k]])) : v);
const readJson = async file => JSON.parse(await readFile(file, 'utf8'));
const stamp = () => new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
function diffKeys(a, b) {
  const keys = new Set([...Object.keys(a || {}), ...Object.keys(b || {})]);
  return [...keys].filter(k => sortedJson(a?.[k] ?? null) !== sortedJson(b?.[k] ?? null)).sort();
}

// --- the editor's modules (scene-feedback's private feedback() is re-exported so "touched" means exactly what the editor means)
const transpileDir = await mkdtemp(path.join(tmpdir(), 'addr-scene-linear-carry-'));
let recoverSceneReview, feedback;
try {
  for (const name of ['scene-feedback', 'scene-structure', 'scene-frame-transform']) {
    let code = ts.transpileModule(await readFile(path.join(ROOT, `src/lib/${name}.ts`), 'utf8'),
      {compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022}}).outputText
      .replaceAll(/from "\.\/([a-z-]+)"/g, 'from "./$1.mjs"');
    if (name === 'scene-feedback') code += '\nexport { feedback as __feedback };\n';
    await writeFile(path.join(transpileDir, `${name}.mjs`), code);
  }
  ({recoverSceneReview, __feedback: feedback} = await import(pathToFileURL(path.join(transpileDir, 'scene-feedback.mjs'))));
} finally {
  await rm(transpileDir, {recursive: true, force: true});
}

const previous = await readJson(options.previous), current = await readJson(options.current), saved = await readJson(options.saved);
const previousById = new Map(previous.objects.map(o => [o.id, o]));
const currentById = new Map(current.objects.map(o => [o.id, o]));
const members = previous.objects.filter(o => !currentById.has(o.id)).map(o => o.id);
const memberSet = new Set(members);
const features = current.objects.filter(o => !previousById.has(o.id));
const replacedBy = new Map();
for (const f of features) for (const m of f.replaces || []) replacedBy.set(m, f.id);

// --- preconditions ---------------------------------------------------------------------------------------------------
const problems = [];
if (saved.model_signature !== previous.model_signature) problems.push(`saved review names ${String(saved.model_signature).slice(0, 12)}, not the previous snapshot ${String(previous.model_signature).slice(0, 12)}`);
if (previous.id !== current.id) problems.push(`snapshot ids differ: ${previous.id} / ${current.id}`);
for (const m of members) if (!replacedBy.has(m)) problems.push(`member ${m} is not in any feature's replaces`);
for (const f of features) for (const m of f.replaces || []) if (!memberSet.has(m)) problems.push(`feature ${f.id} replaces ${m}, which is still in the current snapshot or never was in the previous one`);
for (const f of features) if (f.derived_from !== undefined) problems.push(`feature ${f.id} carries derived_from in the model layer; the model layer records replaces + merged_from only (derived_from is the review layer's)`);
for (const o of previous.objects) if (currentById.has(o.id) && json(o) !== json(currentById.get(o.id))) problems.push(`model object ${o.id} changed between the snapshots`);
if ((saved.reviewed_classes || []).length) problems.push(`reviewed_classes is set (${json(saved.reviewed_classes)}); the recovery resets it to [] and this carry does not decide that`);
if (!Array.isArray(saved.objects) || !Array.isArray(saved.groups)) problems.push('saved review has no objects/groups arrays');
if (problems.length) fail('precondition_failed', problems);

// --- the editor's own recovery ---------------------------------------------------------------------------------------
const recovery = recoverSceneReview(previous, current, saved, {preserveUnchanged: true});
if (!recovery) fail('recovery_null', ['recoverSceneReview returned null (frame, image or signature mismatch)']);
const merged = recovery.review;
const mergedById = new Map(merged.objects.map(o => [o.id, o]));
const touched = o => !previousById.has(o.id) || feedback(o) !== feedback(previousById.get(o.id));
const machineOnly = o => typeof o.provenance?.source === 'string' && o.provenance.source.startsWith('machine:') && !o.provenance.human_confirmed;

const classes = {member_untouched: [], member_inherited: {}, member_machine_dropped: [], member_decided: [], kept_identical: 0, kept_canonical: 0, changed: [], restored: []};
for (const o of saved.objects) {
  const out = mergedById.get(o.id);
  if (memberSet.has(o.id)) {
    if (DECIDED.has(o.status) || o.geometry_reviewed) classes.member_decided.push(o.id);
    if (out) problems.push(`member ${o.id} is still in the carried review (status ${out.status})`);
    if (!touched(o)) classes.member_untouched.push(o.id);
    else if (o.status === 'superseded') problems.push(`member ${o.id} is superseded in the review; the recovery keeps it as a ghost`);
    else if (machineOnly(o)) classes.member_machine_dropped.push(o.id);
    else (classes.member_inherited[replacedBy.get(o.id)] ||= []).push(o.id);
    continue;
  }
  if (!out) { classes.changed.push({id: o.id, status: o.status, diff: ['<missing>']}); continue; }
  if (json(o) === json(out)) { classes.kept_identical++; continue; }
  if (sortedJson(o) === sortedJson(out)) { classes.kept_canonical++; continue; }   // same values, the model's key order
  classes.changed.push({id: o.id, status: o.status, geometry_reviewed: !!o.geometry_reviewed, touched: touched(o), after_status: out.status, diff: diffKeys(o, out)});
}
if (classes.member_decided.length) problems.push(`decided member(s) merged: ${classes.member_decided.join(',')}; the merge view must never join a decided piece`);
if (classes.changed.length) {
  if (options.restoreVerbatim) {
    for (const change of classes.changed) {
      const original = saved.objects.find(o => o.id === change.id);
      const index = merged.objects.findIndex(o => o.id === change.id);
      if (index >= 0) merged.objects[index] = original; else merged.objects.push(original);
      classes.restored.push(change.id);
    }
    classes.changed = [];
  } else {
    problems.push(...classes.changed.map(c => `reviewer-touched object ${c.id} (${c.status}) would come back as ${c.after_status ?? 'missing'}; keys ${c.diff.join(',')} (scene-feedback.ts:157-159 demotes a carried decision; pass --restore-verbatim, the Python default, to restore it verbatim)`));
  }
}
// the review layer keeps the members: superseded, pointing at their feature (inserted before the features, in saved order)
const superseded = saved.objects.filter(o => memberSet.has(o.id)).map(o => ({...o, status: 'superseded', geometry_reviewed: false, replacement_ids: [replacedBy.get(o.id)]}));
const firstFeature = merged.objects.findIndex(o => replacedBy.size && features.some(f => f.id === o.id));
merged.objects.splice(firstFeature < 0 ? merged.objects.length : firstFeature, 0, ...superseded);
for (const o of superseded) mergedById.set(o.id, o);
// the review layer's copy of each feature: derived_from = replaces, inserted after replaces (both ends of the pair lineageIssue checks)
const reviewCopy = f => {
  const copy = {};
  for (const [k, v] of Object.entries(f)) { if (k === 'derived_from') continue; copy[k] = v; if (k === 'replaces') copy.derived_from = [...(f.replaces || [])]; }
  if (!('derived_from' in copy)) copy.derived_from = [...(f.replaces || [])];
  return copy;
};
for (const f of features) {
  const index = merged.objects.findIndex(o => o.id === f.id);
  if (index < 0) continue;
  const copy = reviewCopy(merged.objects[index]);
  merged.objects[index] = copy; mergedById.set(f.id, copy);
}
const featureRows = [];
for (const f of features) {
  const out = mergedById.get(f.id);
  if (!out) { problems.push(`feature ${f.id} missing from the carried review`); continue; }
  const inherited = classes.member_inherited[f.id] || [];
  if (!inherited.length && sortedJson(out) !== sortedJson(reviewCopy(f))) problems.push(`feature ${f.id} differs from the model's dict (plus the review's derived_from) without an inherited decision: ${diffKeys(reviewCopy(f), out).join(',')}`);
  if (sortedJson(out.derived_from) !== sortedJson(f.replaces || [])) problems.push(`feature ${f.id} derived_from in the review is not its replaces`);
  if (inherited.length && !['needs_adjustment', 'rejected'].includes(out.status)) problems.push(`feature ${f.id} inherited ${inherited.join(',')} but reads ${out.status}`);
  featureRows.push({id: f.id, kind: f.kind, replaces: f.replaces || [], status_in_model: f.status, status_in_review: out.status, inherited_from: inherited, notes: out.notes || null});
}
if (merged.model_signature !== current.model_signature) problems.push('merged model_signature is not the current snapshot\'s');
if (merged.revision !== saved.revision) problems.push(`recovery revision ${merged.revision} != saved ${saved.revision}`);
const mergedGroups = new Map(merged.groups.map(g => [g.id, g]));
for (const g of saved.groups) if (sortedJson(mergedGroups.get(g.id) ?? null) !== sortedJson(g)) problems.push(`saved group ${g.id} missing or changed`);
const extraGroups = merged.groups.filter(g => !saved.groups.some(s => s.id === g.id)).map(g => g.id);
const expected = saved.objects.length + features.length;   // members stay (superseded), features join
if (merged.objects.length !== expected) problems.push(`merged objects ${merged.objects.length} != expected ${expected}`);
const ids = new Set(); for (const o of merged.objects) { if (ids.has(o.id)) problems.push(`duplicate id ${o.id} in the carried review`); ids.add(o.id); }
if (problems.length) fail('assertion_failed', problems);

// --- the review to write: saved key order; revision + 1; the app's history convention -------------------------------
const now = stamp();
const {history = [], ...savedWithoutHistory} = saved;
const out = {};
for (const key of Object.keys(saved)) {
  if (key === 'revision') out.revision = saved.revision + 1;
  else if (key === 'model_signature') out.model_signature = current.model_signature;
  else if (key === 'objects') out.objects = merged.objects;
  else if (key === 'groups') out.groups = merged.groups;
  else if (key === 'reviewed_classes') out.reviewed_classes = merged.reviewed_classes;
  else if (key === 'updated_at') out.updated_at = now;
  else if (key === 'updated_by') out.updated_by = options.updatedBy;
  else if (key === 'history') out.history = [...history, savedWithoutHistory];
  else out[key] = saved[key];
}
out.updated_at = now; out.updated_by = options.updatedBy;
if (!('history' in out)) out.history = [...history, savedWithoutHistory];
const text = JSON.stringify(out, null, 2);
await writeFile(options.out, text);
console.log(JSON.stringify({ok: true, out: options.out, out_sha256: createHash('sha256').update(text).digest('hex'), at: now,
  members, features: featureRows, members_untouched: classes.member_untouched, members_inherited: classes.member_inherited,
  members_superseded_in_review: superseded.map(o => o.id),
  members_machine_dropped: classes.member_machine_dropped, kept_identical: classes.kept_identical, kept_canonical_order_only: classes.kept_canonical,
  restored_verbatim: classes.restored, carried_objects: recovery.carried_objects, new_candidates: recovery.new_candidates,
  objects_before: saved.objects.length, objects_after: out.objects.length, groups_before: saved.groups.length, groups_after: out.groups.length,
  extra_groups_from_model: extraGroups, revision_before: saved.revision, revision_after: out.revision, history_entries: out.history.length,
  updated_by: out.updated_by, preserve_unchanged: true, restore_verbatim: options.restoreVerbatim,
  review_layer_derived_from: features.map(f => f.id), model_layer_lineage: ['replaces', 'merged_from']}));
