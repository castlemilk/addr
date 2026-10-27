import type { RoofLineEdits } from "./roof-line-edits";
import type { RoofAnnotation } from "./roof-annotations";
import type { VegetationType } from "./scene-vegetation";
export const SCENE_CLASSES = [
  "building",
  "tree",
  "road",
  "car",
  "solar_panel",
  "skylight",
  "chimney",
  "pool",
  "driveway",
  "footpath",
  "vegetation",
  "water",
  "other",
] as const;
export type SceneKind = (typeof SCENE_CLASSES)[number];
export type ScenePoint = [number, number];
export type RoofFamily = "gabled" | "hipped" | "flat" | "mixed";
export interface SceneRoofLinework {
  vertices: ScenePoint[];
  faces: number[][];
  edges: { a: number; b: number; kind: string }[];
}
export interface SceneRoofFeedback {
  outline: { ring: ScenePoint[]; holes: ScenePoint[][] };
  family?: RoofFamily;
  linework?: SceneRoofLinework;
}
export const SCENE_REVIEW_ISSUES = ["outline", "class", "instances", "shape_3d", "height", "placement"] as const;
export type SceneReviewIssue = (typeof SCENE_REVIEW_ISSUES)[number];
export const CLASS_NAMES: Record<SceneKind, string> = {
  building: "Buildings",
  tree: "Trees",
  road: "Roads",
  car: "Cars",
  solar_panel: "Solar arrays",
  skylight: "Skylights",
  chimney: "Chimneys",
  pool: "Pools",
  driveway: "Driveways",
  footpath: "Footpaths",
  vegetation: "Vegetation",
  water: "Water",
  other: "Other objects",
};
export const CLASS_COLORS: Record<SceneKind, string> = {
  building: "#67d7e7",
  tree: "#8ce2ba",
  road: "#d4b58c",
  car: "#ffadbc",
  solar_panel: "#97b8ff",
  skylight: "#c6caff",
  chimney: "#d0bac5",
  pool: "#62d5ec",
  driveway: "#dccdab",
  footpath: "#e4daca",
  vegetation: "#b4d68b",
  water: "#81baf7",
  other: "#d7adf3",
};
/** Editor paint tier, bottom to top (road-shape 2026-10-07, UNDERSTANDING.md section 5): 0 ground surfaces, 1 standing objects,
 *  2 roof fixtures (the kinds scene-instances.ts and SceneRelationships.tsx treat as rooftop). scenePaintOrder (scene-paint-order.ts)
 *  draws a lower tier first, so roads and driveways sit under cars and trees and every solar panel sits over its roof. The Record
 *  type makes a new kind choose its tier. Display only: never the saved order, the queue order or the advance order. */
export const CLASS_PAINT_TIER: Record<SceneKind, 0 | 1 | 2> = {
  water: 0,
  road: 0,
  driveway: 0,
  footpath: 0,
  vegetation: 0,
  pool: 0,
  tree: 1,
  car: 1,
  building: 1,
  other: 1,
  solar_panel: 2,
  skylight: 2,
  chimney: 2,
};
export interface SceneModel {
  solar?: { tilt_deg?: number; azimuth_deg?: number; clearance_m?: number };
  surface?: { profile: "plane" | "crowned"; grade_percent?: number; crossfall_percent?: number; heading_deg?: number };
  car?: { profile: "car" | "suv" | "van"; heading_deg?: number;
    cabin_length_ratio?: number; cabin_width_ratio?: number; cabin_offset_ratio?: number };
  tree?: { crown_profile: "rounded" | "conical" | "spreading"; crown_base_ratio: number };
  placement?: "roof" | "absolute";
  height_m: number;
  base_m: number;
  height_source: string;
  height_verified: boolean;
  width_m: number;
  length_m: number;
  area_m2: number;
  rotation_deg: number;
  color: string;
  footprint_area_px?: number;
}
/** Where a carried object came from (recapture carry): the structural twin of scene-frame-transform.ts CarriedFrom, which
 *  writes it; validateSceneReview (scene-store.ts) shape-checks it and review_authority.carried_undecided (Python) reads it.
 *  `status` and `geometry_reviewed` are the earlier verdict, a hint and never a label. Extra keys the Python window carry
 *  records (`role`) pass through untouched. */
