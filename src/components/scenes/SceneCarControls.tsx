"use client";
import MetricInput from "@/components/reviews/MetricInput";
import { CAR_PROFILES, carPlan } from "@/lib/scene-car";
import { sceneVisibility } from "@/lib/scene-visibility";
import { transformPoint, type SceneObject, type SceneModelSnapshot, type SceneModel } from "@/lib/scene-types";

export default function SceneCarControls({object,scene,onChange}:{object:SceneObject;scene:SceneModelSnapshot;onChange:(o:SceneObject)=>void}) {
  if(sceneVisibility(object,scene).footprintOnly)return null;
  const plan=carPlan(object.ring.map(p=>transformPoint(p,scene.pixel_to_meters)),object.model), settings=plan.settings;
  const update=(car:SceneModel["car"])=>onChange({...object,model:{...object.model,car,height_verified:false}});
  return <details className="scene-car-controls" open>
    <summary>Vehicle shape</summary>
    <label>Body style<select aria-label="Vehicle body style" value={settings.profile} onChange={e=>{
      const profile=e.target.value as keyof typeof CAR_PROFILES;
      update({...settings,profile,cabin_length_ratio:CAR_PROFILES[profile].length,cabin_offset_ratio:CAR_PROFILES[profile].offset});
    }}>{Object.entries(CAR_PROFILES).map(([key,p])=><option key={key} value={key}>{p.label}</option>)}</select></label>
    <label>Front bearing (°)<MetricInput key={`heading:${object.id}`} aria-label="Vehicle front bearing" min="0" max="359.99" step="1"
      value={Number(plan.heading.toFixed(2))} onValueChange={heading_deg=>update({...settings,heading_deg})}/></label>
    <div className="scene-inline-actions">
      <button className="review-button" onClick={()=>update({...settings,heading_deg:Number(((plan.heading+180)%360).toFixed(2))})}>Flip front</button>
      <button className="review-button" disabled={settings.heading_deg===undefined} onClick={()=>update({...settings,heading_deg:undefined})}>Follow outline</button>
    </div>
    <p className="review-muted">0° north · 90° east. The aerial arrow shows the assumed front. Direction needs a check against the imagery.</p>
    {([
      ["cabin_length_ratio","Cabin length (%)",25,85],
      ["cabin_width_ratio","Cabin width (%)",40,95],
      ["cabin_offset_ratio","Cabin position (%)",-30,30],
    ] as const).map(([key,label,min,max])=><label key={key}>{label}<MetricInput key={`${object.id}:${key}`} aria-label={label}
      min={min} max={max} step="1" value={Number((settings[key]*100).toFixed(2))} onValueChange={value=>update({...settings,[key]:value/100})}/></label>)}
    <p className="review-muted">Positive position moves the cabin toward the front. Proportions follow the outline; body style and height remain estimates.</p>
    {plan.issue && <p className="review-notice" role="status">{plan.issue}</p>}
  </details>;
}
