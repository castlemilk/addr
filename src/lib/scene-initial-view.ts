import { capFrameToImage, objectImageFrame, type ImageFrame } from "./scene-image-frame";
import { transformPoint, type SceneModelSnapshot, type SceneObject, type ScenePoint } from "./scene-types";

/** The initial view of a window scene (decision card item 3, 6 October 2026, "yes to defaults" 7 October): the editor opens on the
 *  union of every asked object, each asked object's building cluster and the planned lot polygon, widened by objectImageFrame's
 *  +24 px / x1.6 rule to the viewport aspect and capped to the image. The URL object is selected and highlighted but not zoomed to;
 *  the per-object fit, Fit parcel and Whole scene stay clicks. Pure: nothing here reads a file or the DOM. */

/** Carried-copy roles as scene_window_ready.py names them (SLICE_ROLE / BODY_ROLE); `carried_from.role` is data, not a typed field. */
export const SLICE_ROLE = "frame_cut_target";
export const BODY_ROLE = "whole_roof_body";
/** Two live building pieces whose metric gap is at most this are one cluster (the census's TOUCH_M; overlap counts as gap 0). */
export const CLUSTER_TOUCH_M = 0.5;

type Lineage = Pick<SceneObject, "id" | "status"> & Partial<Pick<SceneObject, "carried_from" | "replacement_ids">>;

export function carriedRole(object: Pick<SceneObject, "carried_from">): string | null {
  const carried = object.carried_from as (SceneObject["carried_from"] & { role?: unknown }) | undefined;
  return carried && typeof carried === "object" && typeof carried.role === "string" ? carried.role : null;
}

/** The asked set as scene_window_ready.assess reads it, live ids only: a carried frame_cut_target slice stands for itself unless it
 *  is superseded, when its replacement_ids (followed while they are superseded in turn) stand for it; a carried whole_roof_body
 *  likewise. A whole-roof takeover is reached only as a replacer; a linear-merge feature's `replaces` is never asked. */
export function askedObjectIds(objects: readonly Lineage[]): string[] {
  const byId = new Map(objects.map(o => [o.id, o]));
  const asked = new Set<string>();
  const resolve = (id: string, seen: Set<string>) => {
    const object = byId.get(id);
    if (!object || seen.has(id)) return;
    seen.add(id);
    if (object.status === "superseded") { for (const next of object.replacement_ids ?? []) resolve(next, seen); return; }
    asked.add(id);
  };
  for (const object of objects) {
    const role = carriedRole(object as Pick<SceneObject, "carried_from">);
    if (role === SLICE_ROLE || role === BODY_ROLE) resolve(object.id, new Set());
  }
  return [...asked];
}

const orient = (a: ScenePoint, b: ScenePoint, c: ScenePoint) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
function pointSegmentDistance(p: ScenePoint, a: ScenePoint, b: ScenePoint) {
  const dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy;
  const t = l2 > 0 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}
function segmentsCross(a: ScenePoint, b: ScenePoint, c: ScenePoint, d: ScenePoint) {
  const o1 = orient(a, b, c), o2 = orient(a, b, d), o3 = orient(c, d, a), o4 = orient(c, d, b);
  return ((o1 > 0 && o2 < 0) || (o1 < 0 && o2 > 0)) && ((o3 > 0 && o4 < 0) || (o3 < 0 && o4 > 0));
}
function segmentDistance(a: ScenePoint, b: ScenePoint, c: ScenePoint, d: ScenePoint) {
  if (segmentsCross(a, b, c, d)) return 0;
  return Math.min(pointSegmentDistance(a, c, d), pointSegmentDistance(b, c, d), pointSegmentDistance(c, a, b), pointSegmentDistance(d, a, b));
}
/** Even-odd point in ring (boundary points count as inside through the distance test, never here). */
export function pointInRing(p: ScenePoint, ring: readonly ScenePoint[]) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
/** Shapely's polygon distance for two simple rings: 0 when they overlap, touch or one holds the other, else the smallest boundary
 *  gap. One vertex each way plus every edge pair is complete: a ring partly inside crosses an edge, a ring wholly inside has its
 *  first vertex inside. */
export function polygonDistance(a: readonly ScenePoint[], b: readonly ScenePoint[]): number {
  if (a.length < 3 || b.length < 3) return Number.POSITIVE_INFINITY;
  if (pointInRing(a[0], b) || pointInRing(b[0], a)) return 0;
  let best = Number.POSITIVE_INFINITY;
  for (let i = 0; i < a.length; i++) {
    const a1 = a[i], a2 = a[(i + 1) % a.length];
    for (let j = 0; j < b.length; j++) {
      best = Math.min(best, segmentDistance(a1, a2, b[j], b[(j + 1) % b.length]));
      if (best === 0) return 0;
    }
  }
  return best;
}

type Bounds = [number, number, number, number];
const bounds = (ring: readonly ScenePoint[]): Bounds => [
  Math.min(...ring.map(p => p[0])), Math.min(...ring.map(p => p[1])), Math.max(...ring.map(p => p[0])), Math.max(...ring.map(p => p[1])),
];
const boundsGap = (a: Bounds, b: Bounds) => Math.hypot(Math.max(0, a[0] - b[2], b[0] - a[2]), Math.max(0, a[1] - b[3], b[1] - a[3]));

