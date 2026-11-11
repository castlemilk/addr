import { triangulateRing } from "./roof-geometry";
import { convexIntersection } from "./polygon-clipping";
import { footprintMetrics, ringArea, type SceneModel, type ScenePoint } from "./scene-types";

export const CAR_PROFILES = {
  car: { label: "Car", length: .54, offset: -.05, body: .58, rear: .12, front: .27, inset: .13 },
  suv: { label: "SUV / estate", length: .70, offset: -.08, body: .62, rear: .06, front: .20, inset: .09 },
  van: { label: "Van", length: .80, offset: -.05, body: .48, rear: .03, front: .15, inset: .05 },
} as const;

export function carSettings(model: SceneModel) {
  const profile=model.car?.profile || "car", defaults=CAR_PROFILES[profile];
  return { profile, cabin_length_ratio: model.car?.cabin_length_ratio ?? defaults.length,
    cabin_width_ratio: model.car?.cabin_width_ratio ?? .84,
    cabin_offset_ratio: model.car?.cabin_offset_ratio ?? defaults.offset,
    heading_deg: model.car?.heading_deg };
}

/** Fit a cabin to the current metre-space outline, independent of vertex density. */
export function carPlan(ring: ScenePoint[], model: SceneModel) {
  const settings=carSettings(model);
  const metrics=footprintMetrics(ring,[[1,0],[0,1],[0,0]]);
  const heading=settings.heading_deg ?? ((90-metrics.rotation_deg+360)%360);
  const angle=(90-heading)*Math.PI/180,c=Math.cos(angle),s=Math.sin(angle);
  const points=ring.map(([x,y]):ScenePoint=>[x*c+y*s,-x*s+y*c]);
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
  const length=Math.max(...xs)-Math.min(...xs),width=Math.max(...ys)-Math.min(...ys);
  const cx=(Math.min(...xs)+Math.max(...xs))/2+settings.cabin_offset_ratio*length;
  const cy=(Math.min(...ys)+Math.max(...ys))/2;
  const world=([x,y]:ScenePoint):ScenePoint=>[x*c-y*s,x*s+y*c];
  const triangles=triangulateRing(points.map(([x,y])=>({x,y}))).map(face=>face.map(i=>points[i]));
  const rectangle=(scale:number):ScenePoint[]=>{
    const l=length*settings.cabin_length_ratio*scale/2,w=width*settings.cabin_width_ratio*scale/2;
    return [[cx-l,cy-w],[cx+l,cy-w],[cx+l,cy+w],[cx-l,cy+w]];
  };
  const fits=(scale:number)=>{
    const box=rectangle(scale),area=ringArea(box);
    const covered=triangles.reduce((sum,triangle)=>sum+ringArea(convexIntersection(triangle,box)),0);
    return area>1e-12 && Math.abs(covered-area)<=Math.max(1e-10,area*1e-9);
  };
  let scale=1;
  if(!fits(1)) {
    let low=0,high=1;
    // Nested rectangles make containment monotonic, including concave outlines.
    for(let i=0;i<32;i++) {const mid=(low+high)/2;if(fits(mid))low=mid;else high=mid;}
    scale=low*(1-1e-7);
  }
  const cabin=scale>=.15 && length>0 && width>0?rectangle(scale).map(world):[];
  return {settings,heading,scale,cabin,center:world([cx,cy]),length,width,
    front:world([cx+length*.3,cy]),
    issue: !cabin.length ? "No cabin fits at this position. Check the outline or move the cabin."
      : scale<.98 ? `Cabin reduced to ${Math.round(scale*100)}% to stay inside the outline. Check the outline and cabin position.` : undefined};
}

/** A closed, sloped cabin shell. Its convex projection is fully inside the body. */
export function carSurfaces(ring: ScenePoint[], model: SceneModel) {
  const plan=carPlan(ring,model),profile=CAR_PROFILES[plan.settings.profile];
  const body_height=model.height_m*profile.body;
  const roof:number[]=[],glass:number[]=[];
  if(plan.cabin.length && model.height_m>0) {
    const [a,b,,d]=plan.cabin;
    const point=(u:number,v:number,z:number)=>[a[0]+(b[0]-a[0])*u+(d[0]-a[0])*v,a[1]+(b[1]-a[1])*u+(d[1]-a[1])*v,z];
    const bottom=plan.cabin.map(p=>[...p,model.base_m+body_height]);
    const top=[point(profile.rear,profile.inset,model.base_m+model.height_m),point(1-profile.front,profile.inset,model.base_m+model.height_m),
      point(1-profile.front,1-profile.inset,model.base_m+model.height_m),point(profile.rear,1-profile.inset,model.base_m+model.height_m)];
    roof.push(...top[0],...top[1],...top[2],...top[0],...top[2],...top[3]);
    glass.push(...bottom[2],...bottom[1],...bottom[0],...bottom[3],...bottom[2],...bottom[0]);
    for(let i=0;i<4;i++){const j=(i+1)%4;glass.push(...bottom[i],...bottom[j],...top[j],...bottom[i],...top[j],...top[i]);}
  }
  return {...plan,body_height,roof,glass};
}
