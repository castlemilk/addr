import assert from "node:assert/strict";
import {mkdir,readFile,writeFile,rm} from "node:fs/promises";
const base=process.env.APP_URL||"http://localhost:3008";
const id=`qa_scene_address_${Date.now()}`,directory=`data/scenes/${id}`;
await mkdir(`${directory}/models`,{recursive:true});
try {
  const model=JSON.parse(await readFile("data/scenes/roof_training_250-29790/latest.json","utf8"));
  model.id=id;
  for(const object of model.objects)delete object.address_id;
  await writeFile(`${directory}/latest.json`,JSON.stringify(model));
  await writeFile(`${directory}/models/${model.model_signature}.json`,JSON.stringify(model));
  const get=async()=>{const r=await fetch(`${base}/api/scenes?id=${id}`);assert.equal(r.status,200);return r.json();};
  const post=review=>fetch(`${base}/api/scenes`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id,review})});
  const review={revision:0,model_signature:model.model_signature,objects:structuredClone(model.objects),groups:model.groups,reviewed_classes:[]};
  const roof=review.objects.find(o=>o.kind==="building");
  assert(roof);
  for(const invalid of [0,-1,"29790",true,999999999999]) {
    const draft=structuredClone(review);draft.objects.find(o=>o.id===roof.id).address_id=invalid;
    assert.equal((await post(draft)).status,400,"Reject malformed and unknown address IDs");
  }
  roof.address_id=29790;
  const saved=await post(review);assert.equal(saved.status,200,await saved.clone().text());
  const data=await get();
  assert.equal(data.review.revision,1);
  assert.equal(data.review.objects.find(o=>o.id===roof.id).address_id,29790);
  assert.match(data.address_names[29790],/^27 CAMBRIDGE ROAD,/);
  assert.equal(JSON.stringify(data.model),JSON.stringify(model),"Linking a property never rewrites model predictions or capture identity");
  assert.equal(await readFile(`${directory}/latest.json`,"utf8"),JSON.stringify(model));
  assert.deepEqual(data.review.objects.find(o=>o.id===roof.id).ring,roof.ring,"An address link preserves the roof outline");
  assert.equal((await post(review)).status,409,"Address edits use the existing concurrency gate");
  const response=await fetch(`${base}/api/scenes/addresses?id=${id}`);assert.equal(response.status,200);
  const evidence=await response.json();
  assert.equal(evidence.revision,1);assert.equal(evidence.model_signature,model.model_signature);
  const match=evidence.objects.find(o=>o.object_id===roof.id);
  assert.equal(match.status,"linked");assert.equal(match.address_id,29790);
  assert.deepEqual((await get()).review,data.review,"Reading address evidence cannot alter feedback");
  console.log("Address API: canonical names, persisted links, invalid-ID rejection, immutable models/outlines, revision conflicts and read-only evidence passed.");
} finally {await rm(directory,{recursive:true,force:true});}
