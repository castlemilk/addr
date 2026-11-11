import type { SceneObject } from "./scene-types";

/** A fabric membrane is a covered structure, but has no supported rigid roof model. */
export const isShadeSail = (object: Pick<SceneObject, "kind" | "structure_kind">) =>
  object.kind === "building" && object.structure_kind === "shade-sail";

export const hasConventionalRoof = (object: Pick<SceneObject, "kind" | "structure_kind">) =>
  object.kind === "building" && !isShadeSail(object);

/** Remove incompatible priors and checks; the original prediction and review history retain them. */
export function clearConventionalRoofDraft(object: SceneObject): SceneObject {
  const next = { ...object, model: { ...object.model, height_verified: false } };
  for (const field of ["roof_type", "roof_parts", "roof_lines", "roof_annotation", "roof_feedback", "roof_line_edits",
    "roof_geometry_source", "roof_geometry_note", "roof_form_evidence", "roof_form_uncertain", "roof_form_comparison",
    "roof_optical_guidance", "roof_diagnostics", "roof_structure", "roof_line_fit", "roof_physics"]) {
    delete (next as unknown as Record<string, unknown>)[field];
  }
  return next;
}

/** A subtype correction reopens review without inferring an outline, height or membrane surface. */
export function withStructureKind(object: SceneObject, kind: string, label?: string): SceneObject {
  if (object.kind !== "building") return object;
  const next: SceneObject = { ...object, structure_kind: kind || undefined, label: label ?? object.label,
    status: "needs_adjustment", geometry_reviewed: false, model: { ...object.model, height_verified: false } };
  return isShadeSail(next) ? clearConventionalRoofDraft(next) : next;
}