export const LIVE_STATUSES: ReadonlySet<SceneObject["status"]> = new Set(["candidate", "confirmed", "needs_adjustment"]);
export const isLiveBuilding = (o: Pick<SceneObject, "kind" | "status" | "ring">) => o.kind === "building" && LIVE_STATUSES.has(o.status) && o.ring.length >= 3;

/** Connected components of live buildings (not rejected, not superseded) whose metric gap is at most `touchM`, measured on the
 *  rings through pixel_to_meters. Returns id -> the member ids of its cluster (itself included), in object order. */
export function buildingClusters(objects: readonly SceneObject[], pixelToMeters: number[][], touchM = CLUSTER_TOUCH_M): Map<string, string[]> {
  const buildings = objects.filter(isLiveBuilding);
  const metric = buildings.map(o => o.ring.map(p => transformPoint(p, pixelToMeters)));
  const boxes = metric.map(bounds);
  const parent = buildings.map((_, i) => i);
  const find = (i: number): number => (parent[i] === i ? i : (parent[i] = find(parent[i])));
  for (let i = 0; i < buildings.length; i++)
    for (let j = i + 1; j < buildings.length; j++) {
      if (find(i) === find(j) || boundsGap(boxes[i], boxes[j]) > touchM) continue;
      if (polygonDistance(metric[i], metric[j]) <= touchM) parent[find(j)] = find(i);
    }
  const members = new Map<number, string[]>();
  buildings.forEach((o, i) => { const root = find(i); members.set(root, [...(members.get(root) ?? []), o.id]); });
  const clusters = new Map<string, string[]>();
  buildings.forEach((o, i) => clusters.set(o.id, members.get(find(i)) ?? [o.id]));
  return clusters;
}

/** The planned lot's Vicmap pfi where the window has a lot identity (window_frame.lots[0]; 13 of 54 windows), else null. */
export function windowLotPfi(scene: Pick<SceneModelSnapshot, "window_frame"> | null | undefined): string | null {
  const pfi = scene?.window_frame?.lots?.[0]?.pfi;
  return pfi ? String(pfi) : null;
}

export type InitialView = {
  /** Live asked ids (askedObjectIds), in resolution order. */
  asked: string[];
  /** Cluster members pulled in beside the asked buildings (asked ids excluded). */
  cluster: string[];
  /** Every ring the frame must hold: asked outlines, cluster outlines, the lot polygon rings. */
  rings: ScenePoint[][];
  lot: boolean;
};

/** The rings the initial view of a window scene fits, or null when the scene is not a window (no window_frame) or names no asked
 *  object; both leave the editor on its per-object fit. `lotRings` are the planned lot's image-space rings once Vicmap has
 *  answered (parcelImagePolygons(feature, scene).flat()), empty until then. Asked objects of any kind are framed; only building
 *  asked objects bring a cluster. */
export function initialViewRings(objects: readonly SceneObject[], scene: Pick<SceneModelSnapshot, "window_frame" | "pixel_to_meters"> | null | undefined,
  lotRings: readonly (readonly ScenePoint[])[] = []): InitialView | null {
  if (!scene?.window_frame) return null;
  const asked = askedObjectIds(objects);
  if (!asked.length) return null;
  const byId = new Map(objects.map(o => [o.id, o]));
  const clusters = buildingClusters(objects, scene.pixel_to_meters);
  const ids = new Set(asked), cluster: string[] = [];
  for (const id of asked) for (const member of clusters.get(id) ?? []) if (!ids.has(member)) { ids.add(member); cluster.push(member); }
  const rings: ScenePoint[][] = [];
  for (const id of ids) { const object = byId.get(id); if (object && object.ring.length) rings.push(object.ring); }
  for (const ring of lotRings) if (ring.length) rings.push(ring.map(p => [p[0], p[1]] as ScenePoint));
  return { asked, cluster, rings, lot: lotRings.some(r => r.length > 0) };
}

/** The initial frame: objectImageFrame's widening (+24 px, x1.6, the viewport aspect) around the union, then capped to the image. */
export function initialImageFrame(view: InitialView, image: { width: number; height: number }, viewport: { width: number; height: number }): ImageFrame {
  return capFrameToImage(objectImageFrame(view.rings, image, viewport, false), image);
}

export type FramePlacement = "inside" | "clipped" | "outside";
/** Where a ring sits against a frame: every vertex inside, none inside with disjoint boxes (outside), else clipped. */
export function framePlacement(frame: ImageFrame, ring: readonly ScenePoint[]): FramePlacement {
  if (!ring.length) return "outside";
  const right = frame.x + frame.width, bottom = frame.y + frame.height;
  const inside = ring.filter(p => p[0] >= frame.x && p[0] <= right && p[1] >= frame.y && p[1] <= bottom).length;
  if (inside === ring.length) return "inside";
  const [minX, minY, maxX, maxY] = bounds(ring);
  if (maxX < frame.x || minX > right || maxY < frame.y || minY > bottom) return "outside";
  return "clipped";
}

/** The frame's longer side in metres through pixel_to_meters (a uniform scale is assumed, as every window's is). */
export function frameSpanMeters(frame: ImageFrame, pixelToMeters: number[][]) {
  const scale = Math.hypot(pixelToMeters[0][0], pixelToMeters[0][1]);
  return Math.max(frame.width, frame.height) * scale;
}
