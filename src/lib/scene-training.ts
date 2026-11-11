import type { SceneKind } from "./scene-types";
export const TRAINING_SPLITS = ["train", "validation", "test"] as const;
export type TrainingSplit = (typeof TRAINING_SPLITS)[number];
export interface TrainingCounts {
  images: number;
  masks: number;
  empty_images: number;
  groups: number;
}
export type TrainingBlocker = "image_geography" | "stale" | "unassigned" | "missing_group" | "split_conflict" | "pending" | "unchecked";
export interface TrainingLayer {
  scene_id: string;
  kind: SceneKind;
  title: string;
  split: TrainingSplit | "unassigned";
  spatial_group: string | null;
  image_url: string;
  review_url: string;
  revision: number;
  image_geography: { status: string; reason: string; groups: {group: string; split: string}[]; splits: string[] };
  scan: string;
  scan_count: number;
  confirmed: number;
  pending: number;
  rejected: number;
  complete: boolean;
  eligible: boolean;
  blockers: TrainingBlocker[];
}
export interface TrainingClass {
  kind: SceneKind;
  references: number;
  pending: number;
  withheld: number;
  splits: Record<TrainingSplit, TrainingCounts>;
  layers: TrainingLayer[];
}
export interface SceneTrainingCoverage {
  version: string;
  scenes: number;
  reviewed_objects: number;
  false_positives: number;
  classes: TrainingClass[];
  export_current: boolean;
  roof_feedback?: {
    targets: Record<"family" | "linework", { checked: number; withheld: number; splits: Record<TrainingSplit, { objects: number; groups: number }> }>;
    layers: { scene_id: string; object_id: string; title: string; split: TrainingSplit | "unassigned"; review_url: string;
      checked: Record<"family" | "linework", boolean>; eligible: Record<"family" | "linework", boolean>;
      blockers: Record<"family" | "linework", string[]> }[];
  };
  evaluation?: SceneEvaluation;
  latest_export: {
    version: string;
    reviewed_objects: number;
    dataset: string;
    class_exports: Partial<Record<SceneKind, number>>;
  } | null;
}
export interface EvaluationMetrics {
  predictions: number; references: number; matched: number; extra: number; missed: number;
  invalid_predictions: number; precision: number | null; recall: number | null;
}
export interface EvaluationSummary extends EvaluationMetrics {
  images: number; groups: number; empty_images: number; mean_outline_iou: number | null;
}
export interface EvaluationLayer extends EvaluationMetrics {
  scene_id: string; title: string; split: TrainingSplit; spatial_group: string;
  review_url: string; revision: number; model_signature: string; image_sha256: string;
  outline_iou: number | null; sources: string[];
}
export interface SceneEvaluation {
  schema_version: number; dataset_version: string; iou_threshold: number;
  classes: {kind: SceneKind; splits: Record<TrainingSplit, EvaluationSummary>; layers: EvaluationLayer[]}[];
}
export const SPLIT_NAMES = { train: "Training", validation: "Validation", test: "Test", unassigned: "Unassigned" };
export function layerAction(layer: TrainingLayer) {
  if (layer.blockers.includes("stale")) return "Review changed model";
  if (layer.blockers.includes("image_geography")) return "Image geography — withheld";
  if (layer.blockers.includes("split_conflict")) return "Split conflict — withheld";
  if (layer.blockers.includes("unassigned") || layer.blockers.includes("missing_group")) return "Split unresolved — withheld";
  if (layer.pending) return `${layer.pending} ${layer.pending === 1 ? "object needs" : "objects need"} review`;
  if (!layer.complete) return "Check whole image";
  return layer.confirmed ? "Ready for export" : "Checked empty image";
}
