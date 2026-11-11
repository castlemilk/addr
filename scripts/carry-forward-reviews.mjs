// Carry saved review edits onto a promoted scene model through the same path the editor's "Carry edits forward"
// button uses (GET /api/scenes?id= gives recovery.review; POST /api/scenes saves it). Only scenes whose review holds no
// human decision are touched unless --include-human is given; nothing else is written. Usage:
//   node scripts/carry-forward-reviews.mjs [--dry-run] [--include-human] [--scenes id ...]   (APP_URL defaults to :3008)
import { readFileSync, readdirSync, existsSync } from 'node:fs';
const base = process.env.APP_URL || 'http://localhost:3008';
const args = process.argv.slice(2), dry = args.includes('--dry-run'), includeHuman = args.includes('--include-human');
const only = args.includes('--scenes') ? args.slice(args.indexOf('--scenes') + 1).filter(a => !a.startsWith('--')) : null;
const isMachine = o => typeof o?.provenance?.source === 'string' && o.provenance.source.startsWith('machine:');
const humanDecided = review => (review.objects || []).some(o => ['confirmed', 'rejected', 'needs_adjustment'].includes(o.status) && !isMachine(o));
const scenes = (only || readdirSync('data/scenes')).filter(id => existsSync(`data/scenes/${id}/review.json`) && existsSync(`data/scenes/${id}/latest.json`));
const out = { carried: [], skipped: {}, failed: [] };
for (const id of scenes) {
  const review = JSON.parse(readFileSync(`data/scenes/${id}/review.json`, 'utf8')), latest = JSON.parse(readFileSync(`data/scenes/${id}/latest.json`, 'utf8'));
  if (review.model_signature === latest.model_signature) { out.skipped.current = (out.skipped.current || 0) + 1; continue; }
  if (!includeHuman && humanDecided(review)) { out.skipped.human_decided = (out.skipped.human_decided || 0) + 1; continue; }
  const data = await (await fetch(`${base}/api/scenes?id=${encodeURIComponent(id)}`)).json();
  if (!data.stale || !data.recovery?.review) { out.skipped[data.recovery_reason ? 'no_recovery' : 'not_stale'] = (out.skipped[data.recovery_reason ? 'no_recovery' : 'not_stale'] || 0) + 1; out.failed.push({ id, reason: data.recovery_reason || 'not stale' }); continue; }
  if (dry) { out.carried.push({ id, carried_objects: data.recovery.carried_objects, dry_run: true }); continue; }
  // The editor's recovery downgrades every object whose geometry changed to needs_adjustment. For a machine label that
  // would fabricate a human decision, and the server refuses a re-asserted 'confirmed' on a changed model, so such a
  // label reverts to the model's plain candidate (the decision policy can label the new outline again). Machine labels
  // the recovery keeps confirmed (same outline in a verified expansion) pass through untouched.
  const carried = data.recovery.review, modelObjects = new Map((data.model.objects || []).map(o => [o.id, o]));
  let droppedMachine = 0;
  carried.objects = (carried.objects || []).map(o => {
    const was = (review.objects || []).find(x => x.id === o.id);
    if (was && isMachine(was) && !was.provenance?.human_confirmed && was.status === 'confirmed' && o.status !== 'confirmed' && modelObjects.has(o.id)) { droppedMachine++; return modelObjects.get(o.id); }
    return o;
  });
  if (droppedMachine) out.dropped_machine_labels = (out.dropped_machine_labels || 0) + droppedMachine;
  const response = await fetch(`${base}/api/scenes`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, review: carried }) });
  const result = await response.json();
  if (!response.ok) { out.failed.push({ id, reason: result.error || response.status }); continue; }
  out.carried.push({ id, carried_objects: data.recovery.carried_objects, revision: result.review?.revision });
}
console.log(JSON.stringify({ base, dry_run: dry, scenes_checked: scenes.length, carried: out.carried.length, skipped: out.skipped, failed: out.failed.length, failures: out.failed.slice(0, 10), sample: out.carried.slice(0, 3) }));
