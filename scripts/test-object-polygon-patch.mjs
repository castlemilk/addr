import assert from 'node:assert/strict';
import ts from 'typescript';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const req=createRequire(import.meta.url),cache=new Map();
function load(name){if(cache.has(name))return cache.get(name);const exports={};cache.set(name,exports);
 const source=ts.transpileModule(readFileSync(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('exports','require',source)(exports,id=>id.startsWith('./')?load(id.slice(2)):req(id));return exports;}
const {parseObjectPolygonPatch:parse,patchedObjectPolygon:apply,patchedRoofFeedback:roofApply,automatedPolygonEditIssue:protection,polygonDraftFingerprint:fingerprint}=load('roof-feedback-patch');
const ring=[[0,0],[10,0],[10,10],[0,10]],outline={ring,holes:[]},replacement={ring:[[1,0],[9,0],[9,10],[1,10]],holes:[]};
const template={id:'object',label:'Fixture',ring,holes:[],status:'candidate',geometry_reviewed:false,source:'sam3-text-onnx',parent_id:'parent',group_id:'group',visibility:'full',model:{height_m:3,height_verified:false,width_m:10,length_m:10,area_m2:100,rotation_deg:0}};
for(const kind of load('scene-types').SCENE_CLASSES){
 const object={...structuredClone(template),kind,model:{...template.model,solar:{tilt_deg:20},tree:{crown_profile:'rounded',crown_base_ratio:.3},car:{profile:'car'},surface:{profile:'plane'}}};
 const other={...structuredClone(template),id:'parent',kind:'building',notes:'Retain this unrelated object'};
 const model={id:'scene',model_signature:'a'.repeat(64),image_sha256:'b'.repeat(64),pixel_to_meters:[[1,0],[0,1],[0,0]],image_width:100,image_height:100,objects:[object,other],groups:[{id:'group',label:'Group'}]};
 const data={model,review:null,stale:false};
 const binding={scene_id:'scene',object_id:'object',expected_revision:0,model_signature:model.model_signature,image_sha256:model.image_sha256,outline_pixels:outline};
 const patch=parse({...binding,changes:{outline_pixels:replacement,polygon_preview_id:'c'.repeat(64),notes:'Explicit source-bound agent trace'}});
 const before=structuredClone(data),output=apply(data,patch,'fixture');
 assert.deepEqual(output.objects[0].ring,replacement.ring);assert.equal(output.objects[0].kind,kind);
 assert.equal(output.objects[0].parent_id,'parent');assert.equal(output.objects[0].group_id,'group');assert.equal(output.objects[0].geometry_reviewed,false);
 assert.equal(output.objects[0].status,'needs_adjustment');assert.equal(output.objects[0].model.height_verified,false);
 assert.equal(output.objects[0].provenance.source,'machine:mcp-polygon-draft-v1');assert.equal(output.objects[0].provenance.tier,'T3');assert.equal(output.objects[0].provenance.human_approval,false);
 assert.equal(output.objects[0].provenance.draft_fingerprint,fingerprint(output.objects[0]));assert.deepEqual(output.objects[1],other);assert.deepEqual(data,before);
 if(kind!=='building'){for(const field of ['solar','tree','car','surface'])assert.equal(output.objects[0].model[field],undefined);assert.throws(()=>roofApply(data,patch));}
 const saved={...data,review:{...output,revision:1,reviewed_classes:load('scene-types').SCENE_CLASSES}};
 const next=parse({...binding,expected_revision:1,outline_pixels:replacement,changes:{outline_pixels:{ring:[[2,0],[9,0],[9,10],[2,10]],holes:[]}}});
 const continued=apply(saved,next);assert(!continued.reviewed_classes.includes(kind));assert.equal(continued.reviewed_classes.length,12);
 for(const change of [o=>o.notes='Human changed notes',o=>o.model.height_m=5,o=>o.parent_id='new-parent',o=>o.geometry_reviewed=true,o=>o.fieldwork_import={pending:true}]){
  const changed=structuredClone(saved);change(changed.review.objects[0]);assert.throws(()=>apply(changed,next),e=>e.status===409);
 }
 if(kind!=='building'){
  const changed=structuredClone(data);changed.review={revision:1,model_signature:model.model_signature,objects:[structuredClone(object),other],groups:[],reviewed_classes:[]};
  changed.review.objects[0].model.solar={tilt_deg:27};assert.match(protection(changed,changed.review.objects[0]),/reviewer/);
 }
 assert.throws(()=>parse({...binding,changes:{notes:'Not a polygon draft'}}));
 assert.throws(()=>parse({...binding,changes:{roof_line_edits:{vertices_meters:[[1,5],[9,5]],edges:[{a:0,b:1,kind:'ridge'}]}}}));
}
console.log('Generic polygon guards: 13 classes, unchanged classes/relationships/source, per-class reopening, derived estimate invalidation, protected human/imported work and building-only roof line contract passed.');