export interface SceneCarriedFrom {
  scene_id: string;
  model_signature: string;
  object_id: string;
  status: SceneObject["status"];
  geometry_reviewed: boolean;
  iou_with_new: number;
  matched_id: string | null;
  method: "georeferenced-affine";
}
export interface SceneObject {
  id: string;
  kind: SceneKind;
  label: string;
  ring: ScenePoint[];
  holes?: ScenePoint[][];
  status: "candidate" | "confirmed" | "needs_adjustment" | "rejected" | "superseded";
  derived_from?: string[];
  replacement_ids?: string[];
  /** Model seeds this fuller detection took over when the scene was widened (one whole roof may replace several parts). */
  replaces?: string[];
  /** The earlier, smaller frame [x0, y0, x1, y1] whose edge cut this outline; set on a carried seed no fuller detection completed. */
  clipped_by_frame?: [number, number, number, number];
  /** Recapture carry: the object this one was re-projected from, with the earlier verdict as a hint (see SceneCarriedFrom). */
  carried_from?: SceneCarriedFrom;
  geometry_reviewed: boolean;
  visibility?: "full" | "occluded" | "truncated" | "occluded_truncated";
  review_issues?: SceneReviewIssue[];
  source: string;
  score?: number;
  prompt?: string;
  address_id?: number;
  group_id?: string;
  parent_id?: string;
  model: SceneModel;
  notes?: string;
  roof_type?: string;
  /** Reviewer-chosen structure kind for secondary structures (shed, garage, patio cover, ...); the label follows it. */
  structure_kind?: string;
  /** Reviewer-chosen vegetation type, kept only on kind tree or vegetation (validateSceneReview refuses it elsewhere); absent means
   *  not set. Unlike structure_kind it never rewrites the label, the status or the outline (scene-vegetation.ts). */
  vegetation_type?: VegetationType;
  roof_feedback?: SceneRoofFeedback;
  roof_line_edits?: RoofLineEdits;
  roof_geometry_source?: "image-guided-prior" | "reviewer-template";
  roof_geometry_note?: string;
  roof_annotation?: RoofAnnotation | null;
  roof_parts?: {
    ring_meters?: ScenePoint[];
    ring?: ScenePoint[];
    ring_z?: number[];
    z?: number[];
  }[];
  roof_lines?: { line_meters: [ScenePoint, ScenePoint]; kind?: string }[];
}
export interface SceneGroup {
  id: string;
  label: string;
}
/** `frame-transform-v1`, recorded on a recaptured snapshot at capture (scripts/ml/scene_recapture.measured_transform): how the
 *  source scene's frame maps onto this one. The structural twin of scene-frame-transform.ts FrameTransform, which re-derives
 *  and checks every value before a carry; the store also runs the Python verifier on the two images. Every figure is agreement
 *  between two frames, never accuracy. Extra keys the Python window capture records (`role`, `overlap.region_px`) pass through. */
