// src/lib/scene-initial-view.ts askedObjectIds / polygonDistance / buildingClusters / windowLotPfi / initialViewRings / initialImageFrame /
// framePlacement, src/lib/scene-image-frame.ts capFrameToImage and src/lib/scene-simplify.ts simplifyClosedRing / simplifyOutline /
// simplifyMessage: the 6 October 2026 decision card's editor half ("yes to defaults", 7 October). Item 3: the initial view of a
// window scene fits the union of every asked object (scene_window_ready's asked set, live ids only: carried frame_cut_target slices,
// their replacers through replacement_ids, carried whole_roof_body copies), each asked building's cluster (live buildings whose
// metric gap through pixel_to_meters is <= 0.5 m) and the planned lot polygon (window_frame.lots[0].pfi among the loaded Vicmap
// features), widened by objectImageFrame's +24 px / x1.6 / viewport-aspect rule and capped to the image; the URL object still
// selects, it no longer zooms. Item 2: Simplify on the selected object of any kind, closed-ring Douglas-Peucker at 1 px per click,
// then 2 px once 1 px removes nothing, refused on a superseded object or when polygonIssues fails, the area change in the message.
// Pure: nothing here writes outside a temp dir. The census cases read data/scenes read-only and are skipped where the scenes are
// absent. Then static pins on the canvas, the page and the Taskfile.
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import ts from 'typescript';
const directory=await mkdtemp(path.join(tmpdir(),'addr-scene-initial-view-'));
try {
 for(const name of ['scene-types','scene-image-frame','scene-initial-view','scene-simplify']) {
  const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z0-9-]+)"/g,'from "./$1.mjs"');
  await writeFile(path.join(directory,`${name}.mjs`),code);
 }
 const view=await import(pathToFileURL(path.join(directory,'scene-initial-view.mjs')));
 const simplify=await import(pathToFileURL(path.join(directory,'scene-simplify.mjs')));
 const frames=await import(pathToFileURL(path.join(directory,'scene-image-frame.mjs')));
 const {askedObjectIds,buildingClusters,polygonDistance,pointInRing,windowLotPfi,initialViewRings,initialImageFrame,framePlacement,frameSpanMeters,CLUSTER_TOUCH_M,SLICE_ROLE,BODY_ROLE}=view;
 const {simplifyClosedRing,simplifyOutline,simplifyMessage,SIMPLIFY_TOLERANCES_PX,outlineAreaM2}=simplify;
 const {capFrameToImage,objectImageFrame}=frames;
 for(const fn of [askedObjectIds,buildingClusters,polygonDistance,pointInRing,windowLotPfi,initialViewRings,initialImageFrame,framePlacement,frameSpanMeters,simplifyClosedRing,simplifyOutline,simplifyMessage,outlineAreaM2,capFrameToImage,objectImageFrame])assert.equal(typeof fn,'function');
 assert.equal(CLUSTER_TOUCH_M,0.5,'the census TOUCH_M');assert.equal(SLICE_ROLE,'frame_cut_target');assert.equal(BODY_ROLE,'whole_roof_body');
 assert.deepEqual([...SIMPLIFY_TOLERANCES_PX],[1,2],'1 px then 2 px');

 // Fixtures: a 1024 px window at 0.1 m/px with the y axis flipped as every window's pixel_to_meters is.
 const P2M=[[0.1,0],[0,-0.1],[-51.2,51.2]];
 const IMAGE={width:1024,height:1024};
 const rect=(x,y,w,h)=>[[x,y],[x+w,y],[x+w,y+h],[x,y+h]];
 const obj=(id,kind,ring,extra={})=>({id,kind,label:`${kind} ${id}`,ring,status:'candidate',geometry_reviewed:false,source:'test',model:{},...extra});
 const windowScene={window_frame:{version:'frame-cut-window-v1',identity:'win',lots:[]},pixel_to_meters:P2M,image_width:1024,image_height:1024};

 // polygonDistance: shapely's distance on rings.
 assert.equal(polygonDistance(rect(0,0,10,10),rect(20,0,10,10)),10,'a 10 px gap');
 assert.equal(polygonDistance(rect(0,0,10,10),rect(5,5,10,10)),0,'overlap is 0');
 assert.equal(polygonDistance(rect(0,0,10,10),rect(2,2,3,3)),0,'a held ring is 0');
 assert.equal(polygonDistance(rect(2,2,3,3),rect(0,0,10,10)),0,'either way round');
 assert.equal(polygonDistance(rect(0,0,10,10),rect(10,10,5,5)),0,'a corner touch is 0');
 assert.equal(polygonDistance(rect(0,0,10,10),rect(13,14,5,5)),5,'a diagonal gap is the corner distance');
 assert.equal(polygonDistance([[0,0],[1,1]],rect(0,0,10,10)),Infinity,'under three points is no polygon');
 assert.equal(pointInRing([5,5],rect(0,0,10,10)),true);assert.equal(pointInRing([15,5],rect(0,0,10,10)),false);

 // buildingClusters: live buildings only, transitive, measured in metres through pixel_to_meters.
 const A=obj('A','building',rect(100,100,50,50)), B=obj('B','building',rect(154,100,50,50)), G=obj('G','building',rect(208,100,50,50));
 const C=obj('C','building',rect(300,100,50,50)), D=obj('D','building',rect(100,156,50,50));
 const E=obj('E','building',rect(100,151,50,50),{status:'rejected'}), S=obj('S','building',rect(100,151,50,50),{status:'superseded'}), F=obj('F','tree',rect(150,150,10,10));
 const clusters=buildingClusters([A,B,G,C,D,E,S,F],P2M);
 assert.deepEqual(clusters.get('A'),['A','B','G'],'A-B 4 px = 0.4 m and B-G 4 px join transitively');
 assert.deepEqual(clusters.get('B'),['A','B','G']);assert.deepEqual(clusters.get('C'),['C'],'C is 92 px away');
 assert.deepEqual(clusters.get('D'),['D'],'D is 6 px = 0.6 m below A: not touching');
 assert.equal(clusters.has('E'),false,'rejected buildings are not live');assert.equal(clusters.has('S'),false,'superseded buildings are not live');
 assert.equal(clusters.has('F'),false,'a tree is not a building');
 assert.deepEqual(buildingClusters([A,D],P2M,0.7).get('A'),['A','D'],'the touch distance is a parameter (0.6 m gap joins at 0.7)');

 // askedObjectIds: scene_window_ready.assess, live ids only.
 const carried=(role)=>({scene_id:'win-x',model_signature:'s',object_id:'o',status:'candidate',geometry_reviewed:false,iou_with_new:0,matched_id:null,method:'georeferenced-affine',role});
 const lineage=[
  obj('s1','building',rect(0,0,10,10),{status:'needs_adjustment',carried_from:carried(SLICE_ROLE)}),
  obj('s2','building',rect(0,0,10,10),{status:'superseded',carried_from:carried(SLICE_ROLE),replacement_ids:['r2']}),
  obj('r2','building',rect(0,0,20,20),{status:'confirmed',replaces:['s2']}),
  obj('s3','building',rect(0,0,10,10),{status:'superseded',carried_from:carried(SLICE_ROLE),replacement_ids:['r3']}),
  obj('r3','building',rect(0,0,20,20),{status:'superseded',replaces:['s3'],replacement_ids:['r3b']}),
  obj('r3b','building',rect(0,0,20,20),{status:'confirmed',replaces:['r3']}),
  obj('s4','building',rect(0,0,10,10),{status:'superseded',carried_from:carried(SLICE_ROLE)}),
  obj('b1','building',rect(0,0,30,30),{status:'confirmed',carried_from:carried(BODY_ROLE),replaces:['s1']}),
  obj('m1','driveway',rect(0,0,30,30),{replaces:['x1','x2']}),
  obj('t1','building',rect(0,0,30,30),{status:'confirmed'}),
  obj('p1','building',rect(0,0,30,30),{carried_from:carried(undefined)}),
  obj('s5','building',rect(0,0,10,10),{status:'rejected',carried_from:carried(SLICE_ROLE)}),
 ];
 assert.deepEqual(askedObjectIds(lineage).sort(),['b1','r2','r3b','s1','s5'],'pending and decided slices, replacers through superseded chains, bodies; no plain carry, no linear merge, no superseded id');
 assert.deepEqual(askedObjectIds(lineage.filter(o=>o.id!=='r2')).sort(),['b1','r3b','s1','s5'],'a replacer missing from the review names nothing');
 assert.equal(askedObjectIds(lineage.filter(o=>o.id!=='r2')).includes('s2'),false,'the superseded slice stays out even without its replacer');
 const loop=[obj('q1','building',rect(0,0,10,10),{status:'superseded',carried_from:carried(SLICE_ROLE),replacement_ids:['q2']}),obj('q2','building',rect(0,0,10,10),{status:'superseded',replacement_ids:['q1']})];
 assert.deepEqual(askedObjectIds(loop),[],'a replacement loop terminates');

 // windowLotPfi: the planned lot, else null.
 assert.equal(windowLotPfi({window_frame:{lots:[{address_id:30300,class:'numbered',lot:'3',pfi:'214342494',plan:'PS631347',share:.93}]}}),'214342494');
 assert.equal(windowLotPfi({window_frame:{lots:[{address_id:1,class:'numbered',lot:'3',pfi:null,plan:'P',share:1}]}}),null);
 assert.equal(windowLotPfi({window_frame:{lots:[]}}),null);assert.equal(windowLotPfi({window_frame:{}}),null);assert.equal(windowLotPfi({}),null);assert.equal(windowLotPfi(null),null);

 // initialViewRings: window scenes with an asked object only; asked buildings bring their cluster; the lot rings join.
 const asked=obj('s1','building',rect(400,400,60,40),{status:'needs_adjustment',carried_from:carried(SLICE_ROLE)});
 const neighbour=obj('N','building',rect(462,400,30,40),{status:'confirmed'});
 const far=obj('Z','building',rect(800,800,30,40),{status:'confirmed'});
 const carSlice=obj('v1','car',rect(100,100,20,10),{carried_from:carried(SLICE_ROLE)});
 assert.equal(initialViewRings([asked,neighbour],{pixel_to_meters:P2M}),null,'not a window scene');
 assert.equal(initialViewRings([neighbour,far],windowScene),null,'a window with nothing asked');
 const union=initialViewRings([asked,neighbour,far],windowScene);
 assert.deepEqual(union.asked,['s1']);assert.deepEqual(union.cluster,['N'],'the 2 px (0.2 m) neighbour joins, the far roof does not');
 assert.equal(union.rings.length,2);assert.equal(union.lot,false);
 const withLot=initialViewRings([asked,neighbour,far],windowScene,[rect(300,300,300,300)]);
 assert.equal(withLot.rings.length,3);assert.equal(withLot.lot,true);
 assert.deepEqual(withLot.rings[2],rect(300,300,300,300));assert.notEqual(withLot.rings[2],withLot.rings[2].slice(),'rings are copied');
 const carView=initialViewRings([carSlice,neighbour,far],windowScene);
 assert.deepEqual(carView.asked,['v1']);assert.deepEqual(carView.cluster,[],'a non-building asked object brings no cluster');

 // capFrameToImage: inside the image, never larger than it.
 assert.deepEqual(capFrameToImage({x:-50,y:-20,width:300,height:200},IMAGE),{x:0,y:0,width:300,height:200});
 assert.deepEqual(capFrameToImage({x:900,y:900,width:300,height:200},IMAGE),{x:724,y:824,width:300,height:200});
 assert.deepEqual(capFrameToImage({x:-10,y:5,width:2000,height:100},IMAGE),{x:0,y:5,width:1024,height:100});
 assert.deepEqual(capFrameToImage({x:10,y:20,width:100,height:100},IMAGE),{x:10,y:20,width:100,height:100},'a frame inside is unchanged');

 // initialImageFrame: objectImageFrame's widening then the cap.
 const square=initialImageFrame(union,IMAGE,{width:600,height:600});
 assert.equal(square.x>=0&&square.y>=0&&square.x+square.width<=1024&&square.y+square.height<=1024,true,'inside the image');
 for(const ring of union.rings)assert.equal(framePlacement(square,ring),'inside');
 assert.ok(Math.abs(square.width/square.height-1)<1e-9,'the viewport aspect when not capped');
 assert.deepEqual(square,capFrameToImage(objectImageFrame(union.rings,IMAGE,{width:600,height:600},false),IMAGE),'the existing +24 px / x1.6 rule, no edge context');
 const wide=initialImageFrame(union,IMAGE,{width:960,height:600});
 assert.ok(Math.abs(wide.width/wide.height-1.6)<1e-9,'a 1.6 viewport');
 const corner=initialViewRings([obj('c1','building',rect(900,900,120,120),{carried_from:carried(SLICE_ROLE)})],windowScene);
 const capped=initialImageFrame(corner,IMAGE,{width:600,height:600});
 assert.equal(capped.x+capped.width<=1024&&capped.y+capped.height<=1024,true,'capped at the far corner');
 assert.equal(framePlacement(capped,corner.rings[0]),'inside');
 const tiny=initialImageFrame(initialViewRings([obj('t','building',rect(500,500,5,5),{carried_from:carried(SLICE_ROLE)})],windowScene),IMAGE,{width:600,height:600});
 assert.ok(tiny.width>=88&&tiny.height>=88,'a small object keeps the 55 px x 1.6 context');
 assert.ok(Math.abs(frameSpanMeters(square,P2M)-square.width*0.1)<1e-9,'span in metres through the pixel scale');

 // framePlacement.
 const f={x:0,y:0,width:100,height:100};
 assert.equal(framePlacement(f,rect(10,10,10,10)),'inside');assert.equal(framePlacement(f,rect(90,90,20,20)),'clipped');
 assert.equal(framePlacement(f,rect(200,200,10,10)),'outside');assert.equal(framePlacement(f,[]),'outside');

 // simplifyClosedRing: Douglas-Peucker on a closed ring.
 const squareWithMidpoints=[[0,0],[50,0],[100,0],[100,50],[100,100],[50,100],[0,100],[0,50]];
 assert.deepEqual(simplifyClosedRing(squareWithMidpoints,1),[[0,0],[100,0],[100,100],[0,100]],'collinear midpoints go, corners stay in order');
 assert.deepEqual(simplifyClosedRing([[50,0],[100,0],[100,100],[0,100],[0,0]],1),[[100,0],[100,100],[0,100],[0,0]],'a collinear first vertex goes too');
 const squareRing=rect(0,0,100,100);
 assert.equal(simplifyClosedRing(squareRing,1),squareRing,'a square is unchanged (same reference)');
 assert.equal(simplifyClosedRing(squareWithMidpoints,0),squareWithMidpoints,'tolerance 0 is unchanged');
 const triangle=[[0,0],[10,0],[0,10]];
 assert.equal(simplifyClosedRing(triangle,5),triangle,'a triangle is unchanged (same reference)');
 const bump=[[0,0],[50,1.5],[100,0],[100,100],[0,100]];
 assert.equal(simplifyClosedRing(bump,1).length,5,'a 1.5 px bump survives 1 px');
 assert.equal(simplifyClosedRing(bump,2).length,4,'and goes at 2 px');
 assert.equal(simplifyClosedRing([[0,0],[5,0],[10,0],[10,10]],1).length,3,'four vertices with one collinear become a triangle');

 // simplifyOutline: 1 px, then 2 px, refusals, holes, any kind.
 const scene={image_width:1024,image_height:1024,pixel_to_meters:P2M};
 const eight=obj('e8','building',squareWithMidpoints.map(([x,y])=>[x+100,y+100]),{status:'confirmed',geometry_reviewed:true});
 const step1=simplifyOutline(eight,scene);
 assert.equal(step1.ok,true);assert.equal(step1.tolerance,1);assert.deepEqual(step1.before,{vertices:8,area_m2:100});assert.deepEqual(step1.after,{vertices:4,area_m2:100});
 assert.equal(step1.object.ring.length,4);assert.equal(step1.object.status,'needs_adjustment');assert.equal(step1.object.geometry_reviewed,false);
 assert.equal('holes' in step1.object,false,'no holes key is invented');assert.equal(step1.object.id,'e8');assert.equal(eight.ring.length,8,'the input is untouched');
 const message=simplifyMessage(step1,'Building e8');
 assert.equal(message,'Building e8: simplified at 1 px, 8 → 4 points · area 100.0 → 100.0 m² (±0.0 %) · Undo edit restores the outline.');
 const step2=simplifyOutline(step1.object,scene);
 assert.equal(step2.ok,false);assert.match(step2.reason,/Nothing to simplify.*2 px/);
 const bumped=obj('b','building',bump.map(([x,y])=>[x+100,y+100]));
 const bumpStep=simplifyOutline(bumped,scene);
 assert.equal(bumpStep.ok,true);assert.equal(bumpStep.tolerance,2,'1 px removes nothing, so the 2 px step');
 assert.equal(bumpStep.after.vertices,4);assert.ok(bumpStep.after.area_m2>bumpStep.before.area_m2,'the bump dented the roof inward; squaring it adds area');
 assert.match(simplifyMessage(bumpStep,'Building b'),/\(\+0\.\d %\)/);
 assert.deepEqual(simplifyOutline({...eight,status:'superseded'},scene),{ok:false,reason:'A superseded outline is history; simplify its replacement instead.'});
 const outside=obj('o','building',[[-5,100],[50,100],[100,100],[100,200],[-5,200]]);
 const refused=simplifyOutline(outside,scene);
 assert.equal(refused.ok,false);assert.match(refused.reason,/^Simplify at 1 px refused: Keep the outline inside the image\. The outline is unchanged\.$/);
 const holed=obj('h','building',rect(100,100,100,100),{holes:[squareWithMidpoints.map(([x,y])=>[x/5+140,y/5+140])]});
 const holeStep=simplifyOutline(holed,scene);
 assert.equal(holeStep.ok,true);assert.equal(holeStep.tolerance,1);assert.deepEqual(holeStep.before,{vertices:12,area_m2:96});assert.deepEqual(holeStep.after,{vertices:8,area_m2:96});
 assert.equal(holeStep.object.holes.length,1);assert.equal(holeStep.object.holes[0].length,4);assert.equal(holeStep.object.ring,holed.ring,'an outer ring with nothing to remove is the same reference');
 const tree=obj('tr','tree',squareWithMidpoints.map(([x,y])=>[x/2+300,y/2+300]));
 assert.equal(simplifyOutline(tree,scene).ok,true,'any kind');
 assert.equal(simplifyOutline(obj('tri','building',[[100,100],[200,100],[100,200]]),scene).ok,false,'a triangle has nothing to remove');
 assert.equal(outlineAreaM2(rect(0,0,100,100),[rect(10,10,10,10)],P2M),99);

 // The census cases (read-only, skipped without data/scenes).
 const scenes=async(id)=>{try{const [review,latest]=await Promise.all([readFile(`data/scenes/${id}/review.json`,'utf8'),readFile(`data/scenes/${id}/latest.json`,'utf8')]);return {review:JSON.parse(review),latest:JSON.parse(latest)};}catch{return null;}};
 const b690=await scenes('win-b6901e4edcbc5495');
 if(b690){
  const objects=b690.review.objects, latest=b690.latest;
  const ids=askedObjectIds(objects);
  assert.deepEqual(ids.map(id=>id.slice(0,8)).sort(),['655f0498','88cefd7c','a2ff57ef','afd10322','c023a6d6','fd096767'],'six asked objects: three replacers, two decided slices, the pending slice afd10322');
  const initial=initialViewRings(objects,latest);
  assert.equal(initial.lot,false,'a win window: no planned lot');
  for(const [w,h] of [[600,600],[960,600]]){
   const frame=initialImageFrame(initial,{width:latest.image_width,height:latest.image_height},{width:w,height:h});
   for(const id of ids)assert.equal(framePlacement(frame,objects.find(o=>o.id===id).ring),'inside',`${id.slice(0,8)} inside the initial frame at ${w}x${h}`);
   assert.ok(frame.x>=0&&frame.y>=0&&frame.x+frame.width<=latest.image_width&&frame.y+frame.height<=latest.image_height,'capped to the image');
   console.log(`win-b6901e4edcbc5495 ${w}x${h}: frame x ${frame.x.toFixed(1)} y ${frame.y.toFixed(1)} ${frame.width.toFixed(1)} x ${frame.height.toFixed(1)} px, span ${frameSpanMeters(frame,latest.pixel_to_meters).toFixed(1)} m; asked 6, cluster +${initial.cluster.length} (${initial.cluster.map(id=>id.slice(0,8)).join(' ')})`);
  }
 } else console.log('win-b6901e4edcbc5495: data/scenes absent, census case skipped');
 const w694=await scenes('win-69468b2b783cd964');
 if(w694){
  const objects=w694.review.objects, latest=w694.latest;
  const ids=askedObjectIds(objects);
  assert.deepEqual(ids,['92b21ff38ffab81e'],'one asked slice');
  const initial=initialViewRings(objects,latest);
  assert.equal(windowLotPfi(latest),null,'rule subject, no lot');
  const car=objects.find(o=>o.id==='d86ebd7f0565eeac');
  assert.ok(car&&car.kind==='car','the named car is in the review');
  // The card predicted the car outside the asked slice's 40.8 m view. Under the lot/cluster rule it falls INSIDE: the asked slice
  // 92b21ff3 touches the candidate building 5e9d6927 (an umbrella-size piece, 335-719 x 22-653 px), which chains through
  // 6e35daf2 to ce3652b1 on the frame's top edge, so the union is the whole 1024 px scene (75.5 m). Measured, not a reading of
  // the car: the asked-only fit stays the card's 40.8 m with the car outside.
  const image={width:latest.image_width,height:latest.image_height};
  assert.deepEqual(initial.cluster.map(id=>id.slice(0,8)),['5e9d6927','6e35daf2','ce3652b1'],'the asked slice\'s cluster: three candidates');
  for(const [w,h] of [[600,600],[960,600]]){
   const frame=initialImageFrame(initial,image,{width:w,height:h});
   const askedOnly=capFrameToImage(objectImageFrame([objects.find(o=>o.id===ids[0]).ring],image,{width:w,height:h},false),image);
   const placement=framePlacement(frame,car.ring);
   assert.equal(placement,'inside',`the car d86ebd7f0565eeac falls inside the lot/cluster initial view at ${w}x${h} (the cluster reaches the frame top)`);
   assert.deepEqual(frame,{x:0,y:0,width:1024,height:1024},'the cluster union is the whole scene');
   assert.equal(framePlacement(askedOnly,car.ring),'outside','the asked-only fit leaves the car outside, as the card read it');
   assert.equal(frameSpanMeters(askedOnly,latest.pixel_to_meters).toFixed(1),'40.8','the card\'s 40.8 m asked-only view');
   console.log(`win-69468b2b783cd964 ${w}x${h}: initial frame ${frame.width.toFixed(1)} x ${frame.height.toFixed(1)} px, span ${frameSpanMeters(frame,latest.pixel_to_meters).toFixed(1)} m (asked-only ${frameSpanMeters(askedOnly,latest.pixel_to_meters).toFixed(1)} m); cluster +${initial.cluster.length}; car d86ebd7f0565eeac ${placement} (asked-only: ${framePlacement(askedOnly,car.ring)})`);
  }
 } else console.log('win-69468b2b783cd964: data/scenes absent, census case skipped');

 // Static pins: the canvas, the page, the Taskfile.
 const canvas=await readFile('src/components/scenes/SceneCanvas.tsx','utf8');
 assert(canvas.includes('import { initialImageFrame, initialViewRings, windowLotPfi } from "@/lib/scene-initial-view";'),'the canvas reads the helpers');
 assert(canvas.includes('useState(mode==="review"&&!!scene.window_frame)'),'the initial view starts only on a window scene in review mode');
 assert(canvas.includes('useLayoutEffect(()=>{if(selected!==mountSelected.current)setInitialView(false);},[selected]);'),'the first selection change ends it');
 assert(canvas.includes('onClick={() => {setInitialView(false);setParcelFocus(null);'),'Whole scene / Focus object ends it');
 assert(canvas.includes('onClick={()=>{setInitialView(false);setParcelFocus(fittingParcel?null:selected);setZoom(1);}}'),'Fit parcel ends it');
 assert(canvas.includes('parcels.data?.features.find(f=>String(f.properties.pfi)===lotPfi)'),'the planned lot among the loaded Vicmap features');
 assert(canvas.includes('initialViewRings(objects,scene,windowLotRings)'),'asked objects, clusters and the lot rings');
 assert(canvas.includes('}:initialFrame?initialFrame:focused?objectImageFrame('),'the initial frame sits between Fit parcel and the per-object fit');
 assert(canvas.includes('data-initial-view={initialFrame?"true":"false"}'),'the svg says when the initial view is on');
 const page=await readFile('src/app/scenes/page.tsx','utf8');
 assert(page.includes('setSelected(requestedObject || result.model.objects[0]?.id || null);'),'the URL object still selects');
 assert(page.includes('import { simplifyMessage, simplifyOutline } from "@/lib/scene-simplify";'),'the page reads the simplifier');
 assert(page.includes('const result = simplifyOutline(inspected, data.model);'),'Simplify runs on the inspected object against the image');
 assert(page.includes('const dropped = changeObject(result.object);'),'through changeObject -> change(): one undo entry');
 assert(page.includes('disabled={busy || inspected.status === "superseded"}\n                          title="One Douglas-Peucker step'),'never on a superseded object');
 assert(page.includes('Simplify outline\n                        </button>'),'the button');
 assert(page.includes('    return dropped;\n  };'),'changeObject reports the dropped roof lines');
 const taskfile=await readFile('Taskfile.yml','utf8');
 assert(taskfile.includes('      - node scripts/test-scene-initial-view.mjs\n'),'registered in test:reviews');
 console.log('scene initial view + simplify: ok');
} finally {
 await rm(directory,{recursive:true,force:true});
}
