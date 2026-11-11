import { createHash } from "node:crypto";
import { ReviewError } from "./review-feedback";
import { currentRoofLineEdits, initialRoofLineEdits, lineOutline, roofLineEditsIssue, type RoofFeedbackEdge } from "./roof-line-edits";
import { polygonIssues, SCENE_CLASSES, SCENE_REVIEW_ISSUES, type SceneData, type SceneObject, type ScenePoint, type SceneReview, type SceneReviewIssue } from "./scene-types";
import { clearConventionalRoofDraft, hasConventionalRoof, isShadeSail } from "./scene-structure";

type PolygonProvenance = { source?: unknown; human_confirmed?: unknown; [key: string]: unknown };
type ProvenancedObject = SceneObject & { provenance?: PolygonProvenance; fieldwork_import?: { pending?: boolean } };

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().filter(key => (value as Record<string, unknown>)[key] !== undefined).map(key => [key, canonical((value as Record<string, unknown>)[key])]));
  return value;
}
/** Bind machine authority to the exact draft; ordinary CMS edits must invalidate the marker. */
export function polygonDraftFingerprint(object: SceneObject): string {
  const value = structuredClone(object) as SceneObject & { provenance?: unknown };
  delete value.provenance;
  const model = value.model as unknown as Record<string, unknown>;
  // The canonical store recomputes these from the same outline on every save.
  for (const key of ["width_m", "length_m", "area_m2", "rotation_deg", "footprint_area_px"]) delete model[key];
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

export interface RoofFeedbackPatch {
  scene_id: string;
  object_id: string;
  expected_revision: number;
  model_signature: string;
  image_sha256: string;
  outline_pixels: { ring: ScenePoint[]; holes: ScenePoint[][] };
  changes: {
    notes?: string;
    review_issues?: SceneReviewIssue[];
    roof_line_edits?: { vertices_meters: ScenePoint[]; edges: RoofFeedbackEdge[]; notes?: string };
    outline_pixels?: { ring: ScenePoint[]; holes: ScenePoint[][] };
    polygon_preview_id?: string;
  };
}

function record(value: unknown, keys: string[], title: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).some(key => !keys.includes(key)))
    throw new ReviewError(`${title}: use only the documented fields.`);
  return value as Record<string, unknown>;
}
function text(value: unknown, max: number, title: string): string {
  if (typeof value !== "string" || value.length > max) throw new ReviewError(`${title}: use at most ${max} characters.`);
  return value;
}
function points(value: unknown, limit: number): ScenePoint[] {
  if (!Array.isArray(value) || value.length > limit || value.some(point => !Array.isArray(point) || point.length !== 2 || point.some(n => typeof n !== "number" || !Number.isFinite(n) || Math.abs(n) > 10000)))
    throw new ReviewError("Use finite two-dimensional points within the scene coordinate limits.");
  return value as ScenePoint[];
}
function outline(value: unknown, title: string): RoofFeedbackPatch["outline_pixels"] {
  const shape = record(value, ["ring", "holes"], title);
  if (!Array.isArray(shape.holes) || shape.holes.length > 32) throw new ReviewError("Supply the outline and its gaps.");
  return { ring: points(shape.ring, 200), holes: shape.holes.map(hole => points(hole, 200)) };
}
/** This deliberately excludes attestations, source fields and caller-supplied baselines. */
export function parseRoofFeedbackPatch(value: unknown): RoofFeedbackPatch {
  const body = record(value, ["scene_id", "object_id", "expected_revision", "model_signature", "image_sha256", "outline_pixels", "changes"], "Roof feedback patch");
  if (typeof body.scene_id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(body.scene_id) || typeof body.object_id !== "string" || !/^[a-zA-Z0-9_-]{1,100}$/.test(body.object_id))
    throw new ReviewError("Choose valid scene and object identities.");
  if (!Number.isSafeInteger(body.expected_revision) || (body.expected_revision as number) < 0)
    throw new ReviewError("Supply the saved scene revision.");
  if (typeof body.model_signature !== "string" || !/^[a-f0-9]{64}$/.test(body.model_signature) || typeof body.image_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(body.image_sha256))
    throw new ReviewError("Supply the model and image identities from the saved scene.");
  const outline_pixels = outline(body.outline_pixels, "Outline binding");
  const fields = record(body.changes, ["notes", "review_issues", "roof_line_edits", "outline_pixels", "polygon_preview_id"], "Draft changes");
  if (!Object.keys(fields).length) throw new ReviewError("Choose at least one draft field to update.");
  const changes: RoofFeedbackPatch["changes"] = {};
  if ("outline_pixels" in fields) {
    changes.outline_pixels = outline(fields.outline_pixels, "Replacement outline");
    if ("roof_line_edits" in fields) throw new ReviewError("Change the outline separately from roof lines. Recover stale lines explicitly after checking the new outline.");
  }
  if ("polygon_preview_id" in fields) {
    if (!changes.outline_pixels || typeof fields.polygon_preview_id !== "string" || !/^[a-f0-9]{64}$/.test(fields.polygon_preview_id))
      throw new ReviewError("A polygon preview identity requires a replacement outline and a SHA-256 identity.");
    changes.polygon_preview_id = fields.polygon_preview_id;
  }
  if ("notes" in fields) changes.notes = text(fields.notes, 5000, "Object notes");
  if ("review_issues" in fields) {
    const issues = fields.review_issues;
    if (!Array.isArray(issues) || new Set(issues).size !== issues.length || issues.some(issue => !SCENE_REVIEW_ISSUES.includes(issue)))
      throw new ReviewError("Choose valid, distinct review issues.");
    changes.review_issues = issues;
  }
  if ("roof_line_edits" in fields) {
    const lines = record(fields.roof_line_edits, ["vertices_meters", "edges", "notes"], "Draft roof lines");
    if (!Array.isArray(lines.edges) || lines.edges.length > 600) throw new ReviewError("Use at most 600 roof lines.");
    const edges = lines.edges.map(edge => {
      const e = record(edge, ["a", "b", "kind"], "Roof line");
      if (!Number.isSafeInteger(e.a) || !Number.isSafeInteger(e.b) || !["ridge", "hip", "valley"].includes(e.kind as string))
        throw new ReviewError("Check the roof line endpoints and types.");
      return { a: e.a, b: e.b, kind: e.kind } as RoofFeedbackEdge;
    });
    changes.roof_line_edits = { vertices_meters: points(lines.vertices_meters, 600), edges, ...("notes" in lines ? { notes: text(lines.notes, 3000, "Line notes") } : {}) };
  }
  return { scene_id: body.scene_id, object_id: body.object_id, expected_revision: body.expected_revision as number,
    model_signature: body.model_signature, image_sha256: body.image_sha256, outline_pixels, changes };
}

