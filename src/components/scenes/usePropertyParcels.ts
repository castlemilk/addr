"use client";
import { useEffect, useState } from "react";
import { type ParcelData } from "@/lib/property-parcels";

/** Vicmap can stall; after this the status line says so instead of staying "Loading Vicmap…" (retry is unchanged). */
export const PARCEL_TIMEOUT_MS = 25000;
export const PARCEL_TIMEOUT_MESSAGE = "Vicmap is slow; retry.";

export function usePropertyParcels(query: string | null) {
  const [result,setResult]=useState<{query:string;data:ParcelData|null;error:string}|null>(null);
  const [retry,setRetry]=useState(0);
  useEffect(()=>{
    if (!query) return;
    const controller=new AbortController();
    const signal=AbortSignal.any([controller.signal,AbortSignal.timeout(PARCEL_TIMEOUT_MS)]);
    void fetch(`/api/parcels?bounds=${encodeURIComponent(query)}`,{signal}).then(async response=>{
      const value=await response.json();
      if (!response.ok) throw new Error(value.error || "Property boundaries unavailable.");
      if (!controller.signal.aborted) setResult({query,data:value,error:""});
    }).catch(error=>{
      if(controller.signal.aborted)return;
      const timedOut=error instanceof Error&&error.name==="TimeoutError";
      setResult({query,data:null,error:timedOut?PARCEL_TIMEOUT_MESSAGE:(error instanceof Error&&error.message)||"Property boundaries unavailable."});
    });
    return ()=>controller.abort();
  },[query,retry]);
  const current=query && result?.query===query ? result : null;
  return {data:current?.data ?? null,error:current?.error || "",pending:!!query&&!current,
    retry:()=>{setResult(null);setRetry(n=>n+1);}};
}
