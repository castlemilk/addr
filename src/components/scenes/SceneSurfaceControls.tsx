"use client";
import MetricInput from "@/components/reviews/MetricInput";
import { pavedSurface } from "@/lib/scene-surface";
import { transformPoint, type SceneModel, type SceneModelSnapshot, type SceneObject } from "@/lib/scene-types";

export default function SceneSurfaceControls({object,scene,onChange}:{object:SceneObject;scene:SceneModelSnapshot;onChange:(o:SceneObject)=>void}) {
  const surface=pavedSurface(object.ring.map(p=>transformPoint(p,scene.pixel_to_meters)),object.model),settings=surface.settings;
  const update=(value:SceneModel["surface"])=>onChange({...object,model:{...object.model,surface:value,height_verified:false}});
  return <details className="scene-surface-controls" open>
    <summary>Surface slope</summary>
    <label>Across the surface<select aria-label="Surface cross-section" value={settings.profile} onChange={e=>{
      const profile=e.target.value as "plane"|"crowned";
      update({...settings,profile,crossfall_percent:profile==="crowned"?Math.abs(settings.crossfall_percent):settings.crossfall_percent});
    }}><option value="plane">Single slope</option><option value="crowned">Raised centre</option></select></label>
    <label>Direction (°)<MetricInput key={`surface-heading:${object.id}`} aria-label="Surface direction" min="0" max="359.99" step=".1"
      value={Number(surface.heading.toFixed(2))} onValueChange={heading_deg=>update({...settings,heading_deg})}/></label>
    <button className="review-button" disabled={settings.heading_deg===undefined} onClick={()=>update({...settings,heading_deg:undefined})}>Follow outline direction</button>
    <p className="review-muted">0° north · 90° east. Positive grade rises along the aerial arrow.</p>
    <label>Grade along surface (%)<MetricInput key={`surface-grade:${object.id}`} aria-label="Surface grade (%)" min="-30" max="30" step=".1"
      value={settings.grade_percent} onValueChange={grade_percent=>update({...settings,grade_percent})}/></label>
    <label>{settings.profile==="crowned"?"Crossfall from centre (%)":"Cross slope (%)"}<MetricInput key={`surface-crossfall:${object.id}`} aria-label="Surface crossfall (%)"
      min={settings.profile==="crowned"?0:-15} max="15" step=".1" value={settings.crossfall_percent}
      onValueChange={crossfall_percent=>update({...settings,crossfall_percent})}/></label>
    <p className="review-muted">{settings.profile==="crowned"?"Crossfall lowers both sides from the straight centre axis. Zero keeps the cross-section level.":"Positive cross slope raises the left side when looking along the arrow."}</p>
    <p className="scene-surface-range">Surface elevation: {surface.min_elevation.toFixed(2)}–{surface.max_elevation.toFixed(2)} m</p>
    <p className="review-muted">Base elevation is the lowest underside; thickness stays constant. These are local estimates. Check neighbouring objects separately.</p>
    <button className="review-button" disabled={!settings.grade_percent&&!settings.crossfall_percent} onClick={()=>update({...settings,profile:"plane",grade_percent:0,crossfall_percent:0})}>Level surface</button>
  </details>;
}
