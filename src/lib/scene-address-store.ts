import { execFile } from "node:child_process";
import path from "node:path";
import type { SceneData } from "./scene-types";
import type { AddressNames, SceneAddressEvidence } from "./scene-addresses";
import { cachedParcels } from "./property-parcel-service";
import { PARCEL_SOURCE, parcelBoundsIssue, sceneParcelBounds } from "./property-parcels";

function run(args: string[], input?: unknown): Promise<string> {
  return new Promise((resolve,reject) => {
    const child=execFile(path.join(process.cwd(),"scripts/ml/.venv/bin/python"),["-m","scripts.ml.scene_addresses",...args],
      {cwd:process.cwd(),timeout:15000,maxBuffer:8*1024*1024},(error,stdout)=>error?reject(error):resolve(stdout));
    child.stdin?.on("error",reject);
    child.stdin?.end(input === undefined ? undefined : JSON.stringify(input));
  });
}
const namesCache=new Map<number,{title:string;expires:number}>();
export async function addressNames(ids: (number|null|undefined)[]): Promise<AddressNames> {
  const unique=[...new Set(ids.filter((id):id is number=>Number.isSafeInteger(id)&&id!>0))];
  const missing=unique.filter(id=>!namesCache.has(id)||namesCache.get(id)!.expires<Date.now());
  if(missing.length) {
    const found=JSON.parse(await run(["--catalog",missing.join(",")])) as AddressNames;
    if(namesCache.size>10000)namesCache.clear();
    for(const [id,title] of Object.entries(found))namesCache.set(Number(id),{title,expires:Date.now()+600000});
  }
  return Object.fromEntries(unique.filter(id=>namesCache.has(id)).map(id=>[id,namesCache.get(id)!.title]));
}
export async function withAddressNames(data: SceneData): Promise<SceneData> {
  const objects=data.stale?data.model.objects:data.review?.objects||data.model.objects;
  return {...data,address_names:await addressNames([data.model.address_id,...objects.map(o=>o.address_id)])};
}
const evidenceCache=new Map<string,{expires:number;value:Promise<SceneAddressEvidence>}>();
export function invalidateAddressEvidence() { evidenceCache.clear(); }
export function sceneAddressEvidence(data: SceneData): Promise<SceneAddressEvidence> {
  const key=[data.model.id,data.model.model_signature,data.review?.revision||0,data.stale].join(":");
  const previous=evidenceCache.get(key);
  if(previous && previous.expires>Date.now())return previous.value;
  const value=(async()=>{
    const candidate=sceneParcelBounds(data.model),bounds=candidate&&!parcelBoundsIssue(candidate)?candidate:null;
    const parcels=bounds?await cachedParcels(bounds).catch(()=>null):null;
    const objects=data.stale?data.model.objects:data.review?.objects||data.model.objects;
    const [local,official]=await Promise.all([run([],{scene:data.model,objects,bounds,parcels}),run(["--properties"],{scene:data.model,objects})]);
    const result=JSON.parse(local) as SceneAddressEvidence["objects"];
    return {model_signature:data.model.model_signature,revision:data.review?.revision||0,objects:result,
      property_addresses:JSON.parse(official) as SceneAddressEvidence["property_addresses"],
      parcel_status:parcels?"available" as const:"unavailable" as const,parcel_source:parcels?PARCEL_SOURCE.url:undefined};
  })();
  if(evidenceCache.size>=32)evidenceCache.delete(evidenceCache.keys().next().value!);
  evidenceCache.set(key,{expires:Date.now()+60000,value});
  value.catch(()=>{if(evidenceCache.get(key)?.value===value)evidenceCache.delete(key);});
  return value;
}
