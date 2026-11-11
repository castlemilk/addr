// Record reviewer decisions given in chat into the scene review files through the same API the review UI uses.
// Usage: node scripts/record-chat-feedback.mjs <decisions.json> [--apply]   (dry run without --apply)
// Kind-only decisions keep the current status; a decision with status 'confirmed' marks the outline reviewed unless it carries class_only: true (a word about the class only never marks the outline reviewed), and 'candidate' clears the mark as before.
// The app saves 'confirmed' only on a reviewed outline (validateSceneReview), so --apply of a class-only confirmation on an unreviewed outline fails that scene's save: the reviewer confirms that object in the editor; never drop class_only to get past it.
// `sentence` (optional; required with preview_id): the reviewer's words, recorded in provenance.human_decision.sentence.
// preview_id (optional): the reviewer approved, in chat, an agent-drawn outline from a polygon preview card
// (data/reviews/eyes/polygon-preview/<id>/manifest.json, content.proposed_outline_pixels; ADDR_POLYGON_PREVIEW_DIR overrides the folder).
// The sealed preview is verified as scene_polygon_feedback._read_preview does (id = hash of its content, card hash), and refused as stale
// unless its binding (object id, kind, scene, saved revision, model/image identity and bound outline) matches the current saved object.
// Then ring (and holes) become the preview's, footprint metrics are recomputed as the editor does (building roof estimates cleared),
// status stays as the decision gives it (default: unchanged), geometry_reviewed becomes true, and provenance.human_decision records
// {source human:chat, sentence, at, fields [ring, ...], preview_id, drawn_by agent}: the reviewer approved an agent-drawn outline; they did not draw it.
// This is the human path: the agent apply path (scene_polygon_feedback.apply_polygon, roof-feedback-patch) still refuses reviewed outlines.
// A qa_<tag>_<scene> scratch copy stands for <scene> in the binding check.
// vegetation_type (optional): grass, hedge, shrub or tree stores that type on a tree or vegetation outline; null clears it. Validated as the
// app's save does (src/lib/scene-vegetation.ts vegetationTypeIssue: refused on any other kind). It never changes the label, status or outline,
// and is listed in provenance.human_decision.fields like every other decision key.
// source (optional, top level): human:chat (default) or human:fieldwork, for decisions the reviewer clicked in the Fieldwork review queue
// (scripts/ml/fieldwork_review.py pull). A human:fieldwork decision must carry fieldwork {itemId, revision, actor} (actor: who the
// Fieldwork change record says made the change); it is copied into provenance.human_decision.fieldwork. transcribed_by stays
// 'agent'. Any other source is refused.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { VEGETATION_TYPES, vegetationTypeIssue } from '../src/lib/scene-vegetation.ts';
const [file, ...flags] = process.argv.slice(2);
const apply = flags.includes('--apply');
const base = process.env.ADDR_WEB || 'http://localhost:3008';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const previewRoot = path.resolve(process.env.ADDR_POLYGON_PREVIEW_DIR || path.join(root, 'data/reviews/eyes/polygon-preview'));
const PREVIEW_VERSIONS = { 'roof-agent-polygon-preview-v1': 'building', 'object-agent-polygon-preview-v1': null };
const META = ['scene', 'object', 'sentence', 'preview_id', 'fieldwork'];
const SOURCES = ['human:chat', 'human:fieldwork'];
const outlineKey = x => JSON.stringify({ ring: x?.ring, holes: x?.holes || [] });
const payload = JSON.parse(fs.readFileSync(file, 'utf8'));
const source = payload.source ?? 'human:chat';
if (!SOURCES.includes(source)) { console.error(`source: use ${SOURCES.join(' or ')}, not ${JSON.stringify(source)}`); process.exit(2); }
const at = new Date().toISOString();
const byScene = new Map();
for (const d of payload.decisions) { if (!byScene.has(d.scene)) byScene.set(d.scene, []); byScene.get(d.scene).push(d); }
const out = { base, apply, at, source, scenes: [], failed: [] };
// The preview id is the SHA-256 of the content's Python json.dumps(sort_keys, compact) bytes; Python re-encodes it exactly (floats such as 1450.0 included).
const PY_HASH = "import hashlib,json,sys\nc=json.loads(sys.stdin.buffer.read())['content']\nassert isinstance(c,dict)\nprint(hashlib.sha256(json.dumps(c,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest())";
function readPreview(id) {
  if (typeof id !== 'string' || !/^[a-f0-9]{64}$/.test(id)) throw new Error('preview_id: use the 64-hex id the polygon preview tool returned');
  const dir = path.join(previewRoot, id), manifest = path.join(dir, 'manifest.json'), card = path.join(dir, 'card.png');
  if ([previewRoot, dir, manifest, card].some(p => fs.lstatSync(p, { throwIfNoEntry: false })?.isSymbolicLink())) throw new Error('use the original polygon preview artifacts, not links');
  if (!fs.existsSync(manifest) || !fs.existsSync(card)) throw new Error(`polygon preview ${id} not found`);
  const bytes = fs.readFileSync(manifest), saved = JSON.parse(bytes), content = saved.content;
  const python = [path.join(root, 'scripts/ml/.venv/bin/python'), 'python3'].find(p => p === 'python3' || fs.existsSync(p));
  const hashed = spawnSync(python, ['-c', PY_HASH], { input: bytes, encoding: 'utf8' });
  if (hashed.status !== 0 || hashed.stdout.trim() !== id || saved.preview_id !== id || !content || !(content.version in PREVIEW_VERSIONS)
      || JSON.stringify(saved.render) !== JSON.stringify(content.render) || saved.card_sha256 !== createHash('sha256').update(fs.readFileSync(card)).digest('hex'))
    throw new Error(`polygon preview ${id} artifacts were altered; request and inspect a fresh preview`);
  const p = content.proposed_outline_pixels;
  if (!p || !Array.isArray(p.ring) || p.ring.length < 3 || !Array.isArray(p.holes) || !content.binding) throw new Error(`polygon preview ${id} has no proposed outline`);
  return content;
}
// Why this preview cannot stand for the reviewer's approval of this object now (null: it can).
function previewIssue(content, d, data, o, fromSaved) {
  const b = content.binding, id = d.preview_id.slice(0, 12);
  if (b.object_id !== d.object) return `preview ${id} is bound to object ${b.object_id}, not ${d.object}`;
  const scratchOf = /^qa_[a-z0-9]+_(.+)$/.exec(d.scene)?.[1];
  if (b.scene_id !== d.scene && b.scene_id !== scratchOf) return `preview ${id} is bound to scene ${b.scene_id}, not ${d.scene}`;
  const kind = PREVIEW_VERSIONS[content.version] || b.object_kind;
  if (!kind || (content.object_kind !== undefined && content.object_kind !== kind) || o.kind !== kind) return `preview ${id} drew a ${kind} outline; the saved object is a ${o.kind}`;
  if (!fromSaved) return `stale preview ${id}: the scene has no current saved review (model changed or recovering); reload it and request a fresh preview`;
  if (b.model_signature !== data.model?.model_signature || b.image_sha256 !== data.model?.image_sha256) return `stale preview ${id}: the scene model or image changed`;
  if (b.revision !== (data.review?.revision || 0)) return `stale preview ${id}: drawn against saved revision ${b.revision}, the scene is at revision ${data.review?.revision || 0}`;
  if (outlineKey(b.outline_pixels) !== outlineKey(o)) return `stale preview ${id}: the saved outline changed after the preview was drawn`;
  if (o.kind === 'building' && o.roof_line_edits) return `${d.object} has roof-line edits that must be re-anchored to a new outline; approve this outline in the scene editor`;
  return null;
}
let footprintMetrics = null;
for (const [id, decisions] of byScene) {
  const data = await (await fetch(`${base}/api/scenes?id=${encodeURIComponent(id)}`)).json();
  // same draft the review page builds when a scene has no saved review yet (src/app/scenes/page.tsx)
  const review = data?.recovery?.review || (data?.review && !data.stale ? data.review : (data?.model ? {
    revision: data.review?.revision || 0, model_signature: data.model.model_signature, objects: data.model.objects,
    groups: data.model.groups || [], reviewed_classes: [] } : null));
  if (!review) { out.failed.push({ id, reason: 'no model payload' }); continue; }
  // a preview binds the saved review (or, with none saved, the model); a recovery draft or a stale review is neither
  const fromSaved = !data.recovery && !data.stale;
  const modelObjects = new Map((data.model?.objects || []).map(o => [o.id, o]));
  const changes = [], approved = [];
  for (const d of decisions) {
    let o = (review.objects || []).find(x => x.id === d.object), added = false;
    if (!o) {
      const m = modelObjects.get(d.object);
      if (!m) { out.failed.push({ id, object: d.object, reason: 'object not in review or model' }); continue; }
      o = { ...m, status: 'candidate', geometry_reviewed: false }; added = true;
    }
    if (source === 'human:fieldwork' && (typeof d.fieldwork?.itemId !== 'string' || !Number.isInteger(d.fieldwork?.revision) || typeof d.fieldwork?.actor !== 'string' || !d.fieldwork.actor)) { out.failed.push({ id, object: d.object, reason: 'fieldwork: a human:fieldwork decision needs its Fieldwork receipt {itemId, revision} and the change actor' }); continue; }
    if (d.sentence !== undefined && (typeof d.sentence !== 'string' || !d.sentence.trim() || d.sentence.length > 2000)) { out.failed.push({ id, object: d.object, reason: 'sentence: give the reviewer\'s words (at most 2,000 characters)' }); continue; }
    if (d.vegetation_type !== undefined) {
      // null (clear) is checked as the kind's right to a type at all; a value as the app's save checks it.
      const issue = vegetationTypeIssue({ kind: o.kind, vegetation_type: d.vegetation_type === null ? VEGETATION_TYPES[0] : d.vegetation_type });
      if (issue) { out.failed.push({ id, object: d.object, reason: `vegetation_type: ${issue}` }); continue; }
    }
    let preview = null;
    if (d.preview_id !== undefined) {
      try {
        if (!d.sentence) throw new Error('a preview approval needs the reviewer\'s words in sentence');
        if (d.class_only) throw new Error('class_only says the words were not about the outline; a preview approval is about the outline');
        if (['rejected', 'superseded'].includes(d.status)) throw new Error(`an approved outline cannot be recorded with status ${d.status}`);
        preview = readPreview(d.preview_id);
        const issue = previewIssue(preview, d, data, o, fromSaved);
        if (issue) throw new Error(issue);
      } catch (e) { out.failed.push({ id, object: d.object, preview_id: d.preview_id, reason: e.message }); continue; }
    }
    if (added) review.objects = [...(review.objects || []), o];
    const before = { status: o.status, structure_kind: o.structure_kind, vegetation_type: o.vegetation_type, geometry_reviewed: o.geometry_reviewed };
    const outlineBefore = { vertices: o.ring.length, holes: (o.holes || []).length, area_px: o.model?.footprint_area_px };
    if (d.structure_kind) o.structure_kind = d.structure_kind;
    if (d.vegetation_type === null) delete o.vegetation_type; else if (d.vegetation_type !== undefined) o.vegetation_type = d.vegetation_type;
    // class_only: the reviewer's chat word was about the class (roof or not), not the outline, so it never marks the outline reviewed.
    if (d.status) { o.status = d.status; if (d.status === 'confirmed' && !d.class_only) o.geometry_reviewed = true; if (d.status === 'candidate') o.geometry_reviewed = false; }
    if (d.label) o.label = d.label;
    else if (d.structure_kind && /^(Building|Shed|Garage|Carport|Patio or carport cover|Pergola or shade|Shipping container|Water tank|Pool cover|Fragment)$/.test(o.label || '')) {
      const names = { shed: 'Shed', garage: 'Garage', carport: 'Carport', 'attached-cover': 'Patio or carport cover', 'pergola-or-shade': 'Pergola or shade', 'shipping-container': 'Shipping container', 'water-tank': 'Water tank', 'pool-cover': 'Pool cover', fragment: 'Fragment' };
      o.label = names[d.structure_kind] || o.label;
    }
    o.notes = [o.notes, d.notes].filter(Boolean).join(' | ').slice(0, 5000);
    let outlineFields = [];
    if (preview) {
      // the editor's outline change (src/app/scenes/page.tsx changeObject): new ring, footprint metrics from it, building roof estimates cleared
      footprintMetrics ||= (await import(pathToFileURL(path.join(root, 'src/lib/scene-types.ts')).href)).footprintMetrics;
      const proposed = structuredClone(preview.proposed_outline_pixels);
      o.ring = proposed.ring;
      const holesChange = proposed.holes.length > 0 || (o.holes || []).length > 0;
      if (holesChange) o.holes = proposed.holes;
      o.model = { ...o.model, ...footprintMetrics(o.ring, data.model.pixel_to_meters, o.holes || []) };
      if (o.kind === 'building') { for (const k of ['roof_parts', 'roof_lines', 'roof_geometry_source', 'roof_geometry_note']) delete o[k]; o.roof_annotation = null; }
      o.geometry_reviewed = true; // the reviewer approved this outline, whatever status the words give
      outlineFields = ['ring', ...(holesChange ? ['holes'] : []), 'geometry_reviewed'];
      approved.push({ object: d.object, outline: proposed });
    }
    // fields: what the reviewer's words set; class_only is listed, and recorded as true, only when it applied.
    const fields = [...outlineFields, ...Object.keys(d).filter(k => !META.includes(k) && !outlineFields.includes(k) && (k !== 'class_only' || Boolean(d.class_only)))];
    o.provenance = { ...(o.provenance || {}), human_decision: { source, transcribed_by: 'agent', ...(d.sentence ? { sentence: d.sentence } : {}), at, fields,
      ...(source === 'human:fieldwork' ? { fieldwork: d.fieldwork } : {}),
      ...(d.class_only ? { class_only: true } : {}),
      ...(preview ? { preview_id: d.preview_id, drawn_by: 'agent', drawing_agent: preview.reviewer, preview_bound_revision: preview.binding.revision } : {}) } };
    changes.push({ object: d.object, before, after: { status: o.status, structure_kind: o.structure_kind, vegetation_type: o.vegetation_type, geometry_reviewed: o.geometry_reviewed },
      ...(preview ? { outline: { preview_id: d.preview_id, drawn_by: 'agent', drawing_agent: preview.reviewer, before: outlineBefore,
        after: { vertices: o.ring.length, holes: (o.holes || []).length, area_px: o.model.footprint_area_px }, fields } } : {}) });
  }
  if (!changes.length) continue;
  let saved = null, revision;
  if (apply) {
    const response = await fetch(`${base}/api/scenes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, review }) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) { out.failed.push({ id, reason: result.error || response.status }); continue; }
    saved = true; revision = result.review?.revision;
    // the saved review must carry each approved preview outline exactly
    for (const a of approved) {
      const s = (result.review?.objects || []).find(x => x.id === a.object);
      const ok = Boolean(s) && JSON.stringify(s.ring) === JSON.stringify(a.outline.ring) && JSON.stringify(s.holes || []) === JSON.stringify(a.outline.holes) && s.geometry_reviewed === true;
      changes.find(c => c.object === a.object && c.outline).outline.saved_matches_preview = ok;
      if (!ok) out.failed.push({ id, object: a.object, reason: 'saved outline differs from the approved preview; inspect the scene' });
    }
  }
  out.scenes.push({ id, changes, saved, ...(revision !== undefined ? { revision } : {}) });
}
console.log(JSON.stringify(out, null, 1));
