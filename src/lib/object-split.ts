import { createHash } from "node:crypto";
import { ReviewError } from "./review-feedback";
import { automatedPolygonEditIssue, polygonDraftFingerprint } from "./roof-feedback-patch";
import { replaceInstances } from "./scene-instances";
import { SCENE_CLASSES, polygonIssues, type SceneData, type SceneKind, type ScenePoint, type SceneReview } from "./scene-types";

type Shape = { ring: ScenePoint[]; holes: ScenePoint[][] };
export interface ObjectSplitPreview {
  preview_id: string;
  content: {
    version: string;
    binding: { scene_id: string; object_id: string; object_kind: SceneKind; revision: number; model_signature: string; image_sha256: string;
      source_model_sha256: string; review_sha256: string | null; outline_pixels: Shape };
    object_kind: SceneKind;
    children: { key: string; object_id: string; label: string; outline_pixels: Shape; notes: string; role: "object" | "unresolved-residual" }[];
    group: { id: string; label: string } | null;
    inherited_group_id?: string | null;
    notes: string;
    uncertainty: string;
    coverage: { rings: ScenePoint[][]; holes: ScenePoint[][][]; children: Record<string, number | null>; area_px: number };
    can_apply: boolean;
  };
}
type Provenanced = SceneReview["objects"][number] & { provenance?: Record<string, unknown>; boundary_refinement?: unknown; clipped_by_frame?: unknown };

export function splitChildId(binding: ObjectSplitPreview["content"]["binding"], key: string): string {
  return "split_"+createHash("sha256").update(JSON.stringify([binding.scene_id, binding.object_id, binding.model_signature, binding.object_kind, key])).digest("hex").slice(0,32);
}

