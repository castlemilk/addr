import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile,cp,rm} from 'node:fs/promises';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_object_polygon_${Date.now()}`,folder=`data/scenes/${id}`,source='data/scenes/roof_training_250-29790';
const model=JSON.parse(await readFile(`${source}/latest.json`,'utf8'));
const original=structuredClone(model.objects.find(o=>o.kind==='building'));
for(const key of ['roof_annotation','roof_feedback','roof_line_edits','roof_lines','roof_parts','roof_type','roof_geometry_note','roof_geometry_source','group_id','parent_id','address_id','structure_kind','provenance','fieldwork_import'])delete original[key];
Object.assign(original,{id:'parent',ring:[[100,100],[400,100],[400,300],[100,300]],holes:[],status:'candidate',geometry_reviewed:false});original.model.height_verified=false;
const solar={...structuredClone(original),id:'solar',kind:'solar_panel',label:'Solar fixture',parent_id:'parent',ring:[[150,150],[250,150],[250,200],[150,200]],model:{...original.model,solar:{tilt_deg:20,azimuth_deg:180}}};
const tree={...structuredClone(original),id:'tree',kind:'tree',label:'Tree fixture',ring:[[320,320],[420,320],[420,420],[320,420]],model:{...original.model,tree:{crown_profile:'rounded',crown_base_ratio:.3}}};
const human={...structuredClone(original),id:'human',status:'confirmed',geometry_reviewed:true,notes:'Retain human review',fieldwork_import:{pending:true,origin:'http://example.test',projectId:'fixture',itemId:'human',revision:1,cursor:'1'}};
model.id=id;model.objects=[original,solar,tree,human];model.groups=[];
let revision=0;
const binding=(object,outline={ring:object.ring,holes:object.holes||[]})=>({scene_id:id,object_id:object.id,outline_pixels:outline,expected_revision:revision,model_signature:model.model_signature,image_sha256:model.image_sha256});
const patch=(object,changes,overrides={})=>fetch(`${base}/api/scenes/object-feedback`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({...binding(object),changes,...overrides})});
const saved=async()=>JSON.parse(await readFile(`${folder}/review.json`,'utf8'));
await mkdir(folder);await writeFile(`${folder}/latest.json`,JSON.stringify(model));await cp(`${source}/${model.image_sha256}.png`,`${folder}/${model.image_sha256}.png`);
const modelBytes=await readFile(`${folder}/latest.json`),imageBytes=await readFile(`${folder}/${model.image_sha256}.png`);
try{
 let response=await fetch(`${base}/api/scenes/object-feedback?id=${id}&object=solar`);assert.equal(response.status,200,await response.clone().text());let state=await response.json();assert.equal(state.objects[0].kind,'solar_panel');assert.equal(state.objects[0].is_saved_review,false);assert.equal(state.objects[0].automated_polygon_edit_allowed,true);
 response=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,objects:model.objects,groups:[],reviewed_classes:[]}})});
 assert.equal(response.status,200,await response.clone().text());revision=(await response.json()).review.revision;
 let review=await saved();const savedHuman=structuredClone(review.objects.find(o=>o.id==='human')),savedParent=structuredClone(review.objects.find(o=>o.id==='parent'));
 const replacement={ring:[[152,151],[248,151],[248,199],[152,199]],holes:[]};
 const before=await readFile(`${folder}/review.json`,'utf8');
 for(const changes of [{notes:'Only notes is outside this polygon API'},{outline_pixels:replacement,kind:'building'},
  {outline_pixels:replacement,roof_line_edits:{vertices_meters:[[1,1],[2,2]],edges:[]}},
  {outline_pixels:{ring:[[100,100],[400,300],[100,300],[400,100]],holes:[]}}]){
  response=await patch(solar,changes);assert.equal(response.status,400,await response.clone().text());assert.equal(await readFile(`${folder}/review.json`,'utf8'),before);
 }
 response=await patch(human,{outline_pixels:replacement});assert.equal(response.status,409);assert.equal(await readFile(`${folder}/review.json`,'utf8'),before);
 response=await patch(solar,{outline_pixels:replacement,polygon_preview_id:'a'.repeat(64),notes:'Source-bound array correction'});assert.equal(response.status,200,await response.clone().text());const changed=await response.json();revision=changed.revision;
 assert.equal(changed.object.kind,'solar_panel');assert.equal(changed.object.parent_id,'parent');assert.deepEqual(changed.object.ring,replacement.ring);assert.equal(changed.object.geometry_reviewed,false);assert.equal(changed.object.model.height_verified,false);assert.equal(changed.object.model.solar,undefined);assert.equal(changed.object.status,'needs_adjustment');assert.equal(changed.object.provenance.tier,'T3');assert.equal(changed.object.provenance.human_confirmed,false);
 review=await saved();assert.deepEqual(review.objects.find(o=>o.id==='human'),savedHuman);assert.deepEqual(review.objects.find(o=>o.id==='parent'),savedParent);assert.equal(review.objects.find(o=>o.id==='human').fieldwork_import.pending,true);assert.equal(review.history.length,1);
 response=await fetch(`${base}/api/scenes/object-feedback?id=${id}&object=solar`);state=await response.json();assert.equal(state.objects[0].automated_polygon_edit_allowed,true);
 const after=await readFile(`${folder}/review.json`,'utf8');response=await patch(solar,{outline_pixels:replacement},{expected_revision:revision-1});assert.equal(response.status,409);assert.equal(await readFile(`${folder}/review.json`,'utf8'),after);
 const treeReplacement={ring:[[321,321],[419,321],[419,419],[321,419]],holes:[]};response=await patch(tree,{outline_pixels:treeReplacement,notes:'Source-bound canopy correction'});assert.equal(response.status,200,await response.clone().text());const treeChanged=await response.json();revision=treeChanged.revision;assert.equal(treeChanged.object.kind,'tree');assert.equal(treeChanged.object.model.tree,undefined);
 review=await saved();assert.equal(review.history.length,2);assert.deepEqual(review.objects.find(o=>o.id==='human'),savedHuman);
 review.objects.find(o=>o.id==='solar').notes='Human changed unchecked array feedback';
 response=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review})});assert.equal(response.status,200);revision=(await response.json()).review.revision;
 const humanSnapshot=await readFile(`${folder}/review.json`,'utf8');response=await patch({...solar,ring:replacement.ring},{outline_pixels:{ring:[[153,152],[247,152],[247,198],[153,198]],holes:[]}});assert.equal(response.status,409);assert.equal(await readFile(`${folder}/review.json`,'utf8'),humanSnapshot);
 assert.deepEqual(await readFile(`${folder}/latest.json`),modelBytes);assert.deepEqual(await readFile(`${folder}/${model.image_sha256}.png`),imageBytes);
 console.log('Live generic polygon API: source/kind/revision binding, solar/tree unchecked T3 drafts, class-derived estimate invalidation, invalid/protected/human-changed refusal, unchanged source/relationships/history/pending imports passed.');
}finally{await rm(folder,{recursive:true,force:true});}
