"use client";
import MetricInput from "@/components/reviews/MetricInput";
import type { RoofMount } from "@/lib/scene-roof-mount";
import type { SceneModel, SceneObject } from "@/lib/scene-types";

export default function SceneSolarControls({object,mount,onChange}:{object:SceneObject;mount:RoofMount;onChange:(o:SceneObject)=>void}){
  const plane=mount.solar,settings=object.model.solar||{};
  const update=(solar:SceneModel["solar"])=>onChange({...object,model:{...object.model,solar,height_verified:false}});
  const orientation=()=>({tilt_deg:Number((plane?.tilt_deg??0).toFixed(2)),azimuth_deg:Number((plane?.azimuth_deg??0).toFixed(2))%360});
  return <details className="scene-solar-controls" open>
    <summary>Solar mounting plane</summary>
    {!plane?<p className="review-muted">Correct the supporting roof, or turn off Follow parent roof to set an independent mounting plane.</p>:<>
      <p className="review-muted">{plane.automatic?(mount.mode==="absolute"?"Level plane at the entered base elevation.":"Plane estimated from the supporting roof."):"Using your tilt and downhill direction."} The bank stays flat; elevations remain estimates.</p>
      <label>Tilt from horizontal (°)<MetricInput key={`solar-tilt:${object.id}`} aria-label="Solar tilt" min="0" max="80" step=".1"
        value={Number(plane.tilt_deg.toFixed(2))} onValueChange={tilt_deg=>update({...settings,...orientation(),tilt_deg})}/></label>
      <label>Downhill direction (°)<MetricInput key={`solar-bearing:${object.id}`} aria-label="Solar downhill direction" min="0" max="359.99" step=".1"
        value={Number(plane.azimuth_deg.toFixed(2))%360} onValueChange={azimuth_deg=>update({...settings,...orientation(),azimuth_deg})}/></label>
      <p className="review-muted">0° north · 90° east. The aerial arrow points downhill; a level bank has no downhill arrow.</p>
      {mount.mode!=="absolute"?<>
        <label>Minimum roof clearance (m)<MetricInput key={`solar-clearance:${object.id}`} aria-label="Solar roof clearance" min="0" max="3" step=".01"
          value={plane.clearance_m} onValueChange={clearance_m=>update({...settings,clearance_m})}/></label>
        <p className="scene-solar-gap">Estimated gap above roof: {plane.clearance_m.toFixed(2)}–{plane.max_gap_m!.toFixed(2)} m</p>
      </>:<p className="review-muted">Base elevation sets the lowest underside. Roof clearance is retained but only applies when following a parent roof.</p>}
      <div className="scene-solar-actions">
        <button className="review-button" disabled={plane.automatic} onClick={()=>update({clearance_m:settings.clearance_m})}>{mount.mode==="absolute"?"Reset to level":"Fit parent roof plane"}</button>
        <button className="review-button" disabled={plane.tilt_deg<1e-8} onClick={()=>update({...settings,...orientation(),tilt_deg:0})}>Level bank</button>
      </div>
      {mount.issue&&<p className="scene-solar-warning" role="status">{mount.issue}</p>}
    </>}
  </details>;
}
