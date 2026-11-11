#!/usr/bin/env node
// The review carry of a road-band promotion (scripts/ml/scene_road_promote.py step 2): the sibling of scripts/scene-linear-carry.mjs,
// with the editor's own recovery (src/lib/scene-feedback.ts recoverSceneReview, {preserveUnchanged: true}) loaded through
// ts.transpileModule exactly as that script does. Nothing under data/ is written here: the carried review goes to --out and a stats
// line to stdout; the Python side validates it through the app, writes it under the review lock and keeps the byte copies.
//
//   node scripts/scene-road-carry.mjs --previous models/<old>.json --current <new latest.json> --saved review.json --out <file>
//        --bands <bands.json> [--updated-by NAME] [--restore-verbatim]
//
// Why not scene-linear-carry.mjs as is: a road band differs from a linear join in three ways that script refuses by design.
//   1. A band may supersede a reviewer-decided piece (the confirmed seed e31f2cb143c1b8dc, the needs_adjustment retire target
//      d5875542ec9e47a2) - but ONLY when the band carries the reviewer's accept (--bands: accepted true). Without it the carry refuses
//      (needs_reviewer_accept); the Python side refuses the band before it gets here.
//   2. One member may be replaced by several features: a junction band takes the longest straight arm and the seed's other arms
//      stay as they are (road_corridor 'remainder_on_accept'), so the seed is superseded -> [band, remainder pieces].
//   3. An accepted band's review copy is status confirmed + geometry_reviewed true with provenance.human_decision (the accepted-
//      import rule: the reviewer's own accept is the decision), not the model's candidate dict. A band without an accept is carried
//      exactly as the linear join carries a feature (the editor's inheritance rule decides its status).
// Everything else is the linear carry: every reviewer-touched non-member object byte-identical (restored verbatim with
// --restore-verbatim, the Python default, else refused), members kept in the review as superseded with replacement_ids, the
// review's copy of each feature given derived_from = replaces (the lineage pair src/lib/scene-instances.ts lineageIssue requires),
// the model layer recording `replaces` only. A retire target (bands.json 'retires') is superseded -> band with the reason appended to
// its notes.
//
// bands.json: {"bands": [{"id", "accepted": bool, "members": [ids], "remainder_ids": [ids], "retires": [ids], "retire_note": str,
//              "provenance": {...} | null}]}
import {createHash} from 'node:crypto';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import ts from 'typescript';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DECIDED = new Set(['confirmed', 'needs_adjustment', 'rejected', 'superseded']);

const args = process.argv.slice(2);
const options = {previous: null, current: null, saved: null, out: null, bands: null, updatedBy: 'scripts.ml.scene_road_promote', restoreVerbatim: false};
for (let i = 0; i < args.length; i++) {
  const a = args[i];
  if (a === '--previous') options.previous = args[++i];
  else if (a === '--current') options.current = args[++i];
  else if (a === '--saved') options.saved = args[++i];
  else if (a === '--out') options.out = args[++i];
  else if (a === '--bands') options.bands = args[++i];
  else if (a === '--updated-by') options.updatedBy = args[++i];
  else if (a === '--restore-verbatim') options.restoreVerbatim = true;
  else fail('usage', [`Unknown option ${a}`]);
}
for (const key of ['previous', 'current', 'saved', 'out', 'bands']) if (!options[key]) fail('usage', [`--${key} is required`]);

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
const transpileDir = await mkdtemp(path.join(tmpdir(), 'addr-scene-road-carry-'));
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
const bandSpec = (await readJson(options.bands)).bands || [];
const previousById = new Map(previous.objects.map(o => [o.id, o]));
const currentById = new Map(current.objects.map(o => [o.id, o]));
const members = previous.objects.filter(o => !currentById.has(o.id)).map(o => o.id);
const memberSet = new Set(members);
const features = current.objects.filter(o => !previousById.has(o.id));
const featureIds = new Set(features.map(f => f.id));
const bandById = new Map(bandSpec.map(b => [b.id, b]));
const bandOfFeature = new Map();          // feature id -> band spec (the band itself or the band whose remainder it is)
for (const b of bandSpec) { bandOfFeature.set(b.id, b); for (const r of b.remainder_ids || []) bandOfFeature.set(r, b); }
// member -> every feature replacing it, the band first, then its remainder pieces in the order the band lists them
const replacedBy = new Map();
const featureOrder = id => { const b = bandOfFeature.get(id); return !b ? 1e9 : id === b.id ? 0 : 1 + (b.remainder_ids || []).indexOf(id); };
for (const f of [...features].sort((a, b) => featureOrder(a.id) - featureOrder(b.id) || (a.id < b.id ? -1 : 1)))
  for (const m of f.replaces || []) replacedBy.set(m, [...(replacedBy.get(m) || []), f.id]);
