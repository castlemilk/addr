// Recapture carry path (W4-TS): a recaptured scene is a new image at a different scale, so saved decisions can only
// travel as re-projected outlines that return for a recheck. Pattern: scripts/test-scene-feedback.mjs (the module under
// test is transpiled alone and imported); the store's own validators are loaded read-only the way
// scripts/test-roof-feedback-patch.mjs loads them, so every carried object is checked by the code that will save it.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';

const req=createRequire(import.meta.url),cache=new Map();
function load(name){if(cache.has(name))return cache.get(name);const exports={};cache.set(name,exports);
  const source=ts.transpileModule(readFileSync(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  new Function('exports','require',source)(exports,id=>id.startsWith('./')?load(id.slice(2)):req(id));return exports;}
const {polygonIssues}=load('scene-types');
const {lineageIssue}=load('scene-instances');
const {roofLineEditsIssue}=load('roof-line-edits');
const {validAnnotation}=load('roof-annotations');
const {validRoofFeedback}=load('scene-roof-feedback');

const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-recapture-'));
try{
  const moduleSource=await readFile('src/lib/scene-frame-transform.ts','utf8');
  const code=ts.transpileModule(moduleSource,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
  const file=path.join(directory,'frame-transform.mjs');await writeFile(file,code);
  const {frameTransform,frameTransformIssue,projectSceneObject,carryAcrossFrames,carryAcrossFramesResult,outlineIoU}=await import(pathToFileURL(file));

  // Reference maths, written independently of the module: Web-Mercator local metres scaled by cos(lat) of the target
  // centroid (scripts/ml/scene_addresses.image_projection, src/lib/property-parcels.ts parcelImagePoint), and
  // pixel_affine = target.meters_to_pixel o (+metre_offset) o source.pixel_to_meters in the [[a,b],[c,d],[tx,ty]] layout.
  const WORLD=40075016.68557849,mercY=lat=>Math.asinh(Math.tan(lat*Math.PI/180))/(2*Math.PI);
  const offsetOf=(source,target)=>{const scale=WORLD*Math.cos(target.centroid.lat*Math.PI/180);
    return [(source.centroid.lon-target.centroid.lon)/360*scale,(mercY(source.centroid.lat)-mercY(target.centroid.lat))*scale];};
  const affineOf=(source,target,[dx,dy])=>{const A=source.pixel_to_meters,B=target.meters_to_pixel;
    return [[A[0][0]*B[0][0]+A[0][1]*B[1][0],A[0][0]*B[0][1]+A[0][1]*B[1][1]],[A[1][0]*B[0][0]+A[1][1]*B[1][0],A[1][0]*B[0][1]+A[1][1]*B[1][1]],
      [(A[2][0]+dx)*B[0][0]+(A[2][1]+dy)*B[1][0]+B[2][0],(A[2][0]+dx)*B[0][1]+(A[2][1]+dy)*B[1][1]+B[2][1]]];};
  const apply=(m,[x,y])=>[x*m[0][0]+y*m[1][0]+m[2][0],x*m[0][1]+y*m[1][1]+m[2][1]];
  const near=(a,b,tolerance=1e-9,where='')=>{
    if(Array.isArray(a)){assert.ok(Array.isArray(b)&&a.length===b.length,`${where}: shape ${JSON.stringify(a)} vs ${JSON.stringify(b)}`);a.forEach((v,i)=>near(v,b[i],tolerance,where));return;}
    assert.ok(Math.abs(a-b)<=tolerance,`${where}: ${a} vs ${b}`);};
  const ringArea=r=>Math.abs(r.reduce((s,p,i)=>s+p[0]*r[(i+1)%r.length][1]-r[(i+1)%r.length][0]*p[1],0))/2;
  const round2=n=>Math.round(n*100)/100;

  // Source: a fitted 512 px cohort-like crop at 0.0625 m/px. Target: a 512 px z20-like region at 0.125 m/px whose
  // centroid sits about 20 m west and 3 m north of the source centroid, so the right edge of the source falls outside.
  const square=(x,y,s,t=s)=>[[x,y],[x+s,y],[x+s,y+t],[x,y+t]];
  const P2M=[[0.0625,0],[0,-0.0625],[-16,16]],M2P=[[16,0],[0,-16],[256,256]],meters=r=>r.map(p=>apply(P2M,p));
  const model=(extra={})=>({height_m:4,base_m:0,color:'#67d7e7',height_source:'class-prior',height_verified:false,width_m:1,length_m:1,area_m2:1,rotation_deg:0,footprint_area_px:1,...extra});
  const obj=(id,kind,ring,extra={})=>({id,kind,label:id,ring,status:'candidate',geometry_reviewed:false,source:'sam3-text-onnx',score:.9,prompt:kind,model:model(),...extra});
  const roofA=square(100,100,60),holeA=square(120,120,10);
  const annotationA={schema_version:1,roof_type:'gabled',vertices:[...meters(roofA).map(p=>[...p,3]),[-9.5,7,5],[-6.25,7,5]],faces:[[0,1,2,3]],edges:[{a:4,b:5,kind:'ridge'}]};
  const lonlat=[[145.29998,-37.79998],[145.29992,-37.79998],[145.29992,-37.80004]];
  const plane={a:.12,b:-.05,c:3.4};
  const bldgA=obj('bldg-a','building',roofA,{holes:[holeA],group_id:'bldg-a',replaces:['old-seed'],roof_type:'gabled',
    roof_parts:[{ring_meters:meters(roofA),ring_z:[3,3,3,3],ring:meters(roofA),z:[3,3,3,3],coords:meters(roofA),ring_pixels:roofA,ring_lonlat:lonlat,plane,
      triangles:[meters(roofA).slice(0,3).map(p=>[...p,3]),[meters(roofA)[0],meters(roofA)[2],meters(roofA)[3]].map(p=>[...p,3])],
      gable_ends:[[meters(roofA)[0],meters(roofA)[1]]]}],
    roof_lines:[{line_meters:[[-9.5,7],[-6.25,7]],kind:'ridge',line_pixels:[[104,144],[156,144]],line_lonlat:lonlat.slice(0,2),line_z:[5,5]}],roof_annotation:annotationA,
    boundary_refinement:{method:'roof-envelope-v5',raw_ring:square(99,99,62),raw_holes:[square(121,121,8)],holes:[],filled_area_m2:0,
      assembly:{method:'connected-roof-v1',components:[{id:'c1',ring:roofA,holes:[holeA]}]},construction:{inferred_edges:[{line_pixels:[[100,100],[160,100]],canopy_length_m:3.75}]}},
    roof_optical_guidance:{method:'optical-v1',observations:{meters_to_pixel:M2P,source_rgb_shape:[512,512,3]},valley_candidates:[{points_meters:[[-9,8],[-7,8]]}]},
    roof_form_comparison:{status:'compared',alternative:{annotation:{vertices:[[-9.75,9.75,3],[-6,9.75,3],[-6,6,3]]},parts:[{ring_meters:meters(roofA)}]}}});
  // The CRM seed roof-<address_id>: a cohort scene is seeded with it, and a recapture extracted with CRM priors seeds the
  // same id from the same record (scene_extract.py L291, scene_region.corrected_roofs L133).
  const seedRing=square(40,200,50);
  const previous={schema_version:1,id:'roof_training_250-90001',address_id:90001,cohort:'roof_training_250',title:'Fixture source',
    centroid:{lon:145.3,lat:-37.8},image_sha256:'a'.repeat(64),image_width:512,image_height:512,pixel_to_meters:P2M,meters_to_pixel:M2P,
    model_signature:'1'.repeat(64),scans:{},height_note:'',
    groups:[{id:'bldg-a',label:'Building'},{id:'bldg-c',label:'Building'},{id:'bldg-s',label:'Building'},{id:'roof-90001',label:'1 Fixture Street'}],
    objects:[bldgA,obj('bldg-b','building',square(200,300,40),{clipped_by_frame:[0,0,256,256]}),obj('bldg-out','building',square(470,50,30)),
      obj('bldg-clip','building',square(430,200,40)),obj('tree-1','tree',square(20,20,20)),obj('bldg-m','building',square(300,200,30)),
      obj('bldg-h','building',square(300,250,30)),obj('bldg-s','building',square(300,20,60,40),{group_id:'bldg-s'}),
      obj('bldg-c','building',square(200,400,60),{group_id:'bldg-c'}),
      obj('roof-90001','building',seedRing,{source:'roof-pipeline',address_id:90001,group_id:'roof-90001'})]};
  const edits=(ring,holes=[])=>({schema_version:1,outline:{ring,holes},baseline:{vertices:[[-9.5,7],[-6.25,7]],edges:[{a:0,b:1,kind:'ridge'}]},
    vertices:[[-9.5,7.1],[-6.25,7.1]],edges:[{a:0,b:1,kind:'ridge'}],checked:true,notes:'ridge moved'});
  const byId=new Map(previous.objects.map(o=>[o.id,o])),reviewed=(id,changes)=>({...structuredClone(byId.get(id)),...changes});
  const clipEdits={...edits(square(430,200,40)),vertices:[[11,3],[13,3]],baseline:{vertices:[[11,3],[13,3]],edges:[{a:0,b:1,kind:'ridge'}]}};
  // A U opening left whose base lies beyond the new right edge (x 448.5 in source pixels): the edge leaves two arms.
  const uRing=[[430,300],[470,300],[470,340],[430,340],[430,330],[460,330],[460,310],[430,310]];
  const agentNotes='Assistant visually traced saved Google imagery on 2026-09-23 (fixture): an observation, not an independent building sample.';
  const saved={revision:28,model_signature:previous.model_signature,reviewed_classes:['building'],
    groups:[{id:'bldg-a',label:'Main house'},{id:'bldg-c',label:'Building'},{id:'bldg-s',label:'Building'},{id:'garden',label:'Garden'},{id:'roof-90001',label:'Home'}],
    objects:[
      reviewed('bldg-a',{status:'confirmed',geometry_reviewed:true,notes:'Main roof',model:model({height_verified:true,footprint_area_px:3500}),provenance:{human_decision:{source:'human:chat',at:'2026-09-30T10:00:00Z'}},
        roof_feedback:{outline:{ring:roofA,holes:[holeA]},family:'gabled',linework:{vertices:meters(roofA),faces:[[0,1,2,3]],edges:[]}},roof_line_edits:edits(roofA,[holeA])}),
      reviewed('bldg-b',{status:'rejected',notes:'shadow, not a roof'}),
      reviewed('bldg-out',{status:'confirmed',geometry_reviewed:true}),
      reviewed('bldg-clip',{status:'needs_adjustment',review_issues:['outline'],roof_line_edits:clipEdits,roof_feedback:{outline:{ring:square(430,200,40),holes:[]},family:'gabled'}}),
      reviewed('tree-1',{}),
      reviewed('bldg-m',{status:'confirmed',provenance:{source:'machine:decision-policy-v2',tier:'T3',confidence:.9,interval:[.8,.95]}}),
      reviewed('bldg-h',{status:'confirmed',geometry_reviewed:true,provenance:{source:'machine:decision-policy-v2',human_confirmed:true}}),
      reviewed('bldg-s',{status:'superseded',replacement_ids:['split-1','split-2']}),
      {...obj('split-1','building',square(300,20,30,40)),source:'reviewer-split',derived_from:['bldg-s'],group_id:'bldg-s',status:'confirmed',geometry_reviewed:true},
      {...obj('split-2','building',square(330,20,30,40)),source:'reviewer-split',derived_from:['bldg-s'],group_id:'bldg-s',status:'needs_adjustment'},
      {...obj('manual-panel','solar_panel',square(110,140,8)),source:'reviewer',parent_id:'bldg-a',group_id:'bldg-a',status:'confirmed',geometry_reviewed:true},
      reviewed('bldg-c',{}),
      {...obj('panel-c','solar_panel',square(210,410,10)),source:'reviewer',parent_id:'bldg-c',group_id:'bldg-c',status:'needs_adjustment'},
      reviewed('roof-90001',{status:'superseded',replacement_ids:['trace-1']}),
      {...obj('trace-1','building',square(40,200,48,50)),source:'reviewer-trace',derived_from:['roof-90001'],group_id:'roof-90001',status:'confirmed',geometry_reviewed:true},
      {...obj('panel-r','solar_panel',square(50,210,8)),source:'reviewer',parent_id:'roof-90001',group_id:'roof-90001',status:'confirmed',geometry_reviewed:true},
      {...obj('review-east-flat-roof','building',square(20,300,40,30)),source:'reviewer',status:'confirmed',geometry_reviewed:true,notes:agentNotes},
      {...obj('bldg-u','building',uRing),source:'reviewer',status:'confirmed',geometry_reviewed:true},
      {...obj('bldg-sliver','building',square(448,100,4,2)),source:'reviewer',status:'needs_adjustment'},
      {...obj('speck','other',square(60,60,1)),source:'reviewer',status:'confirmed',geometry_reviewed:true},
    ]};
  const target={schema_version:1,id:'lot-90001',address_id:90001,cohort:null,title:'Fixture recapture',
    region:{provider:'google',requested_bounds:[145.29,-37.81,145.31,-37.79],bounds:[145.29,-37.81,145.31,-37.79],imagery_zoom:20,span_m:64,native_meters_per_pixel:.125,spatial_group:'g',split:'train'},
    centroid:{lon:145.3-0.000227,lat:-37.8+0.000027},image_sha256:'b'.repeat(64),image_width:512,image_height:512,
    pixel_to_meters:[[.125,0],[0,-.125],[-32,32]],meters_to_pixel:[[8,0],[0,-8],[256,256]],model_signature:'2'.repeat(64),scans:{},height_note:''};
  const offset=offsetOf(previous,target),affine=affineOf(previous,target,offset),map=r=>r.map(p=>apply(affine,p));
  near(offset,[19.966848671041433,-3.0056268009631326],1e-9,'the source centroid sits about 20 m east and 3 m south of the target centroid');
  for(const p of [[0,0],[100,160],[512,512]])near(apply(affine,p),apply(target.meters_to_pixel,[apply(P2M,p)[0]+offset[0],apply(P2M,p)[1]+offset[1]]),1e-9,'affine = composition');
  // Overlap by hand: the affine is diagonal here, so the source frame lands on an axis-aligned rectangle.
  const spanX=[affine[2][0],affine[2][0]+512*affine[0][0]],spanY=[affine[2][1],affine[2][1]+512*affine[1][1]];
  const insidePx=(Math.min(512,spanX[1])-Math.max(0,spanX[0]))*(Math.min(512,spanY[1])-Math.max(0,spanY[0]));
  const fraction=insidePx/Math.abs(affine[0][0]*affine[1][1])/(512*512);
  near(fraction,(512-affine[2][0])/256,1e-12,'the source right edge falls outside the new image');
  const shifted=(r,dx)=>map(r).map(([x,y])=>[x+dx,y]);
  target.objects=[
    {...obj('new-a','building',map(roofA),{holes:[map(holeA)],group_id:'new-a'})},
    {...obj('new-c','building',shifted(square(200,400,60),3),{group_id:'new-c'})},
    obj('new-tree','tree',map(square(20,20,20))),
    {...obj('new-panel','solar_panel',map(square(110,140,8))),parent_id:'new-a',group_id:'new-a'},
    obj('roof-90001','building',shifted(seedRing,2),{source:'crm-roof-pipeline',address_id:90001,group_id:'roof-90001'})];
  target.groups=[{id:'new-a',label:'Building'},{id:'new-c',label:'Building'},{id:'roof-90001',label:'1 Fixture Street'}];
  // scene_recapture writes overlap.fraction rounded to 6 decimals and source_px_inside_target rounded to an integer.
  target.frame_transform={version:'frame-transform-v1',method:'georeferenced-affine',source_scene_id:previous.id,source_model_signature:previous.model_signature,
    source_image_sha256:previous.image_sha256,source_size:[512,512],source_registration:'fitted',metre_offset:offset,pixel_affine:affine,
    overlap:{source_px_inside_target:Math.round(fraction*512*512),fraction:Math.round(fraction*1e6)/1e6},
    residual:{method:'gradient-ncc+phase-correlation',warped_from:'source',shift_px:[.4,-.2],shift_m:.056,ncc:.61,samples:4096},
    design:{max_shift_m:.7,min_ncc:.35,min_overlap_fraction:.6,match_iou:.5,close_iou:.9,design_sha256:'d'.repeat(64)},verified:true};
  const withTransform=changes=>({...target,frame_transform:{...target.frame_transform,...changes}});
  // A narrow notch whose mouth lies beyond the new right edge: valid in the source frame (its two mouth points are 2.4e-6
  // px apart), but the edge meets it in two points 5.6e-7 px apart, a repeated point the store refuses, so the outline is
  // dropped as invalid in the new frame, not as split by its edge.
  const toSource=([x,y])=>[(x-affine[2][0])/affine[0][0],(y-affine[2][1])/affine[1][1]];
  saved.objects.push({...obj('bldg-slit','building',[[495,380],[520,390-6e-7],[505,390],[520,390+6e-7],[495,400]].map(toSource)),source:'reviewer',status:'needs_adjustment'});
  // The fixture review is one the store could have saved: every outline valid in the source frame, lineage complete.
  for(const o of saved.objects)assert.deepEqual(polygonIssues(o.ring,512,512,o.holes),[],`${o.id}: a valid outline in the source frame`);
  assert.equal(lineageIssue(saved.objects),null,'the fixture review has a complete replacement history');
  const snapshot=JSON.stringify({previous,target,saved});

  // 1. The frame transform is recomputed from the two snapshots and accepted only when the recorded values agree.
  const checked=frameTransform(previous,target);
  assert.ok(checked,'a consistent recorded transform is accepted (overlap rounded as scene_recapture writes it)');assert.equal(frameTransformIssue(previous,target),null);
  near(checked.metre_offset,offset,1e-9,'metre offset');near(checked.pixel_affine,affine,1e-9,'pixel affine');
  assert.ok(frameTransform(previous,withTransform({pixel_affine:[affine[0],affine[1],[affine[2][0]+1e-7,affine[2][1]]]})),'agreement to 1e-6 tolerates float noise');
  assert.ok(frameTransform(previous,withTransform({overlap:{...target.frame_transform.overlap,fraction:fraction}})),'the unrounded overlap is accepted too');
  const refused={
    'wrong source sha':withTransform({source_image_sha256:'f'.repeat(64)}),
    'verified false':withTransform({verified:false}),
    'source_scene_id mismatch':withTransform({source_scene_id:'roof_training_250-90002'}),
    'pixel_affine tx off by 1e-3':withTransform({pixel_affine:[affine[0],affine[1],[affine[2][0]+1e-3,affine[2][1]]]}),
    'pixel_affine scale off by 1e-3':withTransform({pixel_affine:[[affine[0][0]+1e-3,affine[0][1]],affine[1],affine[2]]}),
    'metre_offset off by 1e-3':withTransform({metre_offset:[offset[0],offset[1]+1e-3]}),
    'overlap fraction off by 1e-3':withTransform({overlap:{...target.frame_transform.overlap,fraction:fraction+1e-3}}),
    'source size mismatch':withTransform({source_size:[1024,1024]}),
    'source snapshot mismatch':withTransform({source_model_signature:'9'.repeat(64)}),
    'registration region for a fitted source':withTransform({source_registration:'region'}),
    'design sha not a sha256':withTransform({design:{...target.frame_transform.design,design_sha256:'DESIGN.json'}}),
    'residual below the design floor':withTransform({residual:{...target.frame_transform.residual,ncc:.2}}),
    'another method':withTransform({method:'expansion-256'}),
    'no transform':{...target,frame_transform:undefined},
  };
  for(const [name,current] of Object.entries(refused)){
    assert.equal(frameTransform(previous,current),null,`${name}: refused`);
    assert.equal(typeof frameTransformIssue(previous,current),'string',`${name}: a named reason`);
    assert.equal(carryAcrossFrames(previous,current,saved),null,`${name}: nothing is carried`);
    const outcome=carryAcrossFramesResult(previous,current,saved);assert.equal(outcome.recovery,null);assert.equal(outcome.reason,frameTransformIssue(previous,current));
  }
  assert.match(frameTransformIssue(previous,refused['wrong source sha']),/image/);
  assert.match(frameTransformIssue(previous,refused['pixel_affine tx off by 1e-3']),/pixel affine/);
  assert.match(frameTransformIssue(previous,refused['overlap fraction off by 1e-3']),/overlap/);
  assert.match(frameTransformIssue(previous,refused['registration region for a fitted source']),/registration/);
  assert.match(frameTransformIssue(previous,refused['design sha not a sha256']),/design/);
  assert.equal(carryAcrossFrames(previous,target,{...saved,model_signature:'3'.repeat(64)}),null,'a review of another source snapshot is not carried');
  assert.match(carryAcrossFramesResult(previous,{...target,groups:undefined},saved).reason,/group list/,'a malformed snapshot is refused, not thrown on');

  // 2. projectSceneObject: pixel fields through pixel_affine, metre fields by +metre_offset (z unchanged).
  const projected=projectSceneObject(saved.objects[0],checked);
  near(projected.ring,map(roofA),1e-9,'ring');near(projected.holes,[map(holeA)],1e-9,'holes');
  near(projected.roof_feedback.outline.ring,map(roofA),1e-9,'roof_feedback outline');near(projected.roof_line_edits.outline.holes,[map(holeA)],1e-9,'roof_line_edits outline');
  near(projected.boundary_refinement.raw_ring,map(square(99,99,62)),1e-9,'boundary_refinement.raw_ring');near(projected.boundary_refinement.raw_holes,[map(square(121,121,8))],1e-9,'raw_holes');
  near(projected.boundary_refinement.assembly.components[0].ring,map(roofA),1e-9,'assembly ring');near(projected.boundary_refinement.construction.inferred_edges[0].line_pixels,map([[100,100],[160,100]]),1e-9,'line_pixels');
  const plus=([x,y])=>[x+offset[0],y+offset[1]];
  near(projected.roof_parts[0].ring_meters,meters(roofA).map(plus),1e-9,'roof_parts');assert.deepEqual(projected.roof_parts[0].ring_z,[3,3,3,3]);
  near(projected.roof_parts[0].ring,meters(roofA).map(plus),1e-9,'roof_parts ring');near(projected.roof_parts[0].coords,meters(roofA).map(plus),1e-9,'roof_parts coords');
  near(projected.roof_parts[0].ring_pixels,map(roofA),1e-9,'roof_parts ring_pixels');
  near(projected.roof_parts[0].triangles,bldgA.roof_parts[0].triangles.map(t=>t.map(([x,y,z])=>[x+offset[0],y+offset[1],z])),1e-9,'roof_parts triangles keep z');
  near(projected.roof_parts[0].gable_ends,[[meters(roofA)[0],meters(roofA)[1]].map(plus)],1e-9,'roof_parts gable_ends (metre edges)');
  const moved=projected.roof_parts[0].plane;assert.equal(moved.a,plane.a);assert.equal(moved.b,plane.b);
  for(const [x,y] of meters(roofA))near(moved.a*(x+offset[0])+moved.b*(y+offset[1])+moved.c,plane.a*x+plane.b*y+plane.c,1e-9,'the roof plane gives the same height at a moved point');
  assert.deepEqual(projected.roof_parts[0].ring_lonlat,lonlat,'lon/lat is frame-free');assert.deepEqual(projected.roof_parts[0].z,[3,3,3,3]);
  near(projected.roof_lines[0].line_meters,[[-9.5,7],[-6.25,7]].map(plus),1e-9,'roof_lines');near(projected.roof_lines[0].line_pixels,map([[104,144],[156,144]]),1e-9,'roof_lines line_pixels');
  assert.deepEqual(projected.roof_lines[0].line_lonlat,lonlat.slice(0,2));assert.deepEqual(projected.roof_lines[0].line_z,[5,5]);
  near(projected.roof_annotation.vertices,annotationA.vertices.map(([x,y,z])=>[x+offset[0],y+offset[1],z]),1e-9,'roof_annotation keeps z');
  near(projected.roof_line_edits.vertices,[[-9.5,7.1],[-6.25,7.1]].map(plus),1e-9,'roof_line_edits');near(projected.roof_line_edits.baseline.vertices,[[-9.5,7],[-6.25,7]].map(plus),1e-9,'baseline');
  near(projected.roof_feedback.linework.vertices,meters(roofA).map(plus),1e-9,'linework');
  near(projected.roof_form_comparison.alternative.annotation.vertices,[[-9.75,9.75,3],[-6,9.75,3],[-6,6,3]].map(([x,y,z])=>[x+offset[0],y+offset[1],z]),1e-9,'alternative');
  assert.equal(projected.roof_optical_guidance,undefined,'optical evidence holding the source frame is not re-expressed');
  assert.equal(projected.model.footprint_area_px,round2(3500*Math.abs(affine[0][0]*affine[1][1]-affine[0][1]*affine[1][0])),'footprint_area_px scales by the affine area factor');
  assert.equal(projected.model.area_m2,saved.objects[0].model.area_m2,'metre dimensions are frame-free');
  near(projectSceneObject(saved.objects[1],checked).clipped_by_frame,[...apply(affine,[0,0]),...apply(affine,[256,256])],1e-9,'clipped_by_frame');

  // 3. carryAcrossFrames: the carry itself.
  const recovery=carryAcrossFrames(previous,target,saved);
  assert.ok(recovery,'a verified frame carries the review');
  assert.equal(recovery.method,'georeferenced-affine');
  assert.equal(recovery.review.model_signature,target.model_signature);assert.equal(recovery.review.revision,0,'a new scene has no review yet');
  assert.deepEqual(recovery.review.reviewed_classes,[],'layer approvals reset');
  assert.equal(carryAcrossFrames(previous,target,saved,{revision:3}).review.revision,3);
  const out=new Map(recovery.review.objects.map(o=>[o.id,o]));
  const carried=recovery.review.objects.filter(o=>o.carried_from);
  assert.deepEqual(carried.map(o=>o.id).sort(),['bldg-a','bldg-b','bldg-clip','bldg-h','bldg-s','manual-panel','panel-c','panel-r','review-east-flat-roof',
    'roof-90001-carried','split-1','split-2','trace-1']);
  assert.equal(recovery.carried_objects,carried.length);assert.equal(recovery.new_candidates,target.objects.length);
  for(const o of target.objects)assert.deepEqual(out.get(o.id),o,`${o.id}: every object of the new model stays visible and unchanged for duplicate review`);
  assert.ok(!out.has('tree-1')&&!out.has('bldg-c'),'unedited candidates are not carried');
  assert.deepEqual(recovery.dropped.map(d=>[d.object_id,d.reason]).sort(),[['bldg-m','machine_only'],['bldg-out','outside_new_frame'],
    ['bldg-slit','invalid_in_new_frame'],['bldg-sliver','too_small_in_new_frame'],['bldg-u','split_by_new_frame_edge'],['speck','too_small_in_new_frame']],'drops are named');
  assert.deepEqual(recovery.dropped.find(d=>d.object_id==='bldg-out'),{object_id:'bldg-out',kind:'building',status:'confirmed',reason:'outside_new_frame'});
  assert.deepEqual(recovery.clipped,['bldg-clip']);
  assert.deepEqual(recovery.renamed,[{id:'roof-90001',carried_as:'roof-90001-carried'}]);
  assert.deepEqual(recovery.renamed_groups,[{id:'roof-90001',carried_as:'roof-90001-carried'}]);

  // Status policy: nothing comes back confirmed or rejected; earlier verdicts travel only as hints.
  for(const o of recovery.review.objects){
    assert.ok(!['confirmed','rejected'].includes(o.status),`${o.id}: no carried or fresh object is confirmed or rejected`);
    const source=o.provenance?.source;assert.ok(!(typeof source==='string'&&source.startsWith('machine:')),`${o.id}: no machine provenance`);
  }
  const savedById=new Map(saved.objects.map(o=>[o.id,o])),carriedAs=new Map(recovery.renamed.map(r=>[r.id,r.carried_as]));
  for(const o of carried){
    const sourceId=o.carried_from.object_id,before=savedById.get(sourceId);
    assert.ok(before,`${o.id}: carried_from.object_id names the source object`);assert.equal(o.id,carriedAs.get(sourceId)??sourceId);
    assert.equal(o.status,before.status==='superseded'?'superseded':'needs_adjustment',`${o.id}: status`);
    assert.equal(o.geometry_reviewed,false);assert.equal(o.model.height_verified,false);
    assert.equal(o.provenance,undefined,`${o.id}: no provenance travels`);assert.equal(o.replaces,undefined,`${o.id}: source-model seeds do not travel`);
    assert.deepEqual(Object.keys(o.carried_from).sort(),['geometry_reviewed','iou_with_new','matched_id','method','model_signature','object_id','scene_id','status']);
    assert.equal(o.carried_from.scene_id,previous.id);assert.equal(o.carried_from.model_signature,previous.model_signature);
    assert.equal(o.carried_from.status,before.status);assert.equal(o.carried_from.geometry_reviewed,before.geometry_reviewed);assert.equal(o.carried_from.method,'georeferenced-affine');
    assert.equal(typeof o.carried_from.iou_with_new,'number');assert.ok(o.carried_from.matched_id===null||typeof o.carried_from.matched_id==='string');
    assert.match(o.notes,/Carried from roof_training_250-90001/);assert.ok(o.notes.length<=5000);
    if(before.notes)assert.ok(o.notes.startsWith(`${before.notes}\n`),`${o.id}: the reviewer's notes stay first and whole`);
  }
  const a=out.get('bldg-a'),b=out.get('bldg-b');
  assert.equal(a.carried_from.matched_id,'new-a');assert.equal(a.carried_from.iou_with_new,1);assert.match(a.notes,/^Main roof\n/);assert.match(a.notes,/agrees closely/);
  assert.equal(b.carried_from.matched_id,null);assert.equal(b.carried_from.iou_with_new,0);assert.equal(b.carried_from.status,'rejected');assert.match(b.notes,/rejected/);
  assert.match(b.notes,/hint for this image, not a decision/);assert.match(out.get('bldg-s').notes,/stays replaced/);assert.doesNotMatch(out.get('bldg-s').notes,/decide again/);
  near(b.clipped_by_frame,[...apply(affine,[0,0]),...apply(affine,[256,256])],1e-9,'an earlier frame cut travels with the outline');
  near(a.ring,map(roofA),1e-9,'carried ring');near(a.holes,[map(holeA)],1e-9,'carried holes');
  assert.equal(a.model.footprint_area_px,projected.model.footprint_area_px,'an outline carried whole keeps its scaled pixel area');
  assert.deepEqual(a.roof_feedback,{outline:{ring:a.ring,holes:a.holes},family:'gabled'},'the roof family travels; the earlier linework check does not');
  assert.equal(a.roof_line_edits.checked,false,'an earlier roof-line check is not carried as a check');
  near(a.roof_line_edits.vertices,[[-9.5,7.1],[-6.25,7.1]].map(plus),1e-9,'carried roof-line edits');
  assert.equal(a.roof_optical_guidance,undefined);
  const clip=out.get('bldg-clip');
  assert.ok(clip.ring.every(([x,y])=>x>=0&&x<=512&&y>=0&&y<=512),'a clipped outline stays inside the new image');
  assert.equal(Math.max(...clip.ring.map(p=>p[0])),512);near(ringArea(clip.ring),(512-apply(affine,[430,200])[0])*20,1e-9,'clipped area');
  assert.equal(clip.model.footprint_area_px,round2(ringArea(clip.ring)),'a clipped outline gets its own pixel area');
  assert.equal(clip.model.area_m2,round2(ringArea(clip.ring)*.125*.125),'and its own area in square metres');
  assert.equal(clip.roof_line_edits,undefined,'roof-line edits bound to a cut outline stay with the earlier scene');
  assert.equal(clip.roof_feedback,undefined,'roof feedback bound to a cut outline stays with the earlier scene');
  assert.match(clip.notes,/clipped/);assert.match(clip.notes,/roof-line edits/);assert.match(clip.notes,/roof family \(gabled\) stayed/);
  assert.equal(out.get('bldg-h').carried_from.status,'confirmed','a machine label a human confirmed travels as a hint without its machine provenance');
  // An assistant-authored review is an observation: its notes stay first (the counter's marker) and are named as such.
  const agent=out.get('review-east-flat-roof');
  assert.equal(agent.status,'needs_adjustment');assert.equal(agent.geometry_reviewed,false);
  assert.equal(agent.carried_from.status,'confirmed');assert.equal(agent.carried_from.geometry_reviewed,true,'carried_from keeps the recorded fields');
  assert.ok(agent.notes.startsWith(`${agentNotes}\n`),'the Assistant marker stays first');
  assert.match(agent.notes,/the earlier notes record an assistant observation \(status confirmed\), not a reviewer decision/);
  assert.doesNotMatch(agent.notes,/the earlier review/);assert.doesNotMatch(agent.notes,/outline checked/);assert.doesNotMatch(agent.notes,/decide again/);
  // Lineage travels whole and stays valid.
  assert.equal(out.get('bldg-s').status,'superseded');assert.deepEqual(out.get('bldg-s').replacement_ids,['split-1','split-2']);
  assert.deepEqual(out.get('split-1').derived_from,['bldg-s']);assert.deepEqual(out.get('split-2').derived_from,['bldg-s']);
  assert.equal(lineageIssue(recovery.review.objects),null,'lineage set intact');
  // Parents and groups: kept when the parent travels, re-pointed through matched_id when it does not.
  assert.equal(out.get('manual-panel').parent_id,'bldg-a');assert.equal(out.get('manual-panel').group_id,'bldg-a');
  assert.equal(out.get('panel-c').parent_id,'new-c');assert.equal(out.get('panel-c').group_id,'new-c');
  const groups=new Map(recovery.review.groups.map(g=>[g.id,g.label]));
  assert.equal(groups.get('bldg-a'),'Main house');assert.equal(groups.get('new-a'),'Building');assert.equal(groups.get('new-c'),'Building');
  assert.ok(!groups.has('garden')&&!groups.has('bldg-c'),'groups nobody carried stay behind');
  // The same-id seed: the new model's roof-90001 stays; the carried one takes a distinct id and is compared with it.
  const seed=out.get('roof-90001'),seedCarried=out.get('roof-90001-carried'),trace=out.get('trace-1'),panelR=out.get('panel-r');
  assert.equal(seed.carried_from,undefined);assert.equal(seed.source,'crm-roof-pipeline');
  assert.equal(seedCarried.carried_from.object_id,'roof-90001');assert.equal(seedCarried.carried_from.matched_id,'roof-90001','matched_id names the same-id seed');
  assert.equal(seedCarried.carried_from.iou_with_new,Math.round(23/27*1e4)/1e4,'the affine path is compared with the same-id seed (a 2 px shift of a 25 px square)');
  assert.equal(seedCarried.status,'superseded');assert.deepEqual(seedCarried.replacement_ids,['trace-1']);assert.deepEqual(trace.derived_from,['roof-90001-carried']);
  assert.equal(trace.carried_from.matched_id,'roof-90001');
  assert.equal(panelR.parent_id,'roof-90001-carried');assert.equal(panelR.group_id,'roof-90001-carried');
  assert.equal(seedCarried.group_id,'roof-90001-carried');assert.equal(trace.group_id,'roof-90001-carried');
  assert.equal(groups.get('roof-90001'),'1 Fixture Street','the new model keeps its own group record');assert.equal(groups.get('roof-90001-carried'),'Home');
  assert.match(seedCarried.notes,/already uses its id \(roof-90001\), so it was carried as roof-90001-carried/);
  // Everything the store validates on a recovered review.
  const ids=new Set();
  for(const o of recovery.review.objects){
    assert.ok(!ids.has(o.id),`${o.id}: unique`);ids.add(o.id);assert.match(o.id,/^[a-zA-Z0-9_-]{1,100}$/);
    assert.deepEqual(polygonIssues(o.ring,512,512,o.holes),[],`${o.id}: outline valid in the new image`);
    assert.equal(roofLineEditsIssue(o,target.pixel_to_meters,512,512),null,`${o.id}: roof-line edits valid in the new frame`);
    assert.equal(validRoofFeedback(o),true,`${o.id}: roof feedback valid`);
    if(o.roof_annotation)assert.equal(validAnnotation(o.roof_annotation),true,`${o.id}: roof annotation valid`);
    if(o.group_id)assert.ok(groups.has(o.group_id),`${o.id}: group exists`);
  }
  assert.equal(new Set(recovery.review.groups.map(g=>g.id)).size,recovery.review.groups.length,'group ids are unique');
  for(const o of recovery.review.objects)if(o.parent_id)assert.ok(ids.has(o.parent_id),`${o.id}: parent exists`);
  const text=JSON.stringify(recovery);
  assert.ok(!/accura/i.test(text)&&!/accura/i.test(moduleSource),'wording: agreement between two frames, never accuracy');
  assert.equal(JSON.stringify({previous,target,saved}),snapshot,'the carry mutates no snapshot');

  // The more common same-id case: a confirmed seed with no lineage.
  const confirmedSeed={...saved,objects:saved.objects.filter(o=>o.id!=='trace-1').map(o=>{
    if(o.id!=='roof-90001')return o;const copy={...o,status:'confirmed',geometry_reviewed:true};delete copy.replacement_ids;return copy;})};
  const seeded=carryAcrossFrames(previous,target,confirmedSeed),seededOut=new Map(seeded.review.objects.map(o=>[o.id,o]));
  assert.deepEqual(seededOut.get('roof-90001'),target.objects.find(o=>o.id==='roof-90001'),'the new model\'s seed stays unchanged');
  const again=seededOut.get('roof-90001-carried');
  assert.equal(again.status,'needs_adjustment');assert.equal(again.geometry_reviewed,false);assert.equal(again.carried_from.status,'confirmed');
  assert.equal(again.carried_from.matched_id,'roof-90001');assert.equal(again.carried_from.iou_with_new,Math.round(23/27*1e4)/1e4);assert.match(again.notes,/overlaps this outline/);
  assert.equal(seeded.new_candidates,target.objects.length);assert.equal(lineageIssue(seeded.review.objects),null);

  // 4. Lineage sets are carried whole or not at all.
  const straddle={...saved,objects:saved.objects.map(o=>o.id==='split-2'?{...o,ring:square(470,100,20)}:o)};
  const refusedSet=carryAcrossFramesResult(previous,target,straddle);
  assert.equal(refusedSet.recovery,null);assert.match(refusedSet.reason,/lineage set/);assert.match(refusedSet.reason,/split-2/);
  const outsideSet={...saved,objects:saved.objects.map(o=>['bldg-s','split-1','split-2'].includes(o.id)?{...o,ring:square(470,100+(o.id==='split-2'?20:0),20)}:o)};
  const wholeDrop=carryAcrossFrames(previous,target,outsideSet);
  assert.ok(wholeDrop,'a lineage set wholly outside the new image is dropped whole');
  assert.deepEqual(wholeDrop.dropped.filter(d=>d.reason==='outside_new_frame').map(d=>d.object_id).sort(),['bldg-out','bldg-s','split-1','split-2']);
  assert.equal(lineageIssue(wholeDrop.review.objects),null);
  const machineMember={...saved,objects:saved.objects.map(o=>o.id==='split-1'?{...o,provenance:{source:'machine:decision-policy-v2'}}:o)};
  assert.match(carryAcrossFramesResult(previous,target,machineMember).reason,/lineage set/);
  const pending={...saved,objects:saved.objects.map(o=>o.id==='bldg-b'?{...o,fieldwork_import:{pending:true,itemId:'x'}}:o)};
  assert.deepEqual(carryAcrossFrames(previous,target,pending).dropped.find(d=>d.object_id==='bldg-b').reason,'fieldwork_pending','an unaccepted import is not a decision');

  // 5. The design values are read from the transform: a stricter match_iou leaves panel-c without a parent to point at.
  const strict=carryAcrossFrames(previous,withTransform({design:{...target.frame_transform.design,match_iou:.9,close_iou:.95}}),saved);
  const lonely=strict.review.objects.find(o=>o.id==='panel-c');
  assert.equal(lonely.parent_id,undefined);assert.match(lonely.notes,/choose a parent/);
  assert.equal(lonely.group_id,'bldg-c');assert.ok(strict.review.groups.some(g=>g.id==='bldg-c'),'an unmatched group owner keeps its group record');
  assert.equal(strict.review.objects.find(o=>o.id==='bldg-a').carried_from.matched_id,'new-a');
  assert.equal(strict.review.objects.find(o=>o.id==='roof-90001-carried').carried_from.matched_id,null,'IoU 0.85 is below a match_iou of 0.9');
  assert.ok(Math.abs(recovery.review.objects.find(o=>o.id==='panel-c').carried_from.iou_with_new-0)<1e-12,'no new panel overlaps panel-c');

  // 6. outlineIoU is exact for non-convex outlines with gaps.
  const unit={ring:square(0,0,2)};
  near(outlineIoU(unit,unit),1,1e-12,'identical');near(outlineIoU(unit,{ring:square(1,0,2)}),1/3,1e-12,'half overlap');
  assert.equal(outlineIoU(unit,{ring:square(5,5,1)}),0);
  near(outlineIoU({ring:square(0,0,4),holes:[square(1,1,2)]},{ring:square(0,0,4)}),12/16,1e-12,'a gap is not roof');
  const ell={ring:[[0,0],[4,0],[4,1],[1,1],[1,4],[0,4]]};
  near(outlineIoU(ell,{ring:square(0,0,4)}),7/16,1e-12,'L-shape inside its square');near(outlineIoU(ell,{ring:square(2,-1,1,3)}),1/(7+3-1),1e-12,'L-shape against a crossing bar');

  // 7. The reviewer's notes are never cut for machine text: the carry's sentences give way, the agreement first.
  const tailB=b.notes.slice(b.notes.indexOf('\n')+1),leadB=tailB.slice(0,tailB.indexOf(' No object of this kind'));
  assert.ok(leadB.startsWith('Carried from')&&leadB.endsWith('decide again.'),'bldg-b: lead then agreement');
  const notesWith=text=>carryAcrossFrames(previous,target,{...saved,objects:saved.objects.map(o=>o.id==='bldg-b'?{...o,notes:text}:o)}).review.objects.find(o=>o.id==='bldg-b').notes;
  const full='n'.repeat(4990);assert.equal(notesWith(full),full,'4,990 characters of reviewer notes survive whole');
  const roomy='r'.repeat(5000-1-leadB.length-10);assert.equal(notesWith(roomy),`${roomy}\n${leadB}`,'the agreement sentence gives way before the lead');
  const over=notesWith('o'.repeat(6000));assert.equal(over.length,5000);assert.ok(over.endsWith(' [notes truncated at carry]'),'only over-long notes are cut, and marked');

  console.log(`Scene recapture: transform recomputed and refused on ${Object.keys(refused).length} mismatches; pixel and metre fields re-projected; ${recovery.carried_objects} objects carried for a recheck (none confirmed or rejected, no machine provenance), ${recovery.dropped.length} dropped with reasons (${[...new Set(recovery.dropped.map(d=>d.reason))].sort().join(', ')}), ${recovery.clipped.length} clipped, ${recovery.renamed.length} same-id object carried under a distinct id beside the new model's own; lineage whole; parents and groups re-pointed; reviewer notes kept whole; store validators pass; snapshots unchanged.`);
}finally{await rm(directory,{recursive:true,force:true});}
