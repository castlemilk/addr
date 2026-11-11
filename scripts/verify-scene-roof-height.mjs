import assert from 'node:assert/strict';
import {readFile, writeFile, mkdir, cp, rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';

const base=process.env.APP_URL||'http://localhost:3008', id=`qa_roof_height_${Date.now()}`, directory=`data/scenes/${id}`;
const source=JSON.parse(await readFile('data/scenes/roof_training_250-25218/latest.json','utf8'));
const matrix=source.pixel_to_meters, xyz=(x,y,z)=>[x*matrix[0][0]+y*matrix[1][0]+matrix[2][0],x*matrix[0][1]+y*matrix[1][1]+matrix[2][1],z];
const roof={schema_version:1,roof_type:'gabled',vertices:[[210,210,4],[300,210,4],[300,290,4],[210,290,4],[255,210,6],[255,290,6]].map(p=>xyz(...p)),faces:[[0,4,5,3],[4,1,2,5]],edges:[{a:4,b:5,kind:'ridge'}]};
const building={id:'building',kind:'building',label:'Roof point QA',ring:[[210,210],[300,210],[300,290],[210,290]],status:'candidate',geometry_reviewed:false,visibility:'full',source:'test',roof_annotation:roof,
 model:{height_m:4,base_m:1,height_source:'class-prior',height_verified:false,width_m:8,length_m:9,area_m2:72,rotation_deg:0,color:'#b06d48'}};
const car={...building,id:'car',kind:'car',label:'Other object',roof_annotation:null,model:{...building.model,height_m:1.7}};
const model={...source,id,title:'3D roof point QA',groups:[],objects:[building,car]};
await mkdir(directory);await mkdir('/tmp/addr-roof-height',{recursive:true});
await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-25218/${source.image_sha256}.png`,`${directory}/${source.image_sha256}.png`);
let browser,page;
try {
 const seed=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{revision:0,model_signature:model.model_signature,groups:[],reviewed_classes:['building','car'],objects:model.objects.map(o=>({...o,status:'confirmed',geometry_reviewed:true}))}})});
 assert.equal(seed.status,200,await seed.clone().text());
 browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox']});
 page=await browser.newPage();await page.setViewport({width:1280,height:720});
 const errors=[];let posts=0,failSave=false;
 page.on('pageerror',error=>errors.push(error.message));
 await page.setRequestInterception(true);
 page.on('request',request=>{
  if(request.method()==='POST'&&new URL(request.url()).pathname==='/api/scenes') {
   posts++;
   if(failSave){failSave=false;void request.respond({status:503,contentType:'application/json',body:JSON.stringify({error:'Roof QA save failure. Retry the saved draft.'})});return;}
  }
  void request.continue();
 });
 const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
 const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
 const ready=()=>page.waitForFunction(()=>{const h=document.querySelector('.scene-height-handle');return h&&!h.hidden&&h.style.top;});
 const point=async index=>{await page.select('select[aria-label="3D edit handle"]',String(index));await ready();};
 const expectHeight=value=>page.waitForFunction(v=>Math.abs(Number(document.querySelector('input[aria-label="3D roof point height (m)"]')?.value)-v)<.001,{},value);
 const value=()=>page.$eval('.scene-height-handle',e=>Number(e.getAttribute('aria-valuenow')));
 const center=()=>page.$eval('.scene-height-handle',e=>{e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};});
 const save=async revision=>{await button('Save scene');await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`),{},revision);};
 const typeHeight=async(label,value)=>{const input=await page.$(`input[aria-label="${label}"]`);await input.focus();await input.evaluate(e=>e.select());await input.press('Backspace');await input.type(String(value));await input.press('Tab');};
 const assertSaved=async(height,revision)=>{
  const data=await load(),o=data.review.objects.find(o=>o.id==='building');assert.equal(data.review.revision,revision);
  assert.deepEqual(o.roof_annotation.vertices.map(p=>p[2]),[4,4,4,4,height,height]);
  assert.deepEqual(o.roof_annotation.vertices.map(p=>p.slice(0,2)),roof.vertices.map(p=>p.slice(0,2)));
  assert.equal(o.model.height_m,4);assert.equal(o.model.base_m,1);assert.equal(o.model.height_verified,false);
  assert.equal(o.status,'confirmed');assert(o.geometry_reviewed);assert.deepEqual(data.review.reviewed_classes,['building','car']);
  assert.deepEqual(o.ring,building.ring);assert.deepEqual(data.model,model);
 };
 await page.goto(`${base}/scenes?scene=${id}&kind=building&status=all&object=building`,{waitUntil:'networkidle2'});
 await button('3D scene');await ready();await point(4);await expectHeight(6);
 assert.equal(await page.$eval('.scene-height-handle',e=>e.getAttribute('aria-label')),'3D roof point 5 height handle');
 assert(await page.$eval('.scene-roof-ridge-highlight',e=>e.getAttribute('d').includes('L')),'Connected ridge must be highlighted');
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),'Selecting a roof point is not an edit');
 let p=await center();await page.mouse.move(p.x,p.y);await page.mouse.down();await page.mouse.move(p.x,p.y-30,{steps:6});
 const preview=await value();assert(preview>6);await expectHeight(6);
 assert(await page.$eval('select[aria-label="3D edit handle"]',e=>e.matches(':disabled')));
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled));assert.equal(posts,0);
 await page.mouse.up();await expectHeight(preview);await button('Undo edit');await expectHeight(6);
 assert(await page.$eval('button::-p-text(Undo edit)',e=>e.disabled),'A point drag is one undo step');
 p=await center();await page.mouse.move(p.x,p.y);await page.mouse.down();await page.mouse.move(p.x,p.y-80,{steps:8});
 assert(await page.$eval('.scene-height-handle',e=>!e.hidden&&document.activeElement===e),'Dragging past the viewport edge must retain focus so Escape can cancel');
 await page.keyboard.press('Escape');await page.mouse.up();await expectHeight(6);
 await page.waitForFunction(()=>Number(document.querySelector('.scene-height-handle')?.getAttribute('aria-valuenow'))===6);
 await page.focus('.scene-height-handle');await page.keyboard.press('ArrowUp');await expectHeight(6.1);
 failSave=true;await button('Save scene');await page.waitForFunction(()=>document.body.textContent.includes('Roof QA save failure'));
 await expectHeight(6.1);assert.equal((await load()).review.revision,1);
 await save(2);await assertSaved(6.1,2);
 await button('Prediction');assert.equal(await page.$('select[aria-label="3D edit handle"]'),null);assert.equal(await page.$('.scene-height-handle'),null);
 await button('Your edits');await ready();await expectHeight(6.1);
 await typeHeight('3D roof point height (m)',6.65);await expectHeight(6.65);await save(3);await assertSaved(6.65,3);
 await page.reload({waitUntil:'networkidle2'});await button('3D scene');await point(5);await expectHeight(6.65);
 await button('Edit roof apexes');await page.select('select[aria-label="Select roof point"]','4');
 assert.equal(Number(await page.$eval('input[aria-label="Apex height (m)"]',e=>e.value)),6.65);
 await typeHeight('Apex height (m)',7);await save(4);await assertSaved(7,4);await expectHeight(7);
 await button('Close roof points');
 await point(0);await page.focus('.scene-height-handle');await page.keyboard.down('Shift');await page.keyboard.press('ArrowUp');await page.keyboard.up('Shift');await expectHeight(5);
 await page.waitForFunction(()=>document.body.textContent.includes('Roof slopes need planar adjustment'));
 await button('Undo edit');await expectHeight(4);await point(4);
 await page.click('input[aria-label="Show Buildings"]');assert.equal(await page.$('.scene-height-handle'),null);await page.click('input[aria-label="Show Buildings"]');await ready();
 await page.select('select[aria-label="Object layer filter"]','car');await ready();assert.equal(await page.$('select[aria-label="3D edit handle"]'),null);assert.equal(await value(),1.7);
 await page.select('select[aria-label="Object layer filter"]','building');await ready();await point(4);
 await page.setViewport({width:390,height:844,isMobile:true,hasTouch:true});await page.waitForSelector('.scene-card');await button('3D scene');await point(4);
 const beforeTouchPosts=posts;p=await center();const cdp=await page.createCDPSession();
 const touch=(type,point)=>cdp.send('Input.dispatchTouchEvent',{type,touchPoints:point?[{x:point.x,y:point.y,id:1}]:[]});
 await touch('touchStart',p);await touch('touchMove',{x:p.x,y:p.y-25});const touchValue=await value();assert(touchValue>7);await touch('touchEnd');await expectHeight(touchValue);
 assert.equal(posts,beforeTouchPosts);await button('Undo edit');await expectHeight(7);
 p=await center();await touch('touchStart',p);await touch('touchMove',{x:p.x,y:p.y-20});await touch('touchCancel');await expectHeight(7);
 assert(await page.$eval('button::-p-text(Save scene)',e=>e.disabled));assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 await page.screenshot({path:'/tmp/addr-roof-height/mobile.png',fullPage:true});
 await page.setViewport({width:1280,height:720,isMobile:false,hasTouch:false});await page.waitForSelector('.scene-card');await button('3D scene');await point(4);await center();
 await page.screenshot({path:'/tmp/addr-roof-height/desktop.png'});await assertSaved(7,4);
 assert.deepEqual(JSON.parse(await readFile(`${directory}/latest.json`,'utf8')),model);assert.deepEqual(errors,[]);
 console.log('3D roof points: connected ridges, mouse/touch preview, one-step undo, cancellation, keyboard/numeric height, failed-save retry, save/reload, aerial-editor agreement, independent outline approvals, planarity feedback, read-only/hidden gates and mobile passed.');
} catch(error) {
 if(page){console.error(await page.$eval('body',e=>e.innerText.slice(-4500)));await page.screenshot({path:'/tmp/addr-roof-height/failure.png',fullPage:true});}
 throw error;
} finally {await browser?.close();await rm(directory,{recursive:true,force:true});}
