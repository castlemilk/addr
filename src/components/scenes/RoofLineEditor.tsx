"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Check, CornerDownRight, Maximize, MousePointer2, Pencil, Redo2, Save, Sparkles, Trash2, Undo2 } from "lucide-react";
import { transformPoint, type SceneModelSnapshot, type SceneObject, type ScenePoint } from "@/lib/scene-types";
import { connectRoofLines, currentRoofLineEdits, initialRoofLineEdits, LINE_KINDS, reanchorRoofLineEdits, roofLineGraphIssue, type RoofFeedbackLineKind, type RoofLineEdits } from "@/lib/roof-line-edits";
import { ROOF_LINE_COLORS } from "@/lib/scene-roof-lines";
import { useEditorHistoryShortcuts } from "@/components/reviews/useEditorHistoryShortcuts";

interface Props {
  object: SceneObject; scene: SceneModelSnapshot; image: string; disabled: boolean;
  onChange: (value: RoofLineEdits) => void; onPending: (value: boolean) => void;
  onSave: () => void; dirty: boolean; saving: boolean;
  revision: number; onClose: () => void; onApex: () => void; hasApexes: boolean;
  /** Optional so frozen source snapshots that mount this editor keep compiling; the live page always passes it. */
  onEditOutline?: () => void;
  onEnhance: () => void; enhanced: boolean; enhancing: boolean; imageError: string;
}
/** Edge indices are sorted and deduplicated; a point selection never carries edges. */
type Selection = { edges: number[]; point?: number };
type Rect = [number, number, number, number];
const labels = {ridge:"Ridge / apex",hip:"Hip",valley:"Valley / saddle"};
const descriptions = {ridge:"The high meeting line between roof slopes.",hip:"A sloping outside meeting line, usually running down to an eave corner.",valley:"An inward meeting line where water runs between roof slopes."};
const sortedUnique=(list:number[])=>[...new Set(list)].sort((a,b)=>a-b);
const toggled=(sel:Selection,i:number):Selection=>({edges:sel.edges.includes(i)?sel.edges.filter(x=>x!==i):sortedUnique([...sel.edges,i])});
const rectOf=(a:ScenePoint,b:ScenePoint):Rect=>[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(b[0]-a[0]),Math.abs(b[1]-a[1])];
export default function RoofLineEditor({object,scene,image,disabled,onChange,onPending,onSave,dirty,saving,revision,onClose,onApex,hasApexes,onEditOutline,onEnhance,enhanced,enhancing,imageError}:Props) {
  const seed=useMemo(()=>initialRoofLineEdits(object),[object]);
  const stored=currentRoofLineEdits(object),value=stored||seed;
  const svg=useRef<SVGSVGElement>(null);
  const drag=useRef<{start:ScenePoint; original:RoofLineEdits; indices:number[]; moved:boolean}|null>(null);
  // Box selection in image pixels; it never mutates the graph, so the page stays idle (no onPending) while it is drawn.
  const marquee=useRef<{start:ScenePoint;current:ScenePoint;additive:boolean;moved:boolean}|null>(null);
  // A pointer press selects through `begin`; the focus the browser gives the handle right after must not replace that selection.
  const pointerSelect=useRef(false);
  const latest=useRef<RoofLineEdits|null>(null);
  const [dragging,setDragging]=useState(false);
  const [marqueeRect,setMarqueeRect]=useState<Rect|null>(null);
  const [preview,setPreview]=useState<RoofLineEdits|null>(null),[tool,setTool]=useState<"move"|"draw">("move");
  const [kind,setKind]=useState<RoofFeedbackLineKind>("ridge"),[start,setStart]=useState<ScenePoint|null>(null),[cursor,setCursor]=useState<ScenePoint|null>(null);
  const [selected,setSelected]=useState<Selection>({edges:[]});
  const [past,setPast]=useState<RoofLineEdits[]>([]),[future,setFuture]=useState<RoofLineEdits[]>([]);
  const [error,setError]=useState(""),[compare,setCompare]=useState(false),[focused,setFocused]=useState(true);
  const [showEdits,setShowEdits]=useState(true),[showBoundary,setShowBoundary]=useState(true);
  const [visibleKinds,setVisibleKinds]=useState<Set<RoofFeedbackLineKind>>(()=>new Set(LINE_KINDS));
  useEffect(()=>()=>onPending(false),[onPending]);
  const current=preview||value;
  const ring=object.ring.map(p=>transformPoint(p,scene.pixel_to_meters)),holes=(object.holes||[]).map(h=>h.map(p=>transformPoint(p,scene.pixel_to_meters)));
  const crop=useMemo(()=>{
    const xs=object.ring.map(p=>p[0]),ys=object.ring.map(p=>p[1]),left=Math.min(...xs),right=Math.max(...xs),top=Math.min(...ys),bottom=Math.max(...ys);
    const padding=Math.max(10,Math.max(right-left,bottom-top)*.12);
    return [Math.max(0,left-padding),Math.max(0,top-padding),Math.min(scene.image_width,right+padding)-Math.max(0,left-padding),Math.min(scene.image_height,bottom+padding)-Math.max(0,top-padding)];
  },[object.ring,scene.image_width,scene.image_height]);
  const box=focused?crop:[0,0,scene.image_width,scene.image_height],mark=Math.max(box[2],box[3])/512;
  const cutByFrame=useMemo(()=>object.ring.some(([x,y])=>x<=1||y<=1||x>=scene.image_width-2||y>=scene.image_height-2),[object.ring,scene.image_width,scene.image_height]);
  const project=(point:ScenePoint)=>transformPoint(point,scene.meters_to_pixel);
  const pixelPosition=(event:React.PointerEvent):ScenePoint|null=>{
    const matrix=svg.current?.getScreenCTM();if(!matrix)return null;
    const p=new DOMPoint(event.clientX,event.clientY).matrixTransform(matrix.inverse());
    return [p.x,p.y];
  };
  const position=(event:React.PointerEvent)=>{const px=pixelPosition(event);return px?transformPoint(px,scene.pixel_to_meters):null;};
  const indicesOf=(sel:Selection)=>sel.point!==undefined?[sel.point]:sortedUnique(sel.edges.flatMap(i=>value.edges[i]?[value.edges[i].a,value.edges[i].b]:[]));
  const snap=(point:ScenePoint,excluded:number[]=[])=>{
    const pixel=project(point),candidates=current.vertices.filter((_,i)=>!excluded.includes(i)&&current.edges.some(e=>e.a===i||e.b===i));
    for(const boundary of [ring,...holes])for(let i=0;i<boundary.length;i++){
      const a=boundary[i],b=boundary[(i+1)%boundary.length],dx=b[0]-a[0],dy=b[1]-a[1],t=Math.max(0,Math.min(1,((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy)));
      candidates.push([a[0]+t*dx,a[1]+t*dy]);
    }
    const close=candidates.map(p=>({p,d:Math.hypot(...project(p).map((v,i)=>v-pixel[i]))})).sort((a,b)=>a.d-b.d)[0];
    return close&&close.d<7*mark?close.p:point;
  };
  const clearMarquee=()=>{marquee.current=null;setMarqueeRect(null);};
  const cancel=()=>{drag.current=null;latest.current=null;clearMarquee();setDragging(false);setPreview(null);setStart(null);setCursor(null);onPending(false);};
  /** One accepted commit is one undo step. Returns the stored value, or null when nothing changed or the graph was rejected. */
  const commit=(next:RoofLineEdits,geometry=true):RoofLineEdits|null=>{
    if(disabled||JSON.stringify(next)===JSON.stringify(value))return null;
    const graph=geometry?connectRoofLines(next):next;
    const result={...next,...graph,checked:geometry?false:next.checked};
    const issue=roofLineGraphIssue(result,ring,holes);
    if(issue){setError(issue);return null;}
    setPast(old=>[...old,value].slice(-50));setFuture([]);setError("");setSelected({edges:[]});onChange(result);
    return result;
  };
  const drawPoint=(point:ScenePoint)=>{
    const p=snap(point);
    if(!start){setStart(p);setCursor(p);onPending(true);return;}
    const next=structuredClone(value),a=next.vertices.length;
    next.vertices.push(start,p);next.edges.push({a,b:a+1,kind});commit(next);setStart(null);setCursor(null);onPending(false);
  };
  const begin=(event:React.PointerEvent,selection:Selection)=>{
    event.stopPropagation();if(disabled||!showEdits)return;
    const p=position(event);if(!p)return;
    if(tool==="draw"){drawPoint(p);return;}
    pointerSelect.current=true;window.setTimeout(()=>{pointerSelect.current=false;},0);
    // Shift toggles one line in or out of the selection: no drag, no capture. Shift on a point is a plain press.
    if(event.shiftKey&&selection.edges.length===1){setSelected(s=>toggled(s,selection.edges[0]));return;}
    // A plain press on a line already inside a multi-selection keeps it and drags every selected endpoint together.
    const next=selection.edges.length===1&&selected.edges.length>1&&selected.edges.includes(selection.edges[0])?selected:selection;
    setSelected(next);setError("");
    drag.current={start:p,original:structuredClone(value),indices:indicesOf(next),moved:false};
    setDragging(true);svg.current?.setPointerCapture(event.pointerId);onPending(true);
  };
  const remove=()=>{
    const next=structuredClone(value);
    next.edges=next.edges.filter((e,i)=>!selected.edges.includes(i)&&(selected.point===undefined||(e.a!==selected.point&&e.b!==selected.point)));
    commit(next);
  };
  const nudge=(event:React.KeyboardEvent)=>{
    if(event.ctrlKey||event.metaKey||event.altKey)return;
    const moves:Record<string,ScenePoint>={ArrowLeft:[-.1,0],ArrowRight:[.1,0],ArrowUp:[0,.1],ArrowDown:[0,-.1]};
    if((event.key==="Delete"||event.key==="Backspace")&&!disabled){event.preventDefault();remove();return;}
    const step=moves[event.key];if(!step||disabled)return;event.preventDefault();
    const indices=indicesOf(selected);if(!indices.length)return;
    const next=structuredClone(value);for(const i of indices)next.vertices[i]=[next.vertices[i][0]+step[0],next.vertices[i][1]+step[1]];
    const result=commit(next);if(!result)return;
    // Compaction may renumber points; keep the moved point selected by its new position. Edge indices are stable.
    if(selected.point!==undefined){const moved=next.vertices[selected.point],index=result.vertices.findIndex(p=>Math.hypot(p[0]-moved[0],p[1]-moved[1])<1e-6);setSelected(index>=0?{edges:[],point:index}:{edges:[]});}
    else setSelected(selected);
  };
  const undo=()=>{const previous=past.at(-1);if(!previous)return;cancel();setPast(past.slice(0,-1));setFuture([...future,value]);setSelected({edges:[]});onChange(previous);};
  const redo=()=>{const next=future.at(-1);if(!next)return;cancel();setFuture(future.slice(0,-1));setPast([...past,value].slice(-50));setSelected({edges:[]});onChange(next);};
  const path=(points:ScenePoint[])=>points.map(p=>p.join(",")).join(" ");
  const ready=!disabled&&!saving&&!start&&!dragging;
  useEditorHistoryShortcuts(true,ready,undo,redo);
  const edgeVisible=(edge:{kind:RoofFeedbackLineKind})=>showEdits&&visibleKinds.has(edge.kind);
  const length=(edge:{a:number;b:number})=>Math.hypot(current.vertices[edge.a][0]-current.vertices[edge.b][0],current.vertices[edge.a][1]-current.vertices[edge.b][1]);
  const single=selected.edges.length===1?current.edges[selected.edges[0]]??null:null,multi=selected.edges.length>1;
  const total=selected.edges.reduce((sum,i)=>current.edges[i]?sum+length(current.edges[i]):sum,0);
  const selectEdge=(index:number,additive=false)=>{cancel();setTool("move");setSelected(s=>additive?toggled(s,index):{edges:[index]});setShowEdits(true);setVisibleKinds(old=>new Set([...old,current.edges[index].kind]));};
  const finishMarquee=(event:React.PointerEvent)=>{
    const area=marquee.current;if(!area)return false;
    clearMarquee();svg.current?.releasePointerCapture(event.pointerId);
    if(!area.moved){if(!area.additive)setSelected({edges:[]});return true;}
    // Both projected endpoints must lie inside the box; a line that only crosses it is not selected (Shift-click it).
    const [x,y,w,h]=rectOf(area.start,area.current),inside=(p:ScenePoint)=>p[0]>=x&&p[0]<=x+w&&p[1]>=y&&p[1]<=y+h;
    const hits=current.edges.map((e,i)=>i).filter(i=>{const e=current.edges[i];return edgeVisible(e)&&inside(project(current.vertices[e.a]))&&inside(project(current.vertices[e.b]));});
    setSelected({edges:area.additive?sortedUnique([...selected.edges,...hits]):hits});
    svg.current?.focus({preventScroll:true});
    return true;
  };
  return <section className="roof-line-editor" aria-label="Roof line workspace" onKeyDown={event=>{if(event.key==="Escape"){cancel();setTool("move");}}}>
    <header className="roof-line-heading">
      <div><span className="roof-line-eyebrow">BUILDING / ROOF STRUCTURE</span><h2>{object.label || "Roof lines"}</h2></div>
      <div className="roof-line-heading-state"><span className={`roof-line-state ${stored?.checked?"is-checked":""}`}><span/>{stored?.checked?"Imagery checked":"Draft feedback"}</span><span>{current.edges.length} lines</span></div>
    </header>
    <nav className="roof-line-modes" aria-label="Roof feedback editor">
      <button disabled={!ready} onClick={onClose} aria-label="Close roof editor"><ArrowLeft size={14}/>Back to review</button>
      <button aria-pressed="true" disabled={disabled} onClick={()=>{cancel();setTool("move");}}>Roof lines</button>
      <button disabled={!ready||!hasApexes} onClick={onApex}>3D apexes</button>
      {onEditOutline&&<button data-testid="roof-line-edit-outline" disabled={!ready} title="Opens the outline editor. Your roof line edits stay pending and follow the new outline; lines that fall outside it are removed and can be undone." onClick={onEditOutline}><Pencil size={14}/>Edit outline</button>}
      <span>Trace what you can see. Keep heights separate.</span>
    </nav>
    <div className="roof-line-toolbar">
      <div className="review-view-switch" role="group" aria-label="Roof line tool">
        <button type="button" disabled={disabled} aria-pressed={tool==="move"} onClick={()=>{cancel();setTool("move");setShowEdits(true);}}><MousePointer2 size={15}/>Move lines</button>
        <button type="button" disabled={disabled} aria-pressed={tool==="draw"} onClick={()=>{cancel();setTool("draw");setSelected({edges:[]});setShowEdits(true);setVisibleKinds(old=>new Set([...old,kind]));}}><Pencil size={15}/>Draw roof line</button>
      </div>
      <div className="roof-line-history-tools">
        <button className="review-button" aria-label="Undo line edit" aria-keyshortcuts="Control+z Meta+z" title="Undo · Ctrl/Cmd+Z" disabled={!ready||!past.length} onClick={undo}><Undo2 size={16}/><span>Undo</span></button>
        <button className="review-button" aria-label="Redo line edit" aria-keyshortcuts="Control+x Meta+x Control+Shift+z Meta+Shift+z" title="Redo · Ctrl/Cmd+X or Ctrl/Cmd+Shift+Z" disabled={!ready||!future.length} onClick={redo}><Redo2 size={16}/><span>Redo</span></button>
      </div>
      <div className="roof-line-view-tools">
        <button className="review-button" disabled={!ready} aria-pressed={!focused} onClick={()=>setFocused(!focused)}><Maximize size={15}/>{focused?"Whole image":"Focus roof"}</button>
        <button className="review-button" disabled={!ready||enhancing} aria-pressed={enhanced} aria-label={enhanced?"Show original imagery":"Enhance roof detail"} onClick={onEnhance}><Sparkles size={15}/>{enhancing?"Loading…":enhanced?"Original image":"Enhance detail"}</button>
      </div>
    </div>
    <div className="roof-line-workspace">
      <div className="roof-line-stage">
        <p className="roof-line-instruction" role="status">{start?"Choose the other end · Escape to cancel":tool==="draw"?`Draw ${labels[kind].toLowerCase()} · click two endpoints; nearby points and eaves snap together.`:"Drag a point or line. Drag on empty roof to box-select the lines fully inside the box; Shift-click adds or removes one. Delete removes the selection · arrow keys move 10 cm."}</p>
        {cutByFrame&&<p className="review-notice" role="status">This roof reaches the edge of the saved capture, so part of it is not in the image. Go back to review and use Analyse surroundings to expand the capture before drawing lines beyond the edge.</p>}
    <svg ref={svg} className={`roof-line-image tool-${tool}`} viewBox={box.join(" ")} role="img" aria-label="Editable roof ridge hip and valley lines" tabIndex={-1} data-testid="roof-line-canvas"
      onKeyDown={nudge}
      onPointerDown={event=>{
        if(event.target!==event.currentTarget&&!(event.target instanceof SVGImageElement))return;
        if(!disabled&&showEdits&&tool==="draw"){const p=position(event);if(p)drawPoint(p);return;}
        if(!disabled&&showEdits&&tool==="move"){
          const px=pixelPosition(event);if(!px)return;
          marquee.current={start:px,current:px,additive:event.shiftKey,moved:false};svg.current?.setPointerCapture(event.pointerId);
          return;
        }
        setSelected({edges:[]});
      }}
      onPointerMove={event=>{
        if(disabled)return;
        const area=marquee.current;
        if(area){
          const px=pixelPosition(event);if(!px)return;
          area.current=px;
          if(area.moved||Math.hypot(px[0]-area.start[0],px[1]-area.start[1])>=4*mark){area.moved=true;setMarqueeRect(rectOf(area.start,px));}
          return;
        }
        const p=position(event);if(!p)return;
        if(start)setCursor(snap(p));
        const movement=drag.current;if(!movement)return;
        const target=movement.indices.length===1?snap(p,movement.indices):p,dx=target[0]-movement.start[0],dy=target[1]-movement.start[1];
        if(Math.hypot(dx,dy)<.01&&!movement.moved)return;
        movement.moved=true;const next=structuredClone(movement.original);
        for(const i of movement.indices)next.vertices[i]=[next.vertices[i][0]+dx,next.vertices[i][1]+dy];
        latest.current=next;setPreview(next);
      }}
      onPointerUp={event=>{
        if(finishMarquee(event))return;
        if(!drag.current)return;const next=latest.current;drag.current=null;latest.current=null;setDragging(false);setPreview(null);onPending(false);svg.current?.releasePointerCapture(event.pointerId);if(next)commit(next);
      }}
      onPointerCancel={cancel}>
      <image href={image} width={scene.image_width} height={scene.image_height}/>
      {showBoundary&&[object.ring,...(object.holes||[])].map((boundary,i)=><polygon key={i} points={path(boundary)} fill="none" stroke="#cbd5e1" strokeWidth={mark} strokeDasharray={`${4*mark} ${3*mark}`} pointerEvents="none"/>)}
      {compare&&value.baseline.edges.map((e,i)=>{const a=project(value.baseline.vertices[e.a]),b=project(value.baseline.vertices[e.b]);return <line key={i} x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke="#fbbf24" opacity=".8" strokeWidth={1.5*mark} strokeDasharray={`${4*mark} ${4*mark}`} pointerEvents="none"/>;})}
      {current.edges.map((e,i)=>{if(!edgeVisible(e))return null;const a=project(current.vertices[e.a]),b=project(current.vertices[e.b]),on=selected.edges.includes(i);return <g key={i}>
        <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke={on?"white":ROOF_LINE_COLORS[e.kind]} strokeWidth={(on?3:2)*mark} pointerEvents="none"/>
        <line x1={a[0]} y1={a[1]} x2={b[0]} y2={b[1]} stroke="transparent" strokeWidth={12*mark} pointerEvents={tool==="draw"?"none":undefined} tabIndex={disabled?-1:0} role="button" aria-pressed={on} aria-label={`Roof line ${i+1}: ${labels[e.kind]}`} onPointerDown={event=>begin(event,{edges:[i]})} onFocus={()=>{if(!pointerSelect.current)setSelected({edges:[i]});}}/>
      </g>;})}
      {current.vertices.map((p,i)=>{if(!current.edges.some(e=>edgeVisible(e)&&(e.a===i||e.b===i)))return null;const xy=project(p);return <circle key={i} cx={xy[0]} cy={xy[1]} r={(selected.point===i?4.5:3.5)*mark} fill={selected.point===i?"white":"#67d7e7"} stroke="#101827" strokeWidth={mark} pointerEvents={tool==="draw"?"none":undefined} tabIndex={disabled?-1:0} role="button" aria-label={`Roof line point ${i+1}`} onPointerDown={event=>begin(event,{edges:[],point:i})} onFocus={()=>{if(!pointerSelect.current)setSelected({edges:[],point:i});}}/>;})}
      {start&&cursor&&<line x1={project(start)[0]} y1={project(start)[1]} x2={project(cursor)[0]} y2={project(cursor)[1]} stroke={ROOF_LINE_COLORS[kind]} strokeWidth={2*mark} strokeDasharray={`${3*mark} ${3*mark}`} pointerEvents="none"/>}
      {marqueeRect&&<rect data-testid="roof-line-marquee" className="roof-line-marquee" x={marqueeRect[0]} y={marqueeRect[1]} width={marqueeRect[2]} height={marqueeRect[3]} fill="rgba(255,255,255,.08)" stroke="white" strokeWidth={mark} strokeDasharray={`${3*mark} ${3*mark}`} pointerEvents="none"/>}
    </svg>
        <div className="roof-line-legend" aria-label="Roof line legend">
          {LINE_KINDS.map(type=><span key={type}><i style={{background:ROOF_LINE_COLORS[type]}}/>{labels[type]}</span>)}
          {compare&&<span><i className="is-estimate"/>Starting estimate</span>}
          <span className="roof-line-coordinate-note">Image-registered · metres</span>
        </div>
        {(error||imageError)&&<p className="review-notice" role="alert">{error||imageError}</p>}
        {object.roof_line_edits&&!stored&&<p className="review-notice">Earlier lines belong to a different outline and remain in saved history. New edits will use this outline. <button className="review-button" disabled={!ready} onClick={()=>{const carried=reanchorRoofLineEdits(object,scene.pixel_to_meters).object.roof_line_edits;if(carried)commit(carried);}}>Carry earlier lines onto this outline</button></p>}
      </div>
      <aside className="roof-line-inspector" aria-label="Roof line properties">
        <section className="roof-line-layer-controls">
          <div className="roof-line-section-heading"><h3>Roof layers</h3><span>{current.edges.length} lines</span></div>
          {LINE_KINDS.map(type=><label className="roof-line-layer-row" key={type}>
            <input type="checkbox" aria-label={`Show ${labels[type]} lines`} checked={visibleKinds.has(type)} disabled={!ready} onChange={e=>{setSelected({edges:[]});setVisibleKinds(old=>{const next=new Set(old);if(e.target.checked)next.add(type);else next.delete(type);return next;});}}/>
            <i style={{background:ROOF_LINE_COLORS[type]}}/><span>{labels[type]}</span><b>{current.edges.filter(e=>e.kind===type).length}</b>
          </label>)}
        </section>
        <section className="roof-line-selection">
          <div className="roof-line-section-heading"><h3 data-testid="roof-line-selection-count">{tool==="draw"?"New line":multi?`${selected.edges.length} lines selected`:single?`Line ${selected.edges[0]+1}`:selected.point!==undefined?`Point ${selected.point+1}`:"Selection"}</h3><span>{single?`${length(single).toFixed(2)} m`:multi?`${total.toFixed(1)} m total`:""}</span></div>
          <label>Line type<select aria-label="Roof line type" disabled={!ready||(tool!=="draw"&&!single&&!multi)} value={single?.kind||(multi?current.edges[selected.edges[0]]?.kind:undefined)||kind} onChange={event=>{
            const type=event.target.value as RoofFeedbackLineKind;setKind(type);setVisibleKinds(old=>new Set([...old,type]));
            // One commit for every selected line: one undo step; the same selection stays active afterwards.
            if(selected.edges.length){const edges=selected.edges,next=structuredClone(value);for(const i of edges)if(next.edges[i])next.edges[i].kind=type;if(commit(next))setSelected({edges});}
          }}>{LINE_KINDS.map(k=><option key={k} value={k}>{labels[k]}</option>)}</select></label>
          <p className="roof-line-type-hint">{multi?"Type applies to every selected line. Delete removes them in one step; Undo restores them together.":selected.point!==undefined?"Shared points move all their connected lines.":!single&&tool!=="draw"?"Select a line on the image, or open Find a line below.":descriptions[single?.kind||kind]}</p>
          <div className="roof-line-selection-actions">
            <button className="review-button" disabled={!ready||!single||!showEdits} onClick={()=>{
              if(!single)return;const next=structuredClone(value),a=next.vertices[single.a],b=next.vertices[single.b],i=next.vertices.length;
              next.vertices.push([(a[0]+b[0])/2,(a[1]+b[1])/2]);next.edges.splice(selected.edges[0],1,{...single,b:i},{...single,a:i});commit(next);
            }}><CornerDownRight size={14}/>Add junction</button>
            <button className="review-button roof-line-delete" data-testid="roof-line-delete" aria-label={selected.point!==undefined?"Delete point and lines":multi?"Delete selected lines":"Delete selected line"} disabled={!ready||!showEdits||(!selected.edges.length&&selected.point===undefined)} onClick={remove}><Trash2 size={14}/>{selected.point!==undefined?"Delete point":multi?`Delete ${selected.edges.length} lines`:"Delete line"}</button>
          </div>
          <details className="roof-line-list-details">
            <summary>Find a line <span>{current.edges.length}</span></summary>
            <div className="roof-line-list">
              {current.edges.map((edge,i)=><button key={i} disabled={!ready} aria-pressed={selected.edges.includes(i)} aria-label={`Select roof line ${i+1}: ${labels[edge.kind]}`} onClick={event=>selectEdge(i,event.shiftKey)}><span>{String(i+1).padStart(2,"0")}</span><i style={{background:ROOF_LINE_COLORS[edge.kind]}}/>{labels[edge.kind]}<small>{length(edge).toFixed(1)} m</small></button>)}
              {!current.edges.length&&<p>No lines yet. Choose Draw roof line to start.</p>}
            </div>
          </details>
        </section>
        <section className="roof-line-visibility">
          <h3>Compare & visibility</h3>
          <label><input type="checkbox" checked={showEdits} disabled={!ready} onChange={e=>{setSelected({edges:[]});setShowEdits(e.target.checked);}}/>Your edited lines</label>
          <label><input type="checkbox" checked={compare} disabled={!ready} onChange={e=>setCompare(e.target.checked)}/>Compare starting estimate</label>
          <label><input type="checkbox" checked={showBoundary} disabled={!ready} onChange={e=>setShowBoundary(e.target.checked)}/>Roof boundary</label>
        </section>
        <section className="roof-line-footer">
          <label>Feedback notes<textarea className="resize-none" aria-label="Roof line feedback notes" value={value.notes} maxLength={3000} rows={3} disabled={!ready} placeholder="What should the next estimate get right?" onChange={event=>commit({...value,notes:event.target.value},false)}/></label>
          <label className="roof-line-checked"><input type="checkbox" checked={!!stored?.checked} disabled={!ready} onChange={event=>commit({...value,checked:event.target.checked},false)}/>I checked these lines against the visible roof</label>
          <p className="review-muted">Partial feedback is welcome. Line positions and types are separate from 3D heights.</p>
        </section>
        <details className="roof-line-advanced"><summary>Advanced edits</summary><div>
          <button className="review-button" disabled={!ready||!stored} onClick={()=>commit({...value,...structuredClone(value.baseline)})}>Reset to starting estimate</button>
          <button className="review-button" disabled={!ready} onClick={()=>commit({...value,vertices:[],edges:[]})}>Clear lines</button>
          <p>These changes can be undone before or after saving.</p>
        </div></details>
      </aside>
    </div>
    <footer className="roof-line-save-bar">
      <div aria-live="polite"><span className="roof-line-save-state">{saving?"Saving your feedback…":start||dragging?"Finish this line edit to save":dirty?"Unsaved changes":`Saved revision ${revision}`}</span><span>{current.edges.length} {current.edges.length===1?"line":"lines"} · {stored?.checked?"Imagery checked":"Draft feedback"}</span></div>
      <button className="review-button review-primary" disabled={!ready||!dirty||saving} onClick={onSave}>{!dirty?<Check size={16}/>:<Save size={16}/>}<span>{saving?"Saving…":"Save roof line feedback"}</span></button>
    </footer>
  </section>;
}
