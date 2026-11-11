import type { SceneData, SceneModelSnapshot, SceneObject, SceneReview } from "./scene-types";
import { carryAcrossFramesResult } from "./scene-frame-transform";
import { clearConventionalRoofDraft, isShadeSail } from "./scene-structure";

// Compare editable feedback, excluding dimensions recalculated during saving.
const feedback = (o: SceneObject) => JSON.stringify({
  kind: o.kind, structure_kind: o.structure_kind || "", vegetation_type: o.vegetation_type || "", label: o.label, ring: o.ring, holes: o.holes||[], status: o.status,
  checked: o.geometry_reviewed, notes: o.notes || "", group: o.group_id || "",
  visibility: o.visibility || null,
  review_issues: o.review_issues || [],
  parent: o.parent_id || "", roof: o.roof_annotation || null,
  parts: o.roof_parts || null, lines: o.roof_lines || null, form: o.roof_type || "",
  roof_source: o.roof_geometry_source || "", roof_note: o.roof_geometry_note || "",
  roof_feedback: o.roof_feedback || null,
  roof_line_edits: o.roof_line_edits || null,
  height: o.model.height_m, base: o.model.base_m, color: o.model.color,
  height_source: o.model.height_source, height_verified: o.model.height_verified,
  placement: o.model.placement || "roof",
  tree: o.model.tree || null,
  car: o.model.car || null,
  surface: o.model.surface || null,
  solar: o.model.solar || null,
  derived_from: o.derived_from || null, replacement_ids: o.replacement_ids || null,
  // A carried object's provenance is part of what the reviewer saved: the same outline with and without it differs.
  carried_from: o.carried_from || null,
});

/** Exact padded-image recovery only; arbitrary recaptures still fail closed. */
export function expansionOffset(previous: SceneModelSnapshot, current: SceneModelSnapshot): number | null {
  const e=current.context_expansion;
  if (!e || e.source_model_signature!==previous.model_signature || e.source_image_sha256!==previous.image_sha256 ||
    previous.id!==current.id || previous.image_width!==512 || previous.image_height!==512 || current.image_width!==1024 || current.image_height!==1024 ||
    JSON.stringify(e.offset_pixels)!=="[256,256]" || JSON.stringify(e.source_size)!=="[512,512]" || !/^[a-f0-9]{64}$/.test(e.preserved_pixels_sha256) ||
    !previous.centroid || JSON.stringify(previous.centroid)!==JSON.stringify(current.centroid) || previous.region?.provider!==current.region?.provider || previous.region?.imagery_zoom!==current.region?.imagery_zoom) return null;
  const m=previous.pixel_to_meters, p=previous.meters_to_pixel;
  const expectedMeters=[m[0],m[1],m[2].map((v,i)=>v-256*(m[0][i]+m[1][i]))];
  const expectedPixels=[p[0],p[1],p[2].map(v=>v+256)];
  const same=(a:number[][],b:number[][])=>a.length===b.length && a.every((row,i)=>row.length===b[i].length && row.every((v,j)=>Number.isFinite(b[i][j])&&Math.abs(v-b[i][j])<1e-9));
  return same(expectedMeters,current.pixel_to_meters)&&same(expectedPixels,current.meters_to_pixel)?256:null;
}

export function shiftSceneObject(object: SceneObject, offset: number): SceneObject {
  const shifted=structuredClone(object);
  const ring=(r: typeof object.ring)=>r.map(([x,y])=>[x+offset,y+offset] as [number,number]);
  shifted.ring=ring(shifted.ring);
  if (shifted.holes) shifted.holes=shifted.holes.map(ring);
  for(const value of [shifted.roof_feedback,shifted.roof_line_edits]) if(value) {
    value.outline.ring=ring(value.outline.ring);value.outline.holes=value.outline.holes.map(ring);
  }
  return shifted;
}

