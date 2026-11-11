import { outlineSupport } from "./scene-roof-mount";
import { type SceneData, type SceneKind, type SceneModelSnapshot, type SceneObject, type SceneReview } from "./scene-types";

export type CandidateMetrics = { images: number; groups: number; references: number; matched: number; extra: number; missed: number };
export type CandidateLayer = { id: string; split: "train" | "validation"; retained: number; filtered: number; matched: number; missed: number; extra: number; lost_matches: number };
export type CandidateCheck = { id: string; created_at: string; dataset_version: string; before: CandidateMetrics; after: CandidateMetrics; scenes: CandidateLayer[] };
export type CandidateSummary = {
  id: string; created_at: string; kind: SceneKind; prompt: string; dataset_version: string; experiment_run: string;
  threshold: number; training_proposals: { usable: number; negative: number; images: number; groups: number };
  supplemental_training?: { dataset_version: string; images: number; usable: number; negative: number };
  before: Record<"train" | "validation", CandidateMetrics>; after: Record<"train" | "validation", CandidateMetrics>;
  scenes: CandidateLayer[];
  checks?: CandidateCheck[]; unavailable_checks?: number;
};
export type CandidateProposal = { object: SceneObject; quality_score: number; retained: boolean;
  frozen_match: { id: string; label: string; iou: number } | null;
  match: { id: string; label: string; status: SceneObject["status"]; iou: number } | null };
export type CandidateScene = {
  check_id?: string;
  candidate_id: string; id: string; title: string; split: "train" | "validation"; image_url: string;
  width: number; height: number; image_sha256: string; model_signature: string; revision: number;
  compatible: boolean; stale: boolean; proposals: CandidateProposal[]; annotations: SceneObject[];
};

/** Filtering is useful only when the lost references are visible alongside removed extras. */
export function candidateAssessment(before: CandidateMetrics, after: CandidateMetrics) {
  const lost = before.matched - after.matched, removed = before.extra - after.extra;
  const status = !before.references || before.references !== after.references || before.images !== after.images || before.groups !== after.groups
    ? "unavailable" : lost > 0 ? "loses-matches" : removed > 0 ? "fewer-extras" : removed < 0 ? "more-extras" : "unchanged";
  return { status, lost_matches: Math.max(0, lost), removed_extras: removed } as const;
}
export const CANDIDATE_ASSESSMENT_LABELS = {
  unavailable: "Needs comparable validation", "loses-matches": "Loses labelled objects",
  "fewer-extras": "Fewer extras, matches preserved", "more-extras": "More extra detections", unchanged: "No detection gain",
};

export function sameCandidateFrame(a: SceneModelSnapshot, b: SceneModelSnapshot) {
  return a.image_sha256 === b.image_sha256 && a.image_width === b.image_width && a.image_height === b.image_height &&
    JSON.stringify(a.pixel_to_meters) === JSON.stringify(b.pixel_to_meters) && JSON.stringify(a.centroid) === JSON.stringify(b.centroid);
}

/** A navigation suggestion, never an automatic replacement or approval. Gaps count as empty space. */
export function candidateMatch(proposal: SceneObject, objects: SceneObject[], frame: SceneModelSnapshot["pixel_to_meters"]): CandidateProposal["match"] {
  let best: CandidateProposal["match"] = null;
  for (const object of objects) {
    if (object.kind !== proposal.kind || object.status === "superseded") continue;
    const overlap = outlineSupport(proposal, object, frame), reverse = outlineSupport(object, proposal, frame);
    const intersection = overlap.area_m2 - overlap.outside_outline_m2;
    const union = overlap.area_m2 + reverse.area_m2 - intersection;
    const iou = union > 0 ? Math.min(1, intersection / union) : 0;
    if (iou >= .5 && (!best || iou > best.iou)) best = { id: object.id, label: object.label, status: object.status, iou };
  }
  return best;
}

export const candidateDraftId = (candidate: string, object: string) => `candidate-${candidate}-${object}`;
export const candidateReviewURL = (scene: string, kind: SceneKind, object?: string) =>
  `/scenes?${new URLSearchParams({ scene, kind, status: "all", ...(object ? { object } : {}) })}`;

/** Explicit import only. Preserve all saved labels, reopening just the candidate's class for review. */
export function candidateDraft(data: SceneData, object: SceneObject, candidate: string, check?: string): SceneReview {
  const review = data.review || { revision: 0, model_signature: data.model.model_signature, objects: data.model.objects, groups: data.model.groups, reviewed_classes: [] };
  const draft: SceneObject = {
    id: candidateDraftId(candidate, object.id), kind: object.kind, label: object.label, ring: structuredClone(object.ring),
    ...(object.holes?.length ? { holes: structuredClone(object.holes) } : {}),
    status: "needs_adjustment", geometry_reviewed: false, source: "experimental-proposal", score: object.score, prompt: object.prompt,
    model: { ...object.model, height_verified: false },
    notes: `Imported from experimental candidate ${candidate}${check ? `, validation check ${check}` : ""}, proposal ${object.id}. Check the outline and class against the image before confirming.`,
  };
  return { ...review, objects: [...review.objects, draft], reviewed_classes: review.reviewed_classes.filter(k => k !== object.kind) };
}
