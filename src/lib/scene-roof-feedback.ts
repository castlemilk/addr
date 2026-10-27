import { currentRoofLineEdits } from "./roof-line-edits";
import { annotationIssues, validAnnotation } from "./roof-annotations";
import { sceneRoofGraph } from "./scene-height";
import { transformPoint, type RoofFamily, type SceneObject, type ScenePoint, type SceneRoofLinework } from "./scene-types";
import { hasConventionalRoof } from "./scene-structure";

export const ROOF_FAMILIES: RoofFamily[] = ["gabled", "hipped", "flat", "mixed"];
export function roofLinework(object: SceneObject): SceneRoofLinework | null {
  const graph = sceneRoofGraph(object);
  return graph ? { vertices: graph.vertices.map(p => [p[0], p[1]]), faces: graph.faces, edges: graph.edges } : null;
}
const outline = (object: SceneObject) => ({ ring: object.ring, holes: object.holes || [] });
const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** Bind observations to the exact footprint/XY graph that was checked. Z is not a label. */
export function currentRoofFeedback(object: SceneObject) {
  const feedback = object.roof_feedback;
  if (!hasConventionalRoof(object) || !feedback || !equal(feedback.outline, outline(object))) return {};
  return { family: feedback.family, linework: !currentRoofLineEdits(object) && !!feedback.linework && equal(feedback.linework, roofLinework(object)) };
}

export function withRoofFeedback(object: SceneObject, change: { family?: RoofFamily | null; linework?: boolean }): SceneObject {
  if (!hasConventionalRoof(object)) return object;
  const current = currentRoofFeedback(object), graph = sceneRoofGraph(object);
  const family = change.family === undefined ? current.family : change.family || undefined;
  const linework = (change.linework ?? current.linework) ? roofLinework(object) || undefined : undefined;
  return { ...object, ...(linework && graph ? { roof_annotation: graph } : {}),
    roof_feedback: family || linework ? structuredClone({ outline: outline(object), ...(family ? { family } : {}), ...(linework ? { linework } : {}) }) : undefined };
}

export function roofLineworkIssue(object: SceneObject, frame: number[][]): string | null {
  const graph = sceneRoofGraph(object);
  if (!graph) return "Create or edit a roof shape first.";
  // Test the image plane independently of estimated heights/pitch.
  return annotationIssues({ ...graph, vertices: graph.vertices.map(p => [p[0], p[1], 0]) },
    object.ring.map(p => transformPoint(p, frame)), object.holes?.map(h => h.map(p => transformPoint(p, frame))))[0] || null;
}

/** Validate retained snapshots too: stale observations stay as audit data, never targets. */
export function validRoofFeedback(object: SceneObject): boolean {
  const value = object.roof_feedback;
  if (value === undefined) return true;
  if (!value || !hasConventionalRoof(object) || typeof value !== "object" || Array.isArray(value) || !value.outline ||
      Object.keys(value).some(k => !["outline", "family", "linework"].includes(k)) ||
      (value.family !== undefined && !ROOF_FAMILIES.includes(value.family))) return false;
  const ring = (r: ScenePoint[]) => Array.isArray(r) && r.length >= 3 && r.length <= 200 && r.every(p =>
    Array.isArray(p) && p.length === 2 && p.every(n => typeof n === "number" && Number.isFinite(n) && n >= 0 && n <= 10000));
  if (!ring(value.outline.ring) || !Array.isArray(value.outline.holes) || value.outline.holes.length > 32 || !value.outline.holes.every(ring)) return false;
  if (value.linework !== undefined) {
    const lines = value.linework;
    if (!lines || !Array.isArray(lines.vertices) || lines.vertices.some(p => !Array.isArray(p) || p.length !== 2) ||
        !Array.isArray(lines.edges) || lines.edges.some(e => !e || typeof e !== "object") ||
        !validAnnotation({ ...lines, vertices: lines.vertices.map(p => [...p, 0]), schema_version: 1, roof_type: "mixed" })) return false;
  }
  return value.family !== undefined || value.linework !== undefined;
}
