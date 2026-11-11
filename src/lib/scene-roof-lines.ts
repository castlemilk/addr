import { currentRoofLineEdits } from "./roof-line-edits";
import { transformPoint, type SceneObject, type ScenePoint } from "./scene-types";
import { hasConventionalRoof } from "./scene-structure";

export const ROOF_LINE_COLORS = { ridge: "#69eda6", hip: "#67d7e7", valley: "#d5a5ff" };
export type RoofLineKind = keyof typeof ROOF_LINE_COLORS;

/** Use the saved graph first, in its registered metre frame, just like 3D. */
export function roofImageLines(object: SceneObject | undefined, metersToPixel: number[][]) {
  if (!object || !hasConventionalRoof(object)) return [];
  const graph = currentRoofLineEdits(object) || object.roof_annotation;
  const lines = graph ? graph.edges.flatMap(edge => {
    const a = graph.vertices[edge.a], b = graph.vertices[edge.b];
    return a && b ? [{ kind: edge.kind, line_meters: [a.slice(0, 2), b.slice(0, 2)] }] : [];
  }) : object.roof_type === "flat" ? [] : object.roof_lines || [];
  const seen = new Set<string>();
  return lines.flatMap(line => {
    if (!line.kind || !Object.hasOwn(ROOF_LINE_COLORS, line.kind)) return [];
    const points = line.line_meters.map(p => transformPoint(p as ScenePoint, metersToPixel));
    if (points.length !== 2 || !points.flat().every(Number.isFinite) || Math.hypot(points[0][0]-points[1][0], points[0][1]-points[1][1]) < .01) return [];
    const key = points.map(p => p.map(v => v.toFixed(4)).join(",")).sort().join("/");
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ a: points[0], b: points[1], kind: line.kind as RoofLineKind }];
  });
}
