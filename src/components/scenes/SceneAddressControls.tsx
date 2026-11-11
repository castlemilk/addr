"use client";
import { useEffect, useState } from "react";
import type { SceneObject } from "@/lib/scene-types";
import type { AddressCandidate, SceneAddressEvidence } from "@/lib/scene-addresses";
import BuildingAddressSummary from "./BuildingAddressSummary";
import { buildingAddressTitle } from "@/lib/building-addresses";

export default function SceneAddressControls({sceneId,signature,revision,object,title,disabled,onChoose}:{
  sceneId:string;signature:string;revision:number;object:SceneObject;title?:string;disabled:boolean;onChoose:(address:AddressCandidate)=>void;
}) {
  const [evidence,setEvidence]=useState<SceneAddressEvidence|null>(null),[error,setError]=useState("");
  useEffect(()=>{
    if(object.address_id)return;
    const controller=new AbortController();
    fetch(`/api/scenes/addresses?id=${encodeURIComponent(sceneId)}`,{signal:controller.signal}).then(async response=>{
      const result=await response.json();
      if(!response.ok)throw new Error(result.error);
      if(result.model_signature!==signature || result.revision!==revision)throw new Error("The scene changed. Reload before checking addresses.");
      if(!controller.signal.aborted)setEvidence(result);
    }).catch(error=>{if(!controller.signal.aborted)setError(error.message);});
    return ()=>controller.abort();
  },[sceneId,signature,revision,object.address_id]);
  const currentEvidence=evidence?.model_signature===signature&&evidence.revision===revision?evidence:null;
  const match=currentEvidence?.objects.find(m=>m.object_id===object.id);
  const property=currentEvidence?.property_addresses?.[object.id];
  return <section className="scene-address-control" aria-label="Roof address">
    <h3>Property address</h3>
    {object.address_id ? <><p>{title || object.label}</p><small>Saved link to this roof · address #{object.address_id}</small></> : <>
      <p>{property?.status!=="unmatched"&&property?buildingAddressTitle(property):match?.full_address || "Address not linked"}</p>
      {property && <BuildingAddressSummary key={`${sceneId}/${object.id}`} address={property} sceneId={sceneId} objectId={object.id} disabled={disabled}/>}
      <small>{error || (!property ? match?.reason || "Checking address points and property boundaries…" : null)}</small>
      {!property&&evidence?.parcel_status==="unavailable"&&<p className="scene-address-note">Parcel reference unavailable. Address-point evidence only.</p>}
      {match?.candidates.map(address=><button key={address.address_id} className="review-button" disabled={disabled} onClick={()=>onChoose(address)}>
        {match.status==="suggested"?"Use":"Link"} {address.full_address.split(",")[0]}
      </button>)}
      {!!match?.candidates.length&&<small>{disabled?"Save or discard outline changes before linking an address.":"Check the property, then save the scene to keep your address link."}</small>}
      {evidence?.parcel_source&&<a href={evidence.parcel_source} target="_blank" rel="noreferrer">Vicmap parcel evidence</a>}
    </>}
  </section>;
}
