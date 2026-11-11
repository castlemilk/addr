"use client";
import { useEffect, useState } from "react";
import { ChevronRight, RotateCcw } from "lucide-react";
import { nextReviewImages, sceneQueueFilter, sceneTitle, type SceneSummary } from "@/lib/scene-queue";

export default function SceneQueueNavigator({currentId,kind,status,query,issue,revision,modelSignature,disabledReason,onNavigate}:{
  currentId:string;kind:string;status:string;query:string;issue:string;revision:number;modelSignature:string;
  disabledReason:string|null;onNavigate:(sceneId:string,objectId:string|null)=>void;
}) {
  const [refresh,setRefresh]=useState(0),[result,setResult]=useState<{key:string;scenes:SceneSummary[]}|null>(null),
    [failure,setFailure]=useState<{key:string;message:string}|null>(null);
  const filter=sceneQueueFilter({kind,status,query,issue});
  const params=new URLSearchParams({kind:filter.kind,status:filter.status,q:filter.query,issue:filter.issue}).toString();
  const key=JSON.stringify([currentId,params,revision,modelSignature,refresh]);
  useEffect(()=>{
    const controller=new AbortController();
    const timer=setTimeout(()=>{
      void fetch(`/api/scenes?${params}`,{signal:controller.signal}).then(async response=>{
        const value=await response.json();if(!response.ok)throw new Error(value.error||"Could not load the image queue.");
        if(!Array.isArray(value.scenes))throw new Error("Could not read the image queue. Try refreshing it.");
        if(!controller.signal.aborted)setResult({key,scenes:value.scenes});
      }).catch(error=>{if(!controller.signal.aborted)setFailure({key,message:error instanceof Error?error.message:"Could not load the image queue."});});
    },200);
    return ()=>{clearTimeout(timer);controller.abort();};
  },[key,params]);
  const loading=result?.key!==key&&failure?.key!==key,error=failure?.key===key?failure.message:null;
  const others=result?.key===key?nextReviewImages(result.scenes,currentId):[],next=others[0];
  const count=others.reduce((total,scene)=>total+scene.queue_count,0);
  return <section className="scene-image-queue" aria-label="Review across images">
    <div>
      <strong>{loading?"Finding matching images…":error?"Image queue unavailable":next?
        `${count} matching object${count===1?"":"s"} across ${others.length} other image${others.length===1?"":"s"}`:"No other images match these filters"}</strong>
      {error?<p role="alert">{error}</p>:next?<p>Next: {sceneTitle(next)}{next.stale?" · updated estimates need review":""}</p>:
        !loading&&<p>Change the class, status, feedback or search to review another set.</p>}
      {disabledReason&&next&&<p>{disabledReason}</p>}
    </div>
    <div className="scene-image-queue-actions">
      <button className="review-button" aria-label={error?"Retry image queue":"Refresh image queue"} disabled={loading} onClick={()=>setRefresh(v=>v+1)}><RotateCcw size={14}/></button>
      <button className="review-button" disabled={loading||!!error||!next||!!disabledReason} onClick={()=>onNavigate(next.id,next.next_object?.id||null)}>
        Next image <ChevronRight size={15}/>
      </button>
    </div>
  </section>;
}
