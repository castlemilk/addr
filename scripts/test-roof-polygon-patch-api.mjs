import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile,cp,rm} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_roof_polygon_${Date.now()}`,folder=`data/scenes/${id}`,source='data/scenes/roof_training_250-29790';
const model=JSON.parse(await readFile(`${source}/latest.json`,'utf8'));
const roof=structuredClone(model.objects.find(o=>o.kind==='building'));
Object.assign(roof,{id:'roof',ring:[[100,100],[400,100],[400,300],[100,300]],holes:[],status:'candidate',geometry_reviewed:false,group_id:undefined,parent_id:undefined,roof_feedback:undefined,roof_line_edits:undefined});roof.model.height_verified=false;
const other=structuredClone(roof);other.id='other';
model.id=id;model.groups=[];model.objects=[roof,other];
let revision=0,currentOutline={ring:roof.ring,holes:[]};
const binding=(object_id='roof',outline_pixels=currentOutline)=>({scene_id:id,object_id,outline_pixels,expected_revision:revision,model_signature:model.model_signature,image_sha256:model.image_sha256});
const patch=(changes,overrides={})=>fetch(`${base}/api/scenes/roof-feedback`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({...binding(),changes,...overrides})});
const load=()=>fetch(`${base}/api/scenes/roof-feedback?id=${id}&object=roof`);
const saved=async()=>JSON.parse(await readFile(`${folder}/review.json`,'utf8'));
await mkdir(folder);await writeFile(`${folder}/latest.json`,JSON.stringify(model));await cp(`${source}/${model.image_sha256}.png`,`${folder}/${model.image_sha256}.png`);
try{
 let response=await fetch(`${base}/api/scenes/roof-feedback?id=${id}`);assert.equal(response.status,200);let state=await response.json();assert.equal(state.revision,0);assert.equal(state.objects.length,2);assert.equal(state.objects[0].is_saved_review,false);assert.equal(state.objects[0].automated_polygon_edit_allowed,true);
 response=await fetch(`${base}/api/scenes/roof-feedback?id=${id}&object=roof&include_model=true`);assert.equal(response.status,200);assert.equal((await response.json()).objects[0].is_saved_review,false);
 const transform=p=>[p[0]*model.pixel_to_meters[0][0]+p[1]*model.pixel_to_meters[1][0]+model.pixel_to_meters[2][0],p[0]*model.pixel_to_meters[0][1]+p[1]*model.pixel_to_meters[1][1]+model.pixel_to_meters[2][1]];
 const graph={vertices:[[150,200],[350,200]].map(transform),edges:[{a:0,b:1,kind:'ridge'}]};
 const machine=structuredClone(roof);
 const human=structuredClone(other);human.status='confirmed';human.geometry_reviewed=true;human.notes='A human-approved roof';human.fieldwork_import={pending:true,origin:'http://example.test',projectId:'fixture',itemId:'other',revision:1,cursor:'1'};
 response=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,objects:[machine,human],groups:[],reviewed_classes:[]}})});
 assert.equal(response.status,200,await response.clone().text());let review=(await response.json()).review;revision=review.revision;const savedOther=structuredClone(review.objects[1]);
 response=await patch({roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges,notes:'Retain original line baseline'}});assert.equal(response.status,200,await response.clone().text());revision=(await response.json()).revision;review=await saved();const oldLines=structuredClone(review.objects[0].roof_line_edits);
 const replacement={ring:[[105,100],[398,100],[398,298],[105,298]],holes:[[[180,160],[200,160],[200,180],[180,180]]]};
 const before=await readFile(`${folder}/review.json`,'utf8');
 for(const proposed of [{ring:[[100,100],[400,300],[100,300],[400,100]],holes:[]},{ring:[[-5,100],[400,100],[400,300],[100,300]],holes:[]},
  {ring:replacement.ring,holes:[[[0,0],[10,0],[10,10],[0,10]]]}]){
  response=await patch({outline_pixels:proposed});assert.equal(response.status,400,await response.clone().text());assert.equal(await readFile(`${folder}/review.json`,'utf8'),before);
 }
 response=await patch({outline_pixels:replacement},{object_id:'other'});assert.equal(response.status,409);assert.equal(await readFile(`${folder}/review.json`,'utf8'),before);
 response=await patch({outline_pixels:replacement,provenance:{source:'reviewer'}});assert.equal(response.status,400);
 response=await patch({outline_pixels:replacement,roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges}});assert.equal(response.status,400);
 response=await patch({outline_pixels:replacement,polygon_preview_id:'a'.repeat(64),notes:'Agent proposal: explicit polygon correction'});assert.equal(response.status,200,await response.clone().text());const result=await response.json();revision=result.revision;
 assert.equal(result.object.id,'roof');assert.deepEqual(result.object.ring,replacement.ring);assert.deepEqual(result.object.holes,replacement.holes);assert.equal(result.object.geometry_reviewed,false);assert.equal(result.object.model.height_verified,false);assert.equal(result.object.status,'needs_adjustment');
 assert.equal(result.object.provenance.source,'machine:mcp-polygon-draft-v1');assert.equal(result.object.provenance.tier,'T3');assert.equal(result.object.provenance.human_confirmed,false);assert.equal(result.object.provenance.preview_id,'a'.repeat(64));assert.deepEqual(result.object.provenance.source_outline_pixels,currentOutline);
 assert.deepEqual(result.object.roof_line_edits,oldLines);assert.equal(result.object.roof_annotation,null);for(const key of ['roof_parts','roof_lines','roof_type','roof_feedback'])assert.equal(result.object[key],undefined);
 review=await saved();assert.equal(review.history.length,2);assert.deepEqual(review.history[1].objects[0].ring,currentOutline.ring);assert.deepEqual(review.objects[1],savedOther);assert.equal(review.objects[1].fieldwork_import.pending,true);
 const classification=JSON.parse(execFileSync('scripts/ml/.venv/bin/python',['-c','import json,sys; from scripts.ml.reference_registry import review_tier; p=json.load(sys.stdin); print(json.dumps(review_tier(p["object"],p["model_ring"],model_holes=[])))'],{input:JSON.stringify({object:result.object,model_ring:roof.ring}),encoding:'utf8'}));assert.deepEqual(classification,['T3','review:machine_label',false]);
 state=await (await load()).json();assert.equal(state.objects[0].feedback_status,'stale-outline');assert.equal(state.objects[0].usable_as_review_constraints,false);assert.equal(state.objects[0].automated_polygon_edit_allowed,true);
 const after=await readFile(`${folder}/review.json`,'utf8');response=await patch({outline_pixels:replacement});assert.equal(response.status,409);assert.equal(await readFile(`${folder}/review.json`,'utf8'),after,'The original binding cannot overwrite a newer polygon');
 currentOutline=replacement;const next={ring:[[106,101],[397,101],[397,297],[106,297]],holes:replacement.holes};response=await patch({outline_pixels:next});assert.equal(response.status,200);const continued=await response.json();assert.equal(continued.revision,revision+1);assert.deepEqual(continued.object.roof_line_edits.baseline,oldLines.baseline);revision=continued.revision;currentOutline=next;
 // A normal CMS save changes unchecked draft notes without refreshing its machine fingerprint.
 const intervening=await saved();intervening.objects[0].notes='Human edited this unchecked draft';
 response=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:intervening})});assert.equal(response.status,200);const humanChanged=(await response.json()).review;revision=humanChanged.revision;
 const humanSnapshot=await readFile(`${folder}/review.json`,'utf8');response=await patch({outline_pixels:{ring:[[107,102],[396,102],[396,296],[107,296]],holes:next.holes}});assert.equal(response.status,409);assert.equal(await readFile(`${folder}/review.json`,'utf8'),humanSnapshot);
 state=await(await load()).json();assert.equal(state.objects[0].automated_polygon_edit_allowed,false);assert.match(state.objects[0].automated_polygon_edit_reason,/edited|authorship/);
 assert.deepEqual(JSON.parse(await readFile(`${folder}/latest.json`,'utf8')),JSON.parse(JSON.stringify(model)));
 console.log('Polygon PATCH API: original/persisted bindings, invalid polygons and protected-human refusal, machine/T3 authority exclusion, derivative invalidation, baseline/history/import preservation, stale source rejection and continued draft editing passed.');
}finally{await rm(folder,{recursive:true,force:true});}
