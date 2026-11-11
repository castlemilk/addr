import assert from 'node:assert/strict';
import ts from 'typescript';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const req=createRequire(import.meta.url),cache=new Map();
function load(name){if(cache.has(name))return cache.get(name);const exports={};cache.set(name,exports);
 const source=ts.transpileModule(readFileSync(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
 new Function('exports','require',source)(exports,id=>id.startsWith('./')?load(id.slice(2)):req(id));return exports;}
const {parseRoofFeedbackPatch:parse,patchedRoofFeedback:apply,polygonDraftFingerprint:fingerprint}=load('roof-feedback-patch');
const {currentRoofFeedback}=load('scene-roof-feedback');
const frame=[[1,0],[0,1],[0,0]],ring=[[0,0],[10,0],[10,10],[0,10]],outline={ring,holes:[]};
const graph={vertices:[[1,5],[9,5]],edges:[{a:0,b:1,kind:'ridge'}]};
const roof={id:'roof',kind:'building',label:'Fixture',ring,holes:[],status:'confirmed',geometry_reviewed:true,source:'manual-review',visibility:'full',model:{height_m:3,height_verified:true},
 roof_line_edits:{schema_version:1,outline,baseline:graph,...graph,checked:true,notes:'Original line note'},fieldwork_import:{pending:true,itemId:'unchanged-import'},provenance:{source:'reviewer:original'},roof_feedback:{outline,family:'gabled'}};
const other={...roof,id:'other',notes:'Unrelated note'};
const data={model:{id:'scene',model_signature:'a'.repeat(64),image_sha256:'b'.repeat(64),pixel_to_meters:frame,image_width:100,image_height:100,objects:[roof,other],groups:[]},review:{revision:5,model_signature:'a'.repeat(64),objects:[roof,other],groups:[{id:'g',label:'Preserve group'}],reviewed_classes:['building','tree'],basemap_mode:'raw'},stale:false};
const patch=changes=>({scene_id:'scene',object_id:'roof',expected_revision:5,model_signature:data.model.model_signature,image_sha256:data.model.image_sha256,outline_pixels:outline,changes});
const before=structuredClone(data);
const notes=apply(data,parse(patch({notes:'A source-bound note'})));
assert.equal(notes.objects[0].notes,'A source-bound note');assert.equal(notes.objects[0].status,'confirmed');assert.equal(notes.objects[0].geometry_reviewed,true);assert.equal(notes.objects[0].roof_line_edits.checked,true);
assert.deepEqual(notes.objects[0].provenance,roof.provenance,'Notes-only updates preserve existing human authority');
assert.deepEqual(notes.objects[1],other);assert.deepEqual(notes.reviewed_classes,['building','tree']);assert.equal(notes.basemap_mode,'raw');
const changed=apply(data,parse(patch({roof_line_edits:{vertices_meters:[[1,6],[9,6]],edges:[{a:0,b:1,kind:'valley'}]}})));
assert.equal(changed.objects[0].status,'needs_adjustment');assert.equal(changed.objects[0].geometry_reviewed,false);assert.equal(changed.objects[0].model.height_verified,false);
assert.equal(changed.objects[0].roof_line_edits.checked,false);assert.deepEqual(changed.objects[0].roof_line_edits.baseline,graph);assert.deepEqual(changed.objects[0].roof_line_edits.outline,outline);assert.equal(changed.objects[0].roof_line_edits.notes,'Original line note');
assert.deepEqual(changed.objects[0].fieldwork_import,roof.fieldwork_import);assert.deepEqual(changed.objects[0].provenance.previous_provenance,roof.provenance);assert.deepEqual(changed.objects[0].roof_feedback,roof.roof_feedback);
assert.equal(changed.objects[0].provenance.source,'machine:mcp-roof-feedback-draft-v1');assert.equal(changed.objects[0].provenance.tier,'T3');assert.equal(changed.objects[0].provenance.draft_fingerprint,fingerprint(changed.objects[0]));
assert.equal(currentRoofFeedback({...changed.objects[0],roof_feedback:{outline,family:'gabled',linework:{vertices:graph.vertices,edges:graph.edges,faces:[]}}}).linework,false,'An unchecked XY draft must not revive a prior linework attestation');
assert.deepEqual(changed.objects[1],other);assert.deepEqual(changed.groups,data.review.groups);assert.deepEqual(changed.reviewed_classes,['tree']);assert.deepEqual(data,before);
const issues=apply(data,parse(patch({review_issues:['height']})));assert.deepEqual(issues.objects[0].review_issues,['height']);assert.equal(issues.objects[0].model.height_verified,false);assert.equal(issues.objects[0].status,'needs_adjustment');
for(const input of [null,[],{...patch({notes:''}),unexpected:true},{...patch({notes:''}),expected_revision:-1},{...patch({notes:''}),scene_id:'../unsafe'},
 patch({}),patch({notes:1}),patch({review_issues:['height','height']}),patch({review_issues:['unknown']}),patch({status:'confirmed'}),
 patch({roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges,checked:true}}),patch({roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges,baseline:graph}}),
 patch({roof_line_edits:{vertices_meters:[[NaN,0]],edges:[]}}),patch({roof_line_edits:{vertices_meters:graph.vertices,edges:[{...graph.edges[0],source:'forged'}]}})])assert.throws(()=>parse(input));
