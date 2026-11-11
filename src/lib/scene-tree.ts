import { triangulateFootprint } from "./roof-geometry";
import type { SceneModel, ScenePoint } from "./scene-types";

/** Closed crown surfaces over a conforming triangulation of the captured canopy. */
export function treeSurfaces(ring: ScenePoint[], model: SceneModel, holes: ScenePoint[][] = []) {
  const points: ScenePoint[] = [], indices = new Map<string, number>(), triangles: number[][] = [];
  const add = (p: ScenePoint) => {
    const key = p.map(v => v.toFixed(9)).join(":");
    let index = indices.get(key);
    if (index === undefined) { index = points.length; indices.set(key, index); points.push(p); }
    return index;
  };
  const middle = (a: ScenePoint, b: ScenePoint): ScenePoint => [(a[0]+b[0])/2, (a[1]+b[1])/2];
  const divide = (a: ScenePoint, b: ScenePoint, c: ScenePoint, depth: number) => {
    if (depth) {
      const ab=middle(a,b), bc=middle(b,c), ca=middle(c,a);
      divide(a,ab,ca,depth-1); divide(ab,b,bc,depth-1);
      divide(ca,bc,c,depth-1); divide(ab,bc,ca,depth-1);
    } else {
      const center: ScenePoint = [(a[0]+b[0]+c[0])/3, (a[1]+b[1]+c[1])/3];
      for (const [p,q] of [[a,b],[b,c],[c,a]]) {
        // Even an interior chord whose two ends touch the boundary needs an
        // interior height sample; otherwise upper/lower shells pinch together.
        const mid=middle(p,q);
        triangles.push([add(p),add(mid),add(center)],[add(mid),add(q),add(center)]);
      }
    }
  };
  // Subdivision shares edge midpoints across adjacent triangles. Unlike a fan
  // around an average point, no triangle can cross a notch or interior gap.
  const footprint=triangulateFootprint(ring,holes);
  // Ear clipping can emit zero-area faces or skip almost-collinear pixel
  // vertices. Remove the faces and match long edges to shorter neighbours
  // before lifting them into a crown; otherwise they become duplicate fins
  // or cracks at different heights. Saved rings remain untouched.
  const source:ScenePoint[]=[],sourceIndices=new Map<string,number>();
  const remap=footprint.points.map(p=>{
    const key=p.map(v=>v.toFixed(9)).join(":");let i=sourceIndices.get(key);
    if(i===undefined){i=source.length;source.push(p);sourceIndices.set(key,i);}return i;
  });
  const faces=footprint.triangles.map(t=>t.map(i=>remap[i])).filter(t=>{
    const [a,b,c]=t.map(i=>source[i]);
    return Math.abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))>2e-10;
  });
  const edgeKey=(a:number,b:number)=>[a,b].sort((x,y)=>x-y).join(":");
  const edges=new Map<string,{a:number;b:number;count:number}>();
  for(const face of faces)for(let i=0;i<3;i++) {
    const a=face[i],b=face[(i+1)%3],key=edgeKey(a,b),edge=edges.get(key);
    if(edge)edge.count++;else edges.set(key,{a,b,count:1});
  }
  const breaks=new Map<string,number[]>();
  for(const [key,{a,b,count}] of edges)if(count===1) {
    const p=source[a],q=source[b],dx=q[0]-p[0],dy=q[1]-p[1],length2=dx*dx+dy*dy;
    const inside=source.map((point,i)=>({i,t:((point[0]-p[0])*dx+(point[1]-p[1])*dy)/length2,
      distance:Math.abs(dx*(point[1]-p[1])-dy*(point[0]-p[0]))/Math.sqrt(length2)}))
      .filter(v=>v.i!==a&&v.i!==b&&v.t>1e-9&&v.t<1-1e-9&&v.distance<1e-8).sort((u,v)=>u.t-v.t);
    if(inside.length)breaks.set(key,[a,...inside.map(v=>v.i),b]);
  }
  for(const face of faces) {
    const border:number[]=[];
    for(let i=0;i<3;i++) {
      const a=face[i],b=face[(i+1)%3],split=breaks.get(edgeKey(a,b));
      border.push(...(split?(split[0]===a?split:[...split].reverse()).slice(0,-1):[a]));
    }
    if(border.length===3)divide(source[face[0]],source[face[1]],source[face[2]],2);
    else {
      const center:ScenePoint=[face.reduce((sum,i)=>sum+source[i][0],0)/3,face.reduce((sum,i)=>sum+source[i][1],0)/3];
      for(let i=0;i<border.length;i++)divide(center,source[border[i]],source[border[(i+1)%border.length]],2);
    }
  }
  const boundaries=[ring,...holes];
  const distance = (p: ScenePoint) => {
    let nearest=Infinity;
    for(const boundary of boundaries)for(let i=0;i<boundary.length;i++) {
      const a=boundary[i],b=boundary[(i+1)%boundary.length],dx=b[0]-a[0],dy=b[1]-a[1];
      const t=Math.max(0,Math.min(1,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/(dx*dx+dy*dy || 1)));
      nearest=Math.min(nearest,(p[0]-a[0]-t*dx)**2+(p[1]-a[1]-t*dy)**2);
    }
    return Math.sqrt(nearest);
  };
  // Roundoff on boundary subdivision points must not open a hairline seam
  // between the upper and lower crown surfaces after taking a square root.
  const distances=points.map(p=>{const d=distance(p);return d<1e-8?0:d;}), peak=Math.max(0,...distances);
  if (!peak) return null;
  const anchor=points[distances.indexOf(peak)];
  const height=model.height_m, base=model.base_m, ratio=model.tree?.crown_base_ratio ?? .25;
  const crownBase=height*ratio, shoulder=crownBase+(height-crownBase)*.3;
  const profile=model.tree?.crown_profile ?? "rounded";
  const top=distances.map(d=>{
    const t=Math.min(1,d/peak), dome=Math.sqrt(t*(2-t));
    const rise=profile==="conical"?t:profile==="spreading"?Math.pow(dome,.5):dome;
    return base+shoulder+(height-shoulder)*rise;
  });
  const bottom=distances.map(d=>base+shoulder-(shoulder-crownBase)*Math.sqrt(Math.min(1,d/peak)));
  const positions: number[]=[];
  for(const face of triangles) {
    const [a,b,c]=face.map(i=>points[i]);
    const ordered=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])>0?face:[...face].reverse();
    for(const i of ordered)positions.push(...points[i],top[i]);
    for(const i of [...ordered].reverse())positions.push(...points[i],bottom[i]);
  }
  return {positions, anchor, trunk_radius: Math.min(peak*.45,Math.max(.03,height*.025)),
    trunk_height: shoulder, crown_base_m: crownBase, profile};
}
