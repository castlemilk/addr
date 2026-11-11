"use client";

import { useMemo, useRef, useState } from "react";
import MetricInput from "./MetricInput";
import { useEditorHistoryShortcuts } from "./useEditorHistoryShortcuts";
import { annotationIssues, pointInRoof, pointOnBoundary, withRoofPointHeight, type RoofAnnotation, type RoofObject } from "@/lib/roof-annotations";

interface Props {
  imageUrl?: string; ring: [number,number][]; pixels: [number,number][]; holes?: [number,number][][];
  model: RoofAnnotation; value: RoofAnnotation; objects: RoofObject[];
  mode: "model" | "edited" | "overlay"; disabled?: boolean;
  onChange: (annotation: RoofAnnotation) => void;
  onObjectsChange: (objects: RoofObject[]) => void;
  showObjects?: boolean; comparison?: RoofAnnotation | null; baselineName?: "model" | "proposal"; focusRoofInitially?: boolean;
}
const clone = <T,>(v:T):T => structuredClone(v);
const colors:Record<string,string>={ridge:"#80ed9b",hip:"#67d7e7",valley:"#c084fc",eave:"#fbbf24",step_eave:"#fbbf24"};

export default function RoofAnnotationEditor({imageUrl,ring,pixels,holes=[],model,value,objects,mode,disabled,onChange,onObjectsChange,showObjects=true,comparison,baselineName="model",focusRoofInitially=false}:Props) {
  const svg=useRef<SVGSVGElement>(null), drag=useRef<{index:number;object?:number}|null>(null);
  const [selected,setSelected]=useState<number|null>(null),[selectedObject,setSelectedObject]=useState<number|null>(null);
  const [moving,setMoving]=useState<RoofAnnotation|null>(null),[movingObjects,setMovingObjects]=useState<RoofObject[]|null>(null);
  const [drawing,setDrawing]=useState<[number,number][]|null>(null),[objectKind,setObjectKind]=useState<RoofObject["kind"]>("solar_panel");
  const [undo,setUndo]=useState<RoofAnnotation[]>([]),[redo,setRedo]=useState<RoofAnnotation[]>([]);
  const [hint,setHint]=useState("");
  const [focused,setFocused]=useState(focusRoofInitially);
  const crop=useMemo(()=>{
    const xs=pixels.map(p=>p[0]),ys=pixels.map(p=>p[1]);
    const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
    const span=Math.max(x1-x0,y1-y0),size=Math.min(512,Math.max(32,span+2*Math.max(8,span*.15)));
    return [Math.max(0,Math.min(512-size,(x0+x1-size)/2)),Math.max(0,Math.min(512-size,(y0+y1-size)/2)),size,size];
  },[pixels]);
  const viewBox=focused?crop:[0,0,512,512],markScale=viewBox[2]/512;
  const current=moving||value,currentObjects=movingObjects||objects,editable=mode!=="model"&&!disabled;
  const baseline=comparison===undefined?model:comparison;
  const transform=useMemo(()=>[0,1].map(axis=>{
    const n=Math.min(ring.length,pixels.length),a=ring.slice(0,n).reduce((s,p)=>s+p[axis],0)/n,b=pixels.slice(0,n).reduce((s,p)=>s+p[axis],0)/n;
    const variance=ring.slice(0,n).reduce((s,p)=>s+(p[axis]-a)**2,0);
    const scale=ring.slice(0,n).reduce((s,p,i)=>s+(p[axis]-a)*(pixels[i][axis]-b),0)/variance;
    return {scale,offset:b-a*scale};
  }),[ring,pixels]);
  const aligned=transform.every(t=>Number.isFinite(t.scale)&&Math.abs(t.scale)>1e-8);
  const historyReady=editable&&aligned&&!moving&&!movingObjects&&!drawing;
  const undoEdit=()=>{const previous=undo.at(-1);if(!previous||drag.current)return;setUndo(undo.slice(0,-1));setRedo([...redo,value]);onChange(previous);};
  const redoEdit=()=>{const next=redo.at(-1);if(!next||drag.current)return;setRedo(redo.slice(0,-1));setUndo([...undo,value]);onChange(next);};
  useEditorHistoryShortcuts(mode!=="model"&&aligned,historyReady,undoEdit,redoEdit);
  const project=(p:number[])=>p.slice(0,2).map((v,i)=>v*transform[i].scale+transform[i].offset);
  const points=(xy:number[][])=>xy.map(p=>project(p).join(",")).join(" ");
  const coordinate=(event:React.PointerEvent<SVGSVGElement>)=>{
    const matrix=svg.current?.getScreenCTM();if(!matrix)return null;
    const p=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
    return [(p.x-transform[0].offset)/transform[0].scale,(p.y-transform[1].offset)/transform[1].scale] as [number,number];
  };
  const commit=(next:RoofAnnotation)=>{setUndo(old=>[...old,value]);setRedo([]);onChange(next);};
  const movePoint=(index:number,point:[number,number])=>{
    const original=model.vertices[index];
    if([ring,...holes].some(r=>r.some(p=>Math.hypot(p[0]-original[0],p[1]-original[1])<.01))) return null;
    const boundary=[ring,...holes].find(r=>pointOnBoundary(original,r));
    if(boundary) {
      const projected=boundary.map((a,i)=>{const b=boundary[(i+1)%boundary.length],dx=b[0]-a[0],dy=b[1]-a[1],t=Math.max(0,Math.min(1,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy)));return [a[0]+t*dx,a[1]+t*dy] as [number,number];});
      point=projected.sort((a,b)=>Math.hypot(a[0]-point[0],a[1]-point[1])-Math.hypot(b[0]-point[0],b[1]-point[1]))[0];
    }
    if(!pointInRoof(point,ring,holes))return null;
    const next=clone(current);next.vertices[index]=[point[0],point[1],next.vertices[index][2]];return next;
  };
  const issues=annotationIssues(current,ring,holes);
  const active=selected===null?null:current.vertices[selected];
  const renderEdges=(annotation:RoofAnnotation,baseline=false)=>annotation.edges.map((edge,i)=>{
    const a=project(annotation.vertices[edge.a]),b=project(annotation.vertices[edge.b]);
    return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={baseline?"#fbbf24":colors[edge.kind]||"white"} strokeWidth={(baseline?1.5:2)*markScale} strokeDasharray={baseline?`${4*markScale} ${4*markScale}`:undefined} opacity={baseline ? 0.65 : 1}/>;
  });
  if(!aligned)return <p className="review-notice">A georeferenced image is required before editing roof points.</p>;
  return <div className="roof-editor">
    <div className="roof-editor-toolbar">
      <strong>{mode==="model"?"Original model prediction":"Edit roof geometry"}</strong>
      <button className="review-button" title="Undo · Ctrl/Cmd+Z" aria-keyshortcuts="Control+z Meta+z" disabled={!historyReady||!undo.length} onClick={undoEdit}>Undo</button>
      <button className="review-button" title="Redo · Ctrl/Cmd+X or Ctrl/Cmd+Shift+Z" aria-keyshortcuts="Control+x Meta+x Control+Shift+z Meta+Shift+z" disabled={!historyReady||!redo.length} onClick={redoEdit}>Redo</button>
      <button className="review-button" disabled={!editable} onClick={()=>{commit(clone(model));setSelected(null);}}>Reset to {baselineName}</button>
      <button className="review-button" aria-pressed={focused} onClick={()=>setFocused(!focused)}>{focused?"Whole image":"Focus roof"}</button>
    </div>
    <svg ref={svg} viewBox={viewBox.join(" ")} role="img" aria-label="Editable roof points and object outlines" className="roof-editor-image"
      onPointerMove={event=>{if(!drag.current||!editable)return;const p=coordinate(event);if(!p)return;
        if(drag.current.object!==undefined){const next=clone(currentObjects);if(pointInRoof(p,ring,holes)){next[drag.current.object].ring_meters[drag.current.index]=p;setMovingObjects(next);}}
        else {const next=movePoint(drag.current.index,p);if(next)setMoving(next);}}}
      onPointerUp={event=>{if(drag.current){if(moving)commit(moving);if(movingObjects)onObjectsChange(movingObjects);drag.current=null;setMoving(null);setMovingObjects(null);svg.current?.releasePointerCapture(event.pointerId);return;}
        if(drawing&&editable){const p=coordinate(event);if(p&&pointInRoof(p,ring,holes))setDrawing([...drawing,p]);else setHint("Place object vertices on the roof.");}}}
      onPointerCancel={()=>{drag.current=null;setMoving(null);setMovingObjects(null);}}>
      {imageUrl&&<image href={imageUrl} width="512" height="512"/>}
      <polygon points={points(ring)} fill="none" stroke="#67d7e7" strokeWidth={markScale} strokeDasharray={`${4*markScale} ${4*markScale}`}/>
      {holes.map((hole,i)=><polygon key={i} aria-label={`Interior gap ${i+1}`} points={points(hole)} fill="none" stroke="#fbbf24" strokeWidth={markScale} strokeDasharray={`${4*markScale} ${4*markScale}`}/>)}
      {mode==="overlay"&&baseline&&renderEdges(baseline,true)}
      {renderEdges(mode==="model"?model:current)}
      {currentObjects.filter(o=>o.status!=="rejected").map(o=><polygon key={o.id} points={points(o.ring_meters)} fill="rgba(59,130,246,.22)" stroke={o.status==="confirmed"?"#86efac":"#fbbf24"} strokeWidth={1.5*markScale} strokeDasharray={o.status==="candidate"?`${4*markScale} ${3*markScale}`:undefined}/>)}
      {editable&&!drawing&&current.vertices.map((p,i)=>{const xy=project(p);return <circle key={i} cx={xy[0]} cy={xy[1]} r={(selected===i?4.5:3)*markScale} fill={selected===i?"#fff":"#67d7e7"} stroke="#101827" strokeWidth={markScale} tabIndex={0} role="button" aria-label={`Roof point ${i+1}`} onFocus={()=>setSelected(i)}
        onPointerDown={event=>{event.stopPropagation();setSelected(i);setSelectedObject(null);drag.current={index:i};svg.current?.setPointerCapture(event.pointerId);}}
        onKeyDown={event=>{const delta:Record<string,[number,number]>={ArrowLeft:[-.1,0],ArrowRight:[.1,0],ArrowUp:[0,.1],ArrowDown:[0,-.1]};if(delta[event.key]){event.preventDefault();const d=delta[event.key],next=movePoint(i,[p[0]+d[0],p[1]+d[1]]);if(next)commit(next);}}}><title>Point {i+1}: {p[2].toFixed(2)} m</title></circle>;})}
      {editable&&selectedObject!==null&&currentObjects[selectedObject]?.ring_meters.map((p,i)=>{const xy=project(p);return <circle key={`o${i}`} cx={xy[0]} cy={xy[1]} r={4*markScale} fill="#fbbf24" onPointerDown={event=>{event.stopPropagation();drag.current={index:i,object:selectedObject};svg.current?.setPointerCapture(event.pointerId);}}/>;})}
      {drawing&&<polyline points={points(drawing)} fill="rgba(59,130,246,.15)" stroke="#fbbf24" strokeWidth={2*markScale}/>}
    </svg>
    <div className="roof-editor-controls">
      <label>Roof form<select disabled={!editable} value={current.roof_type} onChange={event=>commit({...current,roof_type:event.target.value as RoofAnnotation["roof_type"]})}>{["gabled","hipped","flat","mixed"].map(kind=><option key={kind}>{kind}</option>)}</select></label>
      <label>Ridge / apex point<select aria-label="Select roof point" value={selected??""} onChange={event=>setSelected(event.target.value===""?null:Number(event.target.value))}><option value="">Select a point…</option>{current.vertices.map((p,i)=><option key={i} value={i}>Point {i+1} · {p[2].toFixed(2)} m</option>)}</select></label>
      <label>Apex height (m){active ? <MetricInput key={`apex:${selected}`} aria-label="Apex height (m)" step="0.1" min="0" max="100" disabled={!editable} value={active[2]} onValueChange={height=>{
        if(selected===null)return;const next=withRoofPointHeight(current,selected,height);if(next!==current)commit(next);
      }}/> : <input type="number" aria-label="Apex height (m)" disabled value="" readOnly />}</label>
    </div>
    <p className="review-field-hint">Drag a ridge point, or select it and use arrow keys. Connected horizontal ridge heights change together. Outline corners stay fixed. Roof form is a label; point edits define the shape.</p>
    {issues.length>0&&<p className="review-notice" role="status">Draft geometry: {issues.join("; ")}. You can save feedback, but fix these before approving shape labels.</p>}
    {showObjects&&<><div className="roof-editor-toolbar"><strong>Roof objects</strong><select aria-label="Object class" disabled={!editable} value={objectKind} onChange={event=>setObjectKind(event.target.value as RoofObject["kind"])}><option value="solar_panel">Solar array</option><option value="skylight">Skylight</option><option value="chimney">Chimney</option></select>
      <button className="review-button" disabled={!editable} onClick={()=>{setDrawing(drawing?null:[]);setHint("");}}>{drawing?"Cancel drawing":"Draw object"}</button>
      {drawing&&<button className="review-button" disabled={drawing.length<3} onClick={()=>{onObjectsChange([...objects,{id:crypto.randomUUID(),kind:objectKind,ring_meters:drawing,status:"confirmed",source:"reviewer"}]);setDrawing(null);}}>Finish polygon</button>}
    </div>
    {drawing&&<p className="review-field-hint">Click at least three corners, then finish the polygon.</p>}{hint&&<p role="status">{hint}</p>}
    <ul className="roof-object-list">{currentObjects.map((object,i)=><li key={object.id}><span>{object.kind.replaceAll("_"," ")} · {object.source}{object.score!==undefined?` · ${Math.round(object.score*100)}% detector score`:""}</span><select aria-label={`Object ${i+1} review`} disabled={!editable} value={object.status} onChange={event=>onObjectsChange(objects.map((o,j)=>j===i?{...o,status:event.target.value as RoofObject["status"]}:o))}><option value="candidate">Needs review</option><option value="confirmed">Confirmed</option><option value="rejected">False positive</option></select><button className="review-button" disabled={!editable} onClick={()=>setSelectedObject(selectedObject===i?null:i)}>Edit outline</button></li>)}</ul>
    {!objects.length&&<p className="review-field-hint">No object annotations yet. Draw missed objects; mark the object review complete only after checking the image.</p>}</>}
  </div>;
}
