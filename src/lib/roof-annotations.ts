export type RoofVertex = [number, number, number];
export interface RoofAnnotation {
  schema_version: 1;
  vertices: RoofVertex[];
  faces: number[][];
  edges: { a: number; b: number; kind: string }[];
  roof_type: "hipped" | "gabled" | "flat" | "mixed";
}
export interface RoofObject {
  id: string;
  kind: "solar_panel" | "skylight" | "chimney";
  ring_meters: [number, number][];
  status: "candidate" | "confirmed" | "rejected";
  source: string;
  score?: number;
}

/** A horizontal connected ridge changes elevation as one roof feature. */
export function roofHeightGroup(annotation: RoofAnnotation, index: number): number[] {
  if (!Number.isInteger(index) || !annotation.vertices[index]) return [];
  const group = new Set([index]);
  for (let changed = true; changed;) {
    changed = false;
    for (const edge of annotation.edges) {
      const a = annotation.vertices[edge.a], b = annotation.vertices[edge.b];
      if (edge.kind !== "ridge" || !a || !b || Math.abs(a[2] - b[2]) >= .05 || !(group.has(edge.a) || group.has(edge.b))) continue;
      for (const point of [edge.a, edge.b]) if (!group.has(point)) { group.add(point); changed = true; }
    }
  }
  return [...group].sort((a, b) => a - b);
}

