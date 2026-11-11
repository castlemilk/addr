// Outline hints computed in the editor from an object's stored ring and the scene affines: corner density along
// the edge, edge directions against the outline's own axes, degenerate size, and (frame_cut, from the pixel ring and the
// image size; its design value is scripts/ml/scene_lot_tags DESIGN['edge_px']) a ring the image frame cut. Pure and Node-free so the scene
// page and a node test can import it. The thresholds mirror scripts/ml/scene_object_hints DESIGN (object-hints-v2; these
// three rules, themes T4 and T6, are unchanged since v1, and v2's solar_fixture_like needs the image so it is served only)
// so the editor can say the same thing before the eyes index has a row for the object; when the
// index does have one (served through /api/scenes/hints), the served hint wins and these fill in only the codes it
// lacks (mergeObjectHints). Agent hints, never labels: nothing here changes a status, a kind or a ring. Every number
// in a sentence is measured from the stored ring; "usually straight" is the only generalisation and it is about
// roofs, not about this object.
import type { SceneModelSnapshot, SceneObject } from "@/lib/scene-types";

export const OUTLINE_HINTS_SOURCE = "editor-geometry";
export const OUTLINE_HINTS_VERSION = "object-hints-v2";
/** Design values, frozen with the object-hints module (the same in v1 and v2); any change is a new version there and here. */
export const OUTLINE_DESIGN = {
  noisy_outline: { min_area_m2: 20, simplify_m: 0.3, min_vertices_per_metre: 0.45, min_wonky_share: 0.85, axis_tolerance_deg: 4.5, min_edge_m: 0.2 },
  degenerate_outline: { max_area_m2: 1, max_rect_width_m: 0.5 },
  tiny_area_for_kind: { building: 4, tree: 1, vegetation: 1, pool: 2 } as Record<string, number>,
  quiet_evidence_min_area_m2: 20,
  /** scripts/ml/scene_lot_tags DESIGN['edge_px']: a ring within one pixel of the frame boundary (or beyond it) was cut by the frame. */
  frame_cut: { edge_px: 1 },
} as const;

export type Point = [number, number];
export interface OutlineMetrics {
  area_m2: number;
  perimeter_m: number;
  vertices: number;
  /** Corners left after a Douglas-Peucker pass at OUTLINE_DESIGN.noisy_outline.simplify_m; pixel staircases drop out, real corners stay. */
  simplified_vertices: number;
  /** simplified_vertices / perimeter_m: corners per metre of edge (resolution-independent because of the simplify pass). */
  vertices_per_metre: number;
  rect_width_m: number;
  rect_length_m: number;
  /** Direction of the minimum rotated rectangle's long side, degrees in the local metre frame, mod 90. */
  axis_deg: number;
  /** Raw edges at least min_edge_m long (shorter ones carry no direction). */
  edges_measured: number;
  /** Measured edges more than axis_tolerance_deg off both rectangle axes. */
  off_axis_edges: number;
  wonky_share: number;
  rectilinearity: number;
}
export interface OutlineHint {
  code: string;
  severity: "context" | "review";
  sentence: string;
  evidence: Record<string, number | string | boolean | null>;
  source: typeof OUTLINE_HINTS_SOURCE;
}
/** The shape shared with queues.ObjectHint (served rows); structural so this module needs no import from queues. */
export interface HintLike { code: string; severity: "context" | "review"; sentence: string; evidence: Record<string, unknown> }

