import type { TrainingSplit } from "./scene-training";

export const ROOF_FAMILIES = ["hipped", "gabled"] as const;
export type RoofRunCounts = Record<TrainingSplit, Record<(typeof ROOF_FAMILIES)[number], { roofs: number; images: number; areas: number }>>;
export type RoofRunScore = { balanced_accuracy: number; log_loss: number; ends: number };
export interface RoofTrainingRun {
  id: string;
  created_at: string;
  dataset_version: string;
  status: "needs-reviewed-labels" | "experimental-candidate";
  requirements: Record<TrainingSplit, { roofs_per_class: number; areas_per_class: number }>;
  reviewed: RoofRunCounts;
  usable: RoofRunCounts;
  test_evaluation: "not-run" | "incomplete-visible-evidence" | "frozen-weights-report-only";
  validation?: RoofRunScore;
  baseline?: RoofRunScore;
  test?: RoofRunScore;
  exclusions: { scene_id: string; object_id: string; family: string; split: "train" | "validation"; reason: string; url: string }[];
  held_out_exclusions: number;
}
export interface RoofTrainingRuns { runs: RoofTrainingRun[]; unavailable: number }

export function roofRunGaps(run: RoofTrainingRun) {
  return (["train", "validation", "test"] as const).flatMap(split => ROOF_FAMILIES.flatMap(family => {
    const assessed = split !== "test" || run.test_evaluation !== "not-run";
    const count = (assessed ? run.usable : run.reviewed)[split][family], required = run.requirements[split];
    const roofs = Math.max(0, required.roofs_per_class - count.roofs), areas = Math.max(0, required.areas_per_class - count.areas);
    return roofs || areas ? [{ split, family, roofs, areas, assessed }] : [];
  }));
}