export function withRoofPointHeight(annotation: RoofAnnotation, index: number, height: number): RoofAnnotation {
  if (!Number.isFinite(height) || height < 0 || height > 100) return annotation;
  const group = new Set(roofHeightGroup(annotation, index));
  if (!group.size || [...group].every(i => annotation.vertices[i][2] === height)) return annotation;
  return { ...annotation, vertices: annotation.vertices.map((p, i) => group.has(i) ? [p[0], p[1], height] : [...p]) };
}
type Part = { ring_meters?: [number, number][]; ring?: [number, number][]; ring_z?: number[]; z?: number[] };
type Line = { line_meters: [[number, number], [number, number]]; kind?: string };
export function annotationFromModel(parts: Part[], lines: Line[], roofType = "hipped"): RoofAnnotation {
  const vertices: RoofVertex[] = [], indices = new Map<string, number>();
  const key = (p: number[]) => `${p[0].toFixed(5)},${p[1].toFixed(5)}`;
  const faces = parts.map(p => (p.ring_meters || p.ring || []).map((xy, i) => {
    const k = key(xy);
    if (!indices.has(k)) { indices.set(k, vertices.length); vertices.push([xy[0], xy[1], (p.ring_z || p.z || [])[i] ?? 3]); }
    return indices.get(k)!;
  }));
  const edges = lines.flatMap(l => {
    const a = indices.get(key(l.line_meters[0])), b = indices.get(key(l.line_meters[1]));
    return a === undefined || b === undefined ? [] : [{a, b, kind: l.kind || "ridge"}];
  });
  return {schema_version: 1, vertices, faces, edges, roof_type: ["hipped","gabled","flat","mixed"].includes(roofType) ? roofType as RoofAnnotation["roof_type"] : "mixed"};
}
export function annotationParts(annotation: RoofAnnotation) {
  return annotation.faces.map(face => {
    const ring = face.map(i => annotation.vertices[i].slice(0, 2) as [number, number]);
    const z = face.map(i => annotation.vertices[i][2]);
    const area = Math.abs(ring.reduce((sum,p,i) => sum+p[0]*ring[(i+1)%ring.length][1]-p[1]*ring[(i+1)%ring.length][0],0))/2;
    return {ring, ring_meters:ring, z, ring_z:z, area_m2:area};
  });
}
export function annotationLines(annotation: RoofAnnotation) {
  return annotation.edges.map(({a,b,kind}) => ({kind,
    line_meters:[annotation.vertices[a].slice(0,2),annotation.vertices[b].slice(0,2)] as [[number,number],[number,number]],
    line_z:[annotation.vertices[a][2],annotation.vertices[b][2]] as [number,number]}));
}
export function pointOnBoundary(p: number[], ring: number[][], tolerance = .01) {
  return ring.some((a,i) => {
    const b=ring[(i+1)%ring.length], dx=b[0]-a[0],dy=b[1]-a[1],d=dx*dx+dy*dy;
    if (d<1e-12) return false;
    const t=((p[0]-a[0])*dx+(p[1]-a[1])*dy)/d;
    return t>=-1e-6 && t<=1+1e-6 && Math.abs(dx*(p[1]-a[1])-dy*(p[0]-a[0]))/Math.sqrt(d)<tolerance;
  });
}
export function pointInRoof(p: number[], ring: number[][], holes:number[][][]=[]) {
  if(holes.some(h=>!pointOnBoundary(p,h)&&pointInRoof(p,h)))return false;
  if (pointOnBoundary(p,ring)) return true;
  let inside=false;
  for(let i=0,j=ring.length-1;i<ring.length;j=i++) {
    const a=ring[i],b=ring[j];
    if ((a[1]>p[1]) !== (b[1]>p[1]) && p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0]) inside=!inside;
  }
  return inside;
}
export function validAnnotation(value: unknown): value is RoofAnnotation {
  if (!value || typeof value!=="object") return false;
  const a=value as RoofAnnotation;
  return a.schema_version===1 && ["hipped","gabled","flat","mixed"].includes(a.roof_type)
    && Array.isArray(a.vertices) && a.vertices.length>=3 && a.vertices.length<=1500
    && a.vertices.every(p=>Array.isArray(p)&&p.length===3&&p.every(v=>typeof v==="number"&&Number.isFinite(v)&&Math.abs(v)<=10000)&&p[2]>=0&&p[2]<=100)
    && Array.isArray(a.faces)&&a.faces.length>0&&a.faces.length<=300
    && a.faces.every(f=>Array.isArray(f)&&f.length>=3&&f.length<=150&&f.every(i=>Number.isInteger(i)&&i>=0&&i<a.vertices.length))
    && Array.isArray(a.edges)&&a.edges.length<=1500&&a.edges.every(e=>[e.a,e.b].every(i=>Number.isInteger(i)&&i>=0&&i<a.vertices.length)&&["ridge","hip","valley","eave","step_eave"].includes(e.kind));
}
export function annotationIssues(annotation: RoofAnnotation, boundary: number[][], holes:number[][][]=[]): string[] {
  const issues = new Set<string>();
  if (!validAnnotation(annotation)) return ["Invalid annotation data"];
  if (annotation.vertices.some(p=>!pointInRoof(p,boundary,holes))) issues.add("A roof point is outside the captured outline or inside a gap");
  for (const face of annotation.faces) {
    const points=face.map(i=>annotation.vertices[i]), p=points[0];
    let plane:number[]|undefined;
    for(let i=1;i<points.length-1;i++) {
      const u=points[i].map((v,j)=>v-p[j]),v=points[i+1].map((v,j)=>v-p[j]);
      const n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];
      if(Math.abs(n[2])>1e-5) {plane=n;break;}
    }
    if (!plane) {issues.add("A roof face collapsed");continue;}
    const n=plane;
    if (points.some(q=>Math.abs(n.reduce((sum,v,j)=>sum+v*(q[j]-p[j]),0)/n[2])>.1)) issues.add("Roof slopes need planar adjustment");
    if(Math.hypot(n[0],n[1])/Math.abs(n[2])>Math.tan(70*Math.PI/180)) issues.add("A roof slope is too steep");
  }
  const area=(ring:number[][])=>Math.abs(ring.reduce((s,p,i)=>s+p[0]*ring[(i+1)%ring.length][1]-p[1]*ring[(i+1)%ring.length][0],0))/2;
  const total=annotationParts(annotation).reduce((s,p)=>s+p.area_m2,0), target=area(boundary)-holes.reduce((sum,h)=>sum+area(h),0);
  if(Math.abs(total-target)>Math.max(.1,target*.005)) issues.add("Roof faces do not cover the outline consistently");
  return [...issues];
}
