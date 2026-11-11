import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm,mkdtemp} from 'node:fs/promises';
import {execFileSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import path from 'node:path';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_roof_feedback_${Date.now()}`,directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-25218/latest.json','utf8'));
const imageBytes=await readFile(`data/scenes/roof_training_250-25218/${source.image_sha256}.png`);
const matrix=source.pixel_to_meters,xyz=(x,y,z)=>[x*matrix[0][0]+y*matrix[1][0]+matrix[2][0],x*matrix[0][1]+y*matrix[1][1]+matrix[2][1],z];
const graph={schema_version:1,roof_type:'gabled',vertices:[[210,210,4],[300,210,4],[300,290,4],[210,290,4],[255,210,6],[255,290,6]].map(p=>xyz(...p)),faces:[[0,4,5,3],[4,1,2,5]],edges:[{a:4,b:5,kind:'ridge'}]};
const building={id:'building',kind:'building',label:'Roof feedback QA',ring:[[210,210],[300,210],[300,290],[210,290]],status:'candidate',geometry_reviewed:false,visibility:'full',source:'test',roof_annotation:graph,
 model:{height_m:4,base_m:1,height_source:'class-prior',height_verified:false,width_m:8,length_m:9,area_m2:72,rotation_deg:0,color:'#b06d48'}};
const model={...source,id,title:'Roof feedback QA',groups:[],objects:[building]};
await mkdir(directory);await mkdir('/tmp/addr-roof-feedback',{recursive:true});
await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-25218/${source.image_sha256}.png`,`${directory}/${source.image_sha256}.png`);
let browser,page,exportRoot;
try{
 const seed=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,groups:[],reviewed_classes:[],objects:[{...building,status:'confirmed',geometry_reviewed:true}]}})});
 assert.equal(seed.status,200,await seed.clone().text());
 browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
 page=await browser.newPage();await page.setViewport({width:1280,height:720});
 const errors=[];let posts=0,failSave=false,coverage=null;
 page.on('pageerror',e=>errors.push(e.message));await page.setRequestInterception(true);
 page.on('request',r=>{
  if(new URL(r.url()).pathname==='/api/scenes/image'&&new URL(r.url()).searchParams.get('id')==='roof-feedback-fixture'){
   void r.respond({status:200,contentType:'image/png',body:imageBytes});return;
  }
  if(new URL(r.url()).pathname==='/api/scenes/training'&&coverage){void r.respond({status:200,contentType:'application/json',body:JSON.stringify(coverage)});return;}
  if(r.method()==='POST'&&new URL(r.url()).pathname==='/api/scenes'){
   posts++;if(failSave){failSave=false;void r.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Roof feedback QA save failure. Retry.'})});return;}
  }
  void r.continue();
 });
 const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
 const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
 const field='[aria-label="Observed roof family"]',check='[aria-label="Rooflines checked against imagery"]';
 const state=()=>page.$eval('.scene-roof-feedback',el=>({family:el.querySelector('select').value,lines:el.querySelector('input').checked}));
 const expect=async(family,lines)=>{await page.waitForFunction((f,l)=>{const c=document.querySelector('.scene-roof-feedback');return c?.querySelector('select').value===f&&c.querySelector('input').checked===l;},{},family,lines);};
 const save=async revision=>{await button('Save scene');await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`),{},revision);};
 await page.goto(`${base}/scenes?scene=${id}&kind=building&status=all&object=building`,{waitUntil:'networkidle2'});
 await page.waitForSelector(field);assert.deepEqual(await state(),{family:'',lines:false});
 await page.select(field,'gabled');await page.click(check);await expect('gabled',true);assert.equal(posts,0);
 failSave=true;await button('Save scene');await page.waitForFunction(()=>document.body.textContent.includes('Roof feedback QA save failure'));
 assert.equal((await load()).review.revision,1);await expect('gabled',true);await save(2);
 let data=await load();assert.equal(data.review.objects[0].roof_feedback.family,'gabled');assert.equal(data.review.objects[0].model.height_verified,false);
 assert.equal(data.review.objects[0].status,'confirmed');assert.deepEqual(data.review.reviewed_classes,[]);assert.deepEqual(data.model,model);
 await page.reload({waitUntil:'networkidle2'});await expect('gabled',true);
 await button('Prediction');assert(await page.$eval(field,e=>e.matches(':disabled')));assert(await page.$eval(check,e=>e.matches(':disabled')));await expect('',false);
 await button('Compare');await expect('gabled',true);
 await button('3D scene');await page.select('[aria-label="3D edit handle"]','4');
 const input=await page.waitForSelector('[aria-label="3D roof point height (m)"]');await input.focus();await input.evaluate(e=>e.select());await input.press('Backspace');await input.type('7');await input.press('Tab');
 await expect('gabled',true);await save(3);
 await button('Edit roof apexes');await page.$eval('[aria-label="Roof point 5"]',e=>e.focus());await page.keyboard.press('ArrowRight');
 await expect('gabled',false);assert(await page.$eval('.scene-roof-feedback',e=>e.innerText.includes('Roof geometry changed')));
 assert.equal(posts,3,'Nudging a roof point must never trigger the global swipe/save shortcut');
 await button('Undo edit');await expect('gabled',true);
 await page.$eval('[aria-label="Roof point 5"]',e=>e.focus());await page.keyboard.press('ArrowRight');await expect('gabled',false);
 await page.click(check);await expect('gabled',true);await save(4);await button('Close roof points');await button('Aerial');
 await button('Edit outline');await page.$eval('[aria-label="Outline point 1"]',e=>e.focus());await page.keyboard.press('ArrowRight');await expect('',false);
 await button('Undo edit');await expect('gabled',true);await button('Finish editing');
 await page.select('.scene-inspector select','car');await page.waitForFunction(()=>!document.querySelector('.scene-roof-feedback'));await button('Undo edit');await expect('gabled',true);
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled));
 await page.$eval('.scene-roof-feedback',e=>e.scrollIntoView({block:'center'}));await page.screenshot({path:'/tmp/addr-roof-feedback/desktop.png'});
 await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});await page.waitForSelector(field);await expect('gabled',true);
 await page.$eval('.scene-roof-feedback',e=>e.scrollIntoView({block:'center'}));await page.select(field,'mixed');await expect('mixed',true);await save(5);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.$eval('.scene-roof-feedback',e=>e.scrollIntoView({block:'center'}));await page.screenshot({path:'/tmp/addr-roof-feedback/mobile.png'});
 data=await load();assert.equal(data.review.objects[0].roof_feedback.family,'mixed');assert.deepEqual(data.model,model);
 // Exercise real API-saved feedback through the real exporter in an isolated dataset root.
 exportRoot=await mkdtemp(path.join(tmpdir(),'addr-roof-feedback-'));
 const folder=path.join(exportRoot,'data/scenes/roof-feedback-fixture');await mkdir(folder,{recursive:true});await mkdir(path.join(exportRoot,'data/cohorts'),{recursive:true});
 await writeFile(path.join(folder,'latest.json'),JSON.stringify({...model,id:'roof-feedback-fixture'}));await writeFile(path.join(folder,'review.json'),JSON.stringify(data.review));
 await cp(`${directory}/${source.image_sha256}.png`,path.join(folder,`${source.image_sha256}.png`));await cp(`data/cohorts/${model.cohort}.json`,path.join(exportRoot,`data/cohorts/${model.cohort}.json`));
 const output=JSON.parse(execFileSync('scripts/ml/.venv/bin/python',['-c','import json,sys; from pathlib import Path; from scripts.ml.scene_training import export_scenes,scene_coverage; root=Path(sys.argv[1]); print(json.dumps({"manifest":export_scenes(root),"coverage":scene_coverage(root)}))',exportRoot],{encoding:'utf8'}));
 assert.equal(output.manifest.roof_feedback.objects,1);assert.deepEqual(output.manifest.class_exports,{});
 const target=JSON.parse(await readFile(output.manifest.roof_feedback.dataset,'utf8'));
 assert.equal(target.reference.family,'mixed');assert.equal(target.reference.height_supervision,'none');assert(target.reference.linework.vertices.every(p=>p.length===2));assert.equal(target.revision,5);
 coverage=output.coverage;
 await page.goto(`${base}/scenes/training?kind=building`,{waitUntil:'networkidle2'});await page.waitForSelector('.training-roof-feedback');
 await page.$eval('.training-roof-feedback',e=>e.scrollIntoView({block:'start'}));
 await page.waitForFunction(()=>[...document.querySelectorAll('.training-layer-list img')].every(e=>e.complete&&e.naturalWidth>0));
 await page.click('.training-roof-feedback summary');assert(await page.$eval('.training-roof-feedback',e=>e.innerText.includes('ready for export')));
 assert.equal(await page.$eval('.training-roof-feedback li a',e=>e.getAttribute('href')),coverage.roof_feedback.layers[0].review_url);
 assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.$eval('.training-roof-feedback',e=>e.scrollIntoView({block:'start'}));await page.screenshot({path:'/tmp/addr-roof-feedback/coverage-mobile.png'});
 await page.setViewport({width:1280,height:900,isMobile:false,hasTouch:false});await page.waitForSelector('.training-roof-feedback');await page.$eval('.training-roof-feedback',e=>e.scrollIntoView({block:'start'}));await page.screenshot({path:'/tmp/addr-roof-feedback/coverage-desktop.png'});
 assert.equal(posts,5);assert.deepEqual(errors,[]);
 console.log('Roof feedback: independent family/XY checks, no implicit labels, immutable prediction, failed-save retry, save/reload, Z independence, XY/outline invalidation, undo/recheck, class reset, mobile controls, canonical saved-review export, geographic gates and coverage links passed.');
}catch(e){if(page){console.error(await page.$eval('body',e=>e.innerText.slice(-4000)));await page.screenshot({path:'/tmp/addr-roof-feedback/failure.png',fullPage:true});}throw e;}
finally{await browser?.close();await rm(directory,{recursive:true,force:true});if(exportRoot)await rm(exportRoot,{recursive:true,force:true});}