for(const overrides of [{expected_revision:4},{model_signature:'c'.repeat(64)},{image_sha256:'c'.repeat(64)},{outline_pixels:{ring:[[0,0],[11,0],[11,10],[0,10]],holes:[]}}])assert.throws(()=>apply(data,parse({...patch({notes:'No save'}),...overrides})),e=>e.status===409);
assert.throws(()=>apply({...data,stale:true},parse(patch({notes:'No recovery'}))),e=>e.status===409);
assert.throws(()=>apply(data,parse({...patch({notes:'No save'}),object_id:'absent'})),e=>e.status===404);
for(const object of [{...roof,kind:'tree'},{...roof,status:'rejected'},{...roof,status:'superseded'}])assert.throws(()=>apply({...data,review:{...data.review,objects:[object]}},parse(patch({notes:'No save'}))));
const staleLines={...roof,roof_line_edits:{...roof.roof_line_edits,outline:{ring:[[0,0],[11,0],[11,10],[0,10]],holes:[]}}};
assert.throws(()=>apply({...data,review:{...data.review,objects:[staleLines]}},parse(patch({roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges}}))),e=>e.status===409);
for(const lines of [{vertices_meters:[[0,5],[20,5]],edges:graph.edges},{vertices_meters:graph.vertices,edges:[{a:0,b:999,kind:'ridge'}]}])assert.throws(()=>apply(data,parse(patch({roof_line_edits:lines}))));
const first={...roof};delete first.roof_line_edits;first.roof_annotation={schema_version:1,roof_type:'gabled',vertices:[[0,5,5],[10,5,5]],faces:[],edges:graph.edges};
const firstData={...data,review:null,model:{...data.model,objects:[first]}};
const initial=apply(firstData,parse({...patch({roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges,notes:'New draft'}}),expected_revision:0}));
assert.deepEqual(initial.objects[0].roof_line_edits.baseline,{vertices:[[0,5],[10,5]],edges:graph.edges});assert.equal(initial.objects[0].roof_line_edits.notes,'New draft');assert.equal(initial.objects[0].roof_line_edits.checked,false);
// Polygon drafts retain source snapshots while invalidating every footprint-derived estimate.
const candidate=structuredClone(roof);Object.assign(candidate,{status:'candidate',geometry_reviewed:false,source:'sam3-text-onnx',roof_annotation:{estimate:true},roof_parts:[{estimate:true}],roof_lines:[{estimate:true}],roof_type:'hipped',roof_geometry_source:'image-guided-prior',roof_geometry_note:'Old estimate'});candidate.model.height_verified=false;
for(const key of ['provenance','fieldwork_import','roof_feedback','roof_line_edits'])delete candidate[key];
const polygonData={...data,review:null,model:{...data.model,objects:[candidate,other]}};
const replacement={ring:[[0,0],[9,0],[9,10],[0,10]],holes:[[[2,2],[3,2],[3,3],[2,3]]]};
const polygonPatch=changes=>parse({...patch(changes),expected_revision:0});
const polygon=apply(polygonData,polygonPatch({outline_pixels:replacement,polygon_preview_id:'d'.repeat(64)}),'fixture-actor');
const output=polygon.objects[0];assert.deepEqual(output.ring,replacement.ring);assert.deepEqual(output.holes,replacement.holes);
for(const key of ['roof_parts','roof_lines','roof_type','roof_feedback','roof_geometry_source','roof_geometry_note'])assert.equal(output[key],undefined);
assert.equal(output.roof_annotation,null);assert.equal(output.visibility,undefined);assert.equal(output.status,'needs_adjustment');assert.equal(output.geometry_reviewed,false);assert.equal(output.model.height_verified,false);
assert.equal(output.provenance.source,'machine:mcp-polygon-draft-v1');assert.equal(output.provenance.tier,'T3');assert.equal(output.provenance.actor,'fixture-actor');assert.equal(output.provenance.preview_id,'d'.repeat(64));
assert.equal(output.provenance.human_confirmed,false);assert.equal(output.provenance.human_approval,false);assert.equal(output.provenance.training_eligible,false);assert.deepEqual(output.provenance.source_outline_pixels,outline);
assert.deepEqual(polygon.objects[1],other);assert.deepEqual(polygonData.model.objects[0],candidate);assert.deepEqual(polygonData.model.objects[0].ring,ring);
const machineDraft={...candidate,provenance:{source:'machine:previous-agent',confidence:.5},roof_line_edits:{...structuredClone(roof.roof_line_edits),checked:false}};
machineDraft.provenance.draft_fingerprint=fingerprint(machineDraft);
const machineData={...polygonData,review:{revision:5,model_signature:data.model.model_signature,objects:[machineDraft,other],groups:[],reviewed_classes:[]}};
const moved=apply(machineData,parse(patch({outline_pixels:replacement})));
assert.deepEqual(moved.objects[0].roof_line_edits, machineDraft.roof_line_edits,'Saved line baseline and original outline remain stale audit data');
assert.equal(load('roof-line-edits').currentRoofLineEdits(moved.objects[0]),null);assert.deepEqual(moved.objects[0].provenance.previous_provenance,machineDraft.provenance);
assert.equal(moved.objects[0].provenance.draft_fingerprint,fingerprint(moved.objects[0]));
// Ordinary CMS changes retain provenance but cannot retain machine authority over the changed draft.
for(const change of [object=>object.ring[1][0]=8.5,object=>object.notes='Human changed an unchecked draft',object=>object.review_issues=['outline'],
 object=>object.roof_annotation={human_estimate:true},object=>object.model.height_m=4,object=>object.roof_line_edits.notes='Human line note']) {
 const humanChanged=structuredClone(machineDraft);change(humanChanged);
 const changedData={...machineData,review:{...machineData.review,objects:[humanChanged,other]}};
 assert.throws(()=>apply(changedData,parse({...patch({outline_pixels:replacement}),outline_pixels:{ring:humanChanged.ring,holes:humanChanged.holes||[]}})),e=>e.status===409);
}
const legacyMachine=structuredClone(machineDraft);delete legacyMachine.provenance.draft_fingerprint;
assert.throws(()=>apply({...machineData,review:{...machineData.review,objects:[legacyMachine]}},parse(patch({outline_pixels:replacement}))),e=>e.status===409);
const machineNote=apply(machineData,parse(patch({notes:'Another explicitly machine-authored note'})));
assert.equal(machineNote.objects[0].provenance.draft_fingerprint,fingerprint(machineNote.objects[0]));
for(const change of [object=>object.geometry_reviewed=true,object=>object.status='confirmed',object=>object.source='reviewer-trace']) {
 const checkedMachine=structuredClone(machineDraft);change(checkedMachine);checkedMachine.provenance.draft_fingerprint=fingerprint(checkedMachine);
 assert.throws(()=>apply({...machineData,review:{...machineData.review,objects:[checkedMachine]}},parse(patch({outline_pixels:replacement}))),e=>e.status===409&&e.message.includes('human-reviewed'));
}
const computed=structuredClone(machineDraft);Object.assign(computed.model,{width_m:20,length_m:30,area_m2:900,rotation_deg:123,footprint_area_px:12});assert.equal(fingerprint(computed),machineDraft.provenance.draft_fingerprint);
for(const outline_pixels of [outline,{ring:[[0,0],[10,10],[0,10],[10,0]],holes:[]},{ring:[[0,0],[101,0],[101,10],[0,10]],holes:[]},{ring:[[0,0],[10,0],[10,10],[0,10]],holes:[[[8,8],[11,8],[11,11],[8,11]]]}])
 assert.throws(()=>apply(polygonData,polygonPatch({outline_pixels})),e=>e.status===400);
