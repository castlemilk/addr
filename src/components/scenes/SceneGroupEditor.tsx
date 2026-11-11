"use client";
import {useEffect, useMemo, useRef, useState} from "react";
import {Search, X} from "lucide-react";
import Scene3D from "./Scene3D";
import {activeGroupObjects, removeSceneGroup, renameSceneGroup, setGroupMembers} from "@/lib/scene-groups";
import {CLASS_COLORS, CLASS_NAMES, SCENE_CLASSES, scenePathData, type SceneData, type SceneGroup, type SceneObject, type SceneReview} from "@/lib/scene-types";

function GroupName({group,disabled,onRename}:{group:SceneGroup;disabled:boolean;onRename:(name:string)=>void}) {
  const [name,setName]=useState(group.label);
  return <form noValidate className="scene-group-name" onSubmit={e=>{e.preventDefault();if(!disabled&&name.trim()&&name.length<=150)onRename(name);}}>
    <label>Group name<input aria-label="Group name" value={name} maxLength={150} disabled={disabled} onChange={e=>setName(e.target.value)}/></label>
    <button className="review-button" disabled={disabled||!name.trim()||name.trim()===group.label}>Rename group</button>
  </form>;
}

export default function SceneGroupEditor({data,review,selectedGroup,onSelectGroup,layer,onLayer,onChange,onInspect,onClose,disabled,visible}:{
  data:SceneData;review:SceneReview;selectedGroup:string|null;onSelectGroup:(id:string)=>void;
  layer:"model"|"edited"|"overlay";onLayer:(layer:"model"|"edited"|"overlay")=>void;
  onChange:(review:SceneReview)=>void;onInspect:(object:SceneObject,groupId:string)=>void;onClose:()=>void;disabled:boolean;
  visible:Set<string>;
}) {
  const [name,setName]=useState(""),[query,setQuery]=useState(""),[kind,setKind]=useState("all"),[membership,setMembership]=useState("all");
  const [view,setView]=useState("aerial"),[focused,setFocused]=useState(false),[membersOnly,setMembersOnly]=useState(false);
  const host=useRef<HTMLElement>(null),search=useRef<HTMLInputElement>(null);
  useEffect(()=>{host.current?.focus({preventScroll:true});host.current?.scrollIntoView({block:"start"});},[]);
  const readOnly=disabled||data.stale||layer==="model";
  // Keep reviewer-created groups selectable in Prediction, with an explicit
  // empty counterpart, rather than silently switching to a different group.
  const groups=layer==="model"?[...data.model.groups,...review.groups.filter(g=>!data.model.groups.some(m=>m.id===g.id))]:review.groups;
  const objects=layer==="model"?data.model.objects:review.objects;
  const group=groups.find(g=>g.id===selectedGroup)||groups[0]||null;
  const groupId=group?.id||"";
  const hasPrediction=!!group&&data.model.groups.some(g=>g.id===group.id);
  const groupLabels=new Map(groups.map(g=>[g.id,`${g.label}${groups.some(other=>other.id!==g.id&&other.label===g.label)?` · ${g.id.slice(-6)}`:""}`]));
  const active=useMemo(()=>activeGroupObjects(objects),[objects]);
  const members=useMemo(()=>active.filter(o=>o.group_id===groupId),[active,groupId]);
  const memberIds=useMemo(()=>members.map(o=>o.id),[members]);
  const results=active.filter(o=>(kind==="all"||o.kind===kind)&&
    (membership==="all"||membership==="members"&&o.group_id===groupId||membership==="ungrouped"&&!o.group_id)&&
    `${o.label} ${o.kind} ${o.id}`.toLowerCase().includes(query.trim().toLowerCase()));
  const additions=results.filter(o=>o.group_id!==groupId),removals=results.filter(o=>o.group_id===groupId);
  const inactive=objects.filter(o=>o.group_id===groupId&&["rejected","superseded"].includes(o.status)).length;
  const setMembers=(ids:string[],include:boolean)=>{
    if(readOnly||!group)return;
    const next=setGroupMembers(review,group.id,ids,include);if(next!==review)onChange(next);
  };
  const toggle=(object:SceneObject)=>setMembers([object.id],object.group_id!==group?.id);
  const points=members.filter(o=>visible.has(o.kind)).flatMap(o=>o.ring),xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
  const width=data.model.image_width,height=data.model.image_height;
  const span=points.length?Math.min(Math.max(width,height),Math.max(55,Math.max(Math.max(...xs)-Math.min(...xs),Math.max(...ys)-Math.min(...ys))*1.25)):Math.max(width,height);
  const crop=points.length?[Math.max(0,Math.min(width-span,(Math.min(...xs)+Math.max(...xs)-span)/2)),Math.max(0,Math.min(height-span,(Math.min(...ys)+Math.max(...ys)-span)/2)),span,span].join(" "):`0 0 ${width} ${height}`;
  const preview=(membersOnly?members:active).filter(o=>visible.has(o.kind)).sort((a,b)=>Number(a.group_id===groupId)-Number(b.group_id===groupId));
  return <section className="scene-group-editor" aria-label="Scene groups" tabIndex={-1} ref={host}>
    <div className="scene-group-heading"><div><h2>Groups</h2><p className="review-muted">Organise related objects across layers. Each outline, review and parent relationship stays separate.</p></div>
      <button className="review-button" disabled={disabled} onClick={onClose}>Back to object review</button>
    </div>
    <div className="scene-group-toolbar">
      <label>Inspect group<select aria-label="Inspect group" value={group?.id||""} disabled={disabled||!groups.length} onChange={e=>onSelectGroup(e.target.value)}>
        {!groups.length&&<option value="">No groups yet</option>}
        {groups.map(g=><option key={g.id} value={g.id}>{groupLabels.get(g.id)} · {active.filter(o=>o.group_id===g.id).length} objects{layer==="model"&&!data.model.groups.some(m=>m.id===g.id)?" · no prediction":""}</option>)}
      </select></label>
      <div className="review-view-switch" aria-label="Group geometry layer">{(["model","edited","overlay"] as const).map(l=><button key={l} disabled={disabled} aria-pressed={layer===l} onClick={()=>onLayer(l)}>{l==="model"?"Prediction":l==="edited"?"Your edits":"Compare"}</button>)}</div>
      <div className="review-view-switch" aria-label="Group view"><button aria-pressed={view==="aerial"} onClick={()=>setView("aerial")}>Group image</button><button aria-pressed={view==="3d"} onClick={()=>setView("3d")}>Group 3D</button></div>
    </div>
    {layer==="model"&&<p className="review-notice">Prediction groups are read-only. Choose Your edits to change membership.</p>}
    {layer==="model"&&group&&!hasPrediction&&<p className="review-notice">This group was created during review and has no model group. Choose Your edits to see its members.</p>}
    {data.stale&&<p className="review-notice">Recover or start reviewing the current model before editing groups.</p>}
    <div className="scene-group-workspace">
      <div className="scene-group-preview">
        <p className="scene-group-count" role="status">{group?`${group.label} · ${members.length} active members${inactive?` · ${inactive} inactive history references`:""}`:"Create a group, then choose its members."}</p>
        {view==="3d"?(members.length?<Scene3D scene={data.model} objects={objects} predictions={layer==="overlay"?data.model.objects:undefined} selected={null} visible={visible} groupIds={memberIds}
          onSelect={id=>{const object=objects.find(o=>o.id===id);if(object&&group&&!disabled)onInspect(object,group.id);}}/>:
          <div className="scene-group-empty"><p>This group has no active members to model.</p><button className="review-button" onClick={()=>setView("aerial")}>Choose objects in image</button></div>):<>
          <div className="scene-inline-actions">
            <button className="review-button" disabled={!members.length&&!focused} aria-pressed={focused} onClick={()=>setFocused(!focused)}>{focused?"Whole image":"Focus group"}</button>
            <label className="review-checkbox"><input type="checkbox" checked={membersOnly} onChange={e=>setMembersOnly(e.target.checked)}/>Show members only</label>
          </div>
          <svg className="scene-group-image" role="group" aria-label="Group outline selection" viewBox={focused?crop:`0 0 ${width} ${height}`}>
            <image href={data.image_url} width={width} height={height}/>
            {layer==="overlay"&&activeGroupObjects(data.model.objects).filter(o=>visible.has(o.kind)&&(!membersOnly||memberIds.includes(o.id))).map(o=><path key={`baseline-${o.id}`} d={scenePathData(o)} fill="none" stroke="#f4cc89" strokeDasharray="5 4" strokeWidth="1.2" opacity=".7" vectorEffect="non-scaling-stroke" pointerEvents="none"/>)}
            {preview.map(o=>{
              const checked=o.group_id===groupId;
              return <path key={o.id} data-group-object-id={o.id} d={scenePathData(o)} fillRule="evenodd" fill={`${CLASS_COLORS[o.kind]}${checked?"32":"08"}`}
                stroke={CLASS_COLORS[o.kind]} opacity={checked?1:.4} strokeWidth={checked?2.5:1} vectorEffect="non-scaling-stroke"
                role="checkbox" aria-label={`Group membership: ${o.label} · ${o.id.slice(-4)}`} aria-checked={checked} aria-disabled={readOnly||!group}
                tabIndex={readOnly||!group?-1:0} onClick={()=>toggle(o)} onKeyDown={e=>{if(e.key===" "||e.key==="Enter"){e.preventDefault();toggle(o);}}}>
                <title>{o.label} · {checked?"In this group":o.group_id?`In ${groups.find(g=>g.id===o.group_id)?.label||"another group"}`:"Ungrouped"}</title>
              </path>;
            })}
          </svg>
          <p className="review-muted">Click an outline or use its checkbox to add or remove it. Adding an object moves it from its previous group. Layer visibility applies to the image and 3D. Amber dashes show the prediction in Compare.</p>
        </>}
        {view==="3d"&&members.length>0&&<p className="review-muted">Select a 3D object to inspect its outline and model details. Heights remain estimates.</p>}
      </div>
      <div className="scene-group-details">
        <form noValidate className="scene-group-name" onSubmit={e=>{
          e.preventDefault();if(readOnly||!name.trim()||name.length>150||review.groups.length>=250)return;
          const id=crypto.randomUUID();onChange({...review,groups:[...review.groups,{id,label:name.trim()}]});onSelectGroup(id);setName("");setMembership("all");
        }}>
          <label>New group name<input aria-label="New group name" value={name} maxLength={150} disabled={readOnly} onChange={e=>setName(e.target.value)} placeholder="e.g. Southern garden"/></label>
          <button className="review-button" disabled={readOnly||!name.trim()||review.groups.length>=250}>Create group</button>
        </form>
        {group&&<GroupName key={`${group.id}:${group.label}`} group={group} disabled={readOnly} onRename={name=>onChange(renameSceneGroup(review,group.id,name))}/>}
        <div className="review-search"><Search size={15}/><input ref={search} aria-label="Search group objects" placeholder="Find a member or object…" value={query} maxLength={200} onChange={e=>setQuery(e.target.value)}/>
          {query&&<button aria-label="Clear group search" onClick={()=>{setQuery("");search.current?.focus();}}><X size={15}/></button>}
        </div>
        <div className="scene-group-filters">
          <label>Class<select aria-label="Group object class" value={kind} onChange={e=>setKind(e.target.value)}><option value="all">All classes</option>{SCENE_CLASSES.map(k=><option key={k} value={k}>{CLASS_NAMES[k]}</option>)}</select></label>
          <label>List<select aria-label="Group membership filter" value={membership} onChange={e=>setMembership(e.target.value)}><option value="all">All active objects</option><option value="members">Members of this group</option><option value="ungrouped">Ungrouped objects</option></select></label>
        </div>
        <p className="review-muted">{results.length} objects in this list. Rejected and replaced outlines remain in review history.</p>
        <div className="scene-group-members" aria-label="Group object list">
          {results.map(o=><div className="scene-group-member" key={o.id} data-member-id={o.id}>
            <label><input type="checkbox" checked={o.group_id===groupId} disabled={readOnly||!group} onChange={()=>toggle(o)}/>
              <span><strong>{o.label} · {o.id.slice(-4)}</strong><small>{CLASS_NAMES[o.kind]} · {o.status.replaceAll("_"," ")}{o.group_id&&o.group_id!==groupId?` · From ${groups.find(g=>g.id===o.group_id)?.label||o.group_id}`:""}</small></span>
            </label>
            <button className="review-text-button" disabled={disabled} aria-label={`Inspect ${o.label} · ${o.id.slice(-4)}`} onClick={()=>onInspect(o,group?.id||"")}>Inspect</button>
          </div>)}
          {!results.length&&<p className="review-notice">No matching objects. Clear the search or change the list filters.</p>}
        </div>
        <div className="scene-inline-actions">
          <button className="review-button" disabled={readOnly||!group||!additions.length} onClick={()=>setMembers(additions.map(o=>o.id),true)}>Add {additions.length} listed objects</button>
          <button className="review-button" disabled={readOnly||!group||!removals.length} onClick={()=>setMembers(removals.map(o=>o.id),false)}>Remove {removals.length} listed members</button>
        </div>
        {group&&<div className="scene-group-remove"><button className="review-button" disabled={readOnly} onClick={()=>onChange(removeSceneGroup(review,group.id))}>Remove group</button>
          <p className="review-muted">Ungroups its objects. Their outlines, reviews and parent relationships are kept. Undo edit restores the group.</p></div>}
        <p className="review-muted">Group changes are drafts. Save scene keeps them in revision history.</p>
      </div>
    </div>
  </section>;
}
