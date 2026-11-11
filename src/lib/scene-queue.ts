import { SCENE_CLASSES, SCENE_REVIEW_ISSUES, type SceneKind, type SceneModelSnapshot, type SceneObject, type SceneReviewIssue } from "./scene-types";
import { hasOutlineIssue } from "./scene-review-issues";
import { propertyTitle, sceneProperties, type AddressNames } from "./scene-addresses";
import { OUTLINE_DESIGN, touchesFrame } from "./outline-hints";

export const REVIEW_STATUS_LABELS = {
  candidate: "Unreviewed", needs_adjustment: "Needs edit", confirmed: "Confirmed",
  rejected: "Rejected", superseded: "Replaced",
} as const;
export type SceneQueueFilter = {kind: SceneKind|"all";status: SceneObject["status"]|"all";query:string;issue:SceneReviewIssue|"all"|"any"};
export type SceneProgress = Record<SceneObject["status"],number> & {total:number;active:number;resolved:number};

export function sceneQueueFilter(value:{kind?:string|null;status?:string|null;query?:string|null;issue?:string|null}):SceneQueueFilter {
  return {kind:SCENE_CLASSES.includes(value.kind as SceneKind)?value.kind as SceneKind:"all",
    status:value.status==="all"||Object.hasOwn(REVIEW_STATUS_LABELS,value.status||"")?value.status as SceneQueueFilter["status"]:"candidate",
    query:(value.query||"").trim().toLowerCase().slice(0,200),
    issue:value.issue==="any"||SCENE_REVIEW_ISSUES.includes(value.issue as SceneReviewIssue)?value.issue as SceneQueueFilter["issue"]:"all"};
}

const reviewStatus=(object:SceneObject)=>object.status==="confirmed"&&(!object.geometry_reviewed||hasOutlineIssue(object))?"needs_adjustment":object.status;

export function matchesSceneQueue(object:SceneObject, filter:SceneQueueFilter) {
  const status=reviewStatus(object);
  return (filter.kind==="all"||object.kind===filter.kind) &&
    (status!=="superseded"||filter.status==="superseded") &&
    (filter.status==="all"||status===filter.status) &&
    (filter.issue==="all"||(filter.issue==="any"?!!object.review_issues?.length:object.review_issues?.includes(filter.issue))) &&
    `${object.label} ${object.kind} ${object.id}`.toLowerCase().includes(filter.query);
}

export function sceneProgress(objects:SceneObject[]):SceneProgress {
  const counts:SceneProgress={candidate:0,needs_adjustment:0,confirmed:0,rejected:0,superseded:0,total:0,active:0,resolved:0};
  for(const object of objects)counts[reviewStatus(object)]++;
  counts.active=counts.candidate+counts.needs_adjustment+counts.confirmed;
  counts.total=counts.active+counts.rejected;
  counts.resolved=counts.confirmed+counts.rejected;
  return counts;
}

export function sceneSummary(model:Pick<SceneModelSnapshot,"id"|"title"|"address_id">,objects:SceneObject[],filter=sceneQueueFilter({}),stale=false,names:AddressNames={}) {
  const progress=sceneProgress(objects),queue=objects.filter(o=>matchesSceneQueue(o,filter));
  const properties=sceneProperties(objects,names);
  return {id:model.id,title:model.title,address_id:model.address_id,objects:progress.total,reviewed:progress.resolved,
    properties,property_title:propertyTitle(properties),
    progress,stale,queue_count:queue.length,next_object:queue[0]?{id:queue[0].id,label:queue[0].label}:null};
}
export type SceneSummary = ReturnType<typeof sceneSummary>;
export const sceneTitle=(scene:Pick<SceneSummary,"title"|"address_id"> & {property_title?:string})=>scene.property_title || (scene.address_id?scene.title.split(",")[0]:scene.title);

export function nextReviewImages(scenes:SceneSummary[],currentId:string) {
  const index=scenes.findIndex(scene=>scene.id===currentId);
  const ordered=index<0?scenes:[...scenes.slice(index+1),...scenes.slice(0,index)];
  return ordered.filter(scene=>scene.id!==currentId&&scene.queue_count>0);
}

/** The editor's client-side frame filter. Frame-cut objects (a vertex within OUTLINE_DESIGN.frame_cut.edge_px of an image edge, or beyond
 *  it: touchesFrame) leave the queue and the advance order under "skip" (the default), are the whole queue under "only" and are left alone
 *  under "all". Not part of SceneQueueFilter: the server's scene summaries (matchesSceneQueue) never see it. */
export type FrameFilter="skip"|"all"|"only";
export const frameFilterValue=(value:string|null|undefined):FrameFilter=>value==="all"||value==="only"?value:"skip";
export function frameFilterAllows(object:Pick<SceneObject,"ring">,model:Partial<Pick<SceneModelSnapshot,"image_width"|"image_height">>|null|undefined,frame:FrameFilter) {
  if(frame==="all")return true;
  const cut=!!model&&touchesFrame(object.ring,model.image_width,model.image_height,OUTLINE_DESIGN.frame_cut.edge_px);
  return (frame==="only")===cut;
}

