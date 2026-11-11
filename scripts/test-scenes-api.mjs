import assert from "node:assert/strict";
import { readFile, writeFile, mkdir, rm, cp } from "node:fs/promises";
const base = process.env.APP_URL || "http://localhost:3008";
const id = `qa_scene_${Date.now()}`,
  directory = `data/scenes/${id}`;
await mkdir(directory, { recursive: true });
try {
  const fixture = JSON.parse(
    await readFile("data/scenes/roof_training_250-29790/latest.json", "utf8"),
  );
  fixture.id = id;
  await mkdir(`${directory}/models`);
  await writeFile(`${directory}/models/${fixture.model_signature}.json`,JSON.stringify(fixture));
  await writeFile(`${directory}/latest.json`, JSON.stringify(fixture));
  await cp(
    `data/scenes/roof_training_250-29790/${fixture.image_sha256}.png`,
    `${directory}/${fixture.image_sha256}.png`,
  );
  const load = async () => {
    const r = await fetch(`${base}/api/scenes?id=${id}`);
    assert.equal(r.status, 200);
    return r.json();
  };
  const initial = await load();
  const summaries=await (await fetch(`${base}/api/scenes?kind=car&status=needs_adjustment&q=vehicle`)).json();
  assert(summaries.scenes.length>0);
  assert(!summaries.scenes.some(s=>s.id.startsWith('qa_')),'Temporary scenes stay out of the real review queue');
  for(const summary of summaries.scenes) {
    const scene=await (await fetch(`${base}/api/scenes?id=${summary.id}`)).json();
    const objects=scene.stale?scene.model.objects:scene.review?.objects||scene.model.objects;
    const eligible=objects.filter(o=>o.kind==='car'&&o.status==='needs_adjustment'&&`${o.label} ${o.kind} ${o.id}`.toLowerCase().includes('vehicle'));
    assert.equal(summary.queue_count,eligible.length);assert.equal(summary.next_object?.id||null,eligible[0]?.id||null);
    assert.equal(summary.progress.needs_adjustment,objects.filter(o=>o.status==='needs_adjustment').length);
    assert.equal(summary.reviewed,objects.filter(o=>['confirmed','rejected'].includes(o.status)).length);
    assert.equal(summary.stale,scene.stale);
  }
  let review = {
    revision: 0,
    model_signature: fixture.model_signature,
    objects: structuredClone(fixture.objects),
    groups: fixture.groups,
    reviewed_classes: [],
  };
  const post = async (value) =>
    fetch(`${base}/api/scenes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, review: value }),
    });
  const target = review.objects.find((o) => o.kind === "car");
  assert(target);
  target.status = "confirmed";
  target.geometry_reviewed = true;
  let r = await post(review);
  assert.equal(r.status, 200, await r.clone().text());
  review = (await r.json()).review;
  assert.equal(review.revision, 1);
  const reloaded = await load();
  assert.equal(
    reloaded.review.objects.find((o) => o.id === target.id).status,
    "confirmed",
  );
  assert.deepEqual(reloaded.model.objects, initial.model.objects);
  r = await post({ ...review, revision: 0 });
  assert.equal(r.status, 409);
  r = await post({ ...review, model_signature: "stale" });
  assert.equal(r.status, 409);
  const invalid = structuredClone(review);
  invalid.objects.find((o) => o.id === target.id).ring = [
    [10, 10],
    [50, 50],
    [10, 50],
    [50, 10],
  ];
  r = await post(invalid);
  assert.equal(r.status, 400);
  for(const holes of [null,[null],[[]],[[[20,20],[80,20],[80,80],[20,80]]],
    [[[10,20],[30,20],[30,30],[10,30]]],
    [[[20,20],[40,20],[40,40],[20,40]],[[25,25],[30,25],[30,30],[25,30]]]]){
    const invalidGap=structuredClone(review);
    Object.assign(invalidGap.objects[0],{ring:[[10,10],[60,10],[60,60],[10,60]],holes});
    assert.equal((await post(invalidGap)).status,400,'Invalid interior gaps must be rejected rather than crash or fill');
  }
  const invalidPlacement=structuredClone(review);
  for(const issues of [null,'height',{},['unknown'],['height','height'],[false]]) {
    const invalidFeedback=structuredClone(review);invalidFeedback.objects[0].review_issues=issues;
    assert.equal((await post(invalidFeedback)).status,400,'Malformed feedback is rejected');
  }
  const contradictory=structuredClone(review);contradictory.objects.find(o=>o.id===target.id).review_issues=['outline'];
  assert.equal((await post(contradictory)).status,400,'Outstanding 2D feedback prevents approval');
  const flaggedHeight=structuredClone(review);flaggedHeight.objects[0].review_issues=['height'];flaggedHeight.objects[0].model.height_verified=true;
  assert.equal((await post(flaggedHeight)).status,400,'A disputed height is not measured supervision');
  invalidPlacement.objects[0].model.placement='unsupported';
  assert.equal((await post(invalidPlacement)).status,400);
  for(const params of [{profile:'unknown'},{profile:'car',heading_deg:360},{profile:'van',heading_deg:null},
    {profile:'car',cabin_length_ratio:.1},{profile:'car',cabin_width_ratio:1},{profile:'suv',cabin_offset_ratio:.5}]) {
    const invalidCar=structuredClone(review);invalidCar.objects.find(o=>o.kind==='car').model.car=params;
    assert.equal((await post(invalidCar)).status,400);
  }
  const wrongClass=structuredClone(review);wrongClass.objects.find(o=>o.kind==='tree').model.car={profile:'car'};
  assert.equal((await post(wrongClass)).status,400);
  for(const feedback of [null,[],true,{family:'unknown'}, {family:'gabled',outline:{ring:null,holes:[]}},
    {family:'gabled',outline:{ring:[[0,0],[10,0],[0,10]],holes:[]},linework:{vertices:[null]}}]) {
    const invalidRoof=structuredClone(review);invalidRoof.objects.find(o=>o.kind==='building').roof_feedback=feedback;
    assert.equal((await post(invalidRoof)).status,400,'Malformed roof observations must not enter training');
  }
  const wrongRoof=structuredClone(review);wrongRoof.objects.find(o=>o.kind==='tree').roof_feedback={family:'gabled',outline:{ring:[[0,0],[10,0],[0,10]],holes:[]}};
  assert.equal((await post(wrongRoof)).status,400,'Roof observations apply only to buildings');
  for(const params of [null,{profile:'unknown'},{profile:'plane',heading_deg:360},{profile:'plane',heading_deg:null},
    {profile:'plane',grade_percent:30.1},{profile:'plane',crossfall_percent:-15.1},{profile:'crowned',crossfall_percent:-.1}]) {
    const invalidSurface=structuredClone(review);invalidSurface.objects.find(o=>o.kind==='road').model.surface=params;
    assert.equal((await post(invalidSurface)).status,400);
  }
  const wrongSurface=structuredClone(review);wrongSurface.objects.find(o=>o.kind==='tree').model.surface={profile:'plane'};
  assert.equal((await post(wrongSurface)).status,400);
  for(const settings of [null,[],true,"bad",{tilt_deg:25},{azimuth_deg:90},{tilt_deg:-1,azimuth_deg:0},{tilt_deg:81,azimuth_deg:0},
    {tilt_deg:25,azimuth_deg:360},{tilt_deg:null,azimuth_deg:0},{clearance_m:-.01},{clearance_m:3.1},{clearance_m:null}]){
    const invalidSolar=structuredClone(review);invalidSolar.objects.find(o=>o.kind==='solar_panel').model.solar=settings;
    assert.equal((await post(invalidSolar)).status,400);
  }
  const wrongSolar=structuredClone(review);wrongSolar.objects.find(o=>o.kind==='tree').model.solar={clearance_m:.1};
  assert.equal((await post(wrongSolar)).status,400);
  for(const visibility of [null,'unassessed','invalid',false,{truncated:true}]) {
    const invalidVisibility=structuredClone(review);invalidVisibility.objects[0].visibility=visibility;
    assert.equal((await post(invalidVisibility)).status,400);
  }
  for(const params of [{crown_profile:'unsupported',crown_base_ratio:.2},{crown_profile:'rounded',crown_base_ratio:1.2},{crown_profile:'conical',crown_base_ratio:null}]) {
    const invalidTree=structuredClone(review);
    invalidTree.objects.find(o=>o.kind==='tree').model.tree=params;
    assert.equal((await post(invalidTree)).status,400);
  }
  const cycle = structuredClone(review);
  cycle.objects[0].parent_id = cycle.objects[1].id;
  cycle.objects[1].parent_id = cycle.objects[0].id;
  r = await post(cycle);
  assert.equal(r.status, 400);
  r = await post({ ...review, reviewed_classes: ["tree"] });
  assert.equal(r.status, 400);
  const next = structuredClone(review);
  const tree = next.objects.find((o) => o.kind === "tree");
  tree.model.height_m = 9.2;
  tree.model.height_source = "reviewer-estimate";
  tree.model.tree = {crown_profile:'spreading',crown_base_ratio:.4};
  const vehicle={profile:'van',heading_deg:212.5,cabin_length_ratio:.77,cabin_width_ratio:.83,cabin_offset_ratio:-.12};
  next.objects.find(o=>o.kind==='car').model.car=vehicle;
  next.objects.find(o=>o.kind==='car').visibility='occluded_truncated';
  const surface={profile:'crowned',heading_deg:212.5,grade_percent:-3.75,crossfall_percent:2.25};
  next.objects.find(o=>o.kind==='road').model.surface=surface;
  const road=next.objects.find(o=>o.kind==='road');
  road.ring=[[40,40],[400,40],[400,300],[40,300]];
  road.holes=[[[100,100],[150,100],[150,150],[100,150]]];
  const solar={tilt_deg:22.5,azimuth_deg:112.75,clearance_m:.15};
  next.objects.find(o=>o.kind==='solar_panel').model.solar=solar;
  next.groups.push({ id: "garden", label: "Garden" });
  tree.group_id = "garden";
  r = await post(next);
  assert.equal(r.status, 200, await r.clone().text());
  review = (await r.json()).review;
  assert.equal(review.history.length, 1);
  assert.deepEqual(review.objects.find(o=>o.id===tree.id).model.tree,tree.model.tree);
  assert.deepEqual(review.objects.find(o=>o.kind==='car').model.car,vehicle);
  assert.equal(review.objects.find(o=>o.kind==='car').visibility,'occluded_truncated');
  assert.deepEqual(review.objects.find(o=>o.kind==='road').model.surface,surface);
  assert.deepEqual(review.objects.find(o=>o.id===road.id).holes,road.holes);
  assert.equal(review.objects.find(o=>o.id===road.id).model.footprint_area_px,91100);
  assert.deepEqual(review.objects.find(o=>o.kind==='solar_panel').model.solar,solar);
  assert.equal(
    review.objects.find((o) => o.id === tree.id).model.height_m,
    9.2,
  );
  r = await post({
    ...review,
    ...review.history[0],
    revision: review.revision,
  });
  assert.equal(r.status, 200, await r.clone().text());
  const restored = await load();
  assert.equal(
    restored.review.objects.find((o) => o.id === target.id).status,
    "confirmed",
  );
  const touching=structuredClone(restored.review),touchingRoad=touching.objects.find(o=>o.kind==='road');
  touchingRoad.ring=[[10,10],[60,10],[60,60],[10,60]];
  touchingRoad.holes=[[[10,30],[20,25],[20,35]],[[20,35],[30,35],[30,45],[20,45]]];
  r=await post(touching);
  assert.equal(r.status,200,await r.clone().text());
  const touchingSaved=await load();
  assert.deepEqual(touchingSaved.review.objects.find(o=>o.id===touchingRoad.id).holes,touchingRoad.holes,
    'Saving valid point contacts must preserve the exact original mask');
  assert.equal(touchingSaved.review.objects.find(o=>o.id===touchingRoad.id).model.footprint_area_px,2350);
  assert.deepEqual(touchingSaved.model.objects,initial.model.objects);
  const disconnected=structuredClone(touchingSaved.review);
  disconnected.objects.find(o=>o.id===touchingRoad.id).holes=[[[10,30],[35,20],[60,30],[35,40]]];
  assert.equal((await post(disconnected)).status,400,'Two shell contacts cannot disconnect the polygon interior');
  const rejectedOrigin = await fetch(`${base}/api/scenes`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: "https://outside.example",
    },
    body: JSON.stringify({ id, review }),
  });
  assert.equal(rejectedOrigin.status, 403);
  assert.equal((await fetch(`${base}/api/scenes?id=..%2fprivate`)).status, 400);
  for(const body of [
    {bounds:[0,0,1,1],provider:'google'},
    {bounds:[145.3121,-37.7908,145.313,-37.7900],provider:'unsupported'},
    {bounds:[145.3121,-37.7908,145.313,-37.7900],provider:'google',scene_id:id},
    {scene_id:'../private'},
    {scene_id:id,cohort:'roof_training_250'},
    {cohort:'roof_training_250',address_id:29790,capture_only:true},
  ]){
    const invalidCapture=await fetch(`${base}/api/scenes/extract`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    assert.equal(invalidCapture.status,400,JSON.stringify(body));
  }
  const updated={...fixture,model_signature:'c'.repeat(64)};
  await writeFile(`${directory}/models/${updated.model_signature}.json`,JSON.stringify(updated));
  await writeFile(`${directory}/latest.json`,JSON.stringify(updated));
  const changed=await load();assert(changed.stale);assert(changed.recovery);
  assert.equal(changed.recovery.review.objects.find(o=>o.id===target.id).status,'needs_adjustment');
  assert.equal(changed.recovery.review.objects.find(o=>o.id===target.id).geometry_reviewed,false);
  r=await post(changed.recovery.review);assert.equal(r.status,200,await r.clone().text());
  const recovered=(await r.json()).review;
  assert.equal(recovered.history.at(-1).model_signature,fixture.model_signature);
  assert.equal((await post(changed.recovery.review)).status,409,'A second recovery must respect the revision check');
  await writeFile(`${directory}/latest.json`,JSON.stringify({...updated,model_signature:'d'.repeat(64),image_sha256:'e'.repeat(64)}));
  const recaptured=await load();assert(recaptured.stale);assert.equal(recaptured.recovery,null);assert.match(recaptured.recovery_reason,/image or coordinate frame changed/);
  await writeFile(`${directory}/review.json`, "corrupt");
  r = await fetch(`${base}/api/scenes?id=${id}`);
  assert.equal(r.status, 503);
  assert.equal(await readFile(`${directory}/review.json`, "utf8"), "corrupt");
  console.log(
    "Scene API: classification persistence, immutable predictions, geometry edits, grouping, history restore, stale-write/cycle/crossing/coverage rejection, origin and corruption preservation passed.",
  );
} finally {
  await rm(directory, { recursive: true, force: true });
}
