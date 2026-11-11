// Recapture decision-carry wiring (docs/recapture-carry-2026-09-30.md section 6, landed 2026-10-03): recoverSceneReview takes
// the georeferenced-affine branch for a recaptured snapshot of ANOTHER scene and refuses with a reason through
// options.onRefuse; the same-frame and context-expansion paths answer as before; the edit comparison sees carried_from;
// validateSceneReview shape-checks carried_from (the Python window carry's extra `role` key passes); the editor banner names
// the re-projection and never calls a recapture the same image. Fixtures only: nothing under data/ is read or written.
// Pattern: scripts/test-scene-types-gaps.mjs compile() (the store's own validator runs on the carried review).
import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';

const directory=await mkdtemp(path.join(tmpdir(),'addr-recapture-wiring-'));
const compiled=new Map();
async function compile(source){
  const file=path.resolve(source);
  if(compiled.has(file))return compiled.get(file);
  const out=path.join(directory,path.relative(path.resolve('src'),file)).replace(/\.tsx?$/,'.mjs');
  compiled.set(file,out);
  let code=ts.transpileModule(await readFile(file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
  const targets=new Map();
  for(const [,specifier] of code.matchAll(/\bfrom\s*"([^"]+)"/g)){
    if(targets.has(specifier)||specifier.startsWith('node:'))continue;
    if(specifier.startsWith('.')||specifier.startsWith('@/')){
      const base=specifier.startsWith('@/')?path.resolve('src',specifier.slice(2)):path.resolve(path.dirname(file),specifier);
      const found=[`${base}.ts`,`${base}.tsx`,path.join(base,'index.ts')].find(p=>existsSync(p));
      assert(found,`${source}: cannot resolve ${specifier}`);
      targets.set(specifier,pathToFileURL(await compile(found)).href);
    } else targets.set(specifier,import.meta.resolve(specifier));
  }
  code=code.replace(/\bfrom\s*"([^"]+)"/g,(match,specifier)=>targets.has(specifier)?`from ${JSON.stringify(targets.get(specifier))}`:match);
  await mkdir(path.dirname(out),{recursive:true});
  await writeFile(out,code);
  return out;
}
try{
  const {recoverSceneReview}=await import(pathToFileURL(await compile('src/lib/scene-feedback.ts')).href);
  const {validateSceneReview}=await import(pathToFileURL(await compile('src/lib/scene-store.ts')).href);
  const {metreOffset,pixelAffine}=await import(pathToFileURL(await compile('src/lib/scene-frame-transform.ts')).href);
  const {ReviewError}=await import(pathToFileURL(await compile('src/lib/review-feedback.ts')).href);

  const square=(x,y,s,t=s)=>[[x,y],[x+s,y],[x+s,y+t],[x,y+t]];
  const apply=(m,[x,y])=>[x*m[0][0]+y*m[1][0]+m[2][0],x*m[0][1]+y*m[1][1]+m[2][1]];
  const model=()=>({height_m:4,base_m:0,color:'#67d7e7',height_source:'class-prior',height_verified:false,width_m:1,length_m:1,area_m2:1,rotation_deg:0});
  const obj=(id,kind,ring,extra={})=>({id,kind,label:id,ring,status:'candidate',geometry_reviewed:false,source:'sam3-text-onnx',model:model(),...extra});
  const SIG=c=>c.repeat(64);

  // Source: a fitted 512 px cohort crop at 0.0625 m/px. Target: a 1024 px capture at 0.125 m/px of another scene whose centroid
  // sits about 10.6 m west and 4.5 m south of the source's, so the whole source frame lands inside it (overlap 1), nothing is clipped.
  const source={schema_version:1,id:'roof_training_250-90002',address_id:90002,cohort:'roof_training_250',title:'Fixture source',centroid:{lon:145.3,lat:-37.8},
    image_sha256:SIG('a'),image_width:512,image_height:512,pixel_to_meters:[[0.0625,0],[0,-0.0625],[-16,16]],meters_to_pixel:[[16,0],[0,-16],[256,256]],
    model_signature:SIG('1'),scans:{},height_note:'',groups:[],
    objects:[obj('bldg-a','building',square(100,100,120)),obj('bldg-r','building',square(300,300,80)),obj('tree-1','tree',square(20,20,40)),obj('bldg-k','building',square(400,100,60))]};
  const target={...structuredClone(source),id:'lot-90002',title:'Fixture recapture',centroid:{lon:145.29988,lat:-37.80004},image_sha256:SIG('b'),
    image_width:1024,image_height:1024,pixel_to_meters:[[0.125,0],[0,-0.125],[-64,64]],meters_to_pixel:[[8,0],[0,-8],[512,512]],model_signature:SIG('2'),groups:[]};
  const offset=metreOffset(source.centroid,target.centroid),affine=pixelAffine(source.pixel_to_meters,offset,target.meters_to_pixel);
  assert.ok(offset[0]>9&&offset[0]<12&&offset[1]>3&&offset[1]<6,`fixture offset ${offset}: the source centroid sits about 10.6 m east and 4.5 m north in the target's metres`);
  // new-a is bldg-a seen whole by the new model (IoU 1); bldg-k is a CRM seed both models carry under the same id.
  target.objects=[obj('new-a','building',square(100,100,120).map(p=>apply(affine,p))),obj('bldg-k','building',square(700,700,40),{source:'crm-roof-pipeline'})];
  target.frame_transform={version:'frame-transform-v1',method:'georeferenced-affine',source_scene_id:source.id,source_model_signature:source.model_signature,
    source_image_sha256:source.image_sha256,source_size:[512,512],source_registration:'fitted',metre_offset:offset,pixel_affine:affine,
    overlap:{source_px_inside_target:512*512,fraction:1},
    residual:{method:'gradient-ncc+phase-correlation',warped_from:'source',shift_px:[.6,-.8],shift_m:.125,ncc:.72,samples:4096},
    design:{max_shift_m:.7,min_ncc:.35,min_overlap_fraction:.6,match_iou:.5,close_iou:.9,design_sha256:SIG('d')},verified:true};
  const byId=new Map(source.objects.map(o=>[o.id,o]));
  const saved={revision:7,model_signature:source.model_signature,reviewed_classes:[],groups:[],objects:[
    {...structuredClone(byId.get('bldg-a')),status:'confirmed',geometry_reviewed:true,notes:'Main roof.',model:{...model(),height_verified:true}},
    {...structuredClone(byId.get('bldg-r')),status:'rejected',notes:'Shadow.'},
    structuredClone(byId.get('tree-1')),
    {...structuredClone(byId.get('bldg-k')),status:'needs_adjustment',notes:'Check the carport.'},
    obj('drawn-1','building',square(200,350,40),{source:'reviewer',status:'needs_adjustment',geometry_reviewed:true,notes:'Traced shed.'}),
  ]};
  const frozen=JSON.stringify({source,target,saved});

  // 1. The recapture branch: another scene's snapshot with a frame_transform -> the georeferenced-affine carry.
  const refusals=[];
  const recovery=recoverSceneReview(source,target,saved,{revision:0,onRefuse:r=>refusals.push(r)});
  assert.ok(recovery,'a recaptured snapshot of another scene takes the georeferenced-affine branch');
  assert.deepEqual(refusals,[]);
  assert.equal(recovery.method,'georeferenced-affine');
  assert.equal(recovery.source_scene_id,source.id);
  assert.equal(recovery.review.revision,0,'the carried review goes out at the target\'s own revision (0: no review yet), so the first save is revision 1');
  assert.equal(recovery.review.model_signature,target.model_signature);
  assert.deepEqual(recovery.review.reviewed_classes,[]);
  assert.equal(recovery.carried_objects,4,'bldg-a, bldg-r, bldg-k and the drawn shed are carried; the untouched tree is not');
  assert.equal(recovery.new_candidates,2);
  assert.deepEqual(recovery.dropped,[]);assert.deepEqual(recovery.clipped,[]);
  assert.deepEqual(recovery.renamed,[{id:'bldg-k',carried_as:'bldg-k-carried'}],'a source id the new model also uses is carried under a distinct id');
  assert.deepEqual(recovery.renamed_groups,[]);
  const carried=recovery.review.objects.filter(o=>o.carried_from);
  assert.equal(carried.length,4);
  for(const o of carried){
    assert.equal(o.status,'needs_adjustment',`${o.id} returns for a recheck`);
    assert.equal(o.geometry_reviewed,false);assert.equal(o.model.height_verified,false);
    assert.equal(o.carried_from.method,'georeferenced-affine');assert.equal(o.carried_from.scene_id,source.id);assert.equal(o.carried_from.model_signature,source.model_signature);
    assert.equal(o.carried_from.status,saved.objects.find(s=>s.id===o.carried_from.object_id).status,`${o.id} keeps the earlier verdict only as a hint`);
    assert.ok(typeof o.carried_from.iou_with_new==='number'&&o.carried_from.iou_with_new>=0&&o.carried_from.iou_with_new<=1);
  }
  const a=carried.find(o=>o.id==='bldg-a');
  assert.equal(a.carried_from.status,'confirmed');assert.equal(a.carried_from.matched_id,'new-a');assert.ok(a.carried_from.iou_with_new>.999,'new-a is bldg-a re-projected');
  assert.equal(carried.find(o=>o.id==='bldg-r').carried_from.status,'rejected','a rejection travels as a hint, never as a verdict');
  assert.equal(carried.find(o=>o.id==='bldg-k-carried').carried_from.object_id,'bldg-k');
  for(const m of target.objects){const kept=recovery.review.objects.find(o=>o.id===m.id);assert.ok(kept&&!kept.carried_from);assert.deepEqual(kept.ring,m.ring,`${m.id} stays under its own id`);}
  a.ring.forEach((p,i)=>{const q=apply(affine,saved.objects[0].ring[i]);assert.ok(Math.hypot(p[0]-q[0],p[1]-q[1])<1e-6,'the carried outline maps through pixel_affine');});
  // The store's own validator accepts the carried review on the target snapshot and keeps carried_from.
  const validated=validateSceneReview(recovery.review,target);
  assert.equal(validated.objects.filter(o=>o.carried_from).length,4,'validateSceneReview keeps carried_from');
  assert.equal(JSON.stringify({source,target,saved}),frozen,'inputs are not mutated');

  // 2. Refusals name the step and return null; without onRefuse they return null quietly.
  const reasons=[];
  assert.equal(recoverSceneReview(source,target,{...saved,model_signature:SIG('3')},{onRefuse:r=>reasons.push(r)}),null);
  assert.match(reasons[0],/another snapshot of the source scene/,'a saved review on another source snapshot is refused with its reason');
  assert.equal(recoverSceneReview(source,{...target,frame_transform:{...target.frame_transform,source_model_signature:SIG('3')}},saved,{onRefuse:r=>reasons.push(r)}),null);
  assert.match(reasons[1],/another snapshot of the source scene/,'a frame transform measured against another source snapshot is refused');
  assert.equal(recoverSceneReview(source,{...target,frame_transform:{...target.frame_transform,verified:false}},saved,{onRefuse:r=>reasons.push(r)}),null);
  assert.match(reasons[2],/residual check did not pass/);
  assert.equal(recoverSceneReview(source,{...target,frame_transform:{...target.frame_transform,source_scene_id:'lot-1'}},saved),null,'null without a callback too');
  assert.equal(reasons.length,3);
  // The branch needs another scene: a snapshot of the SAME scene carrying a frame_transform falls through to the same-frame
  // check (which refuses a changed image without a reason), so the existing paths never see the module.
  assert.equal(recoverSceneReview(source,{...target,id:source.id},saved,{onRefuse:r=>reasons.push(r)}),null);
  assert.equal(reasons.length,3,'the same-frame path does not call onRefuse');

  // 3. The same-frame path as before: same scene, same image, re-extracted; an edited object is carried, no method is set.
  const plain=(id,extra={})=>({...obj(id,'tree',[[0,0],[10,0],[10,10],[0,10]]),...extra});
  const previous={id:'scene',model_signature:'old',centroid:{lon:145.3,lat:-37.8},image_sha256:'image',image_width:512,image_height:512,pixel_to_meters:[[1,0],[0,-1],[0,0]],meters_to_pixel:[[1,0],[0,-1],[0,0]],
    objects:[plain('roof',{kind:'building'}),plain('tree')],groups:[]};
  const current={...structuredClone(previous),model_signature:'new',objects:[previous.objects[0],plain('road',{kind:'road'})]};
  const sameSaved={revision:4,model_signature:'old',objects:[{...structuredClone(previous.objects[0]),status:'confirmed',geometry_reviewed:true},structuredClone(previous.objects[1])],groups:[],reviewed_classes:[]};
  const same=recoverSceneReview(previous,current,sameSaved);
  assert.ok(same);assert.equal(same.carried_objects,1);assert.equal(same.new_candidates,1);assert.equal(same.review.revision,4);
  assert.equal(same.method,undefined,'the same-frame recovery is unchanged: no method, no source_scene_id');assert.equal(same.source_scene_id,undefined);
  assert.equal(same.review.objects.find(o=>o.id==='roof').status,'needs_adjustment');
  assert.deepEqual(recoverSceneReview(previous,{...current,frame_transform:target.frame_transform},sameSaved),same,'a frame_transform on a same-scene snapshot changes nothing');
  // The context-expansion path as before: the same pixels inside a 1024 px frame keep an unchanged interior confirmation.
  const shift=o=>({...o,ring:o.ring.map(([x,y])=>[x+256,y+256])});
  const expanded={...structuredClone(previous),model_signature:'wide',image_sha256:'wide',image_width:1024,image_height:1024,pixel_to_meters:[[1,0],[0,-1],[-256,256]],meters_to_pixel:[[1,0],[0,-1],[256,256]],
    objects:previous.objects.map(shift),context_expansion:{source_model_signature:'old',source_image_sha256:'image',offset_pixels:[256,256],source_size:[512,512],preserved_pixels_sha256:SIG('a')}};
  const kept=recoverSceneReview(previous,expanded,sameSaved);
  assert.ok(kept);assert.equal(kept.method,undefined);assert.equal(kept.review.objects.find(o=>o.id==='roof').status,'confirmed','the expansion path still keeps an unchanged interior decision');
  assert.deepEqual(kept.review.objects.find(o=>o.id==='roof').ring,[[256,256],[266,256],[266,266],[256,266]]);

  // 4. The edit comparison sees carried_from: the same outline with provenance differs from the model's without it.
  const provenance={scene_id:'roof_training_250-1',model_signature:SIG('5'),object_id:'roof',status:'confirmed',geometry_reviewed:true,iou_with_new:.91,matched_id:null,method:'georeferenced-affine'};
  const untouched={...sameSaved,objects:structuredClone(previous.objects)};
  assert.equal(recoverSceneReview(previous,current,untouched).carried_objects,0,'an untouched object is not carried');
  assert.equal(recoverSceneReview(previous,current,{...untouched,objects:[{...structuredClone(previous.objects[0]),carried_from:provenance},untouched.objects[1]]}).carried_objects,1,
    'the same object with carried_from counts as the reviewer\'s');

  // 5. validateSceneReview shape-checks carried_from; the Python window carry's extra `role` key passes through.
  const checked=(carried_from)=>validateSceneReview({revision:0,model_signature:target.model_signature,reviewed_classes:[],groups:[],
    objects:[obj('c1','building',square(100,100,50),{status:'needs_adjustment',carried_from})]},target);
  const good={...provenance,object_id:'bldg-a',matched_id:'new-a',role:'frame_cut_target'};
  assert.deepEqual(checked(good).objects[0].carried_from,good,'a well-formed carried_from (with role) is kept as sent');
  assert.deepEqual(checked({...good,status:'candidate',matched_id:null}).objects[0].carried_from.status,'candidate','any earlier status is a valid hint');
  for(const [name,bad] of Object.entries({array:[],string:'carried',object_id_number:{...good,object_id:5},bad_signature:{...good,model_signature:'abc'},
    bad_scene_id:{...good,scene_id:'../x'},other_method:{...good,method:'same-frame'},iou_above_one:{...good,iou_with_new:1.5},iou_nan:{...good,iou_with_new:NaN},
    matched_id_number:{...good,matched_id:7},unknown_status:{...good,status:'approved'},checked_not_boolean:{...good,geometry_reviewed:'yes'}})){
    assert.throws(()=>checked(bad),e=>e instanceof ReviewError&&/carried object came from/.test(e.message),`malformed carried_from (${name}) is refused`);
  }
  assert.equal(checked(undefined).objects[0].carried_from,undefined,'no carried_from stays none');

  // 6. Pins: the store takes the branch only for a scene without a review and runs the verifier with the agreed limits; the
  // banner chooses the recapture sentence before the expansion and same-image ones, matching scripts/verify-scene-recapture.mjs.
  const store=await readFile('src/lib/scene-store.ts','utf8'),page=await readFile('src/app/scenes/page.tsx','utf8');
  assert.match(store,/\} else if \(review === null\) \{[\s\S]{0,400}?await recaptureRecovery\(id, model\)/,'the recapture branch runs only when the scene has no review of its own');
  assert.match(store,/"--verify-source", sourceModelFile, "--verify-target", targetModelFile/);
  assert.match(store,/timeout: 60000, maxBuffer: 1024 \* 1024/,'the verifier runs with a 60 s timeout and a 1 MB buffer');
  assert.match(store,/if \(stale && review\) \{[\s\S]*?recovery_reason = `The earlier model snapshot could not be verified/,'the existing stale branch is still there');
  assert.match(page,/data\.recovery\.method === "georeferenced-affine" \? recaptureSentence\(data\) : data\.model\.context_expansion \? "The original image pixels were preserved[^"]*" : "The image is unchanged\."/,
    'the recapture sentence is chosen before the expansion and same-image sentences');
  assert.equal(page.split('"The image is unchanged."').length-1,1,'"The image is unchanged." appears once, behind the method guard');
  assert.match(page,/re-projected through the georeferenced frame from \$\{source\} onto this image \(georeferenced affine; \$\{measured\}\)/);
  assert.match(page,/residual \$\{px\.toFixed\(1\)\} px \(\$\{residual\.shift_m\.toFixed\(2\)\} m\), NCC \$\{residual\.ncc\.toFixed\(2\)\}/);
  assert.match(page,/Confirmed and rejected decisions come back as needs review on this image: every carried decision returns for a recheck\./);
  console.log('Scene recapture wiring: georeferenced-affine branch for another scene\'s recaptured snapshot (4 carried for a recheck, renamed seed, matched counterpart, store validation), refusals with reasons, same-frame and expansion paths unchanged, carried_from in the edit comparison and shape-checked (role passes, 11 malformed refused), banner pins.');
}finally{await rm(directory,{recursive:true,force:true});}
