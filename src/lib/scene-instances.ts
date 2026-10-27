import { footprintMetrics, SCENE_REVIEW_ISSUES, type SceneObject, type ScenePoint, type SceneReview, type SceneModelSnapshot } from "./scene-types";
import { isShadeSail } from "./scene-structure";

export const MAX_TRACE_SOURCES = 64;
export type InstanceOperation = "split" | "merge" | "trace";

/** Exclude descendants as well as self so the editor never offers a parent loop. */
export function availableParents(object: SceneObject, objects: SceneObject[]) {
  const excluded = new Set([object.id]);
  for (let changed = true; changed;) {
    changed = false;
    for (const item of objects) if (item.parent_id && excluded.has(item.parent_id) && !excluded.has(item.id)) {
      excluded.add(item.id); changed = true;
    }
  }
  const rooftop = ["solar_panel", "skylight", "chimney"].includes(object.kind);
  return objects.filter(item => !excluded.has(item.id) && !["rejected", "superseded"].includes(item.status) && (!rooftop || (item.kind === "building" && !isShadeSail(item))));
}

export function selectionAfterRestore(id: string|null, previous: SceneObject[], next: SceneObject[]) {
  // An exhausted swipe queue has no selection. Let its existing filters choose
  // the restored object instead of jumping to the first object in another layer.
  if (!id) return null;
  const old=new Map(previous.map(o=>[o.id,o])), restored=new Map(next.map(o=>[o.id,o]));
  const queue=[id],seen=new Set<string>();
  while(queue.length) {
    const current=queue.shift()!;
    if(seen.has(current))continue;
    seen.add(current);
    if(restored.get(current) && restored.get(current)!.status!=="superseded")return current;
    for(const object of [old.get(current),restored.get(current)])
      if(object)queue.push(...(object.derived_from||[]),...(object.replacement_ids||[]));
  }
  return next.find(o=>o.status!=="superseded")?.id || null;
}

export function lineageIssue(objects: SceneObject[]) {
  const all = new Map(objects.map(o=>[o.id,o]));
  for(const object of objects) {
    for(const key of ["derived_from","replacement_ids"] as const) {
      const links=object[key];
      if(links!==undefined && (!Array.isArray(links) || !links.length || links.length>(key==="derived_from"?MAX_TRACE_SOURCES:8) || new Set(links).size!==links.length || links.some(id=>typeof id!=="string" || id===object.id || !all.has(id))))
        return "Replacement history must refer to existing, distinct objects.";
    }
    if((object.status==="superseded")!==Boolean(object.replacement_ids?.length) || (object.status==="superseded" && object.geometry_reviewed))
      return "Replaced outlines must retain their replacements and cannot be approved.";
    if(object.derived_from?.some(id=>!all.get(id)?.replacement_ids?.includes(object.id)) ||
       object.replacement_ids?.some(id=>!all.get(id)?.derived_from?.includes(object.id)))
      return "The replacement history is incomplete.";
  }
  const done=new Set<string>(),visiting=new Set<string>();
  const cycle=(id:string):boolean=>{
    if(visiting.has(id))return true;
    if(done.has(id))return false;
    visiting.add(id);
    if(all.get(id)?.replacement_ids?.some(cycle))return true;
    visiting.delete(id);done.add(id);return false;
  };
  return objects.some(o=>cycle(o.id))?"Replacement history cannot form a loop.":null;
}

export function replaceInstances(review: SceneReview, scene: SceneModelSnapshot, ids: string[], operation: InstanceOperation,
  geometry: {rings: ScenePoint[][];holes?:ScenePoint[][][];children: Record<string,number|null>}, makeId: ()=>string) {
  const sources=ids.map(id=>review.objects.find(o=>o.id===id)!).filter(Boolean), first=sources[0];
  const nextIds=geometry.rings.map(()=>makeId());
  const replacements:SceneObject[]=geometry.rings.map((ring,i)=>{
    const holes=geometry.holes?.[i]||[],metrics=footprintMetrics(ring,scene.pixel_to_meters,holes);
    const height=first.kind==="tree" && first.model.height_source==="class-prior"
      ? Math.round(Math.max(2,Math.min(24,Math.sqrt(metrics.area_m2)*1.15))*10)/10 : first.model.height_m;
    return {...first,id:nextIds[i],ring,holes,label:`${first.label} · ${operation==="split"?`part ${i+1}`:operation==="trace"?"traced":"merged"}`.slice(0,150),
      source:`reviewer-${operation}`,score:undefined,prompt:undefined,address_id:undefined,
      notes:operation==="trace"?`Manually traced replacement for ${sources.length} outline${sources.length===1?"":"s"}. Check the visible boundary, interior gaps and estimated details before confirming.`:first.notes,
      parent_id:sources.every(o=>o.parent_id===first.parent_id)&&!ids.includes(first.parent_id||"")?first.parent_id:undefined,
      group_id:sources.every(o=>o.group_id===first.group_id)?first.group_id:undefined,
      status:"needs_adjustment",geometry_reviewed:false,visibility:undefined,derived_from:ids,replacement_ids:undefined,
      review_issues:SCENE_REVIEW_ISSUES.filter(issue=>sources.some(source=>source.review_issues?.includes(issue))),
      roof_parts:undefined,roof_lines:undefined,roof_annotation:null,roof_type:undefined,roof_geometry_source:undefined,roof_geometry_note:undefined,
      roof_feedback:undefined,roof_line_edits:undefined,
      model:{...first.model,...metrics,car:undefined,surface:undefined,solar:undefined,height_m:height,height_verified:false}};
  });
  const changedKinds=new Set([first.kind]);
  const objects=review.objects.map((o):SceneObject=>{
    if(ids.includes(o.id))return {...o,status:"superseded",geometry_reviewed:false,replacement_ids:nextIds};
    if(Object.hasOwn(geometry.children,o.id)) {
      changedKinds.add(o.kind);
      const index=geometry.children[o.id];
      return {...o,parent_id:index===null?undefined:nextIds[index],status:"needs_adjustment",geometry_reviewed:false,
        model:{...o.model,height_verified:false},
        notes:index===null?`${o.notes||""}\nParent outline was replaced. Choose a parent after checking the new outlines.`.trim().slice(0,5000):o.notes};
    }
    return o;
  });
  return {review:{...review,objects:[...objects,...replacements],reviewed_classes:review.reviewed_classes.filter(k=>!changedKinds.has(k))},selected:nextIds[0]};
}
