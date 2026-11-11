import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-groups-'));
try {
  const code=ts.transpileModule(await readFile('src/lib/scene-groups.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
  const file=path.join(directory,'groups.mjs');await writeFile(file,code);
  const {setGroupMembers,renameSceneGroup,removeSceneGroup,activeGroupObjects}=await import(pathToFileURL(file));
  const object=(id,kind,group_id,status='confirmed')=>({id,kind,label:id,group_id,status,geometry_reviewed:status==='confirmed',
    ring:[[0,0],[10,0],[10,10],[0,10]],model:{height_m:3,base_m:0,height_verified:false}});
  const review={revision:3,model_signature:'snapshot',groups:[{id:'site',label:'Site'},{id:'garden',label:'Garden'}],reviewed_classes:['solar_panel','car'],
    objects:[object('roof','building','site'),{...object('panel','solar_panel','site'),parent_id:'roof'},object('tree','tree','garden'),object('car','car'),
      object('false','tree','garden','rejected'),{...object('old','building','site','superseded'),replacement_ids:['roof']}]};
  const before=JSON.stringify(review);
  const next=setGroupMembers(review,'garden',['roof','panel','car','false','old','missing'],true);
  assert.deepEqual(next.objects.map(o=>o.group_id),['garden','garden','garden','garden','garden','site']);
  assert.deepEqual(next.reviewed_classes,review.reviewed_classes);
  for(let i=0;i<review.objects.length;i++)assert.deepEqual({...next.objects[i],group_id:null},{...review.objects[i],group_id:null},'Grouping must preserve outlines, approvals, heights and parent relationships');
  assert.equal(next.objects[1].parent_id,'roof');
  assert.equal(setGroupMembers(next,'missing',['roof'],true),next);
  assert.equal(setGroupMembers(next,'site',['tree'],false),next,'Removing from another group is a no-op');
  const removed=setGroupMembers(next,'garden',['car','panel'],false);
  assert.equal(removed.objects[1].group_id,undefined);assert.equal(removed.objects[1].parent_id,'roof');
  assert.equal(setGroupMembers(removed,'garden',['car'],false),removed);
  assert.equal(renameSceneGroup(review,'site','  '),review);
  assert.equal(renameSceneGroup(review,'site','x'.repeat(151)),review);
  const renamed=renameSceneGroup(next,'garden','  Southern garden  ');
  assert.equal(renamed.groups[1].label,'Southern garden');assert.equal(renamed.objects,next.objects);
  const disbanded=removeSceneGroup(renamed,'garden');
  assert.deepEqual(disbanded.groups,[{id:'site',label:'Site'}]);
  assert(disbanded.objects.filter(o=>o.id!=='old').every(o=>o.group_id===undefined));
  assert.equal(disbanded.objects[1].parent_id,'roof');assert.equal(disbanded.objects[4].status,'rejected');
  assert.equal(activeGroupObjects(disbanded.objects).length,4);
  assert.equal(JSON.stringify(review),before,'All group actions keep the prior revision immutable');
  console.log('Scene groups: cross-class membership, no-op edits, rename/disband, inactive-reference cleanup, preserved approvals/parents/geometry and immutable drafts passed.');
}finally{await rm(directory,{recursive:true,force:true});}