export interface SceneFrameTransform {
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
export interface SceneModelSnapshot {
  schema_version: 1;
  id: string;
  address_id: number | null;
  cohort: string | null;
  region?: {
    provider: "google" | "esri";
    requested_bounds: [number, number, number, number];
    bounds: [number, number, number, number];
    imagery_zoom: number;
    span_m: number;
    native_meters_per_pixel: number;
    spatial_group: string;
    split: string;
  };
  title: string;
  centroid: { lon: number; lat: number };
  image_sha256: string;
  image_width: number;
  image_height: number;
  pixel_to_meters: number[][];
  meters_to_pixel: number[][];
  model_signature: string;
  context_expansion?: {
    source_model_signature: string;
    source_image_sha256: string;
    offset_pixels: [number, number];
    source_size: [number, number];
    preserved_pixels_sha256: string;
  };
  /** A planned recapture window (frame-cut-window-v1): the planned lots are the parcels the window was drawn for, the first
   *  being the address's own lot where the window has a lot identity. Only the fields the editor reads are typed. */
  window_frame?: {
    version?: string;
    planned_window_id?: string;
    identity?: string;
    address_id?: number | null;
    lots?: {
      address_id: number | null;
      class: string;
      lot: string | null;
      pfi: string | null;
      plan: string | null;
      share: number;
    }[];
  };
  /** Set on a recapture (a scene captured --carry-from another scene): the measured transform back to the source frame. */
  frame_transform?: SceneFrameTransform;
  objects: SceneObject[];
  groups: SceneGroup[];
  scans: Record<string, { status: string; count: number; prompt: string }>;
  height_note: string;
}
export interface SceneReview {
  revision: number;
  model_signature: string;
  objects: SceneObject[];
  groups: SceneGroup[];
  reviewed_classes: SceneKind[];
  updated_at?: string;
  updated_by?: string;
  history?: Omit<SceneReview, "history">[];
}
export interface SceneData {
  address_names?: Record<string, string>;
  model: SceneModelSnapshot;
  review: SceneReview | null;
  stale: boolean;
  image_url: string;
  recovery?: {
    review: SceneReview;
    carried_objects: number;
    new_candidates: number;
    /** Set only by the recapture carry (scene-frame-transform.ts): the earlier decisions were re-projected through the
     *  georeferenced frame from `source_scene_id`, and every one returns as needs_adjustment. The same-image and
     *  context-expansion recoveries leave these fields undefined, as before. */
    method?: "georeferenced-affine";
    source_scene_id?: string;
    /** Source objects the carry left behind (by source id), each with its reason. */
    dropped?: { object_id: string; kind: SceneKind; status: SceneObject["status"]; reason: string }[];
    /** Carried outlines the new image edge cut, by their id in the carried review. */
    clipped?: string[];
    /** Source object / group ids the new model already uses, and the ids they are carried under instead. */
    renamed?: { id: string; carried_as: string }[];
    renamed_groups?: { id: string; carried_as: string }[];
  } | null;
  recovery_reason?: string;
}
export function transformPoint(p: number[], matrix: number[][]): ScenePoint {
  return [
    p[0] * matrix[0][0] + p[1] * matrix[1][0] + matrix[2][0],
    p[0] * matrix[0][1] + p[1] * matrix[1][1] + matrix[2][1],
  ];
}
export function ringArea(ring: ScenePoint[]) {
  return (
    Math.abs(
      ring.reduce((s, p, i) => {
        const q = ring[(i + 1) % ring.length];
        return s + p[0] * q[1] - q[0] * p[1];
      }, 0),
    ) / 2
  );
}
export function polygonIssues(
  ring: ScenePoint[],
  width = 512,
  height = 512,
  holes: ScenePoint[][] = [],
  minimumArea = 1,
): string[] {
  if (
    !Array.isArray(ring) ||
    ring.length < 3 ||
    ring.length > 200 ||
    ring.some(
      (p) =>
        !Array.isArray(p) ||
        p.length !== 2 ||
        p.some((v) => typeof v !== "number" || !Number.isFinite(v)),
    )
  )
    return ["Use at least three finite outline points"];
  const errors = [];
  if (ring.some((p) => p[0] < 0 || p[1] < 0 || p[0] > width || p[1] > height))
    errors.push("Keep the outline inside the image");
  if (ringArea(ring) < minimumArea) errors.push("The outline needs a visible area");
  const turn = (a: ScenePoint, b: ScenePoint, c: ScenePoint) =>
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  if (
    ring.some((p, i) =>
      ring.some((q, j) => j > i && Math.hypot(p[0] - q[0], p[1] - q[1]) < 1e-6),
    )
  )
    return [...errors, "Outline points overlap; remove the duplicate point"];
  const onSegment = (a: ScenePoint, b: ScenePoint, p: ScenePoint) =>
    Math.abs(turn(a, b, p)) < 1e-8 &&
    p[0] >= Math.min(a[0], b[0]) - 1e-8 &&
    p[0] <= Math.max(a[0], b[0]) + 1e-8 &&
    p[1] >= Math.min(a[1], b[1]) - 1e-8 &&
    p[1] <= Math.max(a[1], b[1]) + 1e-8;
  for (let i = 0; i < ring.length; i++)
    for (let j = i + 2; j < ring.length; j++) {
      if (i === 0 && j === ring.length - 1) continue;
      const a = ring[i],
        b = ring[(i + 1) % ring.length],
        c = ring[j],
        d = ring[(j + 1) % ring.length];
      if (
        (turn(a, b, c) * turn(a, b, d) < -1e-8 &&
          turn(c, d, a) * turn(c, d, b) < -1e-8) ||
        onSegment(a, b, c) ||
        onSegment(a, b, d) ||
        onSegment(c, d, a) ||
        onSegment(c, d, b)
      )
        return [...errors, "Outline edges cross; move or remove a point"];
    }
  if (!Array.isArray(holes) || holes.length > 32 || holes.some(h=>!Array.isArray(h)) ||
      ring.length + holes.reduce((n,h)=>n+h.length,0) > 2000) return [...errors,"Use at most 32 interior gaps and 2,000 total outline points"];
  const contains = (p:ScenePoint, boundary:ScenePoint[]) => {
    let inside=false;
    for(let i=0,j=boundary.length-1;i<boundary.length;j=i++) {
      const a=boundary[i],b=boundary[j];
      if((a[1]>p[1])!==(b[1]>p[1])&&p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0])inside=!inside;
    }
    return inside;
  };
  const samePoint = (a:ScenePoint,b:ScenePoint) => Math.hypot(a[0]-b[0],a[1]-b[1])<1e-6;
  const onBoundary = (p:ScenePoint,boundary:ScenePoint[]) =>
    boundary.some((a,i)=>onSegment(a,boundary[(i+1)%boundary.length],p));
  const strictlyInside = (p:ScenePoint,boundary:ScenePoint[]) => contains(p,boundary)&&!onBoundary(p,boundary);
  const samples = (boundary:ScenePoint[]):ScenePoint[] => boundary.flatMap((p,i)=>{
    const q=boundary[(i+1)%boundary.length];
    return [p,[(p[0]+q[0])/2,(p[1]+q[1])/2] as ScenePoint];
  });
  const contacts = (a:ScenePoint[],b:ScenePoint[]):ScenePoint[]|null => {
    const points:ScenePoint[]=[];
    for(let i=0;i<a.length;i++)for(let j=0;j<b.length;j++) {
      const p=a[i],s=a[(i+1)%a.length],q=b[j],t=b[(j+1)%b.length];
      if(turn(p,s,q)*turn(p,s,t)<-1e-8&&turn(q,t,p)*turn(q,t,s)<-1e-8)return null;
      const shared:ScenePoint[]=[];
      for(const v of [p,s,q,t])if(onSegment(p,s,v)&&onSegment(q,t,v)&&!shared.some(w=>samePoint(v,w)))shared.push(v);
      if(shared.length>1)return null; // Shared edges, unlike isolated contacts, are invalid.
      for(const v of shared)if(!points.some(w=>samePoint(v,w)))points.push(v);
    }
    return points;
  };
  // Valid masks can have isolated point contacts. A cycle of contacts closes
  // off a separate interior region, which is not a single polygon. Model the
  // contact points explicitly so several gaps meeting at one point stay valid.
  const parents=Array.from({length:holes.length+1},(_,i)=>i),contactPoints:ScenePoint[]=[],edges=new Set<string>();
  const root=(node:number):number=>{
    while(parents[node]!==node)node=parents[node];
    return node;
  };
  const connect=(a:number,b:number,points:ScenePoint[])=>{
    for(const point of points) {
      let index=contactPoints.findIndex(p=>samePoint(p,point));
      if(index<0){index=contactPoints.length;contactPoints.push(point);parents.push(parents.length);}
      const node=holes.length+1+index;
      for(const boundary of [a,b]) {
        const edge=`${boundary}:${node}`;
        if(edges.has(edge))continue;
        edges.add(edge);
        const first=root(boundary),second=root(node);
        if(first===second)return false;
        parents[first]=second;
      }
    }
    return true;
  };
  for(let i=0;i<holes.length;i++) {
    const hole=holes[i],issue=polygonIssues(hole,width,height,[],1e-4)[0];
    if(issue)return [...errors,`Gap ${i+1}: ${issue}`];
    const outerContacts=contacts(hole,ring);
    if(outerContacts===null||samples(hole).some(p=>!contains(p,ring)&&!onBoundary(p,ring)))
      return [...errors,`Gap ${i+1} must stay inside the outer outline without crossing or sharing an edge`];
    if(!connect(i+1,0,outerContacts))return [...errors,"Interior gaps cannot disconnect the outline"];
    for(let j=0;j<i;j++) {
      const shared=contacts(hole,holes[j]);
      if(shared===null||samples(hole).some(p=>strictlyInside(p,holes[j]))||samples(holes[j]).some(p=>strictlyInside(p,hole)))
        return [...errors,"Interior gaps cannot overlap or contain one another"];
      if(!connect(i+1,j+1,shared))return [...errors,"Interior gaps cannot disconnect the outline"];
    }
  }
  if(ringArea(ring)-holes.reduce((sum,h)=>sum+ringArea(h),0)<minimumArea)errors.push("The remaining outline needs a visible area");
  return [...new Set(errors)];
}
/** Why repairHoles dropped an interior gap. */
export type GapDropReason = "sliver" | "outside_outline" | "crosses_outline" | "disconnects_outline" | "invalid";
export interface DroppedGap {
  /** Index into the gap list given to repairHoles; the reviewer sees it as gap `index + 1`. */
  index: number;
  reason: GapDropReason;
}
/** Gaps under this area are model vectorisation noise: 2 px² is 0.11 m² at 0.236 m/px. */
export const MIN_GAP_AREA_PX = 2;
/**
 * Explicit repair of interior gaps (holes) against an outer ring; polygonIssues itself never repairs. Drops, in this order:
 * slivers (fewer than three finite points or under MIN_GAP_AREA_PX), gaps that are not valid polygons on their own, gaps with
 * no sample point inside the outer ring, gaps that cross, share an edge with or leave the outer ring, and gaps that touch it at
 * more than one point (they cut the outline apart). A crossing gap is dropped rather than clipped: its clipped part would run
 * along the outer ring, which a gap may not share, and the outer ring is never changed here. Every other gap is returned
 * untouched (the same arrays, in order). Pure: nothing is mutated.
 */
