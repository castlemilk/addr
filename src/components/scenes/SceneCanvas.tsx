"use client";
import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import MetricInput from "@/components/reviews/MetricInput";
import { carPlan } from "@/lib/scene-car";
import { isPavedSurface, pavedSurface } from "@/lib/scene-surface";
import { sceneVisibility } from "@/lib/scene-visibility";
import { roofMount } from "@/lib/scene-roof-mount";
import { imageBearing, normalizedBearing, withObjectDirection } from "@/lib/scene-direction";
import { imageFrameScale, objectImageFrame } from "@/lib/scene-image-frame";
import { initialImageFrame, initialViewRings, windowLotPfi } from "@/lib/scene-initial-view";
import { currentRoofLineEdits } from "@/lib/roof-line-edits";
import { minAreaRectRing, type RoofRing } from "@/lib/roof-geometry";
import { ROOF_LINE_COLORS, roofImageLines } from "@/lib/scene-roof-lines";
import type { SquaredCandidate } from "@/lib/queues";
import { PARCEL_SOURCE, parcelBoundsIssue, parcelGeometryGroups, parcelImagePolygons, parcelLabelPoint, parcelLotLabel, parcelLotSentence, parcelNearOutline, parcelOutlineSummary, parcelsTouchingOutline, sceneGeoBounds, sceneParcelBounds } from "@/lib/property-parcels";
import { usePropertyParcels } from "./usePropertyParcels";
import { sceneObjectBeneath, sceneObjectStack, scenePaintAreas, scenePaintOrder } from "@/lib/scene-paint-order";
import {
  CLASS_COLORS,
  checkOutlineWithGapRepair,
  droppedGapsNotice,
  polygonIssues,
  scenePathData,
  transformPoint,
  type SceneModelSnapshot,
  type SceneObject,
  type ScenePoint,
} from "@/lib/scene-types";
interface Props {
  scene: SceneModelSnapshot;
  image: string;
  objects: SceneObject[];
  selected: string | null;
  visible: Set<string>;
  objectIds?: ReadonlySet<string>;
  mode: "review" | "edit" | "draw";
  layer: "model" | "edited" | "overlay";
  original: SceneObject[];
  disabled: boolean;
  onSelect: (id: string) => void;
  onChange: (object: SceneObject) => void;
  onDraw: (ring: ScenePoint[]) => void;
  onSwipe: (right: boolean) => void;
  onDrawingChange: (pending: boolean) => void;
  onExpand?: () => void;
  /** The selected object's outline differs from the saved review: a decision stores the reviewer's outline. */
  pendingOutline?: boolean;
  /** The object-hints module's orthogonalised outline for the selected object (its squared_candidate, unchanged since object-hints-v1: a pixel ring
   *  with its agreement numbers, or the gate's refusal). Drawn as a dashed overlay only when the reviewer turns it on, applied
   *  only through "Use squared candidate"; the stored ring is never replaced by itself. */
  squaredCandidate?: SquaredCandidate | null;
  /** The scene's lot-frame plan sentence (lot-tags-v1 "_scene" row via /api/scenes/hints): printed under the lot sentence on
   *  cohort (fitted) frames only, text only; no capture button. */
  lotFrameSentence?: string | null;
}
/** localStorage key of the squared-candidate overlay toggle; default off (Step 6 of the CP20 spec: default-on is decided by numbers later). */
export const SQUARED_CANDIDATE_KEY = "scene.showSquaredCandidate";
export default function SceneCanvas({
  scene,
  image,
  objects,
  selected,
  visible,
  objectIds,
  mode,
  layer,
  original,
  disabled,
  onSelect,
  onChange,
  onDraw,
  onSwipe,
  onDrawingChange,
  onExpand,
  pendingOutline = false,
  squaredCandidate = null,
  lotFrameSentence = null,
}: Props) {
  const svg = useRef<SVGSVGElement>(null), scroll = useRef<HTMLDivElement>(null), previousZoom = useRef(1),
    gesture = useRef<{
      x: number;
      y: number;
      point?: number;
      id?: string;
      target?: string;
      pointerId?: number;
      direction?: { center: ScenePoint; length: number; initial: number; heading: number };
    } | null>(null);
  const [pan, setPan] = useState(0),
    [draw, setDraw] = useState<ScenePoint[]>([]),
    [drawCursor, setDrawCursor] = useState<ScenePoint | null>(null),
    [point, setPoint] = useState<number | null>(null),
    [zoom, setZoom] = useState(1),
    [focused, setFocused] = useState(mode !== "draw");
  // Decision card item 3 (6 October 2026, "yes to defaults" 7 October): a window scene opens on the union of its asked objects (the
  // carried frame-cut slices, their replacers, carried bodies), each asked roof's building cluster (touch <= 0.5 m) and the planned
  // lot polygon, capped to the image. The URL object is selected and highlighted, not zoomed to. The first selection change after
  // mount (an object click, Previous / Next, a decision that advances), Whole scene or Fit parcel ends the initial view; the
  // per-object fit is then what it was. Edit and draw mode never start it; a non-window scene (no window_frame) never starts it.
  const [initialView,setInitialView]=useState(mode==="review"&&!!scene.window_frame);
  const mountSelected=useRef(selected);
  useLayoutEffect(()=>{if(selected!==mountSelected.current)setInitialView(false);},[selected]);
  const [boundary,setBoundary]=useState(-1),[drawingHole,setDrawingHole]=useState(false),[drawingError,setDrawingError]=useState("");
  const [corner,setCorner]=useState<ScenePoint>([scene.image_width/2,scene.image_height/2]);
  const [directionPreview,setDirectionPreview]=useState<{id:string;heading:number;center:ScenePoint;length:number}|null>(null);
  const [viewport,setViewport]=useState<{width:number;height:number}|null>(null);
  const [showRoofStructure,setShowRoofStructure]=useState(true);
  // A vertex drag or a typed coordinate previews its ring here and commits once (pointer-up, field blur or Enter). The
  // ref is the value committed; the state mirrors it for rendering.
  const [ringPreview,setRingPreview]=useState<{id:string;boundary:number;ring:ScenePoint[]}|null>(null);
  const ringGesture=useRef<{id:string;boundary:number;ring:ScenePoint[]}|null>(null);
  const [showParcels,setShowParcels]=useState(true), [parcelFocus,setParcelFocus]=useState<string|null>(null);
  // The squared-candidate overlay is off until the reviewer turns it on; the choice persists per browser, never per object.
  const [showSquaredCandidate,setShowSquaredCandidate]=useState(()=>{try{return typeof window!=="undefined"&&window.localStorage.getItem(SQUARED_CANDIDATE_KEY)==="true";}catch{return false;/* storage unavailable: stays off */}});
  const toggleSquaredCandidate=(on:boolean)=>{setShowSquaredCandidate(on);try{window.localStorage.setItem(SQUARED_CANDIDATE_KEY,on?"true":"false");}catch{/* storage unavailable: session only */}};
  // Every scene with affines carries geography: a registered capture exactly, a cohort frame through its fitted affines.
  const geo=sceneGeoBounds(scene);
  const parcelBounds=sceneParcelBounds(scene);
  const parcelQuery=parcelBounds&&!parcelBoundsIssue(parcelBounds)?parcelBounds.join(","):null;
  const parcels=usePropertyParcels(showParcels?parcelQuery:null);
  const fittedFrame=geo?.registration==="fitted";
  const contextClipId=useId();
  const contextUrl=scene.region&&scene.image_width===512&&scene.image_height===512
    ?`/api/scenes/context?id=${encodeURIComponent(scene.id)}&version=${scene.model_signature}`:null;
  const [contextState,setContextState]=useState<{url:string;status:"loaded"|"failed"}|null>(null);
  // Edit mode is keyed by selected object in the workspace. Keep that frame
  // fixed while a vertex moves, rather than moving the image under the pointer.
  const [editFrameRings,setEditFrameRings]=useState(()=>[objects.find(o=>o.id===selected)?.ring||[],original.find(o=>o.id===selected)?.ring||[]]);
  useLayoutEffect(()=>{
    const element=scroll.current;
    if(element){
      const ratio=zoom/previousZoom.current;
      element.scrollLeft=(element.scrollLeft+element.clientWidth/2)*ratio-element.clientWidth/2;
      element.scrollTop=(element.scrollTop+element.clientHeight/2)*ratio-element.clientHeight/2;
    }
    previousZoom.current=zoom;
  },[zoom]);
  useLayoutEffect(()=>{
    const element=scroll.current;
    if(element){element.scrollLeft=(element.scrollWidth-element.clientWidth)/2;element.scrollTop=(element.scrollHeight-element.clientHeight)/2;}
  },[selected,focused,parcelFocus,initialView]);
  useLayoutEffect(()=>{
    const element=svg.current;
    if(!element)return;
    // The keyed edit canvas must start at its measured aspect before painting
    // or accepting a pointer; waiting for ResizeObserver briefly uses a square.
    const {width,height}=element.getBoundingClientRect();
    setViewport({width,height});
    const observer=new ResizeObserver(([entry])=>{
      const {width,height}=entry.contentRect;
      setViewport(old=>old?.width===width&&old.height===height?old:{width,height});
    });
    observer.observe(element);
    return ()=>observer.disconnect();
  },[]);
  const drawing=mode==="draw"||drawingHole;
  const current = objects.find((o) => o.id === selected),
    canEdit = mode === "edit" && layer !== "model" && !disabled,
    editable=canEdit&&!drawingHole;
  const activeBoundary=boundary>=0&&current?.holes?.[boundary]?boundary:-1;
  const committedRing=activeBoundary<0?current?.ring:current?.holes?.[activeBoundary];
  // One page change per gesture: one undo entry, and the page re-anchors pending roof lines against the final ring only,
  // never against a transient position of the pointer or a half-typed number (a line that is only passingly outside is
  // never dropped). Handles, fields and the outline path all follow the preview while it is live.
  const previewRing=ringPreview&&current&&ringPreview.id===current.id&&ringPreview.boundary===activeBoundary?ringPreview.ring:null;
  const currentRing=previewRing??committedRing;
  const withRing=(o:SceneObject,ring:ScenePoint[]):SceneObject=>({...o,...(activeBoundary<0?{ring}:{holes:(o.holes||[]).map((h,i)=>i===activeBoundary?ring:h)})});
  const updateBoundary=(ring:ScenePoint[])=>{
    if(!current)return;
    onChange({...withRing(current,ring),status:"needs_adjustment",geometry_reviewed:false});
  };
  const showPreview=(ring:ScenePoint[]|null)=>{ringGesture.current=ring&&current?{id:current.id,boundary:activeBoundary,ring}:null;setRingPreview(ringGesture.current);};
  const previewPoint=(index:number,p:ScenePoint)=>{if(committedRing)showPreview(committedRing.map((v,i)=>i===index?p:v));};
  const commitPreview=()=>{
    const pending=ringGesture.current;showPreview(null);
    if(pending&&current&&pending.id===current.id&&pending.boundary===activeBoundary&&JSON.stringify(pending.ring)!==JSON.stringify(committedRing))updateBoundary(pending.ring);
  };
  const displayed=layer==="model"?original.find(o=>o.id===selected):current;
  const roofLines=roofImageLines(displayed,scene.meters_to_pixel);
  const roofVisible=mode==="review"&&!drawing&&!!displayed&&visible.has(displayed.kind)
    &&(!objectIds||objectIds.has(displayed.id))&&!["rejected","superseded"].includes(displayed.status)&&roofLines.length>0;
  const solar=useMemo(()=>{
    const items=layer==="model"?original:objects,object=items.find(o=>o.id===selected);
    return object?.kind==="solar_panel"?roofMount(object,items,scene.pixel_to_meters).solar:null;
  },[layer,original,objects,selected,scene.pixel_to_meters]);
  const front=displayed?.kind==="car"&&!sceneVisibility(displayed,scene).footprintOnly?carPlan(displayed.ring.map(p=>transformPoint(p,scene.pixel_to_meters)),displayed.model)
    :displayed && isPavedSurface(displayed.kind)?pavedSurface(displayed.ring.map(p=>transformPoint(p,scene.pixel_to_meters)),displayed.model,displayed.holes?.map(h=>h.map(p=>transformPoint(p,scene.pixel_to_meters)))):solar&&solar.tilt_deg>1e-8?solar:null;
  const directionLabel=displayed?.kind==="car"?"Estimated vehicle front":displayed?.kind==="solar_panel"?"Solar downhill direction":"Surface grade direction";
  const directionVisible=!!front&&!!displayed&&visible.has(displayed.kind)&&(!objectIds||objectIds.has(displayed.id))&&!["rejected","superseded"].includes(displayed.status);
  const directionEditable=directionVisible&&mode==="review"&&layer!=="model"&&!disabled&&!drawing&&(!solar||solar.tilt_deg<=80);
  const preview=directionEditable&&directionPreview?.id===selected?directionPreview:null;
  const directionHeading=preview?.heading??front?.heading??0;
  const directionCenter=preview?.center??front?.center;
  const directionEnd=preview?[preview.center[0]+Math.sin(preview.heading*Math.PI/180)*preview.length,
    preview.center[1]+Math.cos(preview.heading*Math.PI/180)*preview.length] as ScenePoint:front?.front;
  const frontStart=directionCenter?transformPoint(directionCenter,scene.meters_to_pixel):null;
  const frontEnd=directionEnd?transformPoint(directionEnd,scene.meters_to_pixel):null;
  const handleLabel=displayed?.kind==="car"?"Vehicle direction handle":displayed?.kind==="solar_panel"?"Solar direction handle":"Surface direction handle";
  // A cancelled vertex drag (pointercancel, lost capture) discards its preview: the committed ring stays, nothing is emitted.
  const cancelDirection=()=>{if(gesture.current?.point!==undefined)showPreview(null);gesture.current=null;setDirectionPreview(null);setPan(0);};
  const frameRings=[current?.ring||[],original.find(o=>o.id===selected)?.ring||[]];
  // The squared candidate belongs to the selected object: a ring of three or more pixel corners, else the refusal reason.
  const candidateRing=squaredCandidate?.ring&&squaredCandidate.ring.length>=3&&displayed&&!["rejected","superseded"].includes(displayed.status)?squaredCandidate.ring as ScenePoint[]:null;
  const candidateVisible=showSquaredCandidate&&!!candidateRing&&!drawing;
  const candidateClaim=candidateRing?`Model estimate: the stored outline snapped to its own two axes${squaredCandidate?.iou!=null?`, agreement (IoU) ${squaredCandidate.iou.toFixed(2)} with the stored ring`:""}${squaredCandidate?.hausdorff_m!=null?`, largest offset ${squaredCandidate.hausdorff_m.toFixed(1)} m`:""}. Squared outlines were measured to sit on image edges more often than raw ones (J1 0.38 vs 0.30); this one is unverified until you look.`:null;
  const canApplyCandidate=canEdit&&!!candidateRing&&activeBoundary<0&&!!current;
  const nearbyParcel=parcelNearOutline(parcels.data?.features||[],displayed?.ring||[],scene);
  // Unit rows sit across several lots: list every lot under the outline (allocation context, not ownership)
  // and draw each distinct polygon once, so stacked strata lots do not pretend to be unit cuts.
  const touchingLots=parcelsTouchingOutline(parcels.data?.features||[],displayed?.ring||[],scene);
  const parcelGroups=parcelGeometryGroups(parcels.data?.features||[]);
  const touchingIds=new Set(touchingLots.map(t=>t.feature.id));
  const outlineSummary=parcelOutlineSummary(touchingLots);
  // The selected object's lot in one sentence: its lot and plan, whether the frame covers that lot, and the largest live roof
  // on it. Cadastral context for the reviewer, never an owner or a label.
  const lotSentence=parcels.data?parcelLotSentence(touchingLots,displayed,layer==="model"?original:objects,scene):null;
  const parcelRings=nearbyParcel?parcelImagePolygons(nearbyParcel,scene).flat()
    :touchingLots.length>1?touchingLots.flatMap(t=>parcelImagePolygons(t.feature,scene).flat()):[];
  const parcelPoints=parcelRings.flat();
  // The planned lot (window_frame.lots[0].pfi) among the loaded Vicmap features: the geometry Fit parcel draws, joining the initial
  // view once the fetch lands (parcels arrive after the first paint; the frame widens then, while the initial view is still on).
  const lotPfi=windowLotPfi(scene);
  const lotFeature=(lotPfi&&parcels.data?.features.find(f=>String(f.properties.pfi)===lotPfi))||null;
  const windowLotRings=useMemo(()=>lotFeature?parcelImagePolygons(lotFeature,scene).flat():[],[lotFeature,scene]);
  const initial=useMemo(()=>initialView?initialViewRings(objects,scene,windowLotRings):null,[initialView,objects,scene,windowLotRings]);
  const initialFrame=initial?initialImageFrame(initial,{width:scene.image_width,height:scene.image_height},viewport||{width:264,height:264}):null;
  const contextMargin=contextUrl?256:0;
  const canFitParcel=parcelPoints.length>0&&parcelPoints.every(([x,y])=>x>=-contextMargin+12&&x<=scene.image_width+contextMargin-12&&y>=-contextMargin+12&&y<=scene.image_height+contextMargin-12);
  const fittingParcel=showParcels&&mode==="review"&&parcelFocus===selected&&canFitParcel;
  const frame=fittingParcel?{
    x:Math.min(...parcelPoints.map(p=>p[0]))-12,y:Math.min(...parcelPoints.map(p=>p[1]))-12,
    width:Math.max(...parcelPoints.map(p=>p[0]))-Math.min(...parcelPoints.map(p=>p[0]))+24,
    height:Math.max(...parcelPoints.map(p=>p[1]))-Math.min(...parcelPoints.map(p=>p[1]))+24,
  }:initialFrame?initialFrame:focused?objectImageFrame(mode==="edit"?editFrameRings:frameRings,
    {width:scene.image_width,height:scene.image_height},viewport||{width:264,height:264},!!contextUrl)
    :{x:0,y:0,width:scene.image_width,height:scene.image_height};
  const needsContext=!!contextUrl&&(focused||fittingParcel)&&(frame.x<0||frame.y<0||frame.x+frame.width>scene.image_width||frame.y+frame.height>scene.image_height);
  const imageScale=imageFrameScale(frame,viewport||{width:264,height:264});
  const coordinate = (e: React.PointerEvent, clamp = true): ScenePoint => {
    const matrix = svg.current!.getScreenCTM()!;
    const p = new DOMPoint(e.clientX, e.clientY).matrixTransform(
      matrix.inverse(),
    );
    return clamp?[Math.max(0, Math.min(scene.image_width, p.x)), Math.max(0, Math.min(scene.image_height, p.y))]:[p.x,p.y];
  };
  const updatePoint = (index: number, p: ScenePoint) => {
    if (currentRing) updateBoundary(currentRing.map((v,i)=>i===index?p:v));
  };
  const paintable = (o: SceneObject) => visible.has(o.kind) && (!objectIds || objectIds.has(o.id)) && !["rejected","superseded"].includes(o.status);
  // Paint order (road-shape 2026-10-07, E1): ground surfaces under standing objects under roof fixtures, the larger outline first
  // within a tier (scene-paint-order.ts). Areas come from the committed objects, never the drag preview, so a vertex drag cannot
  // reshuffle the stack. draft.objects is never reordered: queue, advance, keys and the saved order keep array order.
  const paintAreas = useMemo(() => ({ edited: scenePaintAreas(objects), model: scenePaintAreas(original) }), [objects, original]);
  // Alt/Option-click: the object beneath the selected one under the pointer (the only pointer route to the lower of two stacked outlines).
  // `root` is the scene svg, taken from the event (never the ref, which render must not read).
  const objectBeneath = (e: React.MouseEvent, root: Element | null) =>
    root ? sceneObjectBeneath(sceneObjectStack(document.elementsFromPoint(e.clientX, e.clientY), root), selected) : undefined;
  const echoSource = layer === "model" ? original.find(o => o.id === selected) : current && previewRing ? withRing(current, previewRing) : current;
  const selectedEcho = echoSource && paintable(echoSource) ? echoSource : null;
  const render = (items: SceneObject[], baseline = false) =>
    scenePaintOrder(items.filter(paintable), baseline || layer === "model" ? paintAreas.model : paintAreas.edited)
      .map((o) => (
        <path
          key={`${baseline ? "model" : "edit"}-${o.id}`}
          d={scenePathData(o)}
          fillRule="evenodd"
          fill={
            baseline
              ? "none"
              : o.id === selected
                ? `${CLASS_COLORS[o.kind]}38`
                : `${CLASS_COLORS[o.kind]}09`
          }
          stroke={baseline ? "#f4cc89" : CLASS_COLORS[o.kind]}
          strokeWidth={o.id === selected ? 2.7 : 1.2}
          strokeDasharray={baseline ? "5 4" : undefined}
          opacity={baseline ? 0.7 : o.id === selected ? 1 : 0.6}
          vectorEffect="non-scaling-stroke"
          role="button"
          tabIndex={!baseline && o.id === selected ? 0 : -1}
          aria-label={`Select ${o.label} ${o.id.slice(-4)}`}
          data-object-id={o.id}
          onKeyDown={(e) => {
            if (!disabled && !drawing && (e.key === "Enter" || e.key === " ")) {
              e.preventDefault();
              onSelect(o.id);
            }
          }}
          onClick={(e) => {
            if (!disabled && !drawing && mode === "edit") onSelect(e.altKey ? objectBeneath(e, e.currentTarget.ownerSVGElement) ?? o.id : o.id);
          }}
        >
          <title>
            {o.label} · {o.status.replaceAll("_", " ")}
          </title>
        </path>
      ));
  return (
    <div className={`scene-canvas-panel${contextUrl ? " has-map-context" : ""}${parcelQuery ? " has-parcel-context" : ""}`}>
      <div className="scene-canvas-tools">
        <span>
          {drawing
            ? drawingHole?"Place corners around the interior gap":"Place outline corners"
            : mode === "edit"
              ? "Drag or arrow-nudge a point (Shift 5 px) · Backspace removes it · Enter = Looks right · Shift+Enter = Still needs work · Esc finishes editing"
              : directionEditable?"Drag the direction handle · swipe elsewhere to review":"Swipe the card · right looks right, left needs edit"}
        </span>
        <div>
          <button
            className="review-button"
            disabled={!current || drawing}
            aria-pressed={focused}
            onClick={() => {setInitialView(false);setParcelFocus(null);if(fittingParcel){setFocused(true);return;}if(!focused)setEditFrameRings(frameRings);setFocused(!focused);}}
          >
            {fittingParcel ? "Focus object" : focused ? "Whole scene" : "Focus object"}
          </button>
          <button
            className="review-button"
            aria-label="Zoom image out"
            disabled={zoom <= 1}
            onClick={() => setZoom(Math.max(1, zoom - 0.5))}
          >
            −
          </button>
          <button className="review-button" aria-label="Reset image zoom" onClick={() => setZoom(1)}>
            {Math.round(zoom * 100)}%
          </button>
          <button
            className="review-button"
            aria-label="Zoom image in"
            disabled={zoom >= 4}
            onClick={() => setZoom(Math.min(4, zoom + 0.5))}
          >
            +
          </button>
        </div>
      </div>
      {parcelQuery&&<div className="scene-parcel-controls" data-parcel-registration={geo?.registration}>
        <label><input type="checkbox" checked={showParcels} onChange={e=>{setShowParcels(e.target.checked);setParcelFocus(null);}}/>Property boundaries <span aria-hidden="true">┄</span></label>
        {showParcels&&<>
          <span role="status">{parcels.pending?"Loading Vicmap…":parcels.error?"Boundaries unavailable":nearbyParcel?`Nearby lot ${nearbyParcel.properties.spi}`:outlineSummary?outlineSummary:parcels.data?.features.length?"Surrounding parcels":"No parcels in this area"}{fittedFrame&&!parcels.pending&&!parcels.error?" Boundaries placed from the fitted cohort frame; alignment unverified.":""}</span>
          {lotSentence&&<p className="scene-lot-sentence" data-testid="scene-lot-sentence">{lotSentence}</p>}
          {fittedFrame&&lotFrameSentence&&<p className="scene-lot-sentence scene-lot-frame" data-testid="scene-lot-frame">{lotFrameSentence}</p>}
          {canFitParcel&&<button className="review-button" disabled={mode!=="review"||drawing||disabled} aria-pressed={fittingParcel} onClick={()=>{setInitialView(false);setParcelFocus(fittingParcel?null:selected);setZoom(1);}}>{touchingLots.length>1&&!nearbyParcel?"Fit lots":"Fit parcel"}</button>}
          {contextUrl&&onExpand&&canFitParcel&&<button className="review-button" disabled={mode!=="review"||drawing||disabled} onClick={onExpand}>Analyse surrounding area</button>}
          {parcels.error&&<button className="review-button" onClick={parcels.retry}>Retry boundaries</button>}
          <details><summary>Source</summary><div>
            <a href={PARCEL_SOURCE.url} target="_blank" rel="noreferrer">Vicmap Parcel</a> · {PARCEL_SOURCE.attribution} · <a href={PARCEL_SOURCE.license_url} target="_blank" rel="noreferrer">CC BY 4.0</a>.
            <p>Approved surface parcels. Land boundaries, separate from roof outlines. Proximity is a viewing aid; shared lots can contain multiple addresses.</p>
            {parcels.data&&<p>Retrieved {new Date(parcels.data.retrieved_at).toLocaleDateString("en-AU")}. Geometry reprojected to the saved image frame.</p>}
            {fittedFrame&&<p data-testid="scene-parcel-fitted-note">Cohort frame registration is fitted from the CRM roof by least squares, not from a tile capture; boundaries may sit a few pixels off.</p>}
            <p>Vicmap Parcel carries separate unit polygons but no stacked-strata cuts, fence lines, party walls, building envelopes or easements. Fence lines are not a data download; they would need an imagery line detector.</p>
            {nearbyParcel&&<p>PFI {nearbyParcel.properties.pfi} · {nearbyParcel.properties.horizontal_uncertainty_m===null?"Source positional uncertainty unavailable":`Source positional uncertainty: ${nearbyParcel.properties.horizontal_uncertainty_m} m`}. Imagery alignment may differ.</p>}
            {touchingLots.length>1&&<p>{touchingLots.length} lots under this outline: {touchingLots.map(t=>`${parcelLotLabel([t.feature])} ${Math.round(t.share*100)}%`).join(", ")}. Lot cuts are allocation edges, not roof folds; stacked lots share one polygon in Vicmap Parcel and carry no unit cuts.</p>}
            {parcels.error&&<p role="alert">{parcels.error}</p>}
          </div></details>
        </>}
      </div>}
      {roofVisible && <div className="scene-roof-legend">
        <label><input type="checkbox" checked={showRoofStructure} onChange={e=>setShowRoofStructure(e.target.checked)} /> Show roof structure</label>
        {showRoofStructure && <span aria-label="Roof structure legend">
          <span style={{color:ROOF_LINE_COLORS.ridge}}>Ridges / apexes</span>
          <span style={{color:ROOF_LINE_COLORS.hip}}>Hips</span>
          <span style={{color:ROOF_LINE_COLORS.valley}}>Valleys</span>
        </span>}
        <small>{displayed && currentRoofLineEdits(displayed) ? "Your roof line feedback · 3D surfaces remain estimates" : "Roof shape estimate · adjust with Edit roof lines"}</small>
      </div>}
      <div className="scene-image-scroll" ref={scroll}>
        <div
          className="scene-swipe-surface"
          style={{
            width: `${zoom * 100}%`,
            height: `calc(var(--scene-viewport-height) * ${zoom})`,
            transform: `translateX(${pan}px) rotate(${pan / 45}deg)`,
          }}
        >
          {Math.abs(pan) > 30 && (
            <strong
              className={`scene-swipe-stamp ${pan > 0 ? "is-right" : "is-left"}`}
            >
              {pan > 0 ? (pendingOutline ? "Save outline · Looks right" : "Looks right") : (pendingOutline ? "Save outline · Still needs work" : "Needs edit")}
            </strong>
          )}
          <svg
            ref={svg}
            viewBox={`${frame.x} ${frame.y} ${frame.width} ${frame.height}`}
            role="group"
            aria-label="Scene image with object annotations"
            className={`scene-svg mode-${mode}`}
            data-initial-view={initialFrame?"true":"false"}
            onPointerDown={(e) => {
              if (disabled || mode !== "review" || drawingHole || e.button !== 0 || !e.isPrimary) return;
              // Pointer capture keeps swipes on the card, but retargets clicks to
              // the SVG. Remember the touched outline so a tap still selects it.
              const hit = (e.target as Element).closest("[data-object-id]")?.getAttribute("data-object-id");
              const target = e.altKey ? objectBeneath(e, e.currentTarget) ?? hit : hit;
              gesture.current = { x: e.clientX, y: e.clientY, target: target || undefined };
              e.currentTarget.setPointerCapture(e.pointerId);
            }}
            onPointerMove={(e) => {
              if (drawing && !disabled) setDrawCursor(coordinate(e));
              const g = gesture.current;
              if (!g || disabled) return;
              if(g.direction){
                if(!directionEditable||g.id!==selected||g.pointerId!==e.pointerId)return;
                const heading=imageBearing(g.direction.center,coordinate(e,false),scene.pixel_to_meters);
                if(heading===null)return;
                g.direction.heading=heading;
                setDirectionPreview({id:g.id!,...g.direction});
                return;
              }
              if (g.point !== undefined && g.id === selected) {
                previewPoint(g.point, coordinate(e));  // committed once, on pointer-up
                return;
              }
              const dx = e.clientX - g.x,
                dy = e.clientY - g.y;
              if (Math.abs(dx) > Math.abs(dy) * 1.3)
                setPan(Math.max(-140, Math.min(140, dx)));
            }}
            onPointerUp={(e) => {
              const g = gesture.current;
              if(g?.direction&&g.pointerId!==e.pointerId)return;
              gesture.current = null;
              if(g?.direction){
                setDirectionPreview(null);setPan(0);
                if(directionEditable&&current&&g.id===selected&&Math.abs(g.direction.heading-g.direction.initial)>1e-6)
                  onChange(withObjectDirection(current,g.direction.heading,solar?.tilt_deg));
                return;
              }
              if (g?.point !== undefined) {
                setPan(0);
                commitPreview();
                return;
              }
              if (drawing && !disabled && layer!=="model" && draw.length<200) {
                setDraw((old) => [...old, coordinate(e)]);
                return;
              }
              const dx = g ? e.clientX - g.x : 0,
                dy = g ? e.clientY - g.y : 0;
              setPan(0);
              if (
                g &&
                Math.abs(dx) > 90 &&
                Math.abs(dx) > Math.abs(dy) * 1.3 &&
                !disabled
              )
                onSwipe(dx > 0);
              else if (g?.target && Math.hypot(dx, dy) < 8 && !disabled)
                onSelect(g.target);
            }}
            onPointerCancel={cancelDirection}
            onLostPointerCapture={cancelDirection}
          >
            <image href={image} width={scene.image_width} height={scene.image_height} />
            {needsContext&&(contextState?.url!==contextUrl||contextState?.status!=="failed") ? <g pointerEvents="none" aria-label="Surrounding aerial imagery">
              <defs><clipPath id={contextClipId}><path d="M-256 -256H768V768H-256Z M0 0H512V512H0Z" clipRule="evenodd" /></clipPath></defs>
              <image href={contextUrl!} x={-256} y={-256} width={1024} height={1024} clipPath={`url(#${contextClipId})`}
                onLoad={()=>setContextState({url:contextUrl!,status:"loaded"})} onError={()=>setContextState({url:contextUrl!,status:"failed"})} />
              <rect x={0} y={0} width={scene.image_width} height={scene.image_height} fill="none" stroke="#e2e8f0" strokeOpacity={.35} strokeDasharray="4 5" vectorEffect="non-scaling-stroke" />
            </g>:null}
            {showParcels&&parcels.data&&<g pointerEvents="none" aria-label="Vicmap property boundaries">
              {parcelGroups.map(group=>{
                const polygons=parcelImagePolygons(group.feature,scene);
                const highlighted=group.features.some(f=>f.id===nearbyParcel?.id||touchingIds.has(f.id));
                // Every distinct polygon is drawn: a faint dashed ring for context, a stronger one for lots touching the outline.
                return <path key={group.key} className="scene-parcel" data-parcel-pfi={group.feature.properties.pfi} data-parcel-lots={group.features.map(f=>f.properties.lot).join(",")} data-parcel-highlighted={highlighted?"true":undefined} data-parcel-context={highlighted?undefined:"true"}
                  d={polygons.flat().map(ring=>`M${ring.map(p=>p.join(",")).join("L")}Z`).join(" ")}
                  fill="none" stroke="#fbbf24" strokeWidth={highlighted?2:.8} strokeOpacity={highlighted?1:.4} strokeDasharray="6 4" vectorEffect="non-scaling-stroke">
                  <title>{group.features.length>1?`Vicmap parcels ${group.features.map(f=>f.properties.spi).join(", ")} · stacked lots, one land boundary`:`Vicmap parcel ${group.feature.properties.spi} · land boundary`}</title>
                </path>;})}
              {parcelGroups.filter(group=>group.features.some(f=>f.id===nearbyParcel?.id||touchingIds.has(f.id))).map(group=>{
                const point=parcelLabelPoint(parcelImagePolygons(group.feature,scene));
                if(!point)return null;
                const [x,y]=point;
                return <text key={`label-${group.key}`} className="scene-parcel-label" data-parcel-label={parcelLotLabel(group.features)} x={x} y={y} textAnchor="middle" dominantBaseline="middle"
                  fontSize={11/imageScale} fontWeight={600} fill="#fbbf24" stroke="#0f172a" strokeWidth={3/imageScale} paintOrder="stroke" style={{fontFamily:"inherit"}}>{parcelLotLabel(group.features)}</text>;})}
            </g>}
            {layer === "overlay" && render(original, true)}
            {render(layer === "model" ? original : previewRing && current ? objects.map(o => o.id === current.id ? withRing(o, previewRing) : o) : objects)}
            {/* The selected outline again on top of the stack: an echo that never takes a click (no data-object-id, pointer-events none),
                so a selected road reads above the cars and trees painted over it while they stay clickable. */}
            {selectedEcho && <path className="scene-selected-echo" d={scenePathData(selectedEcho)} fill="none" stroke={CLASS_COLORS[selectedEcho.kind]}
              strokeWidth={2.7} vectorEffect="non-scaling-stroke" pointerEvents="none" aria-hidden="true" />}
            {candidateVisible&&candidateRing&&<path className="scene-squared-candidate" data-testid="scene-squared-candidate" pointerEvents="none" fill="none" stroke="#67d7e7" strokeWidth={2} strokeDasharray="7 4" vectorEffect="non-scaling-stroke"
              d={`M${candidateRing.map(p=>p.join(",")).join("L")}Z`}>
              <title>{`Squared candidate · model estimate, not applied${squaredCandidate?.iou!=null?` · IoU ${squaredCandidate.iou.toFixed(2)} with the stored outline`:""}${squaredCandidate?.hausdorff_m!=null?` · largest offset ${squaredCandidate.hausdorff_m.toFixed(1)} m`:""}`}</title>
            </path>}
            {roofVisible && showRoofStructure && <g pointerEvents="none" aria-label="Selected roof structure">
              {roofLines.map((line,i)=><line key={i} x1={line.a[0]} y1={line.a[1]} x2={line.b[0]} y2={line.b[1]}
                stroke={ROOF_LINE_COLORS[line.kind]} strokeWidth={line.kind==="ridge"?2.8:1.8}
                strokeDasharray={line.kind==="valley"?"5 3":undefined} vectorEffect="non-scaling-stroke">
                <title>{line.kind === "valley" ? "Valley / saddle line" : line.kind === "ridge" ? "Ridge / apex line" : "Hip line"}</title>
              </line>)}
            </g>}
            {directionVisible && frontStart && frontEnd && <g pointerEvents="none" aria-label={`${directionLabel} ${Math.round(directionHeading)} degrees`}>
              <defs><marker id="scene-car-front" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="white"/></marker></defs>
              <line x1={frontStart[0]} y1={frontStart[1]} x2={frontEnd[0]} y2={frontEnd[1]} stroke="white" strokeWidth="2" vectorEffect="non-scaling-stroke" markerEnd="url(#scene-car-front)">
                <title>{directionLabel} · {Math.round(directionHeading)}°</title>
              </line>
            </g>}
            {directionEditable&&front&&frontEnd&&current&&<g className="scene-direction-handle" role="slider" tabIndex={0}
              aria-label={handleLabel} aria-valuemin={0} aria-valuemax={359.99} aria-valuenow={normalizedBearing(Number(normalizedBearing(directionHeading).toFixed(2)))}
              aria-valuetext={`${normalizedBearing(directionHeading).toFixed(1)} degrees clockwise from north`}
              onPointerDown={e=>{
                if(e.button!==0||!e.isPrimary||gesture.current?.direction)return;
                e.preventDefault();e.stopPropagation();e.currentTarget.focus();
                const direction={center:front.center,length:Math.hypot(front.front[0]-front.center[0],front.front[1]-front.center[1]),initial:front.heading,heading:front.heading};
                gesture.current={x:e.clientX,y:e.clientY,id:current.id,pointerId:e.pointerId,direction};
                setDirectionPreview({id:current.id,...direction});
                svg.current?.setPointerCapture(e.pointerId);
              }}
              onClick={e=>e.stopPropagation()}
              onKeyDown={e=>{
                if(e.key==="Escape"){e.preventDefault();e.stopPropagation();cancelDirection();return;}
                if(!["ArrowLeft","ArrowRight","ArrowUp","ArrowDown","Home","End"].includes(e.key))return;
                e.preventDefault();e.stopPropagation();
                if(gesture.current?.direction||e.altKey||e.ctrlKey||e.metaKey)return;
                const step=e.shiftKey?10:1;
                const heading=e.key==="Home"?0:e.key==="End"?359.99:front.heading+(["ArrowRight","ArrowUp"].includes(e.key)?step:-step);
                if(Math.abs(normalizedBearing(heading)-normalizedBearing(front.heading))>1e-6)
                  onChange(withObjectDirection(current,heading,solar?.tilt_deg));
              }}>
              <title>Drag to turn · arrow keys 1° · Shift 10° · Escape cancels a drag</title>
              <circle cx={frontEnd[0]} cy={frontEnd[1]} r={20/imageScale} fill="transparent" pointerEvents="all"/>
              <circle className="scene-direction-ring" cx={frontEnd[0]} cy={frontEnd[1]} r={7/imageScale} stroke="white" strokeWidth="2" vectorEffect="non-scaling-stroke" fill={CLASS_COLORS[current.kind]} pointerEvents="none"/>
            </g>}
            {/* Vertex handles follow the direction-handle pattern: radii and hit disc divided by imageScale and a
                non-scaling stroke, so a handle is the same size on screen at every zoom (7 px dot, 10 px selected with
                inverted fill, 24 px hit disc). The focusable <g> carries the label; focus styling lives in scenes.css. */}
            {editable && current &&
              currentRing?.map((p, i) => (
                <g
                  key={i}
                  className={`scene-vertex-handle${point === i ? " is-selected" : ""}`}
                  tabIndex={0}
                  role="button"
                  aria-label={`Outline point ${i + 1}`}
                  onFocus={() => setPoint(i)}
                  onPointerDown={(e) => {
                    e.stopPropagation();
                    setPoint(i);
                    e.currentTarget.focus({ preventScroll: true });
                    gesture.current = { x: e.clientX, y: e.clientY, point: i, id: current.id };
                    svg.current?.setPointerCapture(e.pointerId);
                  }}
                  onKeyDown={(e) => {
                    if ((e.key === "Backspace" || e.key === "Delete") && currentRing && currentRing.length > 3) {
                      e.preventDefault();
                      e.stopPropagation();
                      updateBoundary(currentRing.filter((_, j) => j !== i));
                      setPoint(null);
                      return;
                    }
                    const d: Record<string, ScenePoint> = {
                      ArrowLeft: [-1, 0],
                      ArrowRight: [1, 0],
                      ArrowUp: [0, -1],
                      ArrowDown: [0, 1],
                    };
                    if (!d[e.key]) return; // Enter/Escape bubble to the page handler
                    e.preventDefault();
                    const step = e.shiftKey ? 5 : 1,
                      [x, y] = d[e.key];
                    updatePoint(i, [
                      Math.max(0, Math.min(scene.image_width, p[0] + x * step)),
                      Math.max(0, Math.min(scene.image_height, p[1] + y * step)),
                    ]);
                  }}
                >
                  <title>Point {i + 1} · drag or arrow keys · Backspace removes</title>
                  <circle cx={p[0]} cy={p[1]} r={12 / imageScale} fill="transparent" pointerEvents="all" />
                  <circle
                    className="scene-vertex-dot"
                    cx={p[0]}
                    cy={p[1]}
                    r={(point === i ? 5 : 3.5) / imageScale}
                    fill={point === i ? CLASS_COLORS[current.kind] : "white"}
                    stroke={point === i ? "white" : CLASS_COLORS[current.kind]}
                    strokeWidth="1.5"
                    vectorEffect="non-scaling-stroke"
                    pointerEvents="none"
                  />
                </g>
              ))}
            {draw.length > 0 && (
              <polyline
                points={draw.map((p) => p.join(",")).join(" ")}
                fill="rgba(103,215,231,.15)"
                stroke="#67d7e7"
                strokeWidth="2"
              />
            )}
            {drawing && draw.length > 0 && drawCursor && <g pointerEvents="none" aria-hidden="true">
              <line x1={draw[draw.length-1][0]} y1={draw[draw.length-1][1]} x2={drawCursor[0]} y2={drawCursor[1]} stroke="#67d7e7" strokeWidth="2" strokeDasharray="6 4" vectorEffect="non-scaling-stroke" />
              {draw.length >= 2 && <line x1={drawCursor[0]} y1={drawCursor[1]} x2={draw[0][0]} y2={draw[0][1]} stroke="#67d7e7" strokeOpacity={.5} strokeWidth="1.5" strokeDasharray="3 5" vectorEffect="non-scaling-stroke" />}
            </g>}
          </svg>
        </div>
      </div>
      {needsContext&&<p className="scene-context-note" role="status">{contextState?.url===contextUrl&&contextState.status==="failed"
        ?"Surrounding imagery is unavailable. The house stays centred; saved outlines are unchanged."
        :contextState?.url===contextUrl&&contextState.status==="loaded"
          ?"Surrounding imagery shown · outlines and edits cover the saved capture."
          :"Loading surrounding imagery…"}</p>}
      {squaredCandidate&&displayed&&!drawing&&!["rejected","superseded"].includes(displayed.status)&&<div className="scene-inline-actions scene-squared-candidate-controls" data-testid="scene-squared-candidate-controls">
        {candidateRing?<>
          <label><input type="checkbox" checked={showSquaredCandidate} onChange={e=>toggleSquaredCandidate(e.target.checked)}/>Squared candidate <span aria-hidden="true">┅</span></label>
          <button className="review-button" data-testid="scene-use-squared-candidate" disabled={!canApplyCandidate}
            title={canApplyCandidate?"Replaces the outer outline with the squared candidate as needs adjustment; the model outline stays in the saved history. Look at the image first.":canEdit&&activeBoundary>=0?"Choose the outer outline first; the candidate has no interior gaps.":"Enter edit mode (the selected object, not the model layer) to apply the squared candidate."}
            onClick={()=>{if(canApplyCandidate&&candidateRing)updateBoundary(candidateRing);}}>Use squared candidate</button>
          <span className="review-muted">{candidateClaim}</span>
        </>:<span className="review-muted">No squared candidate for this outline{squaredCandidate.refused?`: ${squaredCandidate.refused}`:": the squaring gate refused"}. The stored outline stays; use the vertex tools or redraw.</span>}
      </div>}
      {drawing && (
        <div className="scene-inline-actions">
          <span>{draw.length} corners</span>
          <button
            className="review-button"
            disabled={disabled || !draw.length}
            onClick={() => setDraw(draw.slice(0, -1))}
          >
            Undo corner
          </button>
          <button
            className="review-button review-primary"
            disabled={disabled || draw.length < 3}
            onClick={() => {
              if(drawingHole&&current){
                // Gaps an outline edit left illegal stay in the draft until a save drops them (the notice below names them), so
                // the new gap is checked as drawn against the gaps as they will be saved, named by the number the Boundary list gives it.
                const holes=[...(current.holes||[]),draw],check=checkOutlineWithGapRepair(current.ring,current.holes,scene.image_width,scene.image_height),
                  number=[...holes.keys()].filter(i=>!check.dropped.some(d=>d.index===i)),
                  issue=polygonIssues(current.ring,scene.image_width,scene.image_height,[...check.holes,draw])[0]?.replace(/^Gap (\d+)/,(_,n)=>`Gap ${number[Number(n)-1]+1}`);
                if(issue){setDrawingError(issue);return;}
                onChange({...current,holes,status:"needs_adjustment",geometry_reviewed:false});
                setBoundary(holes.length-1);setPoint(null);setDrawingHole(false);setDrawingError("");onDrawingChange(false);
              } else onDraw(draw);
              setDraw([]);
            }}
          >
            {drawingHole?"Finish interior gap":"Finish outline"}
          </button>
          <button className="review-button" disabled={disabled} onClick={() => {setDraw([]);setDrawingError("");}}>
            Clear drawing
          </button>
          {drawingHole&&<>
            <label>Corner X<MetricInput aria-label="New gap corner X" min="0" max={scene.image_width} value={corner[0]} onValueChange={x=>setCorner([x,corner[1]])}/></label>
            <label>Corner Y<MetricInput aria-label="New gap corner Y" min="0" max={scene.image_height} value={corner[1]} onValueChange={y=>setCorner([corner[0],y])}/></label>
            <button className="review-button" disabled={disabled||draw.length>=200} onClick={()=>setDraw(old=>[...old,corner])}>Add gap corner</button>
            <button className="review-button" onClick={()=>{setDraw([]);setDrawingHole(false);setDrawingError("");onDrawingChange(false);}}>Cancel gap</button>
            {drawingError&&<p className="review-notice" role="alert">{drawingError}</p>}
          </>}
        </div>
      )}
      {canEdit&&current&&!drawingHole&&<div className="scene-inline-actions scene-gap-controls">
        <label>Boundary<select aria-label="Outline boundary" value={activeBoundary} onChange={e=>{setBoundary(Number(e.target.value));setPoint(null);}}>
          <option value={-1}>Outer outline</option>
          {current.holes?.map((_,i)=><option key={i} value={i}>Interior gap {i+1}</option>)}
        </select></label>
        <button className="review-button" disabled={disabled||!currentRing||currentRing.length<3} title="Smallest rectangle around the outline; for L-shaped roofs use the squared candidate. The model outline stays in the saved history." onClick={()=>{if(currentRing)updateBoundary(minAreaRectRing(currentRing as RoofRing) as ScenePoint[]);}}>Square up outline</button>
        <button className="review-button" disabled={(current.holes?.length||0)>=32} onClick={()=>{setDraw([]);setDrawingHole(true);setPoint(null);setDrawingError("");onDrawingChange(true);}}>Add interior gap</button>
        <button className="review-button" disabled={activeBoundary<0} onClick={()=>{
          onChange({...current,holes:current.holes!.filter((_,i)=>i!==activeBoundary),status:"needs_adjustment",geometry_reviewed:false});setBoundary(-1);setPoint(null);
        }}>Fill selected gap</button>
        <p className="review-muted">Keep real courtyards and visible gaps open. Fill a detector gap only after checking the image. Both changes need a new outline review.</p>
        {/* Gaps an outline edit left illegal are dropped on save: a notice (not a blocking alert) names each one and why before
            the save, and the alert names only what is left to fix. */}
        {(()=>{const check=checkOutlineWithGapRepair(current.ring,current.holes,scene.image_width,scene.image_height);return <>
          {check.issues.length>0&&<p className="review-notice" role="alert">{check.issues[0]}</p>}
          {check.dropped.length>0&&!check.issues.length&&<p className="review-notice" role="status">{droppedGapsNotice(check.dropped,{pending:true})}</p>}
        </>;})()}
      </div>}
      {editable && current && (
        <div className="scene-inline-actions">
          <label>
            Outline point
            <select
              aria-label="Select outline point"
              value={point ?? ""}
              onChange={(e) =>
                setPoint(e.target.value === "" ? null : Number(e.target.value))
              }
            >
              <option value="">Choose a point</option>
              {currentRing!.map((_, i) => (
                <option value={i} key={i}>
                  Point {i + 1}
                </option>
              ))}
            </select>
          </label>
          {point !== null && currentRing![point] && (
            <>
              <label>
                Image X
                <MetricInput
                  key={`x:${current.id}:${activeBoundary}:${point}`}
                  aria-label="Outline point X"
                  min="0"
                  max={scene.image_width}
                  value={currentRing![point][0]}
                  onValueChange={(value) =>
                    previewPoint(point, [value, currentRing![point][1]])
                  }
                  onBlur={commitPreview}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); } }}
                />
              </label>
              <label>
                Image Y
                <MetricInput
                  key={`y:${current.id}:${activeBoundary}:${point}`}
                  aria-label="Outline point Y"
                  min="0"
                  max={scene.image_height}
                  value={currentRing![point][1]}
                  onValueChange={(value) =>
                    previewPoint(point, [currentRing![point][0], value])
                  }
                  onBlur={commitPreview}
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); } }}
                />
              </label>
              <button
                className="review-button"
                disabled={currentRing!.length>=200}
                onClick={() => {
                  const a = currentRing![point],
                    b = currentRing![(point + 1) % currentRing!.length];
                  updateBoundary([
                      ...currentRing!.slice(0, point + 1),
                      [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2],
                      ...currentRing!.slice(point + 1),
                    ]);
                }}
              >
                Insert point
              </button>
              <button
                className="review-button"
                disabled={currentRing!.length <= 3}
                onClick={() => {
                  updateBoundary(currentRing!.filter((_,i)=>i!==point));
                  setPoint(null);
                }}
              >
                Remove point
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
