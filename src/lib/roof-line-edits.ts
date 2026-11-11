import { annotationFromModel, pointOnBoundary } from "./roof-annotations";
import { transformPoint, polygonIssues, type SceneObject, type ScenePoint } from "./scene-types";
import { hasConventionalRoof } from "./scene-structure";

export const LINE_KINDS = ["ridge", "hip", "valley"] as const;
export type RoofFeedbackLineKind = typeof LINE_KINDS[number];
export type RoofFeedbackEdge = { a: number; b: number; kind: RoofFeedbackLineKind };
export interface RoofLineGraph { vertices: ScenePoint[]; edges: RoofFeedbackEdge[] }
export interface RoofLineEdits extends RoofLineGraph {
  schema_version: 1;
  outline: { ring: ScenePoint[]; holes: ScenePoint[][] };
  baseline: RoofLineGraph;
  checked: boolean;
  notes: string;
}
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const cross = (a: number[], b: number[]) => a[0]*b[1]-a[1]*b[0];
const delta = (a: number[], b: number[]) => [a[0]-b[0], a[1]-b[1]];
export const lineOutline = (o: SceneObject) => ({ ring: o.ring, holes: o.holes || [] });
export const currentRoofLineEdits = (o: SceneObject) => hasConventionalRoof(o) && o.roof_line_edits && same(o.roof_line_edits.outline, lineOutline(o)) ? o.roof_line_edits : null;
export function estimatedLineGraph(o: SceneObject): RoofLineGraph {
  if(!hasConventionalRoof(o))return {vertices:[],edges:[]};
  const graph = o.roof_annotation || (o.roof_parts?.length ? annotationFromModel(o.roof_parts, o.roof_lines || [], o.roof_type) : null);
  return graph ? { vertices: graph.vertices.map(p => [p[0],p[1]]), edges: graph.edges.filter(e => LINE_KINDS.includes(e.kind as RoofFeedbackLineKind)).map(e => ({...e,kind:e.kind as RoofFeedbackLineKind})) } : {vertices:[],edges:[]};
}
export function initialRoofLineEdits(o: SceneObject): RoofLineEdits {
  if(!hasConventionalRoof(o))throw new Error("Conventional roof lines are unavailable for this object type.");
  const baseline = estimatedLineGraph(o);
  return {schema_version:1, outline:structuredClone(lineOutline(o)), baseline:structuredClone(baseline), ...structuredClone(baseline), checked:false, notes:""};
}
function intersection(a: ScenePoint, b: ScenePoint, c: ScenePoint, d: ScenePoint): number[] {
  const u=delta(b,a),v=delta(d,c),w=delta(c,a),den=cross(u,v),length=u[0]**2+u[1]**2;
  if(length<1e-12)return [];
  if(Math.abs(den)<1e-9) return Math.abs(cross(w,u))<1e-8 ? [c,d].map(p=>{const q=delta(p,a);return (q[0]*u[0]+q[1]*u[1])/length;}).filter(t=>t>0&&t<1) : [];
  const t=cross(w,v)/den,s=cross(w,u)/den;
  return t>1e-7&&t<1-1e-7&&s>=-1e-7&&s<=1+1e-7 ? [t] : [];
}
const inRoof=(p:ScenePoint,ring:ScenePoint[],holes:ScenePoint[][])=>{
  const inside=(r:ScenePoint[])=>{let value=false;for(let i=0,j=r.length-1;i<r.length;j=i++){
    const a=r[i],b=r[j];if((a[1]>p[1])!==(b[1]>p[1])&&p[0]<(b[0]-a[0])*(p[1]-a[1])/(b[1]-a[1])+a[0])value=!value;
  }return value;};
  return (pointOnBoundary(p,ring,1e-7)||inside(ring))&&!holes.some(h=>!pointOnBoundary(p,h,1e-7)&&inside(h));
};
/** Test every boundary crossing interval, including narrow courtyards/recesses. */
export function lineInsideRoof(a: ScenePoint,b:ScenePoint,ring:ScenePoint[],holes:ScenePoint[][]=[]) {
  if(!inRoof(a,ring,holes)||!inRoof(b,ring,holes))return false;
  const ts=[0,1];
  for(const boundary of [ring,...holes])for(let i=0;i<boundary.length;i++)ts.push(...intersection(a,b,boundary[i],boundary[(i+1)%boundary.length]));
  ts.sort((x,y)=>x-y);
  return ts.slice(1).every((t,i)=>{const k=(t+ts[i])/2;return inRoof([a[0]+k*(b[0]-a[0]),a[1]+k*(b[1]-a[1])],ring,holes);});
}
/** Shared endpoints and crossing lines become explicit editable junctions. */
export function connectRoofLines(graph: RoofLineGraph): RoofLineGraph {
  const vertices:ScenePoint[]=graph.vertices.map(p=>[...p]),edges:RoofFeedbackEdge[]=[],seen=new Set<string>();
  const vertex=(point:ScenePoint)=>{let i=vertices.findIndex(p=>Math.hypot(p[0]-point[0],p[1]-point[1])<1e-5);if(i<0){i=vertices.length;vertices.push([...point]);}return i;};
  for(const edge of graph.edges){
    const a=graph.vertices[edge.a],b=graph.vertices[edge.b];if(!a||!b||Math.hypot(a[0]-b[0],a[1]-b[1])<.01)continue;
    const ts=[0,1];
    for(const other of graph.edges)ts.push(...intersection(a,b,graph.vertices[other.a],graph.vertices[other.b]));
    const ordered=[...new Set(ts.map(t=>Math.round(t*1e8)/1e8))].sort((x,y)=>x-y);
    for(let i=1;i<ordered.length;i++){
      const points=[ordered[i-1],ordered[i]].map(t=>[a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1])] as ScenePoint);
      const x=vertex(points[0]),y=vertex(points[1]),key=[x,y].sort((a,b)=>a-b).join(":");
      if(x===y||seen.has(key))continue;seen.add(key);edges.push({a:x,b:y,kind:edge.kind});
    }
  }
  return compactRoofLineGraph({vertices,edges});
}
/** Keep only vertices that an edge references; reindex a/b; edge order and junction sharing are preserved. */
export function compactRoofLineGraph(graph: RoofLineGraph): RoofLineGraph {
  const referenced=new Set<number>();
  for(const edge of graph.edges){referenced.add(edge.a);referenced.add(edge.b);}
  const index=new Map<number,number>(),vertices:ScenePoint[]=[];
  graph.vertices.forEach((p,i)=>{if(referenced.has(i)){index.set(i,vertices.length);vertices.push([p[0],p[1]]);}});
  return {vertices,edges:graph.edges.map(e=>({...e,a:index.get(e.a)??e.a,b:index.get(e.b)??e.b}))};
}
/** Rebind pending line edits to the object's current outline. Edges that leave the new roof are dropped (the caller reports
 *  the count; the page undo restores them) because the server validates every edge against the captured outline. `checked`
 *  resets: the containment context changed. Baseline and notes are untouched. */