export function repairHoles(
  ring: ScenePoint[],
  holes: ScenePoint[][],
  width = 512,
  height = 512,
): { holes: ScenePoint[][]; dropped: DroppedGap[] } {
  const finite = (p: unknown): p is ScenePoint =>
    Array.isArray(p) && p.length === 2 && p.every((v) => typeof v === "number" && Number.isFinite(v));
  // Without a usable outer ring or gap list there is nothing to repair against; validation reports them as they are.
  if (!Array.isArray(ring) || ring.length < 3 || !ring.every(finite) || !Array.isArray(holes)) return { holes, dropped: [] };
  // The predicates and tolerances polygonIssues applies to a gap against the outer ring.
  const turn = (a: ScenePoint, b: ScenePoint, c: ScenePoint) =>
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const onSegment = (a: ScenePoint, b: ScenePoint, p: ScenePoint) =>
    Math.abs(turn(a, b, p)) < 1e-8 &&
    p[0] >= Math.min(a[0], b[0]) - 1e-8 &&
    p[0] <= Math.max(a[0], b[0]) + 1e-8 &&
    p[1] >= Math.min(a[1], b[1]) - 1e-8 &&
    p[1] <= Math.max(a[1], b[1]) + 1e-8;
  const contains = (p: ScenePoint, boundary: ScenePoint[]) => {
    let inside = false;
    for (let i = 0, j = boundary.length - 1; i < boundary.length; j = i++) {
      const a = boundary[i],
        b = boundary[j];
      if ((a[1] > p[1]) !== (b[1] > p[1]) && p[0] < ((b[0] - a[0]) * (p[1] - a[1])) / (b[1] - a[1]) + a[0]) inside = !inside;
    }
    return inside;
  };
  const onBoundary = (p: ScenePoint, boundary: ScenePoint[]) =>
    boundary.some((a, i) => onSegment(a, boundary[(i + 1) % boundary.length], p));
  const strictlyInside = (p: ScenePoint, boundary: ScenePoint[]) => contains(p, boundary) && !onBoundary(p, boundary);
  const samples = (boundary: ScenePoint[]): ScenePoint[] =>
    boundary.flatMap((p, i) => {
      const q = boundary[(i + 1) % boundary.length];
      return [p, [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2] as ScenePoint];
    });
  // Where a gap meets the outer ring, as polygonIssues finds it: null when a gap edge properly crosses an outer edge or shares
  // more than one point with it (a shared stretch of edge), else the distinct points where the two touch.
  const samePoint = (a: ScenePoint, b: ScenePoint) => Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-6;
  const contacts = (gap: ScenePoint[]): ScenePoint[] | null => {
    const points: ScenePoint[] = [];
    for (let i = 0; i < gap.length; i++)
      for (let j = 0; j < ring.length; j++) {
        const p = gap[i],
          s = gap[(i + 1) % gap.length],
          q = ring[j],
          t = ring[(j + 1) % ring.length];
        if (turn(p, s, q) * turn(p, s, t) < -1e-8 && turn(q, t, p) * turn(q, t, s) < -1e-8) return null;
        const shared: ScenePoint[] = [];
        for (const v of [p, s, q, t]) if (onSegment(p, s, v) && onSegment(q, t, v) && !shared.some((w) => samePoint(v, w))) shared.push(v);
        if (shared.length > 1) return null;
        for (const v of shared) if (!points.some((w) => samePoint(v, w))) points.push(v);
      }
    return points;
  };
  const dropped: DroppedGap[] = [];
  const kept = holes.filter((hole, index) => {
    let reason: GapDropReason | null = null;
    if (!Array.isArray(hole) || hole.filter(finite).length < 3 || (hole.every(finite) && ringArea(hole) < MIN_GAP_AREA_PX))
      reason = "sliver";
    else if (polygonIssues(hole, width, height, [], 1e-4).length) reason = "invalid";
    else {
      const touch = contacts(hole);
      if (touch === null || samples(hole).some((p) => !contains(p, ring) && !onBoundary(p, ring)))
        reason =
          samples(hole).some((p) => strictlyInside(p, ring)) || samples(ring).some((p) => strictlyInside(p, hole))
            ? "crosses_outline"
            : "outside_outline";
      // Two or more contacts with the outer ring cut the outline apart ("Interior gaps cannot disconnect the outline").
      else if (touch.length > 1) reason = "disconnects_outline";
    }
    if (reason) dropped.push({ index, reason });
    return !reason;
  });
  return { holes: dropped.length ? kept : holes, dropped };
}
/**
 * The outline check the client applies where an edited outline is saved (the decide path and Save scene) and the editor shows
 * live; the server validates as sent and never repairs. Validate as sent, and only when that is refused while the outer ring
 * and the gap list are acceptable on their own, drop the gaps repairHoles names and validate again. Only gaps that are valid
 * polygons by themselves are dropped (slivers, gaps the outer ring crosses, shares an edge with, leaves out or touches at more
 * than one point); a gap that is malformed or invalid on its own stays refused, named by its own number, so the duplicate-point,
 * crossing-edge and image-bound rules for gaps hold. Legal geometry comes back untouched (`holes` is the input, `dropped` is
 * empty), so an object nobody edited keeps its model gaps. `issues` belong to the returned `holes`: save those holes only when
 * `issues` is empty.
 */
