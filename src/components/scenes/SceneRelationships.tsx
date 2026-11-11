"use client";
import {useMemo} from "react";
import {availableParents} from "@/lib/scene-instances";
import {outlineSupport, type RoofMount} from "@/lib/scene-roof-mount";
import type {SceneModelSnapshot, SceneObject} from "@/lib/scene-types";

const percentInside = (outside: number, area: number) => area > 0 ? Math.min(100, Math.max(0, 100 * (1 - outside / area))) : 0;
// Do not round a small unsupported sliver into an apparent 100% match.
const coverageLabel = (outside: number, area: number) => outside > Math.max(1e-6, area * 1e-5)
  ? `${Math.min(99.9, percentInside(outside, area)).toFixed(1)}%` : "100%";

export default function SceneRelationships({object,objects,frame,mount,readOnly,busy,onChange,onInspect}:{
  object:SceneObject;objects:SceneObject[];frame:SceneModelSnapshot["pixel_to_meters"];mount:RoofMount|null;
  readOnly:boolean;busy:boolean;onChange:(object:SceneObject)=>void;onInspect:(object:SceneObject)=>void;
}) {
  const rooftop = ["solar_panel","skylight","chimney"].includes(object.kind);
  const parent = objects.find(o => o.id === object.parent_id);
  const options = useMemo(() => availableParents(object,objects).map(candidate => {
    const fit = rooftop ? outlineSupport(object,candidate,frame) : null;
    return {object:candidate,fit,coverage:fit ? percentInside(fit.outside_outline_m2,fit.area_m2) : 0};
  }).sort((a,b) => b.coverage - a.coverage || a.object.label.localeCompare(b.object.label)), [object,objects,frame,rooftop]);
  const attached = objects.filter(o=>o.parent_id===object.id&&!['rejected','superseded'].includes(o.status));
  const support = mount?.support;
  return <section className="scene-relationships" aria-label="Object relationships">
    <fieldset disabled={readOnly||busy}>
      <label>Parent object
        <select value={object.parent_id||""} onChange={e=>onChange({...object,parent_id:e.target.value||undefined})}>
          <option value="">No parent</option>
          {object.parent_id&&!options.some(o=>o.object.id===object.parent_id)&&
            <option value={object.parent_id} disabled>{parent?.label||object.parent_id} · unavailable parent</option>}
          {options.map(({object:option,fit})=><option key={option.id} value={option.id}>
            {option.label} · {option.id.slice(-4)}{fit?` · ${coverageLabel(fit.outside_outline_m2,fit.area_m2)} inside outline`:""}
          </option>)}
        </select>
      </label>
    </fieldset>
    {rooftop&&<p className="review-muted">Buildings are ranked by outline containment. This checks placement, not roof height or ownership.</p>}
    {support&&<dl className="scene-support-metrics">
      <div><dt>Inside building outline</dt><dd>{coverageLabel(support.outside_outline_m2,support.area_m2)}</dd></div>
      <div><dt>Covered by roof faces</dt><dd>{support.uncovered_roof_m2===undefined?"Overlapping faces":coverageLabel(support.uncovered_roof_m2,support.area_m2)}</dd></div>
    </dl>}
    {mount?.issue&&mount.mode==="absolute"&&<p className="scene-solar-warning" role="status">{mount.issue}</p>}
    {parent&&<button type="button" className="review-button" disabled={busy} onClick={()=>onInspect(parent)}>Inspect parent · {parent.label}</button>}
    {!!attached.length&&<details className="scene-attached" open>
      <summary>Attached objects · {attached.length}</summary>
      {attached.map(child=><button key={child.id} type="button" className="review-button" disabled={busy} onClick={()=>onInspect(child)}>{child.label} · {child.id.slice(-4)}</button>)}
    </details>}
  </section>;
}
