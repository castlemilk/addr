import assert from 'node:assert/strict';
import { readFile, writeFile, rm, access, mkdir, rename } from 'node:fs/promises';
const url = process.env.APP_URL || 'http://localhost:3008';
const cohort = `qa_review_${Date.now()}`;
const cohortPath = `data/cohorts/${cohort}.json`;
const feedbackPath = 'data/reviews/feedback.json';
let createdReview = false;
let fileExisted = true;
try { await access(feedbackPath); } catch { fileExisted = false; }
try {
  const records = JSON.parse(await readFile('public/address-roofs.json','utf8'));
  const record = records.find(r => r.boundary_source === 'crm');
  assert(record, 'Need a CRM roof for this integration test');
  await writeFile(cohortPath, JSON.stringify({ name: cohort, entries: [{ address_id: record.address_id }] }));
  const response = await fetch(`${url}/api/reviews?cohort=${cohort}`);
  assert.equal(response.status,200);
  const data = await response.json();
  const entry = data.entries[0];
  const payload = { address_id: entry.address_id, cohort, status:'needs_adjustment', notes:'Isolated API verification', boundary_verified:false, issues:['apex'], rejected_cuts:[[[0,0],[4,0]]], revision:0, boundary_signature:entry.boundary_signature };
  const post = data => fetch(`${url}/api/reviews`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  let result = await post({...payload,status:'approved'});
  assert.equal(result.status,400, await result.text());
  result = await post({...payload,boundary_signature:'old'});
  assert.equal(result.status,409);
  result = await post(payload);
  assert.equal(result.status,200, await result.clone().text());
  createdReview = true;
  const saved = await result.json();
  assert.deepEqual(saved.feedback.rejected_cuts,payload.rejected_cuts);
  assert.equal(saved.feedback.revision,1);
  result = await post(payload);
  assert.equal(result.status,409);
  result = await post({...payload,revision:1,rejected_cuts:[],issues:[],boundary_verified:true,status:'approved'});
  assert.equal(result.status,200, await result.clone().text());
  const reloaded = await (await fetch(`${url}/api/reviews?cohort=${cohort}`)).json();
  assert.equal(reloaded.entries[0].training_eligible,true);
  assert.deepEqual(reloaded.entries[0].feedback.rejected_cuts,[]);
  const vertices=[], indices=new Map();
  const faces=entry.parts.map(part=>(part.ring_meters||part.ring).map((xy,i)=>{
    const key=xy.map(v=>v.toFixed(5)).join(',');
    if(!indices.has(key)){indices.set(key,vertices.length);vertices.push([...xy,(part.ring_z||part.z)[i]]);}
    return indices.get(key);
  }));
  const annotation={schema_version:1,vertices,faces,edges:[],roof_type:entry.roof_type};
  const graphPayload={...payload,revision:2,annotation,model_signature:entry.model_signature};
  result=await post({...graphPayload,model_signature:'0'.repeat(64)});
  assert.equal(result.status,409,'Stale geometry must not replace a new model');
  result=await post(graphPayload);
  assert.equal(result.status,200,await result.clone().text());
  assert.equal((await result.json()).feedback.annotation_audit.passed,true);
  const edited=structuredClone(annotation);
  const apex=edited.vertices.reduce((best,p,i)=>p[2]>edited.vertices[best][2]?i:best,0);
  edited.vertices[apex][2]+=1;
  result=await post({...graphPayload,revision:3,annotation:edited});
  assert.equal(result.status,200,await result.clone().text());
  const editedSaved=await result.json();
  assert.equal(editedSaved.feedback.annotation.vertices[apex][2],edited.vertices[apex][2]);
  assert.equal(editedSaved.feedback.history.length,3);
  const afterEdit=(await (await fetch(`${url}/api/reviews?cohort=${cohort}`)).json()).entries[0];
  assert.deepEqual(afterEdit.parts,entry.parts,'Editing never overwrites the original prediction');
  assert.deepEqual(afterEdit.feedback.annotation,edited);
  const broken=structuredClone(annotation);broken.vertices[0][0]+=500;
  result=await post({...graphPayload,revision:4,annotation:broken,status:'approved',issues:[],rejected_cuts:[],boundary_verified:true,annotation_audit:{passed:true,issues:[]}});
  assert.equal(result.status,400,'The server must independently reject invalid approved geometry');
  assert.equal((await fetch(`${url}/api/reviews?cohort=..%2Fprivate`)).status,400);
  assert.equal((await fetch(`${url}/api/reviews/image?cohort=..%2Fprivate&type=site&address_id=1`)).status,400);
  console.log('Review API: geometry save/reload, immutable model, revision history, independent geometry audit, stale boundary/model/revision rejection and approval gates passed.');
} finally {
  await rm(cohortPath,{force:true});
  if (createdReview) {
    // Coordinate cleanup with real review writes, preserving every unrelated key.
    const lock = `${feedbackPath}.lock`;
    await mkdir(lock);
    try {
      const store = JSON.parse(await readFile(feedbackPath,'utf8'));
      for (const key of Object.keys(store)) if (key.startsWith(`${cohort}:`)) delete store[key];
      if (!fileExisted && !Object.keys(store).length) await rm(feedbackPath,{force:true});
      else {
        const temporary = `${feedbackPath}.${cohort}.tmp`;
        await writeFile(temporary,JSON.stringify(store,null,2));
        await rename(temporary,feedbackPath);
      }
    } finally { await rm(lock,{recursive:true,force:true}); }
  }
}