export function checkOutlineWithGapRepair(
  ring: ScenePoint[],
  holes: ScenePoint[][] = [],
  width = 512,
  height = 512,
): { holes: ScenePoint[][]; dropped: DroppedGap[]; issues: string[] } {
  const issues = polygonIssues(ring, width, height, holes);
  if (!issues.length || polygonIssues(ring, width, height).length || !Array.isArray(holes) || holes.length > 32 ||
      holes.some((h) => !Array.isArray(h)) || ring.length + holes.reduce((n, h) => n + h.length, 0) > 2000)
    return { holes, dropped: [], issues };
  const repaired = repairHoles(ring, holes, width, height);
  for (const { index } of repaired.dropped) {
    const own = polygonIssues(holes[index], width, height, [], 1e-4)[0];
    if (own) return { holes, dropped: [], issues: [`Gap ${index + 1}: ${own}`] };
  }
  if (!repaired.dropped.length) return { holes, dropped: [], issues };
  return { ...repaired, issues: polygonIssues(ring, width, height, repaired.holes) };
}
// What each drop reason says about a gap: as the editor puts it before a save, and as the saved message puts it after.
const GAP_DROP_REASONS: Record<GapDropReason, readonly [pending: string, saved: string]> = {
  sliver: [`is a sliver under ${MIN_GAP_AREA_PX} square pixels`, `was a sliver under ${MIN_GAP_AREA_PX} square pixels`],
  outside_outline: ["lies outside the outer outline", "lay outside the outer outline"],
  crosses_outline: ["crosses or shares an edge with the outer outline", "crossed or shared an edge with the outer outline"],
  disconnects_outline: ["touches the outer outline at more than one point", "touched the outer outline at more than one point"],
  invalid: ["is not a valid outline on its own", "was not a valid outline on its own"],
};
/**
 * One plain sentence for the reviewer naming each dropped gap as the gap list numbers it (index + 1) and why. Kind-neutral: the
 * area joins the object whatever its kind. `pending` words it for the editor before a save ("will be dropped on save");
 * otherwise it reports a save. `label` names the object, so the saved message still says which object it was once the page
 * has moved on.
 */