/** Protect human work; machine-prefixed drafts remain outside reference/calibration authority. */
export function automatedPolygonEditIssue(data: SceneData, value: SceneObject): string | null {
  if (["house-30286", "house-30296"].includes(data.model.id)) return "This is a protected reference capture. Use its human review workflow.";
  const object = value as ProvenancedObject, provenance = object.provenance;
  if (["rejected", "superseded"].includes(object.status)) return "This roof is inactive. Reopen it explicitly in the scene editor.";
  if (object.fieldwork_import) return "Imported Fieldwork feedback is protected. Edit its outline in the scene editor.";
  if (provenance?.human_confirmed === true || provenance?.human_approval === true || object.roof_line_edits?.checked || object.model.height_verified)
    return "This roof carries human checks. Edit its outline in the scene editor.";
  if (object.geometry_reviewed || object.status === "confirmed" || /^(reviewer|manual)/.test(object.source))
    return "This roof has a human-reviewed outline. Edit it in the scene editor.";
  if (typeof provenance?.source === "string" && provenance.source.startsWith("machine:")) {
    if (data.review && (typeof provenance.draft_fingerprint !== "string" || provenance.draft_fingerprint !== polygonDraftFingerprint(object)))
      return "The saved machine draft was edited or its authorship cannot be verified. Preserve these changes and use the scene editor.";
    if (!data.model.objects.some(item => item.id === object.id)) {
      const trustedSource = "machine:mcp-object-split-draft-v1";
      if (object.source !== trustedSource || ![trustedSource, "machine:mcp-polygon-draft-v1", "machine:mcp-roof-feedback-draft-v1"].includes(provenance.source) || provenance.source_scene_id !== data.model.id ||
          provenance.source_model_signature !== data.model.model_signature || provenance.source_image_sha256 !== data.model.image_sha256 ||
          typeof provenance.split_preview_id !== "string" || !/^[a-f0-9]{64}$/.test(provenance.split_preview_id) ||
          object.derived_from?.length !== 1 || !(data.review?.objects || []).some(parent => parent.id === object.derived_from![0] && parent.kind === object.kind && parent.replacement_ids?.includes(object.id)))
        return "This review-only object has no trusted machine split lineage. Preserve it and use the scene editor.";
    }
    return null;
  }
  if (!data.review) return null;
  const original = data.model.objects.find(item => item.id === object.id);
  if (!original || (object.kind !== "building" && polygonDraftFingerprint(original) !== polygonDraftFingerprint(object)) || JSON.stringify(lineOutline(original)) !== JSON.stringify(lineOutline(object)) || object.roof_line_edits || object.roof_feedback ||
      object.status !== original.status || (object.notes || "") !== (original.notes || "") || JSON.stringify(object.review_issues || []) !== JSON.stringify(original.review_issues || []) ||
      JSON.stringify(object.roof_annotation) !== JSON.stringify(original.roof_annotation))
    return "This roof has saved reviewer feedback. Edit its outline in the scene editor.";
  return null;
}

