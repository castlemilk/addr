"use client";
import { useEffect, useRef, useState } from "react";
import MetricInput from "@/components/reviews/MetricInput";
import { getCrmToken } from "@/lib/crm-client";
import { MAX_TRACE_SOURCES, type InstanceOperation } from "@/lib/scene-instances";
import { CLASS_COLORS, polygonIssues, scenePathData, type SceneData, type SceneObject, type ScenePoint, type SceneReview } from "@/lib/scene-types";

export default function SceneInstanceEditor({data,review,object,onApply,onClose,onPending,onDrawingChange,disabled=false}: {
  data: SceneData; review: SceneReview; object: SceneObject;
  onApply: (review: SceneReview,selected:string)=>void; onClose:()=>void; onPending:(pending:boolean)=>void;
  onDrawingChange:(drawing:boolean)=>void;
  disabled?: boolean;
}) {
  const [operation,setOperation]=useState<InstanceOperation>("split"), [targets,setTargets]=useState<string[]>([]);
  const [traceStep,setTraceStep]=useState<"select"|"draw">("select"),[outline,setOutline]=useState<ScenePoint[]>([]);
  const [corner,setCorner]=useState<ScenePoint>([0,0]);
  const [cut,setCut]=useState<ScenePoint[]>(()=>{
    const xs=object.ring.map(p=>p[0]),ys=object.ring.map(p=>p[1]),x=(Math.min(...xs)+Math.max(...xs))/2;
    return [[x,Math.max(0,Math.min(...ys)-8)],[x,Math.min(data.model.image_height,Math.max(...ys)+8)]];
  });
  const [endpoint,setEndpoint]=useState(0),[focused,setFocused]=useState(true),[pending,setPending]=useState(false),[error,setError]=useState("");
  const locked=pending||disabled;
  const svg=useRef<SVGSVGElement>(null),host=useRef<HTMLElement>(null),request=useRef<AbortController|null>(null);
  useEffect(()=>{host.current?.scrollIntoView({block:"start"});return()=>{
    const active=request.current;
    if(active){request.current=null;active.abort();onPending(false);}
  };},[onPending]);
  useEffect(()=>()=>onDrawingChange(false),[onDrawingChange]);
  const choices=review.objects.filter(o=>o.id!==object.id && o.kind===object.kind && !["rejected","superseded"].includes(o.status));
  const selected=new Set([object.id,...(operation!=="split"?targets:[])]);
  const outlines=review.objects.filter(o=>selected.has(o.id));
  const points=outlines.flatMap(o=>o.ring),xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
  const width=data.model.image_width,height=data.model.image_height;
  const size=Math.min(Math.max(width,height),Math.max(60,Math.max(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys))*1.5));
  const crop=[Math.max(0,Math.min(width-size,(Math.min(...xs)+Math.max(...xs)-size)/2)),Math.max(0,Math.min(height-size,(Math.min(...ys)+Math.max(...ys)-size)/2)),size,size];
  const limit=operation==="trace"?MAX_TRACE_SOURCES:8;
  const toggle=(id:string)=>{setTargets(old=>old.includes(id)?old.filter(x=>x!==id):old.length<limit-1?[...old,id]:old);setError("");};
  const updateOutline=(points:ScenePoint[])=>{setOutline(points);onDrawingChange(points.length>0);setError("");};
  const addCorner=(point:ScenePoint)=>{
    if(outline.length>=200){setError("Use at most 200 outline corners.");return;}
    if(point.some((v,i)=>!Number.isFinite(v)||v<0||v>(i===0?width:height))){setError("Keep the corner inside the image.");return;}
    updateOutline([...outline,point]);
  };
  const switchOperation=(next:InstanceOperation)=>{
    setOperation(next);setError("");setTargets(old=>next==="merge"?old.slice(0,7):old);
  };
  const groupChoices=object.group_id?choices.filter(o=>o.group_id===object.group_id):[];
  const outlineIssue=outline.length>=3?polygonIssues(outline,width,height)[0]:null;
  const submit=async()=>{
    if(request.current || disabled)return;
    if(operation==="trace") {
      const issue=polygonIssues(outline,width,height)[0];
      if(issue){setError(issue);return;}
    }
    const controller=new AbortController();request.current=controller;setPending(true);onPending(true);setError("");
    try {
      const token=getCrmToken();
      const response=await fetch("/api/scenes/instances",{method:"POST",signal:controller.signal,
        headers:{"Content-Type":"application/json",...(token?{Authorization:`Bearer ${token}`}:{})},
        body:JSON.stringify({id:data.model.id,review,operation,ids:[...selected],cut,outline:{ring:outline}})});
      const result=await response.json();if(!response.ok)throw new Error(result.error);
      if(!controller.signal.aborted)onApply(result.review,result.selected);
    } catch(e) {if(!controller.signal.aborted)setError(e instanceof Error?e.message:"Could not replace these outlines.");}
    finally{if(request.current===controller){request.current=null;setPending(false);onPending(false);}}
  };
  return <section className="scene-instance-editor roof-editor" ref={host} aria-label="Correct object outlines">
    <div className="roof-editor-toolbar"><strong>Correct outlines</strong>
      <button className="review-button" disabled={locked||outline.length>0} aria-pressed={operation==="split"} onClick={()=>switchOperation("split")}>Split</button>
      <button className="review-button" disabled={locked||outline.length>0} aria-pressed={operation==="merge"} onClick={()=>switchOperation("merge")}>Merge</button>
      <button className="review-button" disabled={locked} aria-pressed={operation==="trace"} onClick={()=>switchOperation("trace")}>Trace replacement</button>
      <button className="review-button" onClick={()=>setFocused(!focused)}>{focused?"Whole image":"Focus selected outlines"}</button>
      <button className="review-button" disabled={pending} onClick={onClose}>{outline.length?"Cancel trace":"Close"}</button>
    </div>
    <p className="review-muted">{operation==="split"?"Place a cut across the outline. Click to move each endpoint, or enter coordinates below.":operation==="merge"?"Select touching outlines of the same class. Separate objects can share a group without merging their shapes.":traceStep==="select"?"Select the fragments of one object. Then trace its visible outer boundary, including any parts the detector missed.":"Click around the visible outer boundary, or add corner coordinates below. The last corner joins the first. Check interior gaps in Edit outline after creating the draft."}</p>
    {operation==="trace"&&<div className="scene-trace-steps" aria-label="Replacement tracing steps">
      <button className="review-button" disabled={locked} aria-pressed={traceStep==="select"} onClick={()=>setTraceStep("select")}>1. Select fragments · {selected.size}</button>
      <button className="review-button" disabled={locked} aria-pressed={traceStep==="draw"} onClick={()=>setTraceStep("draw")}>2. Draw replacement outline</button>
    </div>}
    <svg ref={svg} role="img" aria-label="Outline replacement preview" className="roof-editor-image" viewBox={focused?crop.join(" "):`0 0 ${width} ${height}`}
      onPointerDown={e=>{
        if(locked || e.button!==0 || !e.isPrimary)return;
        if(operation==="merge"||(operation==="trace"&&traceStep==="select")) {
          const id=(e.target as Element).closest("[data-instance-id]")?.getAttribute("data-instance-id");
          if(id && choices.some(o=>o.id===id))toggle(id);
        } else {
          const matrix=svg.current?.getScreenCTM();if(!matrix)return;
          const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(matrix.inverse());
          const point:ScenePoint=[Math.max(0,Math.min(width,p.x)),Math.max(0,Math.min(height,p.y))];
          if(operation==="trace")addCorner(point);
          else {setCut(old=>old.map((oldPoint,i)=>i===endpoint?point:oldPoint));setEndpoint(1-endpoint);}
        }
      }}>
      <image href={data.image_url} width={width} height={height}/>
      {[object,...choices].map(o=><path key={o.id} data-instance-id={o.id} d={scenePathData(o)} fillRule="evenodd"
        fill={selected.has(o.id)?`${CLASS_COLORS[o.kind]}38`:"transparent"} stroke={selected.has(o.id)?"#67d7e7":"#f4cc89"}
        opacity={selected.has(o.id)?1:.65} strokeWidth={selected.has(o.id)?2:1} vectorEffect="non-scaling-stroke"/>) }
      {operation==="split"&&<line x1={cut[0][0]} y1={cut[0][1]} x2={cut[1][0]} y2={cut[1][1]} stroke="#fff" strokeWidth="2" strokeDasharray="5 3" vectorEffect="non-scaling-stroke" pointerEvents="none"/>}
      {operation==="trace"&&outline.length>0&&<g pointerEvents="none" aria-label={`Traced outline with ${outline.length} corners`}>
        <polyline points={[...outline,...(outline.length>=3?[outline[0]]:[])].map(p=>p.join(",")).join(" ")} fill={outline.length>=3?"#ffffff18":"none"} stroke="#fff" strokeWidth="2" vectorEffect="non-scaling-stroke"/>
        {outline.map((p,i)=><circle key={i} cx={p[0]} cy={p[1]} r={(focused?size:width)/180} fill="#fff"/>)}
      </g>}
    </svg>
    <fieldset disabled={locked} className="scene-instance-controls">
      {operation==="split"?<><label>Next image click<select aria-label="Cut endpoint" value={endpoint} onChange={e=>setEndpoint(Number(e.target.value))}><option value="0">Start of cut</option><option value="1">End of cut</option></select></label>
        <div className="scene-dimensions">{cut.map((p,i)=><div key={i}>{[0,1].map(axis=><label key={axis}>{i===0?"Start":"End"} {axis===0?"X":"Y"}<MetricInput aria-label={`Cut ${i===0?"start":"end"} ${axis===0?"X":"Y"}`} value={p[axis]} min="0" max={axis===0?width:height}
          onValueChange={value=>setCut(old=>old.map((point,j)=>j===i?point.map((v,a)=>a===axis?value:v) as ScenePoint:point))}/></label>)}</div>)}</div></>:
        operation==="trace"&&traceStep==="draw"?<>
          <p role="status">{selected.size} source outline{selected.size===1?"":"s"} · {outline.length} of 200 corners</p>
          <div className="scene-trace-coordinates">{[0,1].map(axis=><label key={axis}>New corner {axis===0?"X":"Y"}<MetricInput aria-label={`Replacement corner ${axis===0?"X":"Y"}`} value={corner[axis]} min="0" max={axis===0?width:height} onValueChange={value=>setCorner(old=>old.map((v,i)=>i===axis?value:v) as ScenePoint)}/></label>)}</div>
          <div className="scene-trace-actions">
            <button className="review-button" disabled={outline.length>=200} onClick={()=>addCorner(corner)}>Add outline corner</button>
            <button className="review-button" disabled={!outline.length} onClick={()=>updateOutline(outline.slice(0,-1))}>Undo last corner</button>
            <button className="review-button" disabled={!outline.length} onClick={()=>updateOutline([])}>Clear drawing</button>
          </div>
          {outlineIssue&&<p className="review-notice" role="alert">{outlineIssue}</p>}
        </>:<>
          <p>{selected.size} of {limit} selected · {object.label} is included.</p>
          {operation==="trace"&&groupChoices.length>0&&<button className="review-button" disabled={groupChoices.length>=limit} onClick={()=>{setTargets(groupChoices.map(o=>o.id));setError("");}}>Select this group · {groupChoices.length+1}</button>}
          <div className="scene-merge-choices">{choices.length?choices.map(o=><label className="review-checkbox" key={o.id}>
            <input type="checkbox" checked={targets.includes(o.id)} disabled={targets.length>=limit-1&&!targets.includes(o.id)} onChange={()=>toggle(o.id)}/>{o.label} · {o.id.slice(-4)}
          </label>):<p>No other active outlines in this class.</p>}</div>
        </>}
      {error&&<p className="review-notice" role="alert">{error}</p>}
      {(operation!=="trace"||traceStep==="draw")&&<button className="review-button review-primary" disabled={(operation==="merge"&&!targets.length)||(operation==="trace"&&(outline.length<3||!!outlineIssue))} onClick={()=>void submit()}>{pending?"Updating outlines…":operation==="split"?"Split outline":operation==="merge"?"Merge selected outlines":"Create replacement draft"}</button>}
      <p className="review-muted">Creates a draft for review. Original outlines stay in history; replaced detections are not false positives. Save scene keeps the change.</p>
    </fieldset>
  </section>;
}
