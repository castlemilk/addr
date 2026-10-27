/**
 * Recapture carry path: move a saved review onto a NEW image of the same place, captured at a different scale.
 *
 * A recaptured lot is a new scene (`lot-<address_id>`) with its own image hash, so nothing the earlier image showed is
 * promised to look the same in the new one, and an id both models use (a CRM seed `roof-<address_id>` both frames were
 * seeded with, for instance) names two outlines, not one. The only link between the two frames is the
 * georeferencing: the capture (scripts/ml/scene_lot.py --capture --carry-from, through
 * scripts/ml/scene_recapture.measured_transform) records `frame_transform` on the new snapshot after measuring how well
 * the two images agree (the residual is a measured agreement against pre-registered design values, never proof that
 * the pixels are identical). This module re-derives that transform from the two snapshots, re-projects the reviewer's
 * outlines and metre-frame roof edits through it and returns every carried decision for a recheck:
 *
 * - every carried object comes back `needs_adjustment` with `geometry_reviewed` and `model.height_verified` false
 *   (superseded members of a lineage set stay `superseded`, which the replacement history requires); nothing comes
 *   back `confirmed` and nothing is rejected automatically: the earlier verdict travels only as a hint, in
 *   `carried_from.status` and in the notes, where an assistant-authored review is named as an observation, never as a
 *   reviewer decision;
 * - machine-only labels and unaccepted Fieldwork imports are dropped (neither is a human decision), no provenance
 *   travels, and no `machine:*` provenance is ever written (it would hide a later human confirmation from export);
 * - every object of the new model stays visible, unchanged and under its own id, beside the carried objects for
 *   duplicate review. A carried object or group whose id the new model already uses is carried as `<id>-carried`
 *   (listed in `renamed` / `renamed_groups`; parent, group and lineage pointers follow; `carried_from.object_id` keeps
 *   the source id). `carried_from.matched_id` names the object of the same kind in the new model, a same-id one
 *   included, that agrees best (IoU >= design.match_iou).
 *
 * What is checked here and what is not: this module recomputes `metre_offset`, `pixel_affine` and `overlap.fraction`
 * from the two snapshots and checks the recorded names, sizes, registration and verdict, and the recorded residual
 * against the recorded design values. It reads and writes no files, so whether the recorded design is the
 * pre-registered DESIGN.json (by its sha) and whether the residual re-measures from the two images' bytes are the
 * Python verifier's checks (scripts.ml.scene_recapture --verify-source --verify-target): the caller that loads the
 * snapshots must run it and refuse the carry when it fails. Every figure produced here is agreement between two
 * frames, never a claim about the ground.
 */
import type { SceneKind, SceneModelSnapshot, SceneObject, ScenePoint, SceneReview } from "./scene-types";

/** `frame-transform-v1`, recorded on the new snapshot at capture (scene_recapture.measured_transform); declared here until scene-types carries it. */
export interface FrameTransform {
  version: "frame-transform-v1";
  method: "georeferenced-affine";
  source_scene_id: string;
  source_model_signature: string;
  source_image_sha256: string;
  source_size: [number, number];
  source_registration: "fitted" | "region";
  /** The source centroid in the target's local metres: source metres + metre_offset = target metres. */
  metre_offset: [number, number];
  /** Source pixel -> target pixel in the [[a, b], [c, d], [tx, ty]] layout of pixel_to_meters. */
  pixel_affine: [[number, number], [number, number], [number, number]];
  overlap: { source_px_inside_target: number; fraction: number };
  residual: { method: string; warped_from: string; shift_px: [number, number]; shift_m: number; ncc: number; samples: number };
  design: { max_shift_m: number; min_ncc: number; min_overlap_fraction: number; match_iou: number; close_iou: number; design_sha256: string };
  verified: boolean;
}
/** Where a carried object came from; `status` and `geometry_reviewed` are the earlier verdict, a hint and never a label. */
export interface CarriedFrom {
  scene_id: string;
  model_signature: string;
  object_id: string;
  status: SceneObject["status"];
  geometry_reviewed: boolean;
  iou_with_new: number;
  matched_id: string | null;
  method: "georeferenced-affine";
}
export type RecapturedSnapshot = SceneModelSnapshot & { frame_transform?: FrameTransform };
export type CarriedSceneObject = SceneObject & { carried_from?: CarriedFrom };
export type CarryDropReason = "outside_new_frame" | "machine_only" | "fieldwork_pending" | "too_small_in_new_frame" |
  "split_by_new_frame_edge" | "too_many_points_in_new_frame" | "invalid_in_new_frame";
export interface CarryDrop { object_id: string; kind: SceneKind; status: SceneObject["status"]; reason: CarryDropReason }
/** A source object or group id the new model already uses, and the id it is carried under instead. */
export interface CarryRename { id: string; carried_as: string }
/** The shape of recoverSceneReview's result, plus what the carry left behind, which outlines the new image edge cut
 *  and which ids it had to change. */
export interface FrameCarryRecovery {
  review: SceneReview;
  carried_objects: number;
  new_candidates: number;
  method: "georeferenced-affine";
  /** Source objects left behind (by source id), each with its reason. */
  dropped: CarryDrop[];
  /** Carried outlines the new image edge cut, by their id in the new review. */
  clipped: string[];
  renamed: CarryRename[];
  renamed_groups: CarryRename[];
}
export type FrameCarryResult = { recovery: FrameCarryRecovery; reason: null } | { recovery: null; reason: string };

type Ring = ScenePoint[];
type Matrix = number[][];
type Loose = Record<string, unknown>;
type Outline = { ring: Ring; holes?: Ring[] };

