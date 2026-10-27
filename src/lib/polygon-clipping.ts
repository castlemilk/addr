import type { ScenePoint } from "./scene-types";

/** Intersect convex polygons in XY. Clipping vertices may also carry heights. */
export function convexIntersection(subject: ScenePoint[], boundary: number[][]): ScenePoint[] {
  const cross = (a: number[], b: number[], p: number[]) =>
    (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0]);
  if (boundary.length < 3) return [];
  const direction=Math.sign(boundary.reduce((s,p,i)=>{
    const q=boundary[(i+1)%boundary.length];return s+p[0]*q[1]-q[0]*p[1];
  },0));
  if (!direction) return [];
  let polygon=subject;
  for(let i=0;i<boundary.length && polygon.length;i++) {
    const a=boundary[i],b=boundary[(i+1)%boundary.length],output:ScenePoint[]=[];
    for(let j=0;j<polygon.length;j++) {
      const p=polygon[j],q=polygon[(j+1)%polygon.length];
      const dp=cross(a,b,p)*direction,dq=cross(a,b,q)*direction;
      if(dp>=-1e-9)output.push(p);
      if((dp>=0)!==(dq>=0) && Math.abs(dp-dq)>1e-12) {
        const t=dp/(dp-dq);output.push([p[0]+(q[0]-p[0])*t,p[1]+(q[1]-p[1])*t]);
      }
    }
    polygon=output;
  }
  return polygon;
}
