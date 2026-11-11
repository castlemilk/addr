import { annotationParts } from "./roof-annotations";
import { buildRooflineMesh, triangulateFootprint } from "./roof-geometry";
import { transformPoint, type SceneModelSnapshot, type SceneObject, type ScenePoint } from "./scene-types";
import { convexIntersection as clip } from "./polygon-clipping";
import { solarPlane, type SolarPlane, type SolarSupportTriangle } from "./scene-solar";
import { isShadeSail } from "./scene-structure";

type Point3 = [number, number, number];
export type RoofSupport = {
  area_m2: number;
  outside_outline_m2: number;
  uncovered_roof_m2?: number;
  state: "supported" | "outside_outline" | "roof_gap" | "roof_overlap" | "invalid_roof";
};
export type RoofMount = { positions:number[];min:number;max:number;mode:"roof"|"flat"|"absolute";issue?:string;solar?:SolarPlane;support?:RoofSupport };
const cross = (a: number[], b: number[], p: number[]) =>
  (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
const area = (ring: number[][]) => {
  let result = 0;
  for (let i = 1; i < ring.length - 1; i++) result += cross(ring[0], ring[i], ring[i + 1]) / 2;
  return Math.abs(result);
};

/** Exact intersection of triangulated, possibly concave outlines in metre space. */
export function outlineSupport(object: SceneObject, parent: SceneObject, frame: SceneModelSnapshot["pixel_to_meters"]) {
  const footprint = (o:SceneObject)=>triangulateFootprint(o.ring.map(p=>transformPoint(p,frame)),o.holes?.map(h=>h.map(p=>transformPoint(p,frame))));
  const {points:ring,triangles:faces}=footprint(object),{points:boundary,triangles:supports}=footprint(parent);
  let covered = 0;
  for (const face of faces) for (const support of supports) covered += area(clip(face.map(i => ring[i]), support.map(i => boundary[i])));
  const target = faces.reduce((sum,face)=>sum+area(face.map(i=>ring[i])),0);
  return {area_m2: target, outside_outline_m2: Math.max(0, target - covered)};
}

/** Keep gaps open even if retained roof faces still span their interiors. */
export function parentRoofMesh(parent:SceneObject,frame:SceneModelSnapshot["pixel_to_meters"]) {
  if(isShadeSail(parent))return [];
  const parts=parent.roof_annotation?annotationParts(parent.roof_annotation):parent.roof_parts;
  const ring=parent.ring.map(p=>transformPoint(p,frame));
  const raw=buildRooflineMesh(parts?.length?parts.map(p=>({ring:p.ring_meters||p.ring||[],z:p.ring_z||p.z||[]})):
    [{ring,z:ring.map(()=>parent.model.height_m)}]);
  if(!parent.holes?.length)return raw;
  const footprint=triangulateFootprint(ring,parent.holes.map(h=>h.map(p=>transformPoint(p,frame)))),positions:number[]=[];
  for(let i=0;i<raw.length;i+=9){
    const triangle=[raw.slice(i,i+3),raw.slice(i+3,i+6),raw.slice(i+6,i+9)], [a,b,c]=triangle,denominator=cross(a,b,c);
    if(Math.abs(denominator)<1e-10)continue;
    for(const face of footprint.triangles){
      const polygon=clip(face.map(index=>footprint.points[index]),triangle);
      for(let j=1;j<polygon.length-1;j++){
        const points=[polygon[0],polygon[j],polygon[j+1]];
        if(area(points)<1e-10)continue;
        if(cross(...points as [ScenePoint,ScenePoint,ScenePoint])<0)points.reverse();
        for(const p of points)positions.push(...p,(cross(b,c,p)*a[2]+cross(c,a,p)*b[2]+cross(a,b,p)*c[2])/denominator);
      }
    }
  }
  return positions;
}

export function roofMount(
  object: SceneObject,
  objects: SceneObject[],
  frame: SceneModelSnapshot["pixel_to_meters"],
): RoofMount {
  const absolute = { positions: [], min: object.model.base_m, max: object.model.base_m, mode: "absolute" as const };
  if (!["solar_panel", "skylight"].includes(object.kind)) return absolute;
  if(object.model.placement==="absolute"){
    if(object.kind!=="solar_panel")return absolute;
    const solar=solarPlane(object.ring.map(p=>transformPoint(p,frame)),object.model,[],object.holes?.map(h=>h.map(p=>transformPoint(p,frame))));
    return {...absolute,positions:solar.positions,min:solar.min,max:solar.max,solar};
  }
  const parent = objects.find(o => o.id === object.parent_id && o.kind === "building" && !["rejected","superseded"].includes(o.status));
  if (!parent) return { ...absolute, issue: "Choose a building parent to fit this object to its roof." };
  if (isShadeSail(parent)) return { ...absolute, issue: "A fabric shade sail has no modelled supporting roof surface." };
  const outer=object.ring.map(p=>transformPoint(p,frame)),holes=object.holes?.map(h=>h.map(p=>transformPoint(p,frame)));
  const {points:ring,triangles:faces}=triangulateFootprint(outer,holes);
  const parts = parent.roof_annotation ? annotationParts(parent.roof_annotation) : parent.roof_parts;
  const roof = parentRoofMesh(parent,frame);
  const target = faces.reduce((area, [a, b, c]) => area + Math.abs(cross(ring[a], ring[b], ring[c])) / 2, 0);
  const outline = outlineSupport(object, parent, frame);
  const positions: number[] = [];
  const patches: { triangle: SolarSupportTriangle; bounds: number[] }[] = [];
  let covered = 0, min = Infinity, max = -Infinity;
  const thickness = Math.max(.025, object.model.height_m);
  for (let i = 0; i < roof.length; i += 9) {
    const triangle = [roof.slice(i, i + 3), roof.slice(i + 3, i + 6), roof.slice(i + 6, i + 9)] as Point3[];
    const [a, b, c] = triangle, denominator = cross(a, b, c);
    if (Math.abs(denominator) < 1e-10) continue; // Vertical step walls cannot support an array.
    const lift = (p: ScenePoint): Point3 => [p[0], p[1], parent.model.base_m +
      (cross(b, c, p) * a[2] + cross(c, a, p) * b[2] + cross(a, b, p) * c[2]) / denominator];
    for (const face of faces) {
      const polygon = clip(face.map(index => ring[index]), triangle);
      for (let j = 1; j < polygon.length - 1; j++) {
        const points = [polygon[0], polygon[j], polygon[j + 1]];
        const area = Math.abs(cross(...points as [ScenePoint, ScenePoint, ScenePoint])) / 2;
        if (area < 1e-10) continue;
        if (cross(points[0], points[1], points[2]) < 0) points.reverse();
        covered += area;
        const bottom = points.map(lift), top = bottom.map(([x, y, z]) => [x, y, z + thickness]);
        patches.push({ triangle: bottom as SolarSupportTriangle, bounds: [
          Math.min(...points.map(p => p[0])), Math.min(...points.map(p => p[1])),
          Math.max(...points.map(p => p[0])), Math.max(...points.map(p => p[1])),
        ] });
        for (const p of bottom) { min = Math.min(min, p[2]); max = Math.max(max, p[2]); }
        positions.push(...top.flat(), ...[...bottom].reverse().flat());
        for (let k = 0; k < 3; k++) {
          const n = (k + 1) % 3;
          positions.push(...bottom[k], ...bottom[n], ...top[n], ...bottom[k], ...top[n], ...top[k]);
        }
      }
    }
  }
  // Equal total area alone cannot distinguish complete support from a gap plus
  // an equally sized overlap. Check the actual projected patches as well.
  let overlap = false;
  for (let i = 0; i < patches.length && !overlap; i++) {
    const a = patches[i];
    for (let j = i + 1; j < patches.length; j++) {
      const b = patches[j];
      if (a.bounds[2] <= b.bounds[0] + 1e-9 || b.bounds[2] <= a.bounds[0] + 1e-9 ||
          a.bounds[3] <= b.bounds[1] + 1e-9 || b.bounds[3] <= a.bounds[1] + 1e-9) continue;
      const intersection = clip(a.triangle.map(p => [p[0], p[1]]), b.triangle);
      let area = 0;
      for (let k = 1; k < intersection.length - 1; k++)
        area += Math.abs(cross(intersection[0], intersection[k], intersection[k + 1])) / 2;
      if (area > Math.max(1e-8, target * 1e-6)) { overlap = true; break; }
    }
  }
  const tolerance = Math.max(1e-6, target * 1e-5);
  const state: RoofSupport["state"] = !target || !positions.every(Number.isFinite) ? "invalid_roof"
    : outline.outside_outline_m2 > tolerance ? "outside_outline"
    : overlap ? "roof_overlap" : Math.abs(covered - target) > tolerance ? "roof_gap" : "supported";
  const support: RoofSupport = {...outline, state, uncovered_roof_m2: overlap ? undefined : Math.max(0, target - covered)};
  if (state !== "supported") {
    const issue = state === "outside_outline"
      ? `${outline.outside_outline_m2.toFixed(2)} m² of this object lies outside the parent building outline. Check both outlines or choose another parent.`
      : state === "roof_overlap" ? "The parent roof has overlapping faces beneath this object. Review the parent roof shape."
      : state === "roof_gap" ? "The parent roof faces do not cover the entire object. Review the parent roof shape."
      : "The parent roof cannot provide a valid supporting surface. Review its outline and roof shape.";
    return {...absolute, support, issue: `${issue} The preview uses base elevation.`};
  }
  if(object.kind==="solar_panel"){
    const solar=solarPlane(outer,object.model,patches.map(p=>p.triangle),holes);
    return {positions:solar.positions,min:solar.min,max:solar.max,mode:parts?.length?"roof":"flat",solar,support,
      issue:solar.roof_variation_m>.15?`The roof varies by ${solar.roof_variation_m.toFixed(2)} m beneath this flat bank. Check the roof shape, mounting direction or split banks on different slopes.`:undefined};
  }
  return { positions, min, max, mode: parts?.length ? "roof" : "flat", support };
}
