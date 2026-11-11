"use client";
import { sceneVisibility, VISIBILITY_LABELS } from "@/lib/scene-visibility";
import type { SceneModelSnapshot, SceneObject } from "@/lib/scene-types";
import { isShadeSail } from "@/lib/scene-structure";

export default function SceneVisibilityControls({object,scene,onChange}:{object:SceneObject;scene:SceneModelSnapshot;onChange:(object:SceneObject)=>void}) {
  const visibility=sceneVisibility(object,scene);
  return <details className="scene-visibility-controls" open>
    <summary>Image visibility</summary>
    <label>Visible extent<select aria-label="Object visibility" value={object.visibility||"unassessed"}
      onChange={e=>onChange({...object,visibility:e.target.value==="unassessed"?undefined:e.target.value as SceneObject["visibility"]})}>
      <option value="unassessed">Not assessed</option>
      {Object.entries(VISIBILITY_LABELS).map(([value,label])=><option value={value} key={value}>{label}</option>)}
    </select></label>
    <p className="review-muted">Trace what is visible. Hidden or off-image parts are not part of this outline.</p>
    {!object.visibility&&visibility.touchesEdge&&<p className="review-notice">This outline touches the image edge. Check whether the object is clipped.</p>}
    {visibility.footprintOnly&&(isShadeSail(object)
      ? <p className="review-notice" role="status">Fabric shade sail: the 3D view shows its visible footprint. Membrane curvature and suspension heights are not modelled.</p>
      : <p className="review-notice" role="status">{object.holes?.length?"Interior gaps":"Partial outline"}: the 3D view shows its visible footprint. {object.kind==="tree"&&object.holes?.length&&!object.visibility
      ? "Check whether the whole canopy is visible or parts are hidden. A fully visible canopy can have a 3D crown with open gaps."
      : `A complete ${object.kind==="car"?"vehicle body":"canopy and trunk"} cannot be fitted from this fragment.`} Saved shape settings are retained.</p>)}
    {!visibility.footprintOnly&&object.kind==="tree"&&!!object.holes?.length&&<p className="review-muted">The 3D crown preserves the interior gaps. Height and trunk position remain estimates.</p>}
  </details>;
}
