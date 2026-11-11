"use client";
import { useEffect, useState } from "react";
import { getCrmToken } from "@/lib/crm-client";
type Status={manifest:null|{version:string;roofs:number;boundary_eligible:number;roof_shape_eligible:number;objects_eligible:number;family_labelled_roofs:number};training:null|{status:string;missing:string[];promotion_eligible?:boolean;excluded_family_labels?:number}};
export default function TrainingStatus({cohort}:{cohort:string}) {
  const [data,setData]=useState<Status|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState("");
  useEffect(()=>{const controller=new AbortController();fetch(`/api/reviews/training?cohort=${encodeURIComponent(cohort)}`,{signal:controller.signal}).then(r=>r.json()).then(setData).catch(()=>{});return()=>controller.abort();},[cohort]);
  const prepare=async()=>{setBusy(true);setError("");try {const token=getCrmToken();const response=await fetch("/api/reviews/training",{method:"POST",headers:{"Content-Type":"application/json",...(token?{Authorization:`Bearer ${token}`}:{})},body:JSON.stringify({cohort})});const next=await response.json();if(!response.ok)throw new Error(next.error);setData(next);}catch(e){setError(e instanceof Error?e.message:"Could not prepare training");}finally{setBusy(false);}};
  return <details className="review-notice"><summary>Training readiness{data?.manifest?` · ${data.manifest.roofs} roofs · ${data.manifest.roof_shape_eligible||0} verified shapes`:""}</summary>
    <p>Predictions stay separate from reviewed labels. Geometry, roof-family feedback, confirmed objects and false positives are exported with imagery and spatial holdout groups.</p>
    {data?.manifest&&<p>Version {data.manifest.version} · Boundaries: {data.manifest.boundary_eligible||0} · Shapes: {data.manifest.roof_shape_eligible||0} · Object reviews: {data.manifest.objects_eligible||0} · Roof-family labels: {data.manifest.family_labelled_roofs||0}</p>}
    {data?.training?.status==="needs-reviewed-labels"?<><p>More reviewed examples are needed before retraining.</p><ul>{data.training.missing.map(item=><li key={item}>{item}</li>)}</ul></>:data?.training&&<p>{data.training.promotion_eligible?"Validation passed; training candidate is ready for model promotion.":"Keep the existing model until validation improves."}</p>}
    {!!data?.training?.excluded_family_labels&&<p>{data.training.excluded_family_labels} family labels are saved but have no comparable end features yet; they are excluded from this classifier.</p>}
    <button className="review-button" disabled={busy||["all_roofs","crm_corrections"].includes(cohort)} onClick={prepare}>{busy?"Preparing…":"Export labels & evaluate retraining"}</button>{error&&<p role="alert">{error}</p>}
  </details>;
}