const WORLD = 40075016.68557849;
const AGREE = 1e-6; // recorded against recomputed transform values
const EDGE = 1e-7; // px: a coordinate this close to the new frame edge is placed on it
// The limits scene-store validateSceneReview (and scene-types polygonIssues) enforce on a saved review.
const MAX_NOTES = 5000, MAX_OBJECTS = 1000, MAX_GROUPS = 250, MAX_OUTLINE_POINTS = 2000, MAX_ID = 100;
// An assistant-authored review (r5_precision.AGENT_NOTE, scene_recapture.AGENT_NOTES): an observation, never a decision.
const AGENT_NOTES = /^Assistant (visually traced|image review)/i;
const SHA256 = /^[a-f0-9]{64}$/;

const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isPair = (v: unknown): v is [number, number] => Array.isArray(v) && v.length === 2 && v.every(finite);
const isFrame = (m: unknown): m is Matrix => Array.isArray(m) && m.length === 3 && m.every(isPair);
const record = (v: unknown): v is Loose => !!v && typeof v === "object" && !Array.isArray(v);
const det = (m: Matrix) => m[0][0] * m[1][1] - m[0][1] * m[1][0];
const agree = (a: readonly number[], b: readonly number[]) => a.length === b.length && a.every((v, i) => finite(v) && Math.abs(v - b[i]) <= AGREE);
const apply = (m: Matrix, p: readonly number[]): ScenePoint => [p[0] * m[0][0] + p[1] * m[1][0] + m[2][0], p[0] * m[0][1] + p[1] * m[1][1] + m[2][1]];
const mercatorY = (lat: number) => Math.asinh(Math.tan(lat * Math.PI / 180)) / (2 * Math.PI);
const area = (r: Ring) => Math.abs(r.reduce((s, p, i) => { const q = r[(i + 1) % r.length]; return s + p[0] * q[1] - q[0] * p[1]; }, 0)) / 2;
const outlineArea = (o: Outline) => area(o.ring) - (o.holes || []).reduce((s, h) => s + area(h), 0);
const round2 = (n: number) => Math.round(n * 100) / 100; // scene-types footprintMetrics rounding

/** The source centroid in the target's local Web-Mercator metres, scaled by the cosine of the target latitude: the
 *  projection of scripts/ml/scene_addresses.image_projection and parcelImagePoint (property-parcels.ts), before the
 *  target's meters_to_pixel. */
export function metreOffset(source: { lon: number; lat: number }, target: { lon: number; lat: number }): [number, number] {
  const scale = WORLD * Math.cos(target.lat * Math.PI / 180);
  return [(source.lon - target.lon) / 360 * scale, (mercatorY(source.lat) - mercatorY(target.lat)) * scale];
}

/** target.meters_to_pixel o (+offset) o source.pixel_to_meters as one affine in the pixel_to_meters layout. */
export function pixelAffine(sourcePixelToMeters: Matrix, offset: readonly number[], targetMetersToPixel: Matrix): FrameTransform["pixel_affine"] {
  const a = sourcePixelToMeters, b = targetMetersToPixel, x = a[2][0] + offset[0], y = a[2][1] + offset[1];
  return [
    [a[0][0] * b[0][0] + a[0][1] * b[1][0], a[0][0] * b[0][1] + a[0][1] * b[1][1]],
    [a[1][0] * b[0][0] + a[1][1] * b[1][0], a[1][0] * b[0][1] + a[1][1] * b[1][1]],
    [x * b[0][0] + y * b[1][0] + b[2][0], x * b[0][1] + y * b[1][1] + b[2][1]],
  ];
}

/** Share of the source frame's pixels that land inside the target frame (scene_recapture.overlap, unrounded). */
function overlapFraction(affine: Matrix, source: SceneModelSnapshot, width: number, height: number): number {
  const w = source.image_width, h = source.image_height, scale = Math.abs(det(affine));
  const corners: Ring = [[0, 0], [w, 0], [w, h], [0, h]];
  const inside = clipToFrame(corners.map(p => apply(affine, p)), width, height);
  return inside.length >= 3 && scale > 0 ? Math.min(1, area(inside) / scale / (w * h)) : 0;
}

/** scene_lot_tags.registration: 'region' for a registered tile capture, 'fitted' for a cohort frame fitted from the CRM roof. */
const registrationOf = (s: SceneModelSnapshot) => { const b = s.region?.bounds; return Array.isArray(b) && b.length > 0 ? "region" : "fitted"; };

const framed = (s: SceneModelSnapshot) => !!s && isFrame(s.pixel_to_meters) && isFrame(s.meters_to_pixel) &&
  Math.abs(det(s.pixel_to_meters)) > 1e-12 && Math.abs(det(s.meters_to_pixel)) > 1e-12 && !!s.centroid && finite(s.centroid.lon) &&
  finite(s.centroid.lat) && Number.isSafeInteger(s.image_width) && Number.isSafeInteger(s.image_height) && s.image_width > 0 && s.image_height > 0;