export function droppedGapsNotice(dropped: DroppedGap[], options: { pending?: boolean; label?: string } = {}): string {
  if (!dropped.length) return "";
  const { pending = false, label } = options,
    one = dropped.length === 1,
    tense = pending ? 0 : 1;
  const list = (items: string[]) => (items.length === 1 ? items[0] : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`);
  const gaps = `${one ? "gap" : "gaps"} ${list(dropped.map((d) => String(d.index + 1)))}`,
    why = one
      ? `it ${GAP_DROP_REASONS[dropped[0].reason][tense]}`
      : list(dropped.map((d) => `gap ${d.index + 1} ${GAP_DROP_REASONS[d.reason][tense]}`)),
    area = pending
      ? `any area ${one ? "it covers" : "they cover"} will count as part of the object`
      : `any area ${one ? "it" : "they"} covered now counts as part of the object`;
  const sentence = `${gaps} ${pending ? "will be dropped on save:" : one ? "was dropped because" : "were dropped because"} ${why}, so ${area}; ${
    one ? "redraw the gap if it is real" : "redraw any that are real"
  }.`;
  return label ? `${label}: ${sentence}` : `${sentence.charAt(0).toUpperCase()}${sentence.slice(1)}`;
}
export function scenePathData(object: Pick<SceneObject,"ring"|"holes">) {
  return [object.ring,...(object.holes||[])].map(r=>`M ${r.map(p=>p.join(" ")).join(" L ")} Z`).join(" ");
}
export function footprintMetrics(ring: ScenePoint[], frame: number[][], holes:ScenePoint[][]=[]) {
  const meters = ring.map((p) => transformPoint(p, frame));
  let best = { area: Infinity, length: 0, width: 0, angle: 0 };
  // A minimum rectangle is aligned to an edge of the convex hull, which may
  // span a notch and therefore need not occur among the original outline edges.
  const sorted = [...meters].sort((a, b) => a[0] - b[0] || a[1] - b[1]),
    cross = (a: ScenePoint, b: ScenePoint, c: ScenePoint) =>
      (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const half = (points: ScenePoint[]) => {
    const hull: ScenePoint[] = [];
    for (const p of points) {
      while (
        hull.length > 1 &&
        cross(hull[hull.length - 2], hull[hull.length - 1], p) <= 0
      )
        hull.pop();
      hull.push(p);
    }
    return hull.slice(0, -1);
  };
  const hull = [...half(sorted), ...half([...sorted].reverse())];
  for (let i = 0; i < hull.length; i++) {
    const a = hull[i],
      b = hull[(i + 1) % hull.length],
      angle = Math.atan2(b[1] - a[1], b[0] - a[0]),
      c = Math.cos(angle),
      s = Math.sin(angle);
    const x = meters.map((p) => p[0] * c + p[1] * s),
      y = meters.map((p) => -p[0] * s + p[1] * c),
      width = Math.max(...y) - Math.min(...y),
      length = Math.max(...x) - Math.min(...x);
    if (width * length < best.area)
      best = {
        area: width * length,
        length: Math.max(width, length),
        width: Math.min(width, length),
        angle: angle + (width > length ? Math.PI / 2 : 0),
      };
  }
  const round = (n: number) => Math.round(n * 100) / 100;
  return {
    area_m2: round(ringArea(meters)-holes.reduce((sum,h)=>sum+ringArea(h.map(p=>transformPoint(p,frame))),0)),
    footprint_area_px: round(ringArea(ring)-holes.reduce((sum,h)=>sum+ringArea(h),0)),
    length_m: round(best.length),
    width_m: round(best.width),
    rotation_deg: round(((best.angle * 180) / Math.PI + 180) % 180),
  };
}
