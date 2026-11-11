"use client";
import { useEffect, useState } from "react";
import type { Map as MapLibreMap, GeoJSONSource } from "maplibre-gl";
import { PARCEL_SOURCE, parcelBoundsIssue } from "@/lib/property-parcels";
import { usePropertyParcels } from "./usePropertyParcels";

export default function PropertyMapLayer({map}:{map:MapLibreMap}) {
  const [enabled,setEnabled]=useState(true);
  const [area,setArea]=useState<{query:string|null;issue:string}>({query:null,issue:"Loading property boundaries…"});
  const result=usePropertyParcels(enabled?area.query:null);
  useEffect(()=>{
    let timer:ReturnType<typeof setTimeout>;
    const update=()=>{
      clearTimeout(timer);
      timer=setTimeout(()=>{
        if (map.getZoom()<17) {
          setArea({query:null,issue:"Zoom in to see property boundaries."});
          return;
        }
        const b=map.getBounds(), bounds=[b.getWest(),b.getSouth(),b.getEast(),b.getNorth()];
        // Wide/pitched displays can contain thousands of lots even at zoom 17.
        const issue=parcelBoundsIssue(bounds,700);
        setArea({query:issue?null:bounds.join(","),issue:issue||""});
      },200);
    };
    update();map.on("moveend",update);
    return ()=>{clearTimeout(timer);map.off("moveend",update);};
  },[map]);
  useEffect(()=>{
    let applied=false;
    const install=()=>{
      if (!map.isStyleLoaded()) return;
      if (!map.getSource("vicmap-parcels")) {
        map.addSource("vicmap-parcels",{type:"geojson",data:{type:"FeatureCollection",features:[]},attribution:`${PARCEL_SOURCE.attribution} · <a href="${PARCEL_SOURCE.url}">Vicmap Parcel</a> · CC BY 4.0`});
        map.addLayer({id:"vicmap-parcel-lines",source:"vicmap-parcels",type:"line",paint:{"line-color":"#fbbf24","line-width":1.8,"line-dasharray":[4,2]}});
        map.addLayer({id:"vicmap-parcel-labels",source:"vicmap-parcels",type:"symbol",minzoom:18,layout:{"text-field":["get","spi"],"text-size":10},paint:{"text-color":"#ffdf87","text-halo-color":"#172033","text-halo-width":1.5}});
      }
      (map.getSource("vicmap-parcels") as GeoJSONSource).setData(enabled&&result.data ? result.data : {type:"FeatureCollection",features:[]});
      applied=true;
    };
    const ready=()=>{if(!applied)install();};
    install();map.on("style.load",install);map.on("idle",ready);
    return ()=>{map.off("style.load",install);map.off("idle",ready);};
  },[map,enabled,result.data]);
  return <div className="border-t border-slate-200 pt-2">
    <label className="flex items-center gap-2"><input type="checkbox" checked={enabled} onChange={e=>setEnabled(e.target.checked)} />Property boundaries <span className="text-amber-700">┄</span></label>
    {enabled&&<>
      <p className="mt-1 text-[11px] text-slate-600" role="status">{area.issue || (result.pending?"Loading Vicmap parcels…":result.error || `${result.data?.features.length ?? 0} approved surface parcels · land boundaries`)} {result.error&&<button className="underline" onClick={result.retry}>Retry boundaries</button>}</p>
      <p className="mt-1 text-[10px] text-slate-500"><a href={PARCEL_SOURCE.url} target="_blank" rel="noreferrer" className="underline">Vicmap Parcel</a> · State of Victoria · <a href={PARCEL_SOURCE.license_url} target="_blank" rel="noreferrer" className="underline">CC BY 4.0</a>. Shared lots may contain several addresses.</p>
    </>}
  </div>;
}