export function reanchorRoofLineEdits(o: SceneObject, pixelToMeters: number[][]): { object: SceneObject; dropped: number } {
  const edits=o.roof_line_edits;
  if(!hasConventionalRoof(o)||!edits||same(edits.outline,lineOutline(o)))return {object:o,dropped:0};
  const ring=o.ring.map(p=>transformPoint(p,pixelToMeters)),holes=(o.holes||[]).map(h=>h.map(p=>transformPoint(p,pixelToMeters)));
  const kept=edits.edges.filter(e=>{const a=edits.vertices[e.a],b=edits.vertices[e.b];return !!a&&!!b&&lineInsideRoof(a,b,ring,holes);});
  return {
    object:{...o,roof_line_edits:{...edits,outline:structuredClone(lineOutline(o)),...compactRoofLineGraph({vertices:edits.vertices,edges:kept}),checked:false}},
    dropped:edits.edges.length-kept.length,
  };
}
export function roofLineGraphIssue(graph: RoofLineGraph, ring: ScenePoint[], holes:ScenePoint[][]=[], checkContainment=true): string|null {
  if(!graph || !Array.isArray(graph.vertices)||!Array.isArray(graph.edges)||graph.vertices.length>600||graph.edges.length>600||
    graph.vertices.some(p=>!Array.isArray(p)||p.length!==2||p.some(v=>typeof v!=="number"||!Number.isFinite(v)||Math.abs(v)>10000)))return "Use at most 600 finite roof points and lines.";
  const keys=new Set<string>();
  for(const edge of graph.edges){
    if(!edge||![edge.a,edge.b].every(i=>Number.isInteger(i)&&i>=0&&i<graph.vertices.length)||edge.a===edge.b||!LINE_KINDS.includes(edge.kind))return "Check the roof line endpoints and types.";
    const a=graph.vertices[edge.a],b=graph.vertices[edge.b],key=[edge.a,edge.b].sort((a,b)=>a-b).join(":");
    if(Math.hypot(a[0]-b[0],a[1]-b[1])<.01||keys.has(key))return "Remove duplicate or collapsed roof lines.";
    keys.add(key);
    if(checkContainment&&!lineInsideRoof(a,b,ring,holes))return "Keep each roof line inside the visible outline and outside gaps.";
  }
  return null;
}
export function roofLineEditsIssue(o:SceneObject,frame:number[][],width:number,height:number): string|null {
  const value=o.roof_line_edits;if(value===undefined)return null;
  if(!hasConventionalRoof(o)||!value||value.schema_version!==1||typeof value.checked!=="boolean"||typeof value.notes!=="string"||value.notes.length>3000||
    !value.outline||!Array.isArray(value.outline.holes)||polygonIssues(value.outline.ring,width,height,value.outline.holes).length)return "Check the roof line feedback and its captured outline.";
  const ring=value.outline.ring.map(p=>transformPoint(p,frame)),holes=value.outline.holes.map(h=>h.map(p=>transformPoint(p,frame)));
  // Retain a flawed prediction for comparison without preventing its correction.
  return roofLineGraphIssue(value,ring,holes)||roofLineGraphIssue(value.baseline,ring,holes,false);
}
export function withRoofLineEdits(o:SceneObject,value:RoofLineEdits):SceneObject {
  if(!hasConventionalRoof(o))throw new Error("Conventional roof lines are unavailable for this object type.");
  return {...o,roof_line_edits:structuredClone(value)};
}
export function roofLineChanges(baseline:RoofLineGraph,current:RoofLineGraph) {
  const key=(graph:RoofLineGraph,e:RoofFeedbackEdge)=>[graph.vertices[e.a],graph.vertices[e.b]].map(p=>p.map(v=>v.toFixed(4)).join(",")).sort().join("/")+":"+e.kind;
  const before=new Set(baseline.edges.map(e=>key(baseline,e))),after=new Set(current.edges.map(e=>key(current,e)));
  return {baseline_lines:before.size,edited_lines:after.size,removed_or_moved:[...before].filter(k=>!after.has(k)).length,added_or_moved:[...after].filter(k=>!before.has(k)).length};
}
