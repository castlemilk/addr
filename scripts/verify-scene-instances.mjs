import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_instances_${Date.now()}`,directory=`data/scenes/${id}`;
const original=JSON.parse(await readFile('data/scenes/roof_training_250-29790/latest.json','utf8'));
const item=(id,kind,ring,parent_id)=>({id,kind,label:kind==='road'?'Road':'Car',ring,parent_id,status:'candidate',geometry_reviewed:false,source:'fixture',model:{...original.objects[0].model,height_m:kind==='road'?.08:1.5,height_source:'class-prior',height_verified:false}});
const model={...original,id,groups:[],objects:[item('road','road',[[50,50],[450,50],[450,300],[50,300]]),item('car','car',[[80,80],[120,80],[120,120],[80,120]],'road')]};
await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`data/scenes/roof_training_250-29790/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.setViewport({width:1400,height:1000});
const button=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const ready=async()=>{
 await page.waitForFunction(()=>document.body.textContent.includes('Replacement outlines are ready')||document.querySelector('.scene-instance-editor [role="alert"]')?.textContent);
 assert(!await page.$eval('body',e=>e.querySelector('.scene-instance-editor [role="alert"]')?.textContent));
};
const saved=async revision=>{
 await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`)||document.querySelector('.scene-status-space [role="alert"]')?.textContent,{},revision);
 assert(!await page.$eval('body',e=>e.querySelector('.scene-status-space [role="alert"]')?.textContent));
};
try {
 await page.goto(`${base}/scenes?scene=${id}&kind=road`,{waitUntil:'networkidle2'});
 await button('Correct outlines');
 for(const [label,value] of [['Cut start Y','120'],['Cut end Y','180']]) {
   const input=await page.$(`input[aria-label="${label}"]`);
   await input.focus();await input.evaluate(e=>e.select());await input.press('Backspace');await input.type(value);await input.press('Tab');
 }
 await button('Split outline');
 await page.waitForSelector('.scene-instance-editor [role="alert"]');
 assert.match(await page.$eval('.scene-instance-editor [role="alert"]',e=>e.textContent),/Extend the cut/);
 assert.equal(await page.$eval('input[aria-label="Cut end Y"]',e=>e.value),'180','A failed split retains the cut settings');
 assert.equal((await load()).review,null);
 // Place a real image-space cut with pointer events, then verify its coordinates.
 const svg=await page.$('.scene-instance-editor svg');await svg.scrollIntoView();
 for(const [x,y] of [[250,40],[250,310]]) {
   const at=await svg.evaluate((e,p)=>{const q=new DOMPoint(...p).matrixTransform(e.getScreenCTM());return {x:q.x,y:q.y};},[x,y]);
   await page.mouse.click(at.x,at.y);
 }
 assert(Math.abs(Number(await page.$eval('input[aria-label="Cut start X"]',e=>e.value))-250)<1);
 await button('Split outline');await ready();
 assert.equal((await load()).review,null,'Preview must stay a draft');
 await button('Save scene');await saved(1);
 let data=await load();
 const source=data.review.objects.find(o=>o.id==='road'),parts=data.review.objects.filter(o=>o.derived_from?.includes('road'));
 assert.equal(source.status,'superseded');assert.equal(parts.length,2);
 assert(parts.every(o=>o.status==='needs_adjustment'&&!o.geometry_reviewed&&!o.score));
 assert.equal(data.review.objects.find(o=>o.id==='car').parent_id,parts[0].id);
 assert.equal(data.review.objects.find(o=>o.id==='car').status,'needs_adjustment');
 assert.deepEqual(data.model,JSON.parse(JSON.stringify(model)));
 await page.reload({waitUntil:'networkidle2'});
 await page.select('select[aria-label="Object review filter"]','superseded');
 await page.waitForFunction(()=>document.body.textContent.includes('This outline was replaced'));
 assert.equal(await page.$eval('.scene-inspector fieldset',e=>e.disabled),true);
 await button(`View ${parts[0].label}`);
 await button('Correct outlines');await page.click('.scene-instance-editor button::-p-text(Merge)');
 await page.click('.scene-merge-choices input');
 await button('Merge selected outlines');await ready();
 assert.equal((await load()).review.revision,1,'Merge must stay a draft until save');
 await button('Undo edit');
 assert.equal(await page.$eval('button::-p-text(Save scene)',e=>e.disabled),true,'Undo should restore the saved split');
 await button('Correct outlines');await page.click('.scene-instance-editor button::-p-text(Merge)');await page.click('.scene-merge-choices input');
 await button('Merge selected outlines');await ready();await button('Save scene');await saved(2);
 data=await load();const merged=data.review.objects.find(o=>o.source==='reviewer-merge');
 assert(merged);assert.deepEqual(new Set(merged.derived_from),new Set(parts.map(o=>o.id)));
 assert.equal(data.review.objects.filter(o=>o.kind==='road'&&o.status!=='superseded').length,1);
 assert.equal(data.review.objects.find(o=>o.id==='car').parent_id,merged.id);
 assert.equal(data.review.history.length,1);
 const post=async body=>fetch(`${base}/api/scenes/instances`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const payload={id,review:data.review,operation:'split',ids:[merged.id],cut:[[250,40],[250,310]]};
 assert.equal((await post({...payload,review:{...data.review,revision:0}})).status,409);
 assert.equal((await post({...payload,ids:['road']})).status,400,'Replaced sources cannot be split again');
 assert.equal((await post({...payload,cut:[[20,20],[30,30]]})).status,422);
 assert.equal((await post({...payload,cut:[[Infinity,20],[30,30]]})).status,400);
 assert.equal((await post({...payload,operation:'merge',ids:[merged.id,'car']})).status,400);
 const roof=await fetch(`${base}/api/scenes/roof`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
   id,revision:data.review.revision,model_signature:model.model_signature,form:'gabled',rise:2,
   object:{...merged,kind:'building',model:{...merged.model,height_m:3}},
 })});
 assert.equal(roof.status,200,await roof.clone().text());
 const broken=structuredClone(data.review);broken.objects.find(o=>o.id==='road').replacement_ids=['missing'];
 const badSave=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:broken})});
 assert.equal(badSave.status,400);
 await page.reload({waitUntil:'networkidle2'});await button('3D scene');await page.waitForSelector('.scene-3d-canvas canvas');
 await page.screenshot({path:'/tmp/addr-instance-review.png'});
 assert.deepEqual(errors,[]);
 console.log('Scene instances: pointer split, draft/save/reload, immutable originals, replacement history, parent reassignment, merge, undo, stale/invalid rejection and 3D passed.');
} catch(error){console.error(await page.$eval('body',e=>e.innerText.slice(-5000)));await page.screenshot({path:'/tmp/addr-instance-review-failure.png',fullPage:true});throw error;}
finally{await browser.close();await rm(directory,{recursive:true,force:true});}
