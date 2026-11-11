import type { Feature, FeatureCollection, Polygon } from "geojson";
import type { SceneModelSnapshot } from "./scene-types";
import type { BuildingAddress } from "./building-addresses";

export const EVIDENCE_BANDS = [
  { id: "low", label: "Low evidence", color: "#df796a" },
  { id: "medium", label: "Mixed evidence", color: "#e4bd70" },
  { id: "high", label: "Stronger evidence", color: "#70d8c5" },
  { id: "unassessed", label: "Not assessed", color: "#a0b1c5" },
] as const;
export type EvidenceBand = typeof EVIDENCE_BANDS[number]["id"];
/** A same-footprint capture that scene_confidence.query() hides under this roof. Diagnostic, not accuracy. */
export interface HiddenOverlap {
  scene_id: string; object_id: string; band: EvidenceBand; detector_score: number | null;
  image_line_mismatch_m: number | null; observed_segments: number | null; clipped: boolean | null; iou: number;
}
/** Eyes sidecar rows (scripts/ml/eyes_index.py) joined read-only by scene_processing.inventory. Keys mirror the Python payload. */
export interface RoofEyes {
  /** Best tier-1/2 human reference agreement (highest IoU). Agent observations (T3) never appear here. */
  reference?: {
    tier: "T1" | "T2"; iou: number | null; hausdorff_m: number | null; roof_type_match: boolean | null;
    line_precision: number | null; line_recall: number | null; image_identity: "same_image" | "different_image" | "unknown" | null;
  };
  /** Recomputed propose_roof diagnostics for this exact model signature. */
  diagnostics?: {
    planes: number | null; crease_runs: number | null; short_junction_links: number | null; matched_edges: number | null;
    detected_segments: number | null; line_refinement_reason: string | null; crease_contrast_share: number | null;
    edge_unsupported_fraction: number | null; recomputed_matches_stored: boolean | null; error?: string;
  };
  /** Evidence rule v2 shadow band; production bands stay v1 until a CP3 decision. */
  shadow_band_v2?: EvidenceBand;
  /** Pairs from the cross_capture table; band/iou are null when the sidecar row lacks them. */
  cross_capture?: { n_pairs: number; bands: { scene_id: string; object_id: string; band: EvidenceBand | null; iou: number | null }[]; hidden_by: string[] };
}
export type ProcessingRoof = Feature<Polygon, {
  key: string; scene_id: string; object_id: string; model_signature: string;
  property_address?: BuildingAddress | null;
  label: string; capture_title: string; band: EvidenceBand; band_label: string; color: string;
  reasons: string[]; area_m2: number; detector_score: number | null; geometry_passed?: boolean; clipped?: boolean;
  image_line_mismatch_m?: number; observed_segments?: number;
  /** null when no image lines were observed: no lines means no mismatch measurement, not a zero. */
  image_line_mismatch_display?: number | null;
  hidden_overlaps?: HiddenOverlap[]; eyes?: RoofEyes;
  review_url: string; review_status: string; issue_count: number;
  /** Server-side search index; the roofs part strips it before rows reach the browser. */
  search_text?: string;
  interval: [number, number] | null; calibration_groups: number;
}>;
/** One table row: a roof without its search text. A full ProcessingRoof is always assignable to it. */
export type ProcessingRoofRow = Omit<ProcessingRoof, "properties"> & { properties: Omit<ProcessingRoof["properties"], "search_text"> };
export interface ProcessingTile {
  id: string; order: number; tile: [number, number, number]; status: string; attempts: number;
  elapsed_seconds: number | null; objects: Record<string, number>; confidence: Record<string, number>;
  error: string | null; has_capture: boolean; review_url: string | null;
}
export type ProcessingSuburb = { run: string; status: string; total: number; counts: Record<string, number>; current: string | null;
  started_at: number; updated_at: number; bounds: [number, number, number, number]; boundary: FeatureCollection; coverage: FeatureCollection };
/** Whether the route's cached full inventory exists and is not being refreshed right now. */
export type InventoryState = "ready" | "warming";
/** GET /api/scenes/processing (no part): the complete inventory. Large; the workspace no longer reads it. */
export interface ProcessingData {
  features: ProcessingRoof[]; queue: ProcessingTile[]; counts: Record<string, number>; objects: Record<string, number>;
  fetched_at: number; duplicates?: number;
  address_counts?: Record<string, number>;
  suburb: null | ProcessingSuburb;
}
/** ?part=summary: progress strip, tile queue and object totals. counts are model bands summed from completed tile checkpoints. */
export interface ProcessingSummary {
  suburb: null | ProcessingSuburb; queue: ProcessingTile[]; objects: Record<string, number>; counts: Record<string, number>;
  fetched_at: number; inventory: InventoryState;
}
/** ?part=roofs: one page of trimmed rows plus inventory-wide denominators. */
export interface ProcessingRoofsPage {
  rows: ProcessingRoofRow[]; total: number; page: number; size: number; pages: number; sort: string;
  counts: Record<string, number>; address_counts: Record<string, number>; fetched_at: number; inventory: InventoryState;
}
export interface RoofPreviewData {
  scene: SceneModelSnapshot; image_url: string;
  /** Display crop (scene pixels) and its downscaled PNG urls; the full image stays the evidence source. */
  crop?: { x: number; y: number; w: number; h: number }; crop_url?: string; detail_crop_url?: string;
}
export const reviewLabels: Record<string, string> = {
  candidate: "Unreviewed", confirmed: "Confirmed", needs_adjustment: "Needs edit", rejected: "Rejected",
  superseded: "Replaced", stale: "Feedback needs recovery",
};
export const count = (n: number) => n.toLocaleString("en-AU");
export function duration(seconds: number) {
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}
export function roofCenter(roof: ProcessingRoofRow): [number, number] {
  const ring = roof.geometry.coordinates[0];
  return [(Math.min(...ring.map(p => p[0])) + Math.max(...ring.map(p => p[0]))) / 2,
    (Math.min(...ring.map(p => p[1])) + Math.max(...ring.map(p => p[1]))) / 2];
}