function checkFrameTransform(previous: SceneModelSnapshot, current: RecapturedSnapshot): { transform: FrameTransform } | { issue: string } {
  const t = current?.frame_transform as Partial<FrameTransform> | undefined;
  if (!record(t)) return { issue: "the new snapshot records no frame transform" };
  if (t.version !== "frame-transform-v1" || t.method !== "georeferenced-affine")
    return { issue: "the frame transform is not a frame-transform-v1 georeferenced affine" };
  if (t.source_scene_id !== previous.id) return { issue: "the frame transform names another source scene" };
  if (t.source_model_signature !== previous.model_signature) return { issue: "the frame transform names another snapshot of the source scene" };
  if (t.source_image_sha256 !== previous.image_sha256) return { issue: "the source image differs from the image the frame transform was measured on" };
  if (!isPair(t.source_size) || t.source_size[0] !== previous.image_width || t.source_size[1] !== previous.image_height)
    return { issue: "the source image size differs from the frame transform" };
  if (t.source_registration !== registrationOf(previous)) return { issue: "the frame transform names another registration than the source snapshot's" };
  if (!framed(previous) || !framed(current)) return { issue: "a snapshot has no finite coordinate frame" };
  const d = t.design;
  if (!record(d) || ![d.max_shift_m, d.min_ncc, d.min_overlap_fraction, d.match_iou, d.close_iou].every(finite) ||
      !(d.match_iou > 0 && d.match_iou <= d.close_iou && d.close_iou <= 1) || typeof d.design_sha256 !== "string" || !SHA256.test(d.design_sha256))
    return { issue: "the frame transform carries no usable design values" };
  if (t.verified !== true) return { issue: "the residual check did not pass" };
  const o = t.overlap, r = t.residual;
  if (!record(o) || !record(r) || !finite(o.fraction) || !finite(r.shift_m) || !finite(r.ncc) ||
      !(o.fraction >= d.min_overlap_fraction && r.shift_m <= d.max_shift_m && r.ncc >= d.min_ncc))
    return { issue: "the recorded residual does not meet the design values" };
  const offset = metreOffset(previous.centroid, current.centroid);
  if (!isPair(t.metre_offset) || !agree(t.metre_offset, offset)) return { issue: "the recorded metre offset does not agree with the two centroids" };
  const affine = pixelAffine(previous.pixel_to_meters, offset, current.meters_to_pixel);
  if (!isFrame(t.pixel_affine) || !t.pixel_affine.every((row, i) => agree(row, affine[i])))
    return { issue: "the recorded pixel affine does not agree with the two frames" };
  if (!agree([o.fraction], [overlapFraction(affine, previous, current.image_width, current.image_height)]))
    return { issue: "the recorded overlap does not agree with the two frames" };
  // Project with the recomputed values: they agree with the recorded ones to 1e-6 and are exactly consistent with each
  // other, so a roof line re-anchored in metres still lies inside its outline re-projected in pixels.
  return { transform: { ...(t as FrameTransform), metre_offset: offset, pixel_affine: affine } };
}

/** The first check a recorded frame transform fails, named for recovery_reason; null when it holds. Null is necessary,
 *  not sufficient: the DESIGN.json sha and the residual re-measured from the image bytes are the Python verifier's
 *  checks (scripts.ml.scene_recapture --verify-source --verify-target), and the carry is refused when it fails. */
export function frameTransformIssue(previous: SceneModelSnapshot, current: RecapturedSnapshot): string | null {
  const check = checkFrameTransform(previous, current);
  return "issue" in check ? check.issue : null;
}

/** The recorded transform when it names this source snapshot, passed its residual check and agrees (to 1e-6) with the
 *  offset, affine and overlap recomputed from the two snapshots' centroids and frames; the returned numbers are the
 *  recomputed ones. The pixel verifier's checks still apply (frameTransformIssue). */
export function frameTransform(previous: SceneModelSnapshot, current: RecapturedSnapshot): FrameTransform | null {
  const check = checkFrameTransform(previous, current);
  return "transform" in check ? check.transform : null;
}

// Every coordinate-bearing object field found in data/scenes/*/{latest,review}.json on 2026-09-30, by frame. Frame-free
// fields stay as they are: ring_lonlat / line_lonlat (WGS84), heights (ring_z, z, z_range, line_z), face and edge
// indices, and outline directions (both frames are north-up).
/** Image-pixel geometry, mapped through pixel_affine ("[]" walks an array). */
const PIXEL_PATHS: readonly (readonly string[])[] = [
  ["ring"], ["holes", "[]"],
  ["roof_feedback", "outline", "ring"], ["roof_feedback", "outline", "holes", "[]"],
  ["roof_line_edits", "outline", "ring"], ["roof_line_edits", "outline", "holes", "[]"],
  ["boundary_refinement", "raw_ring"], ["boundary_refinement", "raw_holes", "[]"],
  ["boundary_refinement", "assembly", "components", "[]", "ring"], ["boundary_refinement", "assembly", "components", "[]", "holes", "[]"],
  ["boundary_refinement", "construction", "inferred_edges", "[]", "line_pixels"],
  ["roof_parts", "[]", "ring_pixels"], ["roof_lines", "[]", "line_pixels"],
];
/** Local-metre geometry, translated by metre_offset (a third coordinate, z, is kept). */
const METRE_PATHS: readonly (readonly string[])[] = [
  ["roof_annotation", "vertices"], ["roof_lines", "[]", "line_meters"],
  ["roof_parts", "[]", "ring_meters"], ["roof_parts", "[]", "ring"], ["roof_parts", "[]", "coords"], ["roof_parts", "[]", "triangles", "[]"],
  ["roof_parts", "[]", "gable_ends", "[]"],
  ["roof_line_edits", "vertices"], ["roof_line_edits", "baseline", "vertices"], ["roof_feedback", "linework", "vertices"],
  ["roof_form_comparison", "alternative", "annotation", "vertices"], ["roof_form_comparison", "alternative", "parts", "[]", "ring_meters"],
];
/** Roof planes z = a·x + b·y + c in local metres: moving the frame by (dx, dy) changes only c. */
const PLANE_PATHS: readonly (readonly string[])[] = [["roof_parts", "[]", "plane"], ["roof_form_comparison", "alternative", "parts", "[]", "plane"]];

function mapAt(node: unknown, keys: readonly string[], map: (value: unknown) => unknown): void {
  const [key, ...rest] = keys;
  if (key === "[]") {
    if (Array.isArray(node)) node.forEach((item, i) => { if (rest.length) mapAt(item, rest, map); else node[i] = map(item); });
  } else if (record(node) && key in node) {
    if (rest.length) mapAt(node[key], rest, map); else node[key] = map(node[key]);
  }
}

