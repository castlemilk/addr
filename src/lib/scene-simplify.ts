import { polygonIssues, ringArea, transformPoint, type SceneObject, type ScenePoint } from "./scene-types";

/** The editor's Simplify action (decision card item 2, 6 October 2026, "yes to defaults" 7 October): one Douglas-Peucker step on
 *  the selected object of any kind, 1 px per click, then 2 px once 1 px removes nothing; the outer ring and its gaps together;
 *  the result goes through the page's change() (one undo entry) with the area change in the message. A superseded object is never
 *  touched, and a result polygonIssues refuses is not applied. Pure: no DOM, no file. */
export const SIMPLIFY_TOLERANCES_PX: readonly number[] = [1, 2];

function pointSegmentDistance(p: ScenePoint, a: ScenePoint, b: ScenePoint) {
  const dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy;
  const t = l2 > 0 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2)) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

/** Douglas-Peucker on an open line: both ends kept, a vertex kept while it lies more than `tolerance` from the chord. */
function simplifyOpenLine(points: ScenePoint[], tolerance: number): ScenePoint[] {
  if (points.length < 3) return points;
  const keep = new Array<boolean>(points.length).fill(false);
  keep[0] = keep[points.length - 1] = true;
  const stack: [number, number][] = [[0, points.length - 1]];
  while (stack.length) {
    const [first, last] = stack.pop()!;
    let farthest = -1, distance = tolerance;
    for (let i = first + 1; i < last; i++) {
      const d = pointSegmentDistance(points[i], points[first], points[last]);
      if (d > distance) { distance = d; farthest = i; }
    }
    if (farthest !== -1) { keep[farthest] = true; stack.push([first, farthest], [farthest, last]); }
  }
  return points.filter((_, i) => keep[i]);
}

/** Douglas-Peucker on a closed ring, anchored at vertex 0 and the vertex farthest from it so no corner is pinned by its place in
 *  the list alone; each half is an open line. Unchanged when the tolerance is zero, the ring has under four vertices, or fewer
 *  than three would remain. Vertex order is preserved. */
export function simplifyClosedRing(ring: ScenePoint[], tolerance: number): ScenePoint[] {
  if (!(tolerance > 0) || ring.length < 4) return ring;
  let far = 1, best = -1;
  for (let i = 1; i < ring.length; i++) {
    const d = Math.hypot(ring[i][0] - ring[0][0], ring[i][1] - ring[0][1]);
    if (d > best) { best = d; far = i; }
  }
  const first = simplifyOpenLine(ring.slice(0, far + 1), tolerance);
  const second = simplifyOpenLine([...ring.slice(far), ring[0]], tolerance);
  let out = [...first, ...second.slice(1, -1)];
  // The two anchors were kept by position, not by shape: drop either that lies within the tolerance of its neighbours' chord
  // (the far anchor first, so index 0 still names the first vertex).
  for (const anchor of [first.length - 1, 0]) {
    if (out.length <= 3) break;
    const previous = out[(anchor - 1 + out.length) % out.length], next = out[(anchor + 1) % out.length];
    if (pointSegmentDistance(out[anchor], previous, next) <= tolerance) out = out.filter((_, i) => i !== anchor);
  }
  return out.length >= 3 && out.length < ring.length ? out : ring;
}

export type SimplifyOutcome =
  | { ok: true; object: SceneObject; tolerance: number; before: { vertices: number; area_m2: number }; after: { vertices: number; area_m2: number } }
  | { ok: false; reason: string };

const vertexCount = (ring: ScenePoint[], holes: ScenePoint[][]) => ring.length + holes.reduce((sum, hole) => sum + hole.length, 0);
/** Metric area of an outline less its gaps, rounded to 2 dp as footprintMetrics rounds model.area_m2. */
export function outlineAreaM2(ring: ScenePoint[], holes: ScenePoint[][], pixelToMeters: number[][]) {
  const area = (r: ScenePoint[]) => ringArea(r.map(p => transformPoint(p, pixelToMeters)));
  return Math.round((area(ring) - holes.reduce((sum, hole) => sum + area(hole), 0)) * 100) / 100;
}

/** One simplification step for the selected object. The 1 px step first; when it removes no vertex (an outline simplified
 *  already, or one with no near-collinear point) the 2 px step; when neither removes a vertex the action refuses. The result is
 *  checked by polygonIssues against the image before it is offered; the object comes back as needs_adjustment with
 *  geometry_reviewed false, like a dragged vertex or the squared candidate. */
export function simplifyOutline(object: SceneObject, scene: { image_width: number; image_height: number; pixel_to_meters: number[][] }): SimplifyOutcome {
  if (object.status === "superseded") return { ok: false, reason: "A superseded outline is history; simplify its replacement instead." };
  const holes = object.holes ?? [];
  const beforeCount = vertexCount(object.ring, holes);
  for (const tolerance of SIMPLIFY_TOLERANCES_PX) {
    const ring = simplifyClosedRing(object.ring, tolerance);
    const nextHoles = holes.map(hole => simplifyClosedRing(hole, tolerance));
    const afterCount = vertexCount(ring, nextHoles);
    if (afterCount === beforeCount) continue;
    const issues = polygonIssues(ring, scene.image_width, scene.image_height, nextHoles);
    if (issues.length) return { ok: false, reason: `Simplify at ${tolerance} px refused: ${issues[0]}. The outline is unchanged.` };
    return {
      ok: true,
      tolerance,
      object: { ...object, ring, ...(object.holes ? { holes: nextHoles } : {}), status: "needs_adjustment", geometry_reviewed: false },
      before: { vertices: beforeCount, area_m2: outlineAreaM2(object.ring, holes, scene.pixel_to_meters) },
      after: { vertices: afterCount, area_m2: outlineAreaM2(ring, nextHoles, scene.pixel_to_meters) },
    };
  }
  const last = SIMPLIFY_TOLERANCES_PX[SIMPLIFY_TOLERANCES_PX.length - 1];
  return { ok: false, reason: `Nothing to simplify: no point of this outline lies within ${last} px of the line between its neighbours.` };
}

/** The status line after a step: points before and after, the tolerance, the area before and after with its signed change. */
export function simplifyMessage(result: Extract<SimplifyOutcome, { ok: true }>, label: string): string {
  const change = result.before.area_m2 > 0 ? ((result.after.area_m2 - result.before.area_m2) / result.before.area_m2) * 100 : 0;
  const sign = change > 0.05 ? "+" : change < -0.05 ? "−" : "±";
  return `${label}: simplified at ${result.tolerance} px, ${result.before.vertices} → ${result.after.vertices} points · area ${result.before.area_m2.toFixed(1)} → ${result.after.area_m2.toFixed(1)} m² (${sign}${Math.abs(change).toFixed(1)} %) · Undo edit restores the outline.`;
}