/** The editor's client-side lot scope. An object's dominant lot (lot-tags-v1, served by GET /api/scenes/lots) is compared with a
 *  lot ANCHOR (LotAnchor): "land" (the default) keeps every lot of an anchor plan, common property included; "lot" keeps the anchor
 *  lots only; "all" keeps everything. The scope is a hint, never a label: without a resolved anchor it is inert (nothing is hidden
 *  silently), and an object without a row or without a dominant lot stays visible. Not part of SceneQueueFilter: the server's
 *  scene summaries (matchesSceneQueue) never see it. */
export type LotScope="land"|"lot"|"all";
export const lotScopeValue=(value:string|null|undefined):LotScope=>value==="lot"||value==="all"?value:"land";
/** One lot the scope compares against: a plan and the lot within it (null when only the plan is known; "" is what the index records
 *  for a common-property parcel with no lot number, CP153157 on three windows, and compares as its own lot). */
export type AddressLot={lot:string|number|null;plan:string};
/** The lot anchor and where it came from. "address": the lots route's "_scene" address (the scene's own lot and plan). "planned":
 *  the planned window lot (window_frame.lots[0], on subject windows whose scene row has no plan). "asked": the land of the roof a
 *  window was cut for (DESIGN 2026-10-03, "the address's land" for a window with no address): the dominant lot of every asked
 *  object, a whole-roof takeover or a carried slice (buildings only: isAskedRoof), so a multi-plan window keeps every asked plan. Read in that order; the first
 *  source that names a plan wins. */
export type LotAnchorSource="address"|"planned"|"asked";
export type LotAnchor={source:LotAnchorSource;lots:AddressLot[]};
/** The part of a lot tag the scope reads. */
export type ObjectLotScope={dominant:{lot:string|number|null;plan:string|null}|null};
/** The fields the asked-roof fallback reads: the kind, the status and the lineage. `carried_from` is the review layer's carry record
 *  (scene-frame-transform CarriedFrom, not on SceneObject); only its presence is read here. */
export type AskedLineage=Pick<SceneObject,"id"|"kind"|"status">&{replaces?:unknown;carried_from?:unknown};
/** Whether an object is one the window was cut for: a building, not superseded, and either a whole-roof detection that took over
 *  slices (`replaces`, non-empty) or a slice carried into the window (`carried_from`, a non-empty record). The kind is required on
 *  BOTH branches because lineage alone does not name a roof: a linear-merge feature (scene_linear_promote, DESIGN amendment 6
 *  'linear_merge') also carries a non-empty `replaces`, and without the kind the merged driveway 9de493cd7792d847 on
 *  win-ab373041656385bf joined the window's lot anchor beside the whole roof's lot (Lot 75 LP62268 beside Lot 76 LP62268). The DESIGN
 *  lot_scope text reads "whole-roof detections with a non-empty replaces list and carried slices with a non-empty carried_from",
 *  i.e. roofs; a carried non-building slice is likewise not asked. A reading of kind and lineage, never a label. */
export function isAskedRoof(object:AskedLineage) {
  if(object.kind!=="building"||object.status==="superseded")return false;
  if(Array.isArray(object.replaces)&&object.replaces.length>0)return true;
  const carried=object.carried_from;
  return !!carried&&typeof carried==="object"&&Object.keys(carried).length>0;
}
/** The anchor the scope compares against. (1) The route's "_scene" address when it names a plan. (2) Else the planned window lot
 *  (window_frame.lots[0]) when it names a plan. (3) Else, on a window scene only (model.window_frame present, as export_dataset's
 *  window_scene() reads it): the dominant lots of the asked objects (isAskedRoof) that have a lot row naming a plan, deduplicated by
 *  plan and lot (lot compared as text; null and "" are different lots), in scene order. (4) Else null: the scope is inert. A
 *  non-window scene whose objects carry `replaces` from the September widening never resolves an asked anchor. */
export function resolveLotAnchor(routeAddress:{lot?:string|number|null;plan?:string|null}|null|undefined,model:Partial<Pick<SceneModelSnapshot,"window_frame">>|null|undefined,
  objects:readonly AskedLineage[]|null|undefined,lotRows:Readonly<Record<string,ObjectLotScope|null|undefined>>|null|undefined):LotAnchor|null {
  if(routeAddress?.plan)return {source:"address",lots:[{lot:routeAddress.lot??null,plan:routeAddress.plan}]};
  const planned=model?.window_frame?.lots?.[0];
  if(planned?.plan)return {source:"planned",lots:[{lot:planned.lot??null,plan:planned.plan}]};
  if(!model?.window_frame)return null;
  const lots:AddressLot[]=[],seen=new Set<string>();
  for(const object of objects??[]){
    if(!isAskedRoof(object))continue;
    const dominant=lotRows?.[object.id]?.dominant;
    if(!dominant?.plan)continue;
    const lot=dominant.lot??null,key=JSON.stringify([dominant.plan,lot===null?null:String(lot)]);
    if(seen.has(key))continue;
    seen.add(key);lots.push({lot,plan:dominant.plan});
  }
  return lots.length?{source:"asked",lots}:null;
}
/** The address or planned lot alone, never the asked roof: what the editor compared against before the anchor. Kept for callers that
 *  want one lot; delegates to resolveLotAnchor with no objects, so the asked fallback cannot fire. */