/**
 * Re-express one object in the target frame: pixel fields (ring, holes, the outline snapshots of roof_feedback and
 * roof_line_edits, boundary_refinement.raw_* and its other pixel geometry, roof_parts.ring_pixels,
 * roof_lines.line_pixels, clipped_by_frame) through pixel_affine, and model.footprint_area_px by the affine's area
 * factor; metre fields (roof_line_edits vertices and baseline, roof_annotation.vertices, roof_parts
 * ring_meters|ring|coords|triangles|gable_ends, roof_lines.line_meters, roof_feedback.linework,
 * roof_form_comparison.alternative) by +metre_offset with z unchanged, and roof planes by c - a·dx - b·dy. The other
 * model dimensions are in metres and frame-free (the store's footprintMetrics recomputes every one of them on save).
 * roof_optical_guidance is removed: its observation record holds the source image's own meters_to_pixel and tile grid,
 * which no affine re-expresses. Statuses and decisions are untouched here; the input is not mutated.
 */
export function projectSceneObject<T extends SceneObject>(object: T, transform: Pick<FrameTransform, "pixel_affine" | "metre_offset">): T {
  const m: Matrix = transform.pixel_affine, dx = transform.metre_offset[0], dy = transform.metre_offset[1];
  const copy = structuredClone(object), node = copy as unknown as Loose;
  const toPixels = (r: unknown) => Array.isArray(r) ? r.map(p => isPair(p) ? apply(m, p) : p) : r;
  const toMetres = (r: unknown) => Array.isArray(r)
    ? r.map(p => Array.isArray(p) && p.length >= 2 && finite(p[0]) && finite(p[1]) ? [p[0] + dx, p[1] + dy, ...p.slice(2)] : p) : r;
  const toPlane = (plane: unknown) => record(plane) && finite(plane.a) && finite(plane.b) && finite(plane.c)
    ? { ...plane, c: plane.c - plane.a * dx - plane.b * dy } : plane;
  for (const keys of PIXEL_PATHS) mapAt(node, keys, toPixels);
  for (const keys of METRE_PATHS) mapAt(node, keys, toMetres);
  for (const keys of PLANE_PATHS) mapAt(node, keys, toPlane);
  const cut = node.clipped_by_frame;
  if (Array.isArray(cut) && cut.length === 4 && cut.every(finite)) {
    const corners = [[cut[0], cut[1]], [cut[2], cut[1]], [cut[2], cut[3]], [cut[0], cut[3]]].map(p => apply(m, p));
    const xs = corners.map(p => p[0]), ys = corners.map(p => p[1]);
    node.clipped_by_frame = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  const model = node.model;
  if (record(model) && finite(model.footprint_area_px)) model.footprint_area_px = round2(model.footprint_area_px * Math.abs(det(m)));
  delete node.roof_optical_guidance;
  return copy;
}

/** The ring checks of scene-types polygonIssues (hole contacts aside), so nothing carried fails the store's validation. */
function ringIssue(ring: unknown, width: number, height: number, minimumArea: number): "points" | "outside" | "area" | "duplicate" | "crossing" | null {
  if (!Array.isArray(ring) || ring.length < 3 || ring.length > 200 || !ring.every(isPair)) return "points";
  const r = ring as Ring;
  if (r.some(p => p[0] < 0 || p[1] < 0 || p[0] > width || p[1] > height)) return "outside";
  if (area(r) < minimumArea) return "area";
  if (r.some((p, i) => r.some((q, j) => j > i && Math.hypot(p[0] - q[0], p[1] - q[1]) < 1e-6))) return "duplicate";
  const turn = (a: ScenePoint, b: ScenePoint, c: ScenePoint) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const onSegment = (a: ScenePoint, b: ScenePoint, p: ScenePoint) => Math.abs(turn(a, b, p)) < 1e-8 &&
    p[0] >= Math.min(a[0], b[0]) - 1e-8 && p[0] <= Math.max(a[0], b[0]) + 1e-8 && p[1] >= Math.min(a[1], b[1]) - 1e-8 && p[1] <= Math.max(a[1], b[1]) + 1e-8;
  for (let i = 0; i < r.length; i++)
    for (let j = i + 2; j < r.length; j++) {
      if (i === 0 && j === r.length - 1) continue;
      const a = r[i], b = r[(i + 1) % r.length], c = r[j], d = r[(j + 1) % r.length];
      if ((turn(a, b, c) * turn(a, b, d) < -1e-8 && turn(c, d, a) * turn(c, d, b) < -1e-8) ||
          onSegment(a, b, c) || onSegment(a, b, d) || onSegment(c, d, a) || onSegment(c, d, b)) return "crossing";
    }
  return null;
}

/** Sutherland-Hodgman against the frame rectangle; a concave outline the edge splits comes back with bridge edges,
 *  which ringIssue then refuses. */
function clipToFrame(ring: Ring, width: number, height: number): Ring {
  let out = ring;
  const planes: [0 | 1, number, boolean][] = [[0, 0, false], [0, width, true], [1, 0, false], [1, height, true]];
  for (const [axis, limit, below] of planes) {
    const input = out, inside = (p: ScenePoint) => below ? p[axis] <= limit : p[axis] >= limit;
    const cut = (a: ScenePoint, b: ScenePoint): ScenePoint => {
      const t = (limit - a[axis]) / (b[axis] - a[axis]), other = a[1 - axis] + t * (b[1 - axis] - a[1 - axis]);
      return axis === 0 ? [limit, other] : [other, limit];
    };
    out = [];
    input.forEach((point, i) => {
      const before = input[(i + input.length - 1) % input.length];
      if (inside(point)) { if (!inside(before)) out.push(cut(before, point)); out.push(point); }
      else if (inside(before)) out.push(cut(before, point));
    });
  }
  return out;
}

/** Remove the repeated points and the straight-run midpoints along a frame edge that clipping leaves. */
function tidy(ring: Ring, width: number, height: number): Ring {
  const out: Ring = [];
  const same = (p: ScenePoint, q: ScenePoint) => Math.hypot(p[0] - q[0], p[1] - q[1]) < 1e-6;
  for (const p of ring) if (!out.length || !same(p, out[out.length - 1])) out.push(p);
  while (out.length > 1 && same(out[0], out[out.length - 1])) out.pop();
  const lines: [0 | 1, number][] = [[0, 0], [0, width], [1, 0], [1, height]];
  const between = (a: ScenePoint, b: ScenePoint, c: ScenePoint) => lines.some(([axis, v]) =>
    a[axis] === v && b[axis] === v && c[axis] === v && (a[1 - axis] - b[1 - axis]) * (c[1 - axis] - b[1 - axis]) < 0);
  for (let i = 0; out.length > 3 && i < out.length;) {
    if (between(out[(i + out.length - 1) % out.length], out[i], out[(i + 1) % out.length])) out.splice(i, 1); else i++;
  }
  return out;
}

type Fitted = { object: SceneObject; clipped: boolean; notes: string[] };
/** The new image: its size in pixels and the area of one of its pixels in square metres. */
type Frame = { width: number; height: number; pixelArea: number };

/** Place a projected object in the new image: whole, clipped at its edge (noted), or dropped with a reason. Outline
 *  snapshots that no longer fit take their feedback back to the earlier scene (noted), never invalid into this one. */
function fitToFrame(o: SceneObject, frame: Frame): Fitted | CarryDropReason {
  const { width, height } = frame;
  const onEdge = (v: number, limit: number) => Math.abs(v) <= EDGE ? 0 : Math.abs(v - limit) <= EDGE ? limit : v;
  const snap = (r: Ring): Ring => r.map(p => [onEdge(p[0], width), onEdge(p[1], height)]);
  const notes: string[] = [];
  let ring = snap(o.ring), holes = (o.holes || []).map(snap), clipped = false;
  if (ring.some(p => p[0] < 0 || p[1] < 0 || p[0] > width || p[1] > height)) {
    // Clipping puts every cut point on the edge; snapping once more removes the rounding that can leave one a hair outside.
    const cut = tidy(snap(clipToFrame(ring, width, height)), width, height);
    if (cut.length < 3) return "outside_new_frame";
    const issue = ringIssue(cut, width, height, 1);
    if (issue === "area") return "too_small_in_new_frame";
    if (issue === "points") return "too_many_points_in_new_frame";
    if (issue === "crossing") return "split_by_new_frame_edge";
    if (issue) return "invalid_in_new_frame";
    const inner = holes.filter(h => h.every(p => p[0] > 0 && p[1] > 0 && p[0] < width && p[1] < height));
    if (inner.length < holes.length) notes.push("An interior gap crossing the new image edge was removed.");
    ring = cut; holes = inner; clipped = true;
    notes.push("The new image edge cuts this outline, so it was clipped to the frame.");
  } else {
    const issue = ringIssue(ring, width, height, 1);
    if (issue === "area") return "too_small_in_new_frame";
    if (issue) return "invalid_in_new_frame";
  }
  const kept = holes.filter(h => !ringIssue(h, width, height, 1e-4));
  if (kept.length < holes.length) notes.push("An interior gap too small for the new image was removed.");
  const keptArea = outlineArea({ ring, holes: kept });
  if (keptArea < 1) return "too_small_in_new_frame";
  if (ring.length + kept.reduce((n, h) => n + h.length, 0) > MAX_OUTLINE_POINTS) return "too_many_points_in_new_frame";
  const object = { ...o, ring, ...(o.holes ? { holes: kept } : {}) } as SceneObject & Loose;
  // An outline the edge cut, or one that lost a gap, is no longer the projected one: its two areas follow what was kept.
  // Its width, length and rotation still describe the projected outline until the store's footprintMetrics recomputes
  // every dimension on save.
  if (clipped || kept.length < (o.holes || []).length)
    object.model = { ...object.model, footprint_area_px: round2(keptArea), area_m2: round2(keptArea * frame.pixelArea) };
  const fits = (outline: unknown) => record(outline) && !ringIssue(Array.isArray(outline.ring) ? snap(outline.ring as Ring) : null, width, height, 1) &&
    Array.isArray(outline.holes) && outline.holes.length <= 32 && outline.holes.every(h => Array.isArray(h) && !ringIssue(snap(h as Ring), width, height, 1e-4));
  const edits = object.roof_line_edits;
  if (edits) {
    if (fits(edits.outline)) object.roof_line_edits = { ...edits, outline: { ring: snap(edits.outline.ring), holes: edits.outline.holes.map(snap) } };
    else { delete object.roof_line_edits; notes.push("Its roof-line edits stayed with the earlier scene: their captured outline does not fit the new image."); }
  }
  const feedback = object.roof_feedback;
  if (feedback) {
    if (fits(feedback.outline)) object.roof_feedback = { ...feedback, outline: { ring: snap(feedback.outline.ring), holes: feedback.outline.holes.map(snap) } };
    else {
      delete object.roof_feedback;
      notes.push(feedback.family ? `Its roof family (${feedback.family}) stayed with the earlier scene as a hint: its captured outline does not fit the new image.`
        : "Its roof feedback stayed with the earlier scene: its captured outline does not fit the new image.");
    }
  }
  return { object, clipped, notes };
}

function bounds(r: Ring) {
  const xs = r.map(p => p[0]), ys = r.map(p => p[1]);
  return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
}
/** Sorted heights where a vertical line at x crosses the rings; consecutive pairs bound the covered spans (even-odd). */
function spans(rings: Ring[], x: number): number[] {
  const ys: number[] = [];
  for (const r of rings) r.forEach((p, i) => {
    const q = r[(i + 1) % r.length];
    if ((p[0] < x) !== (q[0] < x)) ys.push(p[1] + (x - p[0]) * (q[1] - p[1]) / (q[0] - p[0]));
  });
  return ys.sort((a, b) => a - b);
}
function sharedLength(a: number[], b: number[]) {
  let total = 0;
  for (let i = 0; i + 1 < a.length; i += 2)
    for (let j = 0; j + 1 < b.length; j += 2) total += Math.max(0, Math.min(a[i + 1], b[j + 1]) - Math.max(a[i], b[j]));
  return total;
}
function crossingX(p: ScenePoint, q: ScenePoint, s: ScenePoint, t: ScenePoint): number | null {
  const rx = q[0] - p[0], ry = q[1] - p[1], sx = t[0] - s[0], sy = t[1] - s[1], den = rx * sy - ry * sx;
  if (den === 0) return null;
  const u = ((s[0] - p[0]) * sy - (s[1] - p[1]) * sx) / den, v = ((s[0] - p[0]) * ry - (s[1] - p[1]) * rx) / den;
  return u >= 0 && u <= 1 && v >= 0 && v <= 1 ? p[0] + u * rx : null;
}

/**
 * Intersection over union of two outlines (outer ring and gaps), exact for non-convex shapes: between consecutive
 * vertex and edge-crossing x positions no edge ends or crosses another, so the shared height is linear in x and its
 * value at the slab midpoint integrates exactly. A measure of agreement between two outlines, nothing more.
 */
export function outlineIoU(a: Outline, b: Outline): number {
  const areaA = outlineArea(a), areaB = outlineArea(b);
  if (!(areaA > 0) || !(areaB > 0)) return 0;
  const [ax0, ay0, ax1, ay1] = bounds(a.ring), [bx0, by0, bx1, by1] = bounds(b.ring);
  const lo = Math.max(ax0, bx0), hi = Math.min(ax1, bx1);
  if (!(lo < hi) || Math.max(ay0, by0) >= Math.min(ay1, by1)) return 0;
  const ringsA = [a.ring, ...(a.holes || [])], ringsB = [b.ring, ...(b.holes || [])];
  const edges = (rings: Ring[]) => rings.flatMap(r => r.map((p, i): [ScenePoint, ScenePoint] => [p, r[(i + 1) % r.length]]));
  const xs = [lo, hi];
  for (const r of [...ringsA, ...ringsB]) for (const p of r) if (p[0] > lo && p[0] < hi) xs.push(p[0]);
  const edgesB = edges(ringsB);
  for (const [p, q] of edges(ringsA)) for (const [s, t] of edgesB) {
    const x = crossingX(p, q, s, t);
    if (x !== null && x > lo && x < hi) xs.push(x);
  }
  xs.sort((x, y) => x - y);
  let shared = 0;
  for (let i = 1; i < xs.length; i++) {
    const step = xs[i] - xs[i - 1];
    if (step > 0) shared += step * sharedLength(spans(ringsA, xs[i - 1] + step / 2), spans(ringsB, xs[i - 1] + step / 2));
  }
  const union = areaA + areaB - shared;
  return union > 0 ? Math.min(1, Math.max(0, shared / union)) : 0;
}

// The edit-comparison serializer of scene-feedback.ts (`const feedback`, L4), re-implemented with the same key set so
// the carry picks exactly the objects recoverSceneReview would; carried_from is the key the wiring adds to it.
const editKey = (o: SceneObject) => JSON.stringify({
  kind: o.kind, label: o.label, ring: o.ring, holes: o.holes || [], status: o.status,
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
  carried_from: (o as CarriedSceneObject).carried_from || null,
});
const machineOnly = (o: SceneObject) => {
  const p = (o as SceneObject & { provenance?: { source?: unknown; human_confirmed?: unknown } }).provenance;
  return typeof p?.source === "string" && p.source.startsWith("machine:") && !p.human_confirmed;
};
const fieldworkPending = (o: SceneObject) => (o as SceneObject & { fieldwork_import?: { pending?: unknown } }).fieldwork_import?.pending === true;
const links = (o: SceneObject | undefined) => o ? [...(o.derived_from || []), ...(o.replacement_ids || [])] : [];

/** Connected derived_from / replacement_ids sets among the chosen objects (single objects are not sets). */
function lineageSets(ids: Set<string>, reviewed: Map<string, SceneObject>): string[][] {
  const parent = new Map([...ids].map(id => [id, id]));
  const root = (id: string) => { let r = id; while (parent.get(r) !== r) r = parent.get(r)!; return r; };
  for (const id of ids) for (const link of links(reviewed.get(id))) if (parent.has(link)) parent.set(root(link), root(id));
  const sets = new Map<string, string[]>();
  for (const id of ids) sets.set(root(id), [...(sets.get(root(id)) || []), id]);
  return [...sets.values()].filter(set => set.length > 1).map(set => set.sort());
}

/** `<id>-carried` (then `-carried-2`, ...), shortened to the store's id length limit, not yet in `taken` (now added). */
function distinctId(id: string, taken: Set<string>): string {
  for (let n = 1; ; n++) {
    const suffix = n === 1 ? "-carried" : `-carried-${n}`, candidate = `${id.slice(0, MAX_ID - suffix.length)}${suffix}`;
    if (!taken.has(candidate)) { taken.add(candidate); return candidate; }
  }
}

/** The carry's two sentences for one object: the lead (where it came from, what the earlier notes record and the ask
 *  to check it again) and its agreement with the new model. */
function carrySentences(sceneId: string, before: SceneObject, iou: number, matched: string | null, design: FrameTransform["design"]) {
  const agent = AGENT_NOTES.test(before.notes || "");
  const verdict = { confirmed: "confirmed it", rejected: "rejected it", needs_adjustment: "marked it for adjustment",
    superseded: "replaced it with split or traced outlines", candidate: "edited it without a decision" }[before.status] || "edited it";
  const earlier = agent ? `the earlier notes record an assistant observation (status ${before.status}), not a reviewer decision`
    : `the earlier review ${verdict}${before.geometry_reviewed ? " with its outline checked" : ""} on the earlier image`;
  const ask = before.status === "superseded" ? "It stays replaced by the carried outlines that took it over."
    : agent ? "That observation is a hint for this image, not a decision; check the outline and decide."
      : "That verdict is a hint for this image, not a decision; recheck the outline and decide again.";
  const agreement = matched
    ? `The new model's ${matched} ${iou >= design.close_iou ? "agrees closely with this outline" : "overlaps this outline"} (IoU ${iou.toFixed(2)}).`
    : `No object of this kind in the new model overlaps it by IoU ${design.match_iou} or more (best ${iou.toFixed(2)}).`;
  return { lead: `Carried from ${sceneId} through the georeferenced frame: ${earlier}. ${ask}`, agreement };
}
/**
 * The reviewer's notes first and whole, then the carry's sentences in order of importance, each only while the whole
 * stays within the store's limit: the first sentence that does not fit ends the list, so the agreement goes first,
 * then the notes about the fit, then the lead, and the reviewer's words are never cut for machine text. Only notes
 * that alone exceed the limit (never a validated review) are cut, and then with an explicit marker.
 */
function joinNotes(existing: string | undefined, sentences: string[]): string {
  const marker = " [notes truncated at carry]", own = existing || "";
  const head = own.length > MAX_NOTES ? `${own.slice(0, MAX_NOTES - marker.length)}${marker}` : own;
  let tail = "";
  for (const sentence of sentences) {
    const next = tail ? `${tail} ${sentence}` : sentence;
    if ((head ? head.length + 1 : 0) + next.length > MAX_NOTES) break;
    tail = next;
  }
  return head && tail ? `${head}\n${tail}` : head || tail;
}

/**
 * Carry a saved review of the source scene onto a recaptured snapshot, or refuse with the reason naming the failed step.
 * Carried: every object whose edit serialization differs from the source model object (an outline the reviewer drew is
 * absent from the source model, so this covers it), with lineage sets (derived_from / replacement_ids / superseded
 * members) closed over and carried whole, dropped whole when they lie wholly outside the new image, or refused. Every
 * object of the new model stays, unchanged; a carried object or group id the new model already uses is carried as
 * `<id>-carried`. Parents and groups keep pointing at carried objects and are re-pointed through matched_id when the
 * object they named stayed behind. `options.revision` is the target scene's current review revision (0: a new lot-*
 * scene has no review yet).
 */
export function carryAcrossFramesResult(previous: SceneModelSnapshot, current: RecapturedSnapshot, saved: SceneReview,
  options: { revision?: number } = {}): FrameCarryResult {
  const refuse = (reason: string): FrameCarryResult => ({ recovery: null, reason });
  if (!saved || !Array.isArray(saved.objects) || previous.model_signature !== saved.model_signature)
    return refuse("the saved review belongs to another snapshot of the source scene");
  if (![previous.objects, previous.groups, current?.objects, current?.groups, saved.groups].every(Array.isArray))
    return refuse("a snapshot or the saved review has no object or group list");
  const check = checkFrameTransform(previous, current);
  if ("issue" in check) return refuse(check.issue);
  const transform = check.transform, design = transform.design;
  const frame: Frame = { width: current.image_width, height: current.image_height, pixelArea: Math.abs(det(current.pixel_to_meters)) };
  const originals = new Map(previous.objects.map(o => [o.id, o])), reviewed = new Map(saved.objects.map(o => [o.id, o]));

  const chosen = new Set(saved.objects.filter(o => {
    const original = originals.get(o.id);
    return !original || editKey(o) !== editKey(original);
  }).map(o => o.id));
  const queue = [...chosen];
  while (queue.length) for (const id of links(reviewed.get(queue.pop()!))) if (reviewed.has(id) && !chosen.has(id)) { chosen.add(id); queue.push(id); }

  const dropped = new Map<string, CarryDropReason>(), fitted = new Map<string, Fitted>();
  for (const id of chosen) {
    const o = reviewed.get(id)!, authority = machineOnly(o) ? "machine_only" : fieldworkPending(o) ? "fieldwork_pending" : null;
    const fit = authority || fitToFrame(projectSceneObject(o, transform), frame);
    if (typeof fit === "string") dropped.set(id, fit); else fitted.set(id, fit);
  }
  for (const set of lineageSets(chosen, reviewed)) {
    const lost = set.filter(id => dropped.has(id));
    if (!lost.length || (lost.length === set.length && lost.every(id => dropped.get(id) === "outside_new_frame"))) continue;
    return refuse(`lineage set ${set.join(", ")} cannot be carried whole: ${lost.map(id => `${id} is ${dropped.get(id)}`).join(", ")}`);
  }

  // Two images never share pixels, so an id both models use (the CRM seed roof-<address_id> both frames were seeded
  // with, for instance) names two outlines: the new model's object keeps its id and the carried one takes another.
  const fresh = current.objects, freshById = new Map(fresh.map(o => [o.id, o]));
  const taken = new Set([...freshById.keys(), ...fitted.keys()]), carriedIds = new Map<string, string>(), renamed: CarryRename[] = [];
  for (const id of fitted.keys()) {
    const as = freshById.has(id) ? distinctId(id, taken) : id;
    carriedIds.set(id, as);
    if (as !== id) renamed.push({ id, carried_as: as });
  }
  const carriedId = (id: string) => carriedIds.get(id) ?? id;
  const matches = new Map<string, { id: string | null; iou: number }>();
  const counterpart = (id: string) => {
    let best = matches.get(id);
    if (best) return best;
    best = { id: null, iou: 0 };
    const known = fitted.get(id), stayed = known ? null : reviewed.get(id) || originals.get(id);
    const placed = known || (stayed ? fitToFrame(projectSceneObject(stayed, transform), frame) : null);
    if (placed && typeof placed !== "string")
      for (const o of fresh) if (o.kind === placed.object.kind) { const iou = outlineIoU(placed.object, o); if (iou > best.iou) best = { id: o.id, iou }; }
    matches.set(id, best);
    return best;
  };
  const matchedId = (id: string) => { const m = counterpart(id); return m.id !== null && m.iou >= design.match_iou ? m.id : null; };
  const groupRecord = (id: string) => saved.groups.find(g => g.id === id) || previous.groups.find(g => g.id === id);
  const freshGroups = new Set(current.groups.map(g => g.id)), groups = new Map(current.groups.map(g => [g.id, g]));
  const groupsTaken = new Set(freshGroups), keptGroups = new Map<string, string | null>(), renamedGroups: CarryRename[] = [];
  /** The id a carried group record takes in the new review (renamed when the new model uses its id), or null. */
  const keepGroup = (id: string): string | null => {
    if (keptGroups.has(id)) return keptGroups.get(id) ?? null;
    const entry = groupRecord(id);
    let as: string | null = null;
    if (entry) {
      if (groupsTaken.has(id)) { as = distinctId(id, groupsTaken); renamedGroups.push({ id, carried_as: as }); }
      else { as = id; groupsTaken.add(id); }
      groups.set(as, { ...entry, id: as });
    }
    keptGroups.set(id, as);
    return as;
  };

  const carried: CarriedSceneObject[] = [], clipped: string[] = [];
  for (const [id, fit] of fitted) {
    const before = reviewed.get(id)!, notes = [...fit.notes], as = carriedId(id);
    const next = { ...fit.object, id: as } as CarriedSceneObject & Loose;
    if (as !== id) notes.push(`The new model already uses its id (${id}), so it was carried as ${as}.`);
    if (next.parent_id) {
      if (fitted.has(next.parent_id)) next.parent_id = carriedId(next.parent_id);
      else {
        const target = matchedId(next.parent_id);
        if (target) next.parent_id = target;
        else { delete next.parent_id; notes.push("Its earlier parent has no counterpart in the new image; choose a parent after checking."); }
      }
    }
    if (next.group_id) {
      const owner = !fitted.has(next.group_id) && (reviewed.has(next.group_id) || originals.has(next.group_id)) ? matchedId(next.group_id) : null;
      const ownerGroup = owner ? freshById.get(owner)?.group_id : undefined;
      if (ownerGroup && freshGroups.has(ownerGroup)) next.group_id = ownerGroup;
      else { const group = keepGroup(next.group_id); if (group) next.group_id = group; else delete next.group_id; }
    }
    if (next.derived_from) next.derived_from = next.derived_from.map(carriedId);
    if (next.replacement_ids) next.replacement_ids = next.replacement_ids.map(carriedId);
    const m = counterpart(id), matched = m.id !== null && m.iou >= design.match_iou ? m.id : null;
    next.carried_from = { scene_id: previous.id, model_signature: previous.model_signature, object_id: id, status: before.status,
      geometry_reviewed: before.geometry_reviewed, iou_with_new: Math.round(m.iou * 1e4) / 1e4, matched_id: matched, method: "georeferenced-affine" };
    next.status = before.status === "superseded" ? "superseded" : "needs_adjustment";
    next.geometry_reviewed = false;
    next.model = { ...next.model, height_verified: false };
    delete next.provenance; delete next.fieldwork_import; delete next.replaces;
    // An earlier roof-line check or linework attestation was made against the earlier pixels: neither travels as a check.
    if (next.roof_line_edits) next.roof_line_edits = { ...next.roof_line_edits, checked: false };
    if (next.roof_feedback?.linework) {
      if (next.roof_feedback.family) next.roof_feedback = { outline: next.roof_feedback.outline, family: next.roof_feedback.family };
      else delete next.roof_feedback;
    }
    const { lead, agreement } = carrySentences(previous.id, before, m.iou, matched, design);
    next.notes = joinNotes(before.notes, [lead, ...notes, agreement]);
    carried.push(next);
    if (fit.clipped) clipped.push(as);
  }

  const objects: SceneObject[] = [...fresh, ...carried];
  if (objects.length > MAX_OBJECTS) return refuse(`the carried review would hold ${objects.length} objects, more than one review allows`);
  if (groups.size > MAX_GROUPS) return refuse(`the carried review would hold ${groups.size} groups, more than one review allows`);
  return {
    recovery: {
      review: { revision: options.revision ?? 0, model_signature: current.model_signature, objects, groups: [...groups.values()], reviewed_classes: [] },
      carried_objects: carried.length,
      new_candidates: fresh.length,
      method: "georeferenced-affine",
      dropped: [...dropped].map(([id, reason]) => { const o = reviewed.get(id)!; return { object_id: id, kind: o.kind, status: o.status, reason }; }),
      clipped,
      renamed,
      renamed_groups: renamedGroups,
    },
    reason: null,
  };
}

/** carryAcrossFramesResult without the reason: the recoverSceneReview-shaped recovery, or null. */
export function carryAcrossFrames(previous: SceneModelSnapshot, current: RecapturedSnapshot, saved: SceneReview,
  options: { revision?: number } = {}): FrameCarryRecovery | null {
  return carryAcrossFramesResult(previous, current, saved, options).recovery;
}
