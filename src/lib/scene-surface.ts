import { triangulateFootprint } from "./roof-geometry";
import { convexIntersection } from "./polygon-clipping";
import { footprintMetrics, ringArea, type SceneKind, type SceneModel, type ScenePoint } from "./scene-types";

export const isPavedSurface = (kind: SceneKind) => ["road","driveway","footpath"].includes(kind);

export function surfaceSettings(model: SceneModel) {
  return {profile:model.surface?.profile ?? "plane",grade_percent:model.surface?.grade_percent ?? 0,
    crossfall_percent:model.surface?.crossfall_percent ?? 0,heading_deg:model.surface?.heading_deg};
}

/** A planar/crowned surface over the exact outline, with constant vertical thickness. */
export function pavedSurface(ring: ScenePoint[], model: SceneModel, holes:ScenePoint[][]=[]) {
  const settings=surfaceSettings(model),metrics=footprintMetrics(ring,[[1,0],[0,1],[0,0]]);
  const heading=settings.heading_deg ?? (90-metrics.rotation_deg+360)%360;
  const angle=(90-heading)*Math.PI/180,c=Math.cos(angle),s=Math.sin(angle);
  const local=ring.map(([x,y]):ScenePoint=>[x*c+y*s,-x*s+y*c]);
  const localHoles=holes.map(h=>h.map(([x,y]):ScenePoint=>[x*c+y*s,-x*s+y*c]));
  const xs=local.map(p=>p[0]),ys=local.map(p=>p[1]);
  const minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(...ys),maxY=Math.max(...ys);
  const cx=(minX+maxX)/2,cy=(minY+maxY)/2;
  const world=([x,y]:ScenePoint):ScenePoint=>[x*c-y*s,x*s+y*c];
  const center=world([cx,cy]),front=world([cx+(maxX-minX)*.3,cy]);
  const points:ScenePoint[]=[],indices=new Map<string,number>();let triangles:number[][]=[];
  const add=(p:ScenePoint)=>{
    const key=p.map(v=>v.toFixed(9)).join(":");let index=indices.get(key);
    if(index===undefined){index=points.length;points.push(p);indices.set(key,index);}return index;
  };
  const regions=settings.profile==="crowned"
    ? [[[minX-1,minY-1],[maxX+1,minY-1],[maxX+1,cy],[minX-1,cy]],
      [[minX-1,cy],[maxX+1,cy],[maxX+1,maxY+1],[minX-1,maxY+1]]] as ScenePoint[][] : null;
  const footprint=triangulateFootprint(local,localHoles);
  for(const face of footprint.triangles) {
    const source=face.map(i=>footprint.points[i]);
    for(const polygon of regions?regions.map(region=>convexIntersection(source,region)):[source]) {
      for(let i=1;i<polygon.length-1;i++) {
        const triangle=[polygon[0],polygon[i],polygon[i+1]];
        if(ringArea(triangle)<1e-10)continue;
        const [a,b,d]=triangle;
        if((b[0]-a[0])*(d[1]-a[1])-(b[1]-a[1])*(d[0]-a[0])<0)triangle.reverse();
        triangles.push(triangle.map(add));
      }
    }
  }
  // Ear clipping can skip almost-collinear vertices on pixel outlines. Match
  // the resulting long edges to their shorter neighbours before adding skirts;
  // otherwise an internal diagonal is mistaken for an outside wall.
  const edgeKey=(a:number,b:number)=>[a,b].sort((x,y)=>x-y).join(":");
  const initialEdges=new Map<string,{a:number;b:number;count:number}>();
  for(const triangle of triangles)for(let i=0;i<3;i++) {
    const a=triangle[i],b=triangle[(i+1)%3],key=edgeKey(a,b),edge=initialEdges.get(key);
    if(edge)edge.count++;else initialEdges.set(key,{a,b,count:1});
  }
  const breaks=new Map<string,number[]>();
  for(const [key,{a,b,count}] of initialEdges)if(count===1) {
    const p=points[a],q=points[b],dx=q[0]-p[0],dy=q[1]-p[1],length2=dx*dx+dy*dy;
    const inside=points.map((point,i)=>({i,t:((point[0]-p[0])*dx+(point[1]-p[1])*dy)/length2,
      distance:Math.abs(dx*(point[1]-p[1])-dy*(point[0]-p[0]))/Math.sqrt(length2)}))
      .filter(v=>v.i!==a&&v.i!==b&&v.t>1e-9&&v.t<1-1e-9&&v.distance<1e-8).sort((u,v)=>u.t-v.t);
    if(inside.length)breaks.set(key,[a,...inside.map(v=>v.i),b]);
  }
  triangles=triangles.flatMap(triangle=>{
    const border:number[]=[];
    for(let i=0;i<3;i++) {
      const a=triangle[i],b=triangle[(i+1)%3],split=breaks.get(edgeKey(a,b));
      border.push(...(split?(split[0]===a?split:[...split].reverse()).slice(0,-1):[a]));
    }
    if(border.length===3)return [triangle];
    const center=add([triangle.reduce((v,i)=>v+points[i][0],0)/3,triangle.reduce((v,i)=>v+points[i][1],0)/3]);
    return border.map((a,i)=>[center,a,border[(i+1)%border.length]]);
  });
  const heights=points.map(([u,v])=>(u-cx)*settings.grade_percent/100 +
    (settings.profile==="crowned"?-Math.abs(v-cy)*Math.abs(settings.crossfall_percent):(v-cy)*settings.crossfall_percent)/100);
  const low=Math.min(...heights),high=Math.max(...heights),thickness=Math.max(0,model.height_m);
  const top=points.map((p,i)=>[...world(p),model.base_m+heights[i]-low+thickness]);
  const bottom=top.map(([x,y,z])=>[x,y,z-thickness]);
  const positions:number[]=[],top_positions:number[]=[],edges=new Map<string,{a:number;b:number;count:number}>();
  for(const triangle of triangles) {
    const face=triangle.flatMap(i=>top[i]);top_positions.push(...face);positions.push(...face);
    if(thickness)positions.push(...[...triangle].reverse().flatMap(i=>bottom[i]));
    for(let i=0;i<3;i++){
      const a=triangle[i],b=triangle[(i+1)%3],key=edgeKey(a,b);
      const edge=edges.get(key);if(edge)edge.count++;else edges.set(key,{a,b,count:1});
    }
  }
  if(thickness)for(const {a,b,count} of edges.values())if(count===1)
    positions.push(...bottom[a],...bottom[b],...top[b],...bottom[a],...top[b],...top[a]);
  return {settings,heading,center,front,positions,top_positions,
    min_elevation:model.base_m+thickness,max_elevation:model.base_m+thickness+(Number.isFinite(high-low)?high-low:0)};
}