export function resolveAddressLot(routeAddress:{lot?:string|number|null;plan?:string|null}|null|undefined,model:Partial<Pick<SceneModelSnapshot,"window_frame">>|null|undefined):AddressLot|null {
  const anchor=resolveLotAnchor(routeAddress,model,null,null);
  return anchor&&anchor.source!=="asked"?anchor.lots[0]:null;
}
/** Whether a lot value names a lot: null, undefined and "" (a parcel with no lot number) do not. For the words only: sameLot still
 *  matches "" with "", so "lot only" keeps an asked roof on such a parcel. */
export const hasLotNumber=(lot:string|number|null|undefined)=>lot!==null&&lot!==undefined&&String(lot)!=="";
/** One lot as the editor names it: "Lot 3 PS631347" (a "CM" lot as the data names it, "Lot CM SP25379"), or "Plan PS631347" when
 *  only the plan is known or the lot number is blank ("Plan CP153157", never "Lot  CP153157"). */
export const lotTitle=(lot:{lot:string|number|null|undefined;plan:string})=>hasLotNumber(lot.lot)?`Lot ${lot.lot} ${lot.plan}`:`Plan ${lot.plan}`;
/** The anchor as the lot note names it: the address or planned lot alone ("Lot 3 PS631347"); an asked anchor behind "Asked roof:"
 *  (one lot) or "Asked roofs:" (several, joined with ", " and a final " and "). */
export function lotAnchorTitle(anchor:LotAnchor) {
  const names=anchor.lots.map(lotTitle);
  const joined=names.length<=1?names.join(""):`${names.slice(0,-1).join(", ")} and ${names[names.length-1]}`;
  return anchor.source==="asked"?`${names.length>1?"Asked roofs":"Asked roof"}: ${joined}`:joined;
}
const sameLot=(a:string|number|null,b:string|number|null)=>a!==null&&b!==null&&String(a)===String(b);
/** Whether the scope keeps an object. "all", no anchor or an anchor without lots keeps everything; so does a missing row or a
 *  missing dominant lot. Under "land" the dominant lot's plan must be an anchor plan (common property of that plan counts); under
 *  "lot" some anchor lot must share the plan and the lot (compared as text). A dominant lot without a plan is other land. */
export function lotScopeAllows(objectLot:ObjectLotScope|null|undefined,anchor:LotAnchor|null|undefined,scope:LotScope) {
  if(scope==="all"||!anchor?.lots.length)return true;
  const dominant=objectLot?.dominant;
  if(!dominant)return true;
  if(!dominant.plan)return false;
  return anchor.lots.some(a=>a.plan===dominant.plan&&(scope==="land"||sameLot(dominant.lot,a.lot)));
}
/** What the scope does to a list of objects' lot rows: kept and hidden count objects with a dominant lot; unknown (no row, no
 *  dominant lot) are always shown and never counted as kept. kept + hidden + unknown is the list length. */
export function lotScopeSummary(objects:readonly (ObjectLotScope|null|undefined)[],anchor:LotAnchor|null|undefined,scope:LotScope) {
  const summary={kept:0,hidden:0,unknown:0};
  for(const objectLot of objects){
    if(!objectLot?.dominant)summary.unknown++;
    else if(lotScopeAllows(objectLot,anchor,scope))summary.kept++;
    else summary.hidden++;
  }
  return summary;
}

/** Which client-side filter keeps an object out of the queue: the frame filter first ("frame_cut": frameFilterAllows is false under the
 *  current frame value), then the lot scope ("other_land": lotScopeAllows is false against the anchor); null when neither excludes it.
 *  The editor uses it to decide whether an object the reviewer asked for (URL, canvas click) is held in view and, when it is not, which
 *  filter to name in the notice. A reading of the filters, never a label: nothing here changes the object. */
export type FilterExclusion="frame_cut"|"other_land";
export function filterExclusion(object:Pick<SceneObject,"ring">,model:Partial<Pick<SceneModelSnapshot,"image_width"|"image_height">>|null|undefined,frame:FrameFilter,
  lotRow:ObjectLotScope|null|undefined,anchor:LotAnchor|null|undefined,scope:LotScope):FilterExclusion|null {
  if(!frameFilterAllows(object,model,frame))return "frame_cut";
  if(!lotScopeAllows(lotRow,anchor,scope))return "other_land";
  return null;
}
/** Whether the inspected object stays in view although a filter excludes it: unsaved edits or an open geometry edit always keep it
 *  (the reviewer must not lose work to a filter); otherwise only a non-excluded object is held. */
export const holdInView=({excluded,dirty,editingGeometry}:{excluded:boolean;dirty:boolean;editingGeometry:boolean})=>dirty||editingGeometry||!excluded;
