import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import ts from 'typescript';
import * as THREE from 'three';

const require = createRequire(import.meta.url), cache = new Map();
const repository = process.cwd();
function load(name) {
  if (cache.has(name)) return cache.get(name);
  const exports = {};
  cache.set(name, exports);
  const source = ts.transpileModule(readFileSync(path.join(repository,`src/lib/${name}.ts`), 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop:true },
  }).outputText;
  new Function('exports', 'require', source)(exports, id => id.startsWith('.')
    ? load(path.posix.normalize(path.posix.join(path.posix.dirname(name),id))) : id==='three' ? THREE : require(id));
  return exports;
}
const { isShadeSail, hasConventionalRoof, withStructureKind, clearConventionalRoofDraft } = load('scene-structure');
const { populateScene } = load('scene-geometry');
const { roofMount, parentRoofMesh } = load('scene-roof-mount');
const { sceneRoofGraph, withObjectRoofAnnotation } = load('scene-height');
const { roofImageLines } = load('scene-roof-lines');
const { currentRoofFeedback, withRoofFeedback, validRoofFeedback } = load('scene-roof-feedback');
const { recoverSceneReview } = load('scene-feedback');
const { sceneVisibility } = load('scene-visibility');
const { availableParents } = load('scene-instances');
const { currentRoofLineEdits, estimatedLineGraph, initialRoofLineEdits, withRoofLineEdits } = load('roof-line-edits');
const { parseRoofFeedbackPatch, parseObjectPolygonPatch, patchedRoofFeedback, patchedObjectPolygon, polygonDraftFingerprint } = load('roof-feedback-patch');
const ring = [[10, 10], [30, 10], [30, 30], [10, 30]], frame = [[1, 0], [0, 1], [0, 0]];
const graph = { schema_version: 1, roof_type: 'hipped', vertices: [[10,10,3],[30,10,3],[30,30,3],[10,30,3],[20,20,6]],
  faces: [[0,1,4],[1,2,4],[2,3,4],[3,0,4]], edges: [{ a:0, b:4, kind:'hip' }] };
const roof = { id:'roof', kind:'building', label:'Building', ring, holes:[], status:'confirmed', geometry_reviewed:true,
  source:'fixture', parent_id:'structure-parent', group_id:'g', visibility:'full',
  model:{ height_m:3, base_m:0, height_source:'class-prior', height_verified:true, color:'#8899aa' },
  roof_type:'hipped', roof_annotation:graph, roof_parts:[{ ring_meters:ring, ring_z:[3,3,3,3] }],
  roof_lines:[{ kind:'hip', line_meters:[[10,10],[20,20]] }], roof_geometry_source:'image-guided-prior',
  roof_feedback:{ outline:{ ring, holes:[] }, family:'hipped' }, roof_form_evidence:{ decision:'pitched' } };
const before = structuredClone(roof), corrected = withStructureKind(roof,'shade-sail','Fabric shade sail');
assert(isShadeSail(corrected)); assert(!hasConventionalRoof(corrected)); assert(hasConventionalRoof(roof));
assert.deepEqual(corrected.ring,ring); assert.equal(corrected.parent_id,roof.parent_id); assert.equal(corrected.group_id,'g');
assert.equal(corrected.geometry_reviewed,false); assert.equal(corrected.status,'needs_adjustment'); assert.equal(corrected.model.height_verified,false);
for(const key of ['roof_type','roof_parts','roof_annotation','roof_lines','roof_feedback','roof_form_evidence','roof_geometry_source'])
  assert.equal(corrected[key],undefined);
assert.deepEqual(roof,before,'Subtype correction does not mutate the source or retained history');