/** Construct one source-bound draft while leaving all other objects and provenance untouched. */
export function patchedRoofFeedback(data: SceneData, patch: RoofFeedbackPatch, actor = "mcp"): SceneReview {
  return patchedFeedback(data, patch, actor, false);
}

/** Generic polygon drafts share all source, authorship and transaction guards; no roof-line writes. */
export function parseObjectPolygonPatch(value: unknown): RoofFeedbackPatch {
  const patch = parseRoofFeedbackPatch(value);
  if (!patch.changes.outline_pixels || patch.changes.roof_line_edits)
    throw new ReviewError("Object polygon patches require a replacement outline and cannot edit roof lines.");
  return patch;
}

export function patchedObjectPolygon(data: SceneData, patch: RoofFeedbackPatch, actor = "mcp"): SceneReview {
  if (!patch.changes.outline_pixels || patch.changes.roof_line_edits)
    throw new ReviewError("Object polygon patches require a replacement outline and cannot edit roof lines.");
  return patchedFeedback(data, patch, actor, true);
}

function patchedFeedback(data: SceneData, patch: RoofFeedbackPatch, actor: string, anyClass: boolean): SceneReview {
  if (data.stale) throw new ReviewError("The scene model changed. Recover saved feedback in the scene editor before editing it.", 409);
  if (data.model.id !== patch.scene_id || data.model.model_signature !== patch.model_signature || data.model.image_sha256 !== patch.image_sha256)
    throw new ReviewError("The scene model or image changed. Reload before editing.", 409);
  if ((data.review?.revision || 0) !== patch.expected_revision)
    throw new ReviewError("This scene changed in another session. Reload before editing.", 409);
  const review: SceneReview = structuredClone(data.review || { revision: 0, model_signature: data.model.model_signature, objects: data.model.objects, groups: data.model.groups, reviewed_classes: [] });
  let object = review.objects.find(item => item.id === patch.object_id);
  if (!object) throw new ReviewError("This roof object does not exist in the current scene.", 404);
  if (!SCENE_CLASSES.includes(object.kind) || (!anyClass && object.kind !== "building")) throw new ReviewError("Choose a supported scene object; roof feedback requires a building object.");
  if (!anyClass && !hasConventionalRoof(object)) throw new ReviewError("Fabric shade sails use object boundary tools; conventional roof feedback is unavailable.");
  if (["rejected", "superseded"].includes(object.status)) throw new ReviewError("This roof object is inactive. Reopen it in the scene editor before editing.", 409);
  if (JSON.stringify(lineOutline(object)) !== JSON.stringify(patch.outline_pixels))
    throw new ReviewError("The roof outline changed. Reload before editing.", 409);
  const existingProvenance = (object as ProvenancedObject).provenance;
  const refreshFingerprint = typeof existingProvenance?.source === "string" && existingProvenance.source.startsWith("machine:") && existingProvenance.draft_fingerprint === polygonDraftFingerprint(object);
  if (changesMachineGeometry(patch) && data.review && typeof existingProvenance?.source === "string" && existingProvenance.source.startsWith("machine:") && !refreshFingerprint)
    throw new ReviewError("The saved machine draft was edited or its authorship cannot be verified. Preserve these changes and use the scene editor.", 409);
  const { changes } = patch;
  if (changes.outline_pixels) {
    const protectedReason = automatedPolygonEditIssue(data, object);
    if (protectedReason) throw new ReviewError(protectedReason, 409);
    const issues = polygonIssues(changes.outline_pixels.ring, data.model.image_width, data.model.image_height, changes.outline_pixels.holes);
    if (issues.length) throw new ReviewError(issues[0]);
    if (JSON.stringify(changes.outline_pixels) === JSON.stringify(lineOutline(object))) throw new ReviewError("The replacement outline is unchanged. Supply an explicit polygon correction.");
    const typed = object as ProvenancedObject;
    const previousProvenance = typed.provenance;
    object.ring = structuredClone(changes.outline_pixels.ring);
    object.holes = structuredClone(changes.outline_pixels.holes);
    // Estimates derived from the old footprint must not render as a corrected roof.
    delete object.roof_parts; delete object.roof_lines; delete object.roof_type; delete object.roof_feedback;
    delete object.roof_geometry_source; delete object.roof_geometry_note;
    object.roof_annotation = null;
    object.visibility = undefined;
    if (object.kind !== "building") {
      // Class-specific surface/crown/vehicle/solar estimates depended on the old footprint.
      delete object.model.solar; delete object.model.surface; delete object.model.car; delete object.model.tree;
    }
    if (object.roof_line_edits) object.roof_line_edits = { ...object.roof_line_edits, checked: false };
    typed.provenance = { source: "machine:mcp-polygon-draft-v1", tier: "T3", human_confirmed: false, human_approval: false, training_eligible: false,
      actor, updated_at: new Date().toISOString(), source_model_signature: patch.model_signature, source_image_sha256: patch.image_sha256,
      ...(object.source === "machine:mcp-object-split-draft-v1" ? { source_scene_id: previousProvenance?.source_scene_id, split_preview_id: previousProvenance?.split_preview_id } : {}),
      source_outline_pixels: structuredClone(patch.outline_pixels), ...(changes.polygon_preview_id ? { preview_id: changes.polygon_preview_id } : {}),
      ...(previousProvenance ? { previous_provenance: previousProvenance } : {}) };
  }
  if (changes.notes !== undefined) object.notes = changes.notes;
  if (changes.review_issues !== undefined) object.review_issues = changes.review_issues;
  if (changes.roof_line_edits) {
    if (object.roof_line_edits && !currentRoofLineEdits(object))
      throw new ReviewError("The saved lines belong to an earlier outline. Re-anchor them explicitly in the scene editor first.", 409);
    const original = object.roof_line_edits || initialRoofLineEdits(object);
    object.roof_line_edits = { ...original, vertices: changes.roof_line_edits.vertices_meters, edges: changes.roof_line_edits.edges, checked: false,
      notes: changes.roof_line_edits.notes ?? original.notes };
    const issue = roofLineEditsIssue(object, data.model.pixel_to_meters, data.model.image_width, data.model.image_height);
    if (issue) throw new ReviewError(issue);
    const typed = object as ProvenancedObject;
    typed.provenance = { source: "machine:mcp-roof-feedback-draft-v1", tier: "T3", human_confirmed: false, human_approval: false, training_eligible: false,
      actor, updated_at: new Date().toISOString(), source_model_signature: patch.model_signature, source_image_sha256: patch.image_sha256,
      ...(object.source === "machine:mcp-object-split-draft-v1" ? { source_scene_id: existingProvenance?.source_scene_id, split_preview_id: existingProvenance?.split_preview_id } : {}),
      source_outline_pixels: structuredClone(patch.outline_pixels), ...(existingProvenance ? { previous_provenance: existingProvenance } : {}) };
  }
  if (changes.review_issues !== undefined || changes.roof_line_edits || changes.outline_pixels) {
    object.status = "needs_adjustment";
    object.geometry_reviewed = false;
    object.model = { ...object.model, height_verified: false };
    const changedKind = object.kind;
    review.reviewed_classes = review.reviewed_classes.filter(kind => kind !== changedKind);
  }
  if (changes.outline_pixels || changes.roof_line_edits || refreshFingerprint) {
    // Match canonical subtype cleanup before binding machine authorship, so a
    // saved sail polygon can be refined again through the generic object tools.
    if (isShadeSail(object)) {
      const cleaned = clearConventionalRoofDraft(object);
      review.objects = review.objects.map(item => item === object ? cleaned : item);
      object = cleaned;
    }
    const typed = object as ProvenancedObject;
    typed.provenance = { ...typed.provenance, draft_fingerprint: polygonDraftFingerprint(object) };
  }
  return review;
}

function changesMachineGeometry(patch: RoofFeedbackPatch): boolean {
  return !!(patch.changes.outline_pixels || patch.changes.roof_line_edits);
}
