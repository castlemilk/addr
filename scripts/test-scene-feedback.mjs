import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-feedback-'));
try{
  for(const name of ['scene-feedback','scene-structure','scene-frame-transform']) { // scene-feedback delegates the recapture carry to scene-frame-transform (2026-10-03)
    const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
    await writeFile(path.join(directory,`${name}.mjs`),code);
  }
  const file=path.join(directory,'scene-feedback.mjs');
  const {recoverSceneReview,shiftSceneObject,expansionOffset}=await import(pathToFileURL(file));
  const object=(id,kind='tree')=>({id,kind,label:id,ring:[[0,0],[10,0],[10,10],[0,10]],status:'candidate',geometry_reviewed:false,source:'model',model:{height_m:3,base_m:0,color:'#334455',height_source:'class-prior',height_verified:false,area_m2:100}});
  const previous={id:'scene',model_signature:'old',image_sha256:'image',image_width:512,image_height:512,pixel_to_meters:[[1,0],[0,-1],[0,0]],meters_to_pixel:[[1,0],[0,-1],[0,0]],objects:[object('roof','building'),object('tree'),object('car','car'),object('grass','vegetation')],groups:[]};
  const saved={revision:4,model_signature:'old',objects:structuredClone(previous.objects),groups:[{id:'garden',label:'Garden'}],reviewed_classes:['car']};
  saved.objects[0].roof_annotation={vertices:[[0,0,3],[5,5,6],[10,0,3]],faces:[[0,1,2]]};
  saved.objects[1].ring[0][0]=1.5;saved.objects[1].group_id='garden';
  saved.objects[2].status='confirmed';saved.objects[2].geometry_reviewed=true;
  saved.objects[3].model.area_m2=100.001; // Saving recalculates metrics, not feedback.
  saved.objects.push({...object('manual','solar_panel'),source:'reviewer',parent_id:'roof',group_id:'garden'});
  const current={...structuredClone(previous),model_signature:'new',objects:[previous.objects[0],{...previous.objects[1],id:'new-tree'},previous.objects[2],previous.objects[3],object('road','road')]};
  const before=JSON.stringify({previous,current,saved});
  const recovered=recoverSceneReview(previous,current,saved);
  assert.equal(recovered.carried_objects,4);
  const familyOnly=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[0],roof_feedback:{family:'gabled',outline:{ring:previous.objects[0].ring,holes:[]}}}]});
  assert.equal(familyOnly.carried_objects,1);
  assert.equal(familyOnly.review.objects.find(o=>o.id==='roof').roof_feedback.family,'gabled','Carry explicit roof labels without changing their evidence snapshot');
  assert.equal(familyOnly.review.objects.find(o=>o.id==='roof').geometry_reviewed,false,'Recovered roof labels still require the outline recheck');
  assert.equal(recovered.new_candidates,3);
  assert.equal(recovered.review.revision,4);
  assert.equal(recovered.review.model_signature,'new');
  assert.deepEqual(recovered.review.reviewed_classes,[]);
  const byId=new Map(recovered.review.objects.map(o=>[o.id,o]));
  assert.deepEqual(byId.get('roof').roof_annotation,saved.objects[0].roof_annotation);
  assert.equal(byId.get('tree').ring[0][0],1.5);
  assert.equal(byId.get('tree').group_id,'garden');
  assert(byId.has('new-tree'),'Changed-ID detections remain visible, not silently suppressed');
  assert.equal(byId.get('car').status,'needs_adjustment');
  assert.equal(byId.get('car').geometry_reviewed,false);
  assert.equal(byId.get('grass').status,'candidate','Recalculated dimensions must not count as an edit');
  assert.equal(byId.get('manual').parent_id,'roof');
  assert.equal(JSON.stringify({previous,current,saved}),before,'Preview must not mutate any snapshot');
  for(const changes of [{image_sha256:'different'},{id:'other'},{image_width:1024},{pixel_to_meters:[[2,0],[0,-2],[0,0]]},{meters_to_pixel:[[2,0],[0,-2],[0,0]]}])
    assert.equal(recoverSceneReview(previous,{...current,...changes},saved),null);
  assert.equal(recoverSceneReview(previous,current,{...saved,model_signature:'other'}),null);
  {
    const source={...previous,centroid:{lon:145.3,lat:-37.8},region:{provider:'google',imagery_zoom:20}};
    const expanded={...source,model_signature:'expanded',image_sha256:'larger-image',image_width:1024,image_height:1024,
      pixel_to_meters:[[1,0],[0,-1],[-256,256]],meters_to_pixel:[[1,0],[0,-1],[256,256]],
      objects:source.objects.map(o=>shiftSceneObject(o,256)),context_expansion:{source_model_signature:'old',source_image_sha256:'image',offset_pixels:[256,256],source_size:[512,512],preserved_pixels_sha256:'a'.repeat(64)}};
    const lines={vertices:[[-2,3],[4,3]],edges:[{a:0,b:1,kind:'ridge'}],checked:false,outline:{ring:source.objects[0].ring,holes:[]}};
    const feedback={...saved,objects:[{...source.objects[0],roof_line_edits:lines}]};
    assert.equal(expansionOffset(source,expanded),256);
    const shifted=recoverSceneReview(source,expanded,feedback);
    // In a verified expansion an interior object the new model re-proposes unchanged keeps its confirmed decision; an
    // object whose outline the reviewer edited still returns for a recheck.
    const confirmedFeedback={...saved,objects:[{...source.objects[2],status:'confirmed',geometry_reviewed:true},{...source.objects[1],ring:[[1.5,0],[10,0],[10,10],[0,10]],status:'confirmed',geometry_reviewed:true}]};
    const kept=recoverSceneReview(source,expanded,confirmedFeedback);
    assert.equal(kept.review.objects.find(o=>o.id===source.objects[2].id).status,'confirmed','unchanged interior object keeps its decision across the expansion');
    assert.equal(kept.review.objects.find(o=>o.id===source.objects[1].id).status,'needs_adjustment','an edited outline is rechecked even across the expansion');
    const roof=shifted.review.objects.find(o=>o.id==='roof');
    assert.deepEqual(roof.roof_line_edits.vertices,lines.vertices,'Roof feedback stays in the exact same metre frame');
    assert.deepEqual(roof.roof_line_edits.outline.ring,roof.ring,'Shift the captured outline binding with the outline');
    assert.deepEqual(roof.ring[0],[256,256]);assert.equal(roof.status,'needs_adjustment');
    for(const changes of [{centroid:{lon:145.4,lat:-37.8}},{context_expansion:{...expanded.context_expansion,offset_pixels:[255,256]}},{pixel_to_meters:[[1,0],[0,-1],[-255,256]]},{context_expansion:{...expanded.context_expansion,source_model_signature:'wrong'}}])
      assert.equal(recoverSceneReview(source,{...expanded,...changes},feedback),null,'Unproven shifts and resampling cannot carry feedback');
    assert.deepEqual(feedback.objects[0].ring[0],[0,0],'Recovery leaves historical feedback untouched');
    // A fuller detection that took over clipped seeds (`replaces`) inherits their decisions for a recheck: the clipped
    // outline does not survive as a ghost, all-rejected seeds reject the whole, mixed decisions return for a recheck.
    const tree=source.objects[1], car=source.objects[2];
    const fuller={...object('fuller-tree'),ring:[[250,250],[300,250],[300,300],[250,300]],replaces:[tree.id]};
    const merged={...object('merged-car','car'),ring:[[250,250],[300,250],[300,300],[250,300]],replaces:[car.id,'part-b']};
    const taken={...expanded,objects:[...expanded.objects.filter(o=>o.id!==tree.id&&o.id!==car.id),fuller,merged]};
    const decided={...saved,objects:[{...tree,status:'confirmed',geometry_reviewed:true,label:'Gum tree',notes:'big gum'},{...car,status:'rejected'},{...object('part-b','car'),status:'rejected'},
      {...object('manual-panel','solar_panel'),source:'reviewer',parent_id:tree.id}]};
    const snapshot=JSON.stringify({taken,decided});
    const moved=recoverSceneReview(source,expanded,decided); // without replaces the clipped seeds are carried as before
    assert.ok(moved.review.objects.some(o=>o.id===tree.id),'without replaces the decided seed is carried');
    const inherited=recoverSceneReview(source,taken,decided);
    assert.ok(!inherited.review.objects.some(o=>o.id===tree.id||o.id===car.id||o.id==='part-b'),'replaced seeds leave no ghost outline');
    const gum=inherited.review.objects.find(o=>o.id==='fuller-tree');
    assert.equal(gum.status,'needs_adjustment');assert.equal(gum.geometry_reviewed,false);assert.equal(gum.label,'Gum tree');assert.equal(gum.notes,'big gum');
    assert.deepEqual(gum.ring,fuller.ring,'the fuller outline is the model\'s, not the clipped one');
    assert.equal(inherited.review.objects.find(o=>o.id==='merged-car').status,'rejected','every seed rejected rejects the whole');
    assert.equal(inherited.review.objects.find(o=>o.id==='manual-panel').parent_id,'fuller-tree','children point at the detection that took over their parent');
    assert.equal(inherited.carried_objects,4);
    assert.equal(JSON.stringify({taken,decided}),snapshot,'inheritance mutates no snapshot');
    // a machine label on a replaced seed is dropped: no ghost, and the fuller detection stays an undecided candidate
    const machine={...saved,objects:[{...tree,status:'confirmed',geometry_reviewed:true,label:'Shed',provenance:{source:'machine:decision-policy-v2'}}]};
    const dropped=recoverSceneReview(source,taken,machine);
    assert.ok(!dropped.review.objects.some(o=>o.id===tree.id),'no ghost of the machine-labelled seed');
    assert.equal(dropped.review.objects.find(o=>o.id==='fuller-tree').status,'candidate','a machine label is not turned into a human decision on the fuller outline');
    assert.equal(dropped.carried_objects,0);
  }
  const child={...object('child','solar_panel'),source:'reviewer',parent_id:'roof'};
  const parentContext=recoverSceneReview(previous,{...current,objects:[]},{...saved,objects:[previous.objects[0],child]});
  assert.equal(parentContext.carried_objects,2,'Keep a missing parent so the saved child relationship remains valid');
  assert.equal(parentContext.review.objects.find(o=>o.id==='roof').status,'needs_adjustment');
  const rejected=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[2],status:'rejected'}]});
  assert.equal(rejected.review.objects.find(o=>o.id==='car').status,'rejected');
  const placement=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[2],model:{...previous.objects[2].model,placement:'absolute'}}]});
  assert.equal(placement.review.objects.find(o=>o.id==='car').model.placement,'absolute');
  const crown={crown_profile:'conical',crown_base_ratio:.4};
  const treeOnly=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[1],model:{...previous.objects[1].model,tree:crown}}]});
  assert.equal(treeOnly.carried_objects,1);
  assert.deepEqual(treeOnly.review.objects.find(o=>o.id==='tree').model.tree,crown,'Carry crown edits even when the outline and height are unchanged');
  const vehicle={profile:'van',heading_deg:212.5,cabin_length_ratio:.77,cabin_offset_ratio:-.12};
  const carOnly=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[2],model:{...previous.objects[2].model,car:vehicle}}]});
  assert.equal(carOnly.carried_objects,1);
  assert.deepEqual(carOnly.review.objects.find(o=>o.id==='car').model.car,vehicle,'Carry vehicle shape edits independently of class or outline changes');
  const visibilityOnly=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[2],visibility:'occluded'}]});
  assert.equal(visibilityOnly.carried_objects,1);assert.equal(visibilityOnly.review.objects.find(o=>o.id==='car').visibility,'occluded');
  const issuesOnly=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[2],review_issues:['shape_3d','height']}]});
  assert.equal(issuesOnly.carried_objects,1);
  assert.deepEqual(issuesOnly.review.objects.find(o=>o.id==='car').review_issues,['shape_3d','height'],'Carry unresolved feedback even without geometry edits');
  const holes=[[[2,2],[4,2],[4,4],[2,4]]];
  const gapOnly=recoverSceneReview(previous,current,{...saved,objects:[{...previous.objects[0],holes}]});
  assert.equal(gapOnly.carried_objects,1);
  assert.deepEqual(gapOnly.review.objects.find(o=>o.id==='roof').holes,holes,'Interior edits survive re-extraction even with an unchanged outer boundary');
  assert.equal(gapOnly.review.objects.find(o=>o.id==='roof').geometry_reviewed,false);
  const surface={profile:'crowned',heading_deg:212.5,grade_percent:-3.75,crossfall_percent:2.25};
  const oldRoad={...previous,objects:[object('road','road')]},newRoad={...oldRoad,model_signature:'new'};
  const surfaceOnly=recoverSceneReview(oldRoad,newRoad,{...saved,objects:[{...oldRoad.objects[0],model:{...oldRoad.objects[0].model,surface}}]});
  assert.equal(surfaceOnly.carried_objects,1);
  assert.deepEqual(surfaceOnly.review.objects[0].model.surface,surface,'Carry slope edits even when the 2D outline is unchanged');
  const solar={tilt_deg:22.5,azimuth_deg:112.75,clearance_m:.15};
  const oldPanel={...previous,objects:[object('panel','solar_panel')]},newPanel={...oldPanel,model_signature:'new'};
  const solarOnly=recoverSceneReview(oldPanel,newPanel,{...saved,objects:[{...oldPanel.objects[0],model:{...oldPanel.objects[0].model,solar}}]});
  assert.equal(solarOnly.carried_objects,1);assert.deepEqual(solarOnly.review.objects[0].model.solar,solar,'Mounting-plane edits survive re-extraction independently of outline edits');
  const splitSaved={...saved,objects:[{...previous.objects[1],status:'superseded',geometry_reviewed:false,replacement_ids:['tree-child']},{...object('tree-child'),source:'reviewer-split',derived_from:['tree']}]};
  const splitRecovery=recoverSceneReview(previous,current,splitSaved);
  assert.equal(splitRecovery.review.objects.find(o=>o.id==='tree').status,'superseded');
  assert.deepEqual(splitRecovery.review.objects.find(o=>o.id==='tree-child').derived_from,['tree']);
  assert.deepEqual(splitRecovery.review.objects.find(o=>o.id==='tree').replacement_ids,['tree-child']);
  const groupedOld={...previous,groups:[{id:'removed',label:'Old group'},{id:'rename',label:'Original name'}],objects:[
    {...object('group-tree'),group_id:'removed'}, {...object('group-roof','building'),group_id:'rename'}]};
  const groupedNew={...groupedOld,model_signature:'next',groups:[...groupedOld.groups,{id:'new-group',label:'New candidate group'}],objects:[...groupedOld.objects,
    {...object('new-tree'),group_id:'removed'},{...object('new-roof','building'),group_id:'new-group'}]};
  const groupedSaved={...saved,groups:[{id:'rename',label:'Reviewed name'}],objects:groupedOld.objects.map(o=>o.id==='group-tree'?{...o,group_id:undefined}:o)};
  const groupRecovery=recoverSceneReview(groupedOld,groupedNew,groupedSaved);
  assert.deepEqual(groupRecovery.review.groups,[{id:'rename',label:'Reviewed name'},{id:'new-group',label:'New candidate group'}]);
  assert(!groupRecovery.review.objects.some(o=>o.group_id==='removed'),'Removed groups and new candidate references must not be resurrected');
  assert.equal(groupRecovery.review.objects.find(o=>o.id==='new-roof').group_id,'new-group');
  console.log('Scene feedback: corrections, apexes, groups and parent recovery; unchanged-frame gate; duplicate visibility; approval reset; immutable previews passed.');
}finally{await rm(directory,{recursive:true,force:true});}