// Imported old feedback may still contain pitched geometry: consumers must refuse it without depending on UI cleanup.
const stale = { ...roof, structure_kind:'shade-sail' };
assert.equal(sceneRoofGraph(stale),null); assert.equal(withObjectRoofAnnotation(stale,graph),stale);
assert.deepEqual(parentRoofMesh(stale,frame),[]); assert.deepEqual(roofImageLines(stale,frame),[]);
assert.deepEqual(currentRoofFeedback(stale),{}); assert.equal(withRoofFeedback(stale,{family:'hipped'}),stale);
assert.equal(validRoofFeedback(stale),false,'Incompatible conventional-roof checks cannot be saved as current sail feedback');
const oldLines = initialRoofLineEdits(roof), staleLined = { ...stale,roof_line_edits:oldLines };
assert.equal(currentRoofLineEdits(staleLined),null); assert.deepEqual(estimatedLineGraph(staleLined),{vertices:[],edges:[]});
assert.throws(()=>initialRoofLineEdits(stale),/Conventional roof lines/);
assert.throws(()=>withRoofLineEdits(stale,oldLines),/Conventional roof lines/);
const scene = { id:'fixture', model_signature:'old', image_sha256:'image', image_width:50,image_height:50,
  pixel_to_meters:frame, meters_to_pixel:frame,objects:[roof],groups:[] };
const root = new THREE.Group();
populateScene(root,[stale],scene,'roof',new Set(['building']));
assert.equal(root.children.length,1,'A sail has neither building walls nor rigid roof edges');
assert.equal(root.children[0].geometry.type,'ShapeGeometry'); assert.equal(root.children[0].userData.geometryRole,'visible-footprint');
const position = root.children[0].geometry.attributes.position;
assert.equal(new Set(Array.from({length:position.count},(_,i)=>position.getZ(i))).size,1,'No inherited ridge or extruded body survives');
const panel = { ...roof, id:'panel', kind:'solar_panel', parent_id:'roof' };
const mounting = roofMount(panel,[stale,panel],frame);
assert.deepEqual(mounting.positions,[]); assert.match(mounting.issue,/fabric shade sail/);
assert.equal(sceneVisibility(stale,scene).footprintOnly,true,'A sail exposes no height or apex drag handles');
{
  const exports = {};
  const source = ts.transpileModule(readFileSync(path.join(repository,'src/components/scenes/SceneVisibilityControls.tsx'),'utf8'), {
    compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX},
  }).outputText;
  new Function('exports','require',source)(exports,id=>id.startsWith('@/lib/')?load(id.slice('@/lib/'.length)):require(id));
  const { createElement } = require('react'), { renderToStaticMarkup } = require('react-dom/server');
  const render = object => renderToStaticMarkup(createElement(exports.default,{object,scene,onChange:()=>{}}));
  const notice = render(stale);
  assert.match(notice,/Fabric shade sail/); assert.match(notice,/Membrane curvature and suspension heights are not modelled/);
  assert.doesNotMatch(notice,/Partial outline|canopy and trunk|from this fragment/,'A complete sail is not described as a clipped tree');
  const clippedTree = render({...roof,kind:'tree',structure_kind:undefined,visibility:'truncated'});
  assert.match(clippedTree,/Partial outline/); assert.match(clippedTree,/canopy and trunk/,'Tree fragment guidance remains available');
}
assert(!availableParents(panel,[stale,panel]).some(o=>o.id==='roof'),'A sail is not offered as a solar/skylight/chimney support');
const normal = new THREE.Group(); populateScene(normal,[roof],scene,'roof',new Set(['building']));
assert(normal.children.length>1,'Ordinary rigid roofs retain their roof and wall meshes');

// A subtype-only correction must survive identical-image model recovery, including replacement detections.
const subtypeOnly = { ...roof, structure_kind:'shade-sail' };
const saved = { revision:1,model_signature:'old',objects:[subtypeOnly],groups:[],reviewed_classes:[] };
const current = { ...scene,model_signature:'next' };
const recovered = recoverSceneReview(scene,current,saved);
assert.equal(recovered.carried_objects,1);
assert.equal(recovered.review.objects[0].structure_kind,'shade-sail');
assert.equal(recovered.review.objects[0].roof_annotation,undefined);
assert.equal(recovered.review.objects[0].status,'needs_adjustment');
const replacement = { ...roof,id:'fuller',replaces:['roof'] };
const inherited = recoverSceneReview(scene,{...current,objects:[replacement]},saved).review.objects[0];
assert.equal(inherited.id,'fuller'); assert.equal(inherited.structure_kind,'shade-sail'); assert.equal(inherited.roof_parts,undefined);
assert.deepEqual(clearConventionalRoofDraft(corrected),corrected,'Cleaning is idempotent');

