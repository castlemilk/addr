// src/lib/scene-queue.ts frameFilterValue / frameFilterAllows: the scene editor's client-side frame filter. Frame-cut objects (a
// vertex within OUTLINE_DESIGN.frame_cut.edge_px of an image edge, or beyond it) leave the queue under "skip" (the default), are the
// whole queue under "only" and are left alone under "all". The existing exports and the server-side filter signature are unchanged:
// matchesSceneQueue never sees the frame. Then the advance order as the page computes it (members in scene order after the decided
// object) with each frame value. Pure: nothing here reads a scene or writes a file outside a temp dir.
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-frame-filter-'));
try {
 for(const name of ['scene-types','scene-queue','scene-review-issues','scene-addresses','outline-hints']) {
  const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
  await writeFile(path.join(directory,`${name}.mjs`),code);
 }
 const queueModule=await import(pathToFileURL(path.join(directory,'scene-queue.mjs')));
 const {frameFilterValue,frameFilterAllows,matchesSceneQueue,sceneQueueFilter}=queueModule;
 const {OUTLINE_DESIGN}=await import(pathToFileURL(path.join(directory,'outline-hints.mjs')));
 for(const name of ['REVIEW_STATUS_LABELS','sceneQueueFilter','matchesSceneQueue','sceneProgress','sceneSummary','sceneTitle','nextReviewImages'])assert.equal(typeof queueModule[name],name==='REVIEW_STATUS_LABELS'?'object':'function',`${name} still exported`);
 assert.deepEqual(Object.keys(sceneQueueFilter({})).sort(),['issue','kind','query','status'],'the server-side filter shape is unchanged (no frame)');
 // URL / select values: anything but all or only is the default, skip.
 assert.equal(frameFilterValue('skip'),'skip');assert.equal(frameFilterValue('all'),'all');assert.equal(frameFilterValue('only'),'only');
 for(const bad of [null,undefined,'','bogus','__proto__','ALL','Skip'])assert.equal(frameFilterValue(bad),'skip',`${bad} -> skip`);
 const model={image_width:512,image_height:512},wide={image_width:1024,image_height:1024};
 const box=(id,x0,y0,x1,y1,status='candidate',kind='building')=>({id,kind,label:id,status,geometry_reviewed:false,ring:[[x0,y0],[x1,y0],[x1,y1],[x0,y1]]});
 const inside=box('inside',10,10,100,100),onEdge=box('onEdge',0,10,100,100),half=box('half',10,10,100,511.5),beyond=box('beyond',-3,10,100,100),atTolerance=box('atTolerance',10,OUTLINE_DESIGN.frame_cut.edge_px,100,100),clear=box('clear',10,1.5,100,100);
 const allows=(frame,m)=>[inside,onEdge,half,beyond,atTolerance,clear].filter(o=>frameFilterAllows(o,m,frame)).map(o=>o.id); // no default: undefined is a case under test
 assert.deepEqual(allows('skip',model),['inside','clear'],'skip drops every frame-cut object (on, 0.5 px inside, beyond, at edge_px)');
 assert.deepEqual(allows('only',model),['onEdge','half','beyond','atTolerance'],'only keeps exactly the frame-cut objects');
 assert.deepEqual(allows('all',model),['inside','onEdge','half','beyond','atTolerance','clear'],'all filters nothing');
 assert.deepEqual(allows('skip',wide),['inside','half','clear'],'a 1024 frame: y = 511.5 is clear of its bottom edge; x = 0, x = -3 and y = 1 still touch');
 assert.deepEqual(allows('only',wide),['onEdge','beyond','atTolerance']);
 for(const missing of [null,undefined,{}]){
  assert.deepEqual(allows('skip',missing),['inside','onEdge','half','beyond','atTolerance','clear'],'without an image size nothing can be called frame-cut: skip keeps all');
  assert.deepEqual(allows('only',missing),[],'without an image size nothing can be called frame-cut: only keeps none');
  assert.deepEqual(allows('all',missing),['inside','onEdge','half','beyond','atTolerance','clear']);
 }
 // The advance order after a decision, as page.tsx computes it: members = queue minus the decided object, in scene order; the next
 // member after the decided index wins, else the last one before it. A frame-cut neighbour never follows under skip.
 const scene=[box('a',10,10,100,100),box('b',0,120,100,200),box('c',10,10,100,512),box('d',200,200,300,300),box('e',0,0,50,50)];
 const advance=(decidedId,frame,status='candidate')=>{
  const filter=sceneQueueFilter({status});
  const members=scene.filter(o=>o.id!==decidedId&&matchesSceneQueue(o,filter)&&frameFilterAllows(o,model,frame));
  const decidedAt=scene.findIndex(o=>o.id===decidedId);
  return members.find(o=>scene.indexOf(o)>decidedAt)?.id??members.at(-1)?.id??null;
 };
 assert.equal(advance('a','skip'),'d','skip: b and c touch the frame, d is next');
 assert.equal(advance('a','all'),'b','all: b is next in scene order');
 assert.equal(advance('a','only'),'b','only: b is the next frame-cut object');
 assert.equal(advance('d','skip'),'a','skip: nothing after d is clear of the frame (e touches), so the last member before it');
 assert.equal(advance('d','only'),'e');
 assert.equal(advance('d','all'),'e');
 // matchesSceneQueue itself is frame-blind: the server's scene summaries keep counting frame-cut objects.
 assert.ok(matchesSceneQueue(onEdge,sceneQueueFilter({status:'candidate'})));
 assert.ok(matchesSceneQueue(beyond,sceneQueueFilter({status:'all'})));
 console.log(`scene frame filter: frameFilterValue defaults, frameFilterAllows under skip/all/only on 512 and 1024 frames and without an image size (edge_px ${OUTLINE_DESIGN.frame_cut.edge_px}), the advance order after a decision, and a frame-blind matchesSceneQueue passed.`);
} finally {
 await rm(directory,{recursive:true,force:true});
}
