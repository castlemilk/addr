import assert from 'node:assert/strict';
import {readFile,mkdir,writeFile,cp,rm} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_roof_patch_${Date.now()}`,folder=`data/scenes/${id}`;
const source='data/scenes/roof_training_250-29790';
const model=JSON.parse(await readFile(`${source}/latest.json`,'utf8'));
const transform=(p,m)=>[p[0]*m[0][0]+p[1]*m[1][0]+m[2][0],p[0]*m[0][1]+p[1]*m[1][1]+m[2][1]];
const building=structuredClone(model.objects.find(o=>o.kind==='building'));
Object.assign(building,{id:'roof',label:'Draft patch fixture',ring:[[100,100],[400,100],[400,300],[100,300]],holes:[],status:'candidate',geometry_reviewed:false,roof_parts:undefined,roof_lines:undefined,roof_annotation:null,roof_type:undefined,group_id:undefined,parent_id:undefined,roof_feedback:undefined,roof_line_edits:undefined});
building.model.height_verified=false;
const other=structuredClone(building);other.id='other';other.notes='Preserve this note';other.provenance={source:'reviewer:fixture'};other.fieldwork_import={pending:true,origin:'http://example.test',projectId:'fixture',itemId:'other',revision:1,cursor:'1'};
model.id=id;model.groups=[];model.objects=[building,other];
await mkdir(folder);await writeFile(`${folder}/latest.json`,JSON.stringify(model));await cp(`${source}/${model.image_sha256}.png`,`${folder}/${model.image_sha256}.png`);
let revision=0;
const binding=()=>({scene_id:id,object_id:'roof',expected_revision:revision,model_signature:model.model_signature,image_sha256:model.image_sha256,outline_pixels:{ring:building.ring,holes:[]}});
const patch=(changes,override={})=>fetch(`${base}/api/scenes/roof-feedback`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({...binding(),changes,...override})});
const saved=async()=>JSON.parse(await readFile(`${folder}/review.json`,'utf8'));
try{
 let response=await patch({notes:'Draft note only'});assert.equal(response.status,200,await response.clone().text());let result=await response.json();revision=result.revision;
 assert.equal(revision,1);assert.equal(result.training_eligible,false);assert.equal(result.human_approval,false);assert.equal(result.object.status,'candidate');assert.equal(result.object.notes,'Draft note only');
 let review=await saved();const expectedOther=JSON.parse(JSON.stringify(other));
 for(const key of ['width_m','length_m','area_m2','rotation_deg','footprint_area_px'])expectedOther.model[key]=review.objects[1].model[key];
 assert.deepEqual(review.objects[1],expectedOther);assert.equal(review.objects[1].fieldwork_import.pending,true);
 const savedOther=structuredClone(review.objects[1]);
 const lines={vertices_meters:[[150,200],[350,200]].map(p=>transform(p,model.pixel_to_meters)),edges:[{a:0,b:1,kind:'ridge'}],notes:'Uncertain visible ridge'};
 response=await patch({roof_line_edits:lines,review_issues:['shape_3d']});assert.equal(response.status,200,await response.clone().text());result=await response.json();revision=result.revision;
 assert.equal(revision,2);assert.equal(result.object.roof_line_edits.checked,false);assert.equal(result.object.geometry_reviewed,false);assert.equal(result.object.status,'needs_adjustment');assert.equal(result.object.model.height_verified,false);
 assert.equal(result.object.provenance.source,'machine:mcp-roof-feedback-draft-v1');assert.equal(result.object.provenance.tier,'T3');assert.match(result.object.provenance.draft_fingerprint,/^[a-f0-9]{64}$/);
 const classification=JSON.parse(execFileSync('scripts/ml/.venv/bin/python',['-c','import json,sys; from scripts.ml.reference_registry import review_tier; p=json.load(sys.stdin); print(json.dumps(review_tier(p["object"],p["model_ring"],model_holes=[])))'],{input:JSON.stringify({object:result.object,model_ring:building.ring}),encoding:'utf8'}));assert.deepEqual(classification,['T3','review:machine_label',false]);
 assert.equal((await saved()).history.length,1);assert.deepEqual((await saved()).objects[1],savedOther);
 const originalBaseline=structuredClone(result.object.roof_line_edits.baseline);
 response=await patch({roof_line_edits:{...lines,vertices_meters:[[150,210],[350,210]].map(p=>transform(p,model.pixel_to_meters)),notes:'Moved draft'}});assert.equal(response.status,200);result=await response.json();revision=result.revision;assert.deepEqual(result.object.roof_line_edits.baseline,originalBaseline);
 const before=await readFile(`${folder}/review.json`,'utf8');
 for(const [changes,override,status] of [[{notes:'stale'},{expected_revision:revision-1},409],[{notes:'wrong model'},{model_signature:'f'.repeat(64)},409],
  [{notes:'wrong image'},{image_sha256:'f'.repeat(64)},409],[{notes:'wrong outline'},{outline_pixels:{ring:[[100,100],[401,100],[401,300],[100,300]],holes:[]}},409],
  [{notes:'missing object'},{object_id:'absent'},404],[{status:'confirmed'},{},400],[{roof_line_edits:{...lines,checked:true}},{},400],
  [{roof_line_edits:{...lines,baseline:{vertices:[],edges:[]}}},{},400],[{roof_line_edits:{...lines,edges:[{a:0,b:999,kind:'ridge'}]}},{},400],
  [{roof_line_edits:{...lines,vertices_meters:[[0,0],[350,200]].map(p=>transform(p,model.pixel_to_meters))}},{},400]]) {
  response=await patch(changes,override);assert.equal(response.status,status,await response.clone().text());assert.equal(await readFile(`${folder}/review.json`,'utf8'),before);
 }
 response=await fetch(`${base}/api/scenes/roof-feedback`,{method:'PATCH',headers:{'Content-Type':'application/json',Origin:'http://other.test'},body:JSON.stringify({...binding(),changes:{notes:'CSRF'}})});assert.equal(response.status,403);
 const races=await Promise.all([patch({notes:'First racing draft'}),patch({notes:'Second racing draft'})]);assert.equal(races.filter(r=>r.status===200).length,1);assert(races.every(r=>[200,409,503].includes(r.status)));revision=(await saved()).revision;
 const sourceImage=await readFile(`${folder}/${model.image_sha256}.png`);await writeFile(`${folder}/${model.image_sha256}.png`,'changed image');assert.equal((await patch({notes:'Damaged source'})).status,409);await writeFile(`${folder}/${model.image_sha256}.png`,sourceImage);
 assert.deepEqual(JSON.parse(await readFile(`${folder}/latest.json`,'utf8')),JSON.parse(JSON.stringify(model)));
 await writeFile(`${folder}/latest.json`,JSON.stringify({...model,model_signature:'f'.repeat(64)}));assert.equal((await patch({notes:'No implicit recovery'})).status,409);
 console.log('Roof feedback PATCH API: canonical history, source image/revision/outline binding, strict draft writes, baseline/import preservation, concurrent saves, origin guard and immutable prediction passed.');
}finally{await rm(folder,{recursive:true,force:true});}