// The canonical API validator removes incompatible fields for clients without the UI helper.
// Its transaction must authorize that normalized change, even when the caller submitted an unchanged stale review.
const directory = await mkdtemp(path.join(tmpdir(),'addr-shade-sail-save-'));
try {
  process.chdir(directory);
  const { validateSceneReview, saveScene } = load('scene-store');
  const bare = { ...stale,parent_id:undefined,group_id:undefined,model:{...stale.model,rotation_deg:0} };
  const draft = { revision:0,model_signature:'old',objects:[bare],groups:[],reviewed_classes:[] };
  const normalized = validateSceneReview(draft,scene).objects[0];
  assert.equal(normalized.roof_type,undefined); assert.equal(normalized.roof_feedback,undefined);
  assert.equal(normalized.roof_annotation,undefined); assert.equal(normalized.model.height_verified,false);
  assert.equal(normalized.status,'confirmed','Existing image outline authority remains separate from roof-family authority');
  assert.equal(draft.objects[0].roof_type,'hipped','Validation leaves the request and history immutable');
  await mkdir('data/scenes/fixture',{recursive:true});
  await writeFile('data/scenes/fixture/latest.json',JSON.stringify({...scene,objects:[bare]}));
  const existing = {...draft,revision:1};
  await writeFile('data/scenes/fixture/review.json',JSON.stringify(existing));
  await assert.rejects(saveScene('fixture',existing,{principal:'viewer',scopes:['crm.read']}),e=>e.status===403,
    'Normalized removals require editing permission even if the submitted old state was unchanged');
  assert.equal(JSON.parse(await readFile('data/scenes/fixture/review.json','utf8')).objects[0].roof_type,'hipped');
  const stored = await saveScene('fixture',existing,{principal:'fixture',scopes:['crm.read','crm.roof.write']});
  assert.equal(stored.revision,2); assert.equal(stored.objects[0].roof_type,undefined);
  assert.equal(stored.history[0].objects[0].roof_type,'hipped');
  assert.equal(JSON.parse(await readFile('data/scenes/fixture/latest.json','utf8')).objects[0].roof_type,'hipped');

  const candidate = { ...corrected,parent_id:undefined,group_id:undefined,model:{...corrected.model,rotation_deg:0} };
  const patchModel = { ...scene,model_signature:'a'.repeat(64),image_sha256:'b'.repeat(64),objects:[candidate] };
  const patchData = { model:patchModel,review:null,stale:false };
  const binding = { scene_id:'fixture',object_id:'roof',expected_revision:0,model_signature:patchModel.model_signature,
    image_sha256:patchModel.image_sha256,outline_pixels:{ring,holes:[]} };
  const rigid = parseRoofFeedbackPatch({...binding,changes:{roof_line_edits:{vertices_meters:[[15,20],[25,20]],edges:[{a:0,b:1,kind:'ridge'}]}}});
  assert.throws(()=>patchedRoofFeedback(patchData,rigid),e=>e.status===400&&/shade sails/.test(e.message));
  const replacementRing = [[11,10],[30,10],[30,30],[11,30]];
  const generic = parseObjectPolygonPatch({...binding,changes:{outline_pixels:{ring:replacementRing,holes:[]}}});
  const polygon = patchedObjectPolygon(patchData,generic);
  const normalizedPolygon = validateSceneReview(polygon,patchModel);
  assert.equal(normalizedPolygon.objects[0].structure_kind,'shade-sail');
  assert.equal(normalizedPolygon.objects[0].provenance.draft_fingerprint,polygonDraftFingerprint(normalizedPolygon.objects[0]),
    'Canonical sail cleanup does not invalidate generic boundary-tool authorship');
  const continued = patchedObjectPolygon({...patchData,review:{...normalizedPolygon,revision:1}},parseObjectPolygonPatch({...binding,expected_revision:1,
    outline_pixels:{ring:replacementRing,holes:[]},changes:{outline_pixels:{ring:[[12,10],[30,10],[30,30],[12,30]],holes:[]}}}));
  assert.equal(continued.objects[0].ring[0][0],12,'Saved sail boundary drafts remain editable through generic tools');
} finally {
  process.chdir(repository);
  await rm(directory,{recursive:true,force:true});
}