/**
 * Carry a saved review onto a newer snapshot, or null when it cannot be carried. Three cases: a verified context expansion
 * (the same pixels inside a wider frame), a recapture (a new image of the place captured as another scene, with a measured
 * frame_transform back to this one; delegated to scene-frame-transform.ts, which names the step it refuses on through
 * `options.onRefuse`, and gives the carried review `options.revision`, the target's own revision), and the same image under
 * a re-extraction. A carried decision returns as needs_adjustment in every case but the expansion's unchanged interior object.
 */
export function recoverSceneReview(
  previous: SceneModelSnapshot,
  current: SceneModelSnapshot,
  saved: SceneReview,
  options: { preserveUnchanged?: boolean; revision?: number; onRefuse?: (reason: string) => void } = {},
): NonNullable<SceneData["recovery"]> | null {
  const offset=expansionOffset(previous,current);
  if(offset!==null && previous.model_signature===saved.model_signature) {
    // A verified context expansion keeps every saved pixel and shifts the frame by a known offset: an interior object the
    // new model re-proposes unchanged is the same evidence, so its confirmed decision may stand.
    return recoverSceneReview({...previous,image_sha256:current.image_sha256,image_width:current.image_width,image_height:current.image_height,
      pixel_to_meters:current.pixel_to_meters,meters_to_pixel:current.meters_to_pixel,objects:previous.objects.map(o=>shiftSceneObject(o,offset))},
      current,{...saved,objects:saved.objects.map(o=>shiftSceneObject(o,offset))},{preserveUnchanged:true});
  }
  // A recapture: another scene's image of the same place, with the measured transform back to the source frame recorded on
  // its snapshot. No pixel is shared, so decisions travel only re-projected through the georeferenced frame and every one
  // returns for a recheck; the module checks the recorded transform against the two snapshots (the store has already run the
  // Python verifier on the image bytes) and refuses with the step that failed.
  const transform = current.frame_transform;
  if (transform && typeof transform === "object" && !Array.isArray(transform) && current.id !== previous.id) {
    const result = carryAcrossFramesResult(previous, current, saved, { revision: options.revision ?? 0 });
    if (!result.recovery) { options.onRefuse?.(result.reason); return null; }
    return { ...result.recovery, source_scene_id: previous.id };
  }
  if (
    previous.model_signature !== saved.model_signature ||
    previous.id !== current.id ||
    previous.image_sha256 !== current.image_sha256 ||
    previous.image_width !== current.image_width ||
    previous.image_height !== current.image_height ||
    JSON.stringify(previous.pixel_to_meters) !== JSON.stringify(current.pixel_to_meters) ||
    JSON.stringify(previous.meters_to_pixel) !== JSON.stringify(current.meters_to_pixel)
  ) return null;

  const originals = new Map(previous.objects.map(o => [o.id, o]));
  const latest = new Map(current.objects.map(o => [o.id, o]));
  const reviewed = new Map(saved.objects.map(o => [o.id, o]));
  const carried = new Map(saved.objects.filter(o => {
    const original = originals.get(o.id);
    return !original || feedback(o) !== feedback(original);
  }).map(o => [o.id, o]));
  // A fuller detection that took over a model seed (`replaces`, written by the extractor when a scene is widened) is
  // the same evidence seen whole. The human decision on the seed moves onto it for a recheck instead of surviving as
  // a ghost of the clipped outline; a human-drawn replacement (superseded seed) keeps its own lineage untouched.
  const replacedBy = new Map<string, SceneObject>();
  for (const o of current.objects) for (const seed of o.replaces || []) if (!latest.has(seed)) replacedBy.set(seed, o);
  // A machine label (provenance machine:*, never confirmed by a human) on a replaced seed is dropped rather than
  // inherited: nobody decided anything about the fuller outline, and carrying it would fabricate a human decision.
  const machineOnly = (o: SceneObject) => { const p = (o as { provenance?: { source?: unknown; human_confirmed?: unknown } }).provenance;
    return typeof p?.source === "string" && p.source.startsWith("machine:") && !p.human_confirmed; };
  const inherited = new Map<string, SceneObject[]>();
  for (const [id, o] of [...carried]) {
    const replacement = replacedBy.get(id);
    if (!replacement || o.status === "superseded") continue;
    carried.delete(id);
    if (machineOnly(o)) continue;
    inherited.set(replacement.id, [...(inherited.get(replacement.id) || []), o]);
  }

  // Keep parent context when a fresh detector no longer proposes that parent, or point at the detection that took it over.
  for (const object of carried.values()) {
    let parent = object.parent_id;
    const visited = new Set<string>();
    while (parent && !visited.has(parent)) {
      visited.add(parent);
      const replacement = replacedBy.get(parent);
      if (replacement) { carried.set(object.id, { ...object, parent_id: replacement.id }); break; }
      const ancestor = reviewed.get(parent);
      if (!ancestor) break;
      if (!latest.has(parent) && !carried.has(parent)) carried.set(parent, ancestor);
      parent = ancestor.parent_id;
    }
  }
  const groups = new Map(current.groups.map(g => [g.id, g]));
  const removedGroups=new Set(previous.groups.filter(g=>!saved.groups.some(s=>s.id===g.id)).map(g=>g.id));
  for(const id of removedGroups)groups.delete(id);
  for (const group of saved.groups) groups.set(group.id, group);
  // A new extraction adds hypotheses, not authority. Preserve edits verbatim
  // and expose changed-ID detections alongside them for duplicate review.
  const objects: SceneObject[] = current.objects.filter(o => !carried.has(o.id))
    .map(o=>o.group_id&&removedGroups.has(o.group_id)?{...o,group_id:undefined}:o)
    .map(o => {
      const decisions = inherited.get(o.id);
      if (!decisions) return o;
      // Every seed rejected: the whole is rejected too. Any other mix returns for a recheck on the new outline, carrying
      // the reviewer's words (label, structure kind, roof type, notes) but never the old geometry or its checked flag.
      const live = decisions.filter(d => d.status !== "rejected");
      if (!live.length) return { ...o, status: "rejected" as const, geometry_reviewed: false, notes: decisions[0].notes };
      const lead = live[0];
      const inheritedObject = { ...o, status: "needs_adjustment" as const, geometry_reviewed: false, label: lead.label, notes: lead.notes,
        ...(lead.structure_kind ? { structure_kind: lead.structure_kind } : {}), ...(lead.roof_type ? { roof_type: lead.roof_type } : {}),
        // A vegetation type follows only onto a fuller outline that can carry one (scene-vegetation.ts VEGETATION_TYPE_KINDS;
        // validateSceneReview refuses it elsewhere). Inlined so this module's imports stay as they are.
        ...(lead.vegetation_type && (o.kind === "tree" || o.kind === "vegetation") ? { vegetation_type: lead.vegetation_type } : {}) };
      return isShadeSail(inheritedObject) ? clearConventionalRoofDraft(inheritedObject) : inheritedObject;
    });
  // A new extraction adds hypotheses, not authority: a carried decision returns to needs_adjustment, except in a verified
  // context expansion where the new model re-proposes the very same outline under the same id (preserveUnchanged).
  const sameOutline=(a:SceneObject,b:SceneObject|undefined)=>!!b&&JSON.stringify([a.ring,a.holes||[]])===JSON.stringify([b.ring,b.holes||[]]);
  objects.push(...[...carried.values()].map(o => {
    if (["rejected","superseded"].includes(o.status)) return o;
    if (options.preserveUnchanged&&o.status==="confirmed"&&o.geometry_reviewed&&sameOutline(o,latest.get(o.id))) return o;
    return {...o, status: "needs_adjustment" as const, geometry_reviewed: false};
  }));
  return {
    carried_objects: carried.size + [...inherited.values()].reduce((n, d) => n + d.length, 0),
    new_candidates: current.objects.filter(o => !carried.has(o.id) && !inherited.has(o.id)).length,
    review: {
      revision: saved.revision,
      model_signature: current.model_signature,
      objects: objects.map(o => isShadeSail(o) ? clearConventionalRoofDraft(o) : o), groups: [...groups.values()], reviewed_classes: [],
    } satisfies SceneReview,
  };
}