const affine = (p: readonly number[], m: number[][]): Point => [p[0] * m[0][0] + p[1] * m[1][0] + m[2][0], p[0] * m[0][1] + p[1] * m[1][1] + m[2][1]];
/** The ring in the scene's local metre frame (pixel_to_meters is the 3x2 affine stored on every scene). */
export function localRing(ring: readonly (readonly number[])[], pixelToMeters: number[][]): Point[] {
  return ring.map(p => affine(p, pixelToMeters));
}
export function ringArea(ring: readonly Point[]): number {
  let twice = 0;
  for (let i = 0; i < ring.length; i++) { const a = ring[i], b = ring[(i + 1) % ring.length]; twice += a[0] * b[1] - b[0] * a[1]; }
  return Math.abs(twice) / 2;
}
export function ringPerimeter(ring: readonly Point[]): number {
  let total = 0;
  for (let i = 0; i < ring.length; i++) { const a = ring[i], b = ring[(i + 1) % ring.length]; total += Math.hypot(b[0] - a[0], b[1] - a[1]); }
  return total;
}
const perpendicular = (p: Point, a: Point, b: Point): number => {
  const dx = b[0] - a[0], dy = b[1] - a[1], length = Math.hypot(dx, dy);
  if (length < 1e-12) return Math.hypot(p[0] - a[0], p[1] - a[1]);
  return Math.abs(dy * p[0] - dx * p[1] + b[0] * a[1] - b[1] * a[0]) / length;
};
/** Douglas-Peucker over the closed ring (first point fixed, as shapely's simplify treats an exterior); at least 3 points survive. */
export function simplifyClosedRing(ring: readonly Point[], tolerance: number): Point[] {
  if (tolerance <= 0 || ring.length < 4) return [...ring];
  const path = [...ring, ring[0]];
  const keep = new Array<boolean>(path.length).fill(false);
  keep[0] = true; keep[path.length - 1] = true;
  const stack: [number, number][] = [[0, path.length - 1]];
  while (stack.length) {
    const [first, last] = stack.pop() as [number, number];
    let farthest = 0, index = -1;
    for (let i = first + 1; i < last; i++) { const d = perpendicular(path[i], path[first], path[last]); if (d > farthest) { farthest = d; index = i; } }
    if (index !== -1 && farthest > tolerance) { keep[index] = true; stack.push([first, index], [index, last]); }
  }
  const out = path.filter((_, i) => keep[i]).slice(0, -1);
  return out.length >= 3 ? out : [...ring];
}
/** Minimum-area rotated rectangle by edge directions (the polygon's own edges carry one side of the answer): width <= length. */
export function minRotatedRect(ring: readonly Point[]): { width: number; length: number; axis_deg: number } {
  let best: { area: number; width: number; length: number; axis_deg: number } | null = null;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i], b = ring[(i + 1) % ring.length];
    if (Math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-9) continue;
    const theta = Math.atan2(b[1] - a[1], b[0] - a[0]), cos = Math.cos(theta), sin = Math.sin(theta);
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [x, y] of ring) { const rx = x * cos + y * sin, ry = -x * sin + y * cos; minX = Math.min(minX, rx); maxX = Math.max(maxX, rx); minY = Math.min(minY, ry); maxY = Math.max(maxY, ry); }
    const w = maxX - minX, h = maxY - minY, area = w * h;
    if (!best || area < best.area) {
      const along = (theta * 180) / Math.PI;
      best = { area, width: Math.min(w, h), length: Math.max(w, h), axis_deg: ((w >= h ? along : along + 90) % 90 + 90) % 90 };
    }
  }
  return best ?? { width: 0, length: 0, axis_deg: 0 };
}
/** Angle between an edge direction and the nearest of the two rectangle axes, degrees in [0, 45]. */
export const axisDeviation = (edgeDeg: number, axisDeg: number): number => {
  const a = ((edgeDeg % 90) + 90) % 90, b = ((axisDeg % 90) + 90) % 90, d = Math.abs(a - b);
  return Math.min(d, 90 - d);
};
/** Every outline measure the hints use, in metres; null for fewer than three corners or a frame without affines. */
export function outlineMetrics(ringPx: readonly (readonly number[])[], pixelToMeters: number[][] | undefined): OutlineMetrics | null {
  if (!Array.isArray(pixelToMeters) || pixelToMeters.length < 3 || ringPx.length < 3) return null;
  const ring = localRing(ringPx, pixelToMeters);
  const area_m2 = ringArea(ring), perimeter_m = ringPerimeter(ring);
  if (!Number.isFinite(area_m2) || !Number.isFinite(perimeter_m)) return null;
  const design = OUTLINE_DESIGN.noisy_outline;
  const simplified = simplifyClosedRing(ring, design.simplify_m);
  const rect = minRotatedRect(ring);
  let edges_measured = 0, off_axis_edges = 0;
  for (let i = 0; i < ring.length; i++) {
    const a = ring[i], b = ring[(i + 1) % ring.length], length = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (length < design.min_edge_m) continue;
    edges_measured++;
    if (axisDeviation((Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI, rect.axis_deg) > design.axis_tolerance_deg) off_axis_edges++;
  }
  const rectArea = rect.width * rect.length;
  return {
    area_m2, perimeter_m, vertices: ring.length, simplified_vertices: simplified.length,
    vertices_per_metre: perimeter_m > 0 ? simplified.length / perimeter_m : 0,
    rect_width_m: rect.width, rect_length_m: rect.length, axis_deg: rect.axis_deg,
    edges_measured, off_axis_edges, wonky_share: edges_measured ? off_axis_edges / edges_measured : 0,
    rectilinearity: rectArea > 0 ? area_m2 / rectArea : 0,
  };
}
const r = (value: number, digits = 2) => Number(value.toFixed(digits));
const metres = (value: number) => (value >= 10 ? value.toFixed(0) : value.toFixed(value >= 1 ? 1 : 2));
const KIND_WORDS: Record<string, string> = { building: "a building", tree: "a tree", vegetation: "vegetation", pool: "a pool" };
/** scene_lot_tags.KIND_NOUNS, copied: "the frame cut it, not the <noun>"; any other kind reads "object". */
const KIND_NOUNS: Record<string, string> = { building: "building", tree: "tree", vegetation: "vegetation", pool: "pool", car: "car", solar_panel: "panel", driveway: "driveway", footpath: "path", road: "road", water: "water" };
/** Signed pixel distance from a point to the nearest image edge; negative beyond the frame. */
const edgeDistance = (p: readonly number[], width: number, height: number): number => Math.min(p[0], p[1], width - p[0], height - p[1]);
/** True when any vertex lies within tolerancePx of an image edge or beyond it (x <= t, y <= t, x >= width - t or y >= height - t). The same
 *  answer as scene_lot_tags' polygon.distance(frame.boundary) <= edge_px: inside the frame the distance to the boundary is smallest at a
 *  vertex (a minimum of linear functions along each straight edge), and a ring that crosses the frame is at distance 0. False without a
 *  finite frame size. */
export function touchesFrame(ring: readonly (readonly number[])[], width: number | undefined, height: number | undefined, tolerancePx: number): boolean {
  if (typeof width !== "number" || typeof height !== "number" || !Number.isFinite(width) || !Number.isFinite(height)) return false;
  return ring.some(p => edgeDistance(p, width, height) <= tolerancePx);
}
/** The outline hints for one object: degenerate_outline (any kind), tiny_area_for_kind (building, tree, vegetation, pool),
 *  noisy_outline (buildings of 20 m² or more) and, when the scene carries its image size, frame_cut (any kind). Rejected and
 *  superseded objects earn nothing. */
export function outlineHints(object: Pick<SceneObject, "kind" | "ring" | "status">, scene: Pick<SceneModelSnapshot, "pixel_to_meters"> & Partial<Pick<SceneModelSnapshot, "image_width" | "image_height">>): OutlineHint[] {
  if (["rejected", "superseded"].includes(object.status)) return [];
  const m = outlineMetrics(object.ring, scene.pixel_to_meters);
  if (!m) return [];
  const out: OutlineHint[] = [];
  const base = { area_m2: r(m.area_m2, 3), vertices: m.vertices, rect_width_m: r(m.rect_width_m), rect_length_m: r(m.rect_length_m) };
  const degenerate = OUTLINE_DESIGN.degenerate_outline;
  if (m.area_m2 < degenerate.max_area_m2 || m.rect_width_m < degenerate.max_rect_width_m) {
    out.push({ code: "degenerate_outline", severity: "review", source: OUTLINE_HINTS_SOURCE,
      sentence: `${m.area_m2.toFixed(2)} m² over ${m.vertices} corners, ${m.rect_width_m.toFixed(2)} by ${m.rect_length_m.toFixed(2)} m; too small to be the outline of anything. Check the image before keeping it.`,
      evidence: { ...base, rule: `area_m2 < ${degenerate.max_area_m2} or rect_width_m < ${degenerate.max_rect_width_m}` } });
  }
  const tiny = OUTLINE_DESIGN.tiny_area_for_kind[object.kind];
  if (tiny !== undefined && m.area_m2 < tiny) {
    out.push({ code: "tiny_area_for_kind", severity: "review", source: OUTLINE_HINTS_SOURCE,
      sentence: `${m.area_m2 < 0.1 ? m.area_m2.toFixed(2) : m.area_m2.toFixed(1)} m² is small for ${KIND_WORDS[object.kind] ?? object.kind} (under ${tiny} m²).`,
      evidence: { ...base, kind: object.kind, rule: `${object.kind} area_m2 < ${tiny}` } });
  }
  const noisy = OUTLINE_DESIGN.noisy_outline;
  if (object.kind === "building" && m.area_m2 >= noisy.min_area_m2) {
    const evidence = { ...base, perimeter_m: r(m.perimeter_m, 1), simplified_vertices: m.simplified_vertices, vertices_per_metre: r(m.vertices_per_metre, 3),
      edges_measured: m.edges_measured, off_axis_edges: m.off_axis_edges, wonky_share: r(m.wonky_share, 3), axis_deg: r(m.axis_deg, 1), rectilinearity: r(m.rectilinearity, 3),
      rule: `vertices_per_metre >= ${noisy.min_vertices_per_metre} and wonky_share >= ${noisy.min_wonky_share} (simplify ${noisy.simplify_m} m, axis tolerance ${noisy.axis_tolerance_deg} deg)` };
    if (m.vertices_per_metre >= noisy.min_vertices_per_metre && m.wonky_share >= noisy.min_wonky_share) {
      out.push({ code: "noisy_outline", severity: "review", source: OUTLINE_HINTS_SOURCE,
        sentence: `${m.vertices} corners along ${metres(m.perimeter_m)} m of edge, ${m.off_axis_edges} of them off the roof's axes; roof edges are usually straight. Check the shadow side.`, evidence });
    }
  }
  const { image_width: width, image_height: height } = scene, cut = OUTLINE_DESIGN.frame_cut;
  if (width !== undefined && height !== undefined && touchesFrame(object.ring, width, height, cut.edge_px)) {
    // The nearest vertex's distance to the frame, floored at 0 as polygon.distance is for a ring that crosses the boundary.
    const edge_px = Math.max(0, Math.min(...object.ring.map(p => edgeDistance(p, width, height))));
    out.push({ code: "frame_cut", severity: "review", source: OUTLINE_HINTS_SOURCE,
      sentence: `Touches the image edge; the frame cut it, not the ${KIND_NOUNS[object.kind] ?? "object"}.`,
      evidence: { edge_px: r(edge_px), width, height, rule: `distance to the frame boundary <= ${cut.edge_px} px (a vertex beyond the frame counts)` } });
  }
  return out;
}
/** "Outline: 19 corners along 72 m of edge, 6 off the roof's axes." for buildings of 20 m² or more (the quiet count the
 *  object-hints module records without a flag); null otherwise. */
export function outlineEvidenceLine(object: Pick<SceneObject, "kind" | "ring" | "status">, scene: Pick<SceneModelSnapshot, "pixel_to_meters">): string | null {
  if (object.kind !== "building" || ["rejected", "superseded"].includes(object.status)) return null;
  const m = outlineMetrics(object.ring, scene.pixel_to_meters);
  if (!m || m.area_m2 < OUTLINE_DESIGN.quiet_evidence_min_area_m2) return null;
  return `Outline: ${m.vertices} corners along ${metres(m.perimeter_m)} m of edge, ${m.off_axis_edges} off the roof's axes (${m.vertices_per_metre.toFixed(2)} corners per metre after a ${OUTLINE_DESIGN.noisy_outline.simplify_m} m simplify).`;
}
/** Served hints first (the index measured the same ring with the frozen module), then editor hints for codes the index did not
 *  carry. Order within each part is kept; nothing is dropped from the served list. */
export function mergeObjectHints<T extends HintLike>(served: readonly T[] | null | undefined, local: readonly OutlineHint[]): (T | OutlineHint)[] {
  const out: (T | OutlineHint)[] = [...(served ?? [])];
  const codes = new Set(out.map(h => h.code));
  for (const hint of local) if (!codes.has(hint.code)) { out.push(hint); codes.add(hint.code); }
  return out;
}
