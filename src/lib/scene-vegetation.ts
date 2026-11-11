import type { SceneKind, SceneObject, SceneReview } from "./scene-types";

/** Vegetation types a reviewer can store on a tree or vegetation outline (decision card 2026-10-08, part 3, "yes to defaults").
 *  The field is optional: absent means not set. The kind stays tree or vegetation, the label is never rewritten, and setting or
 *  clearing a type changes neither the status nor the outline. Garden bed is not offered (the census rule could not tell beds from
 *  mown verges); tree groups stay trees. Only erasable TypeScript here (type imports only): scripts/record-chat-feedback.mjs imports
 *  this file directly through Node's type stripping. */
export const VEGETATION_TYPES = ["grass", "hedge", "shrub", "tree"] as const;
export type VegetationType = (typeof VEGETATION_TYPES)[number];
export const VEGETATION_TYPE_LABELS: Record<VegetationType, string> = { grass: "Grass", hedge: "Hedge", shrub: "Shrub", tree: "Tree" };
/** The kinds that may carry a vegetation type; validateSceneReview refuses the field on every other kind. */
export const VEGETATION_TYPE_KINDS = ["tree", "vegetation"] as const satisfies readonly SceneKind[];
export const carriesVegetationType = (kind: unknown): kind is (typeof VEGETATION_TYPE_KINDS)[number] =>
  (VEGETATION_TYPE_KINDS as readonly unknown[]).includes(kind);
export const isVegetationType = (value: unknown): value is VegetationType => (VEGETATION_TYPES as readonly unknown[]).includes(value);

/** Why an object's vegetation_type cannot be saved; null when it can. Absent (undefined) always can; null is not a value (clearing
 *  removes the key). Shared by the store (validateSceneReview) and scripts/record-chat-feedback.mjs. */
export function vegetationTypeIssue(object: { kind: unknown; vegetation_type?: unknown }): string | null {
  if (object.vegetation_type === undefined) return null;
  if (!isVegetationType(object.vegetation_type)) return `choose a vegetation type (${VEGETATION_TYPES.join(", ")}) or leave it not set.`;
  if (!carriesVegetationType(object.kind)) return "a vegetation type is kept only on a tree or vegetation outline.";
  return null;
}

/** The object with its vegetation type set (a type) or cleared (null). The same object when the kind cannot carry one, the outline
 *  is superseded, the value is not a type, or the type is already that. Nothing else changes: status, geometry_reviewed, ring,
 *  holes, label and model stay as they are. */
export function withVegetationType(object: SceneObject, type: VegetationType | null): SceneObject {
  if (!carriesVegetationType(object.kind) || object.status === "superseded") return object;
  if (type !== null && !isVegetationType(type)) return object;
  if ((object.vegetation_type ?? null) === type) return object;
  const next: SceneObject = { ...object };
  if (type === null) delete next.vegetation_type;
  else next.vegetation_type = type;
  return next;
}

/** The review with one object's vegetation type set or cleared, for the editor's change() (Undo restores the prior draft). The same
 *  review when nothing changes; reviewed_classes and every other object are kept as they are. */
export function withReviewVegetationType(review: SceneReview, objectId: string, type: VegetationType | null): SceneReview {
  let changed = false;
  const objects = review.objects.map((o) => {
    if (o.id !== objectId) return o;
    const next = withVegetationType(o, type);
    changed = next !== o;
    return next;
  });
  return changed ? { ...review, objects } : review;
}

/** The editor's client-side vegetation-type filter: "all" filters nothing; "unset" keeps tree and vegetation outlines without a type;
 *  a type keeps the outlines that carry it. Other kinds leave the queue under any choice but "all". Not part of SceneQueueFilter: the
 *  server's scene summaries never see it. A filter, never a label: nothing here changes an object. */
export const VEGETATION_FILTERS = ["all", "unset", ...VEGETATION_TYPES] as const;
export type VegetationFilter = (typeof VEGETATION_FILTERS)[number];
export const vegetationFilterValue = (value: string | null | undefined): VegetationFilter =>
  (VEGETATION_FILTERS as readonly unknown[]).includes(value) ? (value as VegetationFilter) : "all";
export function vegetationFilterAllows(object: Pick<SceneObject, "kind" | "vegetation_type">, filter: VegetationFilter) {
  if (filter === "all") return true;
  if (!carriesVegetationType(object.kind)) return false;
  return filter === "unset" ? !isVegetationType(object.vegetation_type) : object.vegetation_type === filter;
}
/** Outlines per filter value among those that can carry a type (superseded outlines are not counted), for the filter's option words. */
export function vegetationTypeCounts(objects: readonly Pick<SceneObject, "kind" | "status" | "vegetation_type">[]) {
  const counts: Record<Exclude<VegetationFilter, "all">, number> = { unset: 0, grass: 0, hedge: 0, shrub: 0, tree: 0 };
  for (const o of objects) {
    if (!carriesVegetationType(o.kind) || o.status === "superseded") continue;
    counts[isVegetationType(o.vegetation_type) ? o.vegetation_type : "unset"]++;
  }
  return counts;
}
