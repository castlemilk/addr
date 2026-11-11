import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir,cp,rm} from 'node:fs/promises';
import puppeteer from 'puppeteer-core';
const base=process.env.APP_URL||'http://localhost:3008',id=`qa_relationships_${Date.now()}`,directory=`data/scenes/${id}`;
const source='data/scenes/roof_training_250-30381',model={...JSON.parse(await readFile(`${source}/latest.json`,'utf8')),id};
// Freeze the observed mismatch rather than following later human corrections.
model.objects=model.objects.filter(o=>['building','solar_panel'].includes(o.kind));
const rings={
 '87074ebbf8c35ad7':[[280,191],[301,204],[284,240],[271,233],[265,240],[255,234]],
 '87cf76f07f5ba55e':[[289,269],[339,298],[333,310],[325,305],[318,315],[274,289]],
};
const review={revision:0,model_signature:model.model_signature,groups:model.groups,reviewed_classes:['solar_panel'],
 objects:model.objects.map(o=>rings[o.id]?{...o,ring:rings[o.id],label:o.id==='87074ebbf8c35ad7'?'Solar array — upper bank':'Solar array — lower bank',status:'confirmed',geometry_reviewed:true,visibility:'full'}:o)};
const panelId='87074ebbf8c35ad7',parentId='roof-30381';
assert(model.objects.some(o=>o.id===panelId));
await mkdir(directory);await writeFile(`${directory}/latest.json`,JSON.stringify(model));
await cp(`${source}/${model.image_sha256}.png`,`${directory}/${model.image_sha256}.png`);
const browser=await puppeteer.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1500,height:1080});
const click=async text=>(await page.waitForSelector(`button::-p-text(${text})`)).click();
const load=async()=>(await fetch(`${base}/api/scenes?id=${id}`)).json();
const selected=async object=>page.waitForFunction(object=>new URL(location.href).searchParams.get('object')===object,{},object);
const save=async revision=>{await click('Save scene');await page.waitForFunction(r=>document.body.textContent.includes(`Saved revision ${r}`),{},revision);};
const type=async(selector,text)=>{const field=await page.$(selector);await field.focus();await field.evaluate(e=>e.select());await field.press('Backspace');await field.type(text);await field.press('Tab');};
const coverage=()=>page.$$eval('.scene-support-metrics dd',es=>es.map(e=>e.textContent));
try{
 const seed=await fetch(`${base}/api/scenes`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,review:{...review,revision:0,history:undefined}})});
 assert.equal(seed.status,200,await seed.clone().text());
 await page.goto(`${base}/scenes?scene=${id}&kind=solar_panel&status=confirmed&object=${panelId}`,{waitUntil:'networkidle2'});
 await page.waitForSelector('.scene-support-metrics');assert.deepEqual(await coverage(),['99.3%','99.3%']);
 assert(await page.$eval('.scene-relationships [role="status"]',e=>e.textContent.includes('0.07 m²')));
 const options=await page.$$eval('.scene-relationships option',es=>es.map(e=>({value:e.value,text:e.textContent})));
 assert.equal(options.length,6);assert.equal(options[1].value,parentId);assert(options.slice(1).every(o=>model.objects.find(m=>m.id===o.value)?.kind==='building'));
 await click('Compare');await click('Inspect parent');await selected(parentId);
 assert(await page.$eval('.scene-card',e=>e===document.activeElement&&e.getBoundingClientRect().top>=-1&&e.getBoundingClientRect().top<innerHeight),'Related navigation must bring the image into view and move keyboard focus');
 assert.equal(new URL(page.url()).searchParams.get('kind'),null);assert.equal(new URL(page.url()).searchParams.get('status'),'all');
 assert.equal(await page.$$eval('.scene-attached button',es=>es.length),2);
 const parentOptions=await page.$$eval('.scene-relationships option',es=>es.map(e=>e.value));assert(!parentOptions.includes(panelId),'Parent choices must exclude attached descendants');
 await type('.scene-inspector > fieldset input','Parent draft retained');
 await click('Return to Solar array');await selected(panelId);assert.equal(new URL(page.url()).searchParams.get('kind'),'solar_panel');assert.equal(new URL(page.url()).searchParams.get('status'),'confirmed');
 assert(await page.$eval('button::-p-text(Compare)',e=>e.getAttribute('aria-pressed')==='true'));
 await click('Inspect parent');await selected(parentId);assert.equal(await page.$eval('.scene-inspector > fieldset input',e=>e.value),'Parent draft retained');
 await click('Return to Solar array');await selected(panelId);await click('Discard draft');
 await page.select('.scene-relationships select','086692803f9f8861');await page.waitForFunction(()=>document.querySelector('.scene-support-metrics dd')?.textContent==='0.0%');
 await save(2);await page.reload({waitUntil:'networkidle2'});await page.waitForSelector('.scene-support-metrics');assert.deepEqual(await coverage(),['0.0%','0.0%']);
 let data=await load();assert.equal(data.review.objects.find(o=>o.id===panelId).parent_id,'086692803f9f8861');assert(data.review.objects.find(o=>o.id===panelId).geometry_reviewed);assert.deepEqual(data.review.reviewed_classes,review.reviewed_classes);assert.deepEqual(data.model,model);
 await click('Prediction');await page.waitForFunction(()=>document.querySelector('.scene-relationships select')?.matches(':disabled'));assert.equal(await page.$eval('.scene-relationships select',e=>e.value),parentId);
 await click('Inspect parent');await selected(parentId);assert(await page.$eval('.scene-relationships select',e=>e.matches(':disabled')));await click('Return to Solar array');await selected(panelId);await click('Your edits');
 await page.select('.scene-relationships select',parentId);await save(3);await page.reload({waitUntil:'networkidle2'});await page.waitForSelector('.scene-support-metrics');assert.deepEqual(await coverage(),['99.3%','99.3%']);
 await page.select('.scene-relationships select','');await page.waitForFunction(()=>document.querySelector('.scene-relationships [role="status"]')?.textContent.includes('Choose a building'));await click('Undo edit');
 await click('Inspect parent');await selected(parentId);await click('Solar array — lower bank');await selected('87cf76f07f5ba55e');await page.waitForFunction(()=>document.querySelector('.scene-support-metrics dd')?.textContent==='92.3%');
 await click('Return to Solar array — upper bank');await selected(panelId);
 await page.setViewport({width:390,height:844});await page.$eval('.scene-relationships',e=>e.scrollIntoView());await page.screenshot({path:'/tmp/addr-roof-support/relationships-mobile.png'});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
 assert.deepEqual(errors,[]);console.log('Scene relationships: real outline/face gaps, ranked building parents, descendant exclusion, filtered parent/child navigation, draft retention, prediction immutability, reparent save/reload, preserved 2D approvals, missing parent and mobile passed.');
}catch(error){console.error(await page.$eval('body',e=>e.innerText.slice(-4500)));await page.screenshot({path:'/tmp/addr-roof-support/relationships-failure.png',fullPage:true});throw error;}
finally{await browser.close();await rm(directory,{recursive:true,force:true});}