const retireOf = new Map();
for (const b of bandSpec) for (const r of b.retires || []) retireOf.set(r, b);

// --- preconditions ---------------------------------------------------------------------------------------------------
const problems = [];
if (saved.model_signature !== previous.model_signature) problems.push(`saved review names ${String(saved.model_signature).slice(0, 12)}, not the previous snapshot ${String(previous.model_signature).slice(0, 12)}`);
if (previous.id !== current.id) problems.push(`snapshot ids differ: ${previous.id} / ${current.id}`);
for (const m of members) if (!replacedBy.has(m)) problems.push(`member ${m} is not in any feature's replaces`);
for (const f of features) for (const m of f.replaces || []) if (!memberSet.has(m)) problems.push(`feature ${f.id} replaces ${m}, which is still in the current snapshot or never was in the previous one`);
for (const f of features) if (f.derived_from !== undefined) problems.push(`feature ${f.id} carries derived_from in the model layer; the model layer records replaces only (derived_from is the review layer's)`);
for (const f of features) if (!bandOfFeature.has(f.id)) problems.push(`feature ${f.id} is neither a band nor a remainder piece named in --bands`);
for (const b of bandSpec) {
  if (!featureIds.has(b.id)) problems.push(`band ${b.id} is not a feature of the current snapshot`);
  for (const r of b.remainder_ids || []) if (!featureIds.has(r)) problems.push(`remainder ${r} of band ${b.id} is not a feature of the current snapshot`);
  const f = currentById.get(b.id);
  if (f && sortedJson([...(f.replaces || [])].sort()) !== sortedJson([...(b.members || [])].sort())) problems.push(`band ${b.id} replaces ${json(f.replaces)} but --bands lists members ${json(b.members)}`);
  for (const r of b.retires || []) if (!(b.members || []).includes(r)) problems.push(`retire target ${r} of band ${b.id} is not one of its members`);
  if (b.accepted && !(b.provenance && b.provenance.human_decision && b.provenance.human_decision.source === 'human:chat')) problems.push(`band ${b.id} is accepted without provenance.human_decision source human:chat`);
}
for (const o of previous.objects) if (currentById.has(o.id) && json(o) !== json(currentById.get(o.id))) problems.push(`model object ${o.id} changed between the snapshots`);
if ((saved.reviewed_classes || []).length) problems.push(`reviewed_classes is set (${json(saved.reviewed_classes)}); the recovery resets it to [] and this carry does not decide that`);
if (!Array.isArray(saved.objects) || !Array.isArray(saved.groups)) problems.push('saved review has no objects/groups arrays');
const savedById = new Map((saved.objects || []).map(o => [o.id, o]));
// "reviewer-touched" = the editor's feedback() without the lineage pair: a linear-merge feature's review copy carries derived_from
// (the 3 Oct promotion), which is machine lineage, not a reviewer edit
const withoutLineage = o => ({...o, derived_from: undefined, replacement_ids: undefined});
const reviewerTouched = o => !previousById.has(o.id) || feedback(withoutLineage(o)) !== feedback(withoutLineage(previousById.get(o.id)));
for (const m of members) {
  const o = savedById.get(m);
  if (!o) { problems.push(`member ${m} is not in the saved review`); continue; }
  const band = bandOfFeature.get((replacedBy.get(m) || [])[0]);
  const decided = DECIDED.has(o.status) || o.geometry_reviewed;
  if (o.status === 'superseded' || o.status === 'rejected') problems.push(`member ${m} is ${o.status} in the review; a band never supersedes a rejected or superseded piece`);
  else if ((decided || retireOf.has(m) || reviewerTouched(o)) && !(band && band.accepted)) problems.push(`needs_reviewer_accept: member ${m} (${o.status}) is reviewer-touched; band ${band?.id} has no accept`);
}
if (problems.length) fail(problems.some(p => p.startsWith('needs_reviewer_accept')) ? 'needs_reviewer_accept' : 'precondition_failed', problems);