// Exercise actual route handlers with bounded in-memory scene and process stubs. No inference or service is started.
let routeScene = { model:{...scene,model_signature:'a'.repeat(64),image_sha256:'b'.repeat(64)},
  review:{...saved,model_signature:'a'.repeat(64),objects:[staleLined]},stale:false };
let executions = 0, commits = 0;
const execute = async () => { executions++; return {stdout:JSON.stringify({annotation:graph,audit:{passed:true}})}; };
function route(source) {
  const exports = {};
  const code = ts.transpileModule(readFileSync(path.join(repository,source),'utf8'), {
    compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true},
  }).outputText;
  new Function('exports','require',code)(exports,id=>{
    if(id==='next/server')return {NextResponse:{json:(body,init)=>Response.json(body,init)}};
    if(id==='node:util')return {promisify:()=>execute};
    if(id==='@/lib/scene-store')return {scenePrincipal:async()=>({principal:'fixture',scopes:['crm.read','crm.roof.write']}),
      loadScene:async()=>routeScene,scenePath:()=>'/private/tmp/unused-shade-sail',
      validateSceneReview:load('scene-store').validateSceneReview,
      sceneReviewTransaction:async(_id,_principal,operation)=>operation(routeScene,async value=>{commits++;return value;})};
    return id.startsWith('@/lib/')?load(id.slice('@/lib/'.length)):require(id);
  });
  return exports;
}
const roofRoute = route('src/app/api/scenes/roof/route.ts');
const request = object => ({json:async()=>({id:'fixture',form:'hipped',rise:2,revision:1,model_signature:routeScene.model.model_signature,object})});
let response = await roofRoute.POST(request({...stale,model:{...stale.model,rotation_deg:0}}));
assert.equal(response.status,400); assert.match((await response.json()).error,/shade sails/);
response = await roofRoute.POST(request({...roof,model:{...roof.model,rotation_deg:0}}));
assert.equal(response.status,400); assert.match((await response.json()).error,/saved object/);
assert.equal(executions,0,'Neither submitted nor saved shade-sail classification may start a rigid roof process');
const feedbackRoute = route('src/app/api/scenes/roof-feedback/route.ts');
response = await feedbackRoute.GET({nextUrl:{searchParams:new URLSearchParams({id:'fixture',object:'roof'})}});
assert.equal(response.status,200);
const row = (await response.json()).objects[0];
assert.equal(row.structure_kind,'shade-sail'); assert.equal(row.conventional_roof_supported,false);
assert.equal(row.feedback_status,'unsupported-structure'); assert.equal(row.usable_as_review_constraints,false);
assert.equal(row.saved_3d_estimate,null); assert.match(row.guidance,/historical/);
response = await feedbackRoute.PATCH({json:async()=>({scene_id:'fixture',object_id:'roof',expected_revision:1,
  model_signature:routeScene.model.model_signature,image_sha256:routeScene.model.image_sha256,outline_pixels:{ring,holes:[]},
  changes:{roof_line_edits:{vertices_meters:[[15,20],[25,20]],edges:[{a:0,b:1,kind:'ridge'}]}}})});
assert.equal(response.status,400); assert.equal(commits,0,'Rejected roof-line updates do not reach canonical saving');
routeScene = {...routeScene,review:{...routeScene.review,objects:[roof]}};
response = await roofRoute.POST(request({...roof,model:{...roof.model,rotation_deg:0}}));
assert.equal(response.status,200,await response.clone().text()); assert.equal(executions,1,'Ordinary building proposals keep their existing inference path');
console.log('Shade sails: subtype correction, immutable source, stale rigid geometry suppression, roof mount refusal, roof checks, recovery, canonical authorization, API mutation refusal and continued generic boundary edits passed.');