/** Input comes only from the immutable preview verifier; geometry is checked by the shared exact partition engine. */
export function patchedObjectSplit(data: SceneData, preview: ObjectSplitPreview, actor: string): SceneReview {
  const content = preview.content, binding = content.binding;
  if (content.version !== "object-agent-split-preview-v1" || !/^[a-f0-9]{64}$/.test(preview.preview_id) || !content.can_apply)
    throw new ReviewError("Use an applicable immutable object split preview.", 409);
  if (data.stale || data.model.id !== binding.scene_id || data.model.model_signature !== binding.model_signature ||
      data.model.image_sha256 !== binding.image_sha256 || (data.review?.revision || 0) !== binding.revision)
    throw new ReviewError("The source or scene revision changed. Inspect a fresh split preview.", 409);
  const review: SceneReview = structuredClone(data.review || { revision: 0, model_signature: data.model.model_signature, objects: data.model.objects, groups: data.model.groups, reviewed_classes: [] });
  const source = review.objects.find(object => object.id === binding.object_id);
  if (!source) throw new ReviewError("This split source does not exist in the current scene.", 404);
  if (!SCENE_CLASSES.includes(source.kind) || source.kind !== binding.object_kind || source.kind !== content.object_kind ||
      JSON.stringify(source.ring) !== JSON.stringify(binding.outline_pixels.ring) ||
      JSON.stringify(source.holes || []) !== JSON.stringify(binding.outline_pixels.holes) ||
      (source.group_id ?? null) !== (content.inherited_group_id ?? null))
    throw new ReviewError("The source outline, class or group changed. Inspect a fresh split preview.", 409);
  const protectedReason = automatedPolygonEditIssue(data, source);
  if (protectedReason) throw new ReviewError(protectedReason, 409);
  if (review.objects.some(object => object.parent_id === source.id && !["rejected", "superseded"].includes(object.status)))
    throw new ReviewError("Resolve active physically attached child assignments in the scene editor before splitting.", 409);
  if (!Array.isArray(content.children) || content.children.length < 2 || content.children.length > 8 ||
      new Set(content.children.map(child => child.key)).size !== content.children.length)
    throw new ReviewError("Supply two to eight child parts with unique stable keys.");
  for (const child of content.children) {
    if (!/^[a-zA-Z0-9_-]{1,60}$/.test(child.key) || child.object_id !== splitChildId(binding, child.key) ||
        review.objects.some(object => object.id === child.object_id) || !["object", "unresolved-residual"].includes(child.role) ||
        typeof child.label !== "string" || !child.label.trim() || child.label.length > 150 || typeof child.notes !== "string" || child.notes.length > 2000)
      throw new ReviewError("Split child labels, roles or stable identities changed; inspect a fresh preview.", 409);
    const issues = polygonIssues(child.outline_pixels.ring, data.model.image_width, data.model.image_height, child.outline_pixels.holes);
    if (issues.length) throw new ReviewError(issues[0]);
  }
  if (JSON.stringify(content.coverage.rings) !== JSON.stringify(content.children.map(child => child.outline_pixels.ring)) ||
      JSON.stringify(content.coverage.holes) !== JSON.stringify(content.children.map(child => child.outline_pixels.holes)) || Object.keys(content.coverage.children).length)
    throw new ReviewError("The immutable partition geometry changed.", 409);
  let childIndex = 0;
  const draft = replaceInstances(review, data.model, [source.id], "split", content.coverage,
    () => content.children[childIndex++].object_id).review;
  if (content.group) {
    const expectedGroupId = "splitgroup_"+createHash("sha256").update(JSON.stringify([binding.scene_id, binding.object_id, binding.model_signature, content.group.label])).digest("hex").slice(0,32);
    if (content.group.id !== expectedGroupId || !content.group.label.trim() || content.group.label.length > 150 || draft.groups.some(group => group.id === content.group!.id))
      throw new ReviewError("The proposed shared group changed or its identity already exists.", 409);
    draft.groups.push(content.group);
  }
  const now = new Date().toISOString(), previousProvenance = (source as Provenanced).provenance;
  const common = { source: "machine:mcp-object-split-draft-v1", tier: "T3", human_confirmed: false, human_approval: false, training_eligible: false,
    actor, updated_at: now, source_scene_id: binding.scene_id, source_object_id: source.id,
    source_model_signature: binding.model_signature, source_image_sha256: binding.image_sha256,
    source_outline_pixels: structuredClone(binding.outline_pixels), split_preview_id: preview.preview_id,
    ...(previousProvenance ? { previous_provenance: previousProvenance } : {}) };
  const retained = draft.objects.find(object => object.id === source.id)! as Provenanced;
  retained.model.height_verified = false;
  retained.notes = [source.notes, content.notes, `Superseded by ${content.children.length} unchecked same-class child parts. Full source coverage retained; seams and subgroup labels remain unapproved.`, content.uncertainty].filter(Boolean).join("\n").slice(0,5000);
  retained.provenance = { ...common, split_role: "superseded-source" };
  retained.provenance.draft_fingerprint = polygonDraftFingerprint(retained);
  for (const child of content.children) {
    const object = draft.objects.find(candidate => candidate.id === child.object_id)! as Provenanced;
    object.source = "machine:mcp-object-split-draft-v1";
    object.label = child.label;
    object.group_id = content.group?.id || source.group_id;
    object.notes = [content.notes, child.notes, `Unchecked ${child.role === "unresolved-residual" ? "unresolved residual" : "split"} part of ${source.id}. Same source class retained as a draft; visible internal seams, heights and subgroup interpretation remain unapproved.`, content.uncertainty].filter(Boolean).join("\n").slice(0,5000);
    delete object.boundary_refinement; delete object.clipped_by_frame; delete object.replaces;
    delete object.model.tree; delete object.model.car; delete object.model.surface; delete object.model.solar;
    object.visibility = undefined;
    object.review_issues = [...new Set([...(object.review_issues || []), "instances" as const, ...(child.role === "unresolved-residual" ? ["class" as const] : [])])];
    object.provenance = { ...common, split_role: "child", split_child_key: child.key, split_child_role: child.role };
    object.provenance.draft_fingerprint = polygonDraftFingerprint(object);
  }
  return draft;
}
