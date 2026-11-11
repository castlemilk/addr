import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import ts from "typescript";

// Metres per degree at the scene latitude: longitude shrinks by cos(lat); Mercator latitude does not (small-angle).
const WORLD_SCALE=scene=>40075016.68557849*Math.cos(scene.centroid.lat*Math.PI/180)/360, LAT_SCALE=40075016.68557849/360;
const directory=await mkdtemp(path.join(tmpdir(),"addr-parcels-"));
try {
  for (const name of ["property-parcels","property-parcel-service"]) {
    const code=ts.transpileModule(await readFile(`src/lib/${name}.ts`,"utf8"),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replaceAll(/from "\.\/([a-z-]+)"/g,'from "./$1.mjs"');
    await writeFile(path.join(directory,`${name}.mjs`),code);
  }
  const geo=await import(pathToFileURL(path.join(directory,"property-parcels.mjs")));
  const {fetchParcels,cachedParcels}=await import(pathToFileURL(path.join(directory,"property-parcel-service.mjs")));
  const bounds=[145.312,-37.791,145.313,-37.790];
  for(const bad of [[],[1,2,3,NaN],[145,-38,145,-37],[-180,-80,180,80],[145,-38,146,-37]]) assert(geo.parcelBoundsIssue(bad));
  assert.equal(geo.parcelBoundsIssue(bounds),null);
  assert.equal(geo.parcelBoundsIssue([145,-37.8,145.01,-37.79]),null);
  assert(geo.parcelBoundsIssue([145,-37.8,145.01,-37.79],700),"Overview maps should request zoom before fetching thousands of lots");
  const outer=[[145.312,-37.791],[145.313,-37.791],[145.313,-37.790],[145.312,-37.790],[145.312,-37.791]];
  const hole=[[145.3124,-37.7906],[145.3126,-37.7906],[145.3126,-37.7904],[145.3124,-37.7904],[145.3124,-37.7906]];
  const raw={type:"Feature",id:42,geometry:{type:"Polygon",coordinates:[outer,hole]},properties:{OBJECTID:42,parcel_pfi:"abc",parcel_spi:"2\\PS123",parcel_status:"A",parv_status:"A",parv_z_level:"G",parv_horiz_pos_uncertainty:.1}};
  const reply=(features,extra={})=>new Response(JSON.stringify({type:"FeatureCollection",features,...extra}));
  let queries=[];
  const fake=async url=>{queries.push(new URL(url));return reply([raw]);};
  const data=await fetchParcels(bounds,fake);
  assert.deepEqual(data.features[0].geometry,raw.geometry,"Retain holes and exact source coordinates");
  assert.equal(data.features[0].properties.horizontal_uncertainty_m,.1);
  assert.equal(data.source.service,geo.PARCEL_SOURCE.service);
  assert(data.retrieved_at);
  const params=queries[0].searchParams;
  assert.equal(params.get("outSR"),"4326");assert.equal(params.get("inSR"),"4326");
  assert(params.get("where").includes("parcel_status='A'"));assert(params.get("where").includes("parv_z_level IN ('G','S')"));
  assert.equal(params.get("geometry"),bounds.join(","));
  const multi={...raw,id:43,geometry:{type:"MultiPolygon",coordinates:[[outer,hole],[outer.map(([x,y])=>[x+.01,y])]]}};
  const result=await fetchParcels(bounds,async()=>reply([multi]));
  assert.deepEqual(result.features[0].geometry,multi.geometry,"Do not discard multipart parcels");
  assert.equal(geo.parcelContains([145.3125,-37.7905],[raw.geometry.coordinates]),false,"A hole is outside the parcel");
  assert.equal(geo.parcelContains([145.3121,-37.7905],[raw.geometry.coordinates]),true);
  assert.equal(geo.parcelContains([145.314,-37.7905],[raw.geometry.coordinates]),false);
  let page=0;
  assert.equal((await fetchParcels(bounds,async url=>{assert.equal(new URL(url).searchParams.get("resultOffset"),String(page*500));return page++===0?reply([raw],{exceededTransferLimit:true}):reply([multi]);})).features.length,2);
  await assert.rejects(fetchParcels(bounds,async()=>reply([raw],{exceededTransferLimit:true})),/repeated/);
  await assert.rejects(fetchParcels(bounds,async()=>new Response(JSON.stringify({error:{message:"upstream error"}}))),/Invalid Vicmap/);
  await assert.rejects(fetchParcels(bounds,async()=>new Response("unavailable",{status:503})),/unavailable/);
  await assert.rejects(fetchParcels(bounds,async()=>reply([{...raw,geometry:{type:"Polygon",coordinates:[[[0,0],[1,1],[NaN,3],[0,0]]]}}])),/coordinates/);
  await assert.rejects(fetchParcels(bounds,async()=>reply([{...raw,properties:{...raw.properties,parcel_status:"P"}}])),/status/);
  assert.deepEqual((await fetchParcels(bounds,async()=>reply([]))).features,[],"No fabricated fallback for no coverage");

  // The fixture frame: 512 px at 8 px/m (0.125 m/px) about the centroid, with both affines and the registered bounds
  // that frame implies (computed here with the small-angle metre helpers, independently of the library's inverse).
  const centroid={lon:145.3125,lat:-37.7905};
  const lonAt=x=>centroid.lon+(x-256)/(8*WORLD_SCALE({centroid}));
  const latAt=y=>centroid.lat-(y-256)/(8*LAT_SCALE);
  const frameBounds=[lonAt(0),latAt(512),lonAt(512),latAt(0)];
  const scene={centroid,image_width:512,image_height:512,meters_to_pixel:[[8,0],[0,-8],[256,256]],pixel_to_meters:[[.125,0],[0,-.125],[-32,32]],region:{bounds:frameBounds}};
  assert.deepEqual(geo.parcelImagePoint([145.3125,-37.7905],scene),[256,256]);
  assert(geo.parcelImagePoint([145.3125,-37.7904],scene)[1]<256,"North is upwards in image coordinates");
  assert(geo.parcelImagePoint([145.3126,-37.7905],scene)[0]>256,"East is rightwards");
  // Pixel -> WGS84 is the exact inverse of parcelImagePoint.
  for(const p of [[0,0],[512,512],[37.5,400.25],[256,256]]){
    const back=geo.parcelImagePoint(geo.sceneLonLat(p,scene),scene);
    assert(Math.hypot(back[0]-p[0],back[1]-p[1])<1e-6,`sceneLonLat round trip ${p} -> ${back}`);
  }
  // Geographic bounds: a registered capture as given; a cohort frame (no region) fitted from its affines, within 1e-6 deg of
  // the frame the registered bounds describe; nothing without a centroid.
  assert.deepEqual(geo.sceneGeoBounds(scene),{bounds:frameBounds,registration:"region"});
  const fitted=geo.sceneGeoBounds({...scene,region:undefined});
  assert.equal(fitted.registration,"fitted");
  fitted.bounds.forEach((v,i)=>assert(Math.abs(v-frameBounds[i])<1e-6,`fitted bounds ${fitted.bounds} vs ${frameBounds}`));
  assert.equal(geo.sceneGeoBounds({...scene,region:undefined,centroid:undefined}),null);
  assert.equal(geo.sceneParcelBounds({...scene,region:undefined,centroid:undefined}),null);
  const padded=geo.sceneParcelBounds({...scene,region:undefined});
  assert(Math.abs((padded[2]-padded[0])-2*(frameBounds[2]-frameBounds[0]))<1e-9,"Fitted bounds are padded by half the span per side, as registered ones are");
  // A cohort-shaped frame: 512 px at 0.065 m/px is about 33 m across.
  const cohort={centroid:{lon:145.31551,lat:-37.79160},image_width:512,image_height:512,pixel_to_meters:[[.065,0],[0,-.065],[-16.64,16.64]],meters_to_pixel:[[1/.065,0],[0,-1/.065],[256,256]]};
  const cohortGeo=geo.sceneGeoBounds(cohort);
  assert.equal(cohortGeo.registration,"fitted");
  // Longitude scales by cos(lat), Mercator latitude does not: 16.64 m is 1.89e-4 deg east-west and 1.49e-4 deg north-south.
  const cohortExpected=[cohort.centroid.lon-16.64/WORLD_SCALE(cohort),cohort.centroid.lat-16.64/LAT_SCALE,cohort.centroid.lon+16.64/WORLD_SCALE(cohort),cohort.centroid.lat+16.64/LAT_SCALE];
  cohortExpected.forEach((v,i)=>assert(Math.abs(cohortGeo.bounds[i]-v)<1e-6,`cohort bounds ${cohortGeo.bounds} vs ${cohortExpected}`));
  [145.315321,-37.79176,145.315699,-37.791461].forEach((v,i)=>assert(Math.abs(cohortGeo.bounds[i]-v)<2e-5,`cohort bounds envelope ${cohortGeo.bounds}`));
  assert(Math.abs((cohortGeo.bounds[2]-cohortGeo.bounds[0])*WORLD_SCALE(cohort)-33.28)<.05,"About 33 m across");
  assert.equal(geo.parcelBoundsIssue(geo.sceneParcelBounds(cohort)),null,"A padded cohort frame is a valid Vicmap query");
  const f=data.features[0],ring=outer.slice(0,4).map(([x,y])=>geo.parcelImagePoint([145.3121+(x-145.312)*.15,-37.79095+(y+37.791)*.15],scene));
  assert.equal(geo.parcelNearOutline([f],ring,scene),f);
  assert.equal(geo.parcelNearOutline([f,{...f,id:"duplicate"}],ring,scene),null,"Do not silently choose among overlapping cadastral records");
  assert.equal(geo.parcelNearOutline([f],hole.slice(0,4).map(p=>geo.parcelImagePoint(p,scene)),scene),null,"A roof in a parcel hole is not assigned to the outer parcel");

  const originalFetch=globalThis.fetch;
  try {
    let calls=0;
    globalThis.fetch=async()=>{calls++;throw new Error("offline");};
    await assert.rejects(cachedParcels(bounds),/offline/);
    globalThis.fetch=async()=>{calls++;return reply([raw]);};
    const [a,b]=await Promise.all([cachedParcels(bounds),cachedParcels(bounds)]);
    assert.equal(a,b);assert.equal(calls,2,"Failed lookups are retryable and concurrent lookups are deduplicated");
  } finally {globalThis.fetch=originalFetch;}

  // Unit rows: several lots cut one outline. Every touching lot is listed with its share, in share order.
  const lotFeature=(id,lot,coords,plan="PS735592")=>({type:"Feature",id,geometry:{type:"Polygon",coordinates:coords},properties:{pfi:`pfi-${id}`,spi:`${lot}\\${plan}`,lot,plan,status:"approved",horizontal_uncertainty_m:.5,geometry_record_created:null}});
  const pxBox=(x0,y0,x1,y1)=>[[[lonAt(x0),latAt(y0)],[lonAt(x1),latAt(y0)],[lonAt(x1),latAt(y1)],[lonAt(x0),latAt(y1)],[lonAt(x0),latAt(y0)]]];
  const lots=[lotFeature("a","11",pxBox(100,100,140,200)),lotFeature("b","12",pxBox(140,100,180,200)),lotFeature("c","13",pxBox(180,100,220,200)),lotFeature("cm","CM1",pxBox(100,200,220,230)),lotFeature("far","1",pxBox(400,400,440,440),"LP1")];
  const rowRing=[[104,120],[216,120],[216,196],[104,196]];
  const touching=geo.parcelsTouchingOutline(lots,rowRing,scene);
  assert.deepEqual(touching.map(t=>t.feature.properties.lot).sort(),["11","12","13"],"Every lot under the outline is listed; the far lot and the common property strip are excluded");
  assert(touching.every((t,i)=>i===0||touching[i-1].share>=t.share),"Touching lots are ordered by their share of the outline");
  assert(touching.every(t=>t.share>.25&&t.share<.4),`Shares are fractions of the outline: ${touching.map(t=>t.share)}`);
  assert.deepEqual(geo.parcelsTouchingOutline(lots,[[400,401],[402,401],[402,403]],scene).map(t=>t.feature.id),["far"]);
  assert.deepEqual(geo.parcelsTouchingOutline(lots,[[10,10],[20,10],[20,20],[10,20]],scene),[],"An outline in no lot touches nothing");
  assert.deepEqual(geo.parcelsTouchingOutline(lots,[[10,10]],scene),[],"Degenerate rings are ignored");
  assert.equal(geo.parcelNearOutline(lots,rowRing,scene),null,"The single-lot hint still refuses to choose among several lots");
  assert.equal(geo.parcelNearOutline(lots,[[404,404],[436,404],[436,436],[404,436]],scene),lots[4],"The single-lot hint is unchanged for one containing lot");

  // Stacked strata lots share one polygon: render once, label together, never pretend there are cuts.
  const stacked=[1,2,3,4].map(i=>lotFeature(`s${i}`,String(i),pxBox(300,100,360,180),"PS907511")).concat(lotFeature("scm","CM1",pxBox(300,100,360,180),"PS907511"));
  const groups=geo.parcelGeometryGroups([...lots,...stacked]);
  assert.equal(groups.length,lots.length+1,"Coincident polygons collapse into one drawn group");
  const stackedGroup=groups.find(g=>g.features.length>1);
  assert.deepEqual(stackedGroup.features.map(f=>f.properties.lot),["1","2","3","4","CM1"],"Lots are listed in lot order with common property last");
  assert.equal(stackedGroup.feature,stackedGroup.features[0]);
  assert.deepEqual(groups.filter(g=>g.features.length===1).map(g=>g.feature.id),lots.map(l=>l.id));
  const stackedRing=[[304,104],[356,104],[356,176],[304,176]];
  assert.equal(geo.parcelOutlineSummary(geo.parcelsTouchingOutline(stacked,stackedRing,scene)),"5 stacked lots share one boundary · PS907511 · no unit cuts in Vicmap");
  assert.equal(geo.parcelOutlineSummary(touching),"3 lots touch this outline · PS735592");
  assert.equal(geo.parcelOutlineSummary(geo.parcelsTouchingOutline(lots,[[404,404],[436,404],[436,436],[404,436]],scene)),"Lot 1 · LP1");
  assert.equal(geo.parcelOutlineSummary([]),null);
  assert.equal(geo.parcelOutlineSummary(geo.parcelsTouchingOutline([lots[0],lotFeature("x","5",pxBox(100,100,140,200),"PS9")],[[104,120],[136,120],[136,196],[104,196]],scene)),"2 stacked lots share one boundary · PS735592, PS9 · no unit cuts in Vicmap");

  // Lot labels: sentence case, common property named, stacked lots joined; label point sits inside the lot.
  assert.equal(geo.parcelLotLabel([lots[0]]),"Lot 11");
  assert.equal(geo.parcelLotLabel([lots[3]]),"Common property CM1");
  assert.equal(geo.parcelLotLabel(stacked),"Lots 1, 2, 3, 4 + CM1 (stacked)");
  assert.equal(geo.parcelLotLabel([{...lots[0],properties:{...lots[0].properties,lot:""}}]),"Parcel 11\\PS735592");
  const point=geo.parcelLabelPoint(geo.parcelImagePolygons(lots[0],scene));
  assert(point[0]>100&&point[0]<140&&point[1]>100&&point[1]<200,`Label point inside the lot: ${point}`);
  assert(geo.parcelContains(point,geo.parcelImagePolygons(lots[0],scene)));
  const lShape=[[[[0,0],[100,0],[100,20],[20,20],[20,100],[0,100],[0,0]]]];
  assert(geo.parcelContains(geo.parcelLabelPoint(lShape),lShape),"A concave lot still gets a label point inside it");
  // Degenerate records must not break the whole overlay: a null lot (unnumbered parcel) and empty geometry.
  const nullLot={...lots[0],id:"nul",properties:{...lots[0].properties,lot:null,spi:"CA12\\PP1"}};
  assert.equal(geo.parcelGeometryGroups([nullLot,lots[1]]).length,2,"A null lot number does not throw while grouping");
  assert.equal(geo.parcelLotLabel([nullLot]),"Parcel CA12\\PP1");
  assert.equal(geo.parcelLotLabel([nullLot,{...nullLot,id:"nul2",properties:{...nullLot.properties,spi:"CA13\\PP1"}}]),"Parcels unnumbered, unnumbered (stacked)");
  assert.equal(geo.parcelLotLabel([]),"");
  assert.deepEqual(geo.parcelsTouchingOutline([nullLot,lots[1]],rowRing,scene).map(t=>t.feature.id).sort(),["b","nul"],"Touching lots with a null lot still sort");
  assert.equal(geo.parcelLabelPoint([]),null,"No geometry, no label point");
  assert.equal(geo.parcelLabelPoint([[[]]]),null);
  assert.deepEqual(geo.parcelLabelPoint([[[]],lShape[0]]),geo.parcelLabelPoint(lShape),"Empty parts are skipped");
  // Lot sentence: the selected object's lot with its share, the largest live roof on it and the size ratio; a lot leaving
  // the frame says so and blames the frame only when the outline touches the frame edge. A viewing hint, never a label.
  const building=(id,ring,extra={})=>({id,kind:"building",label:"Building",ring,status:"candidate",source:"sam3-text-onnx",geometry_reviewed:false,model:{},...extra});
  const main=building("7e6f04dfaaaa",[[110,110],[130,110],[130,190],[110,190]]),garage=building("689ea997bbbb",[[132,110],[138,110],[138,150],[132,150]]);
  const gone=building("deadbeef0000",[[110,110],[139,110],[139,199],[110,199]],{status:"rejected"});
  const objects=[main,garage,gone];
  const garageTouching=geo.parcelsTouchingOutline(lots,garage.ring,scene);
  assert.equal(geo.parcelLotSentence(garageTouching,garage,objects,scene),"Lot 11 PS735592 (100%); largest roof here is 7e6f04df… 25.0 m²; this is 3.8 m² (ratio 0.15).");
  assert.equal(geo.parcelLotSentence(geo.parcelsTouchingOutline(lots,main.ring,scene),main,objects,scene),"Lot 11 PS735592 (100%); the largest roof on this lot (25.0 m²).");
  assert.equal(geo.parcelLotSentence(garageTouching,{...garage,kind:"tree"},objects,scene),"Lot 11 PS735592 (100%).");
  assert.equal(geo.parcelLotSentence([],garage,objects,scene),null);
  assert.equal(geo.parcelLotSentence(garageTouching,undefined,objects,scene),null);
  const leaving=lotFeature("edge","2",pxBox(440,100,600,200),"PS729270");
  const cut=building("593036a5cccc",[[470,120],[512,120],[512,180],[470,180]]),inner=building("a45a68d3dddd",[[450,120],[480,120],[480,180],[450,180]]);
  assert.equal(geo.parcelLotSentence(geo.parcelsTouchingOutline([leaving],cut.ring,scene),cut,[cut,inner],scene),"Sits on Lot 2 PS729270, which leaves this image; the frame cut it, not the building. The largest roof on this lot (39.4 m²).");
  assert.equal(geo.parcelLotSentence(geo.parcelsTouchingOutline([leaving],inner.ring,scene),inner,[cut,inner],scene),"Sits on Lot 2 PS729270, which leaves this image; only part of the lot is visible. Largest roof here is 593036a5… 39.4 m²; this is 28.1 m² (ratio 0.71).");
  assert.equal(geo.parcelLotSentence(geo.parcelsTouchingOutline([leaving],cut.ring,scene),{...cut,kind:"tree"},[cut,inner],scene),"Sits on Lot 2 PS729270, which leaves this image; the frame cut it, not the tree.");
  console.log("Frame geography: pixel/WGS84 inverse, registered vs fitted bounds, cohort frame, lot sentence passed.");
  console.log("Parcel reference: bounds, projection, holes/multipart, ambiguous proximity, approved/surface filtering, pagination, failure/no-coverage handling, retry and cache passed.");
  console.log("Unit lots: touching lots with shares, stacked strata grouping, outline summary, lot labels and label points passed.");
} finally {await rm(directory,{recursive:true,force:true});}
