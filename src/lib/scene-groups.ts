import type {SceneObject, SceneReview} from "./scene-types";

export const activeGroupObjects=(objects:SceneObject[])=>objects.filter(o=>!["rejected","superseded"].includes(o.status));

/** Grouping changes affiliation only; class approvals and physical parents stay intact. */
export function setGroupMembers(review:SceneReview,groupId:string,ids:string[],include:boolean):SceneReview {
  if(!review.groups.some(g=>g.id===groupId))return review;
  const targets=new Set(ids);
  let changed=false;
  const objects=review.objects.map(o=>{
    if(!targets.has(o.id)||["rejected","superseded"].includes(o.status))return o;
    const next=include?groupId:o.group_id===groupId?undefined:o.group_id;
    if(next===o.group_id)return o;
    changed=true;return {...o,group_id:next};
  });
  return changed?{...review,objects}:review;
}

export function renameSceneGroup(review:SceneReview,id:string,label:string):SceneReview {
  const name=label.trim();
  if(!name||name.length>150||!review.groups.some(g=>g.id===id&&g.label!==name))return review;
  return {...review,groups:review.groups.map(g=>g.id===id?{...g,label:name}:g)};
}

/** Remove the container and current references; saved history retains the prior grouping. */
export function removeSceneGroup(review:SceneReview,id:string):SceneReview {
  if(!review.groups.some(g=>g.id===id))return review;
  return {...review,groups:review.groups.filter(g=>g.id!==id),objects:review.objects.map(o=>o.group_id===id?{...o,group_id:undefined}:o)};
}
