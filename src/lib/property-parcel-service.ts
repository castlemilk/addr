import { PARCEL_SOURCE, parcelBoundsIssue, parcelPolygons, type ParcelBounds, type ParcelData, type ParcelFeature } from "./property-parcels";

const cache = new Map<string, { expires: number; value: Promise<ParcelData> }>();
const FIELDS = "OBJECTID,parcel_pfi,parcel_spi,parcel_lot_number,parcel_plan_number,parcel_status,parv_status,parv_z_level,parv_horiz_pos_uncertainty,parv_ufi_created";

function normalize(value: unknown): ParcelFeature {
  const f = value as ParcelFeature & { properties: Record<string, unknown> };
  if (!f || f.type !== "Feature" || !f.geometry || !["Polygon","MultiPolygon"].includes(f.geometry.type) || !f.properties) throw new Error("Invalid parcel response");
  const polygons=parcelPolygons(f.geometry), p=f.properties;
  if (!polygons.length || polygons.some(poly => !Array.isArray(poly) || !poly.length || poly.some(r => !Array.isArray(r) || r.length<4 || r.some(v => !Array.isArray(v) || v.length<2 || !Number.isFinite(v[0]) || !Number.isFinite(v[1]) || Math.abs(v[0])>180 || Math.abs(v[1])>85) || r[0][0]!==r.at(-1)![0] || r[0][1]!==r.at(-1)![1]))) throw new Error("Invalid parcel coordinates");
  if (!p.parcel_pfi || p.parcel_status!=="A" || p.parv_status!=="A" || !["G","S"].includes(String(p.parv_z_level))) throw new Error("Unexpected parcel status");
  const uncertainty = p.parv_horiz_pos_uncertainty, created = p.parv_ufi_created;
  return {type:"Feature",id:String(f.id ?? p.OBJECTID),geometry:f.geometry,properties:{
    pfi:String(p.parcel_pfi),spi:String(p.parcel_spi || ""),lot:String(p.parcel_lot_number || ""),plan:String(p.parcel_plan_number || ""),status:"approved",
    horizontal_uncertainty_m:typeof uncertainty==="number" && Number.isFinite(uncertainty) && uncertainty>=0 ? uncertainty:null,
    geometry_record_created:typeof created==="number" && Number.isFinite(created) && created>0 && created<Date.now() ? new Date(created).toISOString():null,
  }};
}

/** Read-only public reference data. No review or training store is touched. */
export async function fetchParcels(bounds: ParcelBounds, fetcher: typeof fetch = fetch): Promise<ParcelData> {
  const issue=parcelBoundsIssue(bounds);
  if (issue) throw new Error(issue);
  const features: ParcelFeature[]=[];
  const ids=new Set<string>();
  // A bounded complete result, never a silent truncated subset or fabricated fallback.
  for (let offset=0;offset<2000;offset+=500) {
    const params=new URLSearchParams({f:"geojson",geometry:bounds.join(","),geometryType:"esriGeometryEnvelope",inSR:"4326",outSR:"4326",spatialRel:"esriSpatialRelIntersects",
      where:"parcel_status='A' AND parv_status='A' AND parv_z_level IN ('G','S')",outFields:FIELDS,returnGeometry:"true",orderByFields:"OBJECTID",resultOffset:String(offset),resultRecordCount:"500"});
    const response=await fetcher(`${PARCEL_SOURCE.service}/query?${params}`,{signal:AbortSignal.timeout(20000),cache:"no-store"});
    if (!response.ok) throw new Error("Vicmap service unavailable");
    const value=await response.json();
    if (value.error || value.type!=="FeatureCollection" || !Array.isArray(value.features)) throw new Error("Invalid Vicmap response");
    for (const raw of value.features) {
      const f=normalize(raw), id=String(f.id);
      if (ids.has(id)) throw new Error("Vicmap pagination repeated a record");
      ids.add(id);features.push(f);
    }
    if (!value.exceededTransferLimit && !value.properties?.exceededTransferLimit && value.features.length<500) return {type:"FeatureCollection",features,source:PARCEL_SOURCE,retrieved_at:new Date().toISOString(),bounds};
    if (!value.features.length) throw new Error("Incomplete Vicmap response");
  }
  throw new Error("Too many parcels. Zoom in further.");
}

export function cachedParcels(bounds: ParcelBounds): Promise<ParcelData> {
  const key=bounds.join(","), previous=cache.get(key);
  if (previous && previous.expires>Date.now()) return previous.value;
  const value=fetchParcels(bounds);
  if (cache.size>=64) cache.delete(cache.keys().next().value!);
  cache.set(key,{value,expires:Date.now()+6*60*60*1000});
  value.catch(()=>{if(cache.get(key)?.value===value)cache.delete(key);});
  return value;
}