for(const object of [roof,{...candidate,geometry_reviewed:true},{...candidate,model:{...candidate.model,height_verified:true}},
 {...candidate,roof_line_edits:roof.roof_line_edits},{...candidate,fieldwork_import:{pending:true}},
 {...candidate,provenance:{source:'machine:previous-agent',human_confirmed:true}}, {...candidate,source:'reviewer-trace'}])
 assert.throws(()=>apply({...polygonData,model:{...polygonData.model,objects:[object]}},polygonPatch({outline_pixels:replacement})),e=>e.status===409);
assert.throws(()=>apply({...polygonData,review:{...machineData.review,objects:[{...candidate,notes:'Human draft'}]}},parse(patch({outline_pixels:replacement}))),e=>e.status===409);
for(const scene_id of ['house-30286','house-30296'])assert.throws(()=>apply({...polygonData,model:{...polygonData.model,id:scene_id}},parse({...patch({outline_pixels:replacement}),scene_id,expected_revision:0})),e=>e.status===409);
for(const changes of [{outline_pixels:replacement,roof_line_edits:{vertices_meters:graph.vertices,edges:graph.edges}},{outline_pixels:replacement,provenance:{source:'reviewer'}},{polygon_preview_id:'d'.repeat(64)}])assert.throws(()=>parse(patch(changes)));
console.log('Roof feedback patch: source/revision/outline guards, unchecked drafts, immutable baseline/provenance, polygon validation/estimate invalidation, protected human feedback and tier-3 machine drafts passed.');