// --- the editor's own recovery ---------------------------------------------------------------------------------------
const recovery = recoverSceneReview(previous, current, saved, {preserveUnchanged: true});
if (!recovery) fail('recovery_null', ['recoverSceneReview returned null (frame, image or signature mismatch)']);
const merged = recovery.review;
const mergedById = new Map(merged.objects.map(o => [o.id, o]));
const touched = o => !previousById.has(o.id) || feedback(o) !== feedback(previousById.get(o.id));
const machineOnly = o => typeof o.provenance?.source === 'string' && o.provenance.source.startsWith('machine:') && !o.provenance.human_confirmed;

const classes = {member_untouched: [], member_lineage_only: [], member_inherited: {}, member_machine_dropped: [], member_decided_accepted: [], kept_identical: 0, kept_canonical: 0, changed: [], restored: []};
for (const o of saved.objects) {
  const out = mergedById.get(o.id);
  if (memberSet.has(o.id)) {
    if (DECIDED.has(o.status) || o.geometry_reviewed) classes.member_decided_accepted.push(o.id);
    if (out) problems.push(`member ${o.id} is still in the carried review (status ${out.status})`);
    if (!touched(o)) classes.member_untouched.push(o.id);
    else if (!reviewerTouched(o)) classes.member_lineage_only.push(o.id);
    else if (machineOnly(o)) classes.member_machine_dropped.push(o.id);
    else {
      // the recovery moves the words of a touched member onto ONE feature replacing it (the last in current.objects order)
      const onto = current.objects.filter(f => (f.replaces || []).includes(o.id)).map(f => f.id).at(-1);
      (classes.member_inherited[onto] ||= []).push(o.id);
    }
    continue;
  }
  if (!out) { classes.changed.push({id: o.id, status: o.status, diff: ['<missing>']}); continue; }
  if (json(o) === json(out)) { classes.kept_identical++; continue; }
  if (sortedJson(o) === sortedJson(out)) { classes.kept_canonical++; continue; }   // same values, the model's key order
  classes.changed.push({id: o.id, status: o.status, geometry_reviewed: !!o.geometry_reviewed, touched: touched(o), after_status: out.status, diff: diffKeys(o, out)});
}
if (classes.changed.length) {
  if (options.restoreVerbatim) {
    for (const change of classes.changed) {
      const original = savedById.get(change.id);
      const index = merged.objects.findIndex(o => o.id === change.id);
      if (index >= 0) merged.objects[index] = original; else merged.objects.push(original);
      classes.restored.push(change.id);
    }
    classes.changed = [];
  } else {
    problems.push(...classes.changed.map(c => `reviewer-touched object ${c.id} (${c.status}) would come back as ${c.after_status ?? 'missing'}; keys ${c.diff.join(',')} (scene-feedback.ts:157-159 demotes a carried decision; pass --restore-verbatim, the Python default, to restore it verbatim)`));
  }
}
// a reviewer's words reach only an ACCEPTED band (whose own accept is the decision; the words stay on the superseded member): a band
// without an accept has no reviewer-touched member (refused above), so the recovery's inheritance of a lineage-only member (the
// editor's feedback() counts derived_from) is machine lineage and the band stays the model's candidate. The recovery picks the LAST
// feature replacing the member (the snapshot sorts features by id), so with remainder pieces (junction arms, the coverage guard's
// pieces) it may pick a remainder of the accepted band: allowed, because every remainder is overwritten below with the model's
// candidate copy (checked: "differs from the model's dict"), so the words reach no feature either way
for (const [fid, ids] of Object.entries(classes.member_inherited)) {
  const band = bandOfFeature.get(fid);
  if (!band || !band.accepted) problems.push(`reviewer-touched member(s) ${ids.join(',')} would pass their words to ${fid}, which is not an accepted band or one of its remainder pieces`);
}
// the review layer keeps the members: superseded, pointing at every feature replacing them (inserted before the features, in saved order);
// a retire target also carries the reason in its notes
const retireNote = (o, band) => [o.notes, band.retire_note].filter(Boolean).join('\n').slice(0, 5000);
const superseded = saved.objects.filter(o => memberSet.has(o.id)).map(o => {
  const band = retireOf.get(o.id);
  return {...o, status: 'superseded', geometry_reviewed: false, replacement_ids: replacedBy.get(o.id), ...(band ? {notes: retireNote(o, band)} : {})};
});
const firstFeature = merged.objects.findIndex(o => featureIds.has(o.id));
merged.objects.splice(firstFeature < 0 ? merged.objects.length : firstFeature, 0, ...superseded);
for (const o of superseded) mergedById.set(o.id, o);
// the review layer's copy of each feature: derived_from = replaces, inserted after replaces (both ends of the pair lineageIssue checks)
const reviewCopy = f => {
  const copy = {};
  for (const [k, v] of Object.entries(f)) { if (k === 'derived_from') continue; copy[k] = v; if (k === 'replaces') copy.derived_from = [...(f.replaces || [])]; }
  if (!('derived_from' in copy)) copy.derived_from = [...(f.replaces || [])];
  return copy;
};
const accepted = f => ({...reviewCopy(f), status: 'confirmed', geometry_reviewed: true, provenance: bandById.get(f.id).provenance});
for (const f of features) {
  const index = merged.objects.findIndex(o => o.id === f.id);
  if (index < 0) continue;
  const band = bandById.get(f.id);
  const copy = band?.accepted ? accepted(f) : reviewCopy(f);
  merged.objects[index] = copy; mergedById.set(f.id, copy);
}
const featureRows = [];
for (const f of features) {
  const out = mergedById.get(f.id);
  if (!out) { problems.push(`feature ${f.id} missing from the carried review`); continue; }
  const band = bandById.get(f.id);
  const inherited = classes.member_inherited[f.id] || [];
  if (band?.accepted) {
    if (sortedJson(out) !== sortedJson(accepted(f))) problems.push(`accepted band ${f.id} differs from the model's dict plus the accept: ${diffKeys(accepted(f), out).join(',')}`);
    if (out.status !== 'confirmed' || out.geometry_reviewed !== true) problems.push(`accepted band ${f.id} reads ${out.status}`);
  } else {
    if (sortedJson(out) !== sortedJson(reviewCopy(f))) problems.push(`feature ${f.id} differs from the model's dict (plus the review's derived_from): ${diffKeys(reviewCopy(f), out).join(',')}`);
    if (out.status !== 'candidate') problems.push(`feature ${f.id} without an accept reads ${out.status}`);
  }
  if (sortedJson(out.derived_from) !== sortedJson(f.replaces || [])) problems.push(`feature ${f.id} derived_from in the review is not its replaces`);
  featureRows.push({id: f.id, kind: f.kind, role: band ? 'band' : 'remainder', band: bandOfFeature.get(f.id)?.id, accepted: !!band?.accepted, replaces: f.replaces || [],
    status_in_model: f.status, status_in_review: out.status, geometry_reviewed: out.geometry_reviewed, inherited_from: inherited, notes: out.notes || null});
}
for (const o of superseded) for (const r of o.replacement_ids) if (!(mergedById.get(r)?.derived_from || []).includes(o.id)) problems.push(`superseded ${o.id} -> ${r} has no derived_from back`);
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
  members, features: featureRows, members_untouched: classes.member_untouched, members_lineage_only: classes.member_lineage_only, members_inherited: classes.member_inherited,
  members_decided_accepted: classes.member_decided_accepted, members_superseded_in_review: superseded.map(o => o.id),
  retired: superseded.filter(o => retireOf.has(o.id)).map(o => o.id),
  members_machine_dropped: classes.member_machine_dropped, kept_identical: classes.kept_identical, kept_canonical_order_only: classes.kept_canonical,
  restored_verbatim: classes.restored, carried_objects: recovery.carried_objects, new_candidates: recovery.new_candidates,
  objects_before: saved.objects.length, objects_after: out.objects.length, groups_before: saved.groups.length, groups_after: out.groups.length,
  extra_groups_from_model: extraGroups, revision_before: saved.revision, revision_after: out.revision, history_entries: out.history.length,
  updated_by: out.updated_by, preserve_unchanged: true, restore_verbatim: options.restoreVerbatim,
  review_layer_derived_from: features.map(f => f.id), model_layer_lineage: ['replaces']}));
