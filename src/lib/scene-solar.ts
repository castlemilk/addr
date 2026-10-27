import { pavedSurface } from "./scene-surface";
import type { SceneModel, ScenePoint } from "./scene-types";

export type SolarSupportTriangle = [[number,number,number],[number,number,number],[number,number,number]];

/** Fit one mounting plane using area-weighted roof evidence, never vertex density. */
export function solarPlane(ring:ScenePoint[],model:SceneModel,support:SolarSupportTriangle[]=[],holes:ScenePoint[][]=[]){
  const cx=(Math.min(...ring.map(p=>p[0]))+Math.max(...ring.map(p=>p[0])))/2;
  const cy=(Math.min(...ring.map(p=>p[1]))+Math.max(...ring.map(p=>p[1])))/2;
  const samples:{x:number;y:number;z:number;weight:number}[]=[];
  // Three-point quadrature integrates every quadratic term of the least-squares
  // normal equations exactly over each projected triangle.
  for(const triangle of support){
    const [a,b,c]=triangle,area=Math.abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2;
    if(area<1e-10)continue;
    for(let i=0;i<3;i++){
      const point=triangle.reduce<number[]>((sum,p,j)=>sum.map((v,k)=>v+p[k]*(i===j?2/3:1/6)),[0,0,0]);
      samples.push({x:point[0]-cx,y:point[1]-cy,z:point[2],weight:area/3});
    }
  }
  const weight=samples.reduce((v,p)=>v+p.weight,0);
  const mean=(key:"x"|"y"|"z")=>weight?samples.reduce((v,p)=>v+p[key]*p.weight,0)/weight:0;
  const mx=mean("x"),my=mean("y"),mz=mean("z");
  let xx=0,xy=0,yy=0,xz=0,yz=0;
  for(const p of samples){const x=p.x-mx,y=p.y-my,z=p.z-mz;xx+=p.weight*x*x;xy+=p.weight*x*y;yy+=p.weight*y*y;xz+=p.weight*x*z;yz+=p.weight*y*z;}
  const determinant=xx*yy-xy*xy,stable=determinant>Math.max(1e-16,xx*yy*1e-12);
  let a=stable?(xz*yy-yz*xy)/determinant:0,b=stable?(yz*xx-xz*xy)/determinant:0;
  const automatic=model.solar?.tilt_deg===undefined;
  if(!automatic){
    const tilt=model.solar!.tilt_deg!*Math.PI/180,azimuth=(model.solar!.azimuth_deg??0)*Math.PI/180;
    a=-Math.tan(tilt)*Math.sin(azimuth);b=-Math.tan(tilt)*Math.cos(azimuth);
  }
  const tilt=Math.atan(Math.hypot(a,b))*180/Math.PI;
  const azimuth=tilt<1e-8?0:(Math.atan2(-a,-b)*180/Math.PI+360)%360;
  const height=(x:number,y:number)=>a*(x-cx)+b*(y-cy);
  const planeZ=mz-a*mx-b*my;
  const residuals=support.flatMap(triangle=>triangle.map(p=>p[2]-height(p[0],p[1])-planeZ));
  const clearance=model.solar?.clearance_m??.05;
  const high=residuals.length?Math.max(...residuals):0,low=residuals.length?Math.min(...residuals):0;
  // Linear differences reach their extrema at the clipped support vertices.
  // Lift the plane above every supported roof face, not just its average.
  const bottom=ring.map(([x,y])=>height(x,y));
  const base=support.length?planeZ+high+clearance+Math.min(...bottom):model.base_m;
  const thickness=Math.max(0,model.height_m);
  // Reuse the conforming constant-thickness mesher, including concave outlines.
  const mesh=pavedSurface(ring,{...model,base_m:base,height_m:thickness,
    surface:{profile:"plane",heading_deg:0,grade_percent:b*100,crossfall_percent:-a*100}},holes);
  const length=Math.max(...ring.map(([x,y])=>Math.hypot(x-cx,y-cy)))*.55;
  const bearing=azimuth*Math.PI/180;
  return {positions:mesh.positions,top_positions:mesh.top_positions,min:mesh.min_elevation-thickness,max:mesh.max_elevation-thickness,
    tilt_deg:tilt,azimuth_deg:automatic?azimuth:model.solar!.azimuth_deg??0,automatic,clearance_m:clearance,
    roof_variation_m:high-low,max_gap_m:support.length?clearance+high-low:null,
    center:[cx,cy] as ScenePoint,front:[cx+Math.sin(bearing)*length,cy+Math.cos(bearing)*length] as ScenePoint,heading:azimuth};
}
export type SolarPlane = ReturnType<typeof solarPlane>;
