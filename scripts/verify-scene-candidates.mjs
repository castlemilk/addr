import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,rm,cp} from 'node:fs/promises';
import {randomBytes,createHash} from 'node:crypto';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008', token=randomBytes(8).toString('hex');
const id=`qa_candidate_${token}`, experiment=randomBytes(8).toString('hex');
const directory=`data/scenes/${id}`, candidateDirectory=`data/training/scene-proposals/${token}`, experimentDirectory=`data/experiments/scenes/${experiment}`;
let datasetDirectory,laterDatasetDirectory,laterDirectory,browser;
const write=async(file,value)=>writeFile(file,JSON.stringify(value));
const api=async(url,body,headers={})=>{
 const response=await fetch(`${base}${url}`,body?{method:'POST',headers:{'Content-Type':'application/json',...headers},body:JSON.stringify(body)}:{});
 return {status:response.status,data:await response.json()};
};
try {
 const snapshot=JSON.parse(await readFile('data/scenes/roof_training_250-30381/latest.json','utf8'));
 await mkdir(`${directory}/models`,{recursive:true});await mkdir(candidateDirectory,{recursive:true});await mkdir(experimentDirectory,{recursive:true});
 await cp(`data/scenes/roof_training_250-30381/${snapshot.image_sha256}.png`,`${directory}/${snapshot.image_sha256}.png`);
 const object=(name,x)=>({id:name,kind:'solar_panel',label:'Solar bank',ring:[[x,90],[x+40,90],[x+40,120],[x,120]],status:'candidate',geometry_reviewed:false,source:'sam3-text-onnx',score:.7,prompt:'solar panel array',
  model:{height_m:.15,base_m:0,height_source:'class-prior',height_verified:false,color:'#333333',width_m:4,length_m:5,area_m2:20,rotation_deg:0}});
 const proposals=[object('predicted-one',60),object('predicted-two',180),object('predicted-new',300)];
 Object.assign(snapshot,{id,title:'Candidate QA image',objects:proposals.slice(0,2),groups:[]});
 await write(`${directory}/latest.json`,snapshot);await write(`${directory}/models/${snapshot.model_signature}.json`,snapshot);
 const reference=[{...proposals[0],id:'saved-outline',status:'confirmed',geometry_reviewed:true,notes:'Preserve this reviewed outline.'},{...proposals[1],id:'saved-negative',status:'rejected'}];
 const review={revision:1,model_signature:snapshot.model_signature,objects:reference,groups:[],reviewed_classes:['solar_panel'],history:[]};
 await write(`${directory}/review.json`,review);
 const row={scene_id:id,split:'validation',spatial_group:'qa-region',image_sha256:snapshot.image_sha256,model_signature:snapshot.model_signature,pixel_to_meters:snapshot.pixel_to_meters,review,reference:[reference[0]]};
 const jsonl=JSON.stringify(row)+'\n',dataset=createHash('sha256').update(jsonl).digest('hex').slice(0,16);
 datasetDirectory=`data/training/scenes/${dataset}`;await mkdir(datasetDirectory);await writeFile(`${datasetDirectory}/dataset.jsonl`,jsonl);
 const provenance={dataset_version:dataset,experiment_run:experiment,kind:'solar_panel',prompt:'solar panel array'};
 await write(`${candidateDirectory}/model.json`,{model_kind:'regularized-logistic-proposal-quality',provenance,threshold:.5});
 const before={images:1,groups:1,references:1,matched:1,predictions:3,extra:2,missed:0},after={...before,predictions:1,matched:0,extra:1,missed:1};
 const report={candidate_id:token,status:'experimental-candidate',provenance,training_proposals:{usable:8,negative:19,images:10,groups:7},
  baseline:{splits:{train:before,validation:before},layers:[{...before,scene_id:id,split:'validation'}]},metrics:{splits:{train:after,validation:after},layers:[{...after,scene_id:id,split:'validation'}]}};
 await write(`${candidateDirectory}/report.json`,report);
 const result={...row,revision:1,prompts:{'solar panel array':{predictions:proposals,metrics:{matches:[{prediction_id:'predicted-one',reference_id:'saved-outline',iou:1}]}}}};delete result.review;
 await write(`${experimentDirectory}/${id}.json`,{run_id:experiment,result});
 const scores={candidate_id:token,scene_id:id,image_sha256:snapshot.image_sha256,scores:proposals.map((p,i)=>({id:p.id,quality_score:i===1?.7:.2,retained:i===1}))};
 await write(`${candidateDirectory}/${id}.json`,scores);
 const detailURL=`/api/scenes/candidates?candidate=${token}&scene=${id}`, pageURL=`${base}/scenes/candidates?candidate=${token}&scene=${id}`;
 let detail=await api(detailURL);assert.equal(detail.status,200,JSON.stringify(detail));
 assert.equal(detail.data.proposals[0].match.id,'saved-outline');assert.equal(detail.data.proposals[1].match.id,'saved-negative');assert.equal(detail.data.proposals[2].match,null);
 assert.equal(detail.data.proposals[0].frozen_match.id,'saved-outline');
 const body={candidate:token,scene:id,object:'predicted-new',revision:1,model_signature:snapshot.model_signature};
 assert.equal((await api('/api/scenes/candidates',body,{Origin:'https://elsewhere.invalid'})).status,403);
 assert.equal((await api('/api/scenes/candidates',{...body,revision:0})).status,409);
 assert.equal((await api('/api/scenes/candidates?candidate=../../bad&scene='+id)).status,400);
 const existing=await api('/api/scenes/candidates',{...body,object:'predicted-one'});assert.equal(existing.status,200);assert.equal(existing.data.created,false);assert.match(existing.data.review_url,/saved-outline/);
 assert.equal((await api('/api/scenes?id='+id)).data.review.revision,1,'Opening an existing reference must not save');
 await write(`${candidateDirectory}/${id}.json`,{...scores,image_sha256:'0'.repeat(64)});assert.equal((await api(detailURL)).status,409);await write(`${candidateDirectory}/${id}.json`,scores);
 await write(`${directory}/latest.json`,{...snapshot,pixel_to_meters:[[2,0],[0,2],[0,0]]});
 assert.equal((await api(detailURL)).data.compatible,false);assert.equal((await api('/api/scenes/candidates',body)).status,409);await write(`${directory}/latest.json`,snapshot);
 await write(`${directory}/review.json`,{...review,model_signature:'0'.repeat(64)});assert.equal((await api('/api/scenes/candidates',body)).status,409);await write(`${directory}/review.json`,review);
 const badReport=structuredClone(report);badReport.metrics.layers[0].split='test';await write(`${candidateDirectory}/report.json`,badReport);assert.equal((await api(detailURL)).status,503);await write(`${candidateDirectory}/report.json`,report);

 // A later check has its own frozen image/labels and must not replace the original evaluation.
 const laterId=id+'_later';laterDirectory=`data/scenes/${laterId}`;await mkdir(`${laterDirectory}/models`,{recursive:true});
 const laterSource=JSON.parse(await readFile('data/scenes/roof_training_250-30298/latest.json','utf8'));
 const laterSnapshot={...snapshot,id:laterId,title:'Later validation QA image',image_sha256:laterSource.image_sha256};
 await cp(`data/scenes/roof_training_250-30298/${laterSource.image_sha256}.png`,`${laterDirectory}/${laterSource.image_sha256}.png`);
 await write(`${laterDirectory}/latest.json`,laterSnapshot);await write(`${laterDirectory}/models/${laterSnapshot.model_signature}.json`,laterSnapshot);await write(`${laterDirectory}/review.json`,review);
 const laterRow={...row,scene_id:laterId,image_sha256:laterSource.image_sha256};
 const laterJsonl=JSON.stringify(laterRow)+'\n',laterDataset=createHash('sha256').update(laterJsonl).digest('hex').slice(0,16);
 laterDatasetDirectory=`data/training/scenes/${laterDataset}`;await mkdir(laterDatasetDirectory);await writeFile(`${laterDatasetDirectory}/dataset.jsonl`,laterJsonl);
 const laterLayer={scene_id:laterId,split:'validation',spatial_group:row.spatial_group,revision:1,image_sha256:laterSource.image_sha256,model_signature:snapshot.model_signature};
 const checkConfig={schema_version:1,candidate_id:token,candidate_model_sha256:createHash('sha256').update(await readFile(`${candidateDirectory}/model.json`)).digest('hex'),dataset_version:laterDataset,kind:'solar_panel',prompt:provenance.prompt,layers:[laterLayer]};
 const checkEncoded=JSON.stringify(checkConfig),checkId=createHash('sha256').update(checkEncoded).digest('hex').slice(0,16),checkDirectory=`${candidateDirectory}/checks/${checkId}`;await mkdir(checkDirectory,{recursive:true});await writeFile(`${checkDirectory}/config.json`,checkEncoded);
 const laterResult={...result,...laterLayer};laterResult.prompts=structuredClone(result.prompts);laterResult.prompts[provenance.prompt].metrics={...before,matches:result.prompts[provenance.prompt].metrics.matches};
 const artifact={check_id:checkId,result:laterResult,scores:scores.scores,filtered:after};const artifactText=JSON.stringify(artifact);await writeFile(`${checkDirectory}/${laterId}.json`,artifactText);
 const laterReport={check_id:checkId,candidate_id:token,before,after,layers:[{before:{...before,...laterLayer},after:{...after,...laterLayer}}],scene_files:{[laterId]:createHash('sha256').update(artifactText).digest('hex')}};
 await write(`${checkDirectory}/report.json`,laterReport);
 const laterURL=`/api/scenes/candidates?candidate=${token}&scene=${laterId}&check=${checkId}`;
 assert.equal((await api(laterURL)).status,200);assert.equal((await api(laterURL)).data.check_id,checkId);
 assert.equal((await api(laterURL)).data.proposals[0].frozen_match.id,'saved-outline');
 assert.equal((await api(detailURL)).status,200,'The original evaluation remains available');
 assert.equal((await api(detailURL+`&check=${checkId}`)).status,404,'Check scope cannot silently fall back to the original experiment');
 const laterBody={...body,scene:laterId,check:checkId};
 assert.equal((await api('/api/scenes/candidates',{...laterBody,check:0})).status,400);
 assert.equal((await api('/api/scenes/candidates',{...laterBody,object:'predicted-one'})).data.created,false);
 await writeFile(`${checkDirectory}/${laterId}.json`,artifactText+' ');assert.equal((await api(laterURL)).status,409);await writeFile(`${checkDirectory}/${laterId}.json`,artifactText);
 await writeFile(`${laterDatasetDirectory}/dataset.jsonl`,laterJsonl+' ');assert.equal((await api(laterURL)).status,409);await writeFile(`${laterDatasetDirectory}/dataset.jsonl`,laterJsonl);

 browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1440,height:1000});
 let mode='live',failPost=true,held=null,hold=true;
 await page.setRequestInterception(true);
 page.on('request',request=>{void(async()=>{
  const url=new URL(request.url());if(url.pathname!=='/api/scenes/candidates')return request.continue();
  if(request.method()==='POST'&&failPost){failPost=false;return request.respond({status:403,contentType:'application/json',body:JSON.stringify({error:'Editing access is required to add this draft.'})});}
  if(request.method()==='GET'&&!url.search&&mode==='empty')return request.respond({status:200,contentType:'application/json',body:JSON.stringify({candidates:[],unavailable:0})});
  if(request.method()==='GET'&&url.search){
   if(hold){held=request;return;}
   if(mode==='error')return request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Candidate data is unavailable. Please retry.'})});
   if(mode==='stale')return request.respond({status:200,contentType:'application/json',body:JSON.stringify({...detail.data,compatible:false,annotations:[],proposals:detail.data.proposals.map(p=>({...p,match:null}))})});
  }
  return request.continue();
 })().catch(e=>errors.push(e.message));});
 const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
 const ready=async()=>page.waitForSelector('.candidate-image');
 await page.goto(pageURL,{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>document.body.textContent.includes('Loading candidate outlines'));
 for(let i=0;i<40&&!held;i++)await new Promise(resolve=>setTimeout(resolve,50));assert(held);hold=false;await held.continue();await ready();
 assert.equal((await page.$$('path[data-candidate-object]')).length,3);
 assert.match(await page.$eval('.candidate-assessment:not(.candidate-later-warning)',e=>e.textContent),/Loses labelled objects/);
 assert.match(await page.$eval('.candidate-loss',e=>e.textContent),/Filtered a labelled object/);
 await button('Inspect lost matches');assert.equal(await page.$eval('.candidate-inspector select',e=>e.value),'filtered');
 assert.equal(await page.$eval('.candidate-selection h3',e=>e.textContent),'Proposal 1');
 assert.match(await page.title(),/Model candidates/);
 assert.match(await page.$eval('.candidate-selection a',e=>e.href),/saved-outline/);
 await page.click('.candidate-image-controls label:last-child input');assert.equal((await page.$$('.candidate-image path[stroke="white"]')).length,1);
 await page.select('.candidate-inspector select','filtered');assert.equal((await page.$$('path[data-candidate-object]')).length,2);
 await page.$eval('path[data-candidate-object="predicted-new"]',e=>e.focus());await page.keyboard.press('Enter');
 assert.equal(await page.$eval('.candidate-selection h3',e=>e.textContent),'Proposal 3');
 const returnURL=page.url();assert.equal(new URL(returnURL).searchParams.get('proposal'),'predicted-new');assert.equal(new URL(returnURL).searchParams.get('show'),'filtered');
 await button('Focus selected');assert.notEqual(await page.$eval('.candidate-image',e=>e.getAttribute('viewBox')),'0 0 512 512');await button('Whole image');
 await button('Add as review draft');await page.waitForFunction(()=>document.body.textContent.includes('Editing access is required'));
 assert.equal((await api('/api/scenes?id='+id)).data.review.revision,1);
 await page.setViewport({width:390,height:844});
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Candidate workflow fits mobile');
 await page.screenshot({path:'/tmp/addr-candidates-mobile.png',fullPage:true});
 await Promise.all([page.waitForNavigation({waitUntil:'domcontentloaded'}),button('Add as review draft')]);
 assert.equal(new URL(page.url()).pathname,'/scenes');assert.equal(new URL(page.url()).searchParams.get('object'),`candidate-${token}-predicted-new`);
 await page.waitForFunction((scene,object)=>new URL(location.href).searchParams.get('scene')===scene &&
  new URL(location.href).searchParams.get('object')===object && document.body.textContent.includes('Candidate QA image'),{},id,`candidate-${token}-predicted-new`);
 const saved=(await api('/api/scenes?id='+id)).data.review;
 assert.equal(saved.revision,2);assert.equal(saved.objects.length,3);assert.deepEqual(saved.objects.slice(0,2),reference.map(o=>({...o,model:saved.objects.find(s=>s.id===o.id).model})));assert.deepEqual(saved.reviewed_classes,[]);
 assert.equal(saved.objects[2].status,'needs_adjustment');assert.equal(saved.objects[2].geometry_reviewed,false);assert.equal(saved.objects[2].model.height_verified,false);assert.equal(saved.history.length,1);
 assert.deepEqual((await api('/api/scenes?id='+id)).data.model,snapshot,'Imports preserve the model');
 const repeated=await api('/api/scenes/candidates',body);assert.equal(repeated.status,200);assert.equal(repeated.data.created,false);assert.equal((await api('/api/scenes?id='+id)).data.review.revision,2);
 // Canonical edit/save/reload keeps a correction and allows the next training export to see it.
 const corrected=structuredClone(saved);corrected.objects[2].ring[0][0]-=2;corrected.objects[2].notes='Corrected by QA';
 const correction=await api('/api/scenes',{id,review:corrected});assert.equal(correction.status,200);assert.equal(correction.data.review.revision,3);
 assert.equal((await api(detailURL)).data.proposals[2].match.id,saved.objects[2].id);
 await page.setViewport({width:1440,height:1000});await page.goto(returnURL,{waitUntil:'domcontentloaded'});await ready();
 assert.equal(await page.$eval('.candidate-selection h3',e=>e.textContent),'Proposal 3');
 assert.match(await page.$eval('.candidate-selection a',e=>e.href),new RegExp(`candidate-${token}-predicted-new`));
 await page.screenshot({path:'/tmp/addr-candidates-desktop.png',fullPage:true});
 await button('Inspect later regression');await page.waitForFunction(scene=>new URL(location.href).searchParams.get('scene')===scene && document.querySelector('.candidate-scene-heading h2')?.textContent==='Later validation QA image',{},laterId);
 assert.equal(new URL(page.url()).searchParams.get('check'),checkId);assert.match(await page.$eval('.candidate-caveat',e=>e.textContent),/Weights and threshold stayed fixed/);
 assert.match(await page.$eval('.candidate-selection a',e=>e.href),new RegExp(`${laterId}.*saved-outline`));
 await page.select('.candidate-filters label:nth-child(2) select','');await page.waitForFunction(()=>document.querySelector('.candidate-scene-heading h2')?.textContent==='Candidate QA image');
 assert.equal(new URL(page.url()).searchParams.has('check'),false);
 const addedLater=await api('/api/scenes/candidates',laterBody);assert.equal(addedLater.status,200);assert.equal(addedLater.data.created,true);
 const laterSaved=(await api('/api/scenes?id='+laterId)).data.review;assert.equal(laterSaved.revision,2);assert.match(laterSaved.objects[2].notes,new RegExp(checkId));assert.equal(laterSaved.objects[2].geometry_reviewed,false);
 assert.equal((await api('/api/scenes/candidates',laterBody)).data.created,false,'Retries return the existing later-check draft');
 assert.equal((await api('/api/scenes?id='+id)).data.review.revision,3,'Later imports cannot change the original image review');
 mode='error';await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('[role="alert"]');mode='live';await button('Retry image');await ready();
 mode='stale';await page.reload({waitUntil:'domcontentloaded'});await ready();assert(await page.$eval('.candidate-selection button',e=>e.disabled));
 mode='empty';await page.reload({waitUntil:'domcontentloaded'});await page.waitForFunction(()=>document.body.textContent.includes('No model candidates available'));
 assert.deepEqual(errors,[]);
 console.log('Model candidate API and UI: provenance, split/frame gates, saved/rejected matches, revisions, imports, retries, duplicate prevention, edit/save, keyboard, loading/error/empty/stale states and mobile passed.');
} finally {
 if(browser)await browser.close();
 for(const p of [directory,laterDirectory,candidateDirectory,experimentDirectory,datasetDirectory,laterDatasetDirectory].filter(Boolean))await rm(p,{recursive:true,force:true});
}
