import assert from 'node:assert/strict';
import ts from 'typescript';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
const req=createRequire(import.meta.url),cache=new Map();
function load(name){if(cache.has(name))return cache.get(name);const exports={};cache.set(name,exports);
 const source=ts.transpileModule(readFileSync(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('exports','require',source)(exports,id=>id.startsWith('./')?load(id.slice(2)):req(id));return exports;}
const {patchedObjectSplit:apply,splitChildId}=load('object-split');
const {automatedPolygonEditIssue:protection,polygonDraftFingerprint:fingerprint,parseObjectPolygonPatch:parse,patchedObjectPolygon:polygon}=load('roof-feedback-patch');
const {lineageIssue}=load('scene-instances');
const ring=[[64,80],[192,80],[192,176],[64,176]],left=[[64,80],[128,80],[128,176],[64,176]],right=[[128,80],[192,80],[192,176],[128,176]];
const template={id:'source',kind:'tree',label:'Crown cluster',ring,holes:[],status:'candidate',geometry_reviewed:false,source:'sam3-text-onnx',parent_id:'physical-parent',visibility:'occluded',boundary_refinement:{raw_mask_polygon:ring},clipped_by_frame:[0,0,256,256],model:{height_m:9,base_m:0,height_source:'class-prior',height_verified:false,width_m:32,length_m:24,area_m2:768,rotation_deg:0,color:'#green',tree:{crown_profile:'rounded',crown_base_ratio:.3},footprint_area_px:12288}};
function fixture(grouped){const object=structuredClone(template);if(grouped)object.group_id='existing';
 const other={...structuredClone(object),id:'physical-parent',kind:'building',notes:'Keep unrelated geometry and provenance',parent_id:undefined,group_id:undefined,provenance:{source:'manual-ui'},geometry_reviewed:true,status:'confirmed'};
 const model={id:'scene',model_signature:'a'.repeat(64),image_sha256:'b'.repeat(64),pixel_to_meters:[[.25,0],[0,.25],[0,0]],image_width:256,image_height:256,objects:[object,other],groups:grouped?[{id:'existing',label:'Existing collection'}]:[]};
 return {model,review:null,stale:false};}
function preview(data,groupLabel){const binding={scene_id:data.model.id,object_id:'source',object_kind:'tree',revision:data.review?.revision||0,model_signature:data.model.model_signature,image_sha256:data.model.image_sha256,source_model_sha256:'d'.repeat(64),review_sha256:null,
 // Actual Python sort_keys manifests put holes before ring.
 outline_pixels:{holes:[],ring}};
 const children=[['left',left,'object'],['right',right,'unresolved-residual']].map(([key,ring,role])=>({key,object_id:splitChildId(binding,key),label:key,outline_pixels:{holes:[],ring},notes:'Visible patch, ownership uncertain',role}));
 return {preview_id:'e'.repeat(64),content:{version:'object-agent-split-preview-v1',binding,object_kind:'tree',children,group:groupLabel?{id:'splitgroup_'+createHash('sha256').update(JSON.stringify(['scene','source',binding.model_signature,groupLabel])).digest('hex').slice(0,32),label:groupLabel}:null,inherited_group_id:data.model.objects[0].group_id??null,notes:'Scratch split',uncertainty:'Seams unapproved',coverage:{rings:children.map(c=>c.outline_pixels.ring),holes:[[],[]],children:{},area_px:12288},can_apply:true}};}
for(const grouped of [false,true])for(const groupLabel of ['', 'Jardin 🌳']){
 const data=fixture(grouped),packet=preview(data,groupLabel),before=structuredClone(data),draft=apply(data,packet,'fixture');
 assert.deepEqual(data,before);assert.equal(lineageIssue(draft.objects),null);
 const parent=draft.objects.find(o=>o.id==='source'),children=draft.objects.filter(o=>o.derived_from?.includes('source'));
 assert.equal(parent.status,'superseded');assert.equal(parent.geometry_reviewed,false);assert.deepEqual(parent.boundary_refinement,template.boundary_refinement);
 assert.deepEqual(parent.replacement_ids,packet.content.children.map(c=>c.object_id));assert.deepEqual(draft.objects[1],data.model.objects[1]);
 for(const [index,child] of children.entries()){
  assert.equal(child.kind,'tree');assert.equal(child.source,'machine:mcp-object-split-draft-v1');assert.equal(child.status,'needs_adjustment');assert.equal(child.geometry_reviewed,false);assert.equal(child.model.height_verified,false);assert.equal(child.visibility,undefined);
  assert.equal(child.parent_id,'physical-parent');assert.equal(child.group_id,groupLabel?packet.content.group.id:grouped?'existing':undefined);assert.equal(child.model.tree,undefined);assert.equal(child.boundary_refinement,undefined);assert.equal(child.clipped_by_frame,undefined);
  assert.deepEqual(child.ring,[left,right][index]);assert.deepEqual(child.derived_from,['source']);assert.equal(child.provenance.tier,'T3');assert.equal(child.provenance.human_confirmed,false);assert.equal(child.provenance.human_approval,false);assert.equal(child.provenance.training_eligible,false);assert.equal(child.provenance.draft_fingerprint,fingerprint(child));assert.equal(child.provenance.source_scene_id,'scene');
 }
 assert(children[1].review_issues.includes('class'));
 const saved={...data,review:{...draft,revision:1}};
 assert.equal(protection(saved,children[0]),null);
 const changedOutline={ring:[[65,80],[128,80],[128,176],[65,176]],holes:[]};
 const patch=parse({scene_id:'scene',object_id:children[0].id,expected_revision:1,model_signature:data.model.model_signature,image_sha256:data.model.image_sha256,outline_pixels:{ring:left,holes:[]},changes:{outline_pixels:changedOutline,notes:'Audited child boundary correction'}});
 const updated=polygon(saved,patch),child=updated.objects.find(o=>o.id===children[0].id),current={...data,review:{...updated,revision:2}};
 assert.equal(child.provenance.source,'machine:mcp-polygon-draft-v1');assert.equal(child.provenance.split_preview_id,packet.preview_id);assert.equal(child.provenance.source_scene_id,'scene');assert.equal(protection(current,child),null);
 const recursive=preview(current,'');recursive.content.binding.object_id=child.id;recursive.content.binding.outline_pixels=changedOutline;recursive.content.inherited_group_id=child.group_id??null;recursive.content.children=[['upper',[[65,80],[128,80],[128,128],[65,128]]],['lower',[[65,128],[128,128],[128,176],[65,176]]]].map(([key,ring])=>({key,object_id:splitChildId(recursive.content.binding,key),label:key,notes:'Recursive patch draft',role:'object',outline_pixels:{ring,holes:[]}}));recursive.content.coverage={rings:recursive.content.children.map(c=>c.outline_pixels.ring),holes:[[],[]],children:{},area_px:6048};
 const resplit=apply(current,recursive,'fixture');assert.equal(lineageIssue(resplit.objects),null);assert.equal(resplit.objects.find(o=>o.id==='source').replacement_ids[0],child.id);assert.deepEqual(resplit.objects.find(o=>o.id===child.id).derived_from,['source']);
 for(const change of [o=>o.notes='Human changed notes',o=>o.source='reviewer-split',o=>o.fieldwork_import={pending:true},o=>o.provenance.human_confirmed=true,o=>o.kind='vegetation']){
  const foreign=structuredClone(current);change(foreign.review.objects.find(o=>o.id===child.id));assert.notEqual(protection(foreign,foreign.review.objects.find(o=>o.id===child.id)),null);
 }
}
for(const change of [d=>d.model.objects[0].geometry_reviewed=true,d=>d.model.objects[0].source='reviewer-manual',d=>d.model.objects[0].model.height_verified=true,d=>d.model.objects[0].fieldwork_import={pending:true},d=>d.model.objects.push({...structuredClone(template),id:'attached',parent_id:'source'})]){const data=fixture(false);change(data);assert.throws(()=>apply(data,preview(data,''),'fixture'));}
for(const change of [p=>p.content.object_kind='vegetation',p=>p.content.binding.outline_pixels.ring=[[0,0],[10,0],[10,10],[0,10]],p=>p.content.inherited_group_id='foreign',p=>p.content.children[0].object_id='foreign',p=>p.content.binding.revision=2,p=>p.content.children[1].key='left']){const data=fixture(false),packet=preview(data,'');change(packet);assert.throws(()=>apply(data,packet,'fixture'));}
console.log('Split guards: manifest order, ungrouped/null + Unicode groups, full geometry/lineage, physical attachments, unchecked T3 + estimate invalidation, child polygon refinement + recursive splitting, foreign/human/import/stale protection passed.');
